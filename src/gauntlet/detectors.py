"""Trigger detectors for tier-2 classes: each takes {rel: ast.Module} and finds the sites where a trigger fires."""

import ast
import re
from collections import namedtuple
from pathlib import Path

from gauntlet import files as _files
from gauntlet.registry import default_production
from gauntlet.scanners import tree as _tree

Hit = namedtuple("Hit", "site line why")


def _default_tests(rel):
    return rel.startswith("tests/") and "/fixtures/" not in rel


def sources(root, tests=False, production=default_production, is_test=_default_tests, lister=None):
    root = Path(root)
    out = {}
    for path in (lister or _files.publishable)(root):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or not _files.is_python(path):
            continue
        if production(rel) or (tests and is_test(rel)):
            out[rel] = path.read_text(encoding="utf-8", errors="replace")
    return out


def parse(files):
    trees = {rel: _tree(text)[0] for rel, text in files.items()}
    return {rel: t for rel, t in trees.items() if t is not None}


def _prod(trees, production):
    return {rel: t for rel, t in trees.items() if production(rel)}


def _str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _upper(name):
    return bool(re.fullmatch(r"_?[A-Z][A-Z0-9_]*", name or ""))


_WALKED: dict = {}


def _walk(node):
    # ast.walk as a list, built once per node: every detector walks the same trees.
    got = _WALKED.get(id(node))
    if got is None or got[0] is not node:
        got = _WALKED[id(node)] = (node, list(ast.walk(node)))
    return got[1]


def _scopes(tree):
    # (qualname, node) for every function, so a hit can name the function it sits in.
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                yield name, child
                yield from walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, prefix + child.name + ".")
            else:
                yield from walk(child, prefix)
    yield from walk(tree, "")


def _innermost(tree):
    # node id -> qualname of the innermost function holding it, in one traversal.
    owner = {}
    stack = [(tree, "", None)]
    while stack:
        node, prefix, held = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                owner[id(child)] = name
                stack.append((child, name + ".", name))
                continue
            if held is not None:
                owner[id(child)] = held
            sub = prefix + child.name + "." if isinstance(child, ast.ClassDef) else prefix
            stack.append((child, sub, held))
    return owner


def _assign_targets(tree):
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            yield stmt.targets[0].id, stmt.value, stmt.lineno
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            yield stmt.target.id, stmt.value, stmt.lineno


def closed_tables(trees, production=default_production):
    prod = _prod(trees, production)
    tables = {}
    for rel, tree in prod.items():
        for name, value, line in _assign_targets(tree):
            if (_upper(name) and isinstance(value, ast.Dict) and len(value.keys) >= 3
                    and all(_str(k) is not None for k in value.keys)):
                tables.setdefault(name, []).append((rel, line))
    out = []

    def hit(rel, recv, at, why):
        if isinstance(recv, ast.Name) and recv.id in tables:
            homes, name = [h for h in tables[recv.id] if h[0] == rel], recv.id
        elif isinstance(recv, ast.Attribute) and recv.attr in tables:
            homes, name = _module_homes(recv.value, tables[recv.attr], prod), recv.attr
        else:
            return
        out.extend(Hit(f"{home}:{name}", line, f"{why} at {rel}:{at}") for home, line in homes)

    for rel, tree in prod.items():
        parent = {id(c): p for p in _walk(tree) for c in ast.iter_child_nodes(p)}
        for n in _walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                    and n.args and _str(n.args[0]) is None):
                hit(rel, n.func.value, n.lineno, "a fixed table looked up with a fallback; an unlisted key "
                    "falls through to a default")
            elif isinstance(n, ast.Compare) and _str(n.left) is None:
                for op, table in zip(n.ops, n.comparators):
                    if isinstance(op, (ast.In, ast.NotIn)) and _quiet_miss(n, isinstance(op, ast.NotIn), parent):
                        hit(rel, table, n.lineno, "a fixed table tested for membership; an unlisted key is "
                            "dropped, skipped or defaulted")
    return out


def _module_homes(mod, homes, files):
    # `sweep_check.QUIET` is sweep_check's QUIET, not headless.QUIET; an alias (`bm`) names no file, so keep all.
    if not isinstance(mod, ast.Name):
        return homes
    named = {rel for rel in files if Path(rel).stem == mod.id or mod.id in Path(rel).parts[:-1]}
    return [h for h in homes if h[0] in named] if named else homes


def _quiet_miss(node, negated, parent):
    # Whether an unlisted key takes a quiet path: not a raise, and not kept for a report.
    top = node
    while isinstance(parent.get(id(top)), ast.BoolOp):
        top = parent[id(top)]
    up = parent.get(id(top))
    if isinstance(up, ast.If) and up.test is top:
        branch = up.body if negated else up.orelse
        return not (branch and isinstance(branch[0], ast.Raise))
    return isinstance(up, ast.IfExp) and up.test is top or not negated


_NEWEST = re.compile(r"(?is)\bORDER\s+BY\s+[^;]*?\bDESC\s+LIMIT\s+1\b(?!\s*,|\d)")


def _sql_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else " x " for v in node.values)
    return None


def latest_row_state(trees, production=default_production):
    out = []
    for rel, tree in _prod(trees, production).items():
        owner = _innermost(tree)
        seen = set()
        for n in _walk(tree):
            text = _sql_text(n)
            if text is None or not _NEWEST.search(text):
                continue
            scope = owner.get(id(n), "<module>")
            if scope in seen:
                continue
            seen.add(scope)
            out.append(Hit(f"{rel}:{scope}", n.lineno, "state read from the newest row of an append table"))
    return out


_UPDATE = re.compile(r"(?is)\bUPDATE\s+(\w+)\s+SET\b(.*?)(?:\bWHERE\b(.*))?$")
_STATE_SET = re.compile(r"(?i)(?<![\w.])(?:\w+\.)?(state|status)\s*=(?!=)")


def _state_writes(tree):
    # (node, table, column, guarded) for each UPDATE that sets a state or status column.
    parts = {id(v) for n in _walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for n in _walk(tree):
        text = _sql_text(n) if id(n) not in parts else None
        m = _UPDATE.search(text) if text else None
        if not m:
            continue
        where = m.group(3) or ""
        for col in {c.lower() for c in _STATE_SET.findall(m.group(2))}:
            guarded = re.search(rf"(?i)\b{col}\w*\b", where) is not None
            yield n, m.group(1).lower(), col, guarded


def unguarded_transition(trees, production=default_production):
    out, writers = [], {}
    for rel, tree in _prod(trees, production).items():
        if "/migrations/" in rel:  # a migration is a one-time rewrite, not a transition
            continue
        owner = _innermost(tree)
        for n, table, col, guarded in _state_writes(tree):
            if table != "x":
                writers.setdefault(f"{table}.{col}", {}).setdefault(rel, n.lineno)
            if not guarded:
                out.append(Hit(f"{rel}:{owner.get(id(n), '<module>')}", n.lineno,
                               f"sets {table}.{col} with no WHERE on the {col} it leaves"))
    for key, mods in writers.items():
        if len(mods) > 1:
            rel, line = min(mods.items())
            out.append(Hit(f"table:{key}", line, f"{key} written from {len(mods)} modules: {', '.join(sorted(mods))}"))
    return out


_GUARD = re.compile(r"(?:check|guard|ensure|refuse|require)_\w+")


def _referenced(trees):
    names = {}
    for rel, tree in trees.items():
        for n in _walk(tree):
            name = (n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute)
                    else n.name.split(".")[-1] if isinstance(n, ast.alias) else None)
            if name:
                names.setdefault(name, set()).add(rel)
    return names


def unreached_guards(trees, production=default_production):
    prod = _prod(trees, production)
    refs = _referenced(prod)
    out = []
    for rel, tree in prod.items():
        for name, fn in _scopes(tree):
            short = name.rsplit(".", 1)[-1]
            if not _GUARD.fullmatch(short):
                continue
            if not refs.get(short):
                out.append(Hit(f"{rel}:{short}", fn.lineno, "a guard no production path invokes"))
    return out


def _dest(call):
    flags = [_str(a) for a in call.args if _str(a)]
    longs = [f for f in flags if f.startswith("--")]
    shown = longs[0] if longs else (flags[0] if flags else None)
    dest = next((_str(k.value) for k in call.keywords if k.arg == "dest" and _str(k.value)), None)
    if dest is None and shown:
        dest = shown.lstrip("-").replace("-", "_")
    return dest, shown or dest


def _add_argument_calls(tree):
    for n in _walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument":
            yield n


def ignored_options(trees, production=default_production):
    prod = _prod(trees, production)
    declared = {id(a) for tree in prod.values() for c in _add_argument_calls(tree)
                for a in c.args + [k.value for k in c.keywords if k.arg == "dest"]}
    read = set()
    for tree in prod.values():
        for n in _walk(tree):
            if isinstance(n, ast.Attribute):
                read.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in declared:
                read.add(n.value)
    out = []
    for rel, tree in prod.items():
        for n in _add_argument_calls(tree):
            dest, shown = _dest(n)
            if dest and dest not in read:
                out.append(Hit(f"{rel}:{shown}", n.lineno, f"parsed into `{dest}` and never read"))
    return out


