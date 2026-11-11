"""`gauntlet init`: wire a repo to the pinned gauntlet, with today's hits as its backlog so only a new hit fails."""

import json
import re
import subprocess
import tomllib
from pathlib import Path

from gauntlet import __version__, detectors, findings, files, frontends, ratchet, skill
from gauntlet.registry import Registry

URL = "git+https://github.com/unxmaal/gauntlet"
REGISTRY_REL = "tests/gauntlet/registry.py"
TEST_REL = "tests/test_gauntlet.py"
SNAPSHOT_REL = "tests/gauntlet/skill_classes.json"
DEFECTS_REL = "tests/gauntlet/defects.json"
BACKLOG_REL = "tests/gauntlet/backlog.json"
BASELINE_REL = ".gauntlet/baseline.json"
CHECK_REL = ".gauntlet/check.sh"
TODO = "# TODO(gauntlet): "

# What init cannot know about a repo, one line per detector that finds little or nothing until told.
CONFIG_TODOS = {
    "unscoped_query": 'the scope columns and tables of a shared store: {"columns": ("tenant_id",), "tables": ("jobs",)}',
    "effect_reachable": 'the effects and the live-run predicate: {"effects": ("send",), "live": ("LIVE_HOME",)}',
    "unchecked_order": 'the effects and import-time reads that need an order: {"effects": (...), "reads": (...)}',
    "prose_match": 'the fields that hold prose: {"fields": ("note", "reason")}',
    "empty_on_failure": 'the functions that choose a store: {"choosers": ("backend",)}',
    "unpaired_column": 'the migrations and schema paths: {"migrations": ("app/migrations/",), "schema": (...)}',
    "one_sided_schema_check": 'the schema version names and check entry points: {"versions": (...), "phases": (...)}',
    "stored_pid_liveness": 'helpers that test a stored pid besides os.kill(pid, 0): {"alive": ("running",)}',
    "commit_then_link": 'helpers that commit besides conn.commit(): {"committers": ("put",)}',
}


def pin_requirement(pin, extras=(), marker=False):
    name = "gauntlet" + (f"[{','.join(extras)}]" if extras else "")
    return f"{name} @ {URL}@{pin}" + (" ; python_version >= '3.12'" if marker else "")


def _version(text):
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def admits_below_312(spec):
    """True when a requires-python specifier admits some Python older than 3.12 (no specifier admits all)."""
    floor = None
    for clause in (spec or "").split(","):
        m = re.match(r"\s*(>=|>|~=|==|===)\s*([\d.*]+)", clause)
        if not m:
            continue
        v = _version(m.group(2))
        floor = v if floor is None or v > floor else floor
    return floor is None or floor < (3, 12)


def _array_insert(text, start, item):
    # Insert `item` as the first element of the TOML array whose `[` is at text[start].
    after = text[start + 1:]
    if after.lstrip(" ").startswith("\n"):
        return text[:start + 1] + f'\n    "{item}",' + after
    if after.lstrip().startswith("]"):
        return text[:start + 1] + f'"{item}"' + after
    return text[:start + 1] + f'"{item}", ' + after


def _section(text, header):
    m = re.search(rf"(?m)^\[{re.escape(header)}\]\s*$", text)
    if not m:
        return None
    nxt = re.search(r"(?m)^\[", text[m.end():])
    return m.end(), m.end() + nxt.start() if nxt else len(text)


def add_pin(text, requirement):
    """pyproject text with `requirement` as a dev dependency: replacing any gauntlet pin, else in the dev group."""
    existing = re.search(r""""gauntlet\s*(?:\[[^\]]*\])?\s*@[^"]*"|'gauntlet\s*(?:\[[^\]]*\])?\s*@[^']*'""", text)
    if existing:
        return text[:existing.start()] + f'"{requirement}"' + text[existing.end():]
    for header in ("dependency-groups", "project.optional-dependencies"):
        span = _section(text, header)
        if span is None:
            continue
        m = re.search(r"(?m)^dev\s*=\s*\[", text[span[0]:span[1]])
        if m:
            return _array_insert(text, span[0] + m.end() - 1, requirement)
        if header == "dependency-groups":
            return text[:span[0]] + f'\ndev = ["{requirement}"]' + text[span[0]:]
    return text.rstrip("\n") + f'\n\n[dependency-groups]\ndev = ["{requirement}"]\n'


