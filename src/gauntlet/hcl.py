"""The HCL front end (Terraform/OpenTofu): python-hcl2, installed as `gauntlet[hcl]`, parses; the detectors read the model."""

import json
import posixpath
import re
import shlex
from pathlib import Path

from gauntlet import files as _files
from gauntlet.findings import Finding

STATEFUL = ("aws_db_instance", "aws_rds_cluster", "aws_s3_bucket", "aws_route53_zone", "aws_ebs_volume")
COPIED = ("name_servers",)
TIES = ("terraform.workspace", "aws_caller_identity")
IGNORED_LITERALS = ("0.0.0.0/0", "::/0")
IGNORED_DOMAIN_SUFFIXES = (".amazonaws.com",)
NAME_KEYS = ("name", "bucket", "identifier", "cluster_identifier", "domain_name", "zone_name", "zone")
_FILE_EXT = {"html", "json", "tf", "txt", "yml", "yaml", "sh", "py", "zip", "js", "css", "hcl", "tfvars",
             "tfstate", "pem", "key", "md", "tpl", "xml", "csv", "log", "gz"}
_LABELS = {"resource": 2, "data": 2, "variable": 1, "output": 1, "module": 1, "run": 1, "check": 1, "provider": 1,
           "backend": 1}
_CIDR = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")
_REGION = re.compile(r"^(?:us|eu|ap|sa|ca|me|af|il|mx|cn)(?:-gov|-iso[a-z]?)?-[a-z]+-\d$")
_ACCOUNT = re.compile(r"^\d{12}$")
_DOMAIN = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}\.?$")
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_REF = re.compile(r"(?<![\w.\"])([A-Za-z_][\w-]*)((?:\.[A-Za-z_*][\w-]*|\[[^\]]*\])+)(?!\s*\()")
_BY_POSITION = re.compile(r"data\.[\w-]+\.[\w-]+(?:\.[\w-]+)*\s*\[\s*count\.index\s*\]"
                          r"|element\(\s*data\.[^,()]+,\s*count\.index\s*\)")
_ORDER = re.compile(r"(?i)\b(?:before|after|prior to|until)\b")
_ACTION = re.compile(r"(?i)\b(?:apply|applied|applying|plan|deploy\w*|run|update\w*|delegat\w*|registrar|cop(?:y|ied)"
                     r"|import\w*|init|migrat\w*|creat\w*)\b")
_BLOCK_HEAD = re.compile(r'^\s*(resource|data|output|variable|module|check|run)((?:\s+"[^"]*")+)\s*\{')


def available():
    try:
        import hcl2
    except ImportError:
        return False
    return hasattr(hcl2, "loads")


def _hcl2():
    if not available():
        raise SystemExit("the HCL front end needs python-hcl2: pip install 'gauntlet[hcl]'")
    import hcl2
    return hcl2


def _unq(s):
    return s[1:-1] if isinstance(s, str) and len(s) >= 2 and s[0] == s[-1] == '"' else s


def literal(v):
    """A constant's value, or None for an expression."""
    if isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str) and len(v) >= 2 and v[0] == v[-1] == '"' and "${" not in v:
        return v[1:-1]
    return None


def expr(v):
    if isinstance(v, str):
        return v[2:-1] if v.startswith("${") and v.endswith("}") else v
    return json.dumps(v)


def blocks(doc, kind):
    """(labels, body) for each `kind` block in a parsed document."""
    def unwrap(node, depth, labels):
        if depth == 0:
            yield labels, node
            return
        for k, v in node.items():
            if not k.startswith("__"):
                yield from unwrap(v, depth - 1, labels + (_unq(k),))
    for item in (doc or {}).get(kind) or []:
        if isinstance(item, dict):
            yield from unwrap(item, _LABELS.get(kind, 0), ())


