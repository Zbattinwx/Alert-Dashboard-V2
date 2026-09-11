"""Every tracked cell must record which radar measured it.

Live collection wrote `site: None` on all 16,097 rows gathered 2026-09-08..10,
because `live_qa` reads `cell.get("site")` and the cell carried no such key. That
is not cosmetic: without a site there is no range-from-radar, and range is the
nuisance variable this project has already been bitten by -- `llsd_max_shear`
turned out to be very nearly a measurement of it rather than of rotation, and
`audit_features` checks every feature against it. It also meant live rows could
not be merged or audited on the same terms as archive rows, which DO carry a
site because the backfill knows which one it is replaying.

The field is DECLARED on the dataclass on purpose. storm_tracking_service's own
comments record why: `to_dict()` is `asdict()`, which walks declared fields only,
so a dynamically-assigned attribute is computed and then silently dropped before
anything sees it. That is exactly how mean_cc / min_cc / mean_zdr came to be 0.0
in 100% of collected rows in every month of the archive.
"""
import dataclasses

import pytest

from backend.services.storm_tracking_service import (
    StormTrackingService, TrackedStormCell)


def cell(lat, lon, cid="CELL-TEST"):
    """A minimal TrackedStormCell.

    Required fields are filled from their declared TYPES rather than listed by
    hand, so adding a field to the dataclass does not break these tests — and
    so this fixture can never quietly drift from the real shape.
    """
    stub = {"str": "x", "float": 0.0, "int": 0, "bool": False,
            "list": [], "dict": {}}
    kw = {}
    for f in dataclasses.fields(TrackedStormCell):
        if (f.default is not dataclasses.MISSING
                or f.default_factory is not dataclasses.MISSING):
            continue                      # has a default; leave it
        t = str(f.type)
        if "Optional" in t:
            kw[f.name] = None
        else:
            kw[f.name] = next((v for k, v in stub.items() if k in t), None)
    kw.update(cell_id=cid, lat=lat, lon=lon)
    return TrackedStormCell(**kw)


@pytest.fixture
def svc():
    s = StormTrackingService.__new__(StormTrackingService)
    s._radar_locations = {}
    return s


# ── The declared-field trap ────────────────────────────────────────────────

def test_site_is_a_declared_field_so_asdict_carries_it():
    """The mean_cc lesson, asserted. A dynamically-assigned `site` would be set
    and then dropped by to_dict() without a single error."""
    names = {f.name for f in dataclasses.fields(TrackedStormCell)}
    assert "site" in names, (
        "site must be DECLARED — asdict() walks declared fields only, so "
        "assigning it dynamically would never reach the training row")
    c = cell(39.5, -84.0)
    c.site = "KILN"
    assert dataclasses.asdict(c)["site"] == "KILN"


def test_site_defaults_to_none_not_a_string():
    """Absent must stay distinguishable from a site. A placeholder like '?' or
    '' would be indistinguishable from a real one downstream."""
    assert cell(39.5, -84.0).site is None


# ── Which radar owns a cell ────────────────────────────────────────────────

def test_the_nearest_registered_radar_wins(svc):
    """Voronoi, the same rule the detectors use: the owning radar is the one
    with the lowest beam over the cell, i.e. the one its measurements came
    from — not simply whichever volume is being processed."""
    svc._radar_locations = {
        "KILN": (39.4203, -83.8217),   # Wilmington OH
        "KIND": (39.7075, -86.2803),   # Indianapolis
    }
    near_iln = cell(39.5, -83.9)
    near_ind = cell(39.7, -86.2)
    assert svc._owning_site(near_iln) == "KILN"
    assert svc._owning_site(near_ind) == "KIND"


def test_no_registered_radar_yields_none(svc):
    """A real state — the first volume of a session, before any radar's
    coordinates have been read — and it must not be guessed at."""
    assert svc._owning_site(cell(39.5, -84.0)) is None


def test_stamping_sets_every_cell(svc):
    svc._radar_locations = {"KILN": (39.4203, -83.8217)}
    cells = [cell(39.5, -83.9, "A"), cell(40.1, -84.4, "B")]
    svc._stamp_sites(cells)
    assert [c.site for c in cells] == ["KILN", "KILN"]


def test_stamping_with_no_radars_leaves_them_alone(svc):
    cells = [cell(39.5, -83.9)]
    svc._stamp_sites(cells)
    assert cells[0].site is None


def test_a_cell_can_change_hands_between_scans(svc):
    """Storms move. The owning radar is a per-scan answer, not a birth
    certificate — a cell that drifts from one radar's Voronoi cell into
    another's is thereafter measured by the second."""
    svc._radar_locations = {"KILN": (39.4203, -83.8217), "KIND": (39.7075, -86.2803)}
    c = cell(39.5, -83.9)
    svc._stamp_sites([c])
    assert c.site == "KILN"
    c.lat, c.lon = 39.7, -86.2          # drifted west
    svc._stamp_sites([c])
    assert c.site == "KIND"


def test_a_bad_coordinate_does_not_take_the_scan_down(svc):
    """It runs once per scan over every cell; one bad cell must cost one cell."""
    svc._radar_locations = {"KILN": (39.4203, -83.8217)}
    bad = cell(float("nan"), float("nan"))
    svc._stamp_sites([bad])              # must not raise
    good = cell(39.5, -83.9)
    svc._stamp_sites([good])
    assert good.site == "KILN"