_PER_MEMBER = re.compile(r"(?i)ctx|context|timeout|memory|ceiling|budget|batch|steps|max_tokens|threads|"
                         r"(?:^|_)gb(?:_|$)|temperature|reserve|wait")


def member_defaults(trees, production=default_production):
    out = []
    for rel, tree in _prod(trees, production).items():
        for name, fn in _scopes(tree):
            args = fn.args
            pos = args.posonlyargs + args.args
            pairs = list(zip(pos[len(pos) - len(args.defaults):], args.defaults))
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
            for arg, d in pairs:
                const = d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else None
                if not _upper(const):
                    continue
                if _PER_MEMBER.search(arg.arg) or _PER_MEMBER.search(const):
                    out.append(Hit(f"{rel}:{name.rsplit('.', 1)[-1]}({arg.arg})", d.lineno,
                                   f"one constant {const} defaults a value that differs per model or machine"))
    return out


TERMINAL_VERDICTS = {"broken", "declined", "failed"}
_EXIT = re.compile(r"returncode|exit_?code|\bexited\b")


def _verdict_literals(nodes):
    for n in nodes:
        for m in _walk(n):
            if isinstance(m, ast.Constant) and m.value in TERMINAL_VERDICTS:
                yield m


def harness_verdicts(trees, production=default_production):
    out = []
    for rel, tree in _prod(trees, production).items():
        for name, fn in _scopes(tree):
            found = None
            for n in _walk(fn):
                if isinstance(n, ast.ExceptHandler):
                    found = found or next(_verdict_literals(n.body), None)
                elif isinstance(n, (ast.If, ast.IfExp)) and _EXIT.search(ast.unparse(n.test)):
                    body = n.body if isinstance(n.body, list) else [n.body]
                    orelse = n.orelse if isinstance(n.orelse, list) else [n.orelse]
                    found = found or next(_verdict_literals(body + orelse), None)
            inner = {id(m) for _, sub in _scopes(fn) for m in _walk(sub)}
            if found is not None and id(found) not in inner:
                out.append(Hit(f"{rel}:{name}", found.lineno,
                               "a terminal verdict written from an exception or exit status, which may be the "
                               "harness's fault rather than the subject's"))
    return out


def _literal_set(value):
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("frozenset", "set",
                                                                                              "tuple") \
            and len(value.args) == 1:
        value = value.args[0]
    if not isinstance(value, (ast.Tuple, ast.List, ast.Set)) or len(value.elts) < 2:
        return None
    items = [_str(e) for e in value.elts]
    if any(i is None for i in items):
        return None
    return frozenset(items)


def duplicated_literals(trees, production=default_production):
    by_value = {}
    for rel, tree in _prod(trees, production).items():
        for name, value, line in _assign_targets(tree):
            items = _literal_set(value)
            if _upper(name) and items and len(items) >= 2:
                by_value.setdefault(items, []).append((rel, name, line))
    out = []
    for items, homes in by_value.items():
        if len({rel for rel, _, _ in homes}) < 2:
            continue
        for rel, name, line in homes:
            others = ", ".join(f"{r}:{n}" for r, n, _ in homes if (r, n) != (rel, name))
            out.append(Hit(f"{rel}:{name}", line, f"the same list is written down in {others}"))
    return out


# Set by the OS or the shell, so there is no producer in this repo to agree with.
COMMON_ENV = {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PWD",
              "TERM", "CI", "GITHUB_ACTIONS", "VIRTUAL_ENV", "PYTHONPATH", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
              "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "COLUMNS", "NO_COLOR"}


def _env_name(n):
    if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and _is_environ(n.value):
        return _str(n.slice)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.args:
        if n.func.attr == "get" and _is_environ(n.func.value):
            return _str(n.args[0])
        if n.func.attr == "getenv" and isinstance(n.func.value, ast.Name) and n.func.value.id == "os":
            return _str(n.args[0])
    return None


def _is_environ(node):
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ")


def env_seams(trees, production=default_production):
    first = {}
    for rel, tree in sorted(_prod(trees, production).items()):
        for n in _walk(tree):
            name = _env_name(n)
            if name and name not in COMMON_ENV and name not in first:
                first[name] = (rel, n.lineno)
    return [Hit(f"env:{name}", line, f"read at {rel}; whoever sets it is the other half of the seam")
            for name, (rel, line) in first.items()]


_LOOPBACK = re.compile(r"https?://(?:127\.0\.0\.1|localhost)\b")
_EQUALITY = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


def loopback_equality(trees, production=default_production):
    prod = _prod(trees, production)
    names = {name for tree in prod.values() for name, value, _ in _assign_targets(tree)
             if _upper(name) and _LOOPBACK.match(_str(value) or "")}

    def named(node):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return next((got for e in node.elts if (got := named(e))), None)
        if isinstance(node, ast.Name) and node.id in names:
            return node.id
        if isinstance(node, ast.Attribute) and node.attr in names:
            return node.attr
        text = _str(node)
        return text if text and _LOOPBACK.match(text) else None
    out = []
    for rel, tree in sorted(prod.items()):
        owner = _innermost(tree)
        for n in _walk(tree):
            if not isinstance(n, ast.Compare) or not any(isinstance(op, _EQUALITY) for op in n.ops):
                continue
            got = next((g for g in map(named, [n.left, *n.comparators]) if g), None)
            if got:
                out.append(Hit(f"{rel}:{owner.get(id(n), '<module>')}", n.lineno,
                               f"compares an address to {got}; ask the config or the server which engine it is"))
    return out


def _names(node):
    return {n.id for n in _walk(node) if isinstance(n, ast.Name)}


def row_index_keys(trees, production=default_production):
    # A dict keyed on a string holding an enumerate() index pairs rows by list position, not by identity.
    out = []
    for rel, tree in _prod(trees, production).items():
        owner = _innermost(tree)
        for loop in _walk(tree):
            if not (isinstance(loop, (ast.For, ast.comprehension)) and isinstance(loop.iter, ast.Call)
                    and isinstance(loop.iter.func, ast.Name) and loop.iter.func.id == "enumerate"
                    and isinstance(loop.target, ast.Tuple) and isinstance(loop.target.elts[0], ast.Name)):
                continue
            index = loop.target.elts[0].id
            body = loop.body if isinstance(loop, ast.For) else []
            for n in (x for stmt in body for x in _walk(stmt)):
                key = n.slice if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) else None
                if isinstance(key, ast.JoinedStr) and index in _names(key):
                    out.append(Hit(f"{rel}:{owner.get(id(loop), '<module>')}", n.lineno,
                                   "a key built from a row's position pairs two lists only if they share an order"))
    return out


_MOVE_NAME = re.compile(r"_?(?:[A-Z0-9_]*_)?(?:MOVES|TRANSITIONS)")


def _state_ref(node):
    return (isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str))) or isinstance(
        node, (ast.Name, ast.Attribute))


def _state_pair(node):
    return isinstance(node, ast.Tuple) and len(node.elts) == 2 and all(_state_ref(e) for e in node.elts)


def _unwrap(value):
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id in ("frozenset", "set", "dict") and len(value.args) == 1 and not value.keywords):
        return value.args[0]
    return value


def _setlike(node):
    empty = (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set")
             and not node.args)
    return empty or isinstance(_unwrap(node), (ast.Set, ast.List, ast.Tuple))


def _move_shaped(value):
    value = _unwrap(value)
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return bool(value.elts) and all(_state_pair(e) for e in value.elts)
    if isinstance(value, ast.Dict):
        keys = [k for k in value.keys if k is not None]
        if keys and all(_state_pair(k) for k in keys):
            return True
        return bool(keys) and all(_state_ref(k) for k in keys) and all(_setlike(v) for v in value.values)
    return False


def move_tables(trees, production=default_production):
    """Module constants named like a move table whose value is a set of (leaves, enters) pairs or a table of them."""
    out = []
    for rel, tree in _prod(trees, production).items():
        for name, value, line in _assign_targets(tree):
            if _MOVE_NAME.fullmatch(name) and _move_shaped(value):
                out.append(Hit(f"{rel}:{name}", line, "a declared move table"))
    return out


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own(node):
    # Every node under `node` that is not inside a nested function or class.
    out, todo = [], list(ast.iter_child_nodes(node))
    while todo:
        n = todo.pop()
        if isinstance(n, _DEFS):
            continue
        out.append(n)
        todo.extend(ast.iter_child_nodes(n))
    return sorted(out, key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))


