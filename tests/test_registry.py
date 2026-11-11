"""The registry binds a repo's defects to the skill's classes, and the skill parser reads the corpus."""

import os
import re
from pathlib import Path

import pytest

from gauntlet import registry, skill

REPO = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

SKILL_MD = """\
# The gauntlet

## How to run it

No tier here, so this is not a class.

# The corpus

## A fact about the harness recorded as a verdict on the subject

**Tier:** 2, plus a scanner for the classifier's location (tier 1).

## A platform primitive assumed universal

**Tier:** 1. Nearly all of these are scannable.

# Adding an entry

**Tier**. not a class either, the heading is a top-level one.
"""

GENERAL_MD = """\
# The general corpus

## Provenance rule (read this first)

## 10. Encoding assumed

**Tier:** 1 in Python (`open`/`read_text` with no `encoding=`).
"""


def fixture_snapshot():
    return skill.parse_skill(SKILL_MD, "SKILL.md") + skill.parse_skill(GENERAL_MD, "general-corpus.md")


def fixture_defects():
    return {1: {"title": "a", "created": "2026-09-01T00:00:00Z"},
            2: {"title": "b", "created": "2026-09-02T00:00:00Z"},
            3: {"title": "c", "created": "2026-09-03T00:00:00Z"}}


def fixture_registry():
    index = {
        "a-fact-about-the-harness-recorded-as-a-verdict-on-the-subject": {
            "instances": [{"issue": 1}, {"rule": 270}], "scanners": []},
        "a-platform-primitive-assumed-universal": {
            "instances": [{"issue": 2}],
            "scanners": ["tests/test_registry.py::test_a_heading_becomes_a_stable_kebab_id"]},
    }
    pending = {"guard-built-never-invoked": {"tier": 2, "instances": [{"issue": 3}]}}
    return index, pending, {}


def check(index, pending, unclassified, snapshot=None, defects=None):
    return registry.problems(index, pending, unclassified,
                         fixture_snapshot() if snapshot is None else snapshot,
                         fixture_defects() if defects is None else defects, REPO)


def test_a_heading_becomes_a_stable_kebab_id():
    assert skill.slug("A part's cost reported as the whole's") == "a-part-s-cost-reported-as-the-whole-s"
    assert skill.slug("10. Encoding assumed") == "encoding-assumed"
    assert skill.slug("Silent truncation, or a partial read assumed complete") == \
        "silent-truncation-or-a-partial-read-assumed-complete"


def test_only_headings_with_a_tier_are_classes():
    snap = fixture_snapshot()
    assert [c["id"] for c in snap] == [
        "a-fact-about-the-harness-recorded-as-a-verdict-on-the-subject",
        "a-platform-primitive-assumed-universal",
        "encoding-assumed"]
    assert [c["tier"] for c in snap] == [2, 1, 1]
    assert snap[2]["source"] == "general-corpus.md"


def test_the_skill_s_bite_count_is_read_as_an_integer():
    text = "# The corpus\n\n## A class\n\n**Bitten:** 10+ in two days.\n\n**Tier:** 2.\n\n## Another\n\n**Tier:** 1.\n"
    snap = skill.parse_skill(text, "SKILL.md")
    assert [c["bitten"] for c in snap] == [10, None]




def test_the_fixture_registry_is_clean():
    assert check(*fixture_registry()) == []


def test_a_class_the_skill_does_not_define_is_refused():
    index, pending, unc = fixture_registry()
    index["made-up-class"] = {"instances": [{"issue": 1}], "scanners": []}
    assert any("made-up-class" in p and "not in the skill" in p for p in check(index, pending, unc))


def test_a_pending_class_the_skill_now_defines_must_move_into_the_index():
    index, pending, unc = fixture_registry()
    pending["encoding-assumed"] = {"tier": 1, "instances": [{"issue": 3}]}
    assert any("encoding-assumed" in p and "move it" in p for p in check(index, pending, unc))


@pytest.mark.parametrize("entry, needle", [
    ({"instances": [{"issue": 1}]}, "missing scanners"),
    ({"scanners": []}, "missing instances"),
    ({"instances": [], "scanners": []}, "no instance"),
    ({"instances": [{"pr": 1}], "scanners": []}, "bad instance"),
    ({"instances": [{"issue": "1"}], "scanners": []}, "bad instance"),
])
def test_an_entry_missing_a_field_is_refused(entry, needle):
    index, pending, unc = fixture_registry()
    index["a-platform-primitive-assumed-universal"] = entry
    assert any(needle in p for p in check(index, pending, unc)), check(index, pending, unc)


def test_a_pending_entry_needs_a_tier():
    index, pending, unc = fixture_registry()
    pending["guard-built-never-invoked"] = {"instances": [{"issue": 3}]}
    assert any("missing tier" in p for p in check(index, pending, unc))


