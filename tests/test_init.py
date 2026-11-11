"""`gauntlet init`: the repo it wires passes its own ratchets on the first run, and only a new hit fails them."""

import importlib.util
import json
import re
import subprocess
import sys
import tomllib

import pytest

from gauntlet import __version__, cli, detectors, hcl, init, registry, skill

TABLE = 'FAMILY = {"qwen": 1, "llama": 2, "gemma": 3}\ndef size(name):\n    return FAMILY.get(name, 0)\n'
OTHER = 'COLOR = {"red": 1, "green": 2, "blue": 3}\ndef shade(name):\n    return COLOR.get(name, 0)\n'
MOVES = 'A = "a"\nMOVES = frozenset({(None, A), (A, "b")})\n'
PYPROJECT = ('[project]\nname = "sample-app"\nversion = "0.1.0"\nrequires-python = "{rp}"\ndependencies = []\n\n'
             '[dependency-groups]\ndev = [\n    "pytest>=8",\n]\n')
DB = 'resource "aws_db_instance" "main" {\n  identifier = "sample-db"\n  skip_final_snapshot = true\n}\n'
needs_hcl = pytest.mark.skipif(not hcl.available(), reason="python-hcl2 is not installed: gauntlet[hcl]")


def _repo(root, files):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def pyrepo(tmp_path):
    return _repo(tmp_path / "sample", {"pyproject.toml": PYPROJECT.format(rp=">=3.11"), "app/t.py": TABLE})


