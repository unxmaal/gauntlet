"""The standard inventory format from shapes.md: a named file, a header, one ranked table."""

from gauntlet import cli, inventory

GOOD = """\
# SM inventory

- Repo: example
- Commit: 1a2b3c4
- Date: 2026-11-11
- Question: SM
- Scope: src/
- Enumerate: AST walk over UPDATE statements

| Sev | Id | Entity | Sites | Shape | Unhandled | Bitten | Scannable |
|---|---|---|---|---|---|---|---|
| high | SM-1 | job | a.py:3 (2 writers) | text column | crash between writes | #12 | unguarded_transition |
| medium | SM-2 | proposal | b.py:9 | enum | none known | none | tier 2: free text |
| low | SM-3 | label | c.py:1 | string | display only | none | none |

Properly done: run (one writer, guarded).
"""


def test_the_shape_questions_are_read_from_the_corpus():
    assert {"SM", "VS", "FT", "OR"} <= inventory.questions()


def test_a_well_formed_inventory_passes():
    assert inventory.check(GOOD, "sm-1a2b3c4.md", inventory.questions()) == []


def test_each_departure_from_the_format_is_named():
    known = inventory.questions()
    assert any("want <question-id>" in p for p in inventory.check(GOOD, "notes.md", known))
    assert any("ZZ is not a question" in p for p in inventory.check(GOOD, "zz-1a2b3c4.md", known))
    assert any("names no scope" in p for p in inventory.check(GOOD.replace("- Scope: src/\n", ""), "sm-1a2b3c4.md", known))
    swapped = GOOD.replace("| Sev | Id |", "| Id | Sev |")
    assert any("columns" in p for p in inventory.check(swapped, "sm-1a2b3c4.md", known))
    assert any("severity 'urgent'" in p for p in inventory.check(GOOD.replace("| high |", "| urgent |"), "sm-1a2b3c4.md", known))
    assert any("Bitten is empty" in p for p in inventory.check(GOOD.replace("| #12 |", "|  |"), "sm-1a2b3c4.md", known))
    assert any("does not start with a question id" in p
               for p in inventory.check(GOOD.replace("SM-2", "XX-2"), "sm-1a2b3c4.md", known))
    assert any("8 cells" not in p and "cells" in p
               for p in inventory.check(GOOD.replace("| none | tier 2: free text |", "| tier 2 |"), "sm-1a2b3c4.md", known))
    assert any("no table" in p for p in inventory.check(GOOD.split("| Sev")[0], "sm-1a2b3c4.md", known))


def test_rows_are_ranked_high_then_medium_then_low():
    lines = GOOD.splitlines()
    i = next(n for n, line in enumerate(lines) if "SM-1" in line)
    lines[i], lines[i + 2] = lines[i + 2], lines[i]
    got = inventory.check("\n".join(lines) + "\n", "sm-1a2b3c4.md", inventory.questions())
    assert any("ranked below a lower severity" in p for p in got)


def test_the_cli_checks_files(tmp_path, capsys):
    good, bad = tmp_path / "sm-1a2b3c4.md", tmp_path / "sm-1a2b3c5.md"
    good.write_text(GOOD, encoding="utf-8")
    bad.write_text(GOOD.replace("| low |", "| trivial |"), encoding="utf-8")
    assert cli.main(["inventory", str(good)]) == 0
    assert cli.main(["inventory", str(good), str(bad)]) == 1
    assert "trivial" in capsys.readouterr().out
