"""Front ends: the Python one reproduces detectors.run exactly, and the ratchet holds HCL findings like Python hits."""

import json
import subprocess

import pytest

from gauntlet import detectors, frontends, hcl, ratchet, skill
from gauntlet.registry import Registry

TABLE = 'FAMILY = {"a": 1, "b": 2, "c": 3}\ndef size(name):\n    return FAMILY.get(name, 0)\n'
DUP = 'NAMES = ("x", "y")\n'


def _write(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def test_python_front_end_finds_what_detectors_run_finds(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path, {"app/t.py": TABLE, "app/a.py": DUP, "app/b.py": DUP, "tests/test_x.py": DUP})
    index = {"closed": {"detector": "closed_tables"}, "dup": {"detector": "duplicated_literals"}}
    trees = detectors.parse(detectors.sources(tmp_path))
    want = detectors.run(trees, index)
    fe = frontends.get("python")
    assert fe.discover(tmp_path) == ["app/a.py", "app/b.py", "app/t.py"]
    got = fe.scan(tmp_path, fe.discover(tmp_path), index)
    assert {(f.cls, f.site, f.line, f.why) for f in got} == {(c, h.site, h.line, h.why) for c, hs in want.items()
                                                            for h in hs}
    assert {f.rule for f in got} == {"closed_tables", "duplicated_literals"} and len(got) == 3


def test_python_front_end_without_an_index_reports_under_detector_names(tmp_path):
    _write(tmp_path, {"t.py": TABLE})
    assert {f.cls for f in frontends.get("python").scan(tmp_path, ["t.py"])} == {"closed_tables"}


def test_an_unknown_front_end_is_refused():
    with pytest.raises(SystemExit):
        frontends.get("cobol")


SKILL_MD = """\
# The corpus

## Identity by name, not by content

**Trigger:** is something keyed by its position?

**Tier:** 2.

**Test shape:** reorder the list.
"""
CLS = "identity-by-name-not-by-content"
TF = ('resource "aws_eip" "b" {\n  count = 2\n  instance = data.aws_instances.web.ids[count.index]\n}\n')
BOUND = (f'import pytest\n@pytest.mark.gauntlet("{CLS}", site="infra/main.tf:aws_eip.b")\n'
         'def test_eip():\n    pass\n')


@pytest.fixture
def hcl_consumer(tmp_path):
    if not hcl.available():
        pytest.skip("python-hcl2 is not installed: gauntlet[hcl]")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    skill_dir = tmp_path / "skill"
    _write(tmp_path, {"skill/SKILL.md": SKILL_MD, "skill/general-corpus.md": "# General\n", "infra/main.tf": TF,
                      "tests/test_eip.py": BOUND,
                      "defects.json": json.dumps([{"number": 1, "title": "t", "created": "2026-01-01"}])})
    skill.write_snapshot(tmp_path / "snap.json", skill.read_skill(skill_dir))
    reg = Registry(root=tmp_path, snapshot_file=tmp_path / "snap.json", defects_file=tmp_path / "defects.json",
                   frontends=("python", "hcl"),
                   index={CLS: {"instances": [{"issue": 1}], "scanners": ["tests/test_eip.py::test_eip"],
                                "detector": "hcl_count_by_position"}})
    return reg, skill_dir


def _failing(reg, skill_dir):
    out = []
    for name, fn in ratchet.tests(reg, skill_dir=skill_dir).items():
        try:
            fn()
        except AssertionError as e:
            out.append((name, str(e)))
    return out


def test_a_bound_hcl_finding_passes_every_ratchet(hcl_consumer):
    assert _failing(*hcl_consumer) == []


def test_an_unbound_hcl_finding_fails_the_ratchet(hcl_consumer):
    reg, skill_dir = hcl_consumer
    (reg.root / "tests" / "test_eip.py").write_text("def test_eip():\n    pass\n", encoding="utf-8")
    failing = _failing(reg, skill_dir)
    assert [n for n, _ in failing] == ["test_gauntlet_no_new_code_trips_a_detector_without_a_bound_test"]
    assert "infra/main.tf:aws_eip.b" in failing[0][1]


def test_a_binding_goes_stale_when_the_hcl_is_fixed(hcl_consumer):
    reg, skill_dir = hcl_consumer
    (reg.root / "infra" / "main.tf").write_text('resource "aws_eip" "b" {\n  for_each = toset(["a"])\n}\n',
                                                encoding="utf-8")
    assert [n for n, _ in _failing(reg, skill_dir)] == ["test_gauntlet_no_binding_or_waiver_is_stale"]


def test_an_hcl_detector_name_is_unknown_unless_the_registry_names_the_front_end(hcl_consumer):
    reg, _ = hcl_consumer
    assert ratchet.unknown_detectors(reg) == []
    reg.frontends = ("python",)
    assert ratchet.unknown_detectors(reg) == [f"{CLS}: detector hcl_count_by_position does not exist"]
