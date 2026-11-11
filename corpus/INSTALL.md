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

## Steps

1. Make a branch.

       git switch -c gauntlet-init

   Expect: `Switched to a new branch 'gauntlet-init'`.

2. Wire the repo.

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

   The numbers differ per repo. For a Terraform repo with no `pyproject.toml`, expect
   `wrote .gauntlet/baseline.json`, `wrote .gauntlet/check.sh` and a `note:` line; then skip to step 7.

   If it prints `refuses to overwrite`, the repo is already wired. **Ask** the user before running it
   again with `--force`, because that replaces their registry.

   If it prints `uvx: command not found`, **Ask** the user how they install Python tools.

3. Install the pinned version.

       uv sync

   Expect: no error. If the repo has no `uv.lock` or does not use uv, **Ask** the user how its dev
   dependencies are installed.

4. Run the new ratchet tests.

       uv run pytest -q tests/test_gauntlet.py

   Expect: `10 passed`. Any failure: stop and show the user the output.

5. Read the backlog count.

       grep -n "BACKLOG_PIN =" tests/gauntlet/registry.py

   Expect: one line, `BACKLOG_PIN = N`. Tell the user N: it is how many existing hits the repo carries.
   It may only go down from here.

6. List the questions init could not answer.

       grep -n "TODO(gauntlet)" tests/gauntlet/registry.py

   Expect: one line per question. For each line, **Ask** the user the question it states. Change the
   registry only for questions the user answers, and only as the user says. Leave the other lines as they
   are. Do not invent paths, columns or names. After any change, run step 4 again; it must still pass.

7. Run the repo's whole test suite.

       uv run pytest -q

   Expect: the same result the repo had before step 1. A new failure: stop and show the user.
   For a Terraform-only repo, run `sh .gauntlet/check.sh` instead and expect exit status 0, then **Ask**
   the user which CI workflow should run that script.

8. Commit the files init wrote, by name.

       git add pyproject.toml uv.lock tests/gauntlet/skill_classes.json tests/gauntlet/defects.json tests/gauntlet/backlog.json tests/gauntlet/registry.py tests/test_gauntlet.py

   For a Terraform-only repo use `git add .gauntlet/baseline.json .gauntlet/check.sh` instead.
   Then:

       git commit -m "Wire the gauntlet at vX.Y.Z"

   Expect: one commit, listing only those files. Never use `git add -A`.

9. Push and open a pull request.

       git push -u origin gauntlet-init

   Then:

       gh pr create --fill

   Expect: a pull request URL. Give it to the user. Do not merge it.
