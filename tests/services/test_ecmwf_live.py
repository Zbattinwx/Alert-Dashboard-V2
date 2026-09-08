"""
ECMWF IFS open data — the model that shares none of the NCEP plumbing.

Everything about ECMWF's archive differs from the seven NCEP models here, and
each difference is a silent failure rather than a loud one:

  * the index is `.index`, and it REPLACES `.grib2` rather than appending to it
    (`...fc.grib2.index` is a NoSuchKey — caught only by actually fetching);
  * the index is JSON lines with explicit `_offset`/`_length`, so the "byte range
    ends where the next record starts" rule used for NCEP is wrong here;
  * parameters are ECMWF short names (`2t`, `msl`, `tp`), sharing no vocabulary
    with the `:TMP:2 m above ground:` matchers;
  * accumulations are run-totals in METRES, not mm — a unit slip renders a
    plausible-looking field that is off by 25.4x.

So this is a live test: it decodes real grids and asserts the values are
physically possible. Skips when ECMWF's bucket is unreachable.

    python -m pytest tests/services/test_ecmwf_live.py -v
"""

import datetime as dt

import numpy as np
import pytest

from backend.services.hrrr_field_service import (
    ECMWF_FIELDS, MODELS, _idx_key, get_hrrr_field_service,
)

DAY = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y%m%d")
RUN = f"{DAY}12"
IDX = 2  # file index 2 x fhour_step 3 = f006


@pytest.fixture(scope="module")
def svc():
    return get_hrrr_field_service()


def _grid(svc, key, idx=IDX):
    try:
        return svc._field_grid("ecmwf", RUN, key, idx)
    except Exception as e:  # noqa: BLE001 - upstream outage is not our bug
        pytest.skip(f"ECMWF unreachable for {key}: {type(e).__name__}: {e}")


def test_index_key_replaces_the_grib_extension():
    """`...fc.grib2.index` does not exist; `...fc.index` does."""
    key = MODELS["ecmwf"]["key"](DAY, 12, IDX, "")
    assert key.endswith(".grib2")
    assert _idx_key("ecmwf", key).endswith("-oper-fc.index")
    assert ".grib2.index" not in _idx_key("ecmwf", key)


def test_ncep_models_still_append_their_idx():
    """The per-model transform must not change how every other model works."""
    for m in ("hrrr", "gfs", "nam", "rrfs"):
        assert _idx_key(m, "some/key.grib2") == "some/key.grib2.idx"


@pytest.mark.parametrize("key,lo,hi", [
    # Physically possible global ranges for a 6-hour forecast.
    ("t2m",    -90.0, 140.0),   # °F
    ("td2m",  -100.0, 100.0),   # °F
    ("mslp",   870.0, 1085.0),  # hPa — below the record low / above record high is a unit slip
    ("z500",  4600.0, 6200.0),  # m
    ("tcc",      0.0, 100.0),   # %
    ("mucape",   0.0, 10000.0), # J/kg
])
def test_field_values_are_physically_possible(svc, key, lo, hi):
    g = _grid(svc, key)
    f = np.isfinite(g)
    assert f.any(), f"{key} decoded to all-NaN"
    lo_v, hi_v = float(np.nanmin(g[f])), float(np.nanmax(g[f]))
    assert lo <= lo_v and hi_v <= hi, f"{key} range {lo_v:.1f}..{hi_v:.1f} outside {lo}..{hi}"


@pytest.mark.parametrize("model,idx", [("ecmwf", 2), ("aifs", 1)])
@pytest.mark.parametrize("key", ["qpf", "sf"])
def test_accumulation_units_are_right_per_model(svc, model, idx, key):
    """IFS `tp`/`sf` are METRES; AIFS's are kg m**-2 (mm).

    Same parameter names, same bucket, units 25.4x apart — read off the GRIB
    `units` key, not assumed. Inheriting IFS's converter gave AIFS a 6-hour
    global max of 1954 inches. That one is obvious; a 25.4x error on a modest
    field would render perfectly plausibly, which is why this asserts a
    physical range rather than eyeballing a picture.
    """
    try:
        g = svc._field_grid(model, RUN, key, idx)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"{model} unreachable: {type(e).__name__}")
    f = np.isfinite(g)
    assert f.any(), f"{model}.{key} decoded to all-NaN"
    mx = float(np.nanmax(g[f]))
    assert 0.0 <= mx < 30.0, f"{model}.{key} 6-hour global max {mx:.1f} in is not credible"


def test_ifs_and_aifs_agree_on_the_same_quantity(svc):
    """Two models of the same atmosphere, six hours out: their global max QPF
    should be the same order of magnitude. A unit slip in either breaks this
    even when each looks individually plausible."""
    try:
        a = svc._field_grid("ecmwf", RUN, "qpf", 2)
        b = svc._field_grid("aifs", RUN, "qpf", 1)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"unreachable: {type(e).__name__}")
    ma = float(np.nanmax(a[np.isfinite(a)]))
    mb = float(np.nanmax(b[np.isfinite(b)]))
    assert 0.1 < ma / max(mb, 1e-6) < 10.0, (
        f"IFS max {ma:.2f} in vs AIFS max {mb:.2f} in — a 25.4x gap here means "
        "one of them has the wrong accumulation unit")


def test_a_derived_wind_magnitude_decodes(svc):
    """u/v come from two SEPARATE ECMWF records, unlike NAM's combined message."""
    g = _grid(svc, "wspd500")
    f = np.isfinite(g)
    mx = float(np.nanmax(g[f]))
    assert 10.0 < mx < 250.0, f"500 mb max wind {mx:.1f} kt"
    assert float(np.nanmin(g[f])) >= 0.0, "a magnitude cannot be negative"


def test_the_grid_matches_the_shared_target(svc):
    """Every model regrids onto the same target raster, or the app's binary wire
    format and the city-value sampling both break."""
    a = _grid(svc, "t2m")
    b = _grid(svc, "z500")
    assert a.shape == b.shape
    from backend.services.hrrr_field_service import T_NI, T_NJ
    assert a.shape == (T_NJ, T_NI)


@pytest.mark.parametrize("model,idx", [("ecmwf", 2), ("aifs", 1)])
def test_aifs_only_advertises_fields_it_has(svc, model, idx):
    """AIFS is an IFS SUBSET: no 10fg, mucape, sd, tcwv, r or vo. Advertising
    them would put six dead entries in the model picker."""
    from backend.services.hrrr_field_service import AIFS_FIELDS
    for gone in ("gust", "mucape", "pwat", "snod", "rh850", "rh700", "vort500"):
        assert gone not in AIFS_FIELDS, gone


def test_every_registered_field_has_a_matcher_in_the_index(svc):
    """A field whose param never appears is a dead entry in the model picker."""
    key = MODELS["ecmwf"]["key"](DAY, 12, IDX, "")
    try:
        lines = svc._read_idx("ecmwf", key)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"ECMWF index unreachable: {type(e).__name__}")
    import json
    entries = [json.loads(l) for l in lines if l.strip()]

    def present(matcher):
        return any(all(str(e.get(k)) == str(v) for k, v in matcher.items()) for e in entries)

    missing = []
    for name, spec in ECMWF_FIELDS.items():
        if "idx" in spec:
            if not present(spec["idx"]):
                missing.append((name, spec["idx"]))
        elif spec.get("derive", (None,))[0] == "mag":
            for m in spec["derive"][1:]:
                if not present(m):
                    missing.append((name, m))
    assert not missing, f"no index entry for {missing}"
