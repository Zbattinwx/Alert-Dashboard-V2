"""
Rotation Classifier — Training & Evaluation
=============================================
Trains a gradient-boosted classifier to distinguish real mesocyclones from
noise, environmental shear, and algorithm artefacts.

The model supplements (and can eventually replace) the physics-based
rotation_detected flag in storm_tracking_service.py.

## Workflow

Normally none of this is run by hand — `backend/services/model_training_service.py`
performs the whole cycle on a schedule and promotes the result only if it beats
the live model on held-out days.  The manual path still exists:

1.  Collect unlabeled data: the live QA reporter appends every tracked cell to
    data/training_data.jsonl when `live_qa_log_training_data` is on.

2.  Apply ground-truth labels from NWS warning polygons:
        python scripts/label_from_warnings.py --days 10 --strict-tornado
    (`--all --overwrite` re-labels the entire archive; that is a maintenance
    action, not something the scheduled job does.)

3.  Train:
        python scripts/train_rotation_model.py [--out candidate.joblib]

4.  Read the held-out numbers and the metrics sidecar written beside the model.

5.  In the live system the storm tracker calls load_rotation_model() from this
    file and predict_rotation(cell) on each TrackedStormCell.

## Reading the numbers

The only scores reported are on rows withheld from fitting AND from calibration,
grouped by convective day.  An earlier version split randomly, which put one
storm's consecutive scans on both sides of the split and reported memorisation
as skill.  Watch **average precision**, not ROC-AUC: at the real class balance
(~0.25% of tracked cells are under a tornado warning) AUC stays flattering while
the precision that matters on air moves a lot.

## Features used (25 total — all computed or derived by the storm tracker)

Reflectivity/structure:
    max_dbz, area_km2, vil_kg_m2, cell_top_km, cell_base_km, depth_km,
    max_ref_height_km, centroid_height_km

Dual-pol:
    mean_cc, min_cc, mean_zdr

Grid-based rotation (noisiest — kept for completeness):
    rot_velocity_ms

LLSD — Low-Level Shear Detection (top single feature per research):
    llsd_max_shear, llsd_elevation_deg

Multi-tilt rotation profile (second most reliable):
    max_rot_vel_profile_ms, max_rot_height_km, rotation_depth_km

Motion:
    motion_speed_kph, motion_dir_deg

Composite score:
    score_rotation

Trend features — rate of change per scan (5-scan window, ≈25 min):
    llsd_trend, rot_vel_trend, vil_trend, echo_top_trend, dbz_trend
    These are the most predictive for PRE-WARNING detection.
    Research (TorNet 2025, WAF 2023) shows temporal trends outperform
    snapshots for lead times of 10-15 minutes.

## Ground truth labeling (use label_from_warnings.py, NOT label_from_lsr.py)

NWS tornado warnings via IEM SBW archive are far better labels than LSRs:
  - Issued when a forecaster SEES rotation on radar (even if no tornado drops)
  - IEM archive: https://mesonet.agron.iastate.edu/request/gis/watchwarn.phtml
  - SVR (severe thunderstorm) warnings give "strong convection" labels
  - MRMS rotation tracks from AWS s3://noaa-mrms-pds/ are a feature input,
    NOT a label (using them as labels is circular — they compute the same
    azimuthal shear we already compute)

## Model

HistGradientBoostingClassifier (sklearn), isotonically calibrated on a
day-disjoint holdout — ~5 ms inference per scan.  Outputs p_rotation in [0, 1].
Decision threshold: 0.45 (configurable; lower = more sensitive, more FP).

Histogram boosting rather than exact boosting because correct labelling grew the
archive to ~400k labelled rows, where exact boosting's per-split sort makes a
CV + fit run take tens of minutes — too slow for an unattended daily job.  It is
also the published choice for this task (HGBT beat a U-Net for SPC-style
probabilistic severe guidance, arXiv 2603.20250).
"""

import datetime as _dt
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TRAINING_DATA = PROJECT_ROOT / "data" / "training_data.jsonl"
MODEL_OUT      = PROJECT_ROOT / "data" / "rotation_model.joblib"

