"""A repo's registry: its defects bound to the corpus's classes, and the checks that keep the binding honest."""

import ast
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gauntlet import review, skill

_CLOSING = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)")
TEST_PREFIXES = ("tests/", "test/")
FETCH = "git fetch origin main (a CI checkout needs fetch-depth: 0)"


def default_production(rel):
    return not rel.startswith(TEST_PREFIXES)


@dataclass
class Registry:
    """What a consumer hands the package: its classes, its files, and what counts as production code there."""
    root: Path
    index: dict
    snapshot_file: Path
    defects_file: Path
    pending: dict = field(default_factory=dict)
    unclassified: dict = field(default_factory=dict)
    backlog_file: Path | None = None
    backlog_pin: int | None = None
    backlog_base: str = "origin/main"
    repo_slug: str | None = None
    defect_label: str = "defect"
    production: Callable[[str], bool] = default_production
    tests: Callable[[str], bool] = lambda rel: rel.startswith("tests/") and "/fixtures/" not in rel
    code_paths: str = review.CODE_PATHS
    files: Callable[[Path], list] | None = None
    moves: tuple = ()
    move_waivers: dict = field(default_factory=dict)
    detector_config: dict = field(default_factory=dict)
    frontends: tuple = ("python",)

    def snapshot(self):
        return skill.load_snapshot(self.snapshot_file)

    def defects(self):
        return load_defects(self.defects_file)

    def backlog(self):
        if self.backlog_file is None or not Path(self.backlog_file).exists():
            return {}
        return parse_backlog(Path(self.backlog_file).read_text(encoding="utf-8"), Path(self.backlog_file).suffix)

    def base_backlog(self):
        """The backlog at the merge base with backlog_base; None when that base predates this snapshot's wiring."""
        base = _git(self.root, "merge-base", "HEAD", self.backlog_base)
        if base.returncode:
            raise LookupError(FETCH)
        sha = base.stdout.strip()
        snap = _at(self.root, sha, self.snapshot_file)
        if snap is None or json.loads(snap) != json.loads(Path(self.snapshot_file).read_text(encoding="utf-8")):
            return None
        path = Path(self.backlog_file)
        text = _at(self.root, sha, path)
        if text is None and path.suffix == ".txt":
            path = path.with_suffix(".json")
            text = _at(self.root, sha, path)
        return {} if text is None else parse_backlog(text, path.suffix)


def parse_backlog(text, suffix):
    """{class id: [sites]} from a backlog's text: JSON, or one `<class id> <site>` row per line for .txt."""
    if suffix != ".txt":
        return json.loads(text)
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        cid, sep, site = line.partition(" ")
        if not sep or not site:
            raise ValueError(f"backlog row {line!r} is not `<class id> <site>`")
        out.setdefault(cid, []).append(site)
    return out


def backlog_text(backlog):
    return "".join(f"{cid} {s}\n" for cid, s in sorted((c, s) for c, sites in backlog.items() for s in sites))


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")


def _at(root, sha, path):
    top = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
    rel = Path(path).resolve().relative_to(Path(top).resolve()).as_posix()
    got = _git(root, "show", f"{sha}:{rel}")
    return got.stdout if got.returncode == 0 else None


def load_defects(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return {r["number"]: {"title": r["title"], "created": r["created"]} for r in rows}


def write_defects(path, defects):
    rows = [{"number": n, **v} for n, v in sorted(defects.items())]
    Path(path).write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    return rows


def _issues(entry):
    return [i["issue"] for i in entry.get("instances", []) if isinstance(i, dict) and "issue" in i]


def bound(index, pending):
    out = set()
    for entry in list(index.values()) + list(pending.values()):
        out.update(_issues(entry))
    return out


def _scanner_exists(repo, ref):
    path, _, name = ref.partition("::")
    file = Path(repo) / path
    if not file.is_file() or not name:
        return False
    tree = ast.parse(file.read_text(encoding="utf-8"))
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name for n in tree.body)


