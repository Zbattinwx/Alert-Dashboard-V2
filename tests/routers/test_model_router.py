"""
The /api/model routes, after being lifted out of main.py.

A route-table comparison proves the paths still register. It does NOT prove the
handlers still RUN, and that gap is not hypothetical: the extraction moved
handlers whose service imports sit inside the function body and read
`from .services...`. One dot resolved to `backend` in main.py and to
`backend.routers` here, so every one of those handlers registered perfectly and
raised ModuleNotFoundError the moment it was called. Route tables matched, 379
tests passed, and every endpoint in the group was broken.

So these tests CALL the endpoints. They assert shape, not content -- the point
is that the handler executes and its imports resolve, which is exactly what the
move endangered.

    python -m pytest tests/routers/test_model_router.py -v
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture(scope="module")
def client():
    # Constructed WITHOUT the context manager on purpose. `with TestClient(app)`
    # runs the app's lifespan, which starts the radar poller and NWWS client --
    # the first version of this fixture did that and spent 104 seconds
    # downloading real Level 2 volumes to test five JSON endpoints, with the
    # side effect of a second radar poller racing whatever else is running.
    #
    # These routes read service singletons and handle their absence, which is
    # also the state a real request hits before startup finishes.
    return TestClient(app, raise_server_exceptions=True)


class TestRoutesAreReachable:
    """Each must execute. A 500 here is an import that did not survive the move."""

    @pytest.mark.parametrize("path", [
        "/api/model/paths",
        "/api/model/scorecard?days=1",
        "/api/model/rotation/status",
        "/api/model/backfill/status",
        "/api/model/training/stats",
    ])
    def test_get_executes(self, client, path):
        r = client.get(path)
        assert r.status_code != 404, f"{path} is not registered"
        assert r.status_code < 500, (
            f"{path} returned {r.status_code} -- the handler raised. If this is "
            f"ModuleNotFoundError, a relative import did not survive the move "
            f"from main.py: {r.text[:200]}")
        assert r.headers["content-type"].startswith("application/json")


class TestPayloads:
    def test_paths_reports_load_state(self, client):
        """The health panel reads exactly these keys."""
        d = client.get("/api/model/paths").json()
        for k in ("frozen", "runtime_dir", "models", "tracker", "degraded"):
            assert k in d, f"missing {k} -- the health panel reads it"

    def test_scorecard_reports_coverage(self, client):
        d = client.get("/api/model/scorecard?days=7").json()
        assert d["window_days"] == 7
        for k in ("targets", "verdict"):
            assert k in d
        # "never scored" must be its own figure, not folded into a probability
        assert "unscored" in d or "error" in d

    def test_scorecard_honours_the_window(self, client):
        assert client.get("/api/model/scorecard?days=3").json()["window_days"] == 3


class TestExtractionIntegrity:
    def test_the_group_lives_in_the_router_not_main(self):
        """Guards against a later edit re-adding a model route to main.py,
        which would leave the group split across two files."""
        from pathlib import Path
        import backend.main as m

        src = Path(m.__file__).read_text(encoding="utf-8")
        assert '@app.get("/api/model/' not in src
        assert '@app.post("/api/model/' not in src

    def test_no_single_dot_service_imports_remain(self):
        """The exact bug this file exists for."""
        import re
        from pathlib import Path
        import backend.routers.model as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        bad = re.findall(r"^\s*from \.(services|config|models|parsers|utils)\b",
                         src, re.M)
        assert not bad, (
            f"{bad} -- one dot resolves to backend.routers here, not backend; "
            "these fail only when the endpoint is called")

    def test_every_model_route_is_on_the_router(self):
        from backend.routers.model import router
        paths = {r.path for r in router.routes}
        assert len(paths) == 11, f"expected 11 model routes, found {len(paths)}"
        assert all(p.startswith("/api/model/") for p in paths)
