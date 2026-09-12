"""The freeze verifier, tested against the shape of the bundle that shipped broken.

On 2026-09-11 a server bundle reached production with no machine-learning models.
Every storm cell lost its rotation, severe and hail probabilities and nothing said
so -- same exe, same folder layout, ~4 MB lighter out of 45. It ran that way for
hours.

The freeze scripts checked that the model files existed IN THE REPO before
starting, and that check passed. Nothing checked the OUTPUT. These tests pin the
check that was missing, against each way a freeze can come out incomplete.
"""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

# Load by PATH, not by package name: the repo's `packaging/` directory collides
# with the installed PyPI `packaging` distribution, and the third-party one wins.
_spec = importlib.util.spec_from_file_location(
    "tbf_verify_freeze",
    Path(__file__).resolve().parents[2] / "packaging" / "verify_freeze.py",
)
_vf = importlib.util.module_from_spec(_spec)
sys.modules["tbf_verify_freeze"] = _vf
_spec.loader.exec_module(_vf)

MIN_MODEL_BYTES = _vf.MIN_MODEL_BYTES
REQUIRED_MODELS = _vf.REQUIRED_MODELS
check = _vf.check


def make_freeze(root, *, models=True, sklearn=True, scripts=True,
                frontend=True, base="/dash/", exe_bytes=20_000_000,
                truncated_model=None, mangled=False):
    """A frozen dashboard-backend tree, complete unless told otherwise."""
    dist = root / "dashboard-backend"
    internal = dist / "_internal"
    internal.mkdir(parents=True)
    (dist / "dashboard-backend.exe").write_bytes(b"\0" * exe_bytes)

    if models:
        data = internal / "data"
        data.mkdir()
        for name in REQUIRED_MODELS:
            size = (MIN_MODEL_BYTES - 1 if name == truncated_model
                    else MIN_MODEL_BYTES + 1000)
            (data / name).write_bytes(b"\0" * size)
    if sklearn:
        (internal / "sklearn").mkdir()
    if scripts:
        (internal / "scripts").mkdir()
    if frontend:
        d = internal / "frontend" / "dist"
        d.mkdir(parents=True)
        asset = "/Program Files/Git/dash/assets/index-x.js" if mangled else f"{base}assets/index-x.js"
        (d / "index.html").write_text(
            f'<!doctype html><script type="module" src="{asset}"></script>',
            encoding="utf-8")
    return dist


def test_a_complete_freeze_passes(tmp_path):
    """The one that keeps the check trustworthy -- a verifier that always fails
    gets bypassed, which is how we got here."""
    dist = make_freeze(tmp_path)
    assert check(dist, "/dash/") == []


# ── The bug that shipped ───────────────────────────────────────────────────

def test_no_models_at_all_is_caught(tmp_path):
    """build-windows.bat adds `backend\\data`, which is EMPTY -- the models live
    at repo-root data/. That path cannot ship them, and did not."""
    dist = make_freeze(tmp_path, models=False)
    problems = check(dist, "/dash/")
    assert problems, "a freeze with no models must not pass"
    assert any("NO MODELS SHIPPED" in p for p in problems), problems


def test_one_missing_model_is_caught(tmp_path):
    dist = make_freeze(tmp_path)
    (dist / "_internal" / "data" / "severe_model.joblib").unlink()
    problems = check(dist, "/dash/")
    assert any("severe_model.joblib" in p for p in problems), problems


def test_a_truncated_model_is_caught(tmp_path):
    """A part-written .joblib unpickles into an exception, not a classifier --
    which at runtime looks exactly like having no model."""
    dist = make_freeze(tmp_path, truncated_model="rotation_model.joblib")
    problems = check(dist, "/dash/")
    assert any("truncated" in p and "rotation_model" in p for p in problems), problems


# ── The silent physics-only failures ───────────────────────────────────────

@pytest.mark.parametrize("pkg", ["sklearn", "scripts"])
def test_a_missing_collect_drops_the_backend_to_physics_only(tmp_path, pkg):
    """Both of these have shipped broken before. Neither raises at startup; the
    backend just stops classifying, which is indistinguishable from quiet
    weather until someone checks a column that should never be empty."""
    dist = make_freeze(tmp_path, **{pkg: False})
    problems = check(dist, "/dash/")
    assert any(pkg in p for p in problems), problems
    assert any("physics-only" in p for p in problems), problems


# ── The white screen ───────────────────────────────────────────────────────

def test_the_wrong_base_path_is_caught(tmp_path):
    """A dashboard frozen at / and served at /dash/ 404s every asset."""
    dist = make_freeze(tmp_path, base="/")
    problems = check(dist, "/dash/")
    assert any("white screen" in p for p in problems), problems
    # ...and the same tree is fine when that IS the intended base.
    assert check(dist, "/") == []


def test_an_msys_mangled_base_is_caught(tmp_path):
    """Setting VITE_BASE_PATH=/dash/ from Git Bash yields
    '/Program Files/Git/dash/' -- a build that looks successful and serves
    nothing. Hit for real on 2026-09-09."""
    dist = make_freeze(tmp_path, mangled=True)
    problems = check(dist, "/dash/")
    assert any("MSYS-mangled" in p for p in problems), problems


def test_no_frontend_is_caught(tmp_path):
    dist = make_freeze(tmp_path, frontend=False)
    assert any("frontend" in p for p in check(dist, "/dash/")), "missing UI not caught"


# ── Degenerate output ──────────────────────────────────────────────────────

def test_a_freeze_that_collected_nothing_is_caught(tmp_path):
    """PyInstaller writing the exe and then failing leaves exactly this."""
    dist = make_freeze(tmp_path)
    shutil.rmtree(dist / "_internal")
    problems = check(dist, "/dash/")
    assert any("_internal" in p for p in problems), problems


def test_a_missing_exe_is_caught_without_crashing(tmp_path):
    dist = make_freeze(tmp_path)
    (dist / "dashboard-backend.exe").unlink()
    problems = check(dist, "/dash/")
    assert len(problems) == 1 and "dashboard-backend.exe" in problems[0]


def test_a_stub_exe_is_caught(tmp_path):
    dist = make_freeze(tmp_path, exe_bytes=1024)
    assert any("only 1,024 bytes" in p for p in check(dist, "/dash/"))


def test_base_check_is_skipped_when_no_base_is_given(tmp_path):
    """The freeze for the DESKTOP has no single right base to assert here."""
    dist = make_freeze(tmp_path, base="/")
    assert check(dist, None) == []
