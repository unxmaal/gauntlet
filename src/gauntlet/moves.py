"""Checks over a declared table of legal moves: every state reachable, no dead end, terminals stay put, no stray names."""

from collections import namedtuple
from collections.abc import Mapping
from dataclasses import dataclass

Finding = namedtuple("Finding", "check state detail")

UNREACHABLE = "unreachable"
DEAD_END = "dead_end"
TERMINAL_EXIT = "terminal_exit"
ABSENT = "absent"
UNLISTED = "unlisted"
STRAY_REOPEN = "stray_reopen"
UNNAMED_REOPEN = "unnamed_reopen"


def _pair(key):
    return isinstance(key, tuple) and len(key) == 2


def normalize(moves):
    """{state: frozenset(next)} from {state: next-states}, {(leaves, enters): anything}, or (leaves, enters) pairs."""
    out = {}
    if isinstance(moves, Mapping) and moves and not all(_pair(k) for k in moves):
        for frm, nexts in moves.items():
            if isinstance(nexts, (str, bytes)) or nexts is None:
                raise TypeError(f"{frm!r}: next states must be a collection, got {nexts!r}")
            out.setdefault(frm, set()).update(nexts)
            for to in nexts:
                out.setdefault(to, set())
    else:
        for key in moves:
            if not _pair(key):
                raise TypeError(f"{key!r} is not a (leaves, enters) pair")
            frm, to = key
            out.setdefault(frm, set()).add(to)
            out.setdefault(to, set())
    return {s: frozenset(n) for s, n in out.items()}


def merge(*tables):
    """One table from several: per-writer tables of the same lifecycle are checked as their union."""
    out = {}
    for t in tables:
        for s, n in normalize(t).items():
            out.setdefault(s, set()).update(n)
    return {s: frozenset(n) for s, n in out.items()}


def reopens_by(moves, reasons):
    """{(leaves, enters): reason} for each move in a {(leaves, enters): reasons} table carrying one of `reasons`."""
    want = set(reasons)
    out = {}
    for key, carried in moves.items():
        hit = sorted(want & set(carried))
        if hit:
            out[key] = ", ".join(hit)
    return out


def _reopens(reopens):
    if isinstance(reopens, Mapping):
        return dict(reopens)
    out = {}
    for row in reopens:
        if not (isinstance(row, tuple) and len(row) == 3):
            raise TypeError(f"{row!r}: a reopen is (leaves, enters, reason)")
        out[(row[0], row[1])] = row[2]
    return out


def _key(s):
    return (s is not None, str(s))


def check(table, initial, terminal, reopens=(), states=()):
    """Findings for a move table: `initial` are the states a row is born in (or the pre-birth state the table names),
    `terminal` the states nothing leaves, `reopens` the named moves out of a terminal ({(leaves, enters): reason} or
    (leaves, enters, reason) rows), `states` every state the code writes or reads."""
    table = normalize(table)
    initial, terminal, reopens = set(initial), set(terminal), _reopens(reopens)
    out = []
    for s in sorted(initial | terminal | set(states), key=_key):
        if s in table:
            continue
        role = "initial" if s in initial else "terminal" if s in terminal else "used"
        out.append(Finding(ABSENT if role != "used" else UNLISTED, s, f"declared {role} but no move names it"))
    seen, todo = set(), [s for s in initial if s in table]
    while todo:
        s = todo.pop()
        if s not in seen:
            seen.add(s)
            todo.extend(table[s])
    for s in sorted(set(table) - seen, key=_key):
        out.append(Finding(UNREACHABLE, s, "no path from an initial state reaches it"))
    for s in sorted(table, key=_key):
        if s not in terminal and not table[s]:
            out.append(Finding(DEAD_END, s, "not terminal, yet no move leaves it"))
    for s in sorted(terminal & set(table), key=_key):
        for to in sorted(table[s], key=_key):
            why = reopens.get((s, to))
            if why is None:
                out.append(Finding(TERMINAL_EXIT, s, f"terminal, yet it moves to {to!r} and no reopen names that"))
    for (frm, to), why in sorted(reopens.items(), key=lambda kv: (_key(kv[0][0]), _key(kv[0][1]))):
        if frm not in terminal or to not in table.get(frm, ()):
            out.append(Finding(STRAY_REOPEN, frm, f"reopen to {to!r} is not a move out of a terminal state"))
        elif not str(why or "").strip():
            out.append(Finding(UNNAMED_REOPEN, frm, f"reopen to {to!r} carries no reason"))
    return out


@dataclass
class MoveTable:
    """One declared lifecycle a consumer registers: where its table lives (`path:NAME`) and what the check needs."""
    site: str
    moves: object
    initial: tuple
    terminal: tuple = ()
    reopens: object = ()
    states: tuple = ()

    def findings(self):
        return check(self.moves, self.initial, self.terminal, self.reopens, self.states)


def problems(tables):
    return [f"{t.site}: {f.check} {f.state!r}: {f.detail}" for t in tables for f in t.findings()]


def assert_sound(tables):
    """The pytest helper: fails with every finding of every registered table."""
    found = problems(tables)
    assert not found, "\n  ".join(["gauntlet moves:"] + found)


def unregistered(sites, tables, waivers=None):
    """Move-table sites the detector finds that nothing registers or waives, and registrations it no longer finds."""
    sites, waivers = set(sites), dict(waivers or {})
    registered = {t.site for t in tables}
    out = [f"{s}: a move table with no registry entry; register a MoveTable or waive it with a reason"
           for s in sorted(sites - registered - set(waivers))]
    out += [f"{s}: registered, but no move table is there" for s in sorted(registered - sites)]
    out += [f"{s}: waived, but no move table is there" for s in sorted(set(waivers) - sites)]
    out += [f"{s}: waived with no reason" for s, why in sorted(waivers.items()) if not str(why).strip()]
    out += [f"{s}: both registered and waived" for s in sorted(registered & set(waivers))]
    return out
