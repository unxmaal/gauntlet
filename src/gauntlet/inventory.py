"""The standard inventory file shapes.md defines: <question-id>-<shortsha>.md, a header, one ranked table."""

import re
from pathlib import Path

from gauntlet import skill

COLUMNS = ("Sev", "Id", "Entity", "Sites", "Shape", "Unhandled", "Bitten", "Scannable")
HEADER = ("repo", "commit", "date", "question", "scope", "enumerate")
SEVERITIES = ("high", "medium", "low")
_NAME = re.compile(r"^(?P<q>[a-z]{2})-(?P<sha>[0-9a-f]{7,40})\.md$")
_QUESTION = re.compile(r"^\*\*([A-Z]{2})\. ", re.M)


def questions(directory=None):
    directory = skill.skill_dir() if directory is None else Path(directory)
    return set(_QUESTION.findall((directory / "shapes.md").read_text(encoding="utf-8")))


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def check(text, name, known):
    out = []
    m = _NAME.match(name)
    if not m:
        out.append(f"{name}: want <question-id>-<shortsha>.md, e.g. sm-1a2b3c4.md")
    elif m["q"].upper() not in known:
        out.append(f"{name}: {m['q'].upper()} is not a question in shapes.md")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    head = "\n".join(lines[:start]).lower()
    out += [f"{name}: the header names no {field}" for field in HEADER if not re.search(rf"\b{field}\w*\b", head)]
    if start is None:
        return out + [f"{name}: no table"]
    if tuple(_cells(lines[start])) != COLUMNS:
        return out + [f"{name}: the table's columns are {_cells(lines[start])}, want {list(COLUMNS)}"]
    rows = []
    for n, line in enumerate(lines[start + 2:], start=start + 3):
        if not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        if len(cells) != len(COLUMNS):
            out.append(f"{name}:{n}: {len(cells)} cells, want {len(COLUMNS)}")
            continue
        row = dict(zip(COLUMNS, cells))
        sev = row["Sev"].lower()
        if sev not in SEVERITIES:
            out.append(f"{name}:{n}: severity {row['Sev']!r} is not one of {', '.join(SEVERITIES)}")
        else:
            rows.append((n, SEVERITIES.index(sev)))
        if row["Id"].split("-")[0] not in known:
            out.append(f"{name}:{n}: id {row['Id']!r} does not start with a question id")
        out += [f"{name}:{n}: {col} is empty" for col in COLUMNS if not row[col]]
    out += [f"{name}:{b}: ranked below a lower severity at line {a}" for (a, x), (b, y) in zip(rows, rows[1:]) if y < x]
    if not rows and not any(p.startswith(f"{name}:") and "cells" in p for p in out):
        out.append(f"{name}: the table has no rows")
    return out


def check_files(paths, directory=None):
    known = questions(directory)
    out = []
    for path in map(Path, paths):
        out += check(path.read_text(encoding="utf-8"), path.name, known)
    return out