def _call_name(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _bodies(tree):
    # (qualname, node) for the module and each function, so a scan of own nodes covers every statement once.
    yield "<module>", tree
    yield from _scopes(tree)


def _kw(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _false(node):
    return isinstance(node, ast.Constant) and node.value in (False, None, 0)


_LINK_SQL = re.compile(r"(?is)^\s*(?:INSERT|UPDATE|REPLACE)\b")
_DEDUP_SQL = re.compile(r"(?is)\bON\s+CONFLICT\b|\bINSERT\s+OR\s+(?:IGNORE|REPLACE)\b|^\s*REPLACE\b")
_DEDUP_KW = {"dedup", "dedupe", "dedup_key", "idempotency_key", "unique_key"}


def _committers(prod, extra):
    names = set(extra)
    for tree in prod.values():
        for name, fn in _scopes(tree):
            if any(isinstance(n, ast.Call) and _call_name(n) == "commit" and not n.args for n in _own(fn)):
                names.add(name.rsplit(".", 1)[-1])
    return names


def commit_then_link(trees, production=default_production, committers=()):
    """A helper that commits, whose returned id is then written by a second write: a death between leaves one."""
    prod = _prod(trees, production)
    known = _committers(prod, committers)
    out = []
    for rel, tree in prod.items():
        for name, fn in _bodies(tree):
            short = name.rsplit(".", 1)[-1]
            nodes = _own(fn)
            local = set(known) - {short}
            for n in nodes:
                if isinstance(n, ast.Assign) and not isinstance(n.value, ast.Call) and any(
                        isinstance(r, (ast.Name, ast.Attribute)) and (getattr(r, "id", None) or r.attr) in local
                        for r in _walk(n.value)):
                    local |= {t.id for t in n.targets if isinstance(t, ast.Name)}

            def commits(call):
                if not (isinstance(call, ast.Call) and _call_name(call) in local):
                    return False
                deferred = _kw(call, "commit")
                return not (deferred is not None and _false(deferred)) and not any(
                    k.arg in _DEDUP_KW for k in call.keywords)
            tainted, first = set(), None
            for n in nodes:
                value = n.value if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) else None
                if value is not None and any(commits(m) for m in _walk(value)):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    tainted |= {x.id for t in targets for x in _walk(t) if isinstance(x, ast.Name)}
                    first = first or n.lineno
                elif (isinstance(n, ast.Call) and _call_name(n) in ("append", "add", "extend")
                      and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and any(commits(m) for a in n.args for m in _walk(a))):
                    tainted.add(n.func.value.id)
                    first = first or n.lineno
            if not tainted:
                continue
            for n in nodes:
                if not isinstance(n, ast.Call) or n.lineno < first:
                    continue
                rest = [a for a in n.args[1:]] + [k.value for k in n.keywords]
                sql = _sql_text(n.args[0]) if n.args else None
                link = (_call_name(n) in ("execute", "executemany") and sql and _LINK_SQL.search(sql)
                        and not _DEDUP_SQL.search(sql) and any(tainted & _names(a) for a in rest))
                link = link or (commits(n) and any(tainted & _names(a) for a in n.args + [k.value for k in n.keywords]))
                if link:
                    out.append(Hit(f"{rel}:{name}", n.lineno, "a committed id written by a second, separate write; "
                                   "a death between the two leaves the first with nothing pointing at it"))
                    break
    return out


_STOPS_ON = {"ValueError", "JSONDecodeError"}


def _stop_reader(fn):
    for loop in (n for n in _own(fn) if isinstance(n, (ast.For, ast.While))):
        for t in (n for n in _own(loop) if isinstance(n, ast.Try)):
            if not any(isinstance(m, ast.Call) and _call_name(m) == "loads" for b in t.body for m in _walk(b)):
                continue
            for h in t.handlers:
                caught = {x.id if isinstance(x, ast.Name) else x.attr for x in _walk(h.type or ast.Name("BaseException"))
                          if isinstance(x, (ast.Name, ast.Attribute))}
                if caught & _STOPS_ON and any(isinstance(s, (ast.Break, ast.Return)) for b in h.body for s in _walk(b)):
                    return True
    return False


def _mode(call):
    m = _kw(call, "mode")
    if m is None:
        pos = 1 if isinstance(call.func, ast.Name) else 0
        m = call.args[pos] if len(call.args) > pos else None
    return _str(m) or ""


def _appends(fn):
    return any(isinstance(n, ast.Call) and _call_name(n) == "open" and "a" in _mode(n) and "+" not in _mode(n)
               for n in _own(fn))


def _repairs(fn):
    for n in _own(fn):
        if not isinstance(n, ast.Call):
            continue
        if _call_name(n) in ("seek", "truncate"):
            return True
        if _call_name(n) == "endswith" and n.args and isinstance(n.args[0], ast.Constant) \
                and n.args[0].value in ("\n", b"\n"):
            return True
    return False


def torn_append(trees, production=default_production):
    """An appender to a line-per-record file whose reader stops at the first bad line: a torn line hides the rest."""
    out = []
    for rel, tree in _prod(trees, production).items():
        scopes = list(_scopes(tree))
        if not any(_stop_reader(fn) for _, fn in scopes):
            continue
        for name, fn in scopes:
            if _appends(fn) and not _repairs(fn):
                out.append(Hit(f"{rel}:{name}", fn.lineno, "appends after a possibly torn last line, and the "
                               "reader in this module stops at the first line it cannot parse"))
    return out


def _new_session(call):
    if _call_name(call) != "Popen":
        return False
    sess, pre, group = _kw(call, "start_new_session"), _kw(call, "preexec_fn"), _kw(call, "process_group")
    if sess is not None and not _false(sess):
        return True
    if pre is not None and "setsid" in ast.unparse(pre):
        return True
    if group is not None and not (isinstance(group, ast.Constant) and group.value is None):
        return True
    argv = call.args[0] if call.args else _kw(call, "args")
    return isinstance(argv, (ast.List, ast.Tuple)) and bool(argv.elts) and _str(argv.elts[0]) == "setsid"


_STOP_NAME = re.compile(r"(?i)stop|kill|term|reap|shutdown|close")


def _installs_term(node):
    nodes = _walk(node)
    return (any(isinstance(n, ast.Call) and _call_name(n) == "signal" for n in nodes)
            and any((isinstance(n, ast.Name) and n.id == "SIGTERM") or (isinstance(n, ast.Attribute)
                    and n.attr == "SIGTERM") for n in nodes))


def _dotted(rel):
    mod = rel[:-3] if rel.endswith(".py") else rel
    parts = mod.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names = {".".join(parts)}
    for i in range(1, len(parts)):
        names.add(".".join(parts[i:]))
    return names


def _imports(rel, tree, by_name):
    pkg = rel.split("/")[:-1]
    out = set()
    for n in _walk(tree):
        mods = []
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                up = pkg[:len(pkg) - (n.level - 1)] if n.level > 1 else pkg
                base = ".".join(up + ([base] if base else []))
            mods = [base] + [f"{base}.{a.name}" if base else a.name for a in n.names]
        for m in mods:
            while m:
                if m in by_name:
                    out |= by_name[m]
                    break
                m = m.rpartition(".")[0]
    return out


def _entry(rel, tree):
    return rel.endswith("__main__.py") or any(
        isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "__name__"
        and any(_str(c) == "__main__" for c in n.comparators) for n in _walk(tree))


def _late(fn_name, node, finals):
    return fn_name.rsplit(".", 1)[-1] in ("__exit__", "__aexit__", "__del__") or id(node) in finals


def _finals(fn):
    return {id(m) for t in _own(fn) if isinstance(t, ast.Try) for s in t.finalbody for m in _walk(s)}


def _atexit_names(tree):
    return {ast.unparse(n.args[0]).rsplit(".", 1)[-1] for n in _walk(tree)
            if isinstance(n, ast.Call) and _call_name(n) == "register" and n.args
            and isinstance(n.func, ast.Attribute) and ast.unparse(n.func.value) == "atexit"}


def _stops(fn, handle):
    for n in _own(fn):
        if not isinstance(n, ast.Call):
            continue
        name = _call_name(n) or ""
        recv = n.func.value if isinstance(n.func, ast.Attribute) else None
        on_handle = recv is not None and handle(recv) and name in ("terminate", "kill", "send_signal")
        passes = _STOP_NAME.search(name) and any(handle(x) for a in n.args for x in _walk(a))
        if on_handle or passes:
            yield n


def _orphan_popens(rel, tree, protected_fn):
    atexits = _atexit_names(tree)
    owner = _innermost(tree)
    scopes = dict(_scopes(tree))
    parent = {id(c): p for p in _walk(tree) for c in ast.iter_child_nodes(p)}
    for call in (n for n in _walk(tree) if isinstance(n, ast.Call) and _new_session(n)):
        name = owner.get(id(call), "<module>")
        fn = scopes.get(name, tree)
        up = parent.get(id(call))
        var = attr = None
        if isinstance(up, ast.Assign) and len(up.targets) == 1:
            t = up.targets[0]
            var = t.id if isinstance(t, ast.Name) else None
            attr = t.attr if isinstance(t, ast.Attribute) else None
        elif isinstance(up, ast.withitem) and isinstance(up.optional_vars, ast.Name):
            var = up.optional_vars.id
        if var is None and attr is None:
            continue
        if attr is not None:
            def handle(x, a=attr):
                return isinstance(x, ast.Attribute) and x.attr == a
            homes = list(scopes.items())
        else:
            def handle(x, v=var):
                return isinstance(x, ast.Name) and x.id == v
            homes = [(name, fn)]
        stops = [(h, s) for h, f in homes for s in _stops(f, handle)]
        if not stops or protected_fn(call, fn, parent):
            continue
        if all(_late(h, s, _finals(scopes.get(h, tree))) or h.rsplit(".", 1)[-1] in atexits for h, s in stops):
            yield Hit(f"{rel}:{name}", call.lineno, "a child in its own session stopped only on the way out "
                      "(finally, __exit__, atexit); a SIGTERM or SIGKILL to the owner skips that and leaves the child")


_MARK_WRITE = ("write_text", "write_bytes", "touch")
_OWNED = re.compile(r"(?i)pid|expir|deadline|ttl|until|lease")


def _mark_key(recv):
    if isinstance(recv, ast.Name):
        return recv.id
    if isinstance(recv, ast.Attribute):
        return recv.attr
    if isinstance(recv, ast.BinOp) and isinstance(recv.op, ast.Div):
        return _str(recv.right)
    return None


def _marker_files(prod):
    writes, lifts, moved = {}, {}, set()
    for rel, tree in sorted(prod.items()):
        for name, fn in _bodies(tree):
            for n in _own(fn):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                    continue
                key = _mark_key(n.func.value)
                if not key:
                    continue
                if n.func.attr in _MARK_WRITE:
                    owned = any(_OWNED.search(x.id if isinstance(x, ast.Name) else x.attr if isinstance(
                        x, ast.Attribute) else _str(x) or "") for x in _walk(fn))
                    writes.setdefault(key, []).append((rel, name, n.lineno, owned))
                elif n.func.attr == "unlink":
                    lifts.setdefault(key, set()).add((rel, name))
                elif n.func.attr in ("replace", "rename"):
                    moved.add(key)
    for key, rows in writes.items():
        lifted = lifts.get(key, set())
        homes = {rel for rel, *_ in rows}
        if key in moved or not {r for r, _ in lifted} & homes or any(owned for *_, owned in rows) or len(lifted) > 1:
            continue
        rel, _, line, _ = rows[0]
        lifter = ":".join(next(iter(lifted)))
        yield Hit(f"{rel}:{key}", line, f"a marker file with no pid or expiry, lifted only by {lifter}; "
                  "if that path does not run, the marker stays")


def orphanable_child(trees, production=default_production):
    """A new-session child whose only stop runs on the way out, reached from an entry point with no SIGTERM handler;
    and a marker file with no owner pid or expiry that one path alone lifts."""
    prod = _prod(trees, production)
    by_name = {}
    for rel in prod:
        for d in _dotted(rel):
            by_name.setdefault(d, set()).add(rel)
    edges = {rel: _imports(rel, tree, by_name) for rel, tree in prod.items()}
    term_fns = {name.rsplit(".", 1)[-1] for tree in prod.values() for name, fn in _scopes(tree) if _installs_term(fn)}

    def reach(start):
        seen, todo = set(), [start]
        while todo:
            r = todo.pop()
            if r not in seen:
                seen.add(r)
                todo.extend(edges.get(r, ()))
        return seen
    entries = {rel: (reach(rel), _installs_term(tree)) for rel, tree in prod.items() if _entry(rel, tree)}

    out = []
    for rel, tree in prod.items():
        def protected(call, fn, parent, rel=rel):
            if _installs_term(fn):
                return True
            node = call
            while id(node) in parent:
                node = parent[id(node)]
                if isinstance(node, (ast.With, ast.AsyncWith)) and any(
                        isinstance(i.context_expr, ast.Call) and _call_name(i.context_expr) in term_fns
                        for i in node.items):
                    return True
            reaching = [term for closure, term in entries.values() if rel in closure]
            return bool(reaching) and all(reaching)
        out.extend(_orphan_popens(rel, tree, protected))
    out.extend(_marker_files(prod))
    return out


_CLOCKS = {"time", "monotonic", "perf_counter", "monotonic_ns", "time_ns", "perf_counter_ns", "now", "utcnow"}
_DEADLINE_PARAM = re.compile(r"(?i)deadline|until|stop_at|expires?|timeout")


def _clocked(node, clock_vars):
    return any((isinstance(n, ast.Call) and _call_name(n) in _CLOCKS) or (isinstance(n, ast.Name)
               and n.id in clock_vars) for n in _walk(node))


def _waits(loop):
    for n in _own(loop):
        if not isinstance(n, ast.Call):
            continue
        name = _call_name(n)
        if name in ("sleep", "wait"):
            return True
        if name == "get" and isinstance(n.func, ast.Attribute) and re.search(r"(?i)queue|^q$|_q$",
                                                                             ast.unparse(n.func.value)):
            return True
    return False


def undeadlined_wait(trees, production=default_production):
    """A while loop that sleeps or waits with no clock comparison and no capped counter: it can wait forever."""
    out = []
    for rel, tree in _prod(trees, production).items():
        for name, fn in _bodies(tree):
            nodes = _own(fn)
            args = fn.args if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
            params = {a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs if args else [])
                      if _DEADLINE_PARAM.search(a.arg)}
            for loop in (n for n in nodes if isinstance(n, ast.While)):
                if not _waits(loop):
                    continue
                clock_vars = set(params)
                for n in nodes:
                    if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.lineno < loop.lineno and n.value is not None \
                            and _clocked(n.value, clock_vars):
                        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                        clock_vars |= {x.id for t in targets for x in _walk(t) if isinstance(x, ast.Name)}
                body = _own(loop)
                counters = {n.target.id for n in body if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name)}
                compares = [c for c in [*_walk(loop.test), *body] if isinstance(c, ast.Compare)]
                if any(_clocked(c, clock_vars) or counters & _names(c) for c in compares):
                    continue
                out.append(Hit(f"{rel}:{name}", loop.lineno, "a loop that waits with no deadline and no cap; "
                               "nothing ends it but the condition it waits on"))
                break
    return out


