# gauntlet

gauntlet finds logic flaws that ordinary tests miss, and stops a repo from adding new ones.

Unit tests check that code does what its author meant. They don't check whether the author meant the right thing.
Most bugs that reach production are of the second kind. Two files each hold their own copy of a setting, and the
copies drift apart. A query forgets which tenant it belongs to. A process ID is saved to disk and later trusted after
the operating system has reused it. A state machine has a state that nothing ever leaves. Each of these looks like a
one-off when it happens. They aren't one-offs: the same few shapes keep coming back in different code.

gauntlet writes those shapes down and checks code against them. It has two parts:

- **The corpus** (`corpus/`): a list of failure classes. Each one is a kind of logic flaw that has caused at least
  one real bug. Each entry gives a trigger question, a test shape, and the real instances that earned it a place.
  The corpus is also a Claude Code skill, so an agent can apply it while writing or reviewing code.
- **The package** (`src/gauntlet/`): Python code that finds those classes mechanically and turns them into failing
  tests in your repo. It uses only the standard library; reading Terraform needs the `hcl` extra.

## Why use it

- **It catches what linters and type checkers don't.** Those tools check one line or one function at a time. The
  flaws here span files: a value defined in two places, a column written but never read, a guard function with no
  caller, an effect a test run can trigger against live state.
- **It adopts without a cleanup project.** `gauntlet init` records every existing hit as a backlog. Your suite
  passes on day one, and from then on only new hits fail. The backlog can only shrink: a row that is not in
  the backlog at the merge base with `origin/main` fails.
- **Every hit leaves a decision behind.** You can't silence a hit. You bind a test that proves the site is safe,
  or you waive it with a written reason, and both are checked on every run. A binding or waiver whose site has gone
  away also fails, so the record can't go stale.
- **It is versioned.** A repo pins a gauntlet tag. The corpus ships inside the package, so your tests run against
  the version you pinned, not whatever a laptop has installed.

## How it works

### The corpus

Each class in `corpus/SKILL.md` looks like this (shortened):

> **One question, two answers**
> **Trigger:** is this value, list, or policy written down in more than one place?
> **Tier:** 1 where the copies can be parsed; 2 otherwise.
> **Test shape:** read both copies from source and assert they agree.
> **Instances:** a model list forked into a shell file and a Python file; ...

The **tier** says how the class can be checked:

| tier | meaning | how gauntlet checks it |
|---|---|---|
| 1 | One repo-wide scan covers every file, including files not written yet. | A scanner in `gauntlet.scanners` that a repo runs as a test. |
| 2 | Has to be judged at each site where the trigger fires. | An AST detector finds candidate sites; each site needs a bound test or a waiver. |
| 3 | Can't be mechanised. A test would pass while the reasoning was still wrong. | A review question raised on a diff, answered by a person. |

`general-corpus.md` holds candidate classes taken from general experience rather than from a real bug here. A
candidate moves into `SKILL.md` once it causes a real bug. `shapes.md` holds whole-codebase inventory questions,
such as "list every lifecycle" or "list every query's scope", in one standard file format.

### The machinery

- **Detectors** (`gauntlet.detectors`, Python AST; `gauntlet.hcl`, Terraform). Each one reports *sites*, such as
  `app/jobs.py:claim`, where a class may apply. A detector reports candidates, not verdicts.
- **Scanners** (`gauntlet.scanners`) run over every file, for tier-1 classes: swallowed exceptions, unchecked exit
  codes in shell scripts, mutable default arguments, bare `bash` calls, platform-specific primitives and similar.
  Each one comes with an inventory count pinned in your tests, and the count may only go down.
- **Review heuristics** (`gauntlet review`). Each tier-3 class can carry a regex. The command checks the lines a
  diff adds, and when one matches, it prints that class's trigger and test shape for a person to answer.
