"""Lead-time scoring — the metric the system is actually judged on.

Every test here encodes a way the headline numbers can look fine while the
system fails at its stated job.
"""
import pytest

from backend.services.lead_time import lead_time_report, format_lead_report


def scan(wid, lead, p):
    return {"warning_id": wid, "lead_min": lead, "p": p}


def test_earliest_crossing_wins_not_the_strongest():
    """A detector that fires 40 min out and holds beats one that only agrees at
    2 min. Scoring on peak probability would call these identical."""
    rows = [scan("w1", 40, 0.6), scan("w1", 20, 0.9), scan("w1", 2, 0.99)]
    rep = lead_time_report(rows, threshold=0.5)
    assert rep["median_lead_min"] == 40
    assert rep["detected_before_issuance"] == 1


def test_below_threshold_before_issuance_counts_as_late_not_detected():
    rows = [scan("w1", 30, 0.2), scan("w1", 10, 0.3), scan("w1", -5, 0.95)]
    rep = lead_time_report(rows, threshold=0.5)
    assert rep["detected_before_issuance"] == 0
    assert rep["late_only"] == 1
    assert rep["missed"] == 0


def test_never_flagged_is_a_miss():
    rows = [scan("w1", 30, 0.1), scan("w1", -5, 0.1)]
    rep = lead_time_report(rows, threshold=0.5)
    assert rep["missed"] == 1
    assert rep["detected_before_issuance"] == 0


def test_storms_we_never_saw_early_are_excluded_not_counted_as_misses():
    """A warning whose storm has no pre-warning scan measures the archive's
    coverage, not the model. Counting it as a miss would make lead time look
    worse every time collection was patchy."""
    rows = [scan("w1", -10, 0.9), scan("w1", -2, 0.95)]
    rep = lead_time_report(rows, threshold=0.5)
    assert rep["warnings_without_pre_warning_scans"] == 1
    assert rep["warnings_scored"] == 0
    assert rep["missed"] == 0


def test_each_warning_contributes_exactly_one_score():
    """Storms are tracked across many scans; without grouping, one long-lived
    supercell would dominate the median."""
    rows = [scan("w1", m, 0.9) for m in (50, 40, 30, 20, 10)]
    rows += [scan("w2", 10, 0.9)]
    rep = lead_time_report(rows, threshold=0.5)
    assert rep["warnings_scored"] == 2
    assert sorted([rep["p25_lead_min"], rep["p75_lead_min"]]) == [20.0, 40.0]
    assert rep["median_lead_min"] == 30.0


def test_median_and_quartiles_over_several_storms():
    rows = []
    for i, lead in enumerate([10, 20, 30, 40]):
        rows.append(scan(f"w{i}", lead, 0.9))
    rep = lead_time_report(rows, threshold=0.5)
    assert rep["median_lead_min"] == 25.0
    assert rep["max_lead_min"] == 40.0
    assert rep["detected_fraction"] == 1.0


def test_threshold_actually_moves_the_answer():
    rows = [scan("w1", 40, 0.55), scan("w1", 10, 0.85)]
    assert lead_time_report(rows, 0.5)["median_lead_min"] == 40
    assert lead_time_report(rows, 0.8)["median_lead_min"] == 10


def test_rows_without_a_warning_id_are_ignored():
    rows = [{"lead_min": 30, "p": 0.9}, scan("w1", 20, 0.9)]
    assert lead_time_report(rows, 0.5)["warnings_scored"] == 1


def test_empty_input_is_reported_not_crashed():
    rep = lead_time_report([], threshold=0.5)
    assert rep["warnings_scored"] == 0
    assert rep["median_lead_min"] is None
    assert "nothing to measure" in format_lead_report(rep)


def test_format_is_readable():
    rows = [scan("w1", 35, 0.9), scan("w2", 15, 0.9), scan("w3", 5, 0.1)]
    text = format_lead_report(lead_time_report(rows, 0.5))
    assert "median lead" in text
    assert "flagged BEFORE the warning" in text