def _dev_requirements(data):
    groups = data.get("dependency-groups", {}).get("dev", [])
    extras = data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    return [r for r in groups + extras if isinstance(r, str)]


def detector_index(snapshot, hcl=False):
    """{class: entry} for every corpus class a detector holds; a class with several detectors names them as a list."""
    names = {}
    for name, cid in detectors.CLASSES.items():
        names.setdefault(cid, []).append(name)
    if hcl:
        from gauntlet import hcl as hcl_mod
        for name, (_, cid) in hcl_mod.DETECTORS.items():
            names.setdefault(cid, []).append(name)
    ids = {c["id"] for c in snapshot}
    return {cid: {"instances": [], "scanners": [], "detector": ds[0] if len(ds) == 1 else ds}
            for cid, ds in sorted(names.items()) if cid in ids}


def _is_git(root):
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-dir"], capture_output=True, text=True)
    return r.returncode == 0


def _registry_text(index, frontends_, backlog_pin, move_sites, todos):
    lines = [f'"""This repo\'s gauntlet registry, written by `gauntlet init` {__version__}; edit it by hand from here."""',
             "", "from pathlib import Path", "", "from gauntlet.registry import Registry", "",
             "HERE = Path(__file__).resolve().parent", "ROOT = HERE.parent.parent", ""]
    lines += [TODO + t for t in todos]
    lines += ["", "INDEX = {"]
    for cid, entry in index.items():
        lines.append(f"    {cid!r}: {entry!r},")
    lines += ["}", "", "# Unbound hits seeded by init; lower it as the backlog is bound, never raise it.",
              f"BACKLOG_PIN = {backlog_pin}", "", "MOVE_WAIVERS = {"]
    for site in move_sites:
        lines.append(f"    {site!r}: \"not yet registered: gauntlet init found it\",")
    lines += ["}", "", "REGISTRY = Registry(", "    root=ROOT,", "    index=INDEX,",
              '    snapshot_file=HERE / "skill_classes.json",', '    defects_file=HERE / "defects.json",',
              '    backlog_file=HERE / "backlog.json",', "    backlog_pin=BACKLOG_PIN,",
              f"    frontends={tuple(frontends_)!r},", "    move_waivers=MOVE_WAIVERS,", ")", ""]
    return "\n".join(lines)


def _test_text(gate):
    head = ['"""The gauntlet ratchets over this repo\'s registry; written by `gauntlet init`."""', "",
            "import importlib.util", "from pathlib import Path", ""]
    if gate:
        head += ["import pytest", "", 'pytest.importorskip("gauntlet", reason="gauntlet needs Python 3.12")', ""]
    head += ["from gauntlet import ratchet, skill", "",
             '_spec = importlib.util.spec_from_file_location("gauntlet_registry", '
             'Path(__file__).resolve().parent / "gauntlet" / "registry.py")',
             "_registry = importlib.util.module_from_spec(_spec)", "_spec.loader.exec_module(_registry)", "",
             "globals().update(ratchet.tests(_registry.REGISTRY, skill_dir=skill.corpus_dir()))", ""]
    return "\n".join(head)


def _need_hcl():
    from gauntlet import hcl
    if not hcl.available():
        raise SystemExit("this repo has .tf files: install the hcl extra, gauntlet[hcl], and run init again")


def _write(root, rel, text, written):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(rel)


