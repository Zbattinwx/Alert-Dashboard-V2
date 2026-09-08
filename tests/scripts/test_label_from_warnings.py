"""
Warning-polygon labelling: the temporal contract.

The labeller decides ground truth for the rotation classifier, so a silent
mistake here does not raise — it trains a confidently wrong model.  Two such
mistakes shipped and are guarded below.

1.  **The match window had no lower bound.**  The test was
        (|scan - issued| <= 30min) | ((scan - expires) <= 10min)
    whose second clause is satisfied by ANY scan earlier than the expiry.  A
    cell sitting where a tornado warning would be issued 69 days later was
    labelled a positive.  15,100 of the archive's 16,449 positives were
    mislabelled this way — the classifier was learning geography, not rotation.

2.  **Records outside the fetched warning window were labelled negative.**
    `--days 7` fetches a week of warnings but the loop walked the whole
    archive, so every record older than a week fell through to "no warning in
    area" and became a false negative.

Both are invisible from reading a label count: the totals look healthy either
way.  Run:  python -m pytest tests/scripts/test_label_from_warnings.py -v
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.label_from_warnings import (
    POST_EXPIRY_MIN,
    PRE_WARNING_MIN,
    auto_label,
)

T0 = datetime(2026, 5, 20, 22, 0, tzinfo=timezone.utc)

# A square degree-ish box around (40.0, -84.0); the cell below sits inside it.
BOX = [(39.5, -84.5), (39.5, -83.5), (40.5, -83.5), (40.5, -84.5), (39.5, -84.5)]


def _warning(issued, expires, wtype="TO", coords=BOX):
    lat = sum(c[0] for c in coords) / len(coords)
    lon = sum(c[1] for c in coords) / len(coords)
    return {
        "wtype": wtype, "issued": issued, "expires": expires, "coords": coords,
        "centroid_lat": lat, "centroid_lon": lon,
        "label_strength": 1.0 if wtype == "TO" else 0.4,
    }


def _write(tmp_path: Path, records) -> Path:
    p = tmp_path / "training_data.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return p


def _cell(ts, lat=40.0, lon=-84.0, cid="CELL-1"):
    return {"ts": ts.isoformat(), "cell_id": cid, "lat": lat, "lon": lon,
            "features": {"max_dbz": 60.0}}


def _read(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _run(path, warnings, **kw):
    kw.setdefault("window_start", T0 - timedelta(days=365))
    kw.setdefault("window_end", T0 + timedelta(days=365))
    kw.setdefault("overwrite", False)
    return auto_label(path, warnings, dry_run=False, **kw)


class TestTemporalWindow:
    def test_scan_months_before_the_warning_is_not_positive(self, tmp_path):
        """The regression that mislabelled 92% of the archive's positives."""
        p = _write(tmp_path, [_cell(T0 - timedelta(days=69))])
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))])
        assert _read(p)[0].get("label") is not True

    def test_scan_during_the_warning_is_positive(self, tmp_path):
        p = _write(tmp_path, [_cell(T0 + timedelta(minutes=10))])
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))])
        rec = _read(p)[0]
        assert rec["label"] is True
        assert rec["label_source"] == "TO.W"

    def test_scan_just_before_issuance_is_positive(self, tmp_path):
        """Pre-warning capture is the POINT of the dataset — a detector has to
        fire before the forecaster does, so the window opens early."""
        p = _write(tmp_path, [_cell(T0 - timedelta(minutes=PRE_WARNING_MIN - 5))])
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))])
        assert _read(p)[0]["label"] is True

    def test_scan_well_before_issuance_is_not_positive(self, tmp_path):
        p = _write(tmp_path, [_cell(T0 - timedelta(minutes=PRE_WARNING_MIN + 30))])
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))])
        assert _read(p)[0].get("label") is not True

    def test_scan_after_expiry_is_not_positive(self, tmp_path):
        expires = T0 + timedelta(minutes=45)
        p = _write(tmp_path, [_cell(expires + timedelta(minutes=POST_EXPIRY_MIN + 20))])
        _run(p, [_warning(T0, expires)])
        assert _read(p)[0].get("label") is not True


class TestWindowCoverage:
    def test_record_outside_the_fetched_window_is_left_untouched(self, tmp_path):
        """`--days 7` must not relabel four months of history as quiet."""
        old = T0 - timedelta(days=120)
        p = _write(tmp_path, [_cell(old)])
        auto_label(p, [_warning(T0, T0 + timedelta(minutes=45))],
                   dry_run=False, overwrite=False,
                   window_start=T0 - timedelta(days=7), window_end=T0)
        rec = _read(p)[0]
        assert "label" not in rec, "record outside the warning window was labelled"

    def test_record_inside_the_window_with_no_nearby_warning_is_negative(self, tmp_path):
        p = _write(tmp_path, [_cell(T0, lat=25.0, lon=-110.0)])
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))])
        rec = _read(p)[0]
        assert rec["label"] is False
        assert rec["label_source"] == "no_warning_in_area"