FEATURE_NAMES = [
    # Reflectivity / structure
    "max_dbz",
    "area_km2",
    "vil_kg_m2",
    "cell_top_km",
    "cell_base_km",
    "depth_km",
    "max_ref_height_km",
    "centroid_height_km",
    # Dual-pol
    "mean_cc",
    "min_cc",
    "mean_zdr",
    # Grid-based rotation (noisy but available)
    "rot_velocity_ms",
    # LLSD — top single feature per published research
    "llsd_max_shear",
    "llsd_elevation_deg",
    # Multi-tilt rotation profile
    "max_rot_vel_profile_ms",
    "max_rot_height_km",
    "rotation_depth_km",
    # Motion
    "motion_speed_kph",
    "motion_dir_deg",
    # Composite score component
    "score_rotation",
    # Trend features (rate of change per 5-scan window ≈ 25 min)
    # These are the key pre-warning signal — a storm whose LLSD is
    # rapidly increasing is far more dangerous than one holding steady.
    "llsd_trend",
    "rot_vel_trend",
    "vil_trend",
    "echo_top_trend",
    "dbz_trend",
    # MRMS multi-radar fused rotation features.  Sampled at cell lat/lon from
    # the cached MRMS rotation service.  Default to 0.0 for rows collected
    # before this feature was wired in; backfill via scripts/backfill_mrms_features.py
    # is recommended before retraining to actually exploit these.
    "mrms_rotation_track_30min",
    "mrms_azshear_0_2km",
    # ── Total lightning (GLM) ─────────────────────────────────────────────
    # The rate says the storm is electrified; the TREND is the lightning jump,
    # which precedes severe reports by roughly 20 minutes because it tracks the
    # updraft strengthening rather than the precipitation that results. That is
    # lead time no reflectivity feature can give. The tracker already computed
    # the rate for its severity score and threw it away.
    "flash_rate_fpm",
    "flash_rate_trend",
    # ── Kinematic wind signatures ─────────────────────────────────────────
    # These were computed every scan for the severity score and never given to
    # the classifier -- the same oversight as the lightning rate. It shows:
    # trained WITHOUT them, wind_severe reached 0.574 held-out AUC against
    # hail_1in's 0.815 on MORE positives, because the model was being asked to
    # predict damaging wind from reflectivity shape and rotation while the
    # actual wind signatures were withheld. Divergent outflow (downburst dV)
    # and mid-altitude radial convergence (MARC) are the published precursors,
    # MARC by 10-20 minutes.
    #
    # Magnitudes are NaN when not detected, not 0.0: "no downburst signature"
    # and "a downburst of 0 m/s" are different statements. The booleans carry
    # the detected/not answer.
    "downburst_delta_v_ms",
    "marc_convergence_ms",
    "max_wind_velocity_ms",
    "strong_wind_swath_km2",
    "downburst_detected",
    "marc_signature_detected",
    "rij_detected",
    # ── Near-storm environment (dashboard >= 2026-09-09) ──────────────────
    # Sampled from the hourly mesoanalysis grids at the cell's own lat/lon; see
    # backend/services/storm_environment.py. Until this shipped the classifier
    # was radar-only, so the same 55 dBZ core with the same couplet looked
    # identical in 0 SRH and in 300 -- and environment plus storm structure is
    # exactly what the literature says decides mode and tornado potential
    # (Thompson et al. 2012).
    #
    # Every row collected BEFORE this shipped carries NaN here and always will,
    # so these features earn their place only as new data accumulates. That is
    # expected, not a bug -- and it is why absent must be NaN, never 0.0: zero
    # CAPE is a real atmosphere, a missing grid is not.
    "env_mlcape",
    "env_mucape",
    "env_mlcin",
    "env_shear06",
    "env_srh01",
    "env_efhl",
    "env_mllcl",
    "env_stp",
    "env_scp",
    "env_ship",
    "env_lapse75",
    "env_pwat",
]

DECISION_THRESHOLD = 0.45

# The most recent N convective days are withheld from fitting entirely, so a
# freshly trained candidate and the incumbent it would replace can be scored on
# the same never-seen data.  Each retrain excludes the newest window, and an
# older incumbent's cutoff is older still, so neither has seen it.
HOLDOUT_DAYS = 21

# Below this many positives the holdout cannot discriminate between two models
# and the promotion gate must not act on it.
MIN_HOLDOUT_POS = 25

# ...and they must be spread across at least this many convective days, or the
# comparison is really a comparison on one storm.
MIN_HOLDOUT_POS_DAYS = 3

# Absolute skill floor a candidate must clear before it is eligible at all.
#
# These exist because of a real near-miss: a candidate with AUC 0.457 and ZERO
# true positives was voted through on Brier score alone.  At this class balance
# (~0.02% of held-out rows are positive) a model that predicts ~0 for every row
# earns a near-perfect Brier while catching nothing, so calibration can only be
# a tie-break BETWEEN models that already discriminate -- never a reason to
# promote one that does not.
MIN_USEFUL_AUC = 0.60      # below this it is not ranking storms at all
MIN_AP_LIFT = 2.0          # AP must beat the base rate by this factor


# ── Data loading ──────────────────────────────────────────────────────────────

def convective_day(ts: str) -> str:
    """Map a scan timestamp to its CONVECTIVE day (12Z -> 12Z), as YYYY-MM-DD.

    This is the grouping unit for every split below.  Calendar days would cut a
    severe evening in half — an Ohio event running 22Z to 04Z would put its
    first hours in train and its last hours in test, which is the leak we are
    trying to remove, not a split.
    """
    from datetime import datetime, timedelta
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return "unknown"
    return (dt - timedelta(hours=12)).strftime("%Y-%m-%d")


