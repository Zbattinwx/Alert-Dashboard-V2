"""Do the SOURCE files actually contain the fields we are about to derive from?

This is the cheap half of the answer to "why do we keep finding data problems
five hours into a thirty-hour job".

`audit_features.py` inspects a matrix that already exists, which means paying
for the derivation first. This asks the question BEFORE any of that, straight
off the GRIB index files: for every field the environment extractor needs, is
it present across the whole time range we intend to train on?

It costs seconds. It reads .idx text, never a GRIB message -- no decode, no
regrid, no download of any actual data.

THE BUG THAT MOTIVATED IT
-------------------------
`env_efhl` (effective-layer SRH) does not exist in the RAP before roughly
2024-07. It is present in every cycle after. We discovered this five hours into
a re-derivation, from 118 identical stderr lines:

    field :EFHL:surface: not in idx for rap.20240402/rap.t09z.awp130pgrbf01.grib2

Both files carry 355 idx lines, so it is a product change, not corruption. The
run would have completed a day later with one of twelve environmental features
NaN for the early period and populated for the late one -- and that is worse
than simply missing, because "is env_efhl null" then becomes a clean readout of
whether the row is from 2024 or 2026. The model can learn the calendar instead
of the atmosphere.

Ten seconds of probing would have caught it before the job started.

USAGE
    python scripts/audit_sources.py --start 2024-02-27 --end 2026-09-09
    python scripts/audit_sources.py --model rap --samples 16
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _env_field_ids() -> list[str]:
    """The RAP field ids the near-storm environment actually asks for."""
    try:
        from backend.services.storm_environment import ENV_FIELDS  # type: ignore
        return list(ENV_FIELDS)
    except Exception:
        return ["mlcape", "mucape", "mlcin", "shear06", "srh01", "efhl",
                "mllcl", "stp", "scp", "ship", "lapse75", "pwat"]


def _meso_field_ids() -> list[str]:
    try:
        from backend.services.mesoanalysis_service import MESO_FIELDS  # type: ignore
        return list(MESO_FIELDS)
    except Exception:
        return []


def _requirements(spec: dict) -> list:
    """Every GRIB record a field needs, as a list of idx matchers.

    A matcher is either a string (must appear in some idx line) or a tuple
    (every part must appear in the SAME line -- RRFS appends aerosol qualifiers
    so `:MASSDEN:8 m` alone matches smoke, dust and total).

    Three shapes exist in the registry and all three must be walked, or the
    audit reports a field missing when it is merely computed:
      * a plain field carries `idx`;
      * a DERIVED field carries `derive = (kind, name, [(idx, conv), ...])`,
        and needs every component -- a component may itself be a list, as the
        u/v pair for a shear magnitude is;
      * a time-aggregated field carries `timeagg` over a base `idx`.
    """
    req: list = []
    idx = spec.get("idx")
    if idx:
        req.append(idx)
    derive = spec.get("derive")
    if isinstance(derive, (tuple, list)) and len(derive) >= 3:
        for comp in derive[2] or []:
            src = comp[0] if isinstance(comp, (tuple, list)) and comp else comp
            if isinstance(src, (list, tuple)) and src and isinstance(src[0], str):
                req.extend(src)          # u/v pair: both records needed
            elif isinstance(src, str):
                req.append(src)
    return req


def _present(spec: dict, lines: list[str]) -> bool:
    """Is every record this field needs in the index?"""
    req = _requirements(spec)
    if not req:
        return False
    for matcher in req:
        parts = matcher if isinstance(matcher, tuple) else (matcher,)
        if not any(all(str(p) in ln for p in parts) for ln in lines):
            return False
    return True


def sample_dates(start: date, end: date, n: int) -> list[date]:
    """Evenly spaced dates across the range, endpoints included.

    Even spacing rather than random: a product change is a STEP in time, and a
    step is found by walking the axis, not by sampling it randomly.
    """
    if n < 2 or end <= start:
        return [start, end][: max(1, n)]
    span = (end - start).days
    return [start + timedelta(days=round(i * span / (n - 1))) for i in range(n)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="rap")
    ap.add_argument("--start", default="2024-02-27")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--hour", type=int, default=12, help="cycle hour to probe")
    ap.add_argument("--fhour", type=int, default=1)
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--fields", default="env",
                    help="'env' (the environment extractor's), 'meso', 'all', "
                         "or a comma-separated list")
    args = ap.parse_args()

    from backend.services.hrrr_field_service import MODELS, get_hrrr_field_service

    if args.fields == "env":
        wanted = _env_field_ids()
    elif args.fields == "meso":
        wanted = _meso_field_ids()
    elif args.fields == "all":
        wanted = sorted(set(_env_field_ids()) | set(_meso_field_ids()))
    else:
        wanted = [f.strip() for f in args.fields.split(",") if f.strip()]

    spec = MODELS.get(args.model, {}).get("fields", {})
    unknown = [f for f in wanted if f not in spec]
    wanted = [f for f in wanted if f in spec]
    if unknown:
        print(f"not registered for {args.model}: {', '.join(unknown)}")
    if not wanted:
        print("nothing to probe")
        return 2

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    dates = sample_dates(start, end, args.samples)
    svc = get_hrrr_field_service()

    print(f"\nprobing {args.model} f{args.fhour:02d} at {args.hour:02d}Z, "
          f"{len(wanted)} field(s), {len(dates)} date(s) from {start} to {end}")
    print("(reads .idx text only - no GRIB decode, no data download)\n")

    # date -> {field: present}
    grid: dict[str, dict[str, bool]] = {}
    missing_idx: list[str] = []
    for d in dates:
        key = svc._key(args.model, d.strftime("%Y%m%d"), args.hour, args.fhour,
                       None)
        try:
            lines = svc._read_idx(args.model, key)
        except Exception as e:
            missing_idx.append(f"{d}: {type(e).__name__}")
            continue
        row = {fid: _present(spec[fid], lines) for fid in wanted}
        grid[d.isoformat()] = row

    if not grid:
        print("no index files could be read - is the bucket reachable?")
        for m in missing_idx[:5]:
            print("   ", m)
        return 2

    # Render: one row per field, one column per sampled date.
    cols = sorted(grid)
    head = "  " + " ".join(c[2:7].replace("-", "") for c in cols)
    print(f"{'field':<14}{head}")
    problems: list[tuple[str, list[str], list[str]]] = []
    for fid in wanted:
        cells = []
        absent, present = [], []
        for c in cols:
            ok = grid[c].get(fid, False)
            cells.append(" ok  " if ok else " --  ")
            (present if ok else absent).append(c)
        print(f"{fid:<14}  {' '.join(cells)}")
        if absent and present:
            problems.append((fid, absent, present))
        elif absent and not present:
            problems.append((fid, absent, []))

    if missing_idx:
        print(f"\n{len(missing_idx)} date(s) had no readable index:")
        for m in missing_idx[:6]:
            print("   ", m)

    print()
    if not problems:
        print("PASS - every field is present across the whole range.")
        return 0

    for fid, absent, present in problems:
        if not present:
            print(f"FAIL  {fid}: absent at EVERY sampled date. It will be NaN "
                  "for the entire training set.")
        else:
            print(f"FAIL  {fid}: absent {absent[0]}..{absent[-1]}, present "
                  f"{present[0]}..{present[-1]}.")
            print(f"      Its missingness would encode the date. Either start the "
                  f"training window at {present[0]}, or drop the feature.")
    print(f"\n{len(problems)} field(s) unusable across this range as-is.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
