"""
Rotation Classifier — Training & Evaluation
=============================================
Trains a gradient-boosted classifier to distinguish real mesocyclones from
noise, environmental shear, and algorithm artefacts.

The model supplements (and can eventually replace) the physics-based
rotation_detected flag in storm_tracking_service.py.

## Workflow

1.  Collect unlabeled data:
        python live_qa.py --log

2.  Apply ground-truth labels via SPC LSR crosscheck:
        python scripts/label_from_lsr.py   (see that script for details)

    OR label manually: open data/training_data.jsonl and set "label": true/false

3.  Train:
        python scripts/train_rotation_model.py

4.  Evaluate, inspect feature importances, save model to data/rotation_model.joblib

5.  To use in the live system, the storm tracker calls load_rotation_model() from
    this file and calls predict_rotation(cell) on each TrackedStormCell.

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

GradientBoostingClassifier (sklearn) — ~5 ms inference per scan.
Outputs probability p_rotation in [0, 1].
Decision threshold: 0.45 (configurable; lower = more sensitive, more FP).
"""

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
]

DECISION_THRESHOLD = 0.45


# ── Data loading ──────────────────────────────────────────────────────────────

def load_labeled_records(path: Path) -> tuple[list, list]:
    """Return (X_rows, y_labels) for all labeled records in the JSONL file."""
    X, y = [], []
    skipped = 0
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
            if rec.get("label") is None:
                skipped += 1
                continue
            feats = rec.get("features") or {}
            row = [float(feats.get(name, 0.0)) for name in FEATURE_NAMES]
            X.append(row)
            y.append(1 if rec["label"] else 0)

    print(f"Loaded {len(X)} labeled records ({skipped} skipped / unlabeled)")
    pos = sum(y)
    print(f"  Positives (real meso): {pos}  Negatives: {len(y)-pos}")
    return X, y


# ── Training ──────────────────────────────────────────────────────────────────