def init_python(root, pin, has_tf, force=False):
    root = Path(root)
    if (root / REGISTRY_REL).exists() and not force:
        raise SystemExit(f"{REGISTRY_REL} exists: init refuses to overwrite it (pass --force to rewrite it)")
    if has_tf:
        _need_hcl()
    written, notes = [], []
    pyproject = root / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    gate = admits_below_312(data.get("project", {}).get("requires-python"))
    req = pin_requirement(pin, ("hcl",) if has_tf else (), gate)
    text = add_pin(pyproject.read_text(encoding="utf-8"), req)
    if req not in _dev_requirements(tomllib.loads(text)):
        raise SystemExit(f"could not place {req!r} in pyproject.toml; add it to the dev dependencies by hand")
    _write(root, "pyproject.toml", text, written)
    snapshot = skill.read_skill(skill.corpus_dir())
    _write(root, SNAPSHOT_REL, json.dumps(snapshot, indent=1) + "\n", written)
    _write(root, DEFECTS_REL, "[]\n", written)
    fes = ("python", "hcl") if has_tf else ("python",)
    index = detector_index(snapshot, hcl=has_tf)
    reg = Registry(root=root, index=index, snapshot_file=root / SNAPSHOT_REL, defects_file=root / DEFECTS_REL,
                   frontends=fes)
    led, _ = ratchet.tree_ledger(reg)
    backlog = {cid: row["unbound"] for cid, row in sorted(led.items()) if row["unbound"]}
    total = sum(len(v) for v in backlog.values())
    _write(root, BACKLOG_REL, json.dumps(backlog, indent=1) + "\n", written)
    move_sites = ratchet.move_sites(reg)
    used = {n for e in index.values() for n in detectors.names(e)}
    todos = ["production: which paths are shipped code; the default is everything outside tests/ and test/.",
             'repo_slug: "owner/repo" of the issue tracker, for `gauntlet snapshot-defects` and `gauntlet audit`.']
    todos += [f"detector_config {name}: {CONFIG_TODOS[name]}" for name in sorted(CONFIG_TODOS) if name in used]
    todos += [f"move table {s}: register a gauntlet.moves.MoveTable and drop its MOVE_WAIVERS line." for s in move_sites]
    _write(root, REGISTRY_REL, _registry_text(index, fes, total, move_sites, todos), written)
    _write(root, TEST_REL, _test_text(gate), written)
    if pin != f"v{__version__}":
        notes.append(f"the snapshot is this installed version's corpus (v{__version__}), not {pin}'s: "
                     f"install {pin} and run init --force")
    return written, len(todos), total, notes


def _check_text(pin):
    req = pin_requirement(pin, ("hcl",))
    return "\n".join([
        "#!/bin/sh",
        "# Fails on a gauntlet finding that is not in .gauntlet/baseline.json; run it as a CI step.",
        "set -eu",
        f'uvx --from "{req}" gauntlet scan --frontend hcl --baseline .gauntlet/baseline.json',
        ""])


def init_terraform(root, pin, force=False):
    root = Path(root)
    if (root / BASELINE_REL).exists() and not force:
        raise SystemExit(f"{BASELINE_REL} exists: init refuses to overwrite it (pass --force to rewrite it)")
    _need_hcl()
    fe = frontends.get("hcl")
    found = sorted(set(fe.scan(root, fe.discover(root))))
    written = []
    _write(root, BASELINE_REL, findings.render(found, "json"), written)
    _write(root, CHECK_REL, _check_text(pin), written)
    (root / CHECK_REL).chmod(0o755)
    notes = ["Terraform only: no pyproject.toml, so no registry and no pytest ratchets. This is the minimal form: "
             f"run {CHECK_REL} in CI; it fails on a finding not in the baseline."]
    return written, 0, len(found), notes


def run(root=".", pin=None, force=False):
    root = Path(root).resolve()
    pin = pin or f"v{__version__}"
    if not _is_git(root):
        raise SystemExit(f"{root} is not a git repository: init reads the files git would publish")
    has_tf = any(rel.endswith(".tf") for rel in files.names(root))
    if (root / "pyproject.toml").is_file():
        return init_python(root, pin, has_tf, force)
    if has_tf:
        return init_terraform(root, pin, force)
    raise SystemExit("found neither pyproject.toml nor a .tf file: nothing to wire")
