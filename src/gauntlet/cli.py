"""gauntlet {init,classes,corpus-dir,inventory,scan,import,audit,review,snapshot-skill,snapshot-defects,mutation}."""

import argparse
import importlib
import os
import sys
from pathlib import Path

from gauntlet import adapters, findings, frontends, inventory, mutation, registry, review, skill

REGISTRY_ENV = "GAUNTLET_REGISTRY"


def load_object(spec):
    # "package.module:ATTR", imported with the working directory on sys.path, as `python -m` would.
    mod, _, attr = spec.partition(":")
    if not mod or not attr:
        raise SystemExit(f"{spec!r}: want package.module:ATTR")
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    obj = importlib.import_module(mod)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def _registry(a):
    spec = a.registry or os.environ.get(REGISTRY_ENV)
    if not spec:
        raise SystemExit(f"name the registry: --registry package.module:ATTR or ${REGISTRY_ENV}")
    return load_object(spec)


def _shown(path, root):
    path = Path(path)
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def cmd_classes(a):
    for c in skill.read_skill(a.skill_dir):
        print(f"{c['tier']}  {c['id']}  ({c['source']})")
    return 0


def cmd_corpus_dir(a):
    print(skill.corpus_dir())
    return 0


def cmd_inventory(a):
    problems = inventory.check_files(a.files, a.skill_dir)
    print("\n".join(problems) if problems else f"{len(a.files)} inventory file(s) in the standard format")
    return 1 if problems else 0


def _emit(found, a):
    text = findings.render(found, a.format)
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def cmd_scan(a):
    reg = load_object(a.registry) if a.registry else None
    root = Path(a.root) if a.root else (reg.root if reg else Path.cwd())
    names = a.frontend or (list(reg.frontends) if reg else ["python"])
    config = None
    if reg:
        from gauntlet import ratchet
        config = ratchet.detector_config(reg)
    found = []
    for name in names:
        fe = frontends.get(name, reg.production if reg else registry.default_production)
        found += fe.scan(root, fe.discover(root, lister=reg.files if reg else None), reg.index if reg else None, config)
    found = sorted(set(found))
    if a.baseline:
        return _against_baseline(found, a)
    _emit(found, a)
    return 1 if found and a.fail else 0


def _against_baseline(found, a):
    known = {(f.cls, f.site) for f in findings.from_json(Path(a.baseline).read_text(encoding="utf-8"))}
    new = [f for f in found if (f.cls, f.site) not in known]
    gone = sorted(known - {(f.cls, f.site) for f in found})
    _emit(new, a)
    for cls, site in gone:
        print(f"gone since the baseline, drop it from {a.baseline}: {cls} {site}", file=sys.stderr)
    return 1 if new else 0


def cmd_init(a):
    from gauntlet import init
    r = init.run(a.root, a.pin, a.force)
    for rel in r.written:
        print(f"wrote {rel}")
    print(f"backlog: {r.backlog} hit(s) seeded")
    print(f"TODO lines: {r.todos}" + (f" in {init.REGISTRY_REL}: ask the user before filling any" if r.todos else ""))
    if r.ratchets:
        print(f"ratchet tests: {r.ratchets} in {init.TEST_REL}: expect `{r.ratchets} passed`")
    for note in r.notes:
        print(f"note: {note}")
    for warning in r.warnings:
        print(f"warning: {warning}")
    return 0


def cmd_import(a):
    found = []
    for path in a.files:
        found += adapters.load(a.tool, Path(path).read_text(encoding="utf-8"))
    _emit(sorted(set(found)), a)
    return 0


def cmd_audit(a):
    reg = _registry(a)
    snapshot, defects = reg.snapshot(), reg.defects()
    history = registry.git_messages(reg.root, a.ref)
    if a.offline or not reg.repo_slug:
        labelled, messages = sorted(defects), history or []
    else:
        labelled = sorted(registry.gh_defects(reg.repo_slug, reg.defect_label))
        messages = (history or []) + registry.gh_merged_pr_bodies(reg.repo_slug)
    closures = registry.unbound_closures(messages, set(labelled) | set(defects), reg.index, reg.pending,
                                         reg.unclassified)
    print(registry.report(labelled, set(defects), closures, reg.index, reg.pending, reg.unclassified,
                          snapshot, defects))
    if history is None:
        print(f"commit messages not read: {a.ref} is not in this clone")
    gaps = registry.unattributed(labelled, reg.index, reg.pending, reg.unclassified)
    return 1 if gaps or closures else 0


