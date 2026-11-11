"""Move-table checks: each finding fires on a table that has the fault and stays quiet on one that does not."""

import ast

import pytest

from gauntlet import detectors, moves, ratchet
from gauntlet.moves import MoveTable

SOUND = {(None, "new"): {"w"}, ("new", "run"): {"w"}, ("run", "done"): {"w"}, ("run", "failed"): {"w"},
         ("done", "new"): {"refetch"}}
SOUND_ARGS = dict(initial=[None], terminal=["done", "failed"], reopens={("done", "new"): "refetch"})


def _checks(table, **kw):
    return {(f.check, f.state) for f in moves.check(table, **{**SOUND_ARGS, **kw})}


def test_a_sound_table_has_no_finding():
    assert moves.check(SOUND, **SOUND_ARGS) == []


def test_every_shape_normalizes_to_the_same_table():
    pairs = set(SOUND)
    by_state = {None: {"new"}, "new": {"run"}, "run": {"done", "failed"}, "done": {"new"}}
    assert moves.normalize(SOUND) == moves.normalize(pairs) == moves.normalize(by_state)
    assert moves.normalize(by_state)["failed"] == frozenset()


def test_per_writer_tables_merge_into_one():
    a, b = {("x", "y"): {"job"}}, {("y", "z"): {"switch"}, ("x", "y"): {"settle"}}
    assert moves.merge(a, b) == {"x": {"y"}, "y": {"z"}, "z": frozenset()}


def test_a_bad_shape_is_refused():
    with pytest.raises(TypeError):
        moves.normalize({"a": "b"})
    with pytest.raises(TypeError):
        moves.normalize([("a", "b", "c")])
    with pytest.raises(TypeError):
        moves.check(SOUND, initial=[None], terminal=[], reopens=[("a", "b")])


def test_an_unreachable_state_is_found():
    table = {**SOUND, ("orphan", "done"): {"w"}}
    assert (moves.UNREACHABLE, "orphan") in _checks(table)
    assert not any(c == moves.UNREACHABLE for c, _ in _checks(SOUND))


def test_reachability_starts_only_from_initial_states():
    assert (moves.UNREACHABLE, "new") in _checks(SOUND, initial=["failed"])


def test_a_non_terminal_dead_end_is_found():
    table = {**SOUND, ("run", "paused"): {"w"}}
    assert (moves.DEAD_END, "paused") in _checks(table)
    assert (moves.DEAD_END, "paused") not in _checks({**table, ("paused", "run"): {"resumed"}})
    assert (moves.DEAD_END, "failed") not in _checks(SOUND)


def test_a_terminal_that_moves_without_a_named_reopen_is_found():
    assert (moves.TERMINAL_EXIT, "done") in _checks(SOUND, reopens={})
    assert (moves.TERMINAL_EXIT, "done") not in _checks(SOUND)
    assert (moves.TERMINAL_EXIT, "done") not in _checks(SOUND, reopens=[("done", "new", "refetch")])


def test_a_reopen_must_carry_a_reason():
    assert (moves.UNNAMED_REOPEN, "done") in _checks(SOUND, reopens={("done", "new"): " "})
    assert (moves.UNNAMED_REOPEN, "done") not in _checks(SOUND)


def test_a_reopen_the_table_does_not_hold_is_stray():
    assert (moves.STRAY_REOPEN, "failed") in _checks(SOUND, reopens={("done", "new"): "r", ("failed", "new"): "r"})
    assert (moves.STRAY_REOPEN, "run") in _checks(SOUND, reopens={("done", "new"): "r", ("run", "done"): "r"})
    assert not any(c == moves.STRAY_REOPEN for c, _ in _checks(SOUND))


def test_a_declared_initial_or_terminal_state_the_table_never_names_is_found():
    got = _checks(SOUND, initial=[None, "born"], terminal=["done", "failed", "gone"])
    assert {(moves.ABSENT, "born"), (moves.ABSENT, "gone")} <= got
    assert not any(c == moves.ABSENT for c, _ in _checks(SOUND))


def test_a_state_the_code_uses_that_the_table_never_names_is_found():
    assert (moves.UNLISTED, "stale") in _checks(SOUND, states=["new", "stale"])
    assert not any(c == moves.UNLISTED for c, _ in _checks(SOUND, states=["new", "run", "done"]))


