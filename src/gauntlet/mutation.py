"""Mutation testing of a repo's core modules with mutmut, ratcheted against a pinned inventory of survivors."""

import argparse
import ast
import difflib
import functools
import io
import json
import os
import shutil
import subprocess
import tempfile
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gauntlet import files as _files

#: In a --found file from a --shard run: the functions each module's keys cover.
FUNCTIONS_KEY = "_functions"


@dataclass
class MutationConfig:
    """A consumer's mutation registry: which modules, which tests kill their mutants, and where the pin lives."""
    root: Path
    modules: dict
    pin_file: Path
    left_out: dict = field(default_factory=dict)
    deselect: tuple = ()
    source_paths: tuple = ("src",)
    source_root: str = ""
    also_copy: tuple = ()
    uv_groups: tuple = ()
    python: str = "3.12"
    timeout_constant: float = 60.0
    tests_glob: str = "tests/test_*.py"
    env: Callable[[Path], dict] = lambda tree: {}
    env_dirs: tuple = ()
    lister: Callable | None = None


# mutmut 3 exit codes: survived and untested are gaps; anything unrecognised means the run is not trustworthy.
GAP = {0, 5, 33}
KILLED = {1, 3, 24, -24, 36, 37, 152}
# 255 is os._exit(-1) after the test runner raised in the worker; -11 a segfaulted worker. Both flap, so are retried.
UNSETTLED = {255, -11}
RETRIES = 3
SKIP_TOKENS = {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
               tokenize.COMMENT, tokenize.ENCODING, tokenize.ENDMARKER}
SEP = "ǁ"


def _nodes(tree, lazy):
    todo = [tree]
    while todo:
        n = todo.pop()
        yield n
        todo.extend(c for c in ast.iter_child_nodes(n)
                    if lazy or not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))


def imported(text, package="", lazy=True):
    """Every dotted name Python source imports (with lazy, inside functions too); relative ones need their package."""
    return set(_imported(text, package, lazy))


@functools.lru_cache(maxsize=4096)
def _imported(text, package, lazy):
    # Parsed once per (text, package, lazy): unplaced asks of every test file once per module.
    names = set()
    for n in _nodes(ast.parse(text), lazy):
        if isinstance(n, ast.Import):
            for a in n.names:
                parts = a.name.split(".")
                names.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                if not package:
                    continue
                up = package.split(".")[:len(package.split(".")) - n.level + 1]
                base = ".".join(up + ([n.module] if n.module else []))
            if base:
                names.add(base)
            names.update(f"{base}.{a.name}" if base else a.name for a in n.names if a.name != "*")
    return frozenset(names)


def dotted(module, source_root=""):
    rel = module[len(source_root) + 1:] if source_root and module.startswith(source_root + "/") else module
    return rel[:-3].replace("/", ".")


def imports(text, module, source_root=""):
    # Whether Python source imports a module, by any of the three spellings.
    return dotted(module, source_root) in imported(text)


def unplaced(cfg, module):
    # Test files that import the module but are neither selected for it nor deliberately left out.
    placed = {Path(t).name for t in cfg.modules[module]} | set(cfg.left_out.get(module, ()))
    return sorted(p.name for p in Path(cfg.root).glob(cfg.tests_glob)
                  if p.name not in placed and imports(p.read_text(encoding="utf-8"), module, cfg.source_root))


class BrokenRun(RuntimeError):
    pass


def config(cfg, module):
    lines = [
        "", "[tool.mutmut]",
        f"source_paths = {json.dumps(list(cfg.source_paths))}",
        f"only_mutate = {json.dumps([module])}",
        f"pytest_add_cli_args_test_selection = {json.dumps(list(cfg.modules[module]))}",
        'process_isolation = "forkserver"',
        f"timeout_constant = {float(cfg.timeout_constant)}",
        f"pytest_add_cli_args = {json.dumps([a for t in cfg.deselect for a in ('--deselect', t)])}",
        f"also_copy = {json.dumps(list(cfg.also_copy))}",
    ]
    return "\n".join(lines) + "\n"