def _consts(tree):
    return {name: _str(v) for name, v, _ in _assign_targets(tree) if _str(v) is not None}


def _pairs(value):
    value = _unwrap(value)
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return [(e.elts[0], e.elts[1]) for e in value.elts if _state_pair(e)]
    if isinstance(value, ast.Dict):
        keys = [k for k in value.keys if k is not None]
        if keys and all(_state_pair(k) for k in keys):
            return [(k.elts[0], k.elts[1]) for k in keys]
        out = []
        for k, v in zip(value.keys, value.values):
            inner = _unwrap(v)
            out += [(k, e) for e in getattr(inner, "elts", [])]
            if not getattr(inner, "elts", None):
                out.append((k, None))
        return out
    return []


def _table_states(rel, tree, name, value, consts):
    # {state: frozenset(next)} and {state: names that stand for it} from a move table's literal.
    def key(node):
        if node is None:
            return None, set()
        if isinstance(node, ast.Constant):
            return node.value, set()
        ident = node.id if isinstance(node, ast.Name) else node.attr
        return consts.get(ident, ident), {ident}
    table, names = {}, {}
    for frm, to in _pairs(value):
        (f, fn), (t, tn) = key(frm), key(to)
        names.setdefault(f, set()).update(fn)
        table.setdefault(f, set())
        if to is not None:
            names.setdefault(t, set()).update(tn)
            table[f].add(t)
            table.setdefault(t, set())
    return table, names


def _registered_tables(tables, prod):
    from gauntlet import moves
    for t in tables:
        rel, _, name = t.site.partition(":")
        tree = prod.get(rel)
        consts = _consts(tree) if tree is not None else {}
        table = moves.normalize(t.moves)
        names = {s: {c for c, v in consts.items() if v == s} for s in table}
        line = next((ln for n, _, ln in _assign_targets(tree) if n == name), 1) if tree is not None else 1
        yield rel, name, line, table, names


def _found_tables(prod):
    for rel, tree in prod.items():
        consts = _consts(tree)
        for name, value, line in _assign_targets(tree):
            if _MOVE_NAME.fullmatch(name) and _move_shaped(value):
                table, names = _table_states(rel, tree, name, value, consts)
                yield rel, name, line, table, names


def _refs(node, state, names):
    return [n for n in _walk(node) if (isinstance(n, ast.Constant) and n.value == state and isinstance(state, str))
            or (isinstance(n, ast.Name) and n.id in names) or (isinstance(n, ast.Attribute) and n.attr in names)]


_WHERE = re.compile(r"(?is)\bWHERE\b(.*)$")