def _instance_problems(cid, entry, defects):
    out = []
    instances = entry["instances"]
    # A class held by a detector may be bound before it bites here; `gauntlet init` binds that way.
    if not instances and not entry.get("detector"):
        out.append(f"{cid}: no instance; a class is bound here only once it has bitten")
    for inst in instances:
        if not (isinstance(inst, dict) and len(inst) == 1 and set(inst) <= {"issue", "rule"}
                and all(isinstance(v, int) for v in inst.values())):
            out.append(f"{cid}: bad instance {inst!r}; use {{'issue': N}} or {{'rule': N}}")
        elif "issue" in inst and inst["issue"] not in defects:
            out.append(f"{cid}: #{inst['issue']} is not in the defects snapshot")
    return out


def _binding_field_problems(cid, entry):
    out = []
    for site, why in entry.get("waivers", {}).items():
        if not str(why).strip():
            out.append(f"{cid}: waiver {site} has no reason")
    if "review" in entry:
        try:
            re.compile(entry["review"])
        except (re.error, TypeError) as e:
            out.append(f"{cid}: review heuristic does not compile: {e}")
    return out


def _tier(cid, entry, tiers):
    return entry.get("tier", tiers.get(cid))


def problems(index, pending, unclassified, snapshot, defects, repo):
    out = []
    tiers = {c["id"]: c["tier"] for c in snapshot}
    for cid, entry in index.items():
        if cid not in tiers:
            out.append(f"{cid}: not in the skill snapshot; propose it as PENDING instead")
        if "tier" in entry and not str(entry.get("tier_reason", "")).strip():
            out.append(f"{cid}: tier rebound here with no tier_reason")
        missing = [f for f in ("instances", "scanners") if f not in entry]
        for f in missing:
            out.append(f"{cid}: missing {f}")
        if missing:
            continue
        out += _instance_problems(cid, entry, defects)
        out += _binding_field_problems(cid, entry)
        for ref in entry["scanners"]:
            if not _scanner_exists(repo, ref):
                out.append(f"{cid}: scanner {ref} does not exist")
    for cid, entry in pending.items():
        if cid in tiers:
            out.append(f"{cid}: the skill now defines it; move it from PENDING into INDEX")
        if "tier" not in entry:
            out.append(f"{cid}: missing tier")
        if "instances" not in entry:
            out.append(f"{cid}: missing instances")
            continue
        out += _instance_problems(cid, entry, defects)
    named = bound(index, pending)
    for n, why in unclassified.items():
        if n in named:
            out.append(f"#{n}: both classified and unclassified")
        if not str(why).strip():
            out.append(f"#{n}: unclassified with no reason")
        if n not in defects:
            out.append(f"#{n}: unclassified but not in the defects snapshot")
    for n in sorted(set(defects) - named - set(unclassified)):
        out.append(f"#{n}: a defect in the snapshot with no class and no reason")
    return out


def unscanned(index, snapshot):
    tiers = {c["id"]: c["tier"] for c in snapshot}
    return sorted(cid for cid, e in index.items() if _tier(cid, e, tiers) == 1 and not e.get("scanners"))


def bite_drift(index, snapshot):
    out = []
    for c in snapshot:
        if c.get("bitten") is None or c["id"] not in index:
            continue
        here = len(_issues(index[c["id"]]))
        if here != c["bitten"]:
            out.append((c["id"], c["bitten"], here))
    return out


def closing_refs(text):
    return {int(n) for n in _CLOSING.findall(text)}


def unattributed(labelled, index, pending, unclassified):
    known = bound(index, pending) | set(unclassified)
    return sorted(n for n in labelled if n not in known)


def unbound_closures(messages, defect_numbers, index, pending, unclassified):
    known = bound(index, pending) | set(unclassified)
    out = []
    for msg in messages:
        for n in sorted(closing_refs(msg)):
            if n in defect_numbers and n not in known:
                out.append((n, msg))
    return out


