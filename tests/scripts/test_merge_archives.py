"""
Merging the server's archive with the Hub's must never invent or lose data.

The failure that matters is silent duplication: the same storm scan recorded on
both machines, surviving as two rows, then landing in both the fit and the
holdout of the next training run -- which reports memorisation as skill. The
other failure is quieter still: keeping one whole copy and discarding a field
only the other had. Probabilities in particular cannot be recreated, because
they are what the model said AT THE TIME and rescoring produces a different
number against a different model.

    python -m pytest tests/scripts/test_merge_archives.py -v
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.merge_training_archives import combine, gained, key_of

ROOT = Path(__file__).resolve().parents[2]
TS = "2026-09-01T18:00:00+00:00"


def row(cid, site="KILN", ts=TS, **kw):
    r = {"ts": ts, "cell_id": cid, "site": site, "features": {"a": 1.0}, "label": None}
    r.update(kw)
    return r


def write(p: Path, rows):
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def run(into: Path, *srcs, extra=()):
    cmd = [sys.executable, str(ROOT / "scripts" / "merge_training_archives.py"),
           "--into", str(into), "--from", *[str(s) for s in srcs], *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))


def read(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestIdentity:
    def test_the_same_scan_on_both_machines_is_one_row(self, tmp_path):
        a = write(tmp_path / "server.jsonl", [row("C1")])
        b = write(tmp_path / "hub.jsonl", [row("C1")])
        assert run(a, b).returncode == 0
        assert len(read(a)) == 1, "the same observation survived twice"

    def test_the_same_cell_id_at_a_different_time_is_a_different_row(self, tmp_path):
        a = write(tmp_path / "a.jsonl", [row("C1")])
        b = write(tmp_path / "b.jsonl", [row("C1", ts="2026-09-01T18:06:00+00:00")])
        run(a, b)
        assert len(read(a)) == 2, "consecutive scans of one cell must both survive"

    def test_the_same_cell_id_at_a_different_site_is_a_different_row(self, tmp_path):
        """Cell ids are only unique within a site's tracking session."""
        a = write(tmp_path / "a.jsonl", [row("C1", site="KILN")])
        b = write(tmp_path / "b.jsonl", [row("C1", site="KIWX")])
        run(a, b)
        assert len(read(a)) == 2

    def test_a_row_with_no_key_is_kept_not_dropped(self, tmp_path):
        orphan = {"features": {}, "label": None}
        assert key_of(orphan) is None
        a = write(tmp_path / "a.jsonl", [row("C1"), orphan])
        b = write(tmp_path / "b.jsonl", [row("C2")])
        run(a, b)
        assert len(read(a)) == 3, "an unkeyable row must not be silently discarded"


class TestFieldWiseCombine:
    def test_a_label_and_a_probability_from_different_copies_both_survive(self):
        """The whole point: the server labelled it, the Hub scored it."""
        server = row("C1", label=True)
        hub = row("C1", p_rotation_model=0.7)
        out = combine(server, hub)
        assert out["label"] is True
        assert out["p_rotation_model"] == 0.7

    def test_it_works_in_either_direction(self):
        server, hub = row("C1", label=True), row("C1", p_rotation_model=0.7)
        a, b = combine(server, hub), combine(hub, server)
        for k in ("label", "p_rotation_model"):
            assert a[k] == b[k], f"{k} depends on merge order"

    def test_an_existing_value_is_never_overwritten(self):
        """Only gaps are filled, so re-running a merge changes nothing."""
        a = row("C1", label=True, p_rotation_model=0.9)
        b = row("C1", label=False, p_rotation_model=0.1)
        out = combine(a, b)
        assert out["label"] is True and out["p_rotation_model"] == 0.9

    def test_merging_is_idempotent(self, tmp_path):
        a = write(tmp_path / "a.jsonl", [row("C1", label=True)])
        b = write(tmp_path / "b.jsonl", [row("C1", p_rotation_model=0.7)])
        run(a, b)
        first = read(a)
        run(a, b)
        assert read(a) == first, "a second merge changed the archive"

    def test_a_false_label_is_rescued_not_treated_as_absent(self):
        """`label: False` is a real negative. A truthiness check would drop it
        and silently convert a confirmed non-event into an unlabelled row."""
        out = combine(row("C1"), row("C1", label=False))
        assert out["label"] is False

    def test_a_zero_probability_is_rescued(self):
        out = combine(row("C1"), row("C1", p_severe_model=0.0))
        assert out["p_severe_model"] == 0.0

    def test_the_richer_feature_set_wins_without_losing_the_other(self):
        a = row("C1"); a["features"] = {"a": 1.0}
        b = row("C1"); b["features"] = {"a": 9.9, "b": 2.0, "c": 3.0}
        out = combine(a, b)
        assert out["features"]["a"] == 1.0, "the incumbent value was overwritten"
        assert out["features"]["b"] == 2.0 and out["features"]["c"] == 3.0

    def test_gained_reports_no_change_when_nothing_was_added(self):
        a = row("C1", label=True, p_rotation_model=0.5)
        assert gained(a, combine(a, row("C1"))) is False


class TestSafety:
    def test_a_backup_is_written(self, tmp_path):
        a = write(tmp_path / "a.jsonl", [row("C1")])
        b = write(tmp_path / "b.jsonl", [row("C2")])
        run(a, b)
        assert list(tmp_path.glob("a.premerge-*.jsonl")), "no backup was taken"

    def test_dry_run_changes_nothing(self, tmp_path):
        a = write(tmp_path / "a.jsonl", [row("C1")])
        b = write(tmp_path / "b.jsonl", [row("C2")])
        before = a.read_text(encoding="utf-8")
        r = run(a, b, extra=("--dry-run",))
        assert r.returncode == 0
        assert a.read_text(encoding="utf-8") == before
        assert not list(tmp_path.glob("a.premerge-*.jsonl"))

    def test_a_missing_source_fails_loudly(self, tmp_path):
        a = write(tmp_path / "a.jsonl", [row("C1")])
        r = run(a, tmp_path / "nope.jsonl")
        assert r.returncode != 0 and "does not exist" in r.stderr

    def test_unparseable_lines_are_skipped_not_fatal(self, tmp_path):
        a = tmp_path / "a.jsonl"
        a.write_text(json.dumps(row("C1")) + "\n{ broken\n", encoding="utf-8")
        b = write(tmp_path / "b.jsonl", [row("C2")])
        r = run(a, b)
        assert r.returncode == 0
        assert len(read(a)) == 2
        assert "unparseable" in r.stdout

    def test_no_temp_file_is_left_behind(self, tmp_path):
        a = write(tmp_path / "a.jsonl", [row("C1")])
        b = write(tmp_path / "b.jsonl", [row("C2")])
        run(a, b)
        assert not list(tmp_path.glob("*.merging.tmp"))