def _split(mutant):
    stem, _, number = mutant.rpartition("__mutmut_")
    parts = stem.split(".")
    i = max(i for i, p in enumerate(parts) if p.startswith(("x_", "x" + SEP)))
    mangled = ".".join(parts[i:])
    if mangled.startswith("x" + SEP):
        _, cls, fn = mangled.split(SEP)
        shown = f"{cls}.{fn}"
    else:
        shown = mangled[2:]
    return ".".join(parts[:i]), mangled, shown, int(number)


def _tokens(node):
    out = []
    for t in tokenize.generate_tokens(io.StringIO(ast.unparse(node)).readline):
        if t.type not in SKIP_TOKENS and t.string != node.name:
            out.append(t.string)
    return out


def kind(orig, mutant):
    a, b = _tokens(orig), _tokens(mutant)
    pieces = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            ctx = a[i1 - 1] if i1 else "^"
            pieces.append(f"{ctx} {' '.join(a[i1:i2])!r}->{' '.join(b[j1:j2])!r}")
    return "; ".join(pieces) or "identical"


def shown(key):
    """The function a survivor key names, `Cls.fn` for a method."""
    return key.split(":", 2)[1]


def globs(module, functions, source_root=""):
    # mutmut name patterns for these functions' mutants, so a daytime run mutates a few functions, not a module.
    name = dotted(module, source_root)
    out = []
    for f in functions:
        cls, _, fn = f.rpartition(".")
        out.append(f"{name}.x{SEP}{cls}{SEP}{fn}__mutmut_*" if cls else f"{name}.x_{fn}__mutmut_*")
    return out


