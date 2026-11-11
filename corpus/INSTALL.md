# Installing the gauntlet in a repo

A runbook. Do the steps in order, one command each. After each command, compare what it printed with
"Expect". If it differs, stop and show the user the output. Do not guess a fix.

Replace `vX.Y.Z` with the gauntlet tag the user names. If the user named none, stop and ask which tag.

Stop and ask the user, and wait for the answer, whenever this runbook says **Ask**.

If the repo is already wired (it has `tests/gauntlet/registry.py`) and the user wants a new tag, follow
"Moving a wired repo to a new tag" at the end instead.

## Before you start

Run every command from the repo's top directory. The repo must be a git checkout with no uncommitted
changes. Check with:

    git status --short

Expect: no output. If there is output, **Ask** the user whether to continue.

## If a step fails

Stop. `gauntlet init` writes nothing unless it finishes, so a failed step 3 leaves the repo as it was. If a
later step fails, undo what init wrote. First check whether the wiring is already committed on this branch:

    git ls-files tests/gauntlet/registry.py

If that prints the path (a retarget, or a branch wired before), restore the committed files. Never delete
them:

    git restore pyproject.toml uv.lock tests/gauntlet tests/test_gauntlet.py

If it prints nothing (a first install), remove what init added:

    git restore pyproject.toml uv.lock && rm -rf tests/gauntlet tests/test_gauntlet.py

Expect: `git status --short` prints nothing again. If `uv.lock` was not tracked before, use `rm uv.lock`
instead of restoring it. Then show the user the failed output and stop.

## Steps

