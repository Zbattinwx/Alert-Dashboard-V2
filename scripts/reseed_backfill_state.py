"""Make the backfill checkpoint agree with a specific output file.

WHY THIS EXISTS
---------------
`backfill_training_data.py` keeps its checkpoint at ONE fixed path shared by
every run, and `--resume` means "skip any (day, site) pair ever recorded as
done" -- not "resume this run". Re-deriving the archive with new code therefore
skipped 76 of 130 in-range pairs on the first attempt, every one of them
completed by the OLD code, i.e. exactly the work the re-derivation existed to
redo. Nothing looked wrong: the run proceeded, the output grew, and the result
would simply have been a quietly incomplete archive.

So before a re-derivation, the checkpoint has to be rebuilt from the output file
that run is actually writing: pairs present there are genuinely done, everything
else is not.

This is Python rather than a few lines of PowerShell because two PS 5.1 traps
bit in a row here -- `Set-Content -Encoding utf8` writes a BOM that
`json.load` rejects, and `ConvertTo-Json` round-tripping an array of two-element
arrays collapsed 54 pairs into 2.

Usage:
    python scripts/reseed_backfill_state.py data/training_data.rederived.jsonl
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE = PROJECT_ROOT / "data" / "backfill_state.json"


def pairs_in(path: Path) -> list[list[str]]:
    """The (day, site) pairs actually represented in an output file."""
    seen: set[tuple[str, str]] = set()
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts") or ""
            site = rec.get("site")
            if len(ts) >= 10 and site:
                seen.add((ts[:10], site))
    return [list(p) for p in sorted(seen)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", help="the .jsonl this run is writing")
    ap.add_argument("--state", default=str(STATE))
    args = ap.parse_args()

    state_path = Path(args.state)
    done = pairs_in(Path(args.output))

    obj: dict = {}
    if state_path.exists():
        # Back up once, so the pre-existing history is recoverable.
        backup = state_path.with_suffix(".preRederive.json")
        if not backup.exists():
            shutil.copy2(state_path, backup)
        raw = state_path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            obj = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            obj = {}

    before = len(obj.get("done") or [])
    obj["done"] = done
    # UTF-8, no BOM, LF -- json.load in the backfill reads it with defaults.
    with state_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1)

    print(f"checkpoint: {before} -> {len(done)} done pair(s), "
          f"rebuilt from {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
