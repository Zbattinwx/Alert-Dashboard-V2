"""
The Precip (QPF) fields must resolve against the REAL .idx of every model that
advertises them.

This is a live-network test on purpose. The failure it guards is not a crash:
every accumulation is published twice — a run-total (`0-6 hour acc fcst`) and a
bucket (`5-6 hour acc fcst`) — and a matcher that hits the wrong one returns a
perfectly valid grid with the wrong meaning. A 1-hour bucket rendered under a
"Total Precip (run)" legend looks completely normal and is completely wrong.

The specific trap: the two records' ORDER in the .idx is not consistent between
models. HRRR lists the run-total first; RRFS lists the bucket first. A plain
`:APCP:surface:` substring therefore serves different quantities on different
models, which is why the run-totals pin their window with `idx_fh`.

Skips (rather than fails) when a model's archive is unreachable — a NOAA bucket
being down is not a bug in this repo.

    python -m pytest tests/services/test_precip_fields_live.py -v
"""

import datetime as dt
import urllib.request

import pytest

from backend.services.hrrr_field_service import (
    GFS_FIELDS, HRRR_FIELDS, MODELS, NAM_FIELDS, RRFS_FIELDS, _idx_for,
)

# Yesterday's 12Z is always fully published.
DAY = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y%m%d")
FHOURS = (1, 6, 12)


def _idx_lines(model: str, fhour: int) -> list[str]:
    m = MODELS[model]
    tok = m.get("default_file")
    key = m["key"](DAY, 12, fhour, tok)
    url = f"https://{m['bucket']}.s3.amazonaws.com/{key}.idx"
    try:
        return urllib.request.urlopen(url, timeout=45).read().decode().splitlines()
    except Exception as e:  # noqa: BLE001 - upstream outage, not our bug
        pytest.skip(f"{model} f{fhour:02d} idx unreachable: {type(e).__name__}")


def _match(lines: list[str], matcher) -> str | None:
    parts = matcher if isinstance(matcher, tuple) else (matcher,)
    for ln in lines:
        if all(p in ln for p in parts):
            return ln
    return None


def _var(line: str) -> str:
    """`d=...:APCP:surface:0-6 hour acc fcst:` -> `APCP:surface:0-6 hour acc fcst`."""
    return ":".join(line.split(":")[3:6])


TABLES = {"hrrr": HRRR_FIELDS, "rrfs": RRFS_FIELDS, "gfs": GFS_FIELDS, "nam": NAM_FIELDS}
PRECIP_KEYS = ("qpf", "qpf_1h", "qpf_3h", "weasd", "frozr")


@pytest.mark.parametrize("model", sorted(TABLES))
@pytest.mark.parametrize("fhour", FHOURS)
def test_every_precip_field_resolves(model, fhour):
    """A registered field whose idx never matches is a dead entry in the UI."""
    table = TABLES[model]
    keys = [k for k in PRECIP_KEYS if k in table]
    if not keys:
        pytest.skip(f"{model} registers no precip fields")
    lines = _idx_lines(model, fhour)
    missing = []
    for k in keys:
        if _match(lines, _idx_for(table[k], fhour)) is None:
            missing.append((k, _idx_for(table[k], fhour)))
    assert not missing, f"{model} f{fhour:02d}: no idx match for {missing}"


@pytest.mark.parametrize("model", sorted(TABLES))
@pytest.mark.parametrize("fhour", FHOURS)
def test_run_totals_really_are_run_totals(model, fhour):
    """The bug this file exists for: a run-total matcher landing on a bucket.

    RRFS lists the bucket BEFORE the run-total, so an order-dependent substring
    silently serves 1 hour of rain as the storm-total.
    """
    table = TABLES[model]
    lines = _idx_lines(model, fhour)
    for k in ("qpf", "weasd", "frozr"):
        if k not in table:
            continue
        hit = _match(lines, _idx_for(table[k], fhour))
        if hit is None:
            pytest.skip(f"{model} has no {k} at f{fhour:02d}")
        var = _var(hit)
        assert f"0-{fhour} hour acc" in var, (
            f"{model} f{fhour:02d} {k} ({table[k]['label']}) resolved to {var!r} "
            "— that is a bucket, not a run total"
        )


@pytest.mark.parametrize("fhour", (6, 12))
def test_buckets_track_the_forecast_hour(fhour):
    """`qpf_1h` must move with the hour: 5-6 at f06, 11-12 at f12."""
    lines = _idx_lines("hrrr", fhour)
    hit = _match(lines, _idx_for(HRRR_FIELDS["qpf_1h"], fhour))
    assert hit is not None
    assert f"{fhour - 1}-{fhour} hour acc" in _var(hit), _var(hit)


def test_nam_gets_a_three_hour_bucket_not_a_run_total():
    """NAM publishes only a 3-hour bucket, so it must not advertise a run total."""
    assert "qpf" not in NAM_FIELDS, "NAM has no run-total APCP"
    assert "qpf_3h" in NAM_FIELDS
    lines = _idx_lines("nam", 6)
    hit = _match(lines, _idx_for(NAM_FIELDS["qpf_3h"], 6))
    assert hit is not None and "3-6 hour acc" in _var(hit), hit


def test_rrfs_does_not_advertise_weasd():
    """RRFS carries APCP and FROZR but no surface WEASD (verified live)."""
    assert "weasd" not in RRFS_FIELDS


def test_every_precip_field_is_renderable_by_the_app():
    """Each precip field needs the `qpf` LUT and the Precip group, or the app
    falls back to a default ramp and the legend reads wrong."""
    for model, table in TABLES.items():
        for k in PRECIP_KEYS:
            if k not in table:
                continue
            spec = table[k]
            assert spec["lut"] == "qpf", f"{model}.{k} lut={spec['lut']}"
            assert spec["group"] == "Precip", f"{model}.{k} group={spec['group']}"
            assert spec["units"] == "in", f"{model}.{k} units={spec['units']}"