def walk(node, key=None):
    """(attribute name, scalar) for every scalar under node."""
    if isinstance(node, dict):
        for k, v in node.items():
            if not k.startswith("__"):
                yield from walk(v, _unq(k))
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, key)
    else:
        yield key, node


def _line(text, pattern):
    m = re.search(pattern, text, re.M)
    return text.count("\n", 0, m.start()) + 1 if m else 1


def block_line(text, kind, labels):
    return _line(text, r"^\s*" + kind + "".join(rf'\s+"?{re.escape(x)}"?' for x in labels) + r"\s*\{")


def _dir(rel):
    return posixpath.dirname(rel) or "."


def _under(rel, d):
    return d == "." or rel == d or rel.startswith(d + "/")


def is_tf(rel):
    return rel.endswith(".tf")


def is_tfvars(rel):
    return rel.endswith(".tfvars")


def is_test(rel):
    return rel.endswith(".tftest.hcl")


def is_lock(rel):
    return posixpath.basename(rel) == ".terraform.lock.hcl"


def is_backend_file(rel):
    base = posixpath.basename(rel)
    return rel.endswith(".tfbackend") or (rel.endswith(".hcl") and "backend" in base and not is_test(rel))


def is_driver(rel):
    base = posixpath.basename(rel)
    return (rel.endswith((".sh", ".bash", ".mk", ".yml", ".yaml")) or base in ("Makefile", "makefile", "GNUmakefile",
                                                                               "Justfile", "justfile"))


def is_deploy_tfvars(rel):
    base = posixpath.basename(rel)
    return is_tfvars(rel) and base != "terraform.tfvars" and not base.endswith(".auto.tfvars")


def wanted(rel):
    if "/.terraform/" in f"/{rel}":
        return False
    return is_tf(rel) or is_tfvars(rel) or is_test(rel) or is_lock(rel) or is_backend_file(rel) or is_driver(rel)