def _uses(tree, state, names, skip):
    # (reads, writes): a read compares against the state or filters on it in SQL; any other mention is a write.
    reads = writes = 0
    counted = {id(v) for n in _walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for n in _walk(tree):
        if id(n) in skip or id(n) in counted:
            continue
        if isinstance(n, ast.Compare) or isinstance(n, ast.match_case):
            got = _refs(n if isinstance(n, ast.Compare) else n.pattern, state, names)
            if got:
                reads += 1
                counted |= {id(g) for g in got}
            continue
        text = _sql_text(n)
        if text and isinstance(state, str) and f"'{state}'" in text:
            where = _WHERE.search(text)
            if where and f"'{state}'" in where.group(1):
                reads += 1
            else:
                writes += 1
            counted.add(id(n))
            continue
        if isinstance(n, (ast.Constant, ast.Name, ast.Attribute)) and _refs(n, state, names) and id(n) not in counted:
            if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)):
                writes += 1
            counted.add(id(n))
    return reads, writes


def terminal_without_reader(trees, production=default_production, tables=None):
    """A state no move leaves whose only mentions are in the modules that write it: nothing settles, expires or
    reports a row that reaches it. `tables` are registered MoveTables; without them the declared tables are read."""
    prod = _prod(trees, production)
    found = list(_registered_tables(tables, prod)) if tables else list(_found_tables(prod))
    out = []
    for rel, name, line, table, names in found:
        home = prod.get(rel)
        decl = set()
        if home is not None:
            decl = {id(m) for n, v, _ in _assign_targets(home) if n == name or n in set().union(*names.values())
                    for m in _walk(v)}
        for state in sorted((s for s, nxt in table.items() if not nxt and s is not None), key=str):
            readers, writers = set(), {rel}
            for other, tree in prod.items():
                r, w = _uses(tree, state, names.get(state, set()), decl if other == rel else set())
                if w:
                    writers.add(other)
                if r:
                    readers.add(other)
            if not readers - writers:
                out.append(Hit(f"{rel}:{name}:{state}", line, "no move leaves this state and nothing outside the "
                               "modules that write it reads it: a row that lands here is never settled or reported"))
    return out


_SQL_START = re.compile(r"(?s)^\s*(?:SELECT|UPDATE|DELETE|WITH|INSERT)\b")
_BY_ID = re.compile(r"(?is)\bWHERE\s+(?:\w+\.)?id\s*(?:=|\bIN\b)")
_CREATE = re.compile(r"(?is)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)")


def _concat(node, hole=None):
    # A string chain's text, with each interpolated or concatenated expression kept as its source (or as `hole`).
    def fill(e):
        return hole if hole is not None else f" {ast.unparse(e)} "
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else fill(v.value) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _concat(node.left, hole), _concat(node.right, hole)
        if left is None and right is None:
            return None
        return (left if left is not None else fill(node.left)) + (right if right is not None else fill(node.right))
    return None


def _strings(tree, hole=None):
    # (qualname, line, text) for each outermost string expression: a chain is read whole, never part by part.
    owner = _innermost(tree)
    inner = set()
    out = []
    for n in _walk(tree):
        if id(n) in inner or not isinstance(n, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            continue
        text = _concat(n, hole)
        if text is None:
            continue
        inner |= {id(m) for m in _walk(n) if m is not n}
        out.append((owner.get(id(n), "<module>"), n.lineno, text))
    return out


def _word(names):
    return re.compile(r"(?<![\w.])(?:\w+\.)*(?:" + "|".join(map(re.escape, names)) + r")\b") if names else None


def _declared_scoped(prod, columns):
    col = _word(columns)
    out = set()
    for tree in prod.values():
        for _, _, text in _strings(tree):
            parts = _CREATE.split(text)
            for name, body in zip(parts[1::2], parts[2::2]):
                if col.search(body):
                    out.add(name.lower())
    return out


def _helper_named(rel, qual, helpers):
    return any(h == f"{rel}:{qual}" or h == qual for h in helpers)


def unscoped_query(trees, production=default_production, columns=(), tables=(), helpers=(), null_helper=None):
    """A query on a table several scopes share that does not filter on the scope; and a scope's NULL meaning written
    anywhere but the one helper named for it. `columns` name the scope columns; `tables` add scoped tables to those
    whose CREATE TABLE names a scope column; `helpers` are fragments that stand for the scope predicate."""
    if not columns:
        return []
    prod = _prod(trees, production)
    scoped = {t.lower() for t in tables} | _declared_scoped(prod, columns)
    scope = _word(tuple(columns) + tuple(helpers))
    null = re.compile(r"(?i)(?<![\w.])(?:\w+\.)?(" + "|".join(map(re.escape, columns)) + r")\s+IS\s+(?:NOT\s+)?NULL\b")
    refs = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?i:(" + "|".join(map(re.escape, sorted(scoped))) + r"))\b") \
        if scoped else None
    out = []
    for rel, tree in prod.items():
        for qual, line, text in _strings(tree):
            if null.search(text) and not (null_helper and _helper_named(rel, qual, [null_helper])):
                out.append(Hit(f"{rel}:{qual}:null-scope", line, "the scope's NULL meaning written outside the one "
                               "helper that owns it; another copy can give an unstamped row another meaning"))
            if refs is None or not _SQL_START.search(text) or _CREATE.search(text):
                continue
            if re.match(r"(?s)\s*INSERT\b(?!.*\bSELECT\b)", text):
                continue
            if scope.search(text) or _BY_ID.search(text):
                continue
            for table in sorted({m.lower() for m in refs.findall(text)}):
                out.append(Hit(f"{rel}:{qual}:{table}", line, f"a query on {table}, which several scopes share, with "
                               "no scope predicate: it reads or writes every scope's rows"))
    return out


_FROM_STORE = {"read_text", "read_bytes", "read", "readline", "readlines", "load", "loads", "safe_load",
               "fetchone", "fetchall", "fetchmany", "execute", "open"}
_IDENTITY_CALLS = {"getpgid", "create_time", "cmdline", "Process", "exe"}
_IDENTITY = re.compile(r"(?i)pgid|start_?time|started|create_time|cmdline|command|\bcomm\b|boot")


def _module_aliases(tree):
    return {(a.asname or a.name).split(".")[0] for n in _walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in n.names}


def _stored_names(fn, readers=frozenset(), modules=frozenset()):
    # Names whose value came from a file, a row or a parsed record, followed through assignments, loops and withs.
    # A repo function that reads counts only when called by name or through a module, never as any object's method.
    tainted = set()

    def source(call):
        name = _call_name(call)
        if name in _FROM_STORE:
            return True
        f = call.func
        return name in readers and (isinstance(f, ast.Name) or (
            isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in modules))

    def stored(node):
        return any((isinstance(m, ast.Call) and source(m))
                   or (isinstance(m, ast.Name) and m.id in tainted) for m in _walk(node))
    nodes = _own(fn)
    for _ in range(3):
        before = len(tainted)
        for n in nodes:
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None and stored(n.value):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                tainted |= {x.id for t in targets for x in _walk(t) if isinstance(x, ast.Name)}
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and stored(n.iter):
                tainted |= {x.id for x in _walk(n.target) if isinstance(x, ast.Name)}
            elif isinstance(n, ast.withitem) and n.optional_vars is not None and stored(n.context_expr):
                tainted |= {x.id for x in _walk(n.optional_vars) if isinstance(x, ast.Name)}
            elif isinstance(n, ast.NamedExpr) and stored(n.value):
                tainted.add(n.target.id)
        if len(tainted) == before:
            break
    return stored


def _checks_identity(fn, helpers=frozenset()):
    for n in _own(fn):
        if isinstance(n, ast.Call) and (_call_name(n) in _IDENTITY_CALLS or _call_name(n) in helpers):
            return True
        if isinstance(n, ast.Compare) and any(
                _IDENTITY.search(m.id if isinstance(m, ast.Name) else m.attr if isinstance(m, ast.Attribute)
                                 else _str(m) or "") for m in _walk(n)):
            return True
    return False


def _kill_zero(call):
    return (isinstance(call, ast.Call) and _call_name(call) == "kill" and len(call.args) == 2
            and isinstance(call.args[1], ast.Constant) and call.args[1].value == 0)


def _alive_helpers(prod):
    out = set()
    for tree in prod.values():
        for name, fn in _scopes(tree):
            params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
            if not _checks_identity(fn) and any(
                    _kill_zero(n) and isinstance(n.args[0], ast.Name) and n.args[0].id in params for n in _own(fn)):
                out.add(name.rsplit(".", 1)[-1])
    return out


