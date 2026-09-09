"""The mesoanalysis initiation gate, and the F01 analysis background.

The gate is the answer to a real on-air failure: the dashboard rated a HIGH
storm threat over Minnesota on an afternoon with no storm chance there at all.
That was not a threshold being too low.  STP/SCP/SHIP are conditional
discriminators fitted to storm-only samples, so asked about a storm-free grid
box they answer a question that was never posed to them.  These tests pin the
gate that stops it.
"""
import numpy as np
import pytest

from backend.services import mesoanalysis_service as ms
from backend.services.mesoanalysis_service import MesoanalysisService

SHAPE = (40, 80)


def grid(v):
    return np.full(SHAPE, float(v), dtype=np.float32)


@pytest.fixture
def svc():
    return MesoanalysisService()


def _warm_sheared_env(refc, mlcin=-20.0):
    """A textbook loaded warm sector: plenty of CAPE, weak cap."""
    return {"mucape": grid(2500), "mlcape": grid(2000),
            "mlcin": grid(mlcin), "refc": grid(refc)}


# ── The Minnesota case ─────────────────────────────────────────────────────

def test_ingredients_without_storms_are_not_an_active_threat(svc):
    gate = svc._initiation(_warm_sheared_env(refc=5.0))
    assert gate["storms"].sum() == 0, "no convection, so nothing is an active threat"
    assert gate["capable"].sum() > 0, "the environment could still support storms"
    assert gate["conditional"].sum() > 0, "so it is reportable as conditional"


def test_the_same_environment_with_storms_is_active(svc):
    gate = svc._initiation(_warm_sheared_env(refc=52.0))
    assert gate["storms"].sum() > 0


def test_a_hard_cap_seals_it_even_with_storms_in_the_field(svc):
    """MLCIN below -200 J/kg is STP's own zero point."""
    gate = svc._initiation(_warm_sheared_env(refc=52.0, mlcin=-350.0))
    assert gate["capable"].sum() == 0
    assert gate["storms"].sum() == 0


def test_no_buoyancy_no_threat(svc):
    p = {"mucape": grid(20), "mlcape": grid(10), "mlcin": grid(-5), "refc": grid(55)}
    gate = svc._initiation(p)
    assert gate["capable"].sum() == 0


def test_reflectivity_needs_buoyancy_to_count_as_convection(svc):
    """SPC's HREF screens simulated reflectivity on MUCAPE > 50 J/kg — bright
    band and other non-convective returns are not storms."""
    p = {"mucape": grid(30), "mlcape": grid(30), "mlcin": grid(-5), "refc": grid(55)}
    assert svc._initiation(p)["storms"].sum() == 0


def test_a_moderate_cap_blocks_the_conditional_tier_but_not_capability(svc):
    """Between -200 and -50 J/kg the atmosphere is capable but not primed;
    a conditional threat needs the weaker inhibition."""
    gate = svc._initiation(_warm_sheared_env(refc=5.0, mlcin=-120.0))
    assert gate["capable"].sum() > 0
    assert gate["conditional"].sum() == 0


def test_storm_neighbourhood_extends_the_active_area(svc):
    """A cell of convection makes its ~40 km surroundings active, the way SPC
    computes neighbourhood reflectivity probabilities."""
    p = _warm_sheared_env(refc=5.0)
    p["refc"] = grid(5.0)
    p["refc"][20, 40] = 55.0
    n = int(svc._initiation(p)["storms"].sum())
    assert n > 1, "a lone storm should activate a neighbourhood, not one cell"
    expected = (ms.REFC_NEIGHBORHOOD_CELLS * 2 + 1) ** 2
    assert n == expected, f"expected a {expected}-cell neighbourhood, got {n}"


def test_missing_reflectivity_leaves_everything_conditional(svc):
    """If the field did not download we do not know whether storms exist, and
    guessing that they do would reintroduce the bug."""
    p = {"mucape": grid(2500), "mlcape": grid(2000), "mlcin": grid(-20)}
    gate = svc._initiation(p)
    assert gate["storms"].sum() == 0
    assert gate["conditional"].sum() > 0


# ── Wording ────────────────────────────────────────────────────────────────

def test_no_level_uses_an_spc_outlook_category_name(svc):
    """MRGL/SLGT/ENH/MDT/HIGH are SPC's names, defined by probabilities of
    severe weather within 25 miles of a point.  A viewer reading 'MODERATE'
    on our graphic will believe SPC issued a Moderate Risk."""
    forbidden = {"marginal", "slight", "enhanced", "moderate", "high"}
    for level in ms.LEVEL_LABELS:
        for cond in (True, False):
            words = set(svc._level_label(level, cond).lower().replace(",", " ").split())
            assert not (words & forbidden), (
                f"{level}/{cond} -> {svc._level_label(level, cond)!r} "
                "reuses an SPC outlook category name")


def test_conditional_wording_says_so(svc):
    assert "if storms form" in svc._level_label("high", True)
    assert "if storms form" not in svc._level_label("high", False)


def test_details_state_the_basis(svc):
    active = svc._threat_details("tornado", {"level": "high", "basis": "storms"})
    cond = svc._threat_details("tornado", {"level": "high", "basis": "conditional"})
    assert active.startswith("Storms present")
    assert "conditional on storms forming" in cond


# ── The F01 analysis background ────────────────────────────────────────────

def test_run_token_round_trips():
    assert MesoanalysisService._parse_run("rap:2026090822:1") == ("rap", "2026090822", 1)


def test_a_bare_cycle_still_resolves():
    """Existing ?run= links and anything stored before the change."""
    model, cycle, fh = MesoanalysisService._parse_run("2026090823")
    assert (model, cycle, fh) == (ms.MODEL, "2026090823", ms.FHOUR)


def test_valid_time_is_the_cycle_plus_the_forecast_hour():
    """A 22Z F01 is valid at 23Z.  Labelling it 22Z would misreport it by an
    hour, and the whole point of the change is an honest valid time."""
    assert MesoanalysisService._run_iso("rap:2026090822:1").startswith("2026-09-08T23:00")
    assert MesoanalysisService._run_iso("rap:2026090823:0").startswith("2026-09-08T23:00")


def test_f01_is_preferred_and_beats_f00_on_age():
    """The F01 of a cycle is valid an hour later than its F00, so preferring it
    is what turns a ~79 minute mean age into ~30."""
    assert ms.MESO_SOURCES[0] == ("rap", 1)
    f01 = MesoanalysisService._run_iso("rap:2026090822:1")
    f00 = MesoanalysisService._run_iso("rap:2026090822:0")
    assert f01 > f00