- **The registry** (yours, in `tests/gauntlet/registry.py`). It records which classes your repo holds itself to,
  what counts as production code, your waivers, your backlog, and your defect history.
- **The ratchet tests** (`gauntlet.ratchet`) turn all of the above into ordinary pytest tests. See the next section.
- **The audit** (`gauntlet audit`) reads your closed defect issues and merged PRs and lists each fixed defect that
  no class covers. That gap is how the corpus grows: name the general flaw behind the defect, then add it.
- **Mutation testing** (`gauntlet mutation`) runs mutmut on your core modules and pins the surviving mutants, so
  the tests can't quietly get weaker.
- **The Stop hook** (`corpus/stop-hook.sh`). Registered as a Claude Code Stop hook, it makes an agent whose turn
  changed source code answer the corpus triggers for the changed files before the turn can end.

### Following one hit through

1. Someone adds `DEVICES = ("cpu", "mps")` to a second module. The `duplicated_literals` detector reports both
   sites under `one-question-two-answers`.
2. Neither site is bound, waived or in the backlog, so this test fails:

       one-question-two-answers: app/b.py:DEVICES
           bind a test with @pytest.mark.gauntlet("one-question-two-answers", site="app/b.py:DEVICES") or waive it with a reason

3. You choose one:
   - **Fix it.** Import the tuple from one place. The hit goes away.
   - **Bind it.** Write a test that proves the copies agree, and mark it
     `@pytest.mark.gauntlet("one-question-two-answers", site="app/b.py:DEVICES")`.
   - **Waive it.** In the registry index, add
     `"waivers": {"app/b.py:DEVICES": "unrelated: a list of CI runners, not a device policy"}`.
4. Later someone deletes `app/b.py`. The binding or waiver now points at nothing, so the stale-binding test fails
   until you remove it.

### Implicit state machines

The class `a-lifecycle-with-no-declared-state` asks one question of any entity that moves through stages (a job
that is queued, running, then done; an order that is paid, shipped or cancelled): is the current stage one field,
written by one function that refuses an illegal move? When it is not, two writers can each move the same entity, a
crash can leave it half moved, and a combination nobody planned for (finished but never started) gets read as some
stage anyway.

The detectors look for the places where a lifecycle exists but nothing declares it:

- **A state write with no guard.** `unguarded_transition` reports an `UPDATE ... SET state = ...` whose `WHERE`
  does not name the state it leaves, so it cannot lose a race. It also reports `job.state = "done"` (any
  `<expr>.state` or `<expr>.status`) inside a function that has not compared that same attribute earlier, by
  `==`, `in` or a `match`. It only looks at attributes some code compares, and only at assignments of a string,
  an upper-case constant or an enum member. Constructors (`__init__`, `__post_init__`), objects the function
  built itself, and functions named in `movers` or that consult a `MOVES` table are left alone.
- **A state written from several modules.** One table column (`table:jobs.state`) or one class attribute
  (`app/jobs.py:Job.state`) written from two or more modules is reported once. The class comes from `self`, a
  parameter annotation, a local built by the constructor, or the one class that declares the attribute.
- **State read from the newest row.** `latest_row_state` (class `state-is-whichever-row-came-last`) reports a
  query that takes the current state from `ORDER BY ... DESC LIMIT 1`.
- **A stage inferred from fields.** `inferred_stage` reports an entity with no state field whose stage is read from
  which of several timestamps or flags are set. It needs two marker fields that are written at separate sites
  (each writer sets one and leaves the other) and some code that reads them together: `started_at IS NOT NULL AND
  finished_at IS NULL` in one `WHERE`, or `row["started_at"] and not row["finished_at"]`,
  `job.paid_at and not job.shipped_at` in Python. The entity is a SQL table (columns from `UPDATE ... SET` and
  `INSERT INTO t (...)`, site `table:<name>`) or a Python class (attributes written outside the constructor, site
  `path:Class`). Markers are names ending `_at` plus flags such as `done`, `complete`, `finished`, `cancelled`,
  `removed`, `started`; `created_at`, `updated_at`, `modified_at` and `inserted_at` never count. A table or class
  that also writes a `state` or `status` field is not reported, since its stage is declared.

