"""Repo-wide scans for tier-1 classes; each takes (text, rel) and returns findings as "rel:line: why"."""

import ast
import functools
import re
from pathlib import Path

from gauntlet import files as _files

_MUTABLE_CALLS = {"list", "dict", "set", "bytearray", "defaultdict", "OrderedDict", "Counter", "deque"}


@functools.cache
def tree(text):
    # One parse per source text a process; (None, line) when it does not parse.
    try:
        return ast.parse(text), 0
    except SyntaxError as e:
        return None, e.lineno or 0


def _parse(text, rel):
    got, line = tree(text)
    return (got, []) if got is not None else (None, [f"{rel}:{line}: does not parse, so it was not scanned"])


def _call_name(node):
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def mutable_defaults(text, rel):
    tree, out = _parse(text, rel)
    if tree is None:
        return out
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        for d in n.args.defaults + [k for k in n.args.kw_defaults if k is not None]:
            if (isinstance(d, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp))
                    or (isinstance(d, ast.Call) and _call_name(d) in _MUTABLE_CALLS)):
                out.append(f"{rel}:{d.lineno}: mutable default argument, shared across calls")
    return out


def _empty(body):
    return all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
               for s in body)


def swallowed_python(text, rel):
    tree, out = _parse(text, rel)
    if tree is None:
        return out
    for n in ast.walk(tree):
        if not isinstance(n, ast.ExceptHandler):
            continue
        if n.type is None:
            out.append(f"{rel}:{n.lineno}: bare except catches everything, KeyboardInterrupt included")
        elif _empty(n.body):
            out.append(f"{rel}:{n.lineno}: caught exception with an empty body")
    return out


_OR_TRUE = re.compile(r"\|\|\s*(?:true|:)(?=\s|;|\)|$)")


def _code_lines(text):
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.lstrip().startswith("#"):
            yield number, line


def swallowed_shell(text, rel):
    return [f"{rel}:{n}: `|| true` discards the exit status" for n, line in _code_lines(text)
            if _OR_TRUE.search(line)]


_SET_OPTS = re.compile(r"^\s*set\s+(.*)$", re.M)
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_COMMANDS = re.compile(r";|&&|\|\||\bthen\b|\bdo\b")
# `producer | grep -q` fails closed without pipefail, and lies with it, so it is not flagged.
_PROBE = re.compile(r"\s*grep\s+-[a-zA-Z]*q")
_CD = re.compile(r"(?:^|[;&(]|\bthen|\bdo|\belse)\s*cd(?:\s|$)")
_CD_GUARDED = re.compile(r"\bcd\b[^;|&]*(?:\|\||&&)")


def _unprobed_pipeline(line):
    for command in _COMMANDS.split(_QUOTED.sub("''", line)):
        stages = command.split("|")
        if len(stages) > 1 and not _PROBE.match(stages[-1]):
            return True
    return False


def _options(text):
    shorts, longs = set(), set()
    for m in _SET_OPTS.finditer(text):
        words = m.group(1).split()
        for i, w in enumerate(words):
            if re.fullmatch(r"-[a-zA-Z]+", w):
                shorts.update(w[1:])
            # `set -uo pipefail`: a short group ending in o takes the next word as a long option.
            if re.fullmatch(r"-[a-zA-Z]*o", w) and i + 1 < len(words):
                longs.add(words[i + 1])
    return ("e" in shorts or "errexit" in longs), ("pipefail" in longs)


def unchecked_status_shell(text, rel):
    errexit, pipefail = _options(text)
    executable = text.startswith("#!")
    out = []
    for n, line in _code_lines(text):
        # Reported once, at the first pipeline: the fix is one line at the top.
        if executable and not pipefail and _unprobed_pipeline(line):
            pipefail = True
            out.append(f"{rel}:{n}: a pipeline with no `set -o pipefail`, so a failing producer is invisible")
        if not errexit and _CD.search(line) and not _CD_GUARDED.search(line):
            out.append(f"{rel}:{n}: unchecked cd with no errexit; the next command runs in the wrong directory")
    return out


_SERVERS = re.compile(r"(?:bind|Server|create_server)$")
_ABS = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
_WRITERS = {"write_text", "write_bytes", "mkdir", "touch", "symlink_to"}