# Features that are only ever absent, never legitimately zero.  Copolar
# correlation for a weather target sits at 0.8-1.0; CC at exactly 0.0 means the
# dual-pol analysis did not run, NOT total decorrelation.
#
# This distinction is not cosmetic.  CC collapsing toward zero IS the debris
# signature — the strongest single tornado indicator on radar — so storing
# "not computed" as 0.0 told the model that 391,471 ordinary storms (78% of the
# archive, every row collected before the dual-pol fix) looked like debris
# balls.  That is worse than missing data: it is confidently wrong data, and it
# doubles as a giveaway for which collection era a row came from.
#
# HistGradientBoostingClassifier handles NaN natively, learning a default branch
# direction per split, so "unknown" is representable and costs nothing.
ENV_PREFIX = "env_"          # absent environment is NaN, never 0.0

# Measurements that are legitimately zero AND legitimately absent, so a plain
# `or 0` would collapse two different states into one. Absent stays NaN.
OPTIONAL_FEATURES = ("flash_rate_fpm", "flash_rate_trend",
                     "downburst_delta_v_ms",
                     "marc_convergence_ms",
                     "max_wind_velocity_ms",
                     "strong_wind_swath_km2")

# Features whose MEASUREMENT was wrong before a given date, and whose recorded
# values are therefore not comparable with what the tracker produces now.
#
# Azimuthal shear was computed over a kernel of a fixed RAY COUNT, so its
# denominator collapsed toward zero near the radar. Measured on the archive,
# median llsd_max_shear per range band: <10 km 0.0751, 10-25 0.0181,
# 25-50 0.0054, 50-100 0.0022, 100-150 0.0016, >=150 0.0015 -- a 50x fall, and
# 100% of rows inside 10 km cleared the "significant rotation" threshold. The
# column is very nearly a measurement of RANGE. `score_rotation` folds the same
# quantity into the severity score, and `llsd_trend` is its rate of change.
#
# The raw velocity was never stored, so these cannot be recomputed in place --
# only re-derived by re-running the backfill from Level 2. Until then, the
# honest value for an untrustworthy measurement is "unknown", not the number
# the broken instrument produced. NaN is representable to
# HistGradientBoosting; a wrong number is not.
#
# Blanking rather than DELETING the columns is deliberate: the kernel is fixed
# as of this date, so rows collected from here on carry good values and start
# contributing immediately, with no second schema change. Measured effect of
# removing the bad values (ablation, 740,706 rows): rotation held-out AP
# 0.0713 -> 0.0803, precision 0.136 -> 0.161; severe unchanged.
CONTAMINATED_BEFORE = {
    "llsd_max_shear": "2026-09-09",
    "llsd_trend":     "2026-09-09",
    "score_rotation": "2026-09-09",
}
DUALPOL_FEATURES = ("mean_cc", "min_cc", "mean_zdr")
DUALPOL_SENTINEL = "mean_cc"   # if this is 0/absent, none of them were computed

# Bumped when the saved bundle's SHAPE changes (not on every retrain).
# 1 = {"bundle_version", "model", "features", "target", "trained_at"}.
# Anything older is a bare estimator with no feature list; see
# model_paths.load_model_bundle for how that is handled.
BUNDLE_VERSION = 1


def feature_row(feats: dict) -> list:
    """Feature vector with genuinely-absent values as NaN rather than 0.0.

    Must stay in lockstep with `StormTrackingService._cell_to_feature_vector`,
    which does the same thing at inference time.
    """
    import math
    dual_missing = not feats.get(DUALPOL_SENTINEL)
    row = []
    for name in FEATURE_NAMES:
        if name in DUALPOL_FEATURES and dual_missing:
            row.append(math.nan)
        elif (name in CONTAMINATED_BEFORE or name in OPTIONAL_FEATURES
              or name.startswith(ENV_PREFIX)):
            # An absent environment field is NaN. It is absent for every row
            # collected before the environment join existed, and 0.0 there
            # would read as "no CAPE, no shear, no helicity" -- a specific and
            # wrong atmosphere -- across most of the archive.
            v = feats.get(name)
            row.append(math.nan if v is None else float(v))
        else:
            row.append(float(feats.get(name, 0.0)))
    return row


# ── Prediction targets ────────────────────────────────────────────────────────
# Both are derived from ONE non-strict labelling pass; see the module docstring
# for why SV.W is excluded from the rotation target rather than counted negative.
# Warning-based targets answer "would a forecaster act on this storm".
# Report-based targets answer "what did this storm actually do", which is the
# only way to get a hazard-specific answer: a severe thunderstorm warning does
# not record whether it was issued for hail or for wind.
TARGETS = ("rotation", "severe", "hail_1in", "hail_2in", "wind_severe", "wind_sig")

# The report-based ones, labelled by scripts/label_from_lsr_hazards.py into
# `hazard_labels`. Read the bias note in that module before trusting these as
# absolute frequencies: storm reports need someone present to make them, so a
# model trained on them learns "severe AND observed".
HAZARD_TARGETS = ("hail_1in", "hail_2in", "wind_severe", "wind_sig")