def stats(index, pending, snapshot, defects):
    tiers = {c["id"]: c["tier"] for c in snapshot}
    per_tier, instances, rules, first, after = {}, {}, {}, {}, {}
    entries = [(cid, e, _tier(cid, e, tiers)) for cid, e in index.items()]
    entries += [(cid, e, e.get("tier")) for cid, e in pending.items()]
    for cid, entry, tier in entries:
        per_tier[tier] = per_tier.get(tier, 0) + 1
        issues = _issues(entry)
        instances[cid] = len(issues)
        rules[cid] = sum(1 for i in entry.get("instances", []) if "rule" in i)
        dates = sorted(defects[n]["created"] for n in issues if n in defects)
        if dates:
            first[cid] = dates[0]
            after[cid] = sum(1 for d in dates if d > dates[0])
    total = sum(instances[c] for c in after)
    return {"classes_per_tier": per_tier, "instances": instances, "rules": rules, "first": first,
            "after_first": after, "kill_rate": (sum(after.values()) / total) if total else None}


def report(labelled, snapshot_numbers, closures, index, pending, unclassified, snapshot, defects):
    lines = []
    gaps = unattributed(labelled, index, pending, unclassified)
    lines.append(f"closed defects with no class: {len(gaps)}")
    lines += [f"  #{n} has no class and no reason" for n in gaps]
    stale = sorted(set(labelled) - set(snapshot_numbers))
    lines.append(f"closed defects not in the snapshot: {len(stale)}")
    lines += [f"  #{n} is not in the snapshot; run snapshot-defects" for n in stale]
    lines.append(f"closing references to a defect with no class: {len(closures)}")
    lines += [f"  #{n} closed by: {msg.strip().splitlines()[0][:80]}" for n, msg in closures]
    lines.append(f"unclassified, with a reason: {len(unclassified)}")
    s = stats(index, pending, snapshot, defects)
    lines.append("classes per tier: " + ", ".join(
        f"tier {t}: {n}" for t, n in sorted(s["classes_per_tier"].items(), key=lambda kv: (kv[0] is None, kv[0]))))
    lines += [f"tier 1 with no scanner yet: {cid}" for cid in unscanned(index, snapshot)]
    lines += [f"skill says bitten {b}, this repo has {h}: {cid}" for cid, b, h in bite_drift(index, snapshot)]
    lines.append(f"pending classes awaiting a skill entry: {len(pending)}")
    lines.append("instances per class (issues / rules / after the first):")
    for cid in sorted(s["instances"], key=lambda c: (-s["instances"][c], c)):
        lines.append(f"  {s['instances'][cid]:3d} {s['rules'][cid]:3d} {s['after_first'].get(cid, 0):3d}  {cid}"
                     + ("  (pending)" if cid in pending else ""))
    kr = s["kill_rate"]
    lines.append("kill rate: " + ("n/a" if kr is None else
                 f"{kr:.2f} of attributed defects arrived after their class's first instance"))
    return "\n".join(lines)


def _gh(argv):
    proc = subprocess.run(["gh", *argv], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


def gh_defects(slug, label="defect"):
    rows = _gh(["issue", "list", "--repo", slug, "--state", "closed", "--label", label,
                "--limit", "1000", "--json", "number,title,createdAt"])
    return {r["number"]: {"title": r["title"], "created": r["createdAt"]} for r in rows}


def gh_merged_pr_bodies(slug):
    rows = _gh(["pr", "list", "--repo", slug, "--state", "merged", "--limit", "1000",
                "--json", "number,title,body"])
    return [f"{r['title']}\n{r['body']}" for r in rows]


def git_messages(repo, ref="origin/main"):
    # None when this clone lacks `ref`: a CI checkout is one commit with no origin/main.
    have = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8")
    if have.returncode != 0:
        return None
    proc = subprocess.run(["git", "log", ref, "--format=%B%x00"], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", check=True)
    return [m for m in proc.stdout.split("\x00") if m.strip()]
