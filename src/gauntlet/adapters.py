"""Import tflint, checkov and trivy JSON output as findings, mapping each tool's rule id to a corpus class."""

import json

from gauntlet.findings import Finding

UNMAPPED = "unclassified"

CLASSES = {
    "tflint": {
        "terraform_required_providers": "external-state-with-no-assertion-on-it",
        "terraform_required_version": "external-state-with-no-assertion-on-it",
        "terraform_module_pinned_source": "external-state-with-no-assertion-on-it",
        "terraform_module_version": "external-state-with-no-assertion-on-it",
    },
    "checkov": {
        "CKV_TF_1": "external-state-with-no-assertion-on-it",
        "CKV_TF_2": "external-state-with-no-assertion-on-it",
        "CKV_AWS_139": "irreplaceable-state-with-no-guard-on-its-destruction",
        "CKV_AWS_293": "irreplaceable-state-with-no-guard-on-its-destruction",
    },
    "trivy": {
        "AVD-AWS-0177": "irreplaceable-state-with-no-guard-on-its-destruction",
    },
}


def _rel(path):
    return str(path or "").lstrip("/").removeprefix("./")


def _cls(tool, rule, classes):
    table = {**CLASSES.get(tool, {}), **(classes or {})}
    return table.get(rule, UNMAPPED)


def tflint(data, classes=None):
    out = []
    for issue in data.get("issues") or []:
        rule = (issue.get("rule") or {}).get("name", "")
        rng = issue.get("range") or {}
        rel = _rel(rng.get("filename"))
        line = int((rng.get("start") or {}).get("line") or 0)
        out.append(Finding(_cls("tflint", rule, classes), f"{rel}:{rule}@{line}", line, issue.get("message", ""),
                           "tflint", rule))
    return out


def checkov(data, classes=None):
    out = []
    for report in data if isinstance(data, list) else [data]:
        for check in (report.get("results") or {}).get("failed_checks") or []:
            rule = check.get("check_id", "")
            lines = check.get("file_line_range") or [0]
            rel = _rel(check.get("repo_file_path") or check.get("file_path"))
            out.append(Finding(_cls("checkov", rule, classes), f"{rel}:{check.get('resource', '')}", int(lines[0]),
                               check.get("check_name", ""), "checkov", rule))
    return out


def trivy(data, classes=None):
    out = []
    for result in data.get("Results") or []:
        rel = _rel(result.get("Target"))
        for m in result.get("Misconfigurations") or []:
            if m.get("Status", "FAIL") != "FAIL":
                continue
            rule = m.get("AVDID") or m.get("ID", "")
            cause = m.get("CauseMetadata") or {}
            resource = cause.get("Resource", "")
            out.append(Finding(_cls("trivy", rule, classes), f"{rel}:{resource or rule}",
                               int(cause.get("StartLine") or 0), m.get("Message") or m.get("Title", ""), "trivy",
                               rule))
    return out


ADAPTERS = {"tflint": tflint, "checkov": checkov, "trivy": trivy}


def load(tool, text, classes=None):
    if tool not in ADAPTERS:
        raise SystemExit(f"no adapter for {tool!r}: want one of {', '.join(sorted(ADAPTERS))}")
    return sorted(ADAPTERS[tool](json.loads(text), classes))