def _abs_literal(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and bool(_ABS.match(node.value))


def _fixed_port(call):
    if not call.args or not isinstance(call.args[0], ast.Tuple) or len(call.args[0].elts) < 2:
        return None
    port = call.args[0].elts[1]
    if isinstance(port, ast.Constant) and type(port.value) is int and port.value != 0:
        return port.value
    return None


def _write_mode(call):
    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and bool(set(mode.value) & set("wax+"))


def fixed_resources(text, rel):
    tree, out = _parse(text, rel)
    if tree is None:
        return out
    found = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = _call_name(n)
        if _SERVERS.search(name) and _fixed_port(n) is not None:
            found.append((n.lineno, f"binds fixed port {_fixed_port(n)}; bind port 0 and ask the OS"))
        elif (name in _WRITERS and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Call)
              and n.func.value.args and _abs_literal(n.func.value.args[0])):
            found.append((n.lineno, "writes a fixed absolute path; use tmp_path"))
        elif name == "open" and isinstance(n.func, ast.Name) and n.args and _abs_literal(n.args[0]) \
                and _write_mode(n):
            found.append((n.lineno, "writes a fixed absolute path; use tmp_path"))
    return out + [f"{rel}:{line}: {why}" for line, why in sorted(found)]


# Names Windows's os and signal modules do not have, and modules it does not ship.
POSIX_ONLY = {
    "os": {"killpg", "getpgid", "setpgid", "setpgrp", "setsid", "getsid", "fork", "forkpty", "getuid",
           "geteuid", "getgid", "getegid", "chown", "lchown", "fchown", "uname", "getloadavg", "wait3",
           "wait4", "WNOHANG", "nice", "mkfifo", "statvfs", "sysconf", "openpty", "pipe2"},
    "signal": {"SIGKILL", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGCHLD", "SIGALRM", "SIGQUIT", "SIGPIPE",
               "SIGTSTP", "SIGCONT", "SIGSTOP", "alarm", "setitimer", "getitimer", "pause", "siginterrupt",
               "pthread_kill"},
}
POSIX_MODULES = {"fcntl", "pwd", "grp", "resource", "termios", "pty", "tty", "posix"}
_PLATFORM_NAME = re.compile(r"(?i)windows|posix|win32|darwin|linux|platform")
_GUARD_ERRORS = {"AttributeError", "ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _is_platform_test(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in ("platform", "name", "system") \
                and isinstance(n.value, ast.Name) and n.value.id in ("sys", "os", "platform"):
            return True
        if isinstance(n, ast.Call) and _call_name(n) == "hasattr":
            return True
        if isinstance(n, ast.Name) and _PLATFORM_NAME.search(n.id):
            return True
    return False


def _exits(body):
    last = body[-1] if body else None
    if isinstance(last, ast.Try):
        return _exits(last.body + last.orelse) and all(_exits(h.body) for h in last.handlers)
    if isinstance(last, ast.If):
        return _exits(last.body) and _exits(last.orelse)
    return isinstance(last, (ast.Return, ast.Raise, ast.Continue, ast.Break)) or (
        isinstance(last, ast.Expr) and isinstance(last.value, ast.Call) and _call_name(last.value) == "skip")


def _catches(handler, names):
    t = handler.type
    elts = t.elts if isinstance(t, ast.Tuple) else [t] if t is not None else []
    return t is None or any(isinstance(e, ast.Name) and e.id in names for e in elts)


def _guarded(node, parents):
    child, cur = node, parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.If, ast.IfExp, ast.While)) and child is not cur.test and _is_platform_test(cur.test):
            return True
        if isinstance(cur, ast.Try) and child in cur.body and any(_catches(h, _GUARD_ERRORS) for h in cur.handlers):
            return True
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and any(_is_platform_test(d) for d in cur.decorator_list):
            return True
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(cur, field, None)
            if isinstance(stmts, list) and child in stmts:
                for prev in stmts[:stmts.index(child)]:
                    if isinstance(prev, ast.If) and _is_platform_test(prev.test) and _exits(prev.body):
                        return True
                    if isinstance(prev, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark"
                                                            for t in prev.targets) and _is_platform_test(prev.value):
                        return True
        child, cur = cur, parents.get(cur)
    return False


def posix_primitives(text, rel):
    tree, out = _parse(text, rel)
    if tree is None:
        return out
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.attr in POSIX_ONLY.get(n.value.id, ()) and not _guarded(n, parents):
            found.append((n.lineno, f"{n.value.id}.{n.attr} does not exist on Windows; guard it by platform"))
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and not _guarded(n, parents):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for m in names:
                if m.split(".")[0] in POSIX_MODULES and not (isinstance(n, ast.ImportFrom) and n.level):
                    found.append((n.lineno, f"imports {m}, which Windows does not ship; guard it by platform"))
    return out + [f"{rel}:{line}: {why}" for line, why in sorted(found)]


_SHELLS = {"bash", "sh", "zsh"}
_SPAWNS = {"run", "Popen", "check_output", "check_call", "call"}


