"""Reads the corpus: each heading with a **Tier:** line is a class, with its trigger and test shape."""

import json
import os
import re
from importlib import resources
from pathlib import Path

SKILL_FILES = ("SKILL.md", "general-corpus.md")


def slug(heading):
    text = re.sub(r"^\d+\.\s*", "", heading.strip())
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _field(body, label):
    # The paragraph that opens with **Label:**, joined onto one line.
    lead = f"**{label}:**"
    for i, line in enumerate(body):
        if line.startswith(lead):
            para = [line[len(lead):]]
            for nxt in body[i + 1:]:
                if not nxt.strip():
                    break
                para.append(nxt)
            return " ".join(" ".join(para).split())
    return ""


def parse_skill(text, source):
    found, heading, body = [], None, []

    def close():
        if heading is None:
            return
        m = re.search(r"\*\*Tier:\*\*\D*?(\d)", "\n".join(body))
        b = re.search(r"\*\*Bitten:\*\*\D*?(\d+)", "\n".join(body))
        if m:
            found.append({"id": slug(heading), "heading": heading, "source": source, "tier": int(m.group(1)),
                          "bitten": int(b.group(1)) if b else None,
                          "trigger": _field(body, "Trigger"), "test_shape": _field(body, "Test shape")})

    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("# "):
            close()
            heading = line[3:].strip() if line.startswith("## ") else None
            body = []
        elif heading is not None:
            body.append(line)
    close()
    return found


def corpus_dir():
    """The corpus this installed version ships: package data in a wheel, the checkout's corpus/ in a source tree."""
    packaged = Path(str(resources.files("gauntlet"))) / "corpus"
    if (packaged / "SKILL.md").is_file():
        return packaged
    return Path(__file__).resolve().parents[2] / "corpus"


def skill_dir():
    """$GAUNTLET_SKILL_DIR when set, else the corpus pinned with this package."""
    override = os.environ.get("GAUNTLET_SKILL_DIR")
    return Path(override) if override else corpus_dir()


def read_skill(directory=None):
    directory = skill_dir() if directory is None else Path(directory)
    out = []
    for name in SKILL_FILES:
        out += parse_skill((directory / name).read_text(encoding="utf-8"), name)
    return out


def write_snapshot(path, found):
    Path(path).write_text(json.dumps(found, indent=1) + "\n", encoding="utf-8")


def load_snapshot(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