class Repo:
    """Every HCL file parsed once, plus the scripts that run terraform, for the detectors to share."""

    def __init__(self, root, rels):
        self.root = Path(root)
        self.rels = sorted(rels)
        self.listed = set(self.rels)
        self.text, self.docs, self.errors = {}, {}, {}
        hcl2 = None
        for rel in self.rels:
            self.text[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            if is_driver(rel) or is_lock(rel):
                continue
            hcl2 = hcl2 or _hcl2()
            try:
                self.docs[rel] = hcl2.loads(self.text[rel])
            except Exception as e:  # lark raises its own hierarchy; any failure is a file the scan cannot see
                self.errors[rel] = str(e).splitlines()[0] if str(e) else type(e).__name__

    def of(self, pred):
        return [(rel, self.docs[rel]) for rel in self.rels if rel in self.docs and pred(rel)]

    def tf(self):
        return self.of(is_tf)

    def module_dirs(self):
        return sorted({_dir(rel) for rel in self.rels if is_tf(rel)})

    def child_modules(self):
        out = set()
        for rel, doc in self.tf():
            for _, body in blocks(doc, "module"):
                src = literal(body.get("source"))
                if isinstance(src, str) and src.startswith(("./", "../")):
                    out.add(posixpath.normpath(posixpath.join(_dir(rel), src)))
        return out

    def backend(self, d):
        """(rel, type, body) of the backend block in module dir d, or None."""
        for rel, doc in self.tf():
            if _dir(rel) != d:
                continue
            for _, term in blocks(doc, "terraform"):
                for typ, body in blocks(term, "backend"):
                    return rel, typ[0], body
        return None

    def roots(self):
        """Root modules: a dir of .tf files with a backend, tfvars or a lock file, that no module block sources."""
        dirs = set(self.module_dirs())
        marked = {d for d in dirs if self.backend(d)}
        marked |= {_dir(rel) for rel in self.rels if is_tfvars(rel) or is_lock(rel)}
        return sorted((marked & dirs) - self.child_modules())

    def first_tf(self, d):
        return next(rel for rel in self.rels if is_tf(rel) and _dir(rel) == d)

    def deploy_tfvars(self, d):
        others = [r for r in self.roots() if r != d and _under(r, d)]
        return [rel for rel in self.rels if is_deploy_tfvars(rel) and _under(rel, d)
                and not any(_under(rel, o) for o in others)]

    def conditions(self, pred=lambda rel: is_tf(rel) or is_test(rel)):
        return [(rel, expr(v)) for rel, doc in self.of(pred) for k, v in walk(doc) if k == "condition"]

    def variable_defaults(self):
        return {labels[0]: body.get("default") for _, doc in self.tf() for labels, body in blocks(doc, "variable")}

    def tfvars_values(self, name):
        return [doc[name] for _, doc in self.of(is_tfvars) if name in doc]

    def driver_lines(self):
        """(rel, line number, joined command) for every driver line, with backslash continuations joined."""
        out = []
        for rel in self.rels:
            if not is_driver(rel):
                continue
            buf, start = "", 0
            for i, line in enumerate(self.text[rel].splitlines(), 1):
                if not buf:
                    start = i
                stripped = line.rstrip()
                if stripped.endswith("\\"):
                    buf += stripped[:-1] + " "
                    continue
                out.append((rel, start, buf + stripped))
                buf = ""
            if buf:
                out.append((rel, start, buf))
        return out


def _f(rule, site, line, why):
    return Finding("", site, line, why, "gauntlet", rule)


def _kind(key, value):
    if not isinstance(value, str) or value in IGNORED_LITERALS:
        return None
    if _CIDR.match(value):
        return "CIDR"
    if _REGION.match(value):
        return "region"
    if _ACCOUNT.match(value):
        return "account id"
    if (_DOMAIN.match(value) and value.rstrip(".").rsplit(".", 1)[-1] not in _FILE_EXT
            and not value.rstrip(".").endswith(IGNORED_DOMAIN_SUFFIXES)):
        return "domain"
    if key and (key in NAME_KEYS or key.endswith("_name")) and len(value) >= 3 and "${" not in value:
        return "name"
    return None


def duplicated_literal(repo):
    homes = {}
    for rel, doc in repo.of(lambda r: is_tf(r) or is_tfvars(r)):
        for key, v in walk(doc):
            value = literal(v)
            kind = _kind(key, value)
            if kind:
                homes.setdefault((kind, value), set()).add(rel)
    out = []
    for (kind, value), rels in sorted(homes.items()):
        if len(rels) < 2:
            continue
        for rel in sorted(rels):
            others = ", ".join(r for r in sorted(rels) if r != rel)
            out.append(_f("hcl_duplicated_literal", f"{rel}:{value}", _line(repo.text[rel], re.escape(f'"{value}"')),
                          f"the {kind} {value} is also written in {others}"))
    return out


def env_two_inputs(repo, ties=TIES):
    out = []
    for d in repo.roots():
        found = repo.backend(d)
        envs = repo.deploy_tfvars(d)
        if not found or len(envs) < 2:
            continue
        rel, typ, body = found
        conds = [c for r, c in repo.conditions(is_tf) if _under(r, d)]
        if any(t in c for c in conds for t in ties):
            continue
        partial = "key" not in body and "path" not in body and "prefix" not in body
        why = (f"state is chosen at init ({'partial ' if partial else ''}backend {typ}) and the environment by "
               f"-var-file ({', '.join(envs)}), and no precondition, check or validation ties one to the other")
        out.append(_f("hcl_env_two_inputs", f"{rel}:backend.{typ}", block_line(repo.text[rel], "backend", (typ,)), why))
    return out


def _backend_args(cmd):
    try:
        words = shlex.split(cmd, comments=True)
    except ValueError:
        words = cmd.split()
    files, keys, chdir, i = [], {}, None, 0
    while i < len(words):
        w = words[i]
        val = None
        if w.startswith("-backend-config="):
            val = w.split("=", 1)[1]
        elif w == "-backend-config" and i + 1 < len(words):
            i += 1
            val = words[i]
        elif w.startswith("-chdir="):
            chdir = w.split("=", 1)[1]
        if val is not None:
            if "=" in val:
                k, _, v = val.partition("=")
                keys[k.strip()] = v.strip()
            else:
                files.append(val)
        i += 1
    return files, keys, chdir


def shared_backend(repo):
    out = []
    by_identity = {}
    for d in repo.roots():
        found = repo.backend(d)
        if found:
            rel, typ, body = found
            ident = tuple(sorted((k, literal(v)) for k, v in body.items() if not k.startswith("__")))
            if ident and all(v is not None for _, v in ident) and any(k in dict(ident) for k in ("key", "path",
                                                                                                    "prefix")):
                by_identity.setdefault((typ, ident), []).append((rel, typ))
    for homes in by_identity.values():
        if len(homes) >= 2:
            for rel, typ in homes:
                others = ", ".join(r for r, _ in homes if r != rel)
                out.append(_f("hcl_shared_backend", f"{rel}:backend.{typ}",
                              block_line(repo.text[rel], "backend", (typ,)),
                              f"the same backend and state key as {others}, so both root modules write one state"))
    uses = {}
    for rel, n, cmd in repo.driver_lines():
        if "-backend-config" not in cmd:
            continue
        cfiles, keys, chdir = _backend_args(cmd)
        for f in cfiles:
            uses.setdefault(posixpath.basename(f), []).append((rel, n, chdir, keys.get("key"), f))
    backend_files = {posixpath.basename(r): r for r in repo.rels if is_backend_file(r)}
    for base, calls in sorted(uses.items()):
        variants = {(c[2], c[3]) for c in calls}
        if len(variants) < 2:
            continue
        rel = backend_files.get(base)
        keyed = rel in repo.docs and "key" in repo.docs[rel]
        how = "names its own key, so every caller shares one state" if keyed else (
            "names no key, so the callers' states differ only by a -backend-config=key=... on the command line")
        where = ", ".join(f"{r}:{n}" for r, n, *_ in calls)
        site, line = (f"{rel}:backend-config", 1) if rel else (f"{calls[0][0]}:{base}", calls[0][1])
        out.append(_f("hcl_shared_backend", site, line, f"one backend config file is used by {len(variants)} "
                                                        f"different init invocations ({where}); it {how}"))
    return out


def _reachable_true(v, repo):
    lit = literal(v)
    if lit is not None:
        return lit is True or lit == "true"
    e = expr(v).strip()
    m = re.fullmatch(r"var\.([\w-]+)", e)
    if m:
        name = m.group(1)
        values = [repo.variable_defaults().get(name)] + repo.tfvars_values(name)
        return any(literal(x) in (True, "true") for x in values if x is not None) or all(
            x is None for x in values)
    return True


def stateful_unprotected(repo, stateful=()):
    types = set(STATEFUL) | set(stateful)
    out = []
    for rel, doc in repo.tf():
        for (typ, name), body in blocks(doc, "resource"):
            if typ not in types:
                continue
            line = block_line(repo.text[rel], "resource", (typ, name))
            guarded = any(literal(lc.get("prevent_destroy")) in (True, "true") for _, lc in blocks(body, "lifecycle"))
            if not guarded:
                out.append(_f("hcl_stateful_unprotected", f"{rel}:{typ}.{name}", line,
                              f"{typ} holds state a re-apply cannot rebuild, and no lifecycle prevent_destroy "
                              f"stops a plan from destroying it"))
            if "skip_final_snapshot" in body and _reachable_true(body["skip_final_snapshot"], repo):
                out.append(_f("hcl_stateful_unprotected", f"{rel}:{typ}.{name}.skip_final_snapshot", line,
                              f"skip_final_snapshot can be true ({expr(body['skip_final_snapshot'])}), so a destroy "
                              f"keeps no copy"))
    return out


def count_by_position(repo):
    out = []
    for rel, doc in repo.tf():
        for kind in ("resource", "data", "module"):
            for labels, body in blocks(doc, kind):
                if "count" not in body:
                    continue
                m = _BY_POSITION.search(json.dumps({k: v for k, v in body.items() if k != "count"}))
                if m:
                    label = ".".join(labels) if kind != "data" else "data." + ".".join(labels)
                    label = label if kind != "module" else "module." + labels[0]
                    out.append(_f("hcl_count_by_position", f"{rel}:{label}", block_line(repo.text[rel], kind, labels),
                                  f"count indexes into a data source list ({m.group(0)}), so the address is its "
                                  f"position: a reordered or shortened list destroys and recreates the rest; use "
                                  f"for_each keyed by a stable value"))
    return out


def lock_missing(repo):
    out = []
    for d in repo.roots():
        lock = ".terraform.lock.hcl" if d == "." else f"{d}/.terraform.lock.hcl"
        if lock in repo.listed:
            continue
        state = "git-ignored or untracked" if (repo.root / lock).exists() else "missing"
        rel = repo.first_tf(d)
        out.append(_f("hcl_lock_missing", f"{rel}:.terraform.lock.hcl", 1,
                      f"root module {d}: its .terraform.lock.hcl is {state}, so each init resolves provider "
                      f"versions afresh and nothing pins what was planned"))
    return out


def _refs(condition):
    body = _STRING.sub('""', condition)
    return [m.group(1) + m.group(2) for m in _REF.finditer(body)]


def constant_assert(repo):
    out = []
    for rel, doc in repo.of(is_test):
        for (run,), body in blocks(doc, "run"):
            for i, (_, a) in enumerate(blocks(body, "assert")):
                cond = expr(a.get("condition", ""))
                refs = _refs(cond)
                if all(r.startswith("var.") for r in refs):
                    what = "variables" if refs else "constants"
                    out.append(_f("hcl_constant_assert", f"{rel}:run.{run}.assert[{i}]",
                                  block_line(repo.text[rel], "run", (run,)),
                                  f"`{cond}` reads only {what}, so it holds whatever the module does"))
    return out


def test_skips_tfvars(repo):
    out = []
    test_runs = [(rel, cmd) for rel, _, cmd in repo.driver_lines() if re.search(r"\btest\b", cmd)]
    for rel in repo.rels:
        if not is_test(rel):
            continue
        d = _dir(rel)
        d = _dir(d) if posixpath.basename(d) == "tests" else d
        text = rel + "\n" + repo.text[rel]
        for tv in repo.deploy_tfvars(d):
            stem = posixpath.basename(tv)[: -len(".tfvars")]
            if not re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(stem)}(?![A-Za-z0-9])", text):
                continue
            if any(posixpath.basename(tv) in cmd for _, cmd in test_runs):
                continue
            out.append(_f("hcl_test_skips_tfvars", f"{rel}:{posixpath.basename(tv)}", 1,
                          f"the test names {stem} but nothing runs it with -var-file={tv}, so it checks values "
                          f"copied into the test, not the file that deploys"))
    return out