def target_label(rec: dict, target: str):
    """Map a labelled record to 1 / 0 for this target, or None to exclude it.

    Returning None is a real third outcome, not an error: the rotation target
    must DROP severe-thunderstorm-only rows. Calling them negative would teach
    the model that a rotating storm a forecaster warned on is a non-event.
    """
    if target in HAZARD_TARGETS:
        # A row is scored only if the hazard labeller actually ran over its
        # day. Absent means we never fetched reports for that date, which is
        # not the same as "no hail fell" -- calling it 0 would fill the
        # negative class with days we simply never looked at.
        haz = rec.get("hazard_labels")
        if not isinstance(haz, dict) or target not in haz:
            return None
        return int(haz[target])

    lab = rec.get("label")
    if lab is None:
        return None
    src = rec.get("label_source") or ""

    if target == "severe":
        # Any warning is a positive. A record labelled True can only have come
        # from a TO.W or SV.W match, so `lab` alone is the answer.
        return 1 if lab else 0

    # rotation
    if not lab:
        return 0
    if src.startswith("TO"):
        return 1
    return None      # SV.W-only: ambiguous, exclude


def _lead_meta(rec: dict, ts: str) -> dict:
    """Which warning this row belongs to, and how far it precedes issuance.

    Lead time is the only metric that says whether a PRE-warning detector
    works, and it cannot be computed from the feature vector -- it needs the
    warning's identity and issue time. `minutes_before_warning` is written by
    the current labeller; for rows labelled before that existed it is
    reconstructed from label_issued, so the whole archive stays measurable.

    The warning id is (issue time, product). Two different offices issuing in
    the same second would collide, which is rare and would merge two storms in
    one median -- acceptable next to having no lead-time measurement at all.
    """
    issued = rec.get("label_issued")
    if not issued or not rec.get("label"):
        return {}
    lead = rec.get("minutes_before_warning")
    if lead is None:
        try:
            from datetime import datetime as _d
            lead = (_d.fromisoformat(issued) - _d.fromisoformat(ts)).total_seconds() / 60.0
        except (ValueError, TypeError):
            return {}
    return {"warning_id": f"{issued}|{rec.get('label_source') or ''}",
            "lead_min": float(lead)}


def load_labeled_records(path: Path, target: str = "rotation"):
    """Return (X_rows, y_labels, groups, times) for all labeled records.

    `target` selects which question is being asked of the same archive --
    "will this cell be tornado-warned" or "will it be warned at all". See
    TARGETS / target_label.

    `groups` is the convective day of each row.  It exists because storm cells
    are tracked across consecutive volume scans, so one storm contributes dozens
    of near-identical rows a few minutes apart.  Any split that separates those
    rows at random puts scan N in train and scan N+1 in validation and reports
    an AUC that measures memorisation, not skill.
    """
    X, y, groups, times, meta = [], [], [], [], []
    skipped = 0
    blanked: dict[str, int] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            lab = target_label(rec, target)
            if lab is None:
                # Unlabelled, or excluded by this target (SV.W under rotation).
                skipped += 1
                continue
            feats = rec.get("features") or {}
            # Blank measurements taken before their instrument was fixed.
            ts_day = (rec.get("ts") or "")[:10]
            if ts_day:
                for name, fixed_on in CONTAMINATED_BEFORE.items():
                    if ts_day < fixed_on and name in feats:
                        feats = dict(feats)
                        feats[name] = None
                        blanked[name] = blanked.get(name, 0) + 1
            X.append(feature_row(feats))
            y.append(lab)
            ts = rec.get("ts") or ""
            groups.append(convective_day(ts))
            times.append(ts)
            meta.append(_lead_meta(rec, ts))

    print(f"Loaded {len(X)} labeled records for target={target!r} "
          f"({skipped} skipped / unlabeled / excluded)")
    if blanked:
        for name, n in sorted(blanked.items()):
            print(f"  blanked {n:,} pre-{CONTAMINATED_BEFORE[name]} values of "
                  f"{name} (measured with the broken kernel)")
    pos = sum(y)
    kind = {
        "rotation": "tornado-warned",
        "severe": "warned (SVR or TOR)",
        "hail_1in": 'hail >= 1" reported',
        "hail_2in": 'hail >= 2" reported',
        "wind_severe": "wind >= 50 kt reported",
        "wind_sig": "wind >= 65 kt reported",
    }.get(target, target)
    print(f"  Positives ({kind}): {pos}  Negatives: {len(y)-pos}")
    print(f"  Convective days: {len(set(groups))}")
    return X, y, groups, times, meta


# ── Training ──────────────────────────────────────────────────────────────────

