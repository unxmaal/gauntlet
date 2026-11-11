"""One findings format for every front end and adapter: {class, site, line, why, tool, rule}, as JSON or SARIF 2.1.0."""

import json
from dataclasses import dataclass

from gauntlet import __version__

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
INFO_URI = "https://github.com/unxmaal/gauntlet"


@dataclass(frozen=True, order=True)
class Finding:
    cls: str
    site: str
    line: int
    why: str
    tool: str = "gauntlet"
    rule: str = ""

    @property
    def path(self):
        return self.site.split(":", 1)[0]

    def to_dict(self):
        return {"class": self.cls, "site": self.site, "line": self.line, "why": self.why, "tool": self.tool,
                "rule": self.rule}

    @classmethod
    def from_dict(cls, d):
        return cls(d["class"], d["site"], int(d.get("line") or 0), d.get("why", ""), d.get("tool", "gauntlet"),
                   d.get("rule", ""))


def from_hits(hits, tool="gauntlet", rules=None):
    """{class: [Hit]} from detectors.run, as findings; `rules` maps a class to the detector that found it."""
    rules = rules or {}
    return sorted(Finding(cid, h.site, h.line, h.why, tool, rules.get(cid, "")) for cid, found in hits.items()
                  for h in found)


def to_json(found):
    return json.dumps([f.to_dict() for f in sorted(found)], indent=1) + "\n"


def from_json(text):
    return [Finding.from_dict(d) for d in json.loads(text)]


def _rule_id(f):
    return f.rule or f.cls


def to_sarif(found):
    """One run per tool. ruleId is the tool's rule, or the class when the tool has none."""
    runs = []
    for tool in sorted({f.tool for f in found}) or ["gauntlet"]:
        mine = sorted(f for f in found if f.tool == tool)
        ids = sorted({_rule_id(f) for f in mine})
        classes = {_rule_id(f): f.cls for f in mine}
        rules = [{"id": rid, "name": rid, "shortDescription": {"text": f"{rid}: {classes[rid]}"},
                  "properties": {"gauntlet-class": classes[rid]}} for rid in ids]
        driver = {"name": tool, "informationUri": INFO_URI, "rules": rules}
        if tool == "gauntlet":
            driver["version"] = __version__
        results = [{
            "ruleId": _rule_id(f), "ruleIndex": ids.index(_rule_id(f)), "level": "warning",
            "message": {"text": f"{f.cls}: {f.why}"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": f.path, "uriBaseId": "%SRCROOT%"},
                "region": {"startLine": max(int(f.line), 1)}}}],
            "partialFingerprints": {"gauntletSite/v1": f"{f.cls}|{f.site}"},
            "properties": {"class": f.cls, "site": f.site, "rule": f.rule},
        } for f in mine]
        runs.append({"tool": {"driver": driver}, "results": results})
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": runs}


def render(found, fmt="json"):
    if fmt == "sarif":
        return json.dumps(to_sarif(found), indent=1) + "\n"
    return to_json(found)
