"""Which files a repo publishes, and which of them are Python or shell."""

import re
import subprocess
from pathlib import Path


def names(root, *within):
    """Tracked plus untracked-not-ignored paths, relative and /-separated, as git prints them."""
    seen, out = set(), []
    spec = ("--", *within) if within else ()
    for args in (("ls-files",), ("ls-files", "--others", "--exclude-standard")):
        r = subprocess.run(("git", "-C", str(root)) + args + spec, capture_output=True, text=True,
                           encoding="utf-8", check=True)
        for line in r.stdout.splitlines():
            if line and line not in seen:
                seen.add(line)
                out.append(line)
    return out


def publishable(root):
    return [Path(root) / line for line in names(root)]


def _shebang(path):
    with open(path, "rb") as f:
        first = f.readline(200)
    return first.decode("utf-8", "replace") if first.startswith(b"#!") else ""


def is_python(path):
    return path.suffix == ".py" or (path.suffix == "" and "python" in _shebang(path))


def is_shell(path):
    return path.suffix in (".sh", ".bash") or (
        path.suffix == "" and bool(re.search(r"\b(?:ba|z)?sh\b", _shebang(path))))
