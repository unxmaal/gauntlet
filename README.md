# gauntlet

A corpus of logical failure classes that have bitten real code, each with a trigger question and a test shape,
plus the Python machinery that holds a repo to it: the skill parser, AST detectors for tier-2 classes, text
scanners for tier-1 classes, review heuristics for a diff, the audit, the mutation ratchet and the
inventory-format check.

- `corpus/`: `SKILL.md`, `general-corpus.md`, `shapes.md`, the Stop hook and its test. This is the Claude skill.
- `src/gauntlet/`: the package. Standard library only; the HCL front end needs the `hcl` extra (python-hcl2).

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

Declared move tables go in `Registry(moves=(...))`, one `gauntlet.moves.MoveTable` each:

```python
from gauntlet.moves import MoveTable, reopens_by
from app import job_state as j

MoveTable("app/job_state.py:MOVES", j.MOVES, initial=[j.PENDING], terminal=[j.DONE, j.FAILED],
          reopens=reopens_by(j.MOVES, {j.REFETCH}), states=j.STATES)
```

`moves` accepts `{state: next_states}`, `{(leaves, enters): reasons_or_writers}` or a set of `(leaves, enters)`
pairs; `moves.merge(...)` unions per-writer tables. The ratchet then fails on an unreachable state, a non-terminal
dead end, a terminal that moves by anything but a named reopen, and a declared or used state the table never names.
It also fails on any module constant named `MOVES`, `TRANSITIONS` or `*_MOVES` holding such pairs that has no
`MoveTable` and no reason in `Registry(move_waivers={site: why})`. Without a Registry, `moves.assert_sound(tables)`
is the same check as a plain assertion.

A detector that needs repo knowledge takes it from `Registry(detector_config={name: kwargs})`.
`commit_then_link` finds committing helpers by their `conn.commit()`; name others with
`{"commit_then_link": {"committers": ("put",)}}`. `terminal_without_reader` gets `Registry.moves` unless
`detector_config` gives it `tables`, and otherwise reads the declared `MOVES` literals. `unscoped_query` finds nothing
until it is told the scope: `{"unscoped_query": {"columns": ("machine_id",), "tables": ("jobs",), "helpers": ("_here",),
"null_helper": "app/scope.py:here"}}`; a table whose CREATE TABLE names a scope column is scoped without being listed,
and uppercase SQL keywords are what mark a string as a query. `stored_pid_liveness` finds a helper that only does
`os.kill(pid, 0)`; name others with `{"stored_pid_liveness": {"alive": ("running",)}}`. `prose_match` needs the
fields that hold prose: `{"prose_match": {"fields": ("note", "detail", "reason")}}`. `empty_on_failure` finds
store choosers only once named: `{"empty_on_failure": {"choosers": ("backend",)}}`, and a module that defines a chooser
is not a copy of it. `unpaired_column` reads the schema from `{"unpaired_column": {"migrations": ("app/migrations/",),
"schema": ("app/schema.py",)}}` (`schema` defaults to `migrations`); neither path counts as a writer or reader, and a
column whose rows are selected with `*` is read by a key lookup anywhere. All three match SQL by its uppercase keywords,
so lowercase SQL is not seen. `one_sided_schema_check` needs the version names and the check entry points:
`{"one_sided_schema_check": {"versions": ("SCHEMA_VERSION", "stored_schema"), "phases": ("smoke",), "markers":
("SMOKE_PHASE",)}}`. `effect_reachable` needs the effects and the live-run predicate, and refuses effect binaries
(osascript, launchctl, ssh, gh and the like; override with `binaries`) unless the test guard lists them:
`{"effect_reachable": {"effects": ("alert", "post"), "live": ("DEFAULT_HOME",), "guarded": ("osascript",),
"spawners": ("spawn_group",)}}`. `one_sided_schema_check` also takes `skips`, helpers that log and return nothing.
`unchecked_order` takes `{"effects": ..., "reads": ("hf_home",), "entries": ("main", "__main__"), "guard":
("env.guard",), "needs": ("models_dir",), "pairs": {"APP_HOME": ("APP_STORE",)}}`; without config it still finds
environment and `home()` reads at import. Both `effect_reachable` and `unchecked_order` report an effect bound as a
default argument at the same site, so a consumer that binds both classes binds or waives it under each. An exempt
loop or child is a
waiver with a reason in the class's index entry, like any other site. New code is held at zero; a consumer's
existing hits go in its backlog.

## Findings, front ends and other tools' output

Every front end and adapter emits one format, `{class, site, line, why, tool, rule}`, as JSON or SARIF 2.1.0, so
code scanning can show it without the gauntlet installed:

    gauntlet scan [ROOT] --frontend hcl --format sarif --output gauntlet.sarif
    gauntlet scan --registry tests.gauntlet.registry:REGISTRY          # the registry's index names the classes
    gauntlet import tflint|checkov|trivy tool-output.json --format sarif

A front end is `discover(root) -> files` and `scan(root, files, index, config) -> findings`. `python` is the AST
detectors above. `hcl` (Terraform/OpenTofu, the `gauntlet[hcl]` extra) reads `.tf`, `.tfvars`, `.tftest.hcl`, backend
config files, lock files, and the scripts and CI files that run terraform. With no registry each detector reports
under its default class; with one, `index[cid]["detector"]` names it. `Registry(frontends=("python", "hcl"))` puts HCL
findings under the same ratchets: bind a site with `@pytest.mark.gauntlet(cid, site="infra/db.tf:aws_db_instance.main")`,
waive it, or backlog it.

| detector | default class | fires on |
|---|---|---|
| `hcl_duplicated_literal` | one-question-two-answers | a CIDR, region, account id, domain or name in two .tf/.tfvars files |
| `hcl_env_two_inputs` | a-non-production-run-writes-live-state | backend chosen at init, env by -var-file, no condition ties them (`ties`) |
| `hcl_shared_backend` | a-non-production-run-writes-live-state | two roots with one backend key, or one backend file split only by a CLI key |
| `hcl_stateful_unprotected` | irreplaceable-state-with-no-guard-on-its-destruction | a stateful resource with no `prevent_destroy`, or `skip_final_snapshot` reachable as true (`stateful`) |
| `hcl_count_by_position` | identity-by-name-not-by-content | `count.index` into a data source list |
| `hcl_lock_missing` | external-state-with-no-assertion-on-it | a root module whose `.terraform.lock.hcl` is missing or git-ignored |
| `hcl_constant_assert` | a-metric-quoted-without-a-negative-control | a test assert whose operands are only constants and variables |
| `hcl_test_skips_tfvars` | a-proxy-checked-in-place-of-the-property | a test that names an env whose tfvars nothing passes to `terraform test` |
| `hcl_unchecked_external` | external-state-with-no-assertion-on-it | an output copied out of band (`copied`, default `name_servers`) with no check |
| `hcl_order_in_prose` | an-order-the-code-needs-but-does-not-check | an apply order stated only in a description or comment |

A file python-hcl2 cannot parse is a `hcl_unparsed` finding, not a silence. The adapters map rule ids to classes in
`gauntlet.adapters.CLASSES` and keep an unmapped rule as `unclassified`. They parse the tools' JSON and never run them.

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