def stored_pid_liveness(trees, production=default_production, alive=()):
    """A pid read back from a file or row and checked only for liveness: a reused pid reads as the same process.
    `alive` names liveness helpers the scan cannot see; a helper that only does os.kill(pid, 0) is found."""
    prod = _prod(trees, production)
    helpers = _alive_helpers(prod) | set(alive)
    readers = {name.rsplit(".", 1)[-1] for tree in prod.values() for name, fn in _scopes(tree)
               if any(isinstance(n, ast.Return) and n.value is not None for n in _own(fn))
               and any(isinstance(n, ast.Call) and _call_name(n) in _FROM_STORE for n in _own(fn))} - helpers
    checkers = {name.rsplit(".", 1)[-1] for tree in prod.values() for name, fn in _scopes(tree) if _checks_identity(fn)}
    out = []
    for rel, tree in prod.items():
        modules = _module_aliases(tree)
        for name, fn in _bodies(tree):
            stored = None
            for n in _own(fn):
                if not isinstance(n, ast.Call):
                    continue
                if _kill_zero(n):
                    pid = n.args[0]
                elif _call_name(n) in helpers and n.args:
                    pid = n.args[0]
                else:
                    continue
                stored = stored or _stored_names(fn, readers, modules)
                if stored(pid) and not _checks_identity(fn, checkers):
                    out.append(Hit(f"{rel}:{name}", n.lineno, "a stored pid checked only for liveness; once it is "
                                   "reused, any process under that pid reads as the one that was recorded"))
                    break
    return out


_CACHE_DECOS = {"cache", "lru_cache", "cached_property"}
_SUBPROCESS = {"run", "check_output", "check_call", "call", "Popen", "getoutput", "getstatusoutput"}
_STAMP = re.compile(r"(?i)mtime|version|stamp|digest|sha\d*$|etag|ctime|size|generation|^rev$|_rev$")


def _cache_deco(fn):
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else None
        if name in _CACHE_DECOS:
            return True
    return False


def _reads_source(fn, readers=()):
    for n in _own(fn):
        if not isinstance(n, ast.Call):
            continue
        name = _call_name(n)
        recv = ast.unparse(n.func.value) if isinstance(n.func, ast.Attribute) else ""
        if (name == "open" and isinstance(n.func, ast.Name)) or name in ("read_text", "read_bytes") \
                or name in ("getoutput", "check_output", "popen") or name in readers \
                or (name in _SUBPROCESS and recv == "subprocess"):
            return True
    return False


def _params(fn):
    a = fn.args
    return [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs + [v for v in (a.vararg, a.kwarg) if v]
            if x.arg not in ("self", "cls")]


def _memo_global(fn):
    # A function that keeps its answer in a module global: `global X`, an assignment to X, and a return of X.
    names = {g for n in _own(fn) if isinstance(n, ast.Global) for g in n.names}
    if not names:
        return False
    nodes = _own(fn)
    sets = {t.id for n in nodes if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    rets = {n.value.id for n in nodes if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)}
    return bool(names & sets & rets)


def _locks(fn):
    for n in _own(fn):
        if isinstance(n, ast.Call) and _call_name(n) in ("flock", "lockf", "FileLock", "SoftFileLock", "acquire"):
            return True
        if isinstance(n, ast.withitem) and re.search(r"(?i)lock", ast.unparse(n.context_expr)):
            return True
    return False


def _json_rmw(fn):
    # (read keys, write keys) for JSON files this function reads and writes, each key the path's source text.
    nodes = _own(fn)
    assigned = {}
    for n in nodes:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            assigned.setdefault(n.targets[0].id, []).append(n.value)

    def key(e, depth=0):
        if isinstance(e, ast.Name) and len(assigned.get(e.id, ())) == 1 and depth < 4:
            return key(assigned[e.id][0], depth + 1)
        return ast.unparse(e)
    def opened(call):
        if not (isinstance(call, ast.Call) and _call_name(call) == "open"):
            return None
        if isinstance(call.func, ast.Attribute) and ast.unparse(call.func.value) not in ("io", "builtins"):
            return call.func.value
        return call.args[0] if call.args else None
    handles = {}
    for n in nodes:
        if isinstance(n, ast.withitem) and isinstance(n.optional_vars, ast.Name) and opened(n.context_expr):
            handles[n.optional_vars.id] = (key(opened(n.context_expr)), _mode(n.context_expr))

    def target(e, writing):
        if isinstance(e, ast.Name) and e.id in handles:
            k, mode = handles[e.id]
            return k if (any(c in mode for c in "wa+") == writing) else None
        if opened(e) is not None:
            return key(opened(e)) if (any(c in _mode(e) for c in "wa+") == writing) else None
        if isinstance(e, ast.Call) and _call_name(e) in ("read_text", "read_bytes", "read") and not writing:
            recv = e.func.value if isinstance(e.func, ast.Attribute) else None
            if recv is not None:
                return handles[recv.id][0] if isinstance(recv, ast.Name) and recv.id in handles else key(recv)
        return None

    def dumps(e):
        return any(isinstance(m, ast.Call) and _call_name(m) == "dumps" for m in _walk(e))
    reads, writes = set(), set()
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        name = _call_name(n)
        if name in ("load", "loads") and n.args:
            k = target(n.args[0], False)
            if k:
                reads.add(k)
        elif name == "dump" and len(n.args) > 1:
            k = target(n.args[1], True)
            if k:
                writes.add(k)
        elif name in ("write_text", "write_bytes") and isinstance(n.func, ast.Attribute) and any(
                dumps(a) for a in n.args):
            writes.add(key(n.func.value))
        elif name == "write" and isinstance(n.func, ast.Attribute) and any(dumps(a) for a in n.args):
            k = target(n.func.value, True)
            if k:
                writes.add(k)
    for n in nodes:
        if not (isinstance(n, ast.Call) and _call_name(n) in ("replace", "rename")):
            continue
        if isinstance(n.func, ast.Name) or ast.unparse(n.func.value) == "os":
            pair = n.args[:2] if len(n.args) >= 2 else None
        else:
            pair = [n.func.value, n.args[0]] if len(n.args) == 1 else None
        if pair and key(pair[0]) in writes:
            writes.add(key(pair[1]))
    return reads, writes


def _persists(call, carries):
    name = _call_name(call)
    if name in ("execute", "executemany") and call.args:
        sql = _sql_text(call.args[0])
        return bool(sql and _LINK_SQL.search(sql)) and any(carries(a) for a in call.args[1:])
    if name in ("write_text", "write_bytes", "dump", "write"):
        return any(carries(a) for a in call.args)
    return False


def uncached_key(trees, production=default_production):
    """A kept copy that no change to its source reaches: a cache over files or subprocesses keyed without an mtime
    or version, a JSON cache written back with no lock, and a process-lifetime cache whose value is persisted."""
    prod = _prod(trees, production)
    out = []
    memo = set()
    for rel, tree in prod.items():
        scopes = list(_scopes(tree))
        readers = {name.rsplit(".", 1)[-1] for name, fn in scopes if _reads_source(fn)}
        for name, fn in scopes:
            cached = _cache_deco(fn)
            stamped = any(_STAMP.search(p) for p in _params(fn))
            if (cached or _memo_global(fn)) and not stamped:
                memo.add(name.rsplit(".", 1)[-1])
            if cached and not stamped and _reads_source(fn, readers - {name.rsplit(".", 1)[-1]}):
                out.append(Hit(f"{rel}:{name}", fn.lineno, "a cache over a file or a subprocess keyed without an "
                               "mtime or version: the source changes and the cache keeps the old answer"))
        rmw = {name: (fn,) + _json_rmw(fn) for name, fn in scopes}
        read_where = {}
        for name, (fn, reads, _) in rmw.items():
            for k in reads:
                read_where.setdefault(k, []).append(fn)
        for name, (fn, _, writes) in rmw.items():
            shared = [k for k in writes if k in read_where]
            if shared and not _locks(fn) and not any(_locks(r) for k in shared for r in read_where[k]):
                out.append(Hit(f"{rel}:{name}", fn.lineno, "a JSON file read and written back with no lock: two "
                               "writers each drop the other's entries"))
    for rel, tree in prod.items():
        for name, fn in _bodies(tree):
            if name.rsplit(".", 1)[-1] in memo:
                continue
            nodes = _own(fn)
            tainted = set()

            def carries(e):
                return any((isinstance(m, ast.Call) and _call_name(m) in memo)
                           or (isinstance(m, ast.Name) and m.id in tainted) for m in _walk(e))
            for n in nodes:
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None and carries(n.value):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    tainted |= {x.id for t in targets for x in _walk(t) if isinstance(x, ast.Name)}
            hit = next((n for n in nodes if isinstance(n, ast.Call) and _persists(n, carries)), None)
            if hit is not None:
                out.append(Hit(f"{rel}:{name}", hit.lineno, "a value cached for the life of the process written to "
                               "durable storage: a long-lived process writes back what was true when it started"))
    return out


_LIKE = re.compile(r"(?<![\w.])(?:\w+\.)?(\w+)\s+(?:NOT\s+)?(?:LIKE|GLOB)\b")
_INSTR = re.compile(r"(?i)\binstr\s*\(\s*(?:\w+\.)?(\w+)")
_REGEX = {"search", "match", "fullmatch", "findall", "finditer"}
_TRIMS = {"strip", "lstrip", "rstrip", "lower", "upper", "casefold"}


