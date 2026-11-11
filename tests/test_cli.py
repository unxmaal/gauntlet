"""The CLI: a consumer names its registry, and each verb reads it."""

import json
import subprocess
import sys

import pytest

from gauntlet import cli, registry

INDEX = {"one-question-two-answers": {"instances": [{"issue": 10}], "scanners": []}}


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "snap.json").write_text(json.dumps([{"id": "one-question-two-answers", "tier": 1}]),
                                        encoding="utf-8")
    (tmp_path / "defects.json").write_text(json.dumps([{"number": 10, "title": "a", "created": "x"}]),
                                           encoding="utf-8")
    (tmp_path / "consumer_reg.py").write_text(
        "from pathlib import Path\nfrom gauntlet.registry import Registry\nHERE = Path(__file__).parent\n"
        f"R = Registry(root=HERE, index={INDEX!r}, snapshot_file=HERE / 'snap.json',\n"
        "             defects_file=HERE / 'defects.json', repo_slug='owner/repo')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    sys.modules.pop("consumer_reg", None)


def test_audit_offline_reads_the_registry_s_snapshot(consumer, capsys):
    assert cli.main(["audit", "--registry", "consumer_reg:R", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "closed defects with no class: 0" in out and "not in this clone" in out


def test_audit_fails_on_a_defect_with_no_class(consumer, capsys):
    (consumer / "defects.json").write_text(json.dumps([{"number": 10, "title": "a", "created": "x"},
                                                       {"number": 11, "title": "b", "created": "y"}]),
                                           encoding="utf-8")
    assert cli.main(["audit", "--registry", "consumer_reg:R", "--offline"]) == 1
    assert "#11 has no class" in capsys.readouterr().out


def test_the_registry_may_come_from_the_environment(consumer, capsys, monkeypatch):
    monkeypatch.setenv(cli.REGISTRY_ENV, "consumer_reg:R")
    assert cli.main(["audit", "--offline"]) == 0
    monkeypatch.delenv(cli.REGISTRY_ENV)
    with pytest.raises(SystemExit, match="name the registry"):
        cli.main(["audit", "--offline"])
    with pytest.raises(SystemExit, match="package.module:ATTR"):
        cli.load_object("no_colon")


def test_snapshot_skill_writes_the_pinned_corpus_into_the_registry(consumer, capsys):
    assert cli.main(["snapshot-skill", "--registry", "consumer_reg:R"]) == 0
    got = json.loads((consumer / "snap.json").read_text(encoding="utf-8"))
    assert len(got) >= 40 and "classes -> snap.json" in capsys.readouterr().out


def test_snapshot_defects_asks_gh_for_the_registry_s_repo(consumer, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(registry, "_gh", lambda argv: seen.append(argv) or [
        {"number": 7, "title": "t", "createdAt": "z"}])
    assert cli.main(["snapshot-defects", "--registry", "consumer_reg:R"]) == 0
    assert "owner/repo" in seen[0]
    assert json.loads((consumer / "defects.json").read_text(encoding="utf-8")) == [
        {"number": 7, "title": "t", "created": "z"}]


def test_classes_and_corpus_dir_need_no_registry(capsys):
    assert cli.main(["classes"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) >= 40 and all(line[0] in "123" for line in lines)
    assert cli.main(["corpus-dir"]) == 0
    assert capsys.readouterr().out.strip().endswith("corpus")


def test_mutation_needs_a_config(capsys):
    with pytest.raises(SystemExit, match="--config"):
        cli.main(["mutation", "run"])


def test_python_m_gauntlet_runs():
    out = subprocess.run([sys.executable, "-m", "gauntlet", "classes"], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert "one-question-two-answers" in out.stdout
