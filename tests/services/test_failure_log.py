"""
Degraded-detector reporting: log once, count always.

Both halves are load-bearing and each fails in the opposite direction.

Logging every occurrence is what the original code avoided, correctly: a radar
volume carries dozens of sweeps and a scan hundreds of cells, so per-occurrence
logging turns a systemic failure into thousands of identical lines nobody reads.
Silence by volume rather than by omission.

Logging only the first and forgetting the rest is the failure this replaces: a
detector that hiccupped once on a bad sweep and one that has failed forty
thousand times produce the same single line. The COUNT is what separates them.

    python -m pytest tests/services/test_failure_log.py -v
"""

import logging

import pytest

from backend.services import failure_log as fl


@pytest.fixture(autouse=True)
def _clean():
    fl.reset()
    yield
    fl.reset()


class TestLogOnce:
    def test_the_first_failure_is_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tbf.degraded"):
            fl.note_failure("llsd.geometry", "LLSD is producing nothing", ValueError("x"))
        assert len(caplog.records) == 1
        assert "LLSD is producing nothing" in caplog.records[0].getMessage()

    def test_a_thousand_more_are_not(self, caplog):
        """A scan carries hundreds of cells; per-occurrence logging is how a
        systemic failure hides in plain sight."""
        with caplog.at_level(logging.WARNING, logger="tbf.degraded"):
            for _ in range(1000):
                fl.note_failure("llsd.geometry", "LLSD is producing nothing")
        assert len(caplog.records) == 1, f"logged {len(caplog.records)} times"

    def test_distinct_detectors_each_get_one_line(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tbf.degraded"):
            for _ in range(50):
                fl.note_failure("a", "detector A stopped")
                fl.note_failure("b", "detector B stopped")
        assert len(caplog.records) == 2

    def test_the_exception_type_reaches_the_log(self, caplog):
        """Without it the first line says something stopped but not why."""
        with caplog.at_level(logging.WARNING, logger="tbf.degraded"):
            fl.note_failure("k", "something stopped", KeyError("sweep_start_ray_index"))
        msg = caplog.records[0].getMessage()
        assert "KeyError" in msg and "sweep_start_ray_index" in msg


class TestCountAlways:
    def test_every_occurrence_is_counted(self):
        for _ in range(2500):
            fl.note_failure("llsd.geometry", "LLSD is producing nothing")
        snap = fl.snapshot()
        assert snap["total"] == 2500
        assert snap["detectors"][0]["count"] == 2500

    def test_one_failure_and_many_are_distinguishable(self):
        """The distinction the log alone cannot make."""
        fl.note_failure("hiccup", "one bad sweep")
        for _ in range(40000):
            fl.note_failure("systemic", "every sweep failing")
        by_key = {d["key"]: d["count"] for d in fl.snapshot()["detectors"]}
        assert by_key["hiccup"] == 1
        assert by_key["systemic"] == 40000

    def test_the_worst_offender_sorts_first(self):
        fl.note_failure("minor", "x")
        for _ in range(10):
            fl.note_failure("major", "y")
        assert fl.snapshot()["detectors"][0]["key"] == "major"

    def test_the_returned_count_lets_a_caller_escalate(self):
        assert fl.note_failure("k", "x") == 1
        assert fl.note_failure("k", "x") == 2


class TestSnapshot:
    def test_nothing_wrong_is_a_real_answer_not_an_absence(self):
        """An empty snapshot means "no detector has failed", which the health
        panel renders as healthy — so it must be structurally valid, not None."""
        snap = fl.snapshot()
        assert snap["total"] == 0
        assert snap["detectors"] == []

    def test_it_carries_what_broke_not_just_a_key(self):
        """A bare key needs someone who already knows the codebase; the message
        has to say what stopped working."""
        fl.note_failure("llsd.geometry", "LLSD rotation detection is producing nothing")
        d = fl.snapshot()["detectors"][0]
        assert d["what"] == "LLSD rotation detection is producing nothing"

    def test_reset_clears_it(self):
        fl.note_failure("k", "x")
        fl.reset()
        assert fl.snapshot()["total"] == 0

    def test_it_is_safe_from_several_threads(self):
        """Sweeps are processed off the event loop, so counts race."""
        import threading
        def hammer():
            for _ in range(500):
                fl.note_failure("shared", "concurrent")
        ts = [threading.Thread(target=hammer) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert fl.snapshot()["total"] == 2000


class TestWiring:
    def test_the_detectors_actually_call_it(self):
        """Guards against the instrumentation being reverted wholesale."""
        from pathlib import Path
        import backend.services.storm_tracking_service as sts
        import backend.services.nexrad_service as ns

        for mod, least in ((sts, 20), (ns, 3)):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            n = src.count("note_failure(")
            assert n >= least, f"{Path(mod.__file__).name} has only {n} call sites"

    def test_shutdown_paths_are_left_silent(self):
        """CancelledError is normal shutdown. Reporting it as a degraded
        detector would train everyone to ignore this panel."""
        from pathlib import Path
        import backend.services.nexrad_service as ns

        lines = Path(ns.__file__).read_text(encoding="utf-8").split("\n")
        for i, ln in enumerate(lines):
            if "CancelledError" in ln and ln.strip().startswith("except"):
                body = "\n".join(lines[i + 1:i + 3])
                assert "note_failure" not in body, f"line {i+1} reports a normal shutdown"