def _field_of(e):
    # The name a value is kept under: x, obj.x, row["x"], row.get("x"), (x or ""), x.strip().
    while isinstance(e, ast.Call) and ((isinstance(e.func, ast.Attribute) and e.func.attr in _TRIMS) or (
            isinstance(e.func, ast.Name) and e.func.id == "str" and len(e.args) == 1)):
        e = e.func.value if isinstance(e.func, ast.Attribute) else e.args[0]
    if isinstance(e, ast.BoolOp) and isinstance(e.op, ast.Or):
        return _field_of(e.values[0])
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attribute):
        return e.attr
    if isinstance(e, ast.Subscript):
        return _str(e.slice)
    if isinstance(e, ast.Call) and _call_name(e) == "get" and isinstance(e.func, ast.Attribute) and e.args:
        return _str(e.args[0])
    return None


def _needle(e):
    if _str(e) is not None:
        return True
    if isinstance(e, ast.Tuple):
        return bool(e.elts) and all(_needle(x) for x in e.elts)
    return isinstance(e, (ast.Name, ast.Attribute)) and _upper(e.id if isinstance(e, ast.Name) else e.attr)


def _prose_subject(n):
    # The expression a call or comparison matches a fixed needle against, or None.
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
        name, recv = n.func.attr, n.func.value
        if name in ("startswith", "endswith") and n.args and _needle(n.args[0]):
            return recv
        if name in _REGEX and isinstance(recv, ast.Name) and recv.id == "re" and len(n.args) >= 2:
            return n.args[1]
        if name in _REGEX and isinstance(recv, (ast.Name, ast.Attribute)) and n.args and _upper(
                recv.id if isinstance(recv, ast.Name) else recv.attr):
            return n.args[0]
    if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.In, ast.NotIn)):
        if (_str(n.left) or "") and len(_str(n.left)) > 1:
            return n.comparators[0]
    return None


def prose_match(trees, production=default_production, fields=()):
    """A decision read back out of a sentence: SQL LIKE/GLOB/instr, startswith/endswith, `"x" in` or a regex on a
    field the consumer names as stored prose (`fields`, such as note, detail, reason). Finds nothing until told."""
    fields = set(fields)
    out = []
    for rel, tree in _prod(trees, production).items():
        found = {}
        for qual, line, text in _strings(tree):
            if not _SQL_START.search(text):
                continue
            for m in list(_LIKE.finditer(text)) + list(_INSTR.finditer(text)):
                if m.group(1) in fields:
                    found.setdefault((qual, m.group(1)), (line, "SQL"))
        owner = _innermost(tree)
        for n in _walk(tree):
            subject = _prose_subject(n)
            field = _field_of(subject) if subject is not None else None
            if field in fields:
                found.setdefault((owner.get(id(n), "<module>"), field), (n.lineno, "Python"))
        for (qual, field), (line, how) in sorted(found.items()):
            out.append(Hit(f"{rel}:{qual}:{field}", line, f"{how} matches words inside {field}, a sentence the code "
                           "writes as prose: rewording the writer changes the decision"))
    return out


_EMPTY_CALLS = {"set", "dict", "list", "tuple", "frozenset"}
_STORE_READS = {"execute", "executemany", "executescript", "fetchone", "fetchall", "fetchmany", "read_text",
                "read_bytes", "read", "readline", "readlines", "load", "safe_load", "connect", "query"}
_NONE_SAID = re.compile(r"(?i)\b(?:no|none|nothing|empty)\b")
_FAILED_SAID = re.compile(r"(?i)could not|cannot|can't|\bfail|\berror|unreadable|unavailable|\bdown\b|\bunknown\b")


def _empty(e):
    if e is None:
        return True
    if isinstance(e, ast.Constant):
        return e.value is None or (e.value in (0, "", b"") and e.value is not False)
    if isinstance(e, (ast.List, ast.Set, ast.Tuple)):
        return not e.elts
    if isinstance(e, ast.Dict):
        return not e.keys
    return isinstance(e, ast.Call) and _call_name(e) in _EMPTY_CALLS and isinstance(e.func, ast.Name) and not e.args


_STORE_ERRORS = {"OperationalError", "DatabaseError", "ProgrammingError", "InterfaceError", "OSError", "IOError",
                 "FileNotFoundError", "PermissionError", "JSONDecodeError"}


def _store_error(handler):
    # The handler catches an error only a store, a file or a parse of one raises (sqlite3.Error, OSError, ...).
    names = [m for m in _walk(handler.type)] if handler.type is not None else []
    return any((isinstance(m, ast.Name) and m.id in _STORE_ERRORS) or (isinstance(m, ast.Attribute) and (
        m.attr in _STORE_ERRORS or (m.attr == "Error" and isinstance(m.value, ast.Name)
                                    and m.value.id in ("sqlite3", "psycopg", "psycopg2")))) for m in names)


def _reads_store(nodes):
    return any(isinstance(n, ast.Call) and (_call_name(n) in _STORE_READS or (
        isinstance(n.func, ast.Name) and n.func.id == "open")) for n in nodes)


def _handler_empties(fn):
    # (handler, names it sets empty) for each except guarding a store or file read that returns or keeps an empty.
    returned = {n.value.id for n in _own(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)}
    out = []
    for t in _own(fn):
        if not isinstance(t, ast.Try):
            continue
        reads = _reads_store([m for s in t.body for m in _walk(s)])
        for h in t.handlers:
            if not (reads or _store_error(h)):
                continue
            inside = [m for s in h.body for m in _walk(s)]
            if any(isinstance(s, ast.Raise) for s in h.body):
                continue
            names = {x.id for m in inside if isinstance(m, ast.Assign) and _empty(m.value)
                     for x in m.targets if isinstance(x, ast.Name)}
            if any(isinstance(m, ast.Return) and _empty(m.value) for m in inside) or names & returned:
                out.append((h, names))
            elif names:
                out.append((None, names))
    return out


def _says_none(node):
    texts = [_concat(m, "") for m in _walk(node) if isinstance(m, (ast.Constant, ast.JoinedStr))]
    return any(t and _NONE_SAID.search(t) and not _FAILED_SAID.search(t) for t in texts)


def _empty_test(test, names):
    # `not x`, `x is None`, `x == []`, `len(x) == 0`, `not len(x)` on one of `names`.
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = test.operand
        if isinstance(inner, ast.Call) and _call_name(inner) == "len" and inner.args:
            inner = inner.args[0]
        return isinstance(inner, ast.Name) and inner.id in names
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left = test.left
        if isinstance(left, ast.Call) and _call_name(left) == "len" and left.args:
            left = left.args[0]
        return isinstance(left, ast.Name) and left.id in names and _empty(test.comparators[0]) \
            and isinstance(test.ops[0], (ast.Is, ast.Eq))
    return False


def empty_on_failure(trees, production=default_production, choosers=()):
    """Nothing and failed returning the same value: an except around a store or file read that returns an empty
    literal; a "none found" message shown for a value an except path made empty; and a store chosen by `choosers`
    (calls such as backend or db_path) in functions copied across more than one module."""
    prod = _prod(trees, production)
    out = []
    emptying = set()
    for rel, tree in prod.items():
        for name, fn in _scopes(tree):
            for h, _ in _handler_empties(fn):
                if h is not None:
                    emptying.add(name.rsplit(".", 1)[-1])
                    out.append(Hit(f"{rel}:{name}:except", h.lineno, "a failed read returns the same empty value as "
                                   "a read that found nothing; the caller cannot tell them apart"))
                    break
    for rel, tree in prod.items():
        modules = _module_aliases(tree)
        for name, fn in _scopes(tree):
            names = {x for _, got in _handler_empties(fn) for x in got}
            for n in _own(fn):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and _call_name(n.value) in emptying:
                    f = n.value.func
                    if isinstance(f, ast.Name) or (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                                                   and f.value.id in modules):
                        names |= {x.id for x in n.targets if isinstance(x, ast.Name)}
            hit = next((n for n in _own(fn) if isinstance(n, (ast.If, ast.IfExp)) and _empty_test(n.test, names)
                        and any(_says_none(b) for b in (n.body if isinstance(n.body, list) else [n.body]))), None)
            if hit is not None:
                out.append(Hit(f"{rel}:{name}:none-message", hit.lineno, "a \"none found\" message shown for a value "
                               "an except path also leaves empty: a failed read is reported as nothing there"))
    if choosers:
        homes = {rel for rel, tree in prod.items() for s in tree.body
                 if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name in choosers}
        copies = []
        for rel, tree in prod.items():
            if rel in homes:
                continue
            for name, fn in _scopes(tree):
                call = next((n for n in _own(fn) if isinstance(n, ast.Call) and _call_name(n) in choosers), None)
                if call is not None:
                    copies.append((rel, name, call.lineno))
        if len({rel for rel, _, _ in copies}) > 1:
            out += [Hit(f"{rel}:{name}:chooser", line, "a store chosen here as well as elsewhere: each copy can "
                        "settle on a different store, and the wrong one reads as empty") for rel, name, line in copies]
    return out


