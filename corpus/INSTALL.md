# Installing the gauntlet in a repo

A runbook. Do the steps in order, one command each. After each command, compare what it printed with
"Expect". If it differs, stop and show the user the output. Do not guess a fix.

Replace `vX.Y.Z` with the gauntlet tag the user names. If the user named none, stop and ask which tag.

Stop and ask the user, and wait for the answer, whenever this runbook says **Ask**.

## Before you start

Run every command from the repo's top directory. The repo must be a git checkout with no uncommitted
changes. Check with:

    git status --short

Expect: no output. If there is output, **Ask** the user whether to continue.

## If a step fails

Stop. `gauntlet init` writes nothing unless it finishes, so a failed step 3 leaves the repo as it was. If a
later step fails, undo what init wrote:

    git restore pyproject.toml uv.lock && rm -rf tests/gauntlet tests/test_gauntlet.py

Expect: `git status --short` prints nothing again. If `uv.lock` was not tracked before, use `rm uv.lock`
instead of restoring it. Then show the user the failed output and stop.

## Steps

0. Find the repo's own test command and record its result.

       grep -nE "pytest|test:" Makefile .github/workflows/*.yml pyproject.toml 2>/dev/null

   Expect: the command the repo's CI or Makefile runs (`make test`, `uv run pytest -q`, with any env vars it
   sets, such as a database port). If there is more than one, or none, **Ask** the user which one is the
   repo's test command. Run it now and write down the result line (for example `412 passed, 3 skipped`).
   That is the baseline. Steps 5 and 9 use this same command, called TEST below.

1. Open the issue, if the repo works issue first.

       gh issue create --title "Wire the gauntlet at vX.Y.Z" --body "Add the gauntlet ratchets."

   Expect: an issue URL; note its number N. **Ask** the user first whether the repo wants an issue; if
   not, skip this step.

2. Make a branch.

       git switch -c gauntlet-init

   Expect: `Switched to a new branch 'gauntlet-init'`. If the checkout is in use by someone else, use a
   worktree instead, `git worktree add ../gauntlet-init -b gauntlet-init`, and run every later step there.

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
   line means the repo runs Python below 3.12 somewhere (requires-python or CI), where the ratchets skip: tell
   the user each warning and **Ask** whether to add a 3.12 job. For a Terraform repo with no
   `pyproject.toml`, expect `wrote .gauntlet/baseline.json`, `wrote .gauntlet/check.sh` and a `note:` line;
   then skip to step 9.

   If it prints `refuses to overwrite`, the repo is already wired. **Ask** the user before running it
   again with `--force`, because that replaces their registry.

   If it prints `uvx: command not found`, **Ask** the user how they install Python tools.

4. Install the pinned version with the repo's own dev dependencies.

       uv sync

   init put the pin in `[dependency-groups] dev`, which plain `uv sync` installs. If the repo keeps its
   test tools in an extra (`[project.optional-dependencies] dev`), use `uv sync --extra dev`; if CI or the
   Makefile names other groups or extras, use the same flags. If it is unclear, or the repo does not use uv,
   **Ask** the user. Expect: no error.

5. Run the new ratchet tests with TEST, limited to the new file (for pytest, append
   `tests/test_gauntlet.py`).

       uv run pytest -q tests/test_gauntlet.py

   Expect: `N passed`, where N is the ratchet count init printed in step 3. `N skipped` means this Python
   is below 3.12: stop and show the user. Any failure: stop and show the user the output.

6. Lint the files init wrote with the repo's own linters, if it has any (look in the Makefile, CI or
   pre-commit config).

       uv run ruff check tests/gauntlet/registry.py tests/test_gauntlet.py

   Expect: no finding. Run the repo's formatter the same way (for example `uv run ruff format` on the same
   two files) if CI checks formatting. If the repo uses another linter, run that instead. A finding the
   formatter cannot fix: show the user.

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

9. Run the repo's whole test suite with TEST.

       make test

   (or whatever TEST is.) Expect: the baseline from step 0 plus the ratchet tests. A new failure: stop and
   show the user. For a Terraform-only repo, run `sh .gauntlet/check.sh` instead and expect exit status 0,
   then **Ask** the user which CI workflow should run that script.

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
