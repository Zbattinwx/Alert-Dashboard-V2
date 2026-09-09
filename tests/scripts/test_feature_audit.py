"""The audits, tested against planted versions of the bugs that motivated them.

A checker nobody has tried to fool is a checker nobody should trust. Each test
here plants one defect this project actually shipped and asserts the audit finds
it -- and, just as importantly, that clean data passes, because an audit that
cries wolf is one people learn to run with --skip-audit.
"""
import json
import math

import pytest

from scripts.audit_features import Audit
from scripts.audit_sources import _present, _requirements, sample_dates

SITE = "KILN"
SITE_LAT, SITE_LON = 39.4203, -83.8217

FEATURES = ["max_dbz", "vil_kg_m2", "llsd_max_shear", "env_efhl", "flash_rate_fpm"]


def row(ts, *, km=40.0, **feats):
    """One training row, `km` from KILN, with the given feature values."""
    dlat = km / 111.0
    base = {"max_dbz": 55.0, "vil_kg_m2": 30.0}
    base.update(feats)
    return {"ts": ts, "site": SITE, "lat": SITE_LAT + dlat, "lon": SITE_LON,
            "label": False, "features": base}


def run(rows, declared=None, optional=()):
    a = Audit(declared if declared is not None else FEATURES, set(optional))
    for r in rows:
        a.add(r)
    return a.report()


def findings(rep, feature=None, check=None, severity=None):
    out = rep["findings"]
    if feature:
        out = [f for f in out if f["feature"] == feature]
    if check:
        out = [f for f in out if f["check"] == check]
    if severity:
        out = [f for f in out if f["severity"] == severity]
    return out


def months(n, per=120, **kw):
    """`n` months of rows, `per` per month, starting 2024-01."""
    out = []
    for i in range(n):
        m = f"2024-{i + 1:02d}" if i < 12 else f"2025-{i - 11:02d}"
        for j in range(per):
            out.append(row(f"{m}-15T18:{j % 60:02d}:00+00:00", **kw))
    return out


# ── The bugs ───────────────────────────────────────────────────────────────

def test_a_column_that_is_always_null_is_not_a_feature():
    """downburst/MARC/RIJ were in FEATURE_NAMES, in the tracker, and in nothing
    in between. The wind models were asked to predict damaging wind with the
    wind fields withheld."""
    rep = run(months(4, flash_rate_fpm=None))
    assert findings(rep, "flash_rate_fpm", "empty", "fail") or \
        findings(rep, "flash_rate_fpm", "absent", "fail")


def test_a_declared_feature_absent_from_every_row_is_fatal():
    rep = run(months(4), declared=FEATURES + ["marc_convergence_ms"])
    f = findings(rep, "marc_convergence_ms", "absent")
    assert f and f[0]["severity"] == "fail"


def test_optional_features_downgrade_to_a_warning():
    """Some columns are legitimately unavailable in the archive (no historical
    GLM feed). They should be reported, not block the run."""
    rep = run(months(4), declared=FEATURES + ["flash_rate_trend"],
              optional=["flash_rate_trend"])
    f = findings(rep, "flash_rate_trend", "absent")
    assert f and f[0]["severity"] == "warn"


def test_a_constant_column_is_fatal():
    rep = run(months(4, llsd_max_shear=0.004))
    assert findings(rep, "llsd_max_shear", "constant", "fail")


def test_presence_that_steps_in_time_is_fatal():
    """env_efhl: absent from the RAP before ~2024-07, present after.

    NaN itself is fine. "is this NaN" being a clean readout of the calendar is
    not -- the model can learn era instead of atmosphere.
    """
    rows = []
    for i in range(6):
        val = None if i < 3 else 150.0
        rows += [row(f"2024-{i + 1:02d}-15T18:00:00+00:00", env_efhl=val)
                 for _ in range(120)]
    rep = run(rows)
    f = findings(rep, "env_efhl", "presence_step")
    assert f and f[0]["severity"] == "fail", [x["message"] for x in rep["findings"]]
    # It names the first month at each extreme — 2024-01 (absent) and whichever
    # month presence first reaches 100% — so the reader knows where the step is.
    assert f[0]["low_month"] == "2024-01", f[0]["message"]
    assert f[0]["high_month"] in {"2024-04", "2024-05", "2024-06"}, f[0]["message"]


