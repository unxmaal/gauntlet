"""The findings format: JSON round trip, and SARIF 2.1.0 whose structure code scanning accepts."""

import json
from collections import namedtuple

from gauntlet import findings
from gauntlet.findings import Finding

Hit = namedtuple("Hit", "site line why")
FOUND = [Finding("identity-by-name-not-by-content", "infra/main.tf:aws_eip.a", 7, "count over a list", "gauntlet",
                 "hcl_count_by_position"),
         Finding("one-question-two-answers", "app/a.py:NAMES", 0, "same list twice", "gauntlet", ""),
         Finding("unclassified", "main.tf:aws_s3_bucket.b", 3, "no versioning", "checkov", "CKV_AWS_21")]


def test_json_round_trips_every_field():
    text = findings.to_json(FOUND)
    assert set(json.loads(text)[0]) == {"class", "site", "line", "why", "tool", "rule"}
    assert findings.from_json(text) == sorted(FOUND)


def test_hits_become_findings_with_their_class_and_rule():
    got = findings.from_hits({"c": [Hit("a.py:X", 3, "w")], "d": []}, rules={"c": "closed_tables"})
    assert got == [Finding("c", "a.py:X", 3, "w", "gauntlet", "closed_tables")]


def _check_sarif(doc):
    # The structure GitHub code scanning requires of SARIF 2.1.0, checked field by field.
    problems = []
    if doc.get("version") != "2.1.0" or "sarif-2.1.0" not in doc.get("$schema", ""):
        problems.append("version/$schema")
    if not isinstance(doc.get("runs"), list) or not doc["runs"]:
        return problems + ["runs"]
    for run in doc["runs"]:
        driver = run.get("tool", {}).get("driver", {})
        if not isinstance(driver.get("name"), str) or not driver["name"]:
            problems.append("driver.name")
        ids = [r.get("id") for r in driver.get("rules", [])]
        if len(ids) != len(set(ids)) or not all(isinstance(i, str) and i for i in ids):
            problems.append("rules ids")
        for res in run.get("results", []):
            i = res.get("ruleIndex", -1)
            if res.get("ruleId") not in ids or not 0 <= i < len(ids) or ids[i] != res["ruleId"]:
                problems.append(f"ruleId/ruleIndex {res.get('ruleId')}")
            if res.get("level") not in ("none", "note", "warning", "error"):
                problems.append("level")
            if not isinstance(res.get("message", {}).get("text"), str):
                problems.append("message.text")
            for loc in res.get("locations", []):
                phys = loc["physicalLocation"]
                uri = phys["artifactLocation"]["uri"]
                if uri.startswith("/") or ":" in uri or not uri:
                    problems.append(f"uri {uri}")
                if not (isinstance(phys["region"]["startLine"], int) and phys["region"]["startLine"] >= 1):
                    problems.append("startLine")
            if not all(isinstance(v, str) for v in res.get("partialFingerprints", {}).values()):
                problems.append("partialFingerprints")
    return problems


def test_sarif_is_structurally_valid_with_one_run_per_tool():
    doc = json.loads(findings.render(FOUND, "sarif"))
    assert _check_sarif(doc) == []
    assert [r["tool"]["driver"]["name"] for r in doc["runs"]] == ["checkov", "gauntlet"]
    results = doc["runs"][1]["results"]
    assert {r["locations"][0]["physicalLocation"]["region"]["startLine"] for r in results} == {1, 7}
    assert {r["properties"]["class"] for r in results} == {"identity-by-name-not-by-content",
                                                           "one-question-two-answers"}
    assert doc["runs"][1]["tool"]["driver"]["rules"][1]["id"] == "one-question-two-answers"


def test_the_sarif_check_catches_a_broken_document():
    doc = findings.to_sarif(FOUND)
    doc["runs"][0]["results"][0]["ruleIndex"] = 5
    doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]["startLine"] = 0
    doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = "/abs/x.tf"
    assert len(_check_sarif(doc)) == 3
    assert _check_sarif({"version": "2.1.0"}) != []


def test_an_empty_scan_is_still_a_valid_sarif_log():
    doc = findings.to_sarif([])
    assert _check_sarif(doc) == [] and doc["runs"][0]["results"] == []
