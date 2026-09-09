"""Does the feature matrix contain what it claims to contain?

WHY THIS EXISTS
---------------
Every data defect this project has hit was found the same way: a model trained
for hours, scored badly, and only then did somebody go looking. Or it was
noticed by accident while reading unrelated code. Not one was found by
something built to find it.

    llsd_max_shear was a measurement of RANGE, not rotation
        -> found by ablation, after training
    downburst / MARC / RIJ were computed every scan and never written to a row
        -> found because the wind models scored near chance, after training
    flash_rate is NaN for every archived row (no historical GLM feed)
        -> found while writing the re-derivation
    env_* nearly stapled tonight's atmosphere onto storms from 2024
        -> found by reading the code
    backfill --resume silently skipped 76 of 130 pairs
        -> found by reading output
    env_efhl does not exist in the RAP before ~2024-07
        -> found MID-RUN, 5 hours into a 30-hour re-derivation

The common defect is not any of those. It is that nothing ever checked whether
a column held what its name promised. This does.

WHAT IT CHECKS, AND WHY EACH ONE
--------------------------------
EMPTY / CONSTANT
    A column that is always missing, or always the same number, is not a
    feature. It is a name. The wind signatures were in FEATURE_NAMES and in the
    tracker and in nothing in between, and a model was asked to predict
    damaging wind with the wind fields withheld.

PRESENCE OVER TIME
    The one that matters most, and the cheapest to get wrong. `env_efhl` is
    absent from the RAP before mid-2024 and present after; `flash_rate` is
    absent from every archived row and present in every live one. The problem
    is NOT the missingness -- NaN is how a gradient-boosted tree is told "no
    value". The problem is that "is this NaN" becomes a clean linear function
    of the date, so the model can read the calendar off the feature vector and
    learn era, not meteorology. A feature whose presence steps from 0% to 100%
    at a point in time is a leak wearing a meteorologist's coat.

NUISANCE CORRELATION WITH RANGE
    Radar features are measured through a beam that widens with distance. A
    feature that tracks range is measuring the geometry of the observation
    rather than the storm. llsd_max_shear's median fell about 50x from inside
    10 km to beyond 150 km, and removing it IMPROVED held-out average
    precision by 13%. Range is not in the feature row, so it is reconstructed
    here from the row's lat/lon and its site.

DRIFT
    A feature whose distribution moves with time will not mean the same thing
    in the temporal holdout as it did in training. Sometimes legitimate (a
    genuinely changed detector), always worth knowing before it is blamed on
    the model.

LABELS
    Target base rates per period. A target with almost no positives in the
    holdout cannot be evaluated there, whatever the AUC says.

Everything is reported. Only the checks that make a matrix unfit to train on
fail the run, so this can gate training without becoming something people pass
--force to out of habit.

USAGE
    python scripts/audit_features.py data/training_merged.jsonl
    python scripts/audit_features.py data/x.jsonl --json report.json
    python scripts/audit_features.py data/x.jsonl --quiet   # exit code only
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Thresholds ─────────────────────────────────────────────────────────────
# A column present in fewer rows than this is reported but not fatal; some
# features are genuinely sparse (a TVS is rare).
SPARSE_WARN = 0.05
# Presence-over-time: the gap between the best and worst month's presence. A
# feature that is 100% present in one month and 0% in another is telling the
# model when the row was collected.
PRESENCE_STEP_FAIL = 0.80
PRESENCE_STEP_WARN = 0.40
# |r| between a feature and range-from-radar.
RANGE_CORR_FAIL = 0.50
RANGE_CORR_WARN = 0.30
# Fold-change in the MEDIAN between the near and far fifths of range. A feature
# that halves or doubles across the domain is reporting beam geometry; one that
# moves 5x or more is barely reporting anything else. llsd_max_shear moved ~50x.
RANGE_MEDIAN_FOLD_FAIL = 5.0
RANGE_MEDIAN_FOLD_WARN = 2.0
# Months with too few rows to judge; avoids "0% present" from a month with 3 rows.
MIN_MONTH_ROWS = 50


def _finite(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 30:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx, dy = x - mx, y - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _site_coords() -> dict[str, tuple[float, float]]:
    try:
        from backend.services.nexrad_sites import NEXRAD_SITES  # type: ignore
        return {k: (v["lat"], v["lon"]) for k, v in NEXRAD_SITES.items()
                if "lat" in v and "lon" in v}
    except Exception:
        return {}


def _feature_names() -> list[str]:
    try:
        from scripts.train_rotation_model import FEATURE_NAMES  # type: ignore
        return list(FEATURE_NAMES)
    except Exception:
        return []


def _optional_features() -> set[str]:
    try:
        from scripts.train_rotation_model import OPTIONAL_FEATURES  # type: ignore
        return set(OPTIONAL_FEATURES)
    except Exception:
        return set()


class Audit:
    def __init__(self, declared: list[str], optional: set[str]):
        self.declared = declared
        self.optional = optional
        self.sites = _site_coords()
        self.rows = 0
        self.months: Counter = Counter()
        # per feature
        self.present: Counter = Counter()
        self.present_by_month: dict[str, Counter] = defaultdict(Counter)
        self.values: dict[str, list[float]] = defaultdict(list)
        self.distinct: dict[str, set] = defaultdict(set)
        self.seen_keys: set[str] = set()
        # nuisance pairings, sampled to stay bounded on a multi-GB file
        self.range_pairs: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.time_pairs: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.no_range_rows = 0
        # labels
        self.labels: dict[str, Counter] = defaultdict(Counter)
        self.label_by_month: dict[str, Counter] = defaultdict(Counter)
        self.SAMPLE_CAP = 20000

    def add(self, rec: dict) -> None:
        self.rows += 1
        ts = str(rec.get("ts") or "")
        month = ts[:7] if len(ts) >= 7 else "unknown"
        self.months[month] += 1

        feats = rec.get("features") or {}
        if not isinstance(feats, dict):
            return
        self.seen_keys.update(feats.keys())

        rng = None
        lat, lon = _finite(rec.get("lat")), _finite(rec.get("lon"))
        site = rec.get("site")
        if lat is not None and lon is not None and site in self.sites:
            slat, slon = self.sites[site]
            rng = _haversine_km(slat, slon, lat, lon)
        else:
            self.no_range_rows += 1

        # epoch-ish ordinal for drift; the exact scale does not matter to r
        tord = None
        if len(ts) >= 10:
            try:
                y, m, d = int(ts[0:4]), int(ts[5:7]), int(ts[8:10])
                tord = y * 372 + m * 31 + d
            except ValueError:
                tord = None

        names = self.declared or sorted(feats)
        for name in names:
            raw = feats.get(name, None)
            v = _finite(raw)
            self.present_by_month[name][month] += 1 if v is not None else 0
            if v is None:
                continue
            self.present[name] += 1
            if len(self.values[name]) < self.SAMPLE_CAP:
                self.values[name].append(v)
            if len(self.distinct[name]) < 8:
                self.distinct[name].add(round(v, 6))
            if rng is not None and len(self.range_pairs[name]) < self.SAMPLE_CAP:
                self.range_pairs[name].append((rng, v))
            if tord is not None and len(self.time_pairs[name]) < self.SAMPLE_CAP:
                self.time_pairs[name].append((float(tord), v))

        for key in ("label",):
            val = rec.get(key)
            if val is not None:
                self.labels[key][bool(val)] += 1
                self.label_by_month[month][bool(val)] += 1
        hz = rec.get("hazard_labels")
        if isinstance(hz, dict):
            for k, val in hz.items():
                if val is not None:
                    self.labels[f"hazard:{k}"][bool(val)] += 1

    # ── Reporting ──────────────────────────────────────────────────────────
    def _month_presence(self, name: str) -> list[tuple[str, float]]:
        out = []
        for m, total in sorted(self.months.items()):
            if m == "unknown" or total < MIN_MONTH_ROWS:
                continue
            out.append((m, self.present_by_month[name][m] / total))
        return out

    def report(self) -> dict:
        findings: list[dict] = []

        def add(sev, feature, kind, msg, **extra):
            findings.append({"severity": sev, "feature": feature, "check": kind,
                             "message": msg, **extra})

        declared = self.declared or sorted(self.seen_keys)

        # Columns the trainer expects that the data has never heard of.
        for name in declared:
            if name not in self.seen_keys:
                sev = "warn" if name in self.optional else "fail"
                add(sev, name, "absent",
                    "declared in FEATURE_NAMES but the key is not present in ANY row")

        for name in declared:
            n = self.present[name]
            frac = n / self.rows if self.rows else 0.0

            if name not in self.seen_keys:
                continue
            if n == 0:
                sev = "warn" if name in self.optional else "fail"
                add(sev, name, "empty",
                    "key exists but every value is null/NaN - this is a name, not a feature")
                continue
            if len(self.distinct[name]) == 1:
                add("fail", name, "constant",
                    f"only ever takes the value {next(iter(self.distinct[name]))}")
            elif frac < SPARSE_WARN:
                add("info", name, "sparse", f"present in {frac:.1%} of rows")

            # Presence over time: the leakage check.
            mp = self._month_presence(name)
            if len(mp) >= 3:
                lo = min(p for _, p in mp)
                hi = max(p for _, p in mp)
                step = hi - lo
                if step >= PRESENCE_STEP_WARN:
                    lo_m = min(mp, key=lambda t: t[1])[0]
                    hi_m = max(mp, key=lambda t: t[1])[0]
                    # Report in DATE order, not severity order: "0% in 2026-04
                    # -> 100% in 2019-05" reads like time running backwards.
                    first, second = sorted([(lo_m, lo), (hi_m, hi)])
                    sev = "fail" if step >= PRESENCE_STEP_FAIL else "warn"
                    add(sev, name, "presence_step",
                        f"present in {first[1]:.0%} of {first[0]} rows but "
                        f"{second[1]:.0%} of {second[0]} rows; missingness encodes "
                        "WHEN the row was collected",
                        low=lo, high=hi, low_month=lo_m, high_month=hi_m)

            # Nuisance: range. TWO tests, because they catch different shapes.
            #
            # NOT for env_*. For a RADAR feature, distance from the radar means
            # beam geometry, and a feature that tracks it is measuring the
            # observation. For the near-storm ENVIRONMENT, distance from KILN
            # is a proxy for GEOGRAPHY -- CAPE really does differ across a
            # 200 km domain, and flagging that as a defect is the audit crying
            # wolf, which is how an audit gets --skip'd. Same for drift: the
            # archive is walked in date order, so "trend against time" over a
            # partial run is mostly SEASON, and SRH really is higher in spring.
            if name.startswith("env_"):
                continue
            pairs = self.range_pairs[name]
            if len(pairs) >= 30:
                r = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
                if r is not None and abs(r) >= RANGE_CORR_WARN:
                    sev = "fail" if abs(r) >= RANGE_CORR_FAIL else "warn"
                    add(sev, name, "range_correlation",
                        f"r={r:+.2f} against distance from the radar - may be "
                        "measuring the observation geometry, not the storm", r=r)

            # The one that matters, and the one a correlation misses.
            #
            # llsd_max_shear's median fell ~50x from inside 10 km to beyond
            # 150 km, and an ablation showed dropping it IMPROVED held-out
            # average precision by 13%. Its Pearson r against range is
            # nevertheless small, because the relationship is a steep monotonic
            # decay buried in enormous variance -- exactly the shape a linear
            # correlation is blind to. Comparing the MEDIAN of the near and far
            # quintiles is how the bug was actually found, so that is the test.
            if len(pairs) >= 200:
                ordered = sorted(pairs, key=lambda p: p[0])
                q = len(ordered) // 5
                near = sorted(v for _, v in ordered[:q])
                far = sorted(v for _, v in ordered[-q:])
                if near and far:
                    mn = near[len(near) // 2]
                    mf = far[len(far) // 2]
                    near_km = ordered[q - 1][0]
                    far_km = ordered[-q][0]
                    ratio = None
                    if mf != 0 and math.isfinite(mn / mf if mf else math.inf):
                        ratio = mn / mf
                    elif mn != 0:
                        ratio = math.inf
                    if ratio is not None and ratio > 0:
                        fold = max(ratio, 1 / ratio) if ratio != math.inf else math.inf
                        if fold >= RANGE_MEDIAN_FOLD_FAIL:
                            # A far-field median of exactly zero is the strongest
                            # form of this: the feature simply stops existing with
                            # distance, which is what llsd_max_shear did.
                            how = ("the far fifth is entirely zero"
                                   if fold == math.inf else f"{fold:.0f}x")
                            add("fail", name, "range_median_shift",
                                f"median {mn:.4g} within {near_km:.0f} km vs {mf:.4g} "
                                f"beyond {far_km:.0f} km ({how}) - this is closer "
                                "to a measurement of range than of the storm",
                                near_median=mn, far_median=mf,
                                fold=(None if fold == math.inf else fold))
                        elif fold >= RANGE_MEDIAN_FOLD_WARN:
                            add("warn", name, "range_median_shift",
                                f"median shifts {fold:.1f}x between the near and far "
                                f"fifths of range ({mn:.4g} -> {mf:.4g})",
                                near_median=mn, far_median=mf, fold=fold)

            # Drift.
            tp = self.time_pairs[name]
            if len(tp) >= 30:
                r = _pearson([p[0] for p in tp], [p[1] for p in tp])
                if r is not None and abs(r) >= 0.30:
                    add("warn", name, "drift",
                        f"r={r:+.2f} against time - the value itself trends across "
                        "the archive", r=r)

        return {
            "rows": self.rows,
            "months": dict(sorted(self.months.items())),
            "declared_features": len(declared),
            "features_seen": len(self.seen_keys & set(declared)) if self.declared else len(self.seen_keys),
            "rows_without_range": self.no_range_rows,
            "labels": {k: dict(v) for k, v in self.labels.items()},
            "findings": findings,
        }


def render(rep: dict, quiet: bool = False) -> None:
    if quiet:
        return
    print(f"\nrows {rep['rows']:,}   "
          f"features declared {rep['declared_features']}   "
          f"seen in data {rep['features_seen']}")
    months = rep["months"]
    if months:
        keys = [k for k in months if k != "unknown"]
        if keys:
            print(f"span {min(keys)} .. {max(keys)}   ({len(keys)} months)")
    if rep["rows_without_range"]:
        print(f"rows with no usable lat/lon/site (range checks skipped): "
              f"{rep['rows_without_range']:,}")

    if rep["labels"]:
        print("\nlabels")
        for k, counts in rep["labels"].items():
            pos = counts.get(True, 0) or counts.get("true", 0)
            tot = sum(counts.values())
            print(f"  {k:22s} {pos:>8,} / {tot:>9,}  ({pos / tot:.2%})" if tot else f"  {k}: none")

    order = {"fail": 0, "warn": 1, "info": 2}
    findings = sorted(rep["findings"], key=lambda f: (order.get(f["severity"], 9), f["feature"]))
    if not findings:
        print("\nno findings.")
        return
    print(f"\n{len(findings)} finding(s):")
    cur = None
    for f in findings:
        if f["severity"] != cur:
            cur = f["severity"]
            print(f"\n  [{cur.upper()}]")
        print(f"    {f['feature']:26s} {f['check']:18s} {f['message']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="training .jsonl to audit")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (a quick look)")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    audit = Audit(_feature_names(), _optional_features())
    bad = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                audit.add(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
            if args.limit and audit.rows >= args.limit:
                break
    if bad:
        print(f"warning: {bad} unparseable line(s)", file=sys.stderr)
    if not audit.rows:
        print("no rows", file=sys.stderr)
        return 2

    rep = audit.report()
    render(rep, args.quiet)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2), encoding="utf-8", newline="\n")
        if not args.quiet:
            print(f"\nreport -> {args.json}")

    fails = [f for f in rep["findings"] if f["severity"] == "fail"]
    if fails and not args.quiet:
        print(f"\nFAIL: {len(fails)} blocking finding(s). "
              "This matrix should not be trained on as-is.")
    elif not args.quiet:
        print("\nPASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