def test_an_instance_issue_must_be_in_the_snapshot():
    index, pending, unc = fixture_registry()
    index["a-platform-primitive-assumed-universal"]["instances"].append({"issue": 999})
    assert any("#999" in p and "snapshot" in p for p in check(index, pending, unc))


def test_every_snapshot_defect_is_bound_or_explained():
    index, pending, unc = fixture_registry()
    del pending["guard-built-never-invoked"]
    assert any("#3" in p and "no class" in p for p in check(index, pending, unc))
    assert check(index, pending, {3: "upstream model behaviour"}) == []
    assert any("#3" in p and "reason" in p for p in check(index, pending, {3: ""}))


def test_an_issue_cannot_be_both_classified_and_unclassified():
    index, pending, unc = fixture_registry()
    assert any("#1" in p and "both" in p for p in check(index, pending, {1: "why"}))


def test_a_scanner_that_does_not_exist_is_refused():
    index, pending, unc = fixture_registry()
    index["a-platform-primitive-assumed-universal"]["scanners"] = [
        "tests/test_registry.py::test_that_was_never_written"]
    assert any("test_that_was_never_written" in p for p in check(index, pending, unc))
    index["a-platform-primitive-assumed-universal"]["scanners"] = ["tests/no_such_file.py::test_x"]
    assert any("no_such_file" in p for p in check(index, pending, unc))


def test_a_tier_1_class_with_no_scanner_yet_is_a_listed_gap_not_a_failure():
    index, pending, unc = fixture_registry()
    index["a-platform-primitive-assumed-universal"]["scanners"] = []
    assert check(index, pending, unc) == []
    assert registry.unscanned(index, fixture_snapshot()) == ["a-platform-primitive-assumed-universal"]
    index["a-platform-primitive-assumed-universal"]["scanners"] = [
        "tests/test_registry.py::test_a_heading_becomes_a_stable_kebab_id"]
    assert registry.unscanned(index, fixture_snapshot()) == []


def test_a_tier_1_class_judged_unscannable_here_is_rebound_to_tier_2_with_a_reason():
    index, pending, unc = fixture_registry()
    index["a-platform-primitive-assumed-universal"]["scanners"] = []
    index["a-platform-primitive-assumed-universal"]["tier"] = 2
    assert any("tier_reason" in p for p in check(index, pending, unc))
    index["a-platform-primitive-assumed-universal"]["tier_reason"] = "only a proxy is scannable"
    assert check(index, pending, unc) == []
    assert registry.unscanned(index, fixture_snapshot()) == []
    assert registry.stats(index, pending, fixture_snapshot(), fixture_defects())["classes_per_tier"] == {2: 3}






def test_read_skill_parses_both_files(tmp_path):
    (tmp_path / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (tmp_path / "general-corpus.md").write_text(GENERAL_MD, encoding="utf-8")
    assert skill.read_skill(tmp_path) == fixture_snapshot()


def test_the_skill_location_can_be_overridden_and_defaults_to_the_pinned_corpus(monkeypatch, tmp_path):
    monkeypatch.setenv("GAUNTLET_SKILL_DIR", str(tmp_path))
    assert skill.skill_dir() == tmp_path
    monkeypatch.delenv("GAUNTLET_SKILL_DIR")
    assert skill.skill_dir() == skill.corpus_dir()
    assert (skill.corpus_dir() / "SKILL.md").is_file()


def test_parse_skill_reads_the_corpus_in_this_repo(monkeypatch):
    monkeypatch.delenv("GAUNTLET_SKILL_DIR", raising=False)
    got = skill.read_skill(REPO / "corpus")
    assert got == skill.read_skill()
    assert len(got) >= 40
    assert {c["source"] for c in got} == {"SKILL.md", "general-corpus.md"}
    assert len({c["id"] for c in got}) == len(got), "two classes in the corpus share an id"
    assert all(c["tier"] in (1, 2, 3) for c in got)
    assert all(c["trigger"] for c in got if c["source"] == "SKILL.md"), "a SKILL.md class has no trigger"


def test_the_corpus_is_ordered_by_bite_count_and_names_only_detectors_that_exist():
    from gauntlet import detectors
    classes = [c for c in skill.read_skill(REPO / "corpus") if c["source"] == "SKILL.md"]
    counts = [c["bitten"] for c in classes]
    assert None not in counts, "a SKILL.md class has no bare-integer Bitten"
    assert [c["id"] for c in classes] == [c["id"] for c in sorted(classes, key=lambda c: -c["bitten"])]
    text = (REPO / "corpus" / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"[Dd]etectors?:?\s*`(\w+)`", text)) | set(re.findall(r";\s*`(\w+)`, a", text))
    assert named and named <= set(detectors.DETECTORS), named - set(detectors.DETECTORS)