class TestArchiveIntegrity:
    def test_no_records_are_dropped(self, tmp_path):
        """Streaming rewrote the write path; an early exit must not eat a row."""
        cells = [
            _cell(T0 + timedelta(minutes=10)),                 # positive
            _cell(T0, lat=25.0, lon=-110.0),                   # negative
            _cell(T0 - timedelta(days=200)),                   # outside window
            {"ts": "", "lat": 0, "lon": 0, "features": {}},    # unusable
        ]
        p = _write(tmp_path, cells)
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))])
        assert len(_read(p)) == len(cells)

    def test_malformed_lines_survive(self, tmp_path):
        p = tmp_path / "training_data.jsonl"
        p.write_text(json.dumps(_cell(T0 + timedelta(minutes=10))) + "\n"
                     + "{not json\n", encoding="utf-8")
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))])
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2
        assert lines[1] == "{not json"

    def test_strict_tornado_leaves_svr_only_matches_unlabelled(self, tmp_path):
        p = _write(tmp_path, [_cell(T0 + timedelta(minutes=10))])
        _run(p, [_warning(T0, T0 + timedelta(minutes=45), wtype="SV")],
             strict_tornado=True)
        assert "label" not in _read(p)[0]


class TestPointInPolygon:
    """The ring is (lat, lon); a swapped unpack silently returns False forever."""

    def test_point_inside_a_conus_polygon(self):
        from scripts.label_from_warnings import point_in_polygon
        assert point_in_polygon(40.0, -84.0, BOX) is True

    def test_point_outside_a_conus_polygon(self):
        from scripts.label_from_warnings import point_in_polygon
        assert point_in_polygon(41.0, -84.0, BOX) is False
        assert point_in_polygon(40.0, -86.0, BOX) is False

    def test_containment_beats_the_centroid_fallback(self):
        """A cell inside the polygon but far from its centroid must match.
        With the swapped unpack this was the exact case that failed: only cells
        within 5 km of the centroid could ever be labelled positive."""
        from scripts.label_from_warnings import point_in_polygon
        assert point_in_polygon(40.45, -83.55, BOX) is True


class TestOverwriteSafety:
    def test_overwrite_does_not_wipe_labels_it_cannot_regenerate(self, tmp_path):
        """`--overwrite --days 7` must leave older labels alone, not strip them.
        The wipe has to happen after the window guard, not before it."""
        old = T0 - timedelta(days=120)
        rec = _cell(old)
        rec.update(label=True, label_strength=1.0, label_source="TO.W")
        p = _write(tmp_path, [rec])
        auto_label(p, [_warning(T0, T0 + timedelta(minutes=45))],
                   dry_run=False, overwrite=True,
                   window_start=T0 - timedelta(days=7), window_end=T0)
        out = _read(p)[0]
        assert out["label"] is True and out["label_source"] == "TO.W"

    def test_overwrite_reevaluates_inside_the_window(self, tmp_path):
        rec = _cell(T0 + timedelta(minutes=10), lat=25.0, lon=-110.0)
        rec.update(label=True, label_strength=1.0, label_source="TO.W")
        p = _write(tmp_path, [rec])
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))], overwrite=True)
        out = _read(p)[0]
        assert out["label"] is False and out["label_source"] == "no_warning_in_area"


class TestConcurrentAppend:
    def test_rows_appended_during_the_run_survive(self, tmp_path, monkeypatch):
        """The dashboard's live QA reporter appends to this file while we work.
        The atomic swap must not discard a live storm's scans."""
        import scripts.label_from_warnings as mod

        p = _write(tmp_path, [_cell(T0 + timedelta(minutes=10))])
        late = _cell(T0 + timedelta(minutes=20), cid="CELL-LATE")

        real = mod.point_in_polygon
        state = {"done": False}

        def _appending(lat, lon, coords):
            # Simulate the collector appending mid-run, exactly once.
            if not state["done"]:
                state["done"] = True
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(late) + "\n")
            return real(lat, lon, coords)

        monkeypatch.setattr(mod, "point_in_polygon", _appending)
        _run(p, [_warning(T0, T0 + timedelta(minutes=45))])

        out = _read(p)
        assert state["done"], "the concurrent append never fired"
        assert len(out) == 2, f"expected the late row to survive, got {len(out)}"
        assert out[1]["cell_id"] == "CELL-LATE"
        assert out[0]["label"] is True


