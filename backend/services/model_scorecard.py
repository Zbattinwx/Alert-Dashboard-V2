"""
TBF Escalation Index -- live scorecard.

Held-out metrics answer "how did this model do on 21 days of history". They do
not answer "is it working right now, on this radar, this season, after the last
promotion". Those diverge: the holdout is a fixed slice of the past, while the
live population shifts with the season, the sites in use, and every retrain.

No new prediction store is needed. Every row the live collector writes already
carries `p_rotation_model` / `p_severe_model` AS SCORED AT THE TIME, and the
labeller later fills in `label` / `label_source` from the warnings that actually
followed. So a row that has both is a completed prediction, and the archive is
already a prediction log. This reads the tail of it and scores those rows the
same way the trainer scores its holdout.

Three things this is careful about, all of which would otherwise flatter it:

  * A row whose probability is None is SKIPPED, never counted as a confident
    miss. None means the tracker could not score the cell (no model loaded, or
    an unusable feature vector) -- scoring it as a 0 would invent a prediction
    the system never made, and would make a broken model look merely cautious.
  * The rotation target EXCLUDES severe-only rows, exactly as training does.
    Counting an SVR-warned cell as a rotation false positive would punish the
    model for a cell a forecaster did warn on.
  * Rows are only counted once labelling has reached them. An unlabelled recent
    row is pending, not negative -- otherwise the last few hours always look
    like a wall of false alarms, because the warnings have not been fetched yet.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Operating thresholds, matched to the app's data/stormModel.ts. Kept as a
# fallback only: the real value is read from each model's metrics sidecar so a
# retrain that shifts the operating point cannot leave the scorecard measuring
# the old one.
DEFAULT_OP = {"rotation": 0.51, "severe": 0.50}

TARGETS = ("rotation", "severe")
PROB_FIELD = {"rotation": "p_rotation_model", "severe": "p_severe_model"}


def _target_truth(rec: dict, target: str) -> Optional[int]:
    """1 / 0 / None(exclude) -- must mirror train_rotation_model.target_label."""
    lab = rec.get("label")
    if lab is None:
        return None
    src = rec.get("label_source") or ""
    if target == "severe":
        return 1 if lab else 0
    if not lab:
        return 0
    if src.startswith("TO"):
        return 1
    return None  # SVR-only: ambiguous for rotation, as in training


def _op_threshold(target: str, data_dir: Path) -> float:
    """Prefer the operating point the live model actually measured."""
    name = "rotation_model" if target == "rotation" else "severe_model"
    mp = data_dir / f"{name}.metrics.json"
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
        v = float(m.get("holdout", {}).get("op_threshold"))
        if 0.0 < v < 1.0:
            return v
    except Exception:  # noqa: BLE001 - missing sidecar is normal on a fresh box
        pass
    return DEFAULT_OP[target]


def _iter_recent(path: Path, since: datetime) -> Iterable[dict]:
    """Stream rows at or after `since`.

    The archive is append-ordered by scan time, so this could binary-search --
    but a rewrite by the labeller can reorder it, and a scorecard that silently
    reads the wrong window is worse than one that takes a few seconds.
    """
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts") or ""
            if not ts:
                continue
            try:
                when = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= since:
                yield rec


def _score(rows: list[tuple[float, int]], thr: float) -> dict:
    tp = sum(1 for p, y in rows if p >= thr and y == 1)
    fp = sum(1 for p, y in rows if p >= thr and y == 0)
    fn = sum(1 for p, y in rows if p < thr and y == 1)
    tn = sum(1 for p, y in rows if p < thr and y == 0)
    n = len(rows)
    pos = tp + fn
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / pos if pos else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
    base = pos / n if n else None
    return {
        "n": n, "positives": pos, "base_rate": base, "threshold": round(thr, 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": prec, "recall": rec, "f1": f1,
        # Precision relative to climatology: 1.0 means the alerts are no better
        # than picking cells at random, which is the number that actually says
        # whether the model is earning its place.
        "lift": (prec / base) if (prec is not None and base) else None,
    }


def scorecard(data_path: Path, data_dir: Path, days: int = 14) -> dict:
    """Rolling live performance over the trailing `days`."""
    data_path, data_dir = Path(data_path), Path(data_dir)
    out = {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "targets": {},
    }
    if not data_path.exists():
        out["error"] = f"no training archive at {data_path}"
        return out

    since = datetime.now(timezone.utc) - timedelta(days=days)
    scored: dict[str, list[tuple[float, int]]] = {t: [] for t in TARGETS}
    seen = pending = unscored = 0

    for rec in _iter_recent(data_path, since):
        seen += 1
        if rec.get("label") is None:
            pending += 1          # labelling has not reached it yet
            continue
        any_prob = False
        for t in TARGETS:
            p = rec.get(PROB_FIELD[t])
            if p is None:
                continue
            any_prob = True
            y = _target_truth(rec, t)
            if y is None:
                continue          # excluded for this target, as in training
            scored[t].append((float(p), y))
        if not any_prob:
            unscored += 1

    out["rows_in_window"] = seen
    out["awaiting_labels"] = pending
    # Rows the tracker never scored. A large number here is the signal that the
    # models are not loading -- the exact failure that went unnoticed for months
    # because it was logged as "running pure physics".
    out["unscored"] = unscored
    for t in TARGETS:
        rows = scored[t]
        if not rows:
            out["targets"][t] = {"n": 0, "note": "no completed predictions in window"}
            continue
        out["targets"][t] = _score(rows, _op_threshold(t, data_dir))
    return out


def verdict(card: dict) -> str:
    """One line an operator can read without knowing what average precision is."""
    parts = []
    for t in TARGETS:
        d = card.get("targets", {}).get(t) or {}
        if not d.get("n"):
            continue
        p, lift = d.get("precision"), d.get("lift")
        if p is None:
            continue
        label = "tornado" if t == "rotation" else "severe"
        if lift and lift >= 2:
            parts.append(f"{label}: {p*100:.0f}% of alerts verified ({lift:.0f}x climatology)")
        else:
            parts.append(f"{label}: {p*100:.0f}% verified - no better than chance")
    if not parts:
        return "Not enough completed predictions yet to judge."
    return " | ".join(parts)
