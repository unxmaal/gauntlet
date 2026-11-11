"""tflint, checkov and trivy JSON read as findings. The fixtures are invented in each tool's output shape."""

import json

import pytest

from gauntlet import adapters, cli
from gauntlet.findings import Finding

TFLINT = {"issues": [
    {"rule": {"name": "terraform_required_providers", "severity": "warning", "link": "https://example.com/r"},
     "message": "Missing version constraint for provider \"aws\"",
     "range": {"filename": "infra/main.tf", "start": {"line": 12, "column": 1}, "end": {"line": 12, "column": 9}}},
    {"rule": {"name": "terraform_naming_convention", "severity": "notice", "link": ""}, "message": "bad name",
     "range": {"filename": "infra/dns.tf", "start": {"line": 3, "column": 1}, "end": {"line": 3, "column": 9}}}],
    "errors": []}
CHECKOV = [{"check_type": "terraform", "results": {
    "passed_checks": [{"check_id": "CKV_TF_1", "check_name": "pinned", "file_path": "/infra/mod.tf",
                       "file_line_range": [1, 4], "resource": "module.net"}],
    "failed_checks": [{"check_id": "CKV_AWS_293", "check_name": "Ensure deletion protection",
                       "file_path": "/infra/db.tf", "repo_file_path": "/infra/db.tf", "file_line_range": [5, 20],
                       "resource": "aws_db_instance.main"},
                      {"check_id": "CKV_AWS_18", "check_name": "Ensure access logging",
                       "file_path": "/infra/s3.tf", "file_line_range": [1, 9], "resource": "aws_s3_bucket.logs"}],
    "skipped_checks": [], "parsing_errors": []}, "summary": {"passed": 1, "failed": 2}}]
TRIVY = {"SchemaVersion": 2, "Results": [
    {"Target": "infra/db.tf", "Class": "config", "Type": "terraform", "Misconfigurations": [
        {"ID": "AVD-AWS-0177", "AVDID": "AVD-AWS-0177", "Title": "Deletion protection", "Message": "off",
         "Severity": "MEDIUM", "Status": "FAIL",
         "CauseMetadata": {"Resource": "aws_db_instance.main", "Provider": "AWS", "StartLine": 5, "EndLine": 20}},
        {"ID": "AVD-AWS-0080", "AVDID": "AVD-AWS-0080", "Title": "Encryption", "Message": "ok",
         "Severity": "HIGH", "Status": "PASS", "CauseMetadata": {"StartLine": 5}}]},
    {"Target": "infra/empty.tf", "Class": "config", "Type": "terraform"}]}


def test_tflint_maps_known_rules_and_keeps_the_rest_unclassified():
    got = adapters.load("tflint", json.dumps(TFLINT))
    assert [(f.cls, f.site, f.line, f.tool, f.rule) for f in got] == [
        ("external-state-with-no-assertion-on-it", "infra/main.tf:terraform_required_providers@12", 12, "tflint",
         "terraform_required_providers"),
        ("unclassified", "infra/dns.tf:terraform_naming_convention@3", 3, "tflint", "terraform_naming_convention")]


def test_checkov_reads_failed_checks_only():
    got = adapters.load("checkov", json.dumps(CHECKOV))
    assert [(f.cls, f.site, f.line, f.rule) for f in got] == [
        ("irreplaceable-state-with-no-guard-on-its-destruction", "infra/db.tf:aws_db_instance.main", 5, "CKV_AWS_293"),
        ("unclassified", "infra/s3.tf:aws_s3_bucket.logs", 1, "CKV_AWS_18")]
    assert adapters.load("checkov", json.dumps(CHECKOV[0])) == got


def test_trivy_reads_failures_and_skips_passes():
    got = adapters.load("trivy", json.dumps(TRIVY))
    assert got == [Finding("irreplaceable-state-with-no-guard-on-its-destruction", "infra/db.tf:aws_db_instance.main",
                           5, "off", "trivy", "AVD-AWS-0177")]


def test_a_consumer_map_overrides_the_shipped_one():
    got = adapters.trivy(TRIVY, classes={"AVD-AWS-0177": "mine"})
    assert [f.cls for f in got] == ["mine"]


def test_an_unknown_tool_is_refused():
    with pytest.raises(SystemExit):
        adapters.load("conftest", "{}")


def test_cli_import_writes_sarif(tmp_path):
    (tmp_path / "t.json").write_text(json.dumps(TFLINT), encoding="utf-8")
    out = tmp_path / "out.sarif"
    assert cli.main(["import", "tflint", str(tmp_path / "t.json"), "--format", "sarif", "--output", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["runs"][0]["tool"]["driver"]["name"] == "tflint" and len(doc["runs"][0]["results"]) == 2