def functions_of(source):
    """Every function mutmut mutates in a module's source, `Cls.fn` for a method."""
    out = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node.name)
        elif isinstance(node, ast.ClassDef):
            out += [f"{node.name}.{n.name}" for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return sorted(out)


def shard(source, spec, pinned=()):
    """Shard `K/N`'s functions: every Nth by name from the Kth; the first also owns pins whose function is gone."""
    k, _, n = spec.partition("/")
    k, n = int(k), int(n)
    if not 1 <= k <= n:
        raise SystemExit(f"--shard {spec}: want K/N with 1 <= K <= N")
    names = functions_of(source)
    gone = sorted({shown(key) for key in pinned} - set(names)) if k == 1 else []
    return tuple(names[k - 1::n]) + tuple(gone)


def within(meta, functions):
    """The meta cut to these functions' mutants; mutmut leaves the rest unrun."""
    if not functions:
        return meta
    return {**meta, "exit_code_by_key": {k: v for k, v in meta["exit_code_by_key"].items()
                                         if _split(k)[2] in functions}}


def unsettled(meta):
    return sorted(k for k, v in meta["exit_code_by_key"].items() if v in UNSETTLED)


def survivors(meta, mutated_source):
    gaps = []
    for name, code in meta["exit_code_by_key"].items():
        if code in KILLED:
            continue
        if code not in GAP:
            raise BrokenRun(f"{name} ended with exit code {code}: the run is not trustworthy")
        gaps.append(name)
    if not gaps:
        return []
    defs = {n.name: n for n in ast.walk(ast.parse(mutated_source))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    keyed = []
    for name in sorted(gaps, key=lambda n: _split(n)[3]):
        module, mangled, shown, number = _split(name)
        orig, mut = defs.get(f"{mangled}__mutmut_orig"), defs.get(f"{mangled}__mutmut_{number}")
        if orig is None or mut is None:
            raise BrokenRun(f"{name} has no source in the mutated file")
        keyed.append(f"{module}:{shown}:{kind(orig, mut)}")
    seen, out = {}, []
    for k in keyed:
        seen[k] = seen.get(k, 0) + 1
        out.append(k if seen[k] == 1 else f"{k} #{seen[k]}")
    return sorted(out)


def ratchet(found, pinned, module, pin_name="the pin file"):
    found, pinned = set(found), set(pinned)
    problems = []
    new = sorted(found - pinned)
    if new:
        problems.append(f"{module}: {len(new)} new surviving mutant(s); add a test that kills each:\n  "
                        + "\n  ".join(new))
    gone = sorted(pinned - found)
    if gone:
        problems.append(f"{module}: {len(gone)} pinned survivor(s) now killed; remove from "
                        f"{pin_name}:\n  " + "\n  ".join(gone))
    return problems


def gate(found_by_module, pin, pin_name="the pin file"):
    problems, notes = [], []
    for module, found in sorted(found_by_module.items()):
        if module in pin:
            problems += ratchet(found, pin[module], module, pin_name)
        else:
            notes.append(f"{module}: unpinned, {len(found)} survivor(s); run with --pin to start its ratchet")
    return problems, notes


def tally(found_by_module, pin):
    """One count line per pinned module, printed last so a log tail that cuts the key lists still shows both."""
    return [f"{m}: {len(set(f) - set(pin[m]))} new survivor(s), {len(set(pin[m]) - set(f))} pinned now killed"
            for m, f in sorted(found_by_module.items()) if m in pin]


def _paths(tree, module):
    base = Path(tree) / "mutants" / module
    return base, Path(f"{base}.meta")


def _uv(cfg):
    return ["uv", "run", "--python", cfg.python, *(a for g in cfg.uv_groups for a in ("--group", g))]


def _mutmut(cfg, tree, env, log, names, jobs=None):
    cap = ["--max-children", str(jobs)] if jobs else []
    r = subprocess.run([*_uv(cfg), "mutmut", "run", *cap, *names],
                       cwd=tree, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    if r.returncode != 0:
        raise BrokenRun(f"mutmut exited {r.returncode}")


def copy_tree(cfg, dest):
    root = Path(cfg.root)
    for path in (cfg.lister or _files.publishable)(root):
        if path.is_file():
            target = dest / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _baseline(cfg, tree, env, module, jobs=None):
    # The selection must pass unmutated; mutmut reports only "exited 1" and hides which test failed.
    deselect = [a for t in cfg.deselect for a in ("--deselect", t)]
    argv = [*_uv(cfg), "python", "-m", "pytest",
            "-p", "no:randomly", "-n", str(jobs) if jobs else "auto", "-rfE", *deselect, *cfg.modules[module]]
    r = subprocess.run(argv, cwd=tree, env=env, capture_output=True, text=True, check=False)
    if r.returncode == 0:
        return
    failed = [line for line in r.stdout.splitlines() if line.startswith(("FAILED ", "ERROR "))]
    raise BrokenRun(f"the selected tests fail unmutated (exit {r.returncode}):\n  "
                    + "\n  ".join(failed or (r.stdout + r.stderr).splitlines()[-20:]))


def run_module(cfg, tree, module, pyproject, log, functions=(), jobs=None):
    (tree / "pyproject.toml").write_text(pyproject + config(cfg, module), encoding="utf-8")
    shutil.rmtree(tree / "mutants", ignore_errors=True)
    extra = {k: str(v) for k, v in cfg.env(tree).items()}
    for d in cfg.env_dirs:
        Path(extra[d]).mkdir(parents=True, exist_ok=True)
    # no_proxy: urllib's macOS proxy lookup reads CFPreferences, which segfaults a forked worker.
    env = dict(os.environ, **extra, OBJC_DISABLE_INITIALIZE_FORK_SAFETY="YES", no_proxy="*")
    try:
        _baseline(cfg, tree, env, module, **({"jobs": jobs} if jobs else {}))
    except BrokenRun as e:
        raise BrokenRun(f"{module}: {e}") from None
    base, meta_path = _paths(tree, module)
    names = globs(module, functions, cfg.source_root)
    for _ in range(1 + RETRIES):
        try:
            _mutmut(cfg, tree, env, log, names, **({"jobs": jobs} if jobs else {}))
        except BrokenRun as e:
            raise BrokenRun(f"{module}: {e}") from None
        meta = within(json.loads(meta_path.read_text(encoding="utf-8")), functions)
        names = unsettled(meta)
        if not names:
            break
    return survivors(meta, base.read_text(encoding="utf-8"))


def load_pin(cfg):
    pin_file = Path(cfg.pin_file)
    return json.loads(pin_file.read_text(encoding="utf-8")) if pin_file.exists() else {}


def write_pin(cfg, pin):
    Path(cfg.pin_file).write_text(json.dumps(dict(sorted(pin.items())), indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")


def cmd_run(cfg, a):
    modules = a.module or list(cfg.modules)
    functions = tuple(a.function or ())
    if a.shard:
        if len(modules) != 1 or functions:
            raise SystemExit("--shard takes exactly one --module and no --function")
        functions = shard((Path(cfg.root) / modules[0]).read_text(encoding="utf-8"), a.shard,
                          load_pin(cfg).get(modules[0], ()))
    elif functions and (len(modules) != 1 or a.found):
        raise SystemExit("--function takes exactly one --module and no --found")
    workdir = Path(a.workdir) if a.workdir else Path(tempfile.mkdtemp(prefix="mutation-"))
    tree = workdir / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    copy_tree(cfg, tree)
    pyproject = (tree / "pyproject.toml").read_text(encoding="utf-8")
    found, broken = {}, []
    with open(workdir / "mutmut.log", "a", encoding="utf-8") as log:
        for module in modules:
            print(f"{module}: running", flush=True)
            try:
                found[module] = run_module(cfg, tree, module, pyproject, log, functions, a.jobs)
            except BrokenRun as e:
                broken.append(str(e))
                print(f"{module}: BROKEN {e}", flush=True)
                continue
            print(f"{module}: {len(found[module])} survivor(s)", flush=True)
    if a.found:
        Path(a.found).parent.mkdir(parents=True, exist_ok=True)
        limited = {FUNCTIONS_KEY: {m: list(functions) for m in found}} if a.shard else {}
        Path(a.found).write_text(json.dumps({**found, **limited}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    pin = load_pin(cfg)
    if functions:
        rest = {m: [k for k in keys if shown(k) not in functions] for m, keys in pin.items() if m in found}
        if a.pin:
            found = {m: sorted(rest.get(m, []) + keys) for m, keys in found.items()}
        else:
            pin = {**pin, **{m: [k for k in pin[m] if shown(k) in functions] for m in rest}}
    if a.pin:
        pin.update(found)
        write_pin(cfg, pin)
        print(f"pinned {', '.join(found)} -> {Path(cfg.pin_file).name}")
        return 1 if broken else 0
    problems, notes = gate(found, pin, Path(cfg.pin_file).name)
    problems += broken
    for line in notes + problems + tally(found, pin):
        print(line)
    print(f"mutmut log: {workdir / 'mutmut.log'}")
    return 1 if problems else 0


def cmd_pin(cfg, a):
    # Pin the survivors a CI run found (its --found artifact), so no pin waits on a quiet local machine.
    pin = load_pin(cfg)
    for path in a.sources:
        found = json.loads(Path(path).read_text(encoding="utf-8"))
        limited = found.pop(FUNCTIONS_KEY, {})
        unknown = sorted(set(found) - set(cfg.modules))
        if unknown:
            raise SystemExit(f"{path}: not mutated modules: {', '.join(unknown)}")
        for m, keys in found.items():
            # A shard's file replaces only its own functions' pins; the other shards' stay.
            kept = [k for k in pin.get(m, []) if m in limited and shown(k) not in limited[m]]
            pin[m] = sorted(set(kept) | set(keys))
        print(f"pinned {', '.join(sorted(found))} from {path}")
    write_pin(cfg, pin)
    return 0


def parser(prog="gauntlet mutation"):
    p = argparse.ArgumentParser(prog=prog)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="mutate the core modules and ratchet survivors against the pin")
    r.add_argument("--module", action="append", help="default: all")
    r.add_argument("--pin", action="store_true", help="write today's survivors as the pin instead of gating")
    r.add_argument("--workdir", help="default: a fresh temporary directory")
    r.add_argument("--found", help="also write each module's survivors here as JSON, for `pin --from`")
    r.add_argument("--function", action="append", help="mutate only this function (Cls.fn for a method); "
                   "the ratchet and --pin cover only these")
    r.add_argument("--jobs", type=int, help="mutmut and baseline pytest workers (default: one per CPU)")
    r.add_argument("--shard", metavar="K/N", help="mutate only every Nth function from the Kth, so N CI jobs split one "
                   "module; ratchets and --found cover only those")
    r.set_defaults(fn=cmd_run)
    q = sub.add_parser("pin", help="pin the survivors in --found files from a CI run")
    q.add_argument("--from", dest="sources", nargs="+", required=True, metavar="FILE")
    q.set_defaults(fn=cmd_pin)
    return p


def main(cfg, argv=None):
    a = parser().parse_args(argv)
    unknown = sorted(set(getattr(a, "module", None) or ()) - set(cfg.modules))
    if unknown:
        raise SystemExit(f"--module: not mutated modules: {', '.join(unknown)}")
    return a.fn(cfg, a)
