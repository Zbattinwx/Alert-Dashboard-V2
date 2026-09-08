r"""
Where the models live -- and, more importantly, where they must NOT.

The bug this pins nearly shipped: `runtime_data_dir()` returned
`sys.executable.parent / "data"`, which in a deployed server is
`server\dashboard-backend\data`. apply-update.ps1 mirrors that whole folder with
`robocopy /MIR`, and /MIR DELETES whatever is in the destination but not the
source -- so every server update would have silently destroyed the collected
training archive and any retrained model. The docstring at the time claimed the
opposite, reasoning about `_internal` being replaced and missing that the entire
backend folder is mirrored.

    python -m pytest tests/services/test_model_paths.py -v
"""

from pathlib import Path

import pytest

from backend.services import model_paths as mp


class TestRuntimeDir:
    def test_an_explicit_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TBF_DATA_DIR", str(tmp_path / "elsewhere"))
        assert mp.runtime_data_dir() == (tmp_path / "elsewhere").resolve()

    def test_frozen_resolves_against_the_cwd_not_the_executable(self, tmp_path, monkeypatch):
        """The deploy root is the CWD start-server.bat sets, and the only place
        the updater preserves. Resolving against the executable instead lands in
        the folder robocopy /MIR wipes."""
        monkeypatch.delenv("TBF_DATA_DIR", raising=False)
        monkeypatch.setattr(mp, "is_frozen", lambda: True)
        exe_dir = tmp_path / "server" / "dashboard-backend"
        exe_dir.mkdir(parents=True)
        monkeypatch.setattr(mp.sys, "executable", str(exe_dir / "dashboard-backend.exe"))
        monkeypatch.chdir(tmp_path / "server")

        got = mp.runtime_data_dir()
        assert got == (tmp_path / "server" / "data").resolve()
        assert got != (exe_dir / "data"), (
            "runtime data resolved inside dashboard-backend/, which the updater "
            "mirrors with /MIR -- the training archive would be deleted on update")

    def test_from_source_it_is_the_repo_data_dir(self, monkeypatch):
        monkeypatch.delenv("TBF_DATA_DIR", raising=False)
        monkeypatch.setattr(mp, "is_frozen", lambda: False)
        assert mp.runtime_data_dir().name == "data"
        assert mp.runtime_data_dir().is_absolute()


class TestResolution:
    def test_a_runtime_model_beats_the_bundled_seed(self, tmp_path, monkeypatch):
        """A retrained model must win over the copy shipped in the exe, or a
        promotion could never take effect."""
        monkeypatch.setenv("TBF_DATA_DIR", str(tmp_path / "rt"))
        (tmp_path / "rt").mkdir()
        (tmp_path / "seed").mkdir()
        (tmp_path / "rt" / "rotation_model.joblib").write_text("new", encoding="utf-8")
        (tmp_path / "seed" / "rotation_model.joblib").write_text("seed", encoding="utf-8")
        monkeypatch.setattr(mp, "bundled_data_dir", lambda: tmp_path / "seed")
        assert mp.find_model("rotation_model.joblib") == tmp_path / "rt" / "rotation_model.joblib"

    def test_the_seed_is_used_until_a_retrain_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TBF_DATA_DIR", str(tmp_path / "rt"))
        (tmp_path / "rt").mkdir()
        (tmp_path / "seed").mkdir()
        (tmp_path / "seed" / "severe_model.joblib").write_text("seed", encoding="utf-8")
        monkeypatch.setattr(mp, "bundled_data_dir", lambda: tmp_path / "seed")
        assert mp.find_model("severe_model.joblib") == tmp_path / "seed" / "severe_model.joblib"

    def test_missing_everywhere_is_none_not_an_exception(self, tmp_path, monkeypatch):
        """No model is a legitimate state -- the tracker runs pure physics."""
        monkeypatch.setenv("TBF_DATA_DIR", str(tmp_path / "rt"))
        (tmp_path / "rt").mkdir()
        monkeypatch.setattr(mp, "bundled_data_dir", lambda: None)
        assert mp.find_model("rotation_model.joblib") is None

    def test_describe_reports_which_source_won(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TBF_DATA_DIR", str(tmp_path / "rt"))
        (tmp_path / "rt").mkdir()
        (tmp_path / "rt" / "rotation_model.joblib").write_text("x", encoding="utf-8")
        monkeypatch.setattr(mp, "bundled_data_dir", lambda: None)
        d = mp.describe()
        assert d["models"]["rotation_model.joblib"]["source"] == "runtime"
        assert d["models"]["severe_model.joblib"]["found"] is False