_DDL_SKIP = {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}
_ALTER_ADD = re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)")
_ALTER_DROP = re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)\s+DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?(\w+)")
_DROP_TABLE = re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)")
_SQL_KIND = re.compile(r"\b(INSERT|REPLACE|UPDATE|DELETE|SELECT)\b")
_SQL_TARGET = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\b")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _ddl_columns(body):
    # (column, is_key) for each top-level item of a CREATE TABLE body, constraints left out.
    body = re.sub(r"--[^\n]*", "", body)
    items, depth, cur = [], 0, ""
    for ch in body:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    out = []
    for item in items:
        m = re.match(r"\s*[\"`\[]?(\w+)", item)
        if m and m.group(1).upper() not in _DDL_SKIP and "{}" not in m.group(1):
            out.append((m.group(1), "PRIMARY KEY" in item.upper()))
    return out


def _schema(trees, migrations):
    # {(table, column): (rel, line, is_key)} from CREATE TABLE and ALTER TABLE in the migration paths, in path order.
    cols = {}
    for rel in sorted(r for r in trees if r.startswith(tuple(migrations))):
        for _, line, text in sorted(_strings(trees[rel], "{}"), key=lambda s: s[1]):
            for m in _CREATE.finditer(text):
                start = text.find("(", m.end())
                if start < 0 or text[m.end():start].strip():
                    continue
                depth, end = 0, start
                for end in range(start, len(text)):
                    depth += (text[end] == "(") - (text[end] == ")")
                    if depth == 0:
                        break
                for col, key in _ddl_columns(text[start + 1:end]):
                    cols.setdefault((m.group(1), col), (rel, line, key))
            for t, c in _ALTER_ADD.findall(text):
                cols.setdefault((t, c), (rel, line, False))
            for t, c in _ALTER_DROP.findall(text):
                cols.pop((t, c), None)
            for t in _DROP_TABLE.findall(text):
                cols = {k: v for k, v in cols.items() if k[0] != t}
    return cols


def unpaired_column(trees, production=default_production, migrations=(), schema=()):
    """A stored column with no live writer or no live reader. The schema is the CREATE TABLE and ALTER TABLE ... ADD
    in the `schema` paths (by default the `migrations` paths); neither is ever a write or read site. A key read from a
    row anywhere counts as a read of a table some module selects with *. Finds nothing until told."""
    if not migrations and not schema:
        return []
    cols = _schema(trees, tuple(schema) or tuple(migrations))
    skip = tuple(schema) + tuple(migrations)
    tables = {}
    for t, c in cols:
        tables.setdefault(t, set()).add(c)
    written, read = set(), set()
    dynamic, starred, named, keyed = {}, {}, {}, {}
    for rel, tree in _prod(trees, production).items():
        if rel.startswith(skip):
            continue
        names, keys = named.setdefault(rel, set()), keyed.setdefault(rel, set())
        for n in _walk(tree):
            if isinstance(n, ast.Subscript) and _str(n.slice):
                keys.add(_str(n.slice))
            elif isinstance(n, ast.Call) and _call_name(n) == "get" and n.args and _str(n.args[0]):
                keys.add(_str(n.args[0]))
            elif isinstance(n, ast.keyword) and n.arg:
                names.add(n.arg)
            if _str(n) is not None and _IDENT.fullmatch(_str(n)):
                names.add(_str(n))
                if _str(n) in tables:
                    dynamic.setdefault(_str(n), set()).add(rel)
                    starred.setdefault(_str(n), set()).add(rel)
        for _, _, text in _strings(tree, "{}"):
            kind = _SQL_KIND.search(text)
            if not kind or not _SQL_TARGET.search(text):
                continue
            words = set(_IDENT.findall(text))
            for t in tables.keys() & words:
                mine = tables[t]
                if kind.group(1) in ("INSERT", "REPLACE"):
                    m = re.search(rf"\bINTO\s+{t}\s*\(([^)]*)\)", text)
                    if m is None or "{}" in m.group(1):
                        dynamic.setdefault(t, set()).add(rel)
                    if m is not None:
                        written.update((t, c) for c in mine & set(_IDENT.findall(m.group(1))))
                    tail = text[m.end():] if m else text
                    upsert = tail.split("DO UPDATE SET", 1)
                    if len(upsert) == 2:
                        written.update((t, c) for c in mine if re.search(rf"\b{c}\s*=(?!=)", upsert[1]))
                    if "SELECT" in upsert[0]:
                        read.update((t, c) for c in mine & set(_IDENT.findall(upsert[0])))
                elif kind.group(1) == "UPDATE" and re.search(rf"\bUPDATE\s+{t}\b", text):
                    sets, _, where = text.partition("SET")[2].partition("WHERE")
                    if "{}" in sets:
                        dynamic.setdefault(t, set()).add(rel)
                    hit = {c for c in mine if re.search(rf"(?<![\w.]){c}\s*=(?!=)", sets)}
                    written.update((t, c) for c in hit)
                    read.update((t, c) for c in mine & set(_IDENT.findall(where)))
                else:
                    if re.search(r"\bSELECT\s+(?:DISTINCT\s+)?\*|\w\.\*", text):
                        starred.setdefault(t, set()).add(rel)
                    read.update((t, c) for c in mine & words)
    out = []
    for (t, c), (rel, line, key) in sorted(cols.items()):
        if (t, c) not in written and not key and not any(c in named[r] for r in dynamic.get(t, ())):
            out.append(Hit(f"{rel}:{t}.{c}:no-writer", line, f"{t}.{c} is read or kept with nothing that writes it: "
                           "every reader sees the default, which looks like a real answer"))
        if (t, c) not in read and not key and not (starred.get(t) and any(c in k for k in keyed.values())):
            out.append(Hit(f"{rel}:{t}.{c}:no-reader", line, f"{t}.{c} is stored and nothing reads it: a control or "
                           "a record that nobody consults"))
    return out


DETECTORS = {
    "prose_match": prose_match,
    "empty_on_failure": empty_on_failure,
    "unpaired_column": unpaired_column,
    "unscoped_query": unscoped_query,
    "stored_pid_liveness": stored_pid_liveness,
    "uncached_key": uncached_key,
    "commit_then_link": commit_then_link,
    "torn_append": torn_append,
    "orphanable_child": orphanable_child,
    "undeadlined_wait": undeadlined_wait,
    "terminal_without_reader": terminal_without_reader,
    "move_tables": move_tables,
    "row_index_keys": row_index_keys,
    "closed_tables": closed_tables,
    "latest_row_state": latest_row_state,
    "unguarded_transition": unguarded_transition,
    "unreached_guards": unreached_guards,
    "ignored_options": ignored_options,
    "member_defaults": member_defaults,
    "harness_verdicts": harness_verdicts,
    "duplicated_literals": duplicated_literals,
    "env_seams": env_seams,
    "loopback_equality": loopback_equality,
}


def run(trees, index, production=default_production, detectors=None, config=None):
    """`config` maps a detector name to the keyword arguments a consumer supplies it (committers, tables, columns)."""
    table = {**DETECTORS, **(detectors or {})}
    config = config or {}
    out = {}
    for cid, entry in index.items():
        name = entry.get("detector")
        if name in table:
            hits = {}
            for h in table[name](trees, production, **config.get(name, {})):
                hits.setdefault(h.site, h)
            out[cid] = sorted(hits.values())
    return out


def _marker(dec, consts):
    if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "gauntlet"):
        return None

    def value(node):
        return consts.get(node.id) if isinstance(node, ast.Name) else _str(node)
    cid = value(dec.args[0]) if dec.args else None
    site = next((value(k.value) for k in dec.keywords if k.arg == "site"), None)
    return (cid, site) if cid and site else None


def bindings(files, is_test=_default_tests):
    out = {}
    for rel, text in files.items():
        tree = _tree(text)[0] if is_test(rel) else None
        if tree is None:
            continue
        consts = {name: _str(v) for name, v, _ in _assign_targets(tree) if _str(v) is not None}
        for stmt in tree.body:
            tests = [(stmt.name, stmt)] if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
            if isinstance(stmt, ast.ClassDef):
                tests = [(f"{stmt.name}::{f.name}", f) for f in stmt.body
                         if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for name, fn in tests:
                for dec in fn.decorator_list:
                    key = _marker(dec, consts)
                    if key:
                        out.setdefault(key, []).append(f"{rel}::{name}")
    return out


def ledger(hits, marks, index):
    out = {}
    for cid, found in hits.items():
        found_sites = [h.site for h in found]
        waivers = index.get(cid, {}).get("waivers", {})
        bound_sites = {s for c, s in marks if c == cid}
        out[cid] = {
            "bound": sorted(s for s in found_sites if s in bound_sites),
            "waived": sorted(s for s in found_sites if s in waivers and s not in bound_sites),
            "unbound": sorted(s for s in found_sites if s not in waivers and s not in bound_sites),
            "stale_bindings": sorted(s for s in bound_sites if s not in found_sites),
            "stale_waivers": sorted(s for s in waivers if s not in found_sites),
        }
    return out
