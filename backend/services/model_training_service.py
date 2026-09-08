"""
Automated retraining of the rotation classifier.

Closes the loop that used to be three manual commands (collect -> label ->
train).  The live QA reporter already appends every tracked cell to
`data/training_data.jsonl`; this service periodically labels the new rows from
NWS warning polygons, trains a CANDIDATE model, and promotes it only if it beats
the model currently in production on data neither of them has seen.

## Why a promotion gate rather than just retraining

Retraining on a schedule and overwriting the live model is worse than not
retraining at all: a bad labelling run, a quiet fortnight, or a feature that
stopped being populated all silently degrade the thing making on-air calls, and
nothing surfaces it.  So every cycle produces a candidate at a scratch path, and
the incumbent stays live unless the candidate wins.

## What makes the comparison fair

`train_rotation_model.train()` withholds the most recent `HOLDOUT_DAYS`
convective days from fitting entirely and records which days those were.  The
incumbent was trained at an earlier cutoff, so those days are unseen by it too.
We reload exactly those rows, score both models on them, and compare.  If the
holdout has too few positives to discriminate (a quiet stretch), the cycle
declines to promote rather than guessing.

Guards, each of which exists because its absence breaks something:

  * **A lock file**, so two cycles cannot interleave and write the model at once.
  * **A minimum-new-labels floor**, so a cycle that would only re-fit the same
    data does not burn 20 minutes of CPU.
  * **A severe-weather check**, because training is CPU-heavy and the storm
    tracker needs those cores far more than we do during an event.
  * **A rollback copy**, so a regression that slips the gate can be undone.
  * **Subprocess isolation for the fit**, so a MemoryError or a segfault in a
    training run cannot take the dashboard down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Frozen-aware and WRITABLE: beside the .exe when bundled, <repo>/data from
# source. Retrain output has to land where the tracker looks first, and it must
# not land inside _internal, which the updater replaces wholesale.
from .model_paths import runtime_data_dir as _runtime_data_dir
DATA_DIR = _runtime_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRAINING_DATA = DATA_DIR / "training_data.jsonl"
MODEL_PATH = DATA_DIR / "rotation_model.joblib"
PREVIOUS_PATH = DATA_DIR / "rotation_model.previous.joblib"
CANDIDATE_PATH = DATA_DIR / "rotation_model.candidate.joblib"
STATE_PATH = DATA_DIR / "model_training_state.json"
LOCK_PATH = DATA_DIR / "model_training.lock"

# A stale lock (killed process, hard reboot mid-cycle) must not wedge retraining
# forever.  Longer than any plausible cycle, shorter than the schedule.
LOCK_STALE_S = 6 * 3600


class ModelTrainingService:
    def __init__(self, settings=None):
        self.settings = settings
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._current: Optional[str] = None      # phase name while a cycle runs
        self.state = self._load_state()

    # ── state ────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"history": []}

    def _save_state(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Keep the tail only; this file is a log, not a database.
            self.state["history"] = self.state.get("history", [])[-40:]
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            logger.warning(f"[retrain] could not persist state: {e}")

    def _record(self, outcome: str, detail: dict) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(),
                 "outcome": outcome, **detail}
        self.state.setdefault("history", []).append(entry)
        self.state["last_run"] = entry
        if outcome == "promoted":
            self.state["last_promotion"] = entry
        self._save_state()

    # ── lock ─────────────────────────────────────────────────────────────
    def _acquire_lock(self) -> bool:
        try:
            if LOCK_PATH.exists():
                age = time.time() - LOCK_PATH.stat().st_mtime
                if age < LOCK_STALE_S:
                    return False
                logger.warning(f"[retrain] clearing stale lock ({age/3600:.1f} h old)")
                LOCK_PATH.unlink(missing_ok=True)
            LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
            return True
        except OSError as e:
            logger.warning(f"[retrain] lock failed: {e}")
            return False

    def _release_lock(self) -> None:
        LOCK_PATH.unlink(missing_ok=True)

    # ── preconditions ────────────────────────────────────────────────────
    @staticmethod
    def _severe_weather_active() -> bool:
        """True when the storm tracker is busy enough that we should stand down.

        Training pins several cores for minutes.  During an event those cores
        belong to the thing detecting rotation, not to the thing learning about
        last week's rotation.
        """
        try:
            from .storm_tracking_service import get_storm_tracking_service
            svc = get_storm_tracking_service()
            if svc is None:
                return False
            cells = getattr(svc, "_cells", None) or getattr(svc, "cells", None) or []
            flagged = [c for c in cells
                       if getattr(c, "rotation_detected", False)
                       or getattr(c, "hail_indicated", False)]
            return len(flagged) > 0
        except Exception:
            return False

    def _count_labeled(self) -> tuple[int, int]:
        """(labeled, positives) in the archive.  Streamed — the file is ~500 MB."""
        labeled = pos = 0
        try:
            with TRAINING_DATA.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("label") is not None:
                        labeled += 1
                        if rec["label"]:
                            pos += 1
        except OSError:
            pass
        return labeled, pos

    # ── the cycle ────────────────────────────────────────────────────────
    async def run_cycle(self, force: bool = False) -> dict:
        """Label -> train candidate -> compare -> promote or reject."""
        if not TRAINING_DATA.exists():
            return {"outcome": "skipped", "reason": "no training data"}
        if not force and self._severe_weather_active():
            return {"outcome": "skipped", "reason": "severe weather active"}
        if not self._acquire_lock():
            return {"outcome": "skipped", "reason": "another cycle holds the lock"}

        try:
            self._current = "labelling"
            label_days = int(self._cfg("auto_retrain_label_days", 10))
            lab = await asyncio.to_thread(self._label_new, label_days)

            labeled, pos = await asyncio.to_thread(self._count_labeled)
            min_new = int(self._cfg("auto_retrain_min_new_labels", 500))
            prev_labeled = self.state.get("labeled_at_last_train", 0)
            if not force and (labeled - prev_labeled) < min_new:
                out = {"outcome": "skipped",
                       "reason": f"only {labeled - prev_labeled} new labels "
                                 f"(need {min_new})",
                       "labeled": labeled, "positives": pos, **lab}
                self._record("skipped", out)
                return out

            self._current = "training"
            trained = await asyncio.to_thread(self._train_candidate)
            if not trained.get("ok"):
                self._record("failed", trained)
                return {"outcome": "failed", **trained}

            self._current = "evaluating"
            decision = await asyncio.to_thread(self._decide, trained["metrics"])

            if decision["promote"]:
                self._current = "promoting"
                await asyncio.to_thread(self._promote)
                await self._reload_tracker()
                self.state["labeled_at_last_train"] = labeled
                self._record("promoted", {**decision, "labeled": labeled})
                logger.info(f"[retrain] promoted new rotation model: {decision['why']}")
                return {"outcome": "promoted", **decision}

            # A rejected candidate still counts as "trained on this much data",
            # or every subsequent cycle would retrain the same rows forever.
            self.state["labeled_at_last_train"] = labeled
            self._record("rejected", {**decision, "labeled": labeled})
            logger.info(f"[retrain] candidate rejected: {decision['why']}")
            return {"outcome": "rejected", **decision}
        except Exception as e:
            logger.exception("[retrain] cycle failed")
            self._record("failed", {"error": str(e)})
            return {"outcome": "failed", "error": str(e)}
        finally:
            self._current = None
            self._release_lock()

    def _cfg(self, name: str, default):
        return getattr(self.settings, name, default) if self.settings else default

    # ── steps ────────────────────────────────────────────────────────────
    def _label_new(self, days: int) -> dict:
        """Label rows from the last `days` of warnings.

        Deliberately NOT --overwrite: a routine cycle only fills in rows that are
        still unlabeled.  Re-labelling the whole archive is a maintenance action
        (`--all --overwrite`), not something a scheduled job should do unattended.
        """
        sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
        from scripts.label_from_warnings import auto_label, fetch_warnings_range

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        warnings = fetch_warnings_range(start, end, ["TO", "SV"], chunk_days=7)
        if not warnings:
            return {"labelled_pos": 0, "labelled_neg": 0,
                    "label_note": "no warnings fetched"}
        p, n, _ = auto_label(TRAINING_DATA, warnings, dry_run=False, overwrite=False,
                             strict_tornado=True,
                             window_start=start, window_end=end)
        return {"labelled_pos": p, "labelled_neg": n}

    def _train_candidate(self, target: str = "rotation") -> dict:
        """Fit in a subprocess so a crash cannot take the dashboard with it."""
        CANDIDATE_PATH.unlink(missing_ok=True)
        # Per-target candidate, so a severe cycle cannot overwrite a rotation
        # candidate that has not been decided on yet.
        cand = (CANDIDATE_PATH if target == "rotation"
                else DATA_DIR / "severe_model.candidate.joblib")
        metrics_path = cand.with_suffix(".metrics.json")
        metrics_path.unlink(missing_ok=True)

        # PyInstaller passes unknown argv through to the entry script, so under
        # a frozen build the subprocess command below would start another
        # dashboard-backend instead of a trainer -- a second backend competing
        # for port 3074, the NWWS socket and the radar poller. Train in-process
        # there instead.
        if getattr(sys, "frozen", False) or self._cfg("auto_retrain_in_process", False):
            return self._train_in_process(target, cand, metrics_path)

        cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "train_rotation_model.py"),
               "--data", str(TRAINING_DATA), "--out", str(cand),
               "--target", target]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env,
                                  capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "training timed out after 1 h"}

        if proc.returncode != 0 or not metrics_path.exists():
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            return {"ok": False, "error": f"trainer exited {proc.returncode}",
                    "stderr": " | ".join(tail)}
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"ok": False, "error": f"unreadable metrics: {e}"}
        return {"ok": True, "metrics": metrics}

    def _free_ram_gb(self) -> Optional[float]:
        """Available physical memory, or None when it cannot be determined.

        None must NOT be read as "plenty" -- the caller treats an unknown as a
        reason to proceed with a warning rather than a reason to refuse, because
        refusing forever on an unmeasurable box is worse than one slow retrain.
        """
        try:
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MS()
            st.dwLength = ctypes.sizeof(_MS)
            # Declare the signature: an undeclared restype truncates the return
            # on 64-bit and the call silently reports success either way.
            fn = ctypes.windll.kernel32.GlobalMemoryStatusEx
            fn.argtypes = [ctypes.POINTER(_MS)]
            fn.restype = ctypes.c_int
            if not fn(ctypes.byref(st)):
                return None
            return st.ullAvailPhys / (1024 ** 3)
        except Exception:  # noqa: BLE001 - non-Windows, or no ctypes
            try:
                import psutil
                return psutil.virtual_memory().available / (1024 ** 3)
            except Exception:  # noqa: BLE001
                return None

    def _train_in_process(self, target: str, cand: Path, metrics_path: Path) -> dict:
        """Fit inside the backend process. Used when no subprocess is possible.

        Runs on the thread `run_cycle` already hands us, so the event loop keeps
        serving -- but the memory is the backend's, hence the floor check.
        """
        floor = float(self._cfg("auto_retrain_min_free_ram_gb", 3.0))
        free = self._free_ram_gb()
        if free is not None and free < floor:
            return {"ok": False, "error": (
                f"in-process training needs about {floor:.0f} GB free and only "
                f"{free:.1f} GB is available. Skipping this cycle rather than "
                "pushing the machine into swap during live operations.")}
        if free is None:
            logger.warning("[retrain] could not read free memory; training anyway")

        try:
            sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
            from scripts.train_rotation_model import run_training
        except ImportError as e:
            return {"ok": False, "error": (
                f"the trainer is not in this build ({e}) - scripts/ must be "
                "collected by the PyInstaller spec")}

        logger.info(f"[retrain] training {target} in-process (free RAM "
                    f"{'unknown' if free is None else f'{free:.1f} GB'})")
        try:
            res = run_training(TRAINING_DATA, cand, target=target)
        except MemoryError:
            return {"ok": False, "error": "ran out of memory during in-process training"}
        except Exception as e:  # noqa: BLE001 - a crash here must not kill the backend
            logger.exception("[retrain] in-process training raised")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        if not res.get("ok"):
            return {"ok": False, "error": res.get("error", "training failed")}
        if not metrics_path.exists():
            return {"ok": False, "error": "trainer wrote no metrics sidecar"}
        return {"ok": True, "metrics": res["metrics"]}

    def _decide(self, cand_metrics: dict) -> dict:
        """Score the incumbent on the candidate's holdout and compare."""
        import joblib

        sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
        from scripts.train_rotation_model import (
            convective_day, evaluate, feature_row, MIN_HOLDOUT_POS,
        )

        cand_hold = cand_metrics.get("holdout") or {}
        if cand_hold.get("degenerate") or cand_hold.get("n_pos", 0) < MIN_HOLDOUT_POS:
            return {"promote": False,
                    "why": f"holdout has {cand_hold.get('n_pos', 0)} positives "
                           f"(need {MIN_HOLDOUT_POS}) - cannot compare fairly",
                    "candidate": cand_hold}

        # Absolute skill first.  Nothing about the incumbent can make an
        # unskilled candidate fit to go on air.
        floor = self._skill_floor(cand_hold)
        if floor:
            return {"promote": False, "why": f"candidate rejected: {floor}",
                    "candidate": cand_hold}

        if not MODEL_PATH.exists():
            return {"promote": True, "why": "no incumbent model in production",
                    "candidate": cand_hold}

        hold_days = set(cand_metrics.get("holdout_days") or [])
        if not hold_days:
            return {"promote": False, "why": "candidate recorded no holdout days",
                    "candidate": cand_hold}

        X, y = [], []
        try:
            with TRAINING_DATA.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("label") is None:
                        continue
                    if convective_day(rec.get("ts") or "") not in hold_days:
                        continue
                    # feature_row, NOT a plain 0.0-filled comprehension: an
                    # absent dual-pol reading has to arrive as NaN here for the
                    # same reason it does at fit and at inference time. Building
                    # this matrix differently judges both models on a
                    # distribution neither of them was trained on.
                    X.append(feature_row(rec.get("features") or {}))
                    y.append(1 if rec["label"] else 0)
        except OSError as e:
            return {"promote": False, "why": f"could not rebuild holdout: {e}"}

        if not X or sum(y) < MIN_HOLDOUT_POS:
            return {"promote": False,
                    "why": "rebuilt holdout is too small to compare",
                    "candidate": cand_hold}

        try:
            incumbent = joblib.load(MODEL_PATH)
            inc_hold = evaluate(incumbent, X, y, label="incumbent")
        except Exception as e:
            return {"promote": True,
                    "why": f"incumbent will not load or score ({e}) - replacing it",
                    "candidate": cand_hold}

        margin = float(self._cfg("auto_retrain_min_improvement", 0.005))
        c_ap, i_ap = cand_hold.get("ap", 0.0), inc_hold.get("ap", 0.0)
        # Average precision is the primary comparison: on a dataset this
        # imbalanced ROC-AUC barely moves while the precision that matters on
        # air can halve.  Brier only ever breaks a tie BETWEEN two models that
        # already clear the floor above -- see MIN_USEFUL_AUC for why.
        better = c_ap > i_ap + margin
        tie = abs(c_ap - i_ap) <= margin
        if tie:
            better = cand_hold.get("brier", 1.0) < inc_hold.get("brier", 1.0) - 1e-4
            why = (f"AP tied ({c_ap:.5f} vs {i_ap:.5f}); "
                   f"Brier {cand_hold.get('brier'):.5f} vs {inc_hold.get('brier'):.5f}")
        else:
            why = f"AP {c_ap:.5f} vs incumbent {i_ap:.5f} (margin {margin})"

        return {"promote": bool(better), "why": why,
                "candidate": cand_hold, "incumbent": inc_hold,
                "holdout_days": sorted(hold_days)}

    @staticmethod
    def _skill_floor(hold: dict) -> Optional[str]:
        """Reason the model is not fit to go live at all, or None if it is.

        Applied to the candidate BEFORE any comparison with the incumbent.
        A real candidate reached this point with ROC-AUC 0.457 and ZERO true
        positives and was voted through on Brier score, because at a 0.02%
        base rate a model that answers "no rotation" to everything scores a
        near-perfect Brier.  Discrimination is therefore a precondition, not
        something calibration can substitute for.
        """
        from scripts.train_rotation_model import MIN_AP_LIFT, MIN_USEFUL_AUC

        auc = hold.get("auc", 0.0)
        if auc < MIN_USEFUL_AUC:
            return (f"held-out ROC-AUC {auc:.3f} is below the {MIN_USEFUL_AUC} "
                    "floor - the model is not ranking storms")
        n, n_pos = hold.get("n", 0), hold.get("n_pos", 0)
        base = (n_pos / n) if n else 0.0
        ap = hold.get("ap", 0.0)
        if base > 0 and ap < base * MIN_AP_LIFT:
            return (f"held-out AP {ap:.5f} is under {MIN_AP_LIFT}x the "
                    f"{base:.5f} base rate - no better than guessing")
        # Judge "catches nothing" at the model's OWN operating point, not at a
        # fixed 0.45 probability.  A calibrated model on a ~3% base rate scores
        # almost nothing above 0.45, so the fixed threshold reported tp=0 for a
        # model with held-out AUC 0.797 and 7x-base-rate AP -- this check would
        # have rejected the first genuinely skilful candidate we produced.
        tp = hold.get("op_tp", hold.get("tp", 0))
        if tp == 0:
            return "catches nothing at any usable threshold (0 true positives)"
        return None

    def _promote(self) -> None:
        if MODEL_PATH.exists():
            shutil.copy2(MODEL_PATH, PREVIOUS_PATH)
        os.replace(CANDIDATE_PATH, MODEL_PATH)
        cand_metrics = CANDIDATE_PATH.with_suffix(".metrics.json")
        if cand_metrics.exists():
            os.replace(cand_metrics, MODEL_PATH.with_suffix(".metrics.json"))

    async def _reload_tracker(self) -> None:
        try:
            from .storm_tracking_service import get_storm_tracking_service
            svc = get_storm_tracking_service()
            if svc is not None:
                await asyncio.to_thread(svc.load_models)
                logger.info("[retrain] storm tracker reloaded the new model")
        except Exception as e:
            logger.warning(f"[retrain] tracker reload failed (new model is on disk "
                           f"and will load at next restart): {e}")

    def rollback(self) -> dict:
        """Restore the previous model.  Manual escape hatch."""
        if not PREVIOUS_PATH.exists():
            return {"ok": False, "error": "no previous model kept"}
        if MODEL_PATH.exists():
            shutil.copy2(MODEL_PATH, CANDIDATE_PATH)
        shutil.copy2(PREVIOUS_PATH, MODEL_PATH)
        self._record("rolled_back", {})
        return {"ok": True}

    # ── scheduler ────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        # Settle before the first check so startup is not competing with a fit.
        await asyncio.sleep(300)
        while self._running:
            try:
                interval_h = float(self._cfg("auto_retrain_interval_hours", 24))
                last = self.state.get("last_run", {}).get("at")
                due = True
                if last:
                    try:
                        age = (datetime.now(timezone.utc)
                               - datetime.fromisoformat(last)).total_seconds()
                        due = age >= interval_h * 3600
                    except ValueError:
                        due = True
                if due:
                    result = await self.run_cycle()
                    logger.info(f"[retrain] cycle: {result.get('outcome')} "
                                f"- {result.get('reason') or result.get('why', '')}")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[retrain] scheduler tick failed")
            # Re-check hourly; the due-test above decides whether to act.
            await asyncio.sleep(3600)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[retrain] auto-retraining scheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def status(self) -> dict:
        metrics = {}
        mp = MODEL_PATH.with_suffix(".metrics.json")
        if mp.exists():
            try:
                metrics = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "enabled": bool(self._cfg("auto_retrain_enabled", False)),
            "running": self._running,
            "phase": self._current,
            "interval_hours": self._cfg("auto_retrain_interval_hours", 24),
            "model_exists": MODEL_PATH.exists(),
            "can_rollback": PREVIOUS_PATH.exists(),
            "last_run": self.state.get("last_run"),
            "last_promotion": self.state.get("last_promotion"),
            "live_model": {
                "trained_at": metrics.get("trained_at"),
                "n_rows": metrics.get("n_rows"),
                "n_pos": metrics.get("n_pos"),
                "holdout": metrics.get("holdout"),
            } if metrics else None,
            "history": self.state.get("history", [])[-10:],
        }


_service: Optional[ModelTrainingService] = None


def get_model_training_service() -> Optional[ModelTrainingService]:
    return _service


def create_model_training_service(settings=None) -> ModelTrainingService:
    global _service
    if _service is None:
        _service = ModelTrainingService(settings=settings)
    return _service