def train(X, y):
    """Train a class-balanced, probability-calibrated rotation classifier.

    Pipeline (each addresses a problem we hit in earlier training rounds):

    1. **Inverse-class-frequency sample weights.**  Our training data is
       heavily skewed positive (~79% in the strict-TOR run) because cells
       collect mostly during severe weather events.  Weighting each sample
       by 1/class_freq restores the effective 50/50 balance during fitting
       so the model isn't free-riding on the majority class.

    2. **Isotonic probability calibration on a held-out 20% set.**  Raw
       GradientBoosting scores aren't true probabilities — a model output
       of 0.7 might correspond to a 50% true positive rate on an imbalanced
       dataset.  The storm tracking service uses fixed probability
       thresholds (0.10 / 0.80) for its score nudges, so the scores
       *have* to be calibrated for those thresholds to mean anything.
       Isotonic regression is non-parametric and the safer choice when
       the score-probability map isn't sigmoid-shaped.

    Saves the wrapped (calibrated) model to MODEL_OUT.
    """
    import numpy as np
    from sklearn.base import clone
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import (
        brier_score_loss, classification_report,
        confusion_matrix, roc_auc_score,
    )
    from sklearn.model_selection import StratifiedKFold, train_test_split
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    import joblib

    Xarr = np.array(X, dtype=float)
    yarr = np.array(y, dtype=int)

    # ── Class-balanced sample weights ───────────────────────────────────
    counts = np.bincount(yarr)
    n_classes = len(counts)
    class_weights = len(yarr) / (n_classes * counts)
    sample_weights = class_weights[yarr]
    print(f"Class weights (inverse frequency):")
    for cls, w in enumerate(class_weights):
        label = "meso" if cls else "no-meso"
        print(f"  {label}: {w:.3f}  (count={counts[cls]})")

    base = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", GradientBoostingClassifier(
            n_estimators=200,
            learning_rate=0.05,
            max_depth=4,
            subsample=0.8,
            min_samples_leaf=5,
            random_state=42,
        )),
    ])

    # ── Hold out 20% for calibration ────────────────────────────────────
    # The calibration step needs data the base model has not seen so the
    # isotonic map doesn't overfit.  Stratified to preserve class balance.
    X_tr, X_cal, y_tr, y_cal, w_tr, _ = train_test_split(
        Xarr, yarr, sample_weights, test_size=0.2,
        stratify=yarr, random_state=42,
    )

    # ── 5-fold CV (with class weights) on the training portion only ─────
    # Manual loop because cross_val_score doesn't pass sample_weight
    # through Pipeline steps when using clf__sample_weight.
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_aucs = []
    for tr_idx, te_idx in cv.split(X_tr, y_tr):
        m = clone(base)
        m.fit(X_tr[tr_idx], y_tr[tr_idx], clf__sample_weight=w_tr[tr_idx])
        p = m.predict_proba(X_tr[te_idx])[:, 1]
        cv_aucs.append(roc_auc_score(y_tr[te_idx], p))
    print(f"\n5-fold CV ROC-AUC (class-balanced): "
          f"{np.mean(cv_aucs):.3f} ± {np.std(cv_aucs):.3f}")

    # ── Fit final base estimator on the full training portion ──────────
    base.fit(X_tr, y_tr, clf__sample_weight=w_tr)

    # ── Probability calibration on the held-out set ────────────────────
    p_uncal = base.predict_proba(X_cal)[:, 1]
    auc_uncal = roc_auc_score(y_cal, p_uncal)
    brier_uncal = brier_score_loss(y_cal, p_uncal)

    # sklearn ≥ 1.6 replaced cv='prefit' with FrozenEstimator wrapping.
    # Fall back to the deprecated API for older sklearn.
    try:
        from sklearn.frozen import FrozenEstimator
        calibrated = CalibratedClassifierCV(
            FrozenEstimator(base), method="isotonic",
        )
    except ImportError:
        calibrated = CalibratedClassifierCV(
            base, method="isotonic", cv="prefit",
        )
    calibrated.fit(X_cal, y_cal)

    p_cal = calibrated.predict_proba(X_cal)[:, 1]
    auc_cal = roc_auc_score(y_cal, p_cal)
    brier_cal = brier_score_loss(y_cal, p_cal)

    print(f"\nCalibration evaluation (held-out 20%, lower Brier = better):")
    print(f"  Uncalibrated:  AUC={auc_uncal:.3f}  Brier={brier_uncal:.4f}")
    print(f"  Calibrated:    AUC={auc_cal:.3f}  Brier={brier_cal:.4f}")
    if brier_cal < brier_uncal:
        print(f"  → Calibration improved Brier by {brier_uncal - brier_cal:.4f}")
    else:
        print("  → Calibration did not help; consider 'sigmoid' or skip")

    # ── In-sample classification report (using calibrated probs) ────────
    y_pred_prob = calibrated.predict_proba(Xarr)[:, 1]
    y_pred = (y_pred_prob >= DECISION_THRESHOLD).astype(int)
    print("\nIn-sample (training set) classification report:")
    print(classification_report(yarr, y_pred, target_names=["no-meso", "meso"]))
    print("Confusion matrix (rows=actual, cols=predicted):")
    print(confusion_matrix(yarr, y_pred))
    print(f"ROC-AUC: {roc_auc_score(yarr, y_pred_prob):.3f}")

    # ── Feature importances from the underlying GBM ─────────────────────
    clf = base.named_steps["clf"]
    importances = sorted(
        zip(FEATURE_NAMES, clf.feature_importances_),
        key=lambda t: t[1], reverse=True,
    )
    print("\nFeature importances (top 10):")
    for name, imp in importances[:10]:
        bar = "#" * int(imp * 200)
        print(f"  {name:<28}  {imp:.4f}  {bar}")

    # Save the CALIBRATED model — this is what production loads
    joblib.dump(calibrated, MODEL_OUT)
    print(f"\nModel saved to {MODEL_OUT}")
    return calibrated


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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Train the rotation classifier")
    parser.add_argument("--data", default=str(TRAINING_DATA),
                        help="Path to training_data.jsonl")
    parser.add_argument("--stats", action="store_true",
                        help="Print labeling stats and exit")
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

    X, y = load_labeled_records(data_path)
    if len(X) < 20:
        print(f"Only {len(X)} labeled records — need at least 20 to train.")
        print("Collect more data with live_qa.py --log and label it.")
        sys.exit(0)

    try:
        import sklearn
    except ImportError:
        print("scikit-learn is not installed. Run:  pip install scikit-learn joblib")
        sys.exit(1)

    train(X, y)


if __name__ == "__main__":
    main()