To fix a hit, declare the lifecycle. Put the states and the legal moves in one module, make one function the only
writer, and have it refuse a move the table does not list (a compare-and-set `UPDATE ... WHERE state = ?` for a
row). Then register the table:

```python
MoveTable("app/job_state.py:MOVES", MOVES, initial=["pending"], terminal=["done", "failed"])
```

Once registered, the ratchets hold it. `test_gauntlet_every_move_table_is_sound` fails on an unreachable state, a
non-terminal state nothing leaves, a terminal state that moves without a named reopen, or a state the code uses that
the table never names. `test_gauntlet_every_move_table_is_registered` fails when a new `MOVES`-shaped constant
appears with no `MoveTable` and no waiver. `terminal_without_reader` reads the registered tables and reports a
terminal state that only its writers mention, so nothing settles, expires or reports a row that reaches it. The
mover itself stops being an unguarded write: its `WHERE` names the state, or it consults `MOVES`, or it is named in
`movers`.

Some of this stays with the reviewer. The detectors read Python and the SQL strings in it. A lifecycle in another
language, a stage kept as a file on disk (a marker, a lock, a JSON record), a stage read from whether a process is
alive, or a state attribute set through `setattr` or a generic setter that takes the new state as an argument is not
seen. The trigger question in the corpus covers those; answer it in review.

## The tests it adds to your repo

`globals().update(gauntlet.ratchet.tests(REGISTRY))` in `tests/test_gauntlet.py` adds these:

| test | fails when | what to do |
|---|---|---|
| `test_gauntlet_no_new_code_trips_a_detector_without_a_bound_test` | A detector reports a site that has no bound test, no waiver and no backlog entry. | Fix the code, bind a test to the site, or waive it with a reason. |
| `test_gauntlet_the_backlog_only_shrinks` | A backlog site has been fixed, bound or waived but is still listed; a row is in the backlog now but not at the merge base with `backlog_base` (default `origin/main`); or that base can't be resolved. A row that moved files is allowed: same class, same site after the path, and exactly one row on each side matches; the test passes with a warning naming the move. | Delete the row's line from `backlog.txt`. For a joined row, bind or waive the site instead. For a site that moved, change its line to the new path (the failure names it). For a missing base, run `git fetch origin main`; a CI checkout needs `fetch-depth: 0`. |
| `test_gauntlet_no_binding_or_waiver_is_stale` | A binding or waiver names a site the detectors no longer report, or a test is bound to a class with no detector. | Delete the binding or waiver. |
| `test_gauntlet_registry_is_clean` | An index entry names a class missing from the corpus, lacks `instances` or `scanners`, has a waiver with no reason, or names a scanner that doesn't exist; or a defect in the snapshot has no class and no reason. | Fix the registry entry the message names. |
| `test_gauntlet_snapshot_is_the_pinned_corpus` | The corpus your pinned gauntlet ships differs from `skill_classes.json`. This happens after you move to a new tag. | Run `gauntlet snapshot-skill --registry ...` and review the diff. |
| `test_gauntlet_ids_are_unique` | Two classes share an id, or a pending class is now defined in the corpus. | Rename the class, or move it from pending into the index. |
| `test_gauntlet_every_named_detector_exists` | The index or `detector_config` names a detector that doesn't exist. Usually a typo, or a detector renamed in a new tag. | Correct the name. |
| `test_gauntlet_review_heuristics_are_present_and_quiet` | A review-only class has no `review` regex, or its regex matches ordinary code such as `import os`. | Add a regex, or make it narrower. |
| `test_gauntlet_every_move_table_is_sound` | A declared state machine has an unreachable state, a non-terminal dead end, a terminal state that moves without a named reopen, or a state it never names. | Fix the table or the code it describes. |
| `test_gauntlet_every_move_table_is_registered` | A constant named `MOVES`, `TRANSITIONS` or `*_MOVES` holds state transitions but has no `MoveTable` and no waiver. | Register it in `Registry(moves=...)` or waive it in `move_waivers`. |

