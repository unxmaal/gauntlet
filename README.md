# gauntlet

A corpus of logical failure classes that have bitten real code, each with a trigger question and a test shape,
plus the Python machinery that holds a repo to it: the skill parser, AST detectors for tier-2 classes, text
scanners for tier-1 classes, review heuristics for a diff, the audit, the mutation ratchet and the
inventory-format check.

- `corpus/`: `SKILL.md`, `general-corpus.md`, `shapes.md`, the Stop hook and its test. This is the Claude skill.
- `src/gauntlet/`: the package. Standard library only.

## Install

The skill: symlink the skill directory to this checkout's corpus.

    ln -sfn "$PWD/corpus" ~/.claude/skills/gauntlet

The package, pinned to a tag:

    pip install "git+https://github.com/unxmaal/gauntlet@v0.1.0"
    gauntlet classes          # the corpus this version ships
    gauntlet corpus-dir       # where it lives

The installed package carries its own copy of the corpus, so a consumer's tests read the pinned version and not
whatever is in the local skill directory. `GAUNTLET_SKILL_DIR` overrides it.

## Wiring a consumer's registry

The registry stays in the consumer's repo: class instances, waivers, the detector backlog, the defects snapshot
and what counts as production code. Describe it once:

```python
# tests/gauntlet/registry.py
from pathlib import Path
from gauntlet.registry import Registry
from tests.gauntlet import classes

HERE = Path(__file__).resolve().parent
REGISTRY = Registry(
    root=HERE.parent.parent,
    index=classes.INDEX, pending=classes.PENDING, unclassified=classes.UNCLASSIFIED,
    snapshot_file=HERE / "skill_classes.json", defects_file=HERE / "defects.json",
    backlog_file=HERE / "backlog.json", backlog_pin=62,
    repo_slug="owner/repo",
    production=lambda rel: not rel.startswith(("tests/", "evals/cases/")),
    code_paths=r"^(?:src/|scripts/)",
)
```

Run the ratchets as tests:

```python
# tests/test_gauntlet.py
import gauntlet.ratchet
from tests.gauntlet.registry import REGISTRY

globals().update(gauntlet.ratchet.tests(REGISTRY))
```

Bind a detector hit to a test with `@pytest.mark.gauntlet("class-id", site="path:NAME")`; the package registers
the marker. Repo-specific detectors go in `tests(REGISTRY, extra_detectors={...})`.

The CLI takes the same object:

    gauntlet audit --registry tests.gauntlet.registry:REGISTRY [--offline]
    gauntlet review --registry tests.gauntlet.registry:REGISTRY origin/main...HEAD
    gauntlet snapshot-skill --registry tests.gauntlet.registry:REGISTRY
    gauntlet snapshot-defects --registry tests.gauntlet.registry:REGISTRY
    gauntlet inventory tmp/gauntlet/sm-1a2b3c4.md
    gauntlet mutation --config tests.gauntlet.mutation:CONFIG run --module src/pkg/core.py

`--config` names a `gauntlet.mutation.MutationConfig`: the modules to mutate, the tests selected for each, the
pin file, and any environment the tree needs.

## Development

    uv sync
    uv run pytest