class TestFetchFiltering:
    """IEM ignores phenomena/significance on this endpoint, so the filter has to
    happen client-side on what the feature actually says it is."""

    @staticmethod
    def _payload():
        ring = [[-84.5, 39.5], [-83.5, 39.5], [-83.5, 40.5], [-84.5, 40.5], [-84.5, 39.5]]
        def feat(ph, sig, issue, expire, begin=None, end=None):
            pr = {"phenomena": ph, "significance": sig,
                  "issue": issue, "expire": expire}
            if begin:
                pr["polygon_begin"], pr["polygon_end"] = begin, end
            return {"properties": pr,
                    "geometry": {"type": "Polygon", "coordinates": [ring]}}
        return {"features": [
            feat("TO", "W", "2026-05-20T22:00:00Z", "2026-05-20T22:45:00Z"),
            feat("SV", "W", "2026-05-20T22:00:00Z", "2026-05-20T23:00:00Z"),
            feat("FL", "W", "2026-05-03T14:53:00Z", "2026-05-24T14:53:00Z"),  # 21 days
            feat("FA", "Y", "2026-05-20T22:00:00Z", "2026-05-20T23:00:00Z"),
            feat("MA", "W", "2026-05-20T22:00:00Z", "2026-05-20T23:00:00Z"),
            feat("TO", "A", "2026-05-20T20:00:00Z", "2026-05-21T04:00:00Z"),  # watch
        ]}

    @pytest.fixture
    def patched(self, monkeypatch):
        import io, urllib.request as u
        import scripts.label_from_warnings as mod
        body = json.dumps(self._payload()).encode()
        monkeypatch.setattr(mod.urllib.request, "urlopen",
                            lambda *a, **k: io.BytesIO(body))
        return mod

    def test_only_requested_types_survive(self, patched):
        w = patched.fetch_iem_warnings(T0, T0 + timedelta(hours=2), ["TO", "SV"])
        assert sorted(x["wtype"] for x in w) == ["SV", "TO"]

    def test_flood_warning_is_not_relabelled_as_a_tornado(self, patched):
        """The bug that put 21-day flood warnings in as TO.W at strength 1.0."""
        w = patched.fetch_iem_warnings(T0, T0 + timedelta(hours=2), ["TO"])
        assert len(w) == 1
        assert w[0]["wtype"] == "TO"
        assert w[0]["label_strength"] == 1.0

    def test_no_duplicate_warnings_across_requested_phenomena(self, patched):
        """One request per phenomenon against an unfiltered endpoint used to add
        every warning twice, with contradictory label strengths."""
        w = patched.fetch_iem_warnings(T0, T0 + timedelta(hours=2), ["TO", "SV"])
        assert len(w) == 2, [x["wtype"] for x in w]
        assert {x["wtype"]: x["label_strength"] for x in w} == {"TO": 1.0, "SV": 0.4}

    def test_absurd_validity_is_clamped(self, patched):
        from scripts.label_from_warnings import MAX_WARNING_MIN
        w = patched.fetch_iem_warnings(T0, T0 + timedelta(hours=2), ["TO", "SV", "FL"])
        for x in w:
            span = (x["expires"] - x["issued"]).total_seconds() / 60
            assert span <= MAX_WARNING_MIN, f"{x['wtype']} span {span} min"


class TestSparseArchiveBlocks:
    """A backfilled archive is sparse: four replayed days can span seven years.
    Fetching the whole span would be ~370 IEM requests to label 4 days, and each
    is a chance for a chunk to fail and look like a quiet period."""

    def _write(self, tmp_path, days):
        p = tmp_path / "training_data.jsonl"
        p.write_text("".join(
            json.dumps({"ts": f"{d}T22:00:00+00:00", "lat": 40.0, "lon": -84.0,
                        "features": {}}) + "\n" for d in days), encoding="utf-8")
        return p

    def test_far_apart_days_become_separate_blocks(self, tmp_path):
        from scripts.label_from_warnings import data_day_blocks
        p = self._write(tmp_path, ["2019-05-27", "2024-05-07",
                                   "2026-08-11", "2026-08-15"])
        blocks = data_day_blocks(p)
        assert len(blocks) == 4, [(a.date(), b.date()) for a, b in blocks]

    def test_adjacent_days_merge_into_one_block(self, tmp_path):
        from scripts.label_from_warnings import data_day_blocks
        p = self._write(tmp_path, ["2026-08-11", "2026-08-12", "2026-08-13"])
        assert len(data_day_blocks(p)) == 1

    def test_a_convective_day_spanning_midnight_stays_one_block(self, tmp_path):
        from scripts.label_from_warnings import data_day_blocks
        p = self._write(tmp_path, ["2019-05-27", "2019-05-28"])
        assert len(data_day_blocks(p)) == 1

    def test_blocks_are_padded_so_edge_records_can_match(self, tmp_path):
        from scripts.label_from_warnings import data_day_blocks
        p = self._write(tmp_path, ["2026-08-11"])
        (a, b), = data_day_blocks(p)
        assert a < datetime(2026, 8, 11, tzinfo=timezone.utc) < b

    def test_an_empty_archive_yields_no_blocks(self, tmp_path):
        from scripts.label_from_warnings import data_day_blocks
        p = tmp_path / "training_data.jsonl"
        p.write_text("", encoding="utf-8")
        assert data_day_blocks(p) == []
