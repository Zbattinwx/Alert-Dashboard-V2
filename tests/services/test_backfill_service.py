"""
Backfill job control and archive statistics behind the Model dashboard.

The progress figures here are parsed out of a subprocess's log, and the stats
are what tell the operator whether the training data is healthy at all — the
page exists because three feature columns sat at 0.0 for months and nothing
said so. Both are easy to get quietly wrong, so both are pinned.

Run:  python -m pytest tests/services/test_backfill_service.py -v
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.services import backfill_service as bs


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bs, "TRAINING_DATA", tmp_path / "training_data.jsonl")
    monkeypatch.setattr(bs, "BACKFILL_OUT", tmp_path / "training_data.backfill.jsonl")
    monkeypatch.setattr(bs, "BACKFILL_STATE", tmp_path / "backfill_state.json")
    monkeypatch.setattr(bs, "JOB_LOG", tmp_path / "backfill_job.log")
    monkeypatch.setattr(bs, "JOB_META", tmp_path / "backfill_job.json")
    return bs.BackfillService()


class TestProgressParsing:
    LOG = """[01:11:45] Replaying 16 pair(s) on 3 worker(s)
[01:11:47]   KILN 2019-05-27: 86 volume(s)
[01:18:02]     20/86 volumes, 410 rows, 0 err, 0.05 vol/s
[01:31:10]     86/86 volumes, 1720 rows, 2 err, 0.06 vol/s
[01:31:11]   [1/16] KILN 2019-05-27: 1720 rows, 86 volumes, 2 errors
[01:31:12]   [2/16] KIWX 2019-05-27: 1310 rows, 64 volumes, 0 errors
[01:44:00]     31/70 volumes, 505 rows, 0 err, 0.04 vol/s
"""

    def test_completed_pairs_are_totalled(self, svc):
        bs.JOB_LOG.write_text(self.LOG, encoding="utf-8")
        st = svc.status()
        assert st["pairs_done"] == 2
        assert st["pairs_total"] == 16
        assert st["rows"] == 1720 + 1310
        assert st["volumes"] == 86 + 64
        assert st["errors"] == 2

    def test_the_in_flight_pair_is_reported(self, svc):
        bs.JOB_LOG.write_text(self.LOG, encoding="utf-8")
        cur = svc.status()["current"]
        assert cur == {"volume": 31, "of": 70, "rows": 505, "errors": 0, "rate": 0.04}

    def test_recent_pairs_carry_their_site_and_day(self, svc):
        bs.JOB_LOG.write_text(self.LOG, encoding="utf-8")
        recent = svc.status()["recent"]
        assert [(r["site"], r["day"]) for r in recent] == [
            ("KILN", "2019-05-27"), ("KIWX", "2019-05-27")]

    def test_an_absent_log_is_not_an_error(self, svc):
        st = svc.status()
        assert st["running"] is False and st["pairs_done"] == 0

    def test_eta_is_extrapolated_from_completed_pairs(self, svc, monkeypatch):
        bs.JOB_LOG.write_text(self.LOG, encoding="utf-8")
        started = datetime.now(timezone.utc) - timedelta(minutes=20)
        bs.JOB_META.write_text(json.dumps({
            "started_at": started.isoformat(), "pid": 1,
            "days": ["2019-05-27"], "sites": ["KILN"], "workers": 3}), encoding="utf-8")
        monkeypatch.setattr(bs.BackfillService, "is_running", lambda self: True)
        # 2 of 16 pairs in 20 min -> ~10 min each -> ~140 min left.
        assert 120 <= svc.status()["eta_minutes"] <= 160


class TestDataStats:
    @staticmethod
    def _rows(tmp_path, n_pos=3, n_neg=7, dead=("mean_cc",)):
        p = tmp_path / "training_data.jsonl"
        base = datetime(2026, 5, 20, 22, tzinfo=timezone.utc)
        lines = []
        for i in range(n_pos + n_neg):
            feats = {"mean_cc": 0.9, "min_cc": 0.7, "mean_zdr": 0.4,
                     "llsd_max_shear": 0.01, "vil_kg_m2": 30.0,
                     "max_rot_vel_profile_ms": 12.0, "rot_velocity_ms": 9.0,
                     "score_rotation": 40.0, "mrms_azshear_0_2km": 0.0,
                     "mrms_rotation_track_30min": 0.0}
            for d in dead:
                feats[d] = 0.0
            lines.append(json.dumps({
                "ts": (base + timedelta(minutes=5 * i)).isoformat(),
                "cell_id": f"C{i}", "site": "KILN", "lat": 39.5, "lon": -84.0,
                "label": i < n_pos, "features": feats,
            }))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_label_balance(self, svc, tmp_path):
        self._rows(tmp_path)
        d = svc.data_stats(max_age_s=0)
        assert d["total"] == 10 and d["positives"] == 3 and d["negatives"] == 7
        assert d["positive_rate"] == pytest.approx(0.3)

    def test_an_all_zero_feature_is_reported_dead(self, svc, tmp_path):
        """The whole reason this panel exists."""
        self._rows(tmp_path, dead=("mean_cc", "min_cc", "mean_zdr"))
        feats = {f["name"]: f for f in svc.data_stats(max_age_s=0)["features"]}
        assert feats["mean_cc"]["dead"] is True
        assert feats["min_cc"]["dead"] is True
        assert feats["mean_zdr"]["dead"] is True
        assert feats["llsd_max_shear"]["dead"] is False
        assert feats["llsd_max_shear"]["pct_nonzero"] == 100.0

    def test_positives_are_grouped_by_convective_day(self, svc, tmp_path):
        """22Z and 02Z-next-day are one severe evening, not two days."""
        p = tmp_path / "training_data.jsonl"
        rows = []
        for ts in ["2026-05-20T22:00:00+00:00", "2026-05-21T02:00:00+00:00"]:
            rows.append(json.dumps({"ts": ts, "label": True, "site": "KILN",
                                    "features": {"vil_kg_m2": 1.0}}))
        p.write_text("\n".join(rows) + "\n", encoding="utf-8")
        assert svc.data_stats(max_age_s=0)["positive_days"] == 1

    def test_a_missing_archive_is_reported_not_raised(self, svc):
        assert svc.data_stats(max_age_s=0)["exists"] is False

    def test_malformed_lines_are_skipped(self, svc, tmp_path):
        p = tmp_path / "training_data.jsonl"
        p.write_text('{"ts":"2026-05-20T22:00:00+00:00","label":true,"features":{}}\n'
                     '{not json\n', encoding="utf-8")
        assert svc.data_stats(max_age_s=0)["total"] == 1

    def test_results_are_cached(self, svc, tmp_path):
        p = self._rows(tmp_path)
        first = svc.data_stats(max_age_s=300)["total"]
        p.write_text("", encoding="utf-8")          # archive emptied underneath
        assert svc.data_stats(max_age_s=300)["total"] == first


class TestMerge:
    def test_merge_appends_and_archives_the_source(self, svc, tmp_path):
        bs.TRAINING_DATA.write_text('{"a":1}\n', encoding="utf-8")
        bs.BACKFILL_OUT.write_text('{"b":2}\n{"c":3}\n', encoding="utf-8")
        res = svc.merge_backfill()
        assert res["ok"] and res["rows_merged"] == 2
        assert bs.TRAINING_DATA.read_text(encoding="utf-8").count("\n") == 3
        # Renamed rather than deleted — merging twice would duplicate rows.
        assert not bs.BACKFILL_OUT.exists()

    def test_merge_without_output_fails_cleanly(self, svc):
        assert svc.merge_backfill()["ok"] is False

    def test_merge_is_refused_while_a_job_is_running(self, svc, monkeypatch):
        bs.BACKFILL_OUT.write_text('{"b":2}\n', encoding="utf-8")
        monkeypatch.setattr(bs.BackfillService, "is_running", lambda self: True)
        res = svc.merge_backfill()
        assert res["ok"] is False and "running" in res["error"]


class TestStartGuards:
    def test_start_is_refused_while_a_job_is_running(self, svc, monkeypatch):
        monkeypatch.setattr(bs.BackfillService, "is_running", lambda self: True)
        res = svc.start(["2026-05-20"], ["KILN"])
        assert res["ok"] is False and "already running" in res["error"]

    def test_start_needs_days_and_sites(self, svc):
        assert svc.start([], ["KILN"])["ok"] is False
        assert svc.start(["2026-05-20"], [])["ok"] is False


class TestWorkerCap:
    """Five workers on a box with ~10 GB free all died with MemoryError on
    ~111 MB allocations — not one big request, five processes that had already
    eaten the headroom."""

    def test_free_ram_is_readable_without_psutil(self):
        from scripts.backfill_training_data import free_ram_gb
        gb = free_ram_gb()
        assert gb is None or gb > 0

    def test_a_single_worker_is_never_capped(self):
        from scripts.backfill_training_data import _cap_workers
        assert _cap_workers(1) == 1

    def test_the_cap_never_returns_zero(self, monkeypatch):
        import scripts.backfill_training_data as bt
        monkeypatch.setattr(bt, "free_ram_gb", lambda: 1.0)   # nothing spare
        assert bt._cap_workers(8) == 1

    def test_plenty_of_ram_leaves_the_request_alone(self, monkeypatch):
        import scripts.backfill_training_data as bt
        monkeypatch.setattr(bt, "free_ram_gb", lambda: 128.0)
        assert bt._cap_workers(4) == 4


class TestTerminalStartedJobs:
    """A run launched from a terminal must be as visible as one started from the
    UI. The long runs are exactly the ones kicked off by hand and then watched,
    and before this the Model page could only report on jobs it launched itself.
    """

    def test_progress_is_read_from_the_jobs_own_log_path(self, svc, tmp_path, monkeypatch):
        elsewhere = tmp_path / "somewhere_else" / "run.log"
        elsewhere.parent.mkdir()
        elsewhere.write_text(
            "[01:31:11]   [4/16] KILN 2019-05-27: 1895 rows, 86 volumes, 0 errors\n"
            "[01:33:00]     12/87 volumes, 240 rows, 0 err, 0.07 vol/s\n",
            encoding="utf-8")
        bs.JOB_META.write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(), "pid": 1,
            "days": ["2019-05-27"], "sites": ["KILN"], "workers": 3,
            "log_path": str(elsewhere),
        }), encoding="utf-8")
        monkeypatch.setattr(bs.BackfillService, "is_running", lambda self: True)

        st = svc.status()
        assert st["pairs_done"] == 4 and st["pairs_total"] == 16
        assert st["rows"] == 1895
        assert st["current"]["volume"] == 12
        assert st["started_by"] == "terminal"

    def test_a_dashboard_started_job_reads_the_default_log(self, svc, monkeypatch):
        bs.JOB_LOG.write_text(
            "[01:31:11]   [1/2] KIWX 2024-05-07: 900 rows, 60 volumes, 0 errors\n",
            encoding="utf-8")
        bs.JOB_META.write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(), "pid": 1,
            "days": ["2024-05-07"], "sites": ["KIWX"], "workers": 2,
        }), encoding="utf-8")
        monkeypatch.setattr(bs.BackfillService, "is_running", lambda self: True)
        st = svc.status()
        assert st["started_by"] == "dashboard"
        assert st["rows"] == 900

    def test_a_missing_log_path_does_not_raise(self, svc, monkeypatch):
        bs.JOB_META.write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(), "pid": 1,
            "log_path": "Z:/nope/gone.log", "days": [], "sites": [],
        }), encoding="utf-8")
        monkeypatch.setattr(bs.BackfillService, "is_running", lambda self: True)
        assert svc.status()["pairs_done"] == 0


class TestMergeLabelsFirst:
    """Merging raw rows is the quiet failure this guards.

    The scheduled retrain only labels a trailing window, so a 2019 or 2024
    replay merged unlabelled would sit in the archive forever and be skipped by
    every training run -- row count up, positives missing, nothing said.
    """

    def test_merge_labels_before_appending(self, svc, tmp_path, monkeypatch):
        bs.TRAINING_DATA.write_text("", encoding="utf-8")
        bs.BACKFILL_OUT.write_text(json.dumps({
            "ts": "2019-05-27T22:00:00+00:00", "lat": 40.0, "lon": -84.0,
            "features": {"max_dbz": 60.0}}) + "\n", encoding="utf-8")

        called = {}

        def fake_fetch(a, b, phenomena, chunk_days=7):
            called["range"] = (a, b)
            box = [(39.5, -84.5), (39.5, -83.5), (40.5, -83.5), (40.5, -84.5)]
            return [{"wtype": "TO",
                     "issued": datetime(2019, 5, 27, 22, tzinfo=timezone.utc),
                     "expires": datetime(2019, 5, 27, 22, 45, tzinfo=timezone.utc),
                     "origin": datetime(2019, 5, 27, 22, tzinfo=timezone.utc),
                     "coords": box, "centroid_lat": 40.0, "centroid_lon": -84.0,
                     "label_strength": 1.0}]

        import scripts.label_from_warnings as lfw
        monkeypatch.setattr(lfw, "fetch_warnings_range", fake_fetch)

        res = svc.merge_backfill()
        assert res["ok"], res
        assert res["labelled_pos"] == 1, res
        merged = [json.loads(l) for l in
                  bs.TRAINING_DATA.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert merged[0]["label"] is True

    def test_a_seven_year_span_does_not_fetch_the_whole_range(self, svc, monkeypatch):
        """Two replayed days seven years apart must be 2 requests, not ~370."""
        bs.TRAINING_DATA.write_text("", encoding="utf-8")
        bs.BACKFILL_OUT.write_text("".join(
            json.dumps({"ts": f"{d}T22:00:00+00:00", "lat": 40.0, "lon": -84.0,
                        "features": {}}) + "\n"
            for d in ("2019-05-27", "2026-08-11")), encoding="utf-8")
        calls = []

        def fake_fetch(a, b, phenomena, chunk_days=7):
            calls.append((a, b))
            return []

        import scripts.label_from_warnings as lfw
        monkeypatch.setattr(lfw, "fetch_warnings_range", fake_fetch)
        svc.merge_backfill()
        assert len(calls) == 2, calls
        for a, b in calls:
            assert (b - a).days <= 5, (a, b)

    def test_label_first_can_be_skipped(self, svc):
        bs.TRAINING_DATA.write_text("", encoding="utf-8")
        bs.BACKFILL_OUT.write_text('{"ts":"2019-05-27T22:00:00+00:00"}\n', encoding="utf-8")
        res = svc.merge_backfill(label_first=False)
        assert res["ok"] and "labelled_pos" not in res