Each failure message lists every offending site, so one run shows the whole list.

### What the Python detectors look for

| detector | class | fires on |
|---|---|---|
| `duplicated_literals` | one-question-two-answers | the same literal list written down in two modules |
| `closed_tables` | a-closed-table-fronting-an-open-set | a fixed table of names looked up with a fallback or a membership test, so an unlisted name silently gets a default |
| `unreached_guards` | a-guard-built-and-tested-that-nothing-invokes | a check or guard function that no production code calls |
| `ignored_options` | an-input-accepted-and-silently-ignored | a command-line option that is parsed and never read |
| `member_defaults` | one-default-tuned-for-one-member-applied-to-all | one constant used as the default for a value that differs per model or machine |
| `harness_verdicts` | a-fact-about-the-harness-recorded-as-a-verdict-on-the-subject | a final verdict written from an exception or exit status, which may be the harness's fault and not the subject's |
| `env_seams` | each-half-verified-against-its-own-spec-the-seam-against-nothing | an environment variable read in one place and set in another, a handoff that each side's tests miss |
| `loopback_equality` | identity-by-name-not-by-content | an equality test against a `localhost` URL used to decide which server is on the other end |
| `one_sided_schema_check` | a-deploy-runs-two-releases-at-once | a schema version check made on only one side of a deploy |
| `effect_reachable` | an-effect-reachable-from-a-run-that-is-not-the-live-deploy | a real side effect (alerts, `launchctl`, `gh`, `ssh`) that a test or dev run can reach |
| `unchecked_order` | an-order-the-code-needs-but-does-not-check | code that needs setup to happen first, such as environment reads at import, with nothing enforcing the order |
| `prose_match` | a-decision-read-back-out-of-a-sentence-the-code-wrote | a decision made by matching text the program itself wrote as prose |
| `empty_on_failure` | nothing-and-failed-return-the-same-value | a function that returns the same empty value for "nothing found" and "it broke" |
| `unpaired_column` | a-stored-value-with-no-live-writer-or-no-reader | a column (SQL or ORM) that nothing writes, or that nothing reads |
| `unscoped_query` | a-shared-store-query-with-no-scope-or-a-scope-with-its-own-idea-of-null | a query on a shared table that leaves out the tenant or machine scope |
| `stored_pid_liveness` | a-stored-pid-trusted-as-the-same-process | a saved PID checked only with `os.kill(pid, 0)`, which is true for any process that later reuses the PID |
| `uncached_key` | a-copy-whose-key-leaves-out-an-input-or-that-nothing-invalidates | a cache over files or subprocesses whose key leaves out an mtime or other input, or a JSON file rewritten with no lock |
| `commit_then_link`, `torn_append` | two-durable-writes-with-no-repair-for-the-gap | two writes that must both land, with nothing to repair a crash between them |
| `orphanable_child` | a-child-that-outlives-its-owner | a subprocess that can keep running after its parent dies |
| `undeadlined_wait`, `terminal_without_reader` | a-wait-with-no-deadline-owner-or-end-state | a loop or wait with no deadline, or a terminal state nothing reads |
| `row_index_keys` | order-assumed-on-unordered-data | a dict keyed on an `enumerate()` index, which pairs rows by list position instead of identity |
| `latest_row_state` | state-is-whichever-row-came-last | current state read from the newest row of an append-only table |
| `unguarded_transition` | a-lifecycle-with-no-declared-state | an `UPDATE` that sets a state or status (or a configured lifecycle column) with no `WHERE` on the state it leaves, an `obj.state = X` with no check of the state it leaves, or one state column or attribute written from several modules |
| `inferred_stage` | a-lifecycle-with-no-declared-state | a table or class with no state field whose stage is read from which timestamps or flags are set, each written at a separate site |

