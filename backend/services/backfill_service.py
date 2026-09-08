"""
Backfill job control + training-archive statistics for the Model dashboard.

Lets the archive replay be driven from the UI instead of the command line:
search for severe days, pick the ones worth replaying, start the job, and watch
it. The point is that routine model work — finding days, kicking off a backfill,
checking whether the last retrain helped — should not need a terminal.

The replay itself stays in `scripts/backfill_training_data.py` and runs as a
SUBPROCESS. That is deliberate: it holds a Py-ART radar and a Barnes2 grid per
worker (~1.5 GB each), and a crash or a MemoryError in it must not be able to
take the dashboard down with it. Progress is read back from the job's log and
from the script's own checkpoint file, so a job survives a dashboard restart —
`status()` will still find it and report on it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
TRAINING_DATA = DATA_DIR / "training_data.jsonl"
BACKFILL_OUT = DATA_DIR / "training_data.backfill.jsonl"
BACKFILL_STATE = DATA_DIR / "backfill_state.json"
JOB_LOG = DATA_DIR / "backfill_job.log"
JOB_META = DATA_DIR / "backfill_job.json"

# "  [3/16] KILN 2026-08-11: 412 rows, 61 volumes, 0 errors"
_PAIR_RE = re.compile(
    r"\[(\d+)/(\d+)\]\s+(\w+)\s+(\d{4}-\d{2}-\d{2}):\s+(\d+) rows,\s+(\d+) volumes,\s+(\d+) errors")
# "    40/61 volumes, 250 rows, 0 err, 0.05 vol/s"
_VOL_RE = re.compile(r"(\d+)/(\d+) volumes,\s+(\d+) rows,\s+(\d+) err,\s+([\d.]+) vol/s")

# Features that are meant to carry data. A column that is all-zero across the
# archive is a wiring bug, not a quiet feature — three of them were, for months,
# and nothing surfaced it. The dashboard calls them out.
_WATCH_FEATURES = [
    "mean_cc", "min_cc", "mean_zdr", "llsd_max_shear", "vil_kg_m2",
    "max_rot_vel_profile_ms", "rot_velocity_ms", "score_rotation",
    "mrms_azshear_0_2km", "mrms_rotation_track_30min",
]


class BackfillService:
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._stats_cache: Optional[dict] = None
        self._stats_at: float = 0.0
        self._stats_lock = threading.Lock()

    # ── candidate days ───────────────────────────────────────────────────
    @staticmethod
    def find_days(start: str, end: str, sites: list[str], min_tor: int = 1) -> list[dict]:
        """Severe days near the given radars, newest first."""
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from scripts.backfill_training_data import find_severe_days

        s = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        e = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        found = find_severe_days(s, e, sites, min_tor=min_tor)

        by_day: dict = {}
        for (day, site), info in found.items():
            d = by_day.setdefault(day, {"day": day, "tor": 0, "svr": 0, "sites": [],
                                        "est_volumes": 0})
            d["tor"] += info["tor"]
            d["svr"] += info["svr"]
            d["sites"].append(site)
            # Estimate from the CLUSTERED windows the replay will actually use,
            # each padded as the replay pads it (~4.5 min VCP).  Quoting the old
            # first..last span over-estimates a day with two warnings 20 h apart
            # by 5x and makes the picker useless for choosing what to run.
            wins = info.get("windows") or (
                [(info["first"], info["last"])] if info.get("first") and info.get("last") else [])
            for a, b in wins:
                span_h = (b - a).total_seconds() / 3600.0
                d["est_volumes"] += int(max(0.0, span_h + 3.0) * (60 / 4.5))
        for d in by_day.values():
            d["sites"].sort()
            # Measured throughput is ~20 s/volume, dominated by Barnes2 gridding.
            d["est_minutes"] = round(d["est_volumes"] * 20 / 60)
        return sorted(by_day.values(), key=lambda x: x["day"], reverse=True)

    # ── job control ──────────────────────────────────────────────────────
    def start(self, days: list[str], sites: list[str], workers: int = 4,
              full_day: bool = False, min_tor: int = 1) -> dict:
        if self.is_running():
            return {"ok": False, "error": "a backfill is already running"}
        if not days or not sites:
            return {"ok": False, "error": "pick at least one day and one site"}

        cmd = [sys.executable,
               str(PROJECT_ROOT / "scripts" / "backfill_training_data.py"),
               "--days", *days, "--sites", *sites,
               "--workers", str(max(1, workers)),
               "--min-tor", str(max(0, int(min_tor))),
               "--out", str(BACKFILL_OUT), "--resume"]
        if full_day:
            cmd.append("--full-day")

        JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Do NOT point the child's stdout at JOB_LOG: the script tees its own
        # output there, and two handles on one file keep independent write
        # positions and overwrite each other -- the progress feed stops updating
        # and the tail fills with padding nulls.  The script owns JOB_LOG; this
        # captures raw stdout/stderr separately so a traceback is not lost.
        log_fh = JOB_LOG.with_suffix(".stdout.log").open("w", encoding="utf-8")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(PROJECT_ROOT), env=env,
                stdout=log_fh, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            log_fh.close()
            return {"ok": False, "error": str(e)}

        JOB_META.write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(),
            "pid": self._proc.pid, "days": days, "sites": sites,
            "workers": workers, "full_day": full_day, "min_tor": min_tor,
        }, indent=2), encoding="utf-8")
        logger.info(f"[backfill] started pid={self._proc.pid} "
                    f"{len(days)} day(s) x {len(sites)} site(s), {workers} worker(s)")
        return {"ok": True, "pid": self._proc.pid,
                "pairs": len(days) * len(sites)}

    def stop(self) -> dict:
        if not self.is_running():
            return {"ok": False, "error": "no backfill running"}
        try:
            # Completed (site, day) pairs are already checkpointed, so --resume
            # picks up from here rather than starting over.
            self._proc.terminate()
            try:
                self._proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def is_running(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        # A job started before a dashboard restart is still ours to report on.
        try:
            meta = json.loads(JOB_META.read_text(encoding="utf-8"))
            pid = int(meta.get("pid", 0))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return False
        if not pid:
            return False
        return _pid_alive(pid)

    def status(self) -> dict:
        meta = {}
        try:
            meta = json.loads(JOB_META.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

        pairs_done, pairs_total = 0, 0
        rows = volumes = errors = 0
        current = None
        recent: list[dict] = []
        tail: list[str] = []
        # A job started from a terminal writes its own log path into the meta.
        # Honouring it is what lets the dashboard report on a run it did not
        # launch -- and the long runs are exactly the ones started by hand.
        log_path = Path(meta.get("log_path") or JOB_LOG)
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = [l for l in lines[-14:] if l.strip()]
            for line in lines:
                m = _PAIR_RE.search(line)
                if m:
                    n, total, site, day, r, v, e = m.groups()
                    pairs_done, pairs_total = int(n), int(total)
                    rows += int(r)
                    volumes += int(v)
                    errors += int(e)
                    recent.append({"site": site, "day": day, "rows": int(r),
                                   "volumes": int(v), "errors": int(e)})
                    continue
                m = _VOL_RE.search(line)
                if m:
                    i, tot, r, e, rate = m.groups()
                    current = {"volume": int(i), "of": int(tot),
                               "rows": int(r), "errors": int(e), "rate": float(rate)}
        except OSError:
            pass

        running = self.is_running()
        eta_min = None
        if running and pairs_total and pairs_done:
            try:
                started = datetime.fromisoformat(meta["started_at"])
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                per_pair = elapsed / pairs_done
                eta_min = round((pairs_total - pairs_done) * per_pair / 60)
            except Exception:
                pass

        checkpoint = []
        try:
            checkpoint = json.loads(BACKFILL_STATE.read_text(encoding="utf-8")).get("done", [])
        except (OSError, json.JSONDecodeError):
            pass

        return {
            "running": running,
            "started_at": meta.get("started_at"),
            "days": meta.get("days", []),
            "sites": meta.get("sites", []),
            "workers": meta.get("workers"),
            "pairs_done": pairs_done,
            "pairs_total": pairs_total or (len(meta.get("days", [])) * len(meta.get("sites", []))),
            "rows": rows, "volumes": volumes, "errors": errors,
            "current": current,
            "eta_minutes": eta_min,
            "recent": recent[-12:],
            "checkpointed_pairs": len(checkpoint),
            "started_by": "dashboard" if not meta.get("log_path")
                          or str(meta.get("log_path")) == str(JOB_LOG) else "terminal",
            "output_exists": BACKFILL_OUT.exists(),
            "output_rows": _count_lines(BACKFILL_OUT) if BACKFILL_OUT.exists() else 0,
            "log_tail": tail,
        }

    # ── archive statistics ───────────────────────────────────────────────
    def data_stats(self, path: Optional[Path] = None, max_age_s: float = 60.0) -> dict:
        """Label balance, month coverage and feature population for the archive.

        Streamed — the file runs to hundreds of MB — and cached, because the UI
        polls and a full pass is seconds.
        """
        path = Path(path) if path else TRAINING_DATA
        key = str(path)
        with self._stats_lock:
            if (self._stats_cache
                    and self._stats_cache.get("_key") == key
                    and (time.time() - self._stats_at) < max_age_s):
                return self._stats_cache

        out = {"_key": key, "path": path.name, "exists": path.exists()}
        if not path.exists():
            return out

        total = labeled = pos = neg = 0
        by_month: Counter = Counter()
        pos_days: Counter = Counter()
        feat_nonzero: Counter = Counter()
        feat_seen = 0
        sites: Counter = Counter()
        first = last = None

        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    ts = rec.get("ts") or ""
                    if ts:
                        by_month[ts[:7]] += 1
                        if first is None or ts < first:
                            first = ts
                        if last is None or ts > last:
                            last = ts
                    if rec.get("site"):
                        sites[rec["site"]] += 1
                    lab = rec.get("label")
                    if lab is not None:
                        labeled += 1
                        if lab:
                            pos += 1
                            pos_days[_conv_day(ts)] += 1
                        else:
                            neg += 1
                    feats = rec.get("features") or {}
                    if feats:
                        feat_seen += 1
                        for k in _WATCH_FEATURES:
                            if feats.get(k):
                                feat_nonzero[k] += 1
        except OSError as e:
            out["error"] = str(e)
            return out

        out.update(
            total=total, labeled=labeled, positives=pos, negatives=neg,
            unlabeled=total - labeled,
            positive_rate=(pos / labeled) if labeled else 0.0,
            first_ts=first, last_ts=last,
            positive_days=len(pos_days),
            top_positive_days=[{"day": d, "n": n} for d, n in pos_days.most_common(8)],
            by_month=[{"month": m, "n": n} for m, n in sorted(by_month.items())],
            sites=[{"site": s, "n": n} for s, n in sites.most_common(10)],
            features=[
                {"name": k,
                 "pct_nonzero": round(feat_nonzero[k] / feat_seen * 100, 1) if feat_seen else 0.0,
                 # An all-zero column is a wiring bug. Three were, for months.
                 "dead": feat_seen > 0 and feat_nonzero[k] == 0}
                for k in _WATCH_FEATURES
            ],
        )
        with self._stats_lock:
            self._stats_cache = out
            self._stats_at = time.time()
        return out

    def merge_backfill(self, label_first: bool = True) -> dict:
        """Label the backfill output, then append it onto the main archive.

        Labelling HAS to happen here, before the merge.  The scheduled retrain
        only labels a trailing window (`auto_retrain_label_days`, 10), so a 2019
        or 2024 replay merged raw would sit in the archive unlabelled forever and
        be silently skipped by every training run — the rows would be there, the
        row count would go up, and nothing would say the positives were missing.

        Appends only: the live collector may be writing to the archive at the
        same time, and the backfill file is a separate, already-complete set.
        """
        if not BACKFILL_OUT.exists():
            return {"ok": False, "error": "no backfill output to merge"}
        if self.is_running():
            return {"ok": False, "error": "backfill still running"}

        labelled = None
        if label_first:
            try:
                labelled = self._label_backfill()
            except Exception as e:
                return {"ok": False, "error": f"labelling failed: {e}"}

        n = 0
        try:
            with TRAINING_DATA.open("a", encoding="utf-8") as dst, \
                    BACKFILL_OUT.open(encoding="utf-8") as src:
                for line in src:
                    if line.strip():
                        dst.write(line if line.endswith("\n") else line + "\n")
                        n += 1
            merged = BACKFILL_OUT.with_suffix(
                f".merged-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
            BACKFILL_OUT.rename(merged)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        with self._stats_lock:
            self._stats_cache = None
        out = {"ok": True, "rows_merged": n, "archived_as": merged.name}
        if labelled:
            out.update(labelled)
        return out

    @staticmethod
    def _label_backfill() -> dict:
        """Label the backfill file from NWS warnings over the days it contains.

        Uses `data_day_blocks` rather than the file's full span: a replay of
        2019-05-27 and 2026-08-11 spans seven years but holds two days, and
        fetching the whole span would be hundreds of IEM requests, each one a
        chance to fail and look like a quiet period.
        """
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from scripts.label_from_warnings import (
            auto_label, data_day_blocks, fetch_warnings_range,
        )

        blocks = data_day_blocks(BACKFILL_OUT)
        if not blocks:
            return {"labelled_pos": 0, "labelled_neg": 0,
                    "label_note": "no timestamped rows"}
        now = datetime.now(timezone.utc)
        warnings = []
        for a, b in blocks:
            warnings.extend(fetch_warnings_range(a, min(b, now), ["TO", "SV"],
                                                 chunk_days=7))
        if not warnings:
            return {"labelled_pos": 0, "labelled_neg": 0,
                    "label_note": "no warnings fetched - rows left unlabelled"}
        pos, neg, _ = auto_label(
            BACKFILL_OUT, warnings, dry_run=False, overwrite=True,
            strict_tornado=True,
            window_start=min(a for a, _ in blocks),
            window_end=min(max(b for _, b in blocks), now),
        )
        return {"labelled_pos": pos, "labelled_neg": neg,
                "label_blocks": len(blocks)}


def _pid_alive(pid: int) -> bool:
    """Is this PID still running?  stdlib only (no psutil dependency).

    Used so a backfill started before a dashboard restart is still reported on
    rather than silently looking finished.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            # Declare HANDLE signatures.  Undeclared, ctypes truncates handles to
            # a 32-bit int; real handles happen to fit, but the pseudo-handle
            # from GetCurrentProcess() does not -- that exact bug made
            # SetPriorityClass a silent no-op in the backfill.
            k.OpenProcess.restype = wintypes.HANDLE
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k.GetExitCodeProcess.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _conv_day(ts: str) -> str:
    try:
        return (datetime.fromisoformat(ts) - timedelta(hours=12)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return "unknown"


def _count_lines(p: Path) -> int:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


_service: Optional[BackfillService] = None


def get_backfill_service() -> BackfillService:
    global _service
    if _service is None:
        _service = BackfillService()
    return _service