0. Find the repo's own test command and record its result. First read every test step in CI, whole:

       grep -nE -A8 "pytest|make test" .github/workflows/*.yml

   Then read the Makefile's test target and its `export` lines, which set env vars the tests need:

       grep -nE -A8 "^export|^test" Makefile

   Expect: the full command, including lines that continue it (`--ignore=...`, `-m "not slow"`, `\`
   continuations) and the env vars it needs, for example `DB_PORT=5499 uv run --extra dev pytest -q
   --ignore=tests/live`. If CI and the Makefile disagree, or there is none, **Ask** the user which one is
   the repo's test command. Run it now and write down the result line (for example `412 passed, 3
   skipped`). That is the baseline. Steps 5 and 9 use this same command, called TEST below.

1. Open the issue, if the repo works issue first.

       gh issue create --title "Wire the gauntlet at vX.Y.Z" --body "Add the gauntlet ratchets."

   Expect: an issue URL; note its number N. **Ask** the user first whether the repo wants an issue; if
   not, skip this step.

2. Make a branch.

       git switch -c gauntlet-init

   Expect: `Switched to a new branch 'gauntlet-init'`. If the checkout is in use by someone else, use a
   worktree instead, inside the repo's git-ignored `tmp/`. Check that `tmp` is ignored:

       git check-ignore tmp

   Expect: `tmp`. If it prints nothing, **Ask** the user where the worktree should go. Then run
   `git worktree add tmp/gauntlet-init -b gauntlet-init` and run every later step there. Do not put it in
   a sibling directory. Make or compose targets named after the directory can collide with services the
   main checkout already runs (same ports, same container names): do not start a second copy from the
   worktree. Reuse the running ones, and **Ask** the user if TEST needs services that are not running.

3. Wire the repo.

       uvx --from "gauntlet[hcl] @ git+https://github.com/unxmaal/gauntlet@vX.Y.Z" gauntlet init

   Expect, for a Python repo, lines like these:

       wrote pyproject.toml
       wrote tests/gauntlet/skill_classes.json
       wrote tests/gauntlet/defects.json
       wrote tests/gauntlet/backlog.json
       wrote tests/gauntlet/registry.py
       wrote tests/test_gauntlet.py
       backlog: 12 hit(s) seeded
       TODO lines: 11 in tests/gauntlet/registry.py: ask the user before filling any
       ratchet tests: 10 in tests/test_gauntlet.py: expect `10 passed`

   The numbers differ per repo. Write down the ratchet count init printed: step 5 expects it. Any `warning:`
   line means the repo runs Python below 3.12 somewhere (requires-python or CI), where the ratchets skip:
   tell the user each warning. A CI Python bump does not go in this PR: if the user wants one, it is a
   separate PR the user approves. For a Terraform repo with no `pyproject.toml`, expect
   `wrote .gauntlet/baseline.json`, `wrote .gauntlet/check.sh` and a `note:` line; then skip to step 9.

   If it prints `refuses to overwrite`, the repo is already wired. Stop and go to "Moving a wired repo to
   a new tag".

   If it prints `uvx: command not found`, **Ask** the user how they install Python tools.

4. Install the pinned version with the repo's own dev dependencies. First read where the repo keeps its
   test tools:

       grep -nE -A4 "^\[dependency-groups\]|^\[project.optional-dependencies\]" pyproject.toml

   Expect one of these, and run the matching command:

   - `pytest` is under `[dependency-groups]` `dev`: run `uv sync`.
   - `pytest` is under `[project.optional-dependencies]` `dev`: run `uv sync --extra dev`.
   - `pytest` is under another group or extra: run `uv sync --group NAME` or `uv sync --extra NAME`.

   If TEST or CI already passes flags to `uv sync` or `uv run`, use those same flags. If it is unclear, or
   the repo does not use uv, **Ask** the user. Expect: no error.

5. Run the new ratchet tests with TEST's own command, env vars and flags, with `tests/test_gauntlet.py` as
   the only path. For example, if TEST is `DB_PORT=5499 uv run --extra dev pytest -q --ignore=tests/live`,
   run:

       DB_PORT=5499 uv run --extra dev pytest -q tests/test_gauntlet.py

   If TEST is a make target, take the command and `export` lines from that target's recipe. Expect:
   `N passed`, where N is the ratchet count init printed in step 3. `1 skipped` means this Python is below
   3.12: stop and show the user. Any failure: stop and show the user the output.

6. Lint the files init wrote with the repo's own linter and formatter. First check that it has any:

       grep -nE "ruff|black|flake8|pylint" pyproject.toml Makefile .pre-commit-config.yaml .github/workflows/*.yml

   Expect: the linter and formatter the repo runs. If it prints nothing, the repo has none: skip this step.
   Otherwise run them on the two files the way CI does, for example:

       uv run ruff check tests/gauntlet/registry.py tests/test_gauntlet.py

   and `uv run ruff format --check` on the same two files. Expect: no finding. A finding: show the user.

7. Read the backlog count.

       grep -n "BACKLOG_PIN =" tests/gauntlet/registry.py

   Expect: one line, `BACKLOG_PIN = N`. Tell the user N: it is how many existing hits the repo carries.
   It may only go down from here.

8. List the questions init could not answer.

       grep -n "TODO(gauntlet)" tests/gauntlet/registry.py

   Expect: one line per question. For each line, **Ask** the user the question it states. Change the
   registry only for questions the user answers, and only as the user says. Leave the other lines as they
   are. Do not invent paths, columns or names. Tell the user that `tests/gauntlet/defects.json` stays empty
   until `repo_slug` is set and `gauntlet snapshot-defects` is run. After any change, run step 5 again; it
   must still pass.

9. Run the repo's whole test suite with TEST, exactly as in step 0.

       make test

   (or whatever TEST is.) Expect: the baseline from step 0 plus the ratchet tests (`N` more passed). If
   TEST runs on Python below 3.12, as a CI on 3.11 does, expect the baseline plus `1 skipped`: the ratchet
   file skips as a whole. A new failure: stop and show the user. For a Terraform-only repo, run
   `sh .gauntlet/check.sh` instead and expect exit status 0, then **Ask** the user which CI workflow should
   run that script.

10. Commit the files init wrote, by name.

        git add pyproject.toml uv.lock tests/gauntlet/skill_classes.json tests/gauntlet/defects.json tests/gauntlet/backlog.json tests/gauntlet/registry.py tests/test_gauntlet.py

    For a Terraform-only repo use `git add .gauntlet/baseline.json .gauntlet/check.sh` instead.
    Then:

        git commit -m "Wire the gauntlet at vX.Y.Z"

    Expect: one commit, listing only those files. Never use `git add -A`.

11. Push and open a pull request.

        git push -u origin gauntlet-init

    Then:

        gh pr create --title "Wire the gauntlet at vX.Y.Z" --body "Closes #N"

    Put `Closes #N` on its own line, with N from step 1; with no issue, give a one-line body saying what
    the PR does. Do not use `--fill`. Expect: a pull request URL. Give it to the user. Do not merge it.

## Moving a wired repo to a new tag

Use this when `tests/gauntlet/registry.py` is already committed and the user names a new tag vX.Y.Z.
Do "Before you start", then steps 0 to 2, then these:

1. Rewire at the new tag.

       uvx --from "gauntlet[hcl] @ git+https://github.com/unxmaal/gauntlet@vX.Y.Z" gauntlet init --force

   Expect: the same `wrote` lines as step 3.

2. Check the pin is one line.

       grep -c "unxmaal/gauntlet@" pyproject.toml

   Expect: `1`, and `git diff pyproject.toml` shows only the tag change. Anything else: stop and show the
   user.

3. See what init dropped from the registry. `--force` writes `registry.py` fresh, so every answered TODO
   and every hand edit is gone.

       git diff tests/gauntlet/registry.py

   Expect: removed (`-`) lines for each earlier answer. Put each one back by hand, as it was. Do not
   restore the whole file: the new INDEX and BACKLOG_PIN come from the new tag. If a removed line is
   unclear, **Ask** the user.

4. Go on with steps 4 to 11. If the issue from step 1 names an older tag, retitle it:

       gh issue edit N --title "Wire the gauntlet at vX.Y.Z"

   Expect: the issue URL.
