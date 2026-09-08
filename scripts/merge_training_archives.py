"""
Combine training archives collected on different machines.

Two systems collect independently: the always-on server (unattended, home site)
and the desktop Hub (whatever is being worked during active weather). They see
DIFFERENT storms -- the Hub follows the operator to wherever the weather is, the
server holds the home region -- so merging them is strictly more data, not
duplicate data.

Identity is (site, ts, cell_id). A tracked cell is one row per site per scan, so
that triple is unique per observation regardless of which machine recorded it.
The same storm seen by both machines produces the SAME key and merges to one
row, which is what you want: two copies of one observation is not two
observations, and a duplicate that survives into training puts the same scan in
both the fit and the holdout, reporting memorisation as skill.

Two copies of one observation are combined FIELD BY FIELD, not resolved by
picking a winner. The machines are asymmetric in exactly the way that matters:
the server may have labelled a row the Hub never did, while the Hub had the
models loaded and holds the probabilities. Keeping either row whole would throw
away half the value of having collected it twice.

    python scripts/merge_training_archives.py --into data/training_data.jsonl \\
        --from /path/from/hub/training_data.jsonl
    python scripts/merge_training_archives.py --into a.jsonl --from b.jsonl --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


def key_of(rec: dict):
    """Identity of one observation. None when the row cannot be identified."""
    site, ts, cid = rec.get("site"), rec.get("ts"), rec.get("cell_id")
    if not ts or not cid:
        return None
    return (site, ts, cid)


# Fields worth rescuing from a second copy of the same observation, and why:
#   label / label_source / label_strength / label_issued
#       needs an IEM fetch for that day; back-labelling an old row means
#       refetching a window that may have aged out of the archive.
#   p_rotation_model / p_severe_model
#       CANNOT be recreated at all. They are what the model said AT THE TIME,
#       which is the entire basis of the live scorecard -- rescoring the row
#       later produces a different number against a different model.
#   mesh_mm / shi_value
#       derived at scan time from data no longer held.
_RESCUE = ("label", "label_source", "label_strength", "label_issued",
           "p_rotation_model", "p_severe_model", "mesh_mm", "shi_value")


def combine(a: dict, b: dict) -> dict:
    """Merge two copies of ONE observation, field by field.

    Picking a whole winning row loses whatever only the loser had -- and the two
    machines are asymmetric in exactly that way: the server may have labelled a
    row the Hub never did, while the Hub had the models loaded and holds the
    probabilities. Choosing either copy wholesale discards half the value of
    having collected it twice.

    `a` is the incumbent; `b` only fills gaps, so a re-run is idempotent and
    merge order cannot change the result.
    """
    out = dict(a)
    for k in _RESCUE:
        if out.get(k) is None and b.get(k) is not None:
            out[k] = b[k]
    fa, fb = a.get("features") or {}, b.get("features") or {}
    if len(fb) > len(fa):
        merged = dict(fb)
        merged.update({k: v for k, v in fa.items() if v is not None})
        out["features"] = merged
    if not (out.get("flags")) and b.get("flags"):
        out["flags"] = b["flags"]
    return out


def gained(before: dict, after: dict) -> bool:
    """Did combining actually add anything? Drives the reported counts."""
    if any(before.get(k) is None and after.get(k) is not None for k in _RESCUE):
        return True
    return len(after.get("features") or {}) > len(before.get("features") or {})


def read(path: Path, stats: Counter):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats["unparseable"] += 1
                continue
            yield rec


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge training archives from two machines")
    ap.add_argument("--into", required=True, help="Archive to merge INTO (modified in place)")
    ap.add_argument("--from", dest="src", required=True, nargs="+",
                    help="One or more archives to merge in")
    ap.add_argument("--dry-run", action="store_true", help="Report and change nothing")
    ap.add_argument("--no-backup", action="store_true",
                    help="Skip the .bak copy (not advised)")
    args = ap.parse_args()

    dst = Path(args.into)
    if not dst.exists():
        print(f"error: {dst} does not exist", file=sys.stderr)
        return 2
    srcs = [Path(s) for s in args.src]
    for s in srcs:
        if not s.exists():
            print(f"error: {s} does not exist", file=sys.stderr)
            return 2

    stats = Counter()
    rows: dict = {}
    unkeyed: list = []

    for rec in read(dst, stats):
        stats["dst_rows"] += 1
        k = key_of(rec)
        if k is None:
            unkeyed.append(rec)          # keep, but it can never be deduped
            stats["dst_unkeyed"] += 1
            continue
        rows[k] = rec

    for s in srcs:
        added = improved = same = 0
        for rec in read(s, stats):
            stats["src_rows"] += 1
            k = key_of(rec)
            if k is None:
                unkeyed.append(rec)
                stats["src_unkeyed"] += 1
                continue
            cur = rows.get(k)
            if cur is None:
                rows[k] = rec
                added += 1
            else:
                merged = combine(cur, rec)
                if gained(cur, merged):
                    rows[k] = merged
                    improved += 1
                else:
                    same += 1
        print(f"  {s.name}: +{added:,} new, {improved:,} enriched from the other copy, "
              f"{same:,} already held")

    total = len(rows) + len(unkeyed)
    print(f"\ndestination held {stats['dst_rows']:,}")
    print(f"sources offered  {stats['src_rows']:,}")
    print(f"merged total     {total:,}  (+{total - stats['dst_rows']:,})")
    if stats["unparseable"]:
        print(f"skipped {stats['unparseable']:,} unparseable line(s)")
    if stats["dst_unkeyed"] or stats["src_unkeyed"]:
        print(f"kept {stats['dst_unkeyed'] + stats['src_unkeyed']:,} row(s) with no "
              "(site, ts, cell_id) -- these cannot be deduplicated")

    if args.dry_run:
        print("\ndry run - nothing written")
        return 0

    if not args.no_backup:
        bak = dst.with_suffix(f".premerge-{datetime.now():%Y%m%d-%H%M%S}.jsonl")
        shutil.copy2(dst, bak)
        print(f"backup: {bak.name}")

    # Write to a temp file in the same directory, then atomically replace, so an
    # interrupted merge cannot leave a half-written archive where the real one was.
    tmp = dst.with_suffix(".merging.tmp")
    ordered = sorted(rows.values(), key=lambda r: (r.get("ts") or "", r.get("cell_id") or ""))
    with tmp.open("w", encoding="utf-8") as f:
        for rec in ordered:
            f.write(json.dumps(rec) + "\n")
        for rec in unkeyed:
            f.write(json.dumps(rec) + "\n")
    os.replace(tmp, dst)
    print(f"wrote {total:,} rows -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
