"""The ratchets a consumer's test suite runs over its registry; tests(reg) returns them as pytest test functions."""

import re

from gauntlet import detectors, frontends, moves, registry, review, skill

_ORDINARY = ["import os", "from pkg import paths", "def f(a, b=None):", "    return x", "    if not rows:",
             "for r in rows:", "    out.append(r)", "class Thing:", "    \"\"\"One line of prose.\"\"\"",
             "name = row[\"name\"]", "    conn.close()", "with open(p, encoding=\"utf-8\") as f:",
             "    print(msg, file=sys.stderr)", "    candidate = spec", "rows = conn.execute(q).fetchall()",
             "x = 1"]


def registry_problems(reg):
    return registry.problems(reg.index, reg.pending, reg.unclassified, reg.snapshot(), reg.defects(), reg.root)


def snapshot_drift(reg, skill_dir=None):
    if skill.read_skill(skill_dir) == reg.snapshot():
        return []
    return ["the skill changed: run `gauntlet snapshot-skill --registry ...` and rebind"]


def duplicate_ids(reg):
    ids = [c["id"] for c in reg.snapshot()]
    out = sorted({i for i in ids if ids.count(i) > 1})
    return [f"{i}: two classes share this id" for i in out] + [
        f"{i}: pending, but the snapshot defines it" for i in sorted(set(reg.pending) & set(ids))]


def unknown_detectors(reg, extra=None):
    known = {*frontends.detector_names(reg.frontends), *detectors.DETECTORS, *(extra or {})}
    out = [f"{cid}: detector {name} does not exist" for cid, e in reg.index.items()
           for name in detectors.names(e) if name not in known]
    return out + [f"detector_config names {name}, which does not exist" for name in sorted(reg.detector_config)
                  if name not in known]


def detector_config(reg):
    """The registry's detector_config, with its move tables handed to terminal_without_reader unless it says otherwise."""
    config = {name: dict(kw) for name, kw in reg.detector_config.items()}
    if reg.moves:
        config.setdefault("terminal_without_reader", {}).setdefault("tables", reg.moves)
    return config


def review_gaps(reg):
    snap = {c["id"]: c for c in reg.snapshot()}
    out = [f"{cid}: a review-only class needs a `review` regex" for cid, e in reg.index.items()
           if review.review_only(cid, e, snap.get(cid, {})) and not e.get("review")]
    for cid, e in reg.index.items():
        if e.get("review"):
            rx = re.compile(e["review"])
            out += [f"{cid}: its review heuristic fires on an ordinary line: {line!r}" for line in _ORDINARY
                    if rx.search(line)]
    return out


def frontend_hits(reg, extra=None):
    """{class: [Hit]} from every front end the registry names, each class seeded so a silent detector still counts."""
    config = detector_config(reg)
    out = {}
    for name in reg.frontends:
        fe = frontends.get(name, reg.production, extra)
        known = fe.detectors()
        for cid, e in reg.index.items():
            if known & set(detectors.names(e)):
                out.setdefault(cid, {})
        for f in fe.scan(reg.root, fe.discover(reg.root, lister=reg.files), reg.index, config):
            out.setdefault(f.cls, {}).setdefault(f.site, detectors.Hit(f.site, f.line, f.why))
    return {cid: sorted(hits.values()) for cid, hits in out.items()}


def tree_ledger(reg, extra=None):
    kw = {"production": reg.production, "is_test": reg.tests, "lister": reg.files}
    hits = frontend_hits(reg, extra)
    marks = detectors.bindings(detectors.sources(reg.root, tests=True, **kw), reg.tests)
    return detectors.ledger(hits, marks, reg.index), marks


def unbound(reg, led):
    backlog = reg.backlog()
    new = [(cid, s) for cid, row in led.items() for s in row["unbound"] if s not in backlog.get(cid, [])]
    return [f'{cid}: {s}\n    bind a test with @pytest.mark.gauntlet("{cid}", site="{s}") or waive it with a reason'
            for cid, s in new]


def backlog_problems(reg, led):
    backlog = reg.backlog()
    out = [f"{cid}: {s} is bound, waived or gone: remove it from the backlog" for cid, rows in backlog.items()
           for s in rows if s not in led.get(cid, {}).get("unbound", [])]
    total = sum(len(v) for v in backlog.values())
    if reg.backlog_pin is not None and total > reg.backlog_pin:
        out.append(f"the backlog grew to {total}, pinned at {reg.backlog_pin}")
    elif reg.backlog_pin is not None and total < reg.backlog_pin:
        out.append(f"the backlog shrank to {total}: lower backlog_pin to {total}")
    return out


def stale(reg, led, marks):
    out = [f"{cid}: binding {s}" for cid, row in led.items() for s in row["stale_bindings"]]
    out += [f"{cid}: waiver {s}" for cid, row in led.items() for s in row["stale_waivers"]]
    detected = {cid for cid, e in reg.index.items() if "detector" in e}
    out += [f"{cid}: binding {s} for a class with no detector" for cid, s in marks if cid not in detected]
    return out


def move_sites(reg):
    kw = {"production": reg.production, "is_test": reg.tests, "lister": reg.files}
    trees = detectors.parse(detectors.sources(reg.root, **kw))
    return sorted({h.site for h in detectors.move_tables(trees, reg.production)})


def move_registration(reg):
    return moves.unregistered(move_sites(reg), reg.moves, reg.move_waivers)


def tests(reg, extra_detectors=None, skill_dir=None):
    """Test functions for a consumer's module: globals().update(gauntlet.ratchet.tests(REGISTRY))."""
    cache = {}

    def ledger():
        if "led" not in cache:
            cache["led"] = tree_ledger(reg, extra_detectors)
        return cache["led"]

    def test_gauntlet_every_move_table_is_sound():
        moves.assert_sound(reg.moves)

    def test_gauntlet_every_move_table_is_registered():
        fail_on(move_registration(reg))

    def fail_on(problems):
        assert not problems, "\n  ".join(["gauntlet:"] + problems)

    def test_gauntlet_registry_is_clean():
        fail_on(registry_problems(reg))

    def test_gauntlet_snapshot_is_the_pinned_corpus():
        fail_on(snapshot_drift(reg, skill_dir))

    def test_gauntlet_ids_are_unique():
        fail_on(duplicate_ids(reg))

    def test_gauntlet_every_named_detector_exists():
        fail_on(unknown_detectors(reg, extra_detectors))

    def test_gauntlet_review_heuristics_are_present_and_quiet():
        fail_on(review_gaps(reg))

    def test_gauntlet_no_new_code_trips_a_detector_without_a_bound_test():
        fail_on(unbound(reg, ledger()[0]))

    def test_gauntlet_the_backlog_only_shrinks():
        fail_on(backlog_problems(reg, ledger()[0]))

    def test_gauntlet_no_binding_or_waiver_is_stale():
        fail_on(stale(reg, *ledger()))

    return {name: fn for name, fn in locals().items() if name.startswith("test_gauntlet_")}
