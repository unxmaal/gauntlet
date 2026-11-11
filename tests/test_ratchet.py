"""The ratchets a consumer runs over its registry, against a small consumer repo built per test."""

import json
import subprocess

import pytest

from gauntlet import ratchet, skill
from gauntlet.registry import Registry

CLOSED = "a-closed-table-fronting-an-open-set"
SKILL_MD = """\
# The corpus

## A closed table fronting an open set

**Trigger:** is there a dict keyed on something an outside party names?

**Tier:** 2.

**Test shape:** feed a key that is not in the table.

## A statistic blind to the effect under test

**Trigger:** is a summary standing in for the thing measured?

**Tier:** 3.

**Test shape:** none. Ask it.
"""
TABLE = 'FAMILY = {"qwen": 1, "llama": 2, "gemma": 3}\ndef size(name):\n    return FAMILY.get(name, 0)\n'
BOUND = ('import pytest\n@pytest.mark.gauntlet("a-closed-table-fronting-an-open-set", site="app/t.py:FAMILY")\n'
         'def test_size():\n    pass\n')


@pytest.fixture
def consumer(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (skill_dir / "general-corpus.md").write_text("# General\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "t.py").write_text(TABLE, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_t.py").write_text(BOUND, encoding="utf-8")
    snap = tmp_path / "snap.json"
    skill.write_snapshot(snap, skill.read_skill(skill_dir))
    (tmp_path / "defects.json").write_text(json.dumps([{"number": 1, "title": "t", "created": "2026-01-01"}]),
                                           encoding="utf-8")
    reg = Registry(root=tmp_path, snapshot_file=snap, defects_file=tmp_path / "defects.json",
                   backlog_file=tmp_path / "backlog.json", backlog_pin=0,
                   index={CLOSED: {"instances": [{"issue": 1}], "scanners": ["tests/test_t.py::test_size"],
                                   "detector": "closed_tables"},
                          "a-statistic-blind-to-the-effect-under-test": {
                              "instances": [{"issue": 1}], "scanners": [], "review": r"\bmedian\(" }})
    return reg, skill_dir


def _results(reg, skill_dir, **kw):
    out = {}
    for name, fn in ratchet.tests(reg, skill_dir=skill_dir, **kw).items():
        try:
            fn()
            out[name] = "ok"
        except AssertionError as e:
            out[name] = str(e)
    return out


def _failing(reg, skill_dir, **kw):
    return sorted(n for n, r in _results(reg, skill_dir, **kw).items() if r != "ok")


def test_a_clean_consumer_passes_every_ratchet(consumer):
    assert _failing(*consumer) == []
    assert len(ratchet.tests(consumer[0])) == 10


def test_an_unbound_hit_fails_until_it_is_bound_waived_or_in_the_backlog(consumer):
    reg, skill_dir = consumer
    (reg.root / "tests" / "test_t.py").write_text("def test_size():\n    pass\n", encoding="utf-8")
    got = _results(reg, skill_dir)
    assert "app/t.py:FAMILY" in got["test_gauntlet_no_new_code_trips_a_detector_without_a_bound_test"]
    (reg.root / "backlog.json").write_text(json.dumps({CLOSED: ["app/t.py:FAMILY"]}), encoding="utf-8")
    assert _failing(reg, skill_dir) == ["test_gauntlet_the_backlog_only_shrinks"]
    reg.backlog_pin = 1
    assert _failing(reg, skill_dir) == []


def test_a_backlog_entry_that_is_bound_must_leave_the_backlog(consumer):
    reg, skill_dir = consumer
    (reg.root / "backlog.json").write_text(json.dumps({CLOSED: ["app/t.py:FAMILY"]}), encoding="utf-8")
    reg.backlog_pin = 1
    got = _results(reg, skill_dir)
    assert "remove it from the backlog" in got["test_gauntlet_the_backlog_only_shrinks"]


def test_a_binding_or_waiver_for_a_site_no_detector_finds_is_stale(consumer):
    reg, skill_dir = consumer
    (reg.root / "app" / "t.py").write_text("def size(name):\n    return 0\n", encoding="utf-8")
    reg.index[CLOSED]["waivers"] = {"app/u.py:GONE": "why"}
    got = _results(reg, skill_dir)
    assert "binding app/t.py:FAMILY" in got["test_gauntlet_no_binding_or_waiver_is_stale"]
    assert "waiver app/u.py:GONE" in got["test_gauntlet_no_binding_or_waiver_is_stale"]


def test_production_is_the_consumer_s_to_define(consumer):
    reg, skill_dir = consumer
    (reg.root / "tests" / "test_t.py").write_text("def test_size():\n    pass\n", encoding="utf-8")
    reg.production = lambda rel: not rel.startswith(("tests/", "app/"))
    assert _failing(reg, skill_dir) == []


def test_a_corpus_that_moved_past_the_snapshot_fails_until_it_is_regenerated(consumer):
    reg, skill_dir = consumer
    (skill_dir / "SKILL.md").write_text(SKILL_MD.replace("**Tier:** 3.", "**Tier:** 3.\n\n**Bitten:** 2."),
                                        encoding="utf-8")
    assert _failing(reg, skill_dir) == ["test_gauntlet_snapshot_is_the_pinned_corpus"]
    skill.write_snapshot(reg.snapshot_file, skill.read_skill(skill_dir))
    assert _failing(reg, skill_dir) == []


def test_a_registry_problem_an_unknown_detector_and_a_missing_heuristic_each_fail(consumer):
    reg, skill_dir = consumer
    reg.index[CLOSED]["instances"] = [{"issue": 99}]
    reg.unclassified = {1: "why"}
    reg.index["a-statistic-blind-to-the-effect-under-test"].pop("review")
    reg.index[CLOSED]["detector"] = "no_such_detector"
    reg.pending = {CLOSED: {"tier": 2, "instances": [{"issue": 1}]}}
    assert _failing(reg, skill_dir) == [
        "test_gauntlet_every_named_detector_exists", "test_gauntlet_ids_are_unique",
        "test_gauntlet_registry_is_clean", "test_gauntlet_review_heuristics_are_present_and_quiet"]


def test_the_registry_configures_a_detector_and_hands_its_move_tables_to_terminal_without_reader(consumer):
    reg, skill_dir = consumer
    from gauntlet.moves import MoveTable
    (reg.root / "app" / "q.py").write_text('def add(conn):\n    return 1\ndef f(conn):\n    k = add(conn)\n'
                                           '    conn.execute("INSERT INTO r (k) VALUES (?)", (k,))\n', encoding="utf-8")
    reg.index[CLOSED]["detector"] = "commit_then_link"
    assert ratchet.tree_ledger(reg)[0][CLOSED]["unbound"] == []
    reg.detector_config = {"commit_then_link": {"committers": ("add",)}}
    assert ratchet.tree_ledger(reg)[0][CLOSED]["unbound"] == ["app/q.py:f"]
    reg.moves = (MoveTable("app/t.py:MOVES", {"a": {"b"}}, initial=["a"]),)
    assert ratchet.detector_config(reg)["terminal_without_reader"]["tables"] == reg.moves
    assert reg.detector_config == {"commit_then_link": {"committers": ("add",)}}
    reg.detector_config["no_such"] = {}
    assert ratchet.unknown_detectors(reg) == ["detector_config names no_such, which does not exist"]


def test_a_consumer_detector_runs_beside_the_package_s(consumer):
    reg, skill_dir = consumer
    from gauntlet import detectors

    def nothing(trees, production):
        return [detectors.Hit("app/t.py:X", 1, "x")]
    reg.index[CLOSED]["detector"] = "nothing"
    got = _results(reg, skill_dir, extra_detectors={"nothing": nothing})
    assert "app/t.py:X" in got["test_gauntlet_no_new_code_trips_a_detector_without_a_bound_test"]
    assert got["test_gauntlet_every_named_detector_exists"] == "ok"
