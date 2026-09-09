"""Hazard labels from storm reports — hail size and wind speed thresholds.

These are the labels that let the system answer "how big" and "how strong",
which warning polygons cannot: a severe thunderstorm warning does not record
which hazard it was issued for.
"""
import json
from datetime import datetime, timezone

import pytest

from scripts.label_from_lsr_hazards import (
    HAIL_SEVERE_IN, HAIL_SIG_IN, WIND_SEVERE_KT, WIND_SIG_KT,
    hazards_for, haversine_km, label_file,
)


def hail(v):
    return {"kind": "hail", "value": v, "lat": 39.5, "lon": -84.0, "epoch": 0.0}


def wind(v):
    return {"kind": "wind", "value": v, "lat": 39.5, "lon": -84.0, "epoch": 0.0}


# ── Thresholds ─────────────────────────────────────────────────────────────

def test_nws_severe_criteria_are_the_thresholds():
    """1.00 inch hail and 50 kt wind are the NWS severe criteria; 2 inch and
    65 kt are the significant-severe ones. These are definitions, not tuning."""
    assert (HAIL_SEVERE_IN, HAIL_SIG_IN) == (1.00, 2.00)
    assert (WIND_SEVERE_KT, WIND_SIG_KT) == (50.0, 65.0)


def test_quarter_size_hail_is_severe_and_not_significant():
    h = hazards_for([hail(1.00)])
    assert h["hail_1in"] == 1 and h["hail_2in"] == 0


def test_sub_severe_hail_is_negative_for_both():
    h = hazards_for([hail(0.88)])
    assert h["hail_1in"] == 0 and h["hail_2in"] == 0


def test_significant_hail_sets_both_flags():
    """A 2.5 inch report is also a >=1 inch report; the thresholds nest."""
    h = hazards_for([hail(2.5)])
    assert h["hail_1in"] == 1 and h["hail_2in"] == 1


def test_wind_thresholds_nest_the_same_way():
    assert hazards_for([wind(70)]) == {
        "hail_1in": 0, "hail_2in": 0, "wind_severe": 1, "wind_sig": 1}
    assert hazards_for([wind(55)])["wind_sig"] == 0


def test_the_largest_report_decides_not_the_first():
    h = hazards_for([hail(0.75), hail(2.25), hail(1.0)])
    assert h["hail_2in"] == 1


def test_hail_and_wind_are_independent():
    h = hazards_for([hail(2.5), wind(30)])
    assert h["hail_2in"] == 1 and h["wind_severe"] == 0


def test_no_reports_is_all_zero_not_missing():
    """Every hazard model needs negatives, and a cell on a day we collected
    reports for with none nearby is the best negative available."""
    assert hazards_for([]) == {
        "hail_1in": 0, "hail_2in": 0, "wind_severe": 0, "wind_sig": 0}


# ── Matching ───────────────────────────────────────────────────────────────

def _row(lat, lon, ts):
    return json.dumps({"lat": lat, "lon": lon, "ts": ts,
                       "features": {}, "label": False})


def test_a_nearby_report_labels_the_cell(tmp_path):
    p = tmp_path / "t.jsonl"
    t = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
    p.write_text(_row(39.5, -84.0, t.isoformat()) + "\n", encoding="utf-8")
    res = label_file(p, [{"kind": "hail", "value": 1.75, "lat": 39.55,
                          "lon": -84.02, "epoch": t.timestamp()}], dry_run=False)
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert res["matched"] == 1
    assert rec["hazard_labels"]["hail_1in"] == 1
    assert rec["hazard_labels"]["hail_2in"] == 0


def test_a_distant_report_does_not(tmp_path):
    p = tmp_path / "t.jsonl"
    t = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
    p.write_text(_row(39.5, -84.0, t.isoformat()) + "\n", encoding="utf-8")
    label_file(p, [{"kind": "hail", "value": 2.5, "lat": 42.0, "lon": -88.0,
                    "epoch": t.timestamp()}], dry_run=False)
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["hazard_labels"]["hail_1in"] == 0


def test_a_report_from_an_hour_later_does_not(tmp_path):
    """A point event has a known time. Crediting a storm for hail that fell
    before it arrived is how a matcher invents skill."""
    p = tmp_path / "t.jsonl"
    t = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
    p.write_text(_row(39.5, -84.0, t.isoformat()) + "\n", encoding="utf-8")
    label_file(p, [{"kind": "hail", "value": 2.5, "lat": 39.5, "lon": -84.0,
                    "epoch": t.timestamp() + 3600}], dry_run=False)
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["hazard_labels"]["hail_1in"] == 0


def test_warning_labels_are_never_overwritten(tmp_path):
    """Hazard labels sit ALONGSIDE the warning label; they answer a different
    question and both are kept."""
    p = tmp_path / "t.jsonl"
    t = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
    p.write_text(json.dumps({"lat": 39.5, "lon": -84.0, "ts": t.isoformat(),
                             "label": True, "label_source": "TO.W"}) + "\n",
                 encoding="utf-8")
    label_file(p, [{"kind": "wind", "value": 70, "lat": 39.5, "lon": -84.0,
                    "epoch": t.timestamp()}], dry_run=False)
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["label"] is True and rec["label_source"] == "TO.W"
    assert rec["hazard_labels"]["wind_sig"] == 1


def test_dry_run_changes_nothing(tmp_path):
    p = tmp_path / "t.jsonl"
    t = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
    original = _row(39.5, -84.0, t.isoformat()) + "\n"
    p.write_text(original, encoding="utf-8")
    label_file(p, [{"kind": "hail", "value": 2.5, "lat": 39.5, "lon": -84.0,
                    "epoch": t.timestamp()}], dry_run=True)
    assert p.read_text(encoding="utf-8") == original


def test_haversine_is_sane():
    assert haversine_km(39.5, -84.0, 39.5, -84.0) == pytest.approx(0.0)
    assert haversine_km(39.0, -84.0, 40.0, -84.0) == pytest.approx(111.2, abs=1.0)
