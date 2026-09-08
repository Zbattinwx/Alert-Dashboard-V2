"""
Radar DISPLAY can be turned off without touching radar INGESTION.

The dashboard has two independent consumers of a Level 2 volume: the gridded
path that feeds storm-cell tracking (and therefore the Escalation Index), and
the per-product render path that exists to draw radar inside the dashboard UI.
Now that the radar app decodes Level 2 client-side, nothing consumes the second
one on a server -- but it still costs 4 renders per volume plus
nexrad_history_count frames retained per product per site.

The thing worth guarding is that turning display off does NOT quietly stop the
tracker. That would remove the cells the Index scores, and the symptom -- no
storm cells -- looks like a radar outage rather than a config change.
"""

import pytest

from backend.config.settings import Settings


class TestSetting:
    def test_display_frames_are_on_by_default(self):
        """Existing deployments must be unchanged by this addition."""
        assert Settings().nexrad_serve_frames is True

    def test_it_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("NEXRAD_SERVE_FRAMES", "false")
        assert Settings().nexrad_serve_frames is False


class TestIngestionIsUnaffected:
    def test_the_render_loop_is_the_only_thing_gated(self):
        """`_serve_frames` must not appear anywhere near the gridding path.

        A source-level check on purpose: the failure being guarded is someone
        later reusing the flag to skip ingestion "since display is off", which
        would silently stop the Escalation Index. The tracker reads the grid via
        on_volume_ready and must never consult this flag.
        """
        from pathlib import Path
        import backend.services.nexrad_service as ns

        src = Path(ns.__file__).read_text(encoding="utf-8")
        uses = [ln.strip() for ln in src.splitlines() if "_serve_frames" in ln]
        # One assignment in __init__, one guard in the render path. Nothing else.
        assert len(uses) == 2, f"unexpected uses of _serve_frames: {uses}"
        assert any("self._serve_frames: bool" in u for u in uses)
        assert any(u.startswith("if not self._serve_frames") for u in uses)

    def test_the_grid_callback_is_not_conditional_on_it(self):
        from pathlib import Path
        import backend.services.nexrad_service as ns

        src = Path(ns.__file__).read_text(encoding="utf-8").splitlines()
        idx = [i for i, ln in enumerate(src) if "if not self.on_volume_ready" in ln]
        assert idx, "grid callback guard not found"
        window = "\n".join(src[max(0, idx[0] - 25): idx[0] + 5])
        assert "_serve_frames" not in window, (
            "the gridding path consults the display flag -- turning off display "
            "would stop storm-cell tracking and silently disable the Escalation Index")


class TestEndpointsAreHonest:
    @pytest.mark.parametrize("route", ["/api/radar/frame/", "/api/radar/frames/"])
    def test_a_disabled_endpoint_says_why(self, route):
        """An empty frame list reads as "no radar right now". It must instead
        say the frames are disabled -- the same ambiguity that let a model which
        failed to load look like a model that was never trained."""
        from pathlib import Path
        import backend.main as m

        src = Path(m.__file__).read_text(encoding="utf-8")
        i = src.find(f'@app.get("{route}')
        assert i != -1, f"route {route} not found"
        block = src[i:i + 1800]
        assert "nexrad_serve_frames" in block, f"{route} does not check the flag"
        assert "disabled" in block.lower()
