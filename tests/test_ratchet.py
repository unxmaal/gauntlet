"""The ratchets a consumer runs over its registry, against a small consumer repo built per test."""

import json
import subprocess

import pytest

from gauntlet import ratchet, registry, skill
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
                   backlog_file=tmp_path / "backlog.txt",
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


def _git(root, *args):
    cmd = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def _commit(root, msg):
    _git(root, "add", "--", ".")
    _git(root, "commit", "-q", "-m", msg)


def _publish(root):
    # This commit becomes origin/main, the base the backlog is held to.
    _commit(root, "main")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")


def _backlog(reg):
    return ratchet.backlog_problems(reg, ratchet.tree_ledger(reg)[0])


def _table(name):
    return f'{name} = {{"qwen": 1, "llama": 2, "gemma": 3}}\ndef f(name):\n    return {name}.get(name, 0)\n'


def _rows(*names):
    return "".join(f"{CLOSED} app/{n.lower()}.py:{n}\n" for n in names)


def test_an_unbound_hit_fails_until_it_is_bound_waived_or_in_the_backlog(consumer):
    reg, skill_dir = consumer
    (reg.root / "tests" / "test_t.py").write_text("def test_size():\n    pass\n", encoding="utf-8")
    got = _results(reg, skill_dir)
    assert "app/t.py:FAMILY" in got["test_gauntlet_no_new_code_trips_a_detector_without_a_bound_test"]
    (reg.root / "backlog.txt").write_text(f"{CLOSED} app/t.py:FAMILY\n", encoding="utf-8")
    _publish(reg.root)
    assert _failing(reg, skill_dir) == []


def test_a_backlog_entry_that_is_bound_must_leave_the_backlog(consumer):
    reg, _ = consumer
    (reg.root / "backlog.txt").write_text(f"{CLOSED} app/t.py:FAMILY\n", encoding="utf-8")
    _publish(reg.root)
    assert _backlog(reg) == [f"{CLOSED}: app/t.py:FAMILY is bound, waived or gone: remove it from the backlog"]


@pytest.fixture
def three_rows(consumer):
    reg, skill_dir = consumer
    for name in ("ALPHA", "BRAVO", "CHARLIE"):
        (reg.root / "app" / f"{name.lower()}.py").write_text(_table(name), encoding="utf-8")
    (reg.root / "backlog.txt").write_text(_rows("ALPHA", "BRAVO", "CHARLIE"), encoding="utf-8")
    _publish(reg.root)
    assert _failing(reg, skill_dir) == []
    return reg, skill_dir


def _fix(root, name):
    (root / "app" / f"{name.lower()}.py").unlink()
    rows = (root / "backlog.txt").read_text(encoding="utf-8").replace(_rows(name), "")
    (root / "backlog.txt").write_text(rows, encoding="utf-8")
    _commit(root, f"fix {name}")


def test_two_branches_that_each_drop_a_row_merge_cleanly_and_stay_green(three_rows):
    reg, skill_dir = three_rows
    root = reg.root
    main = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "switch", "-q", "-c", "drop-charlie")
    _fix(root, "CHARLIE")
    assert _failing(reg, skill_dir) == []
    _git(root, "switch", "-q", "-c", "drop-alpha", main)
    _fix(root, "ALPHA")
    assert _failing(reg, skill_dir) == []
    _git(root, "update-ref", "refs/remotes/origin/main", "drop-charlie")
    assert _failing(reg, skill_dir) == []
    _git(root, "merge", "-q", "--no-edit", "refs/remotes/origin/main")
    assert (root / "backlog.txt").read_text(encoding="utf-8") == _rows("BRAVO")
    assert _git(root, "diff", "--name-only", f"{main}..HEAD") == "app/alpha.py\napp/charlie.py\nbacklog.txt\n"
    assert _failing(reg, skill_dir) == []


def test_a_row_that_joins_the_backlog_fails_even_when_another_leaves(three_rows):
    reg, _ = three_rows
    (reg.root / "app" / "delta.py").write_text(_table("DELTA"), encoding="utf-8")
    (reg.root / "backlog.txt").write_text(_rows("ALPHA", "BRAVO", "CHARLIE", "DELTA"), encoding="utf-8")
    want = f"{CLOSED}: app/delta.py:DELTA joined the backlog since origin/main: bind it or waive it instead"
    assert _backlog(reg) == [want]
    (reg.root / "app" / "alpha.py").unlink()
    (reg.root / "backlog.txt").write_text(_rows("BRAVO", "CHARLIE", "DELTA"), encoding="utf-8")
    assert _backlog(reg) == [want]