Most detectors work with no configuration. The ones that need to know your repo's names (the scope column, the
migration path, the effect functions) find nothing until `detector_config` tells them; see below. The Terraform
detectors are listed under "Findings, front ends and other tools' output".

## Quick start

Install the skill, so Claude Code sessions load the corpus:

    git clone https://github.com/unxmaal/gauntlet && cd gauntlet
    ln -sfn "$PWD/corpus" ~/.claude/skills/gauntlet

Wire a repo, from its top directory:

    uvx --from "gauntlet[hcl] @ git+https://github.com/unxmaal/gauntlet@v0.3.8" gauntlet init
    uv sync && uv run pytest tests/test_gauntlet.py

`corpus/INSTALL.md` is the same procedure as a step-by-step runbook, including how to move a wired repo to a new
tag. To look at the package without wiring anything:

    pip install "git+https://github.com/unxmaal/gauntlet@v0.3.8"
    gauntlet classes          # the classes this version ships
    gauntlet corpus-dir       # where its copy of the corpus lives

The installed package carries its own copy of the corpus, so a consumer's tests read the pinned version and not
whatever is in the local skill directory. `GAUNTLET_SKILL_DIR` overrides it.

### What `gauntlet init` writes

    uvx --from "gauntlet[hcl] @ git+https://github.com/unxmaal/gauntlet@v0.3.8" gauntlet init [--root DIR] [--pin vX.Y.Z] [--force]

In a Python repo it adds the pin to `[dependency-groups] dev` in `pyproject.toml`, never to an extra (with
`python_version >= '3.12'` when `requires-python` admits older Pythons), and writes `tests/gauntlet/registry.py`,
`tests/test_gauntlet.py`, the skill snapshot, an empty defects snapshot and `tests/gauntlet/backlog.txt`, today's
hits as one sorted `<class id> <site>` row per line, so the first run passes and only a new hit fails. There is
no pin: the ratchet compares the backlog with its copy at `git merge-base HEAD origin/main` as a set, so two
branches that each delete different lines merge cleanly. A base whose skill snapshot is missing or different
(the PR that wires or retargets the gauntlet) seeds the backlog instead. CI must check out with
`fetch-depth: 0` (actions/checkout) so the base is there. To migrate from `backlog.json` and `BACKLOG_PIN`,
write each row as a line of `backlog.txt`, `git rm` the JSON, and drop `backlog_pin` from the registry; a base
that still holds `backlog.json` is read in its place. The index holds every corpus class a detector covers; `hcl` joins the front
ends when the repo has `.tf` files. Anything init can't work out for itself (production paths, `detector_config`
values, move tables) becomes a `# TODO(gauntlet):` line in the registry. It refuses to overwrite a registry without `--force`, writes nothing
unless every file was computed, prints how many ratchet tests to expect, and warns where `requires-python` or a CI
workflow runs Python below 3.12. A Terraform repo with no `pyproject.toml` gets the minimal form instead:
`.gauntlet/baseline.json` and `.gauntlet/check.sh`, which runs `gauntlet scan --frontend hcl --baseline
.gauntlet/baseline.json` and fails on a finding not in the baseline.

## Reference

### Wiring a registry by hand

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
    backlog_file=HERE / "backlog.txt",
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

`moves` accepts `{state: next_states}`, `{(leaves, enters): reasons_or_writers}`, a set of `(leaves, enters)`
pairs or `{action: (from_states, to_state)}`; `moves.merge(...)` unions per-writer tables. The ratchet then fails on an unreachable state, a non-terminal
dead end, a terminal that moves by anything but a named reopen, and a declared or used state the table never names.
It also fails on any module constant named `MOVES`, `TRANSITIONS` or `*_MOVES` holding such pairs that has no
`MoveTable` and no reason in `Registry(move_waivers={site: why})`. Without a Registry, `moves.assert_sound(tables)`
is the same check as a plain assertion.