def evaluate(model, X, y, label="holdout") -> dict:
    """Score a fitted model on data it has never seen.

    Returned verbatim into the metrics sidecar and read by the promotion gate,
    so every number here must come from held-out rows only.
    """
    import numpy as np
    from sklearn.metrics import (
        average_precision_score, brier_score_loss, confusion_matrix,
        roc_auc_score,
    )

    Xa, ya = np.asarray(X, dtype=float), np.asarray(y, dtype=int)
    n_pos = int(ya.sum())
    out = {"set": label, "n": int(len(ya)), "n_pos": n_pos,
           "n_neg": int(len(ya) - n_pos)}
    if n_pos == 0 or n_pos == len(ya):
        # A single-class holdout cannot rank anything.  Say so rather than
        # emitting a number the promotion gate would compare against.
        out["degenerate"] = True
        return out

    p = model.predict_proba(Xa)[:, 1]
    pred = (p >= DECISION_THRESHOLD).astype(int)
    tn, fp, fn, tp = confusion_matrix(ya, pred, labels=[0, 1]).ravel()

    # A FIXED probability threshold is meaningless across base rates.  0.45 was
    # chosen when the archive was 60% positive (from mislabelled data); at the
    # real ~3% rate an isotonically-calibrated model's scores barely reach it, so
    # a genuinely skilful model reported tp=0 / precision=0 and would have been
    # thrown out by the promotion gate.  Derive the operating point from the
    # held-out data instead, and report both.
    from sklearn.metrics import precision_recall_curve
    prec, rec, thr = precision_recall_curve(ya, p)
    # precision_recall_curve returns one more prec/rec than thresholds.
    prec, rec = prec[:-1], rec[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        f1 = np.where((prec + rec) > 0, 2 * prec * rec / (prec + rec), 0.0)
    best = int(np.argmax(f1)) if len(f1) else 0
    op_thr = float(thr[best]) if len(thr) else DECISION_THRESHOLD
    op_pred = (p >= op_thr).astype(int)
    o_tn, o_fp, o_fn, o_tp = confusion_matrix(ya, op_pred, labels=[0, 1]).ravel()
    out.update(
        op_threshold=op_thr,
        op_precision=float(prec[best]) if len(prec) else 0.0,
        op_recall=float(rec[best]) if len(rec) else 0.0,
        op_f1=float(f1[best]) if len(f1) else 0.0,
        op_tp=int(o_tp), op_fp=int(o_fp), op_tn=int(o_tn), op_fn=int(o_fn),
    )

    out.update(
        auc=float(roc_auc_score(ya, p)),
        # Average precision is the number to watch on an imbalanced problem:
        # ROC-AUC stays flattering when negatives dominate, AP does not.
        ap=float(average_precision_score(ya, p)),
        brier=float(brier_score_loss(ya, p)),
        precision=float(tp / (tp + fp)) if (tp + fp) else 0.0,
        recall=float(tp / (tp + fn)) if (tp + fn) else 0.0,
        tp=int(tp), fp=int(fp), tn=int(tn), fn=int(fn),
        degenerate=False,
    )
    return out


def train(X, y, groups=None, times=None, out_path=None,
          holdout_days: int = HOLDOUT_DAYS, target: str = "rotation",
          meta=None):
    """Train a class-balanced, probability-calibrated rotation classifier.

    Every split here is grouped by CONVECTIVE DAY.  That is the whole point: a
    tracked storm emits one row per volume scan, so neighbouring rows are nearly
    identical, and the previous random `train_test_split` put scan N in train
    and scan N+1 in the calibration set.  The resulting ROC-AUC measured how
    well the model remembered a storm it had already seen — exactly the number
    an automated promotion gate must not be fed.

    Three splits, all day-disjoint:

    1. **Temporal holdout** - the most recent `holdout_days` convective days are
       removed before anything is fitted, and no model ever trains on them.
       Because each retrain excludes the newest window, a previously-trained
       incumbent (whose own cutoff is older) has not seen the current holdout
       either, so scoring both on it is a fair comparison.
    2. **Calibration set** - a day-disjoint 20% of what remains, so the isotonic
       map is fitted on rows the base model has not seen.
    3. **GroupKFold CV** - reported for stability, grouped the same way.

    Returns (calibrated_model, metrics_dict).
    """
    import numpy as np
    from sklearn.base import clone
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold, GroupShuffleSplit
    from sklearn.pipeline import Pipeline
    import joblib

    out_path = Path(out_path) if out_path else MODEL_OUT
    Xarr = np.array(X, dtype=float)
    yarr = np.array(y, dtype=int)
    garr = np.array(groups if groups is not None else ["all"] * len(yarr))

    metrics = {
        "trained_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "n_rows": int(len(yarr)),
        "n_pos": int(yarr.sum()),
        "n_days": int(len(set(garr.tolist()))),
        "features": list(FEATURE_NAMES),
        "decision_threshold": DECISION_THRESHOLD,
    }

    # -- 1. Temporal holdout by convective day --------------------------------
    days = sorted({d for d in garr.tolist() if d != "unknown"})
    hold_days = set(days[-holdout_days:]) if len(days) > holdout_days else set()
    if hold_days:
        hold_mask = np.isin(garr, list(hold_days))
        # Refuse a holdout that cannot discriminate: an all-negative window (a
        # quiet fortnight) would score every candidate identically and the gate
        # would promote on noise.
        if yarr[hold_mask].sum() < MIN_HOLDOUT_POS or (~hold_mask).sum() < 50:
            print(f"  holdout of {len(hold_days)} days holds "
                  f"{int(yarr[hold_mask].sum())} positives (< {MIN_HOLDOUT_POS})"
                  " - falling back to a grouped random holdout")
            hold_days = set()
    if not hold_days:
        gss0 = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
        _, ho_i = next(gss0.split(Xarr, yarr, garr))
        hold_mask = np.zeros(len(yarr), dtype=bool)
        hold_mask[ho_i] = True
        metrics["holdout_kind"] = "grouped_random"
    else:
        metrics["holdout_kind"] = "temporal"

    # Record the holdout days in BOTH cases.  The promotion gate has to score the
    # incumbent on exactly these rows, and it can only reconstruct them from this
    # list — an unreproducible holdout makes the comparison meaningless.
    metrics["holdout_days"] = sorted(set(garr[hold_mask].tolist()))

    X_hold, y_hold = Xarr[hold_mask], yarr[hold_mask]
    X_fit, y_fit, g_fit = Xarr[~hold_mask], yarr[~hold_mask], garr[~hold_mask]
    print(f"\nHoldout ({metrics['holdout_kind']}): {len(y_hold)} rows, "
          f"{int(y_hold.sum())} positive, "
          f"across {len(set(garr[hold_mask].tolist()))} day(s)")
    print(f"Training pool: {len(y_fit)} rows across {len(set(g_fit.tolist()))} day(s)")

    if len(set(y_fit.tolist())) < 2:
        raise SystemExit("Training pool has only one class - cannot fit.")

    # -- Class-balanced sample weights ----------------------------------------
    counts = np.bincount(y_fit, minlength=2)
    class_weights = len(y_fit) / (2 * np.maximum(counts, 1))
    w_fit = class_weights[y_fit]
    print("Class weights (inverse frequency):")
    for cls, w in enumerate(class_weights):
        name = "meso" if cls else "no-meso"
        print(f"  {name:<8} {w:.3f}  (count={counts[cls]})")

    # Histogram gradient boosting, not the exact GradientBoostingClassifier the
    # first version used.  Two reasons:
    #
    #  * Speed.  Correct labelling took the archive from 27k labelled rows to
    #    401k (most cells are not under a tornado warning, which is the point),
    #    and exact boosting sorts every feature at every split — a 5-fold CV plus
    #    a final fit ran into the tens of minutes, which is not something a daily
    #    unattended job can afford.  Binned splits handle this size in seconds.
    #  * It is the published choice for this task: the HGBT baseline beat a U-Net
    #    for SPC-style probabilistic severe guidance (arXiv 2603.20250).
    #
    # `early_stopping=False` is deliberate.  HistGB's internal early-stopping
    # split is RANDOM, which would put scan N in its fit and scan N+1 in its
    # validation set — reintroducing, inside the estimator, exactly the leak the
    # grouped splits above exist to remove.
    #
    # No scaler: tree splits are scale-invariant, and dropping it removes a
    # fitted transform from the artefact the tracker loads.
    base = Pipeline([
        ("clf", HistGradientBoostingClassifier(
            max_iter=300,
            learning_rate=0.06,
            max_leaf_nodes=31,
            min_samples_leaf=20,
            l2_regularization=1.0,
            early_stopping=False,
            random_state=42,
        )),
    ])

    # -- 2. Day-disjoint calibration split ------------------------------------
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    tr_idx, cal_idx = next(gss.split(X_fit, y_fit, g_fit))
    X_tr, y_tr, w_tr = X_fit[tr_idx], y_fit[tr_idx], w_fit[tr_idx]
    X_cal, y_cal = X_fit[cal_idx], y_fit[cal_idx]

    # -- 3. Grouped CV on the training portion only ---------------------------
    g_tr = g_fit[tr_idx]
    n_days_tr = len(set(g_tr.tolist()))
    if n_days_tr >= 3 and len(set(y_tr.tolist())) == 2:
        cv_aucs = []
        for a, b in GroupKFold(n_splits=min(5, n_days_tr)).split(X_tr, y_tr, g_tr):
            if len(set(y_tr[b].tolist())) < 2:
                continue
            m = clone(base)
            m.fit(X_tr[a], y_tr[a], clf__sample_weight=w_tr[a])
            cv_aucs.append(roc_auc_score(y_tr[b], m.predict_proba(X_tr[b])[:, 1]))
        if cv_aucs:
            metrics["cv_auc_mean"] = float(np.mean(cv_aucs))
            metrics["cv_auc_std"] = float(np.std(cv_aucs))
            print(f"\nGroupKFold CV ROC-AUC (day-disjoint): "
                  f"{np.mean(cv_aucs):.3f} +/- {np.std(cv_aucs):.3f}")

    base.fit(X_tr, y_tr, clf__sample_weight=w_tr)

    # -- Probability calibration on the day-disjoint calibration set ----------
    if len(set(y_cal.tolist())) == 2:
        try:
            from sklearn.frozen import FrozenEstimator
            calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="isotonic")
        except ImportError:
            calibrated = CalibratedClassifierCV(base, method="isotonic", cv="prefit")
        calibrated.fit(X_cal, y_cal)
        metrics["calibrated"] = True
    else:
        print("  calibration set is single-class - shipping the uncalibrated model")
        calibrated = base
        metrics["calibrated"] = False

    # -- Honest scoring on the untouched holdout ------------------------------
    hold = evaluate(calibrated, X_hold, y_hold, label=metrics["holdout_kind"])

    # ── Lead time on the holdout ──────────────────────────────────────────
    # AUC and AP are computed over every row, and about two thirds of the
    # positive rows are storms ALREADY under a warning -- easy to recognise and
    # not the job. A model can improve its AP while its lead time gets worse,
    # and until this was measured nobody could have seen that happen.
    if meta is not None and len(meta) == len(yarr):
        try:
            from backend.services.lead_time import lead_time_report, format_lead_report
            m_hold = [m for m, keep in zip(meta, hold_mask) if keep]
            p_hold = calibrated.predict_proba(X_hold)[:, 1]
            thr = hold.get("op_threshold") or DECISION_THRESHOLD
            rows = [
                {"warning_id": m.get("warning_id"),
                 "lead_min": m.get("lead_min"),
                 "p": float(pp)}
                for m, pp in zip(m_hold, p_hold) if m.get("warning_id")
            ]
            rep = lead_time_report(rows, threshold=float(thr))
            metrics["lead_time"] = rep
            print()
            print(format_lead_report(rep))
        except Exception as e:                              # noqa: BLE001
            print(f"  (lead-time report unavailable: {e})")
    metrics["holdout"] = hold
    print("\nHeld-out performance (never seen during fitting or calibration):")
    if hold.get("degenerate"):
        print("  holdout is single-class - no usable score")
    else:
        print(f"  ROC-AUC {hold['auc']:.3f}   AP {hold['ap']:.3f}   "
              f"Brier {hold['brier']:.4f}")
        print(f"  @{DECISION_THRESHOLD} (fixed):  precision {hold['precision']:.3f}  "
              f"recall {hold['recall']:.3f}  "
              f"(tp={hold['tp']} fp={hold['fp']} fn={hold['fn']})")
        print(f"  @{hold.get('op_threshold', 0):.3f} (best F1): precision "
              f"{hold.get('op_precision', 0):.3f}  recall {hold.get('op_recall', 0):.3f}  "
              f"(tp={hold.get('op_tp')} fp={hold.get('op_fp')} fn={hold.get('op_fn')})")

    # -- Feature importance, by permutation on the holdout --------------------
    # HistGB exposes no impurity importances, and that is no loss: impurity
    # importance is computed on training data and inflates high-cardinality
    # features.  Permutation importance measures the drop in held-out average
    # precision when one column is shuffled, which is the question actually
    # worth asking ("does this feature earn its place?").  The holdout is small,
    # so this stays cheap.
    if not hold.get("degenerate"):
        from sklearn.inspection import permutation_importance
        try:
            r = permutation_importance(
                calibrated, X_hold, y_hold, n_repeats=5,
                random_state=42, scoring="average_precision", n_jobs=1,
            )
            importances = sorted(zip(FEATURE_NAMES, r.importances_mean),
                                 key=lambda t: t[1], reverse=True)
            metrics["feature_importance"] = {k: float(v) for k, v in importances}
            print("\nPermutation importance (drop in held-out AP, top 10):")
            for name, imp in importances[:10]:
                bar = "#" * max(0, int(imp * 200))
                print(f"  {name:<28}  {imp:+.4f}  {bar}")
        except Exception as e:
            print(f"  (permutation importance unavailable: {e})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Save a BUNDLE, not a bare estimator: the feature list travels with the
    # model it was trained on.
    #
    # Without this, serving read the feature order from this module's
    # FEATURE_NAMES constant while `model_paths.find_model` prefers the RUNTIME
    # data/*.joblib over the bundled seed. So the moment the constant changed,
    # any deployment that kept its old model file fed an N-column vector to an
    # M-column estimator -- and the only symptom is every p_*_model quietly
    # becoming None. A model that cannot say which columns it wants is a model
    # you can never safely change the feature set of.
    joblib.dump(
        {
            "bundle_version": BUNDLE_VERSION,
            "model": calibrated,
            "features": list(FEATURE_NAMES),
            "target": target,
            "trained_at": metrics.get("trained_at"),
        },
        out_path,
    )
    metrics_path = out_path.with_suffix(".metrics.json")
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"\nModel saved to {out_path}")
    print(f"Metrics saved to {metrics_path}")
    return calibrated, metrics