def test_a_backlog_with_no_base_to_hold_it_to_fails_with_the_fetch_step(consumer):
    reg, skill_dir = consumer
    (reg.root / "tests" / "test_t.py").write_text("def test_size():\n    pass\n", encoding="utf-8")
    (reg.root / "backlog.txt").write_text(f"{CLOSED} app/t.py:FAMILY\n", encoding="utf-8")
    want = "git fetch origin main (a CI checkout needs fetch-depth: 0)"
    assert _backlog(reg) == [want]
    _commit(reg.root, "no origin/main yet")
    assert _backlog(reg) == [want]
    _git(reg.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert _failing(reg, skill_dir) == []


def test_a_base_from_before_the_wiring_seeds_the_backlog(consumer):
    reg, skill_dir = consumer
    reg.snapshot_file.unlink()
    _publish(reg.root)
    skill.write_snapshot(reg.snapshot_file, skill.read_skill(skill_dir))
    (reg.root / "app" / "alpha.py").write_text(_table("ALPHA"), encoding="utf-8")
    (reg.root / "backlog.txt").write_text(_rows("ALPHA"), encoding="utf-8")
    assert _failing(reg, skill_dir) == []


def test_a_backlog_absent_at_a_wired_base_counts_as_empty(consumer):
    reg, _ = consumer
    _publish(reg.root)
    (reg.root / "app" / "alpha.py").write_text(_table("ALPHA"), encoding="utf-8")
    (reg.root / "backlog.txt").write_text(_rows("ALPHA"), encoding="utf-8")
    assert _backlog(reg) == [
        f"{CLOSED}: app/alpha.py:ALPHA joined the backlog since origin/main: bind it or waive it instead"]


def test_a_json_backlog_at_the_base_holds_a_txt_backlog_on_the_branch(consumer):
    reg, skill_dir = consumer
    for name in ("ALPHA", "BRAVO"):
        (reg.root / "app" / f"{name.lower()}.py").write_text(_table(name), encoding="utf-8")
    (reg.root / "backlog.json").write_text(json.dumps({CLOSED: ["app/alpha.py:ALPHA"]}), encoding="utf-8")
    _publish(reg.root)
    _git(reg.root, "rm", "-q", "backlog.json")
    (reg.root / "backlog.txt").write_text(_rows("ALPHA", "BRAVO"), encoding="utf-8")
    want = f"{CLOSED}: app/bravo.py:BRAVO joined the backlog since origin/main: bind it or waive it instead"
    assert _backlog(reg) == [want]
    (reg.root / "app" / "bravo.py").unlink()
    (reg.root / "backlog.txt").write_text(_rows("ALPHA"), encoding="utf-8")
    assert _failing(reg, skill_dir) == []


def _move(root, name, to):
    (root / "app" / f"{name.lower()}.py").rename(root / "app" / f"{to}.py")
    rows = (root / "backlog.txt").read_text(encoding="utf-8")
    (root / "backlog.txt").write_text(rows.replace(f"app/{name.lower()}.py:{name}", f"app/{to}.py:{name}"),
                                      encoding="utf-8")


def test_a_site_that_moved_files_passes_and_says_so(three_rows):
    reg, skill_dir = three_rows
    _move(reg.root, "ALPHA", "split")
    with pytest.warns(UserWarning, match="app/alpha.py:ALPHA moved to app/split.py:ALPHA"):
        assert _backlog(reg) == []
    with pytest.warns(UserWarning):
        assert _failing(reg, skill_dir) == []


def test_a_move_beside_a_new_row_fails_only_on_the_new_row(three_rows):
    reg, _ = three_rows
    _move(reg.root, "ALPHA", "split")
    (reg.root / "app" / "delta.py").write_text(_table("DELTA"), encoding="utf-8")
    (reg.root / "backlog.txt").write_text((reg.root / "backlog.txt").read_text(encoding="utf-8") + _rows("DELTA"),
                                          encoding="utf-8")
    with pytest.warns(UserWarning, match="moved to app/split.py:ALPHA"):
        assert _backlog(reg) == [
            f"{CLOSED}: app/delta.py:DELTA joined the backlog since origin/main: bind it or waive it instead"]


def test_a_moved_row_with_two_candidates_on_either_side_fails(consumer):
    reg, _ = consumer
    for rel in ("one", "two"):
        (reg.root / "app" / f"{rel}.py").write_text(_table("ECHO"), encoding="utf-8")
    (reg.root / "backlog.txt").write_text(f"{CLOSED} app/one.py:ECHO\n{CLOSED} app/two.py:ECHO\n", encoding="utf-8")
    _publish(reg.root)
    for rel in ("one", "two"):
        (reg.root / "app" / f"{rel}.py").unlink()
    (reg.root / "app" / "three.py").write_text(_table("ECHO"), encoding="utf-8")
    (reg.root / "backlog.txt").write_text(f"{CLOSED} app/three.py:ECHO\n", encoding="utf-8")
    assert _backlog(reg) == [
        f"{CLOSED}: app/three.py:ECHO joined the backlog since origin/main: bind it or waive it instead"]
    (reg.root / "app" / "four.py").write_text(_table("ECHO"), encoding="utf-8")
    (reg.root / "app" / "one.py").write_text(_table("ECHO"), encoding="utf-8")
    (reg.root / "backlog.txt").write_text(f"{CLOSED} app/four.py:ECHO\n{CLOSED} app/one.py:ECHO\n"
                                          f"{CLOSED} app/three.py:ECHO\n", encoding="utf-8")
    assert _backlog(reg) == [
        f"{CLOSED}: app/four.py:ECHO joined the backlog since origin/main: bind it or waive it instead",
        f"{CLOSED}: app/three.py:ECHO joined the backlog since origin/main: bind it or waive it instead"]


def test_a_site_that_moved_under_a_backlog_line_left_behind_names_the_new_path(three_rows):
    reg, _ = three_rows
    (reg.root / "app" / "alpha.py").rename(reg.root / "app" / "split.py")
    assert _backlog(reg) == [
        f"{CLOSED}: app/alpha.py:ALPHA moved to app/split.py:ALPHA: update its backlog line to the new path"]
    (reg.root / "app" / "other.py").write_text(_table("ALPHA"), encoding="utf-8")
    assert _backlog(reg) == [
        f"{CLOSED}: app/alpha.py:ALPHA is bound, waived or gone: remove it from the backlog"]


def test_a_json_and_a_txt_backlog_load_the_same_rows(tmp_path):
    want = {CLOSED: ["app/a.py:A", "app/b.py:B"], "other-class": ["app/c.py:has a space"]}
    (tmp_path / "backlog.json").write_text(json.dumps(want), encoding="utf-8")
    (tmp_path / "backlog.txt").write_text(f"{CLOSED} app/a.py:A\n\n{CLOSED} app/b.py:B\n"
                                          "other-class app/c.py:has a space\n\n", encoding="utf-8")
    for name in ("backlog.json", "backlog.txt"):
        reg = Registry(root=tmp_path, index={}, snapshot_file=tmp_path / "s", defects_file=tmp_path / "d",
                       backlog_file=tmp_path / name)
        assert reg.backlog() == want
    assert registry.backlog_text(want) == (tmp_path / "backlog.txt").read_text().replace("\n\n", "\n")


def test_a_backlog_pin_is_ignored_with_a_deprecation_warning(consumer):
    reg, skill_dir = consumer
    reg.backlog_pin = 7
    with pytest.warns(DeprecationWarning, match="backlog_pin is ignored"):
        assert _failing(reg, skill_dir) == []


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


def test_a_class_names_two_detectors_and_both_are_checked(consumer):
    reg, _ = consumer
    reg.index[CLOSED]["detector"] = ["commit_then_link", "no_such"]
    assert ratchet.unknown_detectors(reg) == [f"{CLOSED}: detector no_such does not exist"]
    reg.index[CLOSED]["detector"] = ["commit_then_link", "torn_append"]
    assert ratchet.unknown_detectors(reg) == []
