"""`gauntlet init`: wire a repo to the pinned gauntlet, with today's hits as its backlog so only a new hit fails."""

import json
import re
import subprocess
import tomllib
from collections import namedtuple
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
    "unscoped_query": 'shared-store scope: {"columns": ("tenant_id",), "tables": ("jobs",)}',
    "effect_reachable": 'effects, live test: {"effects": ("send",), "live": ("LIVE",)}',
    "unchecked_order": 'effects, import-time reads: {"effects": (), "reads": ()}',
    "prose_match": 'fields that hold prose: {"fields": ("note", "reason")}',
    "empty_on_failure": 'functions that choose a store: {"choosers": ("backend",)}',
    "unpaired_column": 'migrations, schema: {"migrations": ("db/",), "schema": ()}',
    "one_sided_schema_check": 'versions, check entry points: {"versions": (), "phases": ()}',
    "stored_pid_liveness": 'pid tests besides os.kill(pid, 0): {"alive": ("running",)}',
    "commit_then_link": 'committers besides conn.commit(): {"committers": ("put",)}',
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


_PIN = re.compile(r""""gauntlet\s*(?:\[[^\]]*\])?\s*@[^"]*"|'gauntlet\s*(?:\[[^\]]*\])?\s*@[^']*'""")


def _drop_item(text, start, end):
    # text without the array item at [start, end) and its trailing comma; its whole line when it stood alone.
    end = re.compile(r"[ \t]*,?[ \t]*").match(text, end).end()
    line = text.rfind("\n", 0, start) + 1
    if not text[line:start].strip() and text[end:end + 1] in ("\n", ""):
        return text[:line] + text[end + 1:]
    return text[:start] + text[end:]


def add_pin(text, requirement):
    """pyproject text with `requirement` as the only gauntlet pin, in [dependency-groups] dev (made if absent).
    Never an optional-dependency extra: that is package metadata, where hatchling refuses a git reference."""
    for m in reversed(list(_PIN.finditer(text))):
        text = _drop_item(text, m.start(), m.end())
    span = _section(text, "dependency-groups")
    if span is None:
        return text.rstrip("\n") + f'\n\n[dependency-groups]\ndev = ["{requirement}"]\n'
    m = re.search(r"(?m)^dev\s*=\s*\[", text[span[0]:span[1]])
    if m:
        return _array_insert(text, span[0] + m.end() - 1, requirement)
    return text[:span[0]] + f'\ndev = ["{requirement}"]' + text[span[0]:]


def pin_problem(text, requirement):
    """Why `text` does not hold `requirement` once, in the dev group and in no extra; None when it does."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return f"the edited pyproject.toml is not valid TOML ({e})"
    group = [r for r in data.get("dependency-groups", {}).get("dev", []) if isinstance(r, str)]
    if group.count(requirement) != 1:
        return "the pin is not in [dependency-groups] dev exactly once"
    extras = data.get("project", {}).get("optional-dependencies", {})
    if any(isinstance(r, str) and _PIN.fullmatch(f'"{r}"') for reqs in extras.values() for r in reqs):
        return "a gauntlet pin is left in [project.optional-dependencies]"
    return None


_CI_KEY = re.compile(r"(?i)python-version|--python\b|\buv python install\b|^\s*-?\s*python\w*\s*:")
_CI_VERSION = re.compile(r"(?<![\d.])3\.(\d+)(?![\d])")


def _below(line):
    return {f"3.{m}" for m in _CI_VERSION.findall(line) if int(m) < 12}


def ci_pythons_below_312(root):
    """{workflow path: versions below 3.12} named by python-version, --python and python matrix lines in CI."""
    out = {}
    wf = Path(root) / ".github" / "workflows"
    for path in sorted([*wf.glob("*.yml"), *wf.glob("*.yaml")]) if wf.is_dir() else []:
        found, listing = set(), False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if _CI_KEY.search(line):
                found |= _below(line)
                listing = line.rstrip().endswith(":")
            elif listing and line.lstrip().startswith("-"):
                found |= _below(line)
            else:
                listing = False
        if found:
            out[path.relative_to(root).as_posix()] = sorted(found, key=_version)
    return out


def python_warnings(root, spec):
    """One warning per place this repo runs Python older than 3.12, where gauntlet is not installed."""
    out = []
    if admits_below_312(spec):
        where = f"requires-python {spec!r} admits Python below 3.12" if spec else "no requires-python"
        out.append(f"{where}: there the marker leaves gauntlet uninstalled and {TEST_REL} skips; "
                   "run the ratchets on Python 3.12 or newer")
    for rel, versions in ci_pythons_below_312(root).items():
        out.append(f"{rel} runs Python {', '.join(versions)}: there {TEST_REL} skips, so that CI never enforces "
                   "the ratchets; add a job on 3.12 or newer")
    return out


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


def _q(value):
    # A literal as the formatters write it: double-quoted strings, tuples with their trailing comma.
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        inner = ", ".join(_q(v) for v in value)
        return f"[{inner}]" if isinstance(value, list) else f"({inner}{',' if len(value) == 1 else ''})"
    return repr(value)


def _dict_lines(name, items):
    # `name = {...}` one key per line with a magic trailing comma, which ruff format and black keep exploded.
    if not items:
        return [f"{name} = {{}}"]
    out = [f"{name} = {{"]
    for key, value in items:
        if isinstance(value, dict):
            out.append(f"    {_q(key)}: {{")
            out += [f"        {_q(k)}: {_q(v)}," for k, v in value.items()]
            out.append("    },")
        else:
            out.append(f"    {_q(key)}: {value},")
    return out + ["}"]


def _registry_text(index, frontends_, backlog_pin, move_sites, todos):
    lines = [f'"""Gauntlet registry, written by `gauntlet init` {__version__}; edit it by hand."""', "",
             "from pathlib import Path", "", "from gauntlet.registry import Registry", "",
             "HERE = Path(__file__).resolve().parent", "ROOT = HERE.parent.parent", ""]
    lines += [TODO + t for t in todos]
    lines += [""] + _dict_lines("INDEX", list(index.items()))
    lines += ["", "# Unbound hits seeded by init; lower it as the backlog is bound, never raise it.",
              f"BACKLOG_PIN = {backlog_pin}", "", 'WAIVED = "not yet registered: gauntlet init found it"', ""]
    lines += _dict_lines("MOVE_WAIVERS", [(site, "WAIVED") for site in move_sites])
    lines += ["", "REGISTRY = Registry(", "    root=ROOT,", "    index=INDEX,",
              '    snapshot_file=HERE / "skill_classes.json",', '    defects_file=HERE / "defects.json",',
              '    backlog_file=HERE / "backlog.json",', "    backlog_pin=BACKLOG_PIN,",
              f"    frontends={_q(tuple(frontends_))},", "    move_waivers=MOVE_WAIVERS,", ")", ""]
    return "\n".join(lines)


def _test_text(gate):
    head = ['"""The gauntlet ratchets over this repo\'s registry; written by `gauntlet init`."""', "",
            "import importlib.util", "from pathlib import Path", ""]
    if gate:
        head += ["import pytest", "", 'pytest.importorskip("gauntlet.ratchet", reason="gauntlet needs Python 3.12")', ""]
    head += ["from gauntlet import ratchet, skill", "",
             "_spec = importlib.util.spec_from_file_location(",
             '    "gauntlet_registry", Path(__file__).resolve().parent / "gauntlet" / "registry.py"',
             ")",
             "_registry = importlib.util.module_from_spec(_spec)", "_spec.loader.exec_module(_registry)", "",
             "globals().update(ratchet.tests(_registry.REGISTRY, skill_dir=skill.corpus_dir()))", ""]
    return "\n".join(head)