# ── Inference helpers (imported by storm_tracking_service) ────────────────────

def load_rotation_model():
    """Load the trained model from disk.  Returns None if not found."""
    try:
        import joblib
        model = joblib.load(MODEL_OUT)
        print(f"[rotation_model] Loaded from {MODEL_OUT}")
        return model
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[rotation_model] Failed to load: {e}")
        return None


def predict_rotation(model, cell_features: dict) -> float:
    """
    Return p_rotation in [0, 1] for a single feature dict.
    Requires the same keys as FEATURE_NAMES.  Missing keys default to 0.
    """
    import numpy as np
    row = np.array(
        [float(cell_features.get(name, 0.0)) for name in FEATURE_NAMES],
        dtype=float,
    ).reshape(1, -1)
    return float(model.predict_proba(row)[0, 1])


# ── Label helper ──────────────────────────────────────────────────────────────

def label_stats(path: Path):
    """Print a summary of labeling progress."""
    total = labeled = pos = neg = 0
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
            if rec.get("label") is not None:
                labeled += 1
                if rec["label"]:
                    pos += 1
                else:
                    neg += 1
    print(f"Records: {total} total  {labeled} labeled ({total-labeled} unlabeled)")
    print(f"  Positives: {pos}  Negatives: {neg}")


# ── Callable entry point (used by the in-process retrain) ────────────────────

