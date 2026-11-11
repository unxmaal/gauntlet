"""Front ends: discover(root) -> files, scan(root, files) -> findings. Python is the first; HCL is `gauntlet[hcl]`."""

from pathlib import Path
from typing import Protocol

from gauntlet import detectors, findings
from gauntlet.registry import default_production


class FrontEnd(Protocol):
    name: str

    def detectors(self) -> set: ...

    def discover(self, root, lister=None) -> list: ...

    def scan(self, root, files, index=None, config=None) -> list: ...


def _default_index(names):
    # With no registry, each detector reports under its own name.
    return {name: {"detector": name} for name in names}


class PythonFrontEnd:
    name = "python"

    def __init__(self, production=default_production, extra=None):
        self.production = production
        self.extra = extra or {}

    def detectors(self):
        return set(detectors.DETECTORS) | set(self.extra)

    def discover(self, root, lister=None):
        return sorted(detectors.sources(root, production=self.production, lister=lister))

    def scan(self, root, files, index=None, config=None):
        root = Path(root)
        texts = {rel: (root / rel).read_text(encoding="utf-8", errors="replace") for rel in files}
        index = index if index is not None else _default_index(self.detectors())
        hits = detectors.run(detectors.parse(texts), index, self.production, self.extra, config)
        rules = {cid: e.get("detector", "") for cid, e in index.items()}
        return findings.from_hits(hits, "gauntlet", rules)


def get(name, production=default_production, extra=None):
    if name == "python":
        return PythonFrontEnd(production, extra)
    if name == "hcl":
        from gauntlet import hcl
        return hcl.HclFrontEnd()
    raise SystemExit(f"no front end named {name!r}: want python or hcl")


def detector_names(names):
    """Every detector name the named front ends know, without importing an optional parser."""
    out = set()
    for name in names:
        if name == "python":
            out |= set(detectors.DETECTORS)
        elif name == "hcl":
            from gauntlet import hcl
            out |= set(hcl.DETECTORS)
    return out