def bare_shells(text, rel):
    # A subprocess argv starting with a bare shell name: on Windows "bash" is the WSL launcher.
    tree, out = _parse(text, rel)
    if tree is None:
        return out
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _call_name(n) in _SPAWNS and n.args and isinstance(n.args[0], ast.List) \
                and n.args[0].elts and isinstance(n.args[0].elts[0], ast.Constant) \
                and n.args[0].elts[0].value in _SHELLS:
            found.append((n.lineno, f"runs a bare {n.args[0].elts[0].value!r}; resolve the shell's full path first"))
    return out + [f"{rel}:{line}: {why}" for line, why in sorted(found)]


SMALL_FILE = 1 << 20


def grown_files(text, rel):
    # truncate() that grows a file: sparse on APFS and ext4, real clusters on NTFS.
    tree, out = _parse(text, rel)
    if tree is None:
        return out
    for n in sorted(ast.walk(tree), key=lambda n: getattr(n, "lineno", 0)):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "truncate"):
            continue
        is_os = isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"
        args = n.args[1:] if is_os else n.args
        size = args[0] if args else next((k.value for k in n.keywords if k.arg in ("size", "length")), None)
        if size is None or (isinstance(size, ast.Constant) and isinstance(size.value, int)
                            and size.value <= SMALL_FILE):
            continue
        out.append(f"{rel}:{n.lineno}: truncate() grows a file, which allocates every byte on Windows; "
                   "fake the size where it is read")
    return out


TEMPFILES = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}


def text_tempfiles(text, rel):
    # A tempfile opened in text mode with no encoding=: the locale codepage, cp1252, on Windows.
    tree, out = _parse(text, rel)
    if tree is None:
        return out
    for n in sorted(ast.walk(tree), key=lambda n: getattr(n, "lineno", 0)):
        if not (isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) in TEMPFILES):
            continue
        mode = n.args[0] if n.args else next((k.value for k in n.keywords if k.arg == "mode"), None)
        binary = mode is None or (isinstance(mode, ast.Constant) and "b" in str(mode.value))
        if binary or any(k.arg == "encoding" for k in n.keywords):
            continue
        out.append(f"{rel}:{n.lineno}: a text-mode temp file with no encoding= writes cp1252 on Windows")
    return out


def _relative_to(node):
    return isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "relative_to"


def native_relpaths(text, rel):
    # str() or an f-string of Path.relative_to(): backslashes on Windows, so a posix spelling never matches.
    tree, out = _parse(text, rel)
    if tree is None:
        return out
    for n in sorted(ast.walk(tree), key=lambda n: getattr(n, "lineno", 0)):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "str" and n.args and _relative_to(n.args[0]):
            out.append(f"{rel}:{n.lineno}: str() of a relative path is native-separated; use .as_posix()")
        elif isinstance(n, ast.JoinedStr):
            out += [f"{rel}:{n.lineno}: an f-string of a relative path is native-separated; use .as_posix()"
                    for v in n.values if isinstance(v, ast.FormattedValue) and _relative_to(v.value)]
    return out


def unguarded_imports(text, rel):
    # (line, top-level module) for each absolute import no ImportError handler covers.
    tree, _ = _parse(text, rel)
    if tree is None:
        return []
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            names = [n.module]
        else:
            continue
        child, cur, covered = n, parents.get(n), False
        while cur is not None and not covered:
            covered = isinstance(cur, ast.Try) and child in cur.body and any(
                _catches(h, {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})
                for h in cur.handlers)
            child, cur = cur, parents.get(cur)
        if not covered:
            found += [(n.lineno, m.split(".")[0]) for m in names]
    return found


def module_skips(text, rel):
    # Top-level modules a module-level pytest.importorskip makes the whole file skip without.
    tree, _ = _parse(text, rel)
    if tree is None:
        return set()
    out = set()
    for stmt in tree.body:
        value = getattr(stmt, "value", None)
        if isinstance(value, ast.Call) and _call_name(value) == "importorskip" and value.args \
                and isinstance(value.args[0], ast.Constant) and isinstance(value.args[0].value, str):
            out.add(value.args[0].value.split(".")[0])
    return out


def ratchet(found, pin, what):
    if len(found) > pin:
        return (f"{len(found)} {what}, pinned at {pin}: a new one was added. The inventory may only shrink.\n  "
                + "\n  ".join(found))
    if len(found) < pin:
        return f"{len(found)} {what}, pinned at {pin}: some were fixed, so lower the pin to {len(found)}"
    return None


is_python = _files.is_python
is_shell = _files.is_shell


def scan_tree(root, scanner, want, only=None, lister=None):
    root = Path(root)
    out = []
    for path in (lister or _files.publishable)(root):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or not want(path) or (only is not None and not only(rel)):
            continue
        out += scanner(path.read_text(encoding="utf-8", errors="replace"), rel)
    return out