def run_training(data_path, out_path, target: str = "rotation",
                 holdout_days: int = HOLDOUT_DAYS,
                 min_rows: int = 20) -> dict:
    """Train one target and return a result dict. Never calls sys.exit.

    This is what the backend's retrain loop calls when it cannot spawn a
    subprocess. `main()` below is a thin argv wrapper over the same work, so the
    CLI and the in-process path cannot drift apart.

    Returns {"ok": bool, ...}; on success it carries "metrics" (the same dict
    written to the .metrics.json sidecar) so the caller need not re-read the
    file it just wrote.
    """
    data_path = Path(data_path)
    out_path = Path(out_path)
    if not data_path.exists():
        return {"ok": False, "error": f"no training data at {data_path}"}

    try:
        import sklearn  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "scikit-learn is not installed"}

    try:
        X, y, groups, times, meta = load_labeled_records(data_path, target=target)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not read training data: {e}"}

    if len(X) < min_rows:
        return {"ok": False,
                "error": f"only {len(X)} labeled rows for target={target!r}, "
                         f"need {min_rows}"}
    if len(set(y)) < 2:
        # One-class data fits happily and scores meaninglessly.
        return {"ok": False,
                "error": f"target={target!r} has only one class in {len(y)} rows"}

    try:
        train(X, y, groups=groups, times=times, out_path=out_path, target=target,
              holdout_days=holdout_days, meta=meta)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"training failed: {type(e).__name__}: {e}"}

    metrics = None
    mp = out_path.with_suffix(".metrics.json")
    if mp.exists():
        try:
            metrics = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metrics = None
    if metrics is None:
        return {"ok": False, "error": "trainer wrote no metrics sidecar"}
    return {"ok": True, "target": target, "rows": len(X),
            "positives": int(sum(y)), "metrics": metrics,
            "out": str(out_path)}


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Train the rotation classifier")
    parser.add_argument("--data", default=str(TRAINING_DATA),
                        help="Path to training_data.jsonl")
    parser.add_argument("--stats", action="store_true",
                        help="Print labeling stats and exit")
    parser.add_argument("--out", default=None,
                        help="Where to write the model. Defaults to the production "
                             "path; the auto-retrain loop points this at a candidate "
                             "file so a bad train never clobbers what is live.")
    parser.add_argument("--target", choices=TARGETS, default="rotation",
                        help="Which question to train: 'rotation' (tornado "
                             "warning, SVR-only rows excluded) or 'severe' "
                             "(any warning). Needs a NON-strict labelling pass "
                             "for 'severe' to see any SV.W positives.")
    parser.add_argument("--holdout-days", type=int, default=HOLDOUT_DAYS,
                        help="Most recent N convective days withheld from fitting.")
    args = parser.parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"No training data found at {data_path}")
        print("Run:  python live_qa.py --log   to start collecting data")
        print("Then label records (set 'label': true/false) and re-run.")
        sys.exit(0)

    if args.stats:
        label_stats(data_path)
        return

    X, y, groups, times, meta = load_labeled_records(data_path, target=args.target)
    if len(X) < 20:
        print(f"Only {len(X)} labeled records — need at least 20 to train.")
        print("Collect more data with live_qa.py --log and label it.")
        sys.exit(0)

    try:
        import sklearn  # noqa: F401
    except ImportError:
        print("scikit-learn is not installed. Run:  pip install scikit-learn joblib")
        sys.exit(1)

    train(X, y, groups=groups, times=times, out_path=args.out, target=args.target,
          holdout_days=args.holdout_days, meta=meta)


if __name__ == "__main__":
    main()
