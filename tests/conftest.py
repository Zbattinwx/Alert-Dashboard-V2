"""
Isolate the test suite from the OPERATOR'S live configuration.

`get_settings()` merges `data/user_settings.json` -- the file the dashboard UI
writes when you change coverage counties, target phenomena or filter states --
over the defaults. Every test that touches settings therefore inherited whatever
the machine's operator had configured, which meant:

  * three alert-parser tests failed on this machine and would pass on another;
  * changing a setting in the dashboard UI could break the build;
  * CI, a fresh clone, and this machine would each disagree.

Those failures were written off as pre-existing parser bugs for some time, which
is its own cost: real regressions hide behind noise everyone has learned to
ignore.

The parser tests are PARSER tests -- they assert that a Winter Storm Warning
yields snow amounts, that a cancellation yields CAN. Judging them by deployment
filters (which states this operator covers, which phenomena they alert on) tests
the wrong thing and produces failures that look like parser bugs. So settings
here are pinned PERMISSIVE: accept every state and every phenomenon, and let the
parser be judged on parsing.

The filtering itself is not thereby untested -- see tests/config/test_alert_filtering.py,
which exercises it directly and was written because this indirect, accidental
coverage was the only coverage it had.
"""

import pytest

# Everything the parser tests construct. Deliberately a superset of the shipped
# defaults: SPS is absent from those, which silently excluded every Special
# Weather Statement fixture.
_PERMISSIVE = {
    "filter_states": [],          # empty = accept all states
    "filter_counties": {},        # no county restriction
    "target_phenomena": [
        "TO", "SV", "FF", "FA", "WS", "WW", "BZ", "IS", "LE", "WC", "HW",
        "SPS", "EW", "SQ", "DS", "TR", "HU", "SS", "MA", "SM", "FL", "FZ",
        "HT", "EH", "FR", "WI", "AV", "FFA", "FFS", "FFW", "FLA",
    ],
}


@pytest.fixture(autouse=True, scope="session")
def _isolate_user_settings(tmp_path_factory):
    """Replace the operator's override file with a pinned permissive one.

    Session-scoped and autouse: the coupling is global, so opting in per test
    would leave the same trap for the next person. `get_settings` is lru_cached,
    so the cache is cleared on the way IN and on the way OUT -- without the
    second clear a later in-process consumer would keep serving test settings.

    A test that needs specific filtering should set it explicitly rather than
    relying on whatever this file happens to contain.
    """
    import json

    from backend.config import settings as cfg

    d = tmp_path_factory.mktemp("cfg")
    f = d / "user_settings.json"
    f.write_text(json.dumps(_PERMISSIVE), encoding="utf-8")

    original = cfg._USER_SETTINGS_FILE
    cfg._USER_SETTINGS_FILE = f
    cfg.get_settings.cache_clear()
    try:
        yield
    finally:
        cfg._USER_SETTINGS_FILE = original
        cfg.get_settings.cache_clear()