def _need_hcl():
    from gauntlet import hcl
    if not hcl.available():
        raise SystemExit("this repo has .tf files: install the hcl extra, gauntlet[hcl], and run init again")


Result = namedtuple("Result", "written todos backlog notes warnings ratchets")


def _write_all(root, plan):
    # Every file at once, after everything was computed: a failure before this point leaves the repo untouched.
    for rel, text in plan.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(plan)


def init_python(root, pin, has_tf, force=False):
    root = Path(root)
    if (root / REGISTRY_REL).exists() and not force:
        raise SystemExit(f"{REGISTRY_REL} exists: init refuses to overwrite it (pass --force to rewrite it)")
    if has_tf:
        _need_hcl()
    plan, notes = {}, []
    before = (root / "pyproject.toml").read_text(encoding="utf-8")
    spec = tomllib.loads(before).get("project", {}).get("requires-python")
    gate = admits_below_312(spec)
    req = pin_requirement(pin, ("hcl",) if has_tf else (), gate)
    plan["pyproject.toml"] = add_pin(before, req)
    why = pin_problem(plan["pyproject.toml"], req)
    if why:
        raise SystemExit(f"could not place {req!r} in pyproject.toml: {why}; add it to [dependency-groups] dev by hand")
    snapshot = skill.read_skill(skill.corpus_dir())
    plan[SNAPSHOT_REL] = json.dumps(snapshot, indent=1) + "\n"
    plan[DEFECTS_REL] = "[]\n"
    fes = ("python", "hcl") if has_tf else ("python",)
    index = detector_index(snapshot, hcl=has_tf)
    reg = Registry(root=root, index=index, snapshot_file=root / SNAPSHOT_REL, defects_file=root / DEFECTS_REL,
                   frontends=fes)
    led, _ = ratchet.tree_ledger(reg)
    backlog = {cid: row["unbound"] for cid, row in sorted(led.items()) if row["unbound"]}
    total = sum(len(v) for v in backlog.values())
    plan[BACKLOG_REL] = json.dumps(backlog, indent=1) + "\n"
    move_sites = ratchet.move_sites(reg)
    used = {n for e in index.values() for n in detectors.names(e)}
    todos = ["production: which paths are shipped code; the default is all but tests/ and test/.",
             'repo_slug: "owner/repo" for snapshot-defects and audit; until set, defects.json stays empty.']
    todos += [f"detector_config {name}: {CONFIG_TODOS[name]}" for name in sorted(CONFIG_TODOS) if name in used]
    todos += [f"move table {s}: register a MoveTable, drop its MOVE_WAIVERS line." for s in move_sites]
    plan[REGISTRY_REL] = _registry_text(index, fes, total, move_sites, todos)
    plan[TEST_REL] = _test_text(gate)
    ratchets = len(ratchet.tests(reg, skill_dir=skill.corpus_dir()))
    if pin != f"v{__version__}":
        notes.append(f"the snapshot is this installed version's corpus (v{__version__}), not {pin}'s: "
                     f"install {pin} and run init --force")
    warnings = python_warnings(root, spec)
    return Result(_write_all(root, plan), len(todos), total, notes, warnings, ratchets)


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
    written = _write_all(root, {BASELINE_REL: findings.render(found, "json"), CHECK_REL: _check_text(pin)})
    (root / CHECK_REL).chmod(0o755)
    notes = ["Terraform only: no pyproject.toml, so no registry and no pytest ratchets. This is the minimal form: "
             f"run {CHECK_REL} in CI; it fails on a finding not in the baseline."]
    return Result(written, 0, len(found), notes, [], 0)


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