def unchecked_external(repo, copied=COPIED):
    out = []
    conds = [c for _, c in repo.conditions()]
    for rel, doc in repo.tf():
        for (name,), body in blocks(doc, "output"):
            value = expr(body.get("value", ""))
            for attr in copied:
                if re.search(rf"\.{re.escape(attr)}\b", value) and not any(attr in c for c in conds):
                    out.append(_f("hcl_unchecked_external", f"{rel}:output.{name}",
                                  block_line(repo.text[rel], "output", (name,)),
                                  f"{attr} must be copied somewhere terraform does not manage, and no check, "
                                  f"postcondition or test assert compares the copy with it"))
    return out


def _orders(text):
    return bool(_ORDER.search(text) and _ACTION.search(text))


def _comments(text):
    """(first line, joined text, label of the block that follows) for each run of whole-line comments."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith(("#", "//")):
            start, para = i + 1, []
            while i < len(lines) and lines[i].strip().startswith(("#", "//")):
                para.append(lines[i].strip().lstrip("#/").strip())
                i += 1
            label = ""
            if i < len(lines):
                m = _BLOCK_HEAD.match(lines[i])
                if m:
                    names = re.findall(r'"([^"]*)"', m.group(2))
                    label = ".".join(([m.group(1)] if m.group(1) not in ("resource",) else []) + names)
            out.append((start, " ".join(para), label or f"L{start}"))
        else:
            i += 1
    return out


def order_in_prose(repo):
    out = []
    for rel, doc in repo.tf():
        for kind in ("output", "variable"):
            for (name,), body in blocks(doc, kind):
                desc = literal(body.get("description"))
                if isinstance(desc, str) and _orders(desc):
                    out.append(_f("hcl_order_in_prose", f"{rel}:{kind}.{name}.description",
                                  block_line(repo.text[rel], kind, (name,)),
                                  f"an order is stated only in prose: {desc!r}; nothing in the code checks it"))
        for line, text, label in _comments(repo.text[rel]):
            if _orders(text):
                out.append(_f("hcl_order_in_prose", f"{rel}:comment.{label}", line,
                              f"an order is stated only in a comment: {text!r}; nothing in the code checks it"))
    return out


def unparsed(repo):
    return [_f("hcl_unparsed", f"{rel}:parse", 1, f"python-hcl2 cannot parse it ({msg}), so no detector saw it")
            for rel, msg in sorted(repo.errors.items())]


DETECTORS = {
    "hcl_duplicated_literal": (duplicated_literal, "one-question-two-answers"),
    "hcl_env_two_inputs": (env_two_inputs, "a-non-production-run-writes-live-state"),
    "hcl_shared_backend": (shared_backend, "a-non-production-run-writes-live-state"),
    "hcl_stateful_unprotected": (stateful_unprotected, "irreplaceable-state-with-no-guard-on-its-destruction"),
    "hcl_count_by_position": (count_by_position, "identity-by-name-not-by-content"),
    "hcl_lock_missing": (lock_missing, "external-state-with-no-assertion-on-it"),
    "hcl_constant_assert": (constant_assert, "a-metric-quoted-without-a-negative-control"),
    "hcl_test_skips_tfvars": (test_skips_tfvars, "a-proxy-checked-in-place-of-the-property"),
    "hcl_unchecked_external": (unchecked_external, "external-state-with-no-assertion-on-it"),
    "hcl_order_in_prose": (order_in_prose, "an-order-the-code-needs-but-does-not-check"),
    "hcl_unparsed": (unparsed, "hcl-unparsed"),
}


def run(repo, index=None, config=None):
    """Findings with their class: from the index's `detector` names, else each detector's default class."""
    config = config or {}
    if index is None:
        plan = [(cls, name) for name, (_, cls) in DETECTORS.items()]
    else:
        plan = [(cid, e["detector"]) for cid, e in index.items() if e.get("detector") in DETECTORS]
        if repo.errors:
            plan.append(("hcl-unparsed", "hcl_unparsed"))
    out = set()
    for cls, name in plan:
        fn = DETECTORS[name][0]
        for f in fn(repo, **config.get(name, {})):
            out.add(Finding(cls, f.site, f.line, f.why, f.tool, f.rule))
    return sorted(out)


class HclFrontEnd:
    name = "hcl"

    def detectors(self):
        return set(DETECTORS)

    def discover(self, root, lister=None):
        root = Path(root)
        paths = (lister or _files.publishable)(root)
        rels = [p.relative_to(root).as_posix() for p in paths]
        return sorted(r for r in rels if wanted(r) and (root / r).is_file())

    def scan(self, root, files, index=None, config=None):
        return run(Repo(root, files), index, config)
