"""
Drop archive rows the collector would no longer keep.

The log gate (`live_qa_service.should_log_cell`) was added after months of
collection that took EVERY tracked cell, so the archive holds rows the live
collector would not write today.  Training on a distribution the collector no
longer produces is the same train/serve mismatch as computing a feature two
different ways: the model learns from a sample it will never see again.

This re-applies today's gate to the stored rows and keeps only what passes.
It is deliberately a separate, explicit step — not something a retrain does
quietly — because it deletes data.

    python scripts/prune_training_data.py --dry-run     # report only
    python scripts/prune_training_data.py               # prune, keeping a backup

Streams and writes atomically, and carries over anything the live collector
appends mid-run, exactly as the labeller does.
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TRAINING_DATA = PROJECT_ROOT / "data" / "training_data.jsonl"
NEWLINE = b"\n"

# Stored rows carry `flags` with slightly different key names than the live cell
# dict the gate expects.
FLAG_ALIASES = {
    "low_level_meso": "low_level_meso_detected",
    "mid_level_meso": "mid_level_meso_detected",
    "llsd_rotation": "llsd_rotation_detected",
}


def row_survives(rec: dict, min_dbz: float) -> bool:
    """Would today's collector have written this row?"""
    from backend.services.live_qa_service import should_log_cell

    feats = rec.get("features") or {}
    cell = {
        "max_reflectivity_dbz": feats.get("max_dbz", 0),
        # severity_score is not stored per row; the flags below carry the same
        # "this cell was interesting" signal, and the dBZ floor covers the rest.
        "severity_score": 0,
    }
    for k, v in (rec.get("flags") or {}).items():
        cell[FLAG_ALIASES.get(k, k)] = v
    return should_log_cell(cell, min_dbz)


def main():
    ap = argparse.ArgumentParser(description="Prune rows today's log gate rejects")
    ap.add_argument("--data", default=str(TRAINING_DATA))
    ap.add_argument("--min-dbz", type=float, default=None,
                    help="Defaults to the live setting (live_qa_log_min_dbz)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    min_dbz = args.min_dbz
    if min_dbz is None:
        try:
            from backend.config.settings import get_settings
            min_dbz = float(get_settings().live_qa_log_min_dbz)
        except Exception:
            min_dbz = 40.0

    path = Path(args.data)
    if not path.exists():
        print(f"No archive at {path}")
        sys.exit(1)

    kept = Counter()
    dropped = Counter()
    size0 = path.stat().st_size
    tmp = path.with_suffix(path.suffix + ".prune.tmp")
    out = None if args.dry_run else tmp.open("wb")
    consumed = 0
    print(f"Pruning {path.name} at min_dbz={min_dbz}")

    try:
        with path.open("rb") as f:
            for raw in f:
                if consumed >= size0:
                    break
                consumed += len(raw)
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if out is not None:
                        out.write(line + b"\n")
                    continue
                lab = rec.get("label")
                bucket = "pos" if lab else ("neg" if lab is False else "unlabelled")
                if row_survives(rec, min_dbz):
                    kept[bucket] += 1
                    if out is not None:
                        out.write(line + b"\n")
                else:
                    dropped[bucket] += 1

        if out is not None:
            with path.open("rb") as f:
                f.seek(consumed)
                tail = f.read()
            if tail:
                if not tail.endswith(NEWLINE):
                    tail += NEWLINE
                out.write(tail)
                print(f"  carried over {tail.count(NEWLINE)} row(s) "
                      "appended by the live collector during the run")
    finally:
        if out is not None:
            out.close()

    nk, nd = sum(kept.values()), sum(dropped.values())
    print(f"\n  keep : {nk:,}  {dict(kept)}")
    print(f"  drop : {nd:,}  {dict(dropped)}")
    if nk + nd:
        print(f"  dropping {nd / (nk + nd) * 100:.1f}% of the archive")

    if args.dry_run:
        tmp.unlink(missing_ok=True)
        print("\nDRY RUN - nothing written")
        return

    if not args.no_backup:
        bak = path.with_suffix(f".preprune-{datetime.now():%Y%m%d-%H%M%S}.jsonl")
        shutil.copy2(path, bak)
        print(f"\n  backup: {bak.name}")
    os.replace(tmp, path)
    print(f"  archive is now {nk:,} rows")


if __name__ == "__main__":
    main()