def test_the_step_message_reads_in_date_order():
    """It first reported 'swings 0% (2026-04) -> 100% (2019-05)', i.e. time
    running backwards, because it ordered by severity rather than by date."""
    import re
    rows = []
    for i in range(6):
        rows += [row(f"2024-{i + 1:02d}-15T18:00:00+00:00",
                     env_efhl=(None if i < 3 else 150.0)) for _ in range(120)]
    msg = findings(run(rows), "env_efhl", "presence_step")[0]["message"]
    named = re.findall(r"\d{4}-\d{2}", msg)
    assert len(named) == 2, msg
    assert named == sorted(named), f"months named out of chronological order: {msg}"


def test_a_feature_that_decays_with_range_is_fatal():
    """llsd_max_shear: median fell ~50x from <10 km to >=150 km, and removing
    it IMPROVED held-out average precision by 13%.

    Its Pearson r against range is modest -- the decay is steep, monotonic and
    buried in variance -- so the correlation check alone misses it. This is why
    the near/far MEDIAN comparison exists.
    """
    rows = []
    for i in range(1200):
        km = 5.0 + (i % 200)
        rows.append(row("2024-05-15T18:00:00+00:00", km=km,
                        llsd_max_shear=0.02 * math.exp(-km / 40.0)))
    rep = run(rows)
    f = findings(rep, "llsd_max_shear", "range_median_shift")
    assert f and f[0]["severity"] == "fail", [x["message"] for x in rep["findings"]]


def test_clean_data_passes():
    """The one that keeps the audit trustworthy."""
    rows = []
    for i in range(6):
        for j in range(200):
            rows.append(row(f"2024-{i + 1:02d}-15T18:{j % 60:02d}:00+00:00",
                            km=20.0 + (j % 150),
                            max_dbz=45.0 + (j % 20),
                            vil_kg_m2=10.0 + (j % 30),
                            llsd_max_shear=0.003 + 0.0001 * (j % 17),
                            env_efhl=100.0 + (j % 50),
                            flash_rate_fpm=float(j % 12)))
    rep = run(rows)
    fails = findings(rep, severity="fail")
    assert not fails, [f"{f['feature']}/{f['check']}: {f['message']}" for f in fails]


def test_a_month_with_too_few_rows_cannot_trigger_a_step():
    """Otherwise one sparse month reports every feature as 0% present."""
    rows = [row("2024-01-15T18:00:00+00:00", env_efhl=150.0) for _ in range(200)]
    rows += [row("2024-02-15T18:00:00+00:00", env_efhl=None) for _ in range(3)]
    assert not findings(run(rows), "env_efhl", "presence_step")


# ── The source preflight ───────────────────────────────────────────────────

def test_requirements_walks_plain_derived_and_paired_fields():
    assert _requirements({"idx": ":EFHL:surface:"}) == [":EFHL:surface:"]
    stp = {"derive": ("calc", "stp", [(":CAPE:surface:", None),
                                      ([":VUCSH:0-6000 m above ground:",
                                        ":VVCSH:0-6000 m above ground:"], None)])}
    req = _requirements(stp)
    assert ":CAPE:surface:" in req
    assert ":VUCSH:0-6000 m above ground:" in req and ":VVCSH:0-6000 m above ground:" in req
    assert _requirements({}) == []


def test_present_requires_every_component():
    lines = [":EFHL:surface:", ":CAPE:surface:", ":VUCSH:0-6000 m above ground:"]
    assert _present({"idx": ":EFHL:surface:"}, lines)
    assert not _present({"idx": ":NOPE:surface:"}, lines)
    # A derived field missing one of its inputs is missing.
    stp = {"derive": ("calc", "stp", [(":CAPE:surface:", None),
                                      (":VVCSH:0-6000 m above ground:", None)])}
    assert not _present(stp, lines)


def test_a_tuple_matcher_needs_all_parts_in_one_line():
    """RRFS appends aerosol qualifiers, so `:MASSDEN:8 m` alone matches smoke,
    dust and total. All parts must land in the SAME idx line."""
    spec = {"idx": (":MASSDEN:8 m above ground:", "Particulate organic matter")}
    assert _present(spec, [":MASSDEN:8 m above ground:Particulate organic matter dry"])
    assert not _present(spec, [":MASSDEN:8 m above ground:Dust dry",
                               "Particulate organic matter somewhere else"])


def test_sampled_dates_span_the_range_evenly():
    """Even, not random: a product change is a STEP in time, found by walking
    the axis rather than sampling it."""
    from datetime import date
    ds = sample_dates(date(2024, 1, 1), date(2026, 1, 1), 5)
    assert ds[0] == date(2024, 1, 1) and ds[-1] == date(2026, 1, 1)
    assert len(ds) == 5
    gaps = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
    assert max(gaps) - min(gaps) <= 1, gaps