def test_reopens_by_reason_picks_the_moves_carrying_them():
    assert moves.reopens_by(SOUND, {"refetch", "recorded"}) == {("done", "new"): "refetch"}


def test_the_pytest_helper_fails_with_every_finding():
    good = MoveTable("app/a.py:MOVES", SOUND, **SOUND_ARGS)
    moves.assert_sound([good])
    bad = MoveTable("app/b.py:MOVES", {**SOUND, ("run", "paused"): {"w"}}, **SOUND_ARGS)
    with pytest.raises(AssertionError, match=r"app/b.py:MOVES: dead_end 'paused'"):
        moves.assert_sound([good, bad])


MODULE = '''
A, B = "a", "b"
MOVES = frozenset({(None, A), (A, B)})
JOB_MOVES: dict[tuple, frozenset] = {(A, B): frozenset({"x"}), **{(B, s): {"y"} for s in (A,)}}
TRANSITIONS = {"a": {"b"}, "b": set()}
_SWITCH_MOVES = {(A, B), (B, "c")}
MOVES_HELP = {(A, B)}
REMOVES = {(A, B)}
OTHER = {(A, B)}
STATES = (A, B)
FAMILY = {"a": 1}
TRANSITIONS_BY_NAME = {"a": 1}
PAGE_MOVES = 3
KNIGHT_MOVES = ("L", "x")
EMPTY_MOVES = set()
'''


def _trees(text, rel="app/m.py"):
    return {rel: ast.parse(text)}


def test_the_detector_finds_every_move_table_shape_and_nothing_else():
    found = {h.site for h in detectors.move_tables(_trees(MODULE))}
    assert found == {"app/m.py:MOVES", "app/m.py:JOB_MOVES", "app/m.py:TRANSITIONS", "app/m.py:_SWITCH_MOVES"}


def test_the_detector_reads_only_production_code():
    assert detectors.move_tables(_trees(MODULE, "tests/test_m.py")) == []


def test_unregistered_names_each_unbound_stale_or_bare_site():
    t = MoveTable("app/m.py:MOVES", SOUND, **SOUND_ARGS)
    assert moves.unregistered(["app/m.py:MOVES"], [t]) == []
    assert moves.unregistered(["app/m.py:MOVES", "app/n.py:MOVES"], [t]) == [
        "app/n.py:MOVES: a move table with no registry entry; register a MoveTable or waive it with a reason"]
    assert moves.unregistered(["app/m.py:MOVES", "app/n.py:MOVES"], [t], {"app/n.py:MOVES": "a lookup"}) == []
    assert moves.unregistered([], [t]) == ["app/m.py:MOVES: registered, but no move table is there"]
    assert moves.unregistered(["app/n.py:MOVES"], [], {"app/n.py:MOVES": ""}) == [
        "app/n.py:MOVES: waived with no reason"]
    assert moves.unregistered([], [], {"app/n.py:MOVES": "x"}) == ["app/n.py:MOVES: waived, but no move table is there"]
    assert moves.unregistered(["app/m.py:MOVES"], [t], {"app/m.py:MOVES": "x"}) == [
        "app/m.py:MOVES: both registered and waived"]


def test_the_ratchet_runs_both_move_checks_over_a_consumer(tmp_path):
    from gauntlet.registry import Registry
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text('MOVES = {("a", "b")}\n', encoding="utf-8")
    reg = Registry(root=tmp_path, index={}, snapshot_file=tmp_path / "s.json", defects_file=tmp_path / "d.json",
                   files=lambda root: sorted(root.rglob("*.py")))
    fns = ratchet.tests(reg)
    with pytest.raises(AssertionError, match="app/m.py:MOVES: a move table with no registry entry"):
        fns["test_gauntlet_every_move_table_is_registered"]()
    reg.moves = (MoveTable("app/m.py:MOVES", {("a", "b")}, initial=["a"], terminal=["a"]),)
    fns["test_gauntlet_every_move_table_is_registered"]()
    with pytest.raises(AssertionError, match=r"dead_end 'b'.*\n.*terminal_exit 'a'"):
        fns["test_gauntlet_every_move_table_is_sound"]()