A detector that needs repo knowledge takes it from `Registry(detector_config={name: kwargs})`.
`commit_then_link` finds committing helpers by their `conn.commit()`, and counts a call only when it resolves to
one (same module, an imported name or a module attribute). A function that returns what a committing helper gave it
(`return finish(conn, rid)`) is a committer too, up to two calls deep, when that helper writes SQL; a helper that
commits after reads alone does not make its callers committers, and a bare configured name is not followed this way. A name a package `__init__` re-exports (`from .x import
name`, `from pkg.x import name`, with or without `as`) is followed to its definition, up to four hops, and a re-export
cycle resolves to nothing. A call through a receiver no import names (a parameter or
a local object) counts when every definition of that method name commits, and not when the name is ambiguous; name others with
`{"commit_then_link": {"committers": ("put",)}}`, which match by name. A qualified entry, `"pkg.store:put"` or
`"pkg/store.py:put"`, matches only calls that resolve to that definition (re-exports followed), and one that names no
definition raises `ValueError`. The second write may happen inside a helper: passing the committed value to a
production function counts when that function, or a function it calls up to two calls deep, runs `INSERT`, `UPDATE` or
`REPLACE` SQL without a dedup clause (a literal, or a string the function assigns to the name it executes), or makes a
committing call. A helper is only followed through a call that
resolves, not through a receiver no import names. `unguarded_transition` checks the `state` and
`status` columns and attributes; widen that with `{"unguarded_transition": {"columns": ("state", "status",
"outcome")}}`. A `WHERE` built from an f-string substitution is not reported, since its guard cannot be read. Name
the declared writers of a state attribute with `{"unguarded_transition": {"movers": ("move", "Job.advance")}}` (a
short name or a qualified name); a function that reads a `MOVES` or `TRANSITIONS` constant is a mover already.
`inferred_stage` takes `markers` (a name starting `_` is a suffix, any other an exact name; a flag also matches
with an `is_` prefix), `ignore` (more names that are not stages, added to `created_at`, `updated_at`, `modified_at`
and `inserted_at`) and `declared` (the fields whose presence means the stage is declared, default `state` and
`status`): `{"inferred_stage": {"markers": ("_at", "done", "shipped"), "ignore": ("seen_at",)}}`. `terminal_without_reader` gets `Registry.moves` unless
`detector_config` gives it `tables`, and otherwise reads the declared `MOVES` literals. `unscoped_query` finds nothing
until it is told the scope: `{"unscoped_query": {"columns": ("machine_id",), "tables": ("jobs",), "helpers": ("_here",),
"null_helper": "app/scope.py:here"}}`; a table whose CREATE TABLE names a scope column is scoped without being listed,
and uppercase SQL keywords are what mark a string as a query. `stored_pid_liveness` finds a helper that only does
`os.kill(pid, 0)`; name others with `{"stored_pid_liveness": {"alive": ("running",)}}`. `uncached_key` pairs a
JSON file's writer and reader across modules by a shared path constant or expression, and reads a two-argument
`replace` or `rename` as a move when called by name or through an imported module; name a wrapper with another name
with `{"uncached_key": {"writers": ("pkg.atomic_file:swap",)}}` (`orphanable_child` takes the same `writers`). `prose_match` needs the
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

### Findings, front ends and other tools' output

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

## Developing gauntlet itself

    uv sync --all-extras
    uv run pytest

The repo's own tests cover each detector with a fixture it must fire on and one it must not, the ratchets, `init`
against throwaway repos, the HCL front end and adapters, the mutation runner, and a privacy check that no tracked
file names a local user, host, home path or IP address. CI runs them on Python 3.12.

## License

MIT. See `LICENSE`.