def _ratchets(root):
    # The generated test module, imported by path as pytest would, and its test functions.
    name = f"generated_{abs(hash(str(root)))}"
    spec = importlib.util.spec_from_file_location(name, root / init.TEST_REL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {n: f for n, f in vars(mod).items() if n.startswith("test_gauntlet_")}


def _failing(root):
    out = {}
    for name, fn in _ratchets(root).items():
        try:
            fn()
        except AssertionError as e:
            out[name] = str(e)
    return out


def test_init_wires_a_python_repo_whose_generated_tests_pass_under_pytest(pyrepo, capsys):
    assert cli.main(["init", "--root", str(pyrepo)]) == 0
    out = capsys.readouterr().out
    for rel in ("pyproject.toml", init.REGISTRY_REL, init.TEST_REL, init.SNAPSHOT_REL, init.DEFECTS_REL,
                init.BACKLOG_REL):
        assert f"wrote {rel}\n" in out
    todos = (pyrepo / init.REGISTRY_REL).read_text(encoding="utf-8").count(init.TODO)
    assert f"TODO lines: {todos} in {init.REGISTRY_REL}" in out and todos >= 2
    assert "backlog: 1 hit(s) seeded" in out
    assert json.loads((pyrepo / init.BACKLOG_REL).read_text()) == {
        "a-closed-table-fronting-an-open-set": ["app/t.py:FAMILY"]}
    assert json.loads((pyrepo / init.DEFECTS_REL).read_text()) == []
    assert skill.load_snapshot(pyrepo / init.SNAPSHOT_REL) == skill.read_skill(skill.corpus_dir())
    dev = tomllib.loads((pyrepo / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    assert dev == [f"gauntlet @ {init.URL}@v{__version__} ; python_version >= '3.12'", "pytest>=8"]
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", init.TEST_REL],
                         cwd=pyrepo, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "10 passed" in run.stdout


def test_a_second_init_refuses_and_force_rewrites(pyrepo):
    init.run(pyrepo)
    reg = pyrepo / init.REGISTRY_REL
    reg.write_text("edited by hand\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="refuses to overwrite"):
        init.run(pyrepo)
    assert reg.read_text() == "edited by hand\n"
    init.run(pyrepo, force=True)
    assert "REGISTRY = Registry(" in reg.read_text()
    assert _failing(pyrepo) == {}
    assert tomllib.loads((pyrepo / "pyproject.toml").read_text())["dependency-groups"]["dev"].count(
        f"gauntlet @ {init.URL}@v{__version__} ; python_version >= '3.12'") == 1


def test_only_a_new_hit_after_init_fails_the_ratchets(pyrepo):
    init.run(pyrepo)
    assert _failing(pyrepo) == {}
    (pyrepo / "app" / "u.py").write_text(OTHER, encoding="utf-8")
    failed = _failing(pyrepo)
    assert list(failed) == ["test_gauntlet_no_new_code_trips_a_detector_without_a_bound_test"]
    assert "app/u.py:COLOR" in failed["test_gauntlet_no_new_code_trips_a_detector_without_a_bound_test"]


def test_a_hit_fixed_after_init_asks_for_the_backlog_to_shrink(pyrepo):
    init.run(pyrepo)
    (pyrepo / "app" / "t.py").write_text("def size(name):\n    return 0\n", encoding="utf-8")
    assert list(_failing(pyrepo)) == ["test_gauntlet_the_backlog_only_shrinks"]


def test_an_unregistered_move_table_is_waived_with_a_todo_so_the_first_run_passes(pyrepo, capsys):
    (pyrepo / "app" / "state.py").write_text(MOVES, encoding="utf-8")
    init.run(pyrepo)
    text = (pyrepo / init.REGISTRY_REL).read_text()
    assert f"{init.TODO}move table app/state.py:MOVES" in text
    assert "'app/state.py:MOVES': " in text
    assert _failing(pyrepo) == {}


def test_every_todo_is_one_comment_line(pyrepo):
    init.run(pyrepo)
    lines = (pyrepo / init.REGISTRY_REL).read_text().splitlines()
    todos = [i for i, line in enumerate(lines) if line.startswith(init.TODO)]
    assert todos and all(not lines[i + 1].startswith("#   ") for i in todos)
    assert any("production" in lines[i] for i in todos)
    assert any("unscoped_query" in lines[i] for i in todos)


@needs_hcl
def test_a_python_repo_with_terraform_gets_the_hcl_front_end(tmp_path):
    root = _repo(tmp_path / "mixed", {"pyproject.toml": PYPROJECT.format(rp=">=3.12"), "app/t.py": TABLE,
                                      "infra/db.tf": DB})
    init.run(root)
    reg = _load_registry(root)
    assert reg.frontends == ("python", "hcl")
    assert reg.index["irreplaceable-state-with-no-guard-on-its-destruction"]["detector"] == "hcl_stateful_unprotected"
    assert reg.index["one-question-two-answers"]["detector"] == ["duplicated_literals", "hcl_duplicated_literal"]
    assert any(s.startswith("infra/db.tf:") for s in reg.backlog()["irreplaceable-state-with-no-guard-on-its-destruction"])
    dev = tomllib.loads((root / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    assert f"gauntlet[hcl] @ {init.URL}@v{__version__}" in dev
    assert _failing(root) == {}


@needs_hcl
def test_a_terraform_only_repo_gets_a_baseline_and_a_ci_check(tmp_path, capsys):
    root = _repo(tmp_path / "infra", {"db.tf": DB})
    assert cli.main(["init", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert f"wrote {init.BASELINE_REL}" in out and f"wrote {init.CHECK_REL}" in out and "Terraform only" in out
    assert "--baseline .gauntlet/baseline.json" in (root / init.CHECK_REL).read_text()
    assert not (root / init.REGISTRY_REL).exists()
    scan = ["scan", str(root), "--frontend", "hcl", "--baseline", str(root / init.BASELINE_REL)]
    assert cli.main(scan) == 0
    (root / "cache.tf").write_text(DB.replace('"main"', '"cache"').replace("sample-db", "sample-cache"))
    capsys.readouterr()
    assert cli.main(scan) == 1
    assert "cache.tf:aws_db_instance.cache" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="refuses to overwrite"):
        init.run(root)


def _load_registry(root):
    spec = importlib.util.spec_from_file_location(f"reg_{abs(hash(str(root)))}", root / init.REGISTRY_REL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.REGISTRY


@pytest.mark.parametrize("spec, below", [
    (">=3.11", True), (">=3.11,<3.14", True), (">3.11", True), ("~=3.10", True), ("==3.11.*", True),
    ("<3.13", True), (None, True), ("", True),
    (">=3.12", False), (">=3.12,<3.15", False), ("~=3.13", False), (">=3.13", False), ("==3.12.*", False),
])
def test_the_python_marker_is_added_only_when_requires_python_admits_older_than_312(spec, below):
    assert init.admits_below_312(spec) is below


def test_the_marker_is_left_out_when_the_repo_already_needs_312(tmp_path):
    root = _repo(tmp_path / "new", {"pyproject.toml": PYPROJECT.format(rp=">=3.12"), "app/t.py": TABLE})
    init.run(root, pin="v9.9.9")
    dev = tomllib.loads((root / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    assert dev[0] == f"gauntlet @ {init.URL}@v9.9.9"
    assert "importorskip" not in (root / init.TEST_REL).read_text()


REQ = "gauntlet @ git+https://example.org/g@v1"


@pytest.mark.parametrize("before", [
    '[project]\nname = "x"\n\n[dependency-groups]\ndev = ["pytest"]\n',
    '[project]\nname = "x"\n\n[dependency-groups]\ndev = []\n',
    '[project]\nname = "x"\n\n[dependency-groups]\nlint = ["ruff"]\n',
    '[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = ["pytest"]\n',
    '[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = ["pytest"]\n\n[tool.hatch.build]\nonly = ["x"]\n',
    '[project]\nname = "x"\n',
    '[project]\nname = "x"\n\n[dependency-groups]\ndev = ["gauntlet @ git+https://example.org/g@v0"]\n',
    ('[project]\nname = "x"\n\n[dependency-groups]\ndev = [\n    "gauntlet[hcl] @ git+https://example.org/g@v0 ; '
     'python_version >= \'3.12\'",\n    "pytest",\n]\n'),
    "[project]\nname = 'x'\n\n[dependency-groups]\ndev = ['gauntlet @ git+https://example.org/g@v0']\n",
    '[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = ["gauntlet @ git+https://example.org/g@v0", "pytest"]\n',
    ('[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = [\n    "pytest",\n    "gauntlet @ '
     'git+https://example.org/g@v0 ; python_version >= \'3.12\'",\n]\n'),
    '[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = ["pytest", "gauntlet @ git+https://example.org/g@v0"]\n',
])
def test_the_pin_lands_in_the_dev_group_once_and_never_in_an_extra(before):
    after = init.add_pin(before, REQ)
    assert init.pin_problem(after, REQ) is None
    data = tomllib.loads(after)
    assert data["dependency-groups"]["dev"].count(REQ) == 1
    extras = data.get("project", {}).get("optional-dependencies", {})
    assert "pytest" in extras.get("dev", ["pytest"])
    assert not [r for reqs in extras.values() for r in reqs if r.startswith("gauntlet")]
    assert not [r for r in data["dependency-groups"]["dev"] if r.startswith("gauntlet") and r != REQ]


def test_init_needs_a_git_repo_with_python_or_terraform(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(SystemExit, match="not a git repository"):
        init.run(tmp_path / "plain")
    with pytest.raises(SystemExit, match="neither pyproject.toml nor a .tf file"):
        init.run(_repo(tmp_path / "empty", {"README": "x\n"}))


def _prose_detectors():
    # {detector: class} for each detector a corpus class body names in backticks.
    out = {}
    for name in skill.SKILL_FILES:
        heading, body = None, []
        for line in (skill.corpus_dir() / name).read_text(encoding="utf-8").splitlines() + ["## end"]:
            if line.startswith(("## ", "# ")):
                if heading and re.search(r"\*\*Tier:\*\*", "\n".join(body)):
                    for d in re.findall(r"`(\w+)`", "\n".join(body)):
                        if d in detectors.DETECTORS:
                            out.setdefault(d, skill.slug(heading))
                heading, body = (line[3:].strip() if line.startswith("## ") else None), []
            else:
                body.append(line)
    return out


def test_every_detector_names_a_corpus_class_and_agrees_with_the_corpus_prose():
    ids = {c["id"] for c in skill.read_skill(skill.corpus_dir())}
    assert set(detectors.DETECTORS) - set(detectors.CLASSES) == {"move_tables"}
    assert set(detectors.CLASSES.values()) - ids == set()
    prose = _prose_detectors()
    assert len(prose) >= 10
    assert {d: c for d, c in prose.items() if detectors.CLASSES.get(d) != c} == {}


def test_a_detector_bound_class_needs_no_instance_and_any_other_class_does():
    snap = [{"id": "c", "tier": 2}, {"id": "d", "tier": 2}]
    index = {"c": {"instances": [], "scanners": [], "detector": "closed_tables"},
             "d": {"instances": [], "scanners": []}}
    assert registry.problems(index, {}, {}, snap, {}, ".") == [
        "d: no instance; a class is bound here only once it has bitten"]


EXTRA = ('[project]\nname = "sample-app"\nversion = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = []\n\n'
         '[project.optional-dependencies]\ndev = ["pytest>=8"]\n')


def test_a_repo_with_only_a_dev_extra_gets_a_dev_group_and_its_extra_is_left_alone(tmp_path):
    root = _repo(tmp_path / "extra", {"pyproject.toml": EXTRA, "app/t.py": TABLE})
    init.run(root)
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert data["dependency-groups"]["dev"] == [f"gauntlet @ {init.URL}@v{__version__}"]
    assert data["project"]["optional-dependencies"] == {"dev": ["pytest>=8"]}
    init.run(root, force=True)
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert data["dependency-groups"]["dev"] == [f"gauntlet @ {init.URL}@v{__version__}"]


def _status(root):
    return subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                          capture_output=True, text=True, check=True).stdout


def test_a_crash_partway_through_init_writes_nothing(pyrepo, monkeypatch):
    subprocess.run(["git", "-C", str(pyrepo), "add", "-A"], check=True)
    before = (pyrepo / "pyproject.toml").read_text()

    def boom(*a, **kw):
        raise RuntimeError("a detector fell over")
    monkeypatch.setitem(detectors.DETECTORS, "closed_tables", boom)
    with pytest.raises(RuntimeError, match="fell over"):
        init.run(pyrepo)
    assert (pyrepo / "pyproject.toml").read_text() == before
    assert _status(pyrepo) == "A  app/t.py\nA  pyproject.toml\n"


CI = "jobs:\n  test:\n    steps:\n      - uses: actions/setup-python@v5\n        with:\n          python-version: {v}\n"
MATRIX = ("jobs:\n  test:\n    strategy:\n      matrix:\n        python:\n          - \"3.10\"\n          - \"3.12\"\n"
          "    steps:\n      - run: uv sync --python ${{ matrix.python }}\n")


@pytest.mark.parametrize("rp, workflows, warned", [
    (">=3.12", {}, []),
    (">=3.12", {"ci.yml": CI.format(v='"3.12"')}, []),
    (">=3.11", {}, ["requires-python '>=3.11'"]),
    (">=3.12", {"ci.yml": CI.format(v='"3.11"')}, [".github/workflows/ci.yml runs Python 3.11"]),
    (">=3.12", {"ci.yaml": CI.format(v="['3.9', '3.13']")}, [".github/workflows/ci.yaml runs Python 3.9"]),
    (">=3.12", {"t.yml": MATRIX}, [".github/workflows/t.yml runs Python 3.10"]),
    (">=3.12", {"t.yml": "jobs:\n  t:\n    steps:\n      - run: uv run --python 3.11 pytest\n"},
     [".github/workflows/t.yml runs Python 3.11"]),
])
def test_init_warns_where_the_repo_runs_python_older_than_312(tmp_path, capsys, rp, workflows, warned):
    files_ = {"pyproject.toml": PYPROJECT.format(rp=rp), "app/t.py": TABLE}
    files_.update({f".github/workflows/{name}": text for name, text in workflows.items()})
    root = _repo(tmp_path / "ci", files_)
    assert cli.main(["init", "--root", str(root)]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("warning: ")]
    assert len(lines) == len(warned)
    for line, start in zip(lines, warned):
        assert line.startswith(f"warning: {start}") and init.TEST_REL in line


def test_init_prints_the_ratchet_count_the_generated_module_holds(pyrepo, capsys):
    assert cli.main(["init", "--root", str(pyrepo)]) == 0
    n = len(_ratchets(pyrepo))
    assert n >= 10
    assert f"ratchet tests: {n} in {init.TEST_REL}: expect `{n} passed`" in capsys.readouterr().out


ACTIONS = ('OFFERED, CONFIRMED, LOCKED = "offered", "confirmed", "locked"\n'
           'MOVES = {"accept": ((OFFERED,), CONFIRMED), "lock": ((CONFIRMED, OFFERED), LOCKED)}\n')


def test_init_wires_a_repo_whose_move_table_maps_actions_to_from_and_to_states(pyrepo):
    (pyrepo / "app" / "booking.py").write_text(ACTIONS, encoding="utf-8")
    init.run(pyrepo)
    assert "'app/booking.py:MOVES': " in (pyrepo / init.REGISTRY_REL).read_text()
    assert _failing(pyrepo) == {}


HIDE = ('import sys\nfrom pathlib import Path\nfrom importlib.machinery import PathFinder\n\n\n'
        'class Hide:\n    # Python below 3.12 as the marker leaves it: no gauntlet package installed.\n'
        '    def find_spec(self, name, path=None, target=None):\n'
        '        if name != "gauntlet":\n            return None\n'
        '        bare = [p for p in sys.path if not (Path(p or ".") / "gauntlet" / "__init__.py").exists()]\n'
        '        return PathFinder.find_spec(name, bare)\n\n\n'
        'for _m in [m for m in sys.modules if m == "gauntlet" or m.startswith("gauntlet.")]:\n'
        '    del sys.modules[_m]\nsys.meta_path.insert(0, Hide())\n')


def test_the_generated_ratchets_skip_where_gauntlet_is_not_installed(pyrepo):
    init.run(pyrepo)
    (pyrepo / "conftest.py").write_text(HIDE, encoding="utf-8")
    env = {**__import__("os").environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", init.TEST_REL],
                         cwd=pyrepo, capture_output=True, text=True, env=env)
    assert run.returncode == 5 and "1 skipped" in run.stdout, run.stdout + run.stderr