def cmd_review(a):
    reg = _registry(a)
    fired = review.fire(review.changed(reg.root, a.range), reg.index, reg.snapshot(), reg.code_paths)
    print(review.render(fired))
    return 0


def cmd_snapshot_skill(a):
    reg = _registry(a)
    found = skill.read_skill(a.skill_dir)
    skill.write_snapshot(reg.snapshot_file, found)
    print(f"{len(found)} classes -> {_shown(reg.snapshot_file, reg.root)}")
    return 0


def cmd_snapshot_defects(a):
    reg = _registry(a)
    if not reg.repo_slug:
        raise SystemExit("the registry names no repo_slug, so there is no issue tracker to read")
    rows = registry.write_defects(reg.defects_file, registry.gh_defects(reg.repo_slug, reg.defect_label))
    print(f"{len(rows)} defects -> {_shown(reg.defects_file, reg.root)}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["mutation"]:
        rest = argv[1:]
        if rest[:1] != ["--config"] or len(rest) < 2:
            raise SystemExit("usage: gauntlet mutation --config package.module:ATTR {run,pin} ...")
        return mutation.main(load_object(rest[1]), rest[2:])
    p = argparse.ArgumentParser(prog="gauntlet")
    sub = p.add_subparsers(dest="cmd", required=True)
    it = sub.add_parser("init", help="wire this repo: pin, registry, ratchet tests, snapshots, a seeded backlog")
    it.add_argument("--root", default=".", help="the repo to wire (default: the working directory)")
    it.add_argument("--pin", help="the gauntlet tag to pin (default: this installed version's tag)")
    it.add_argument("--force", action="store_true", help="rewrite an existing registry or baseline")
    it.set_defaults(fn=cmd_init)
    cl = sub.add_parser("classes", help="the corpus's classes: tier, id, source file")
    cl.add_argument("--skill-dir", help="default: $GAUNTLET_SKILL_DIR, else the corpus this version ships")
    cl.set_defaults(fn=cmd_classes)
    cd = sub.add_parser("corpus-dir", help="where the corpus this version ships lives")
    cd.set_defaults(fn=cmd_corpus_dir)
    iv = sub.add_parser("inventory", help="check shape-question inventory files against the standard format")
    iv.add_argument("files", nargs="+")
    iv.add_argument("--skill-dir", help="where shapes.md lives (default: the corpus this version ships)")
    iv.set_defaults(fn=cmd_inventory)
    sc = sub.add_parser("scan", help="run front ends over a repo and print findings as JSON or SARIF")
    sc.add_argument("root", nargs="?", help="default: the registry's root, else the working directory")
    sc.add_argument("--frontend", action="append", choices=("python", "hcl"),
                    help="repeatable (default: the registry's frontends, else python)")
    sc.add_argument("--registry", help="package.module:ATTR; its index names the classes, else detector defaults")
    sc.add_argument("--fail", action="store_true", help="exit 1 when anything is found")
    sc.add_argument("--baseline", help="a JSON findings file: print only findings not in it, exit 1 if any")
    im = sub.add_parser("import", help="read a linter's JSON output as findings")
    im.add_argument("tool", choices=sorted(adapters.ADAPTERS))
    im.add_argument("files", nargs="+")
    for s in (sc, im):
        s.add_argument("--format", choices=("json", "sarif"), default="json")
        s.add_argument("--output", help="write here instead of stdout")
    sc.set_defaults(fn=cmd_scan)
    im.set_defaults(fn=cmd_import)
    for name, fn, text in (("audit", cmd_audit, "closed defects with no class, and the registry's numbers"),
                           ("review", cmd_review, "the review questions a diff raises, for classes no detector finds"),
                           ("snapshot-skill", cmd_snapshot_skill, "regenerate the registry's skill snapshot"),
                           ("snapshot-defects", cmd_snapshot_defects, "regenerate the registry's defects snapshot")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--registry", help=f"package.module:ATTR naming a gauntlet.registry.Registry "
                                          f"(default: ${REGISTRY_ENV})")
        s.set_defaults(fn=fn)
        if name == "audit":
            s.add_argument("--offline", action="store_true", help="use the committed snapshot, no gh")
            s.add_argument("--ref", default="origin/main", help="history whose commit messages are read")
        elif name == "review":
            s.add_argument("range", nargs="?", default=review.DEFAULT_RANGE, help="a git range (default: %(default)s)")
        elif name == "snapshot-skill":
            s.add_argument("--skill-dir", help="default: $GAUNTLET_SKILL_DIR, else the corpus this version ships")
    a = p.parse_args(argv)
    return a.fn(a)
