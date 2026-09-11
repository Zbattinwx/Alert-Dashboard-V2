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
# TIME BUCKETS ARE ADAPTIVE, and that is not a refinement -- a fixed month
# bucket made this check blind to the thing it exists for.
#
# Presence-over-time was bucketed by MONTH. That is right for the archive, which
# spans years, and useless for LIVE collection, which spans days: on the 16,097
# rows collected 2026-09-08..10 the audit reported "2 findings" while FIVE
# features stepped inside that one month --
#
#     env_mlcape            0% -> 3% -> 88%
#     env_efhl              0% -> 3% -> 88%
#     max_wind_velocity_ms  0% -> 0% -> 52%
#     downburst_detected    0% -> 22% -> 100%
#     flash_rate_fpm        0% -> 24% -> 100%
#
# every one of them keyed to which BUILD was running that day. Training across
# that mix teaches the model the deploy schedule. The bucket must be finer than
# the thing being detected, so it is chosen from the span: months for an archive,
# days for a few weeks, hours for a single session.
BUCKET_BY_SPAN = ((90, 7), (3, 10), (0, 13))   # (min span days, ts prefix length)
# Buckets with too few rows to judge; avoids "0% present" from a bucket of 3.
MIN_BUCKET_ROWS = 50


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


def _feature_row():
    """The trainer's own record -> vector mapping, if importable.

    THE AUDIT MUST SEE WHAT THE MODEL SEES. A raw training record is not the
    feature vector: `feature_row` resolves sentinels on the way through, and
    auditing the record instead of the vector reports things the model never
    experiences.

    The case that proved it: `mean_cc == 0` is DUALPOL_SENTINEL. A zero there
    means cross-correlation, ZDR and min-CC were not computed for that cell at
    all, and `feature_row` turns all three into NaN. Read raw, those zeros look
    like a dual-pol field collapsing to zero with range -- 0% zero inside
    140 km, 56% beyond 200 -- and the audit dutifully reported three FAILs for
    a mechanism that already works correctly. Read through `feature_row`, the
    same rows are simply absent at long range, which is true, useful, and not
    a defect.
    """
    try:
        from scripts.train_rotation_model import feature_row  # type: ignore
        return feature_row
    except Exception:
        return None


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
        self.row_fn = _feature_row()
        self.rows = 0
        self.months: Counter = Counter()
        self.stamps: list[str] = []
        self._bucket_len: Optional[int] = None
        # per feature
        self.present: Counter = Counter()
        self.present_by_month: dict[str, Counter] = defaultdict(Counter)
        # keyed by the FULL ts while collecting; folded to buckets in report()
        self.present_by_bucket: dict[str, dict] = defaultdict(dict)
        self.rows_by_bucket: dict = {}
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
        # Keep the whole timestamp. The bucket width depends on the SPAN, and
        # the span is not known until every row has been read.
        self.stamps.append(ts)
        # ONCE PER ROW. This lived inside the per-feature loop, which made the
        # denominator 48x too large and every presence fraction uniformly tiny --
        # so no feature ever looked like it STEPPED, which is the whole check.
        self.rows_by_bucket[ts] = self.rows_by_bucket.get(ts, 0) + 1
        month = ts[:7] if len(ts) >= 7 else "unknown"
        self.months[month] += 1

        feats = rec.get("features") or {}
        if not isinstance(feats, dict):
            return
        self.seen_keys.update(feats.keys())
        # Resolve the record into the vector the model actually consumes, so
        # sentinels (DUALPOL_SENTINEL) become the NaN the trainer sees rather
        # than the 0.0 the file stores. See _feature_row.
        if self.row_fn is not None and self.declared:
            try:
                vec = self.row_fn(feats)
                if len(vec) == len(self.declared):
                    feats = dict(zip(self.declared, vec))
            except Exception:
                pass

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
            self.present_by_bucket[name][ts] = self.present_by_bucket[name].get(ts, 0) + (1 if v is not None else 0)
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
    def _bucket_width(self) -> int:
        """Timestamp prefix length to group by, chosen from the data's span.

        A month bucket cannot see a step that happens inside a month, and live
        collection produces exactly that. See BUCKET_BY_SPAN.
        """
        if self._bucket_len is not None:
            return self._bucket_len
        days = [s[:10] for s in self.stamps if len(s) >= 10]
        span_days = 0
        if days:
            lo, hi = min(days), max(days)
            try:
                from datetime import date
                span_days = (date.fromisoformat(hi) - date.fromisoformat(lo)).days
            except ValueError:
                span_days = 0
        for min_span, prefix in BUCKET_BY_SPAN:
            if span_days >= min_span:
                self._bucket_len = prefix
                break
        else:
            self._bucket_len = 10
        return self._bucket_len

    def _buckets(self) -> dict[str, int]:
        """Row counts per time bucket at the chosen width."""
        w = self._bucket_width()
        out: dict[str, int] = {}
        for ts, n in self.rows_by_bucket.items():
            key = ts[:w] if len(ts) >= w else "unknown"
            out[key] = out.get(key, 0) + n
        return out

    def _month_presence(self, name: str) -> list[tuple[str, float]]:
        """(bucket, fraction present), ascending, over buckets big enough to judge."""
        w = self._bucket_width()
        totals = self._buckets()
        present: dict[str, int] = {}
        for ts, n in self.present_by_bucket.get(name, {}).items():
            key = ts[:w] if len(ts) >= w else "unknown"
            present[key] = present.get(key, 0) + n
        out = []
        for b, total in sorted(totals.items()):
            if b == "unknown" or total < MIN_BUCKET_ROWS:
                continue
            out.append((b, present.get(b, 0) / total))
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
                    # ZERO-INFLATED FEATURES BREAK THE MEDIAN, and saying so
                    # wrongly is how an audit loses its authority. Once more
                    # than half a quintile is 0 the median IS 0 and the fold
                    # goes infinite, whatever the non-zero values are doing.
                    # llsd_max_shear reads "the far fifth is entirely zero" on
                    # a zero rate that only moves 31% -> 49% -- a mild range
                    # dependence, and evidence the physical-kernel fix WORKED,
                    # reported as though it were the original 50x collapse.
                    zn = sum(1 for v in near if v == 0) / len(near)
                    zf = sum(1 for v in far if v == 0) / len(far)
                    if (zn > 0.10 or zf > 0.10) and abs(zf - zn) < 0.50:
                        if abs(zf - zn) >= 0.15:
                            add("warn", name, "range_zero_rate",
                                f"zero {zn:.0%} within {ordered[q - 1][0]:.0f} km "
                                f"-> {zf:.0%} beyond {ordered[-q][0]:.0f} km; the "
                                "feature thins with range rather than scaling with it",
                                near_zero=zn, far_zero=zf)
                        # Judge the magnitudes on the values that exist.
                        near = [v for v in near if v != 0] or near
                        far = [v for v in far if v != 0] or far
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
            "buckets": dict(sorted(self._buckets().items())),
            "bucket_width": self._bucket_width(),
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
    buckets = rep.get("buckets") or {}
    if buckets:
        keys = [k for k in buckets if k != "unknown"]
        unit = {7: "months", 10: "days", 13: "hours"}.get(rep.get("bucket_width"), "buckets")
        if keys:
            print(f"span {min(keys)} .. {max(keys)}   ({len(keys)} {unit})")
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
