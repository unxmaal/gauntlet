# Shape questions

A shape question gathers one cross-file structure into a single list, so the
gaps show: every lifecycle and its writers, every query and its scope. The
classes in SKILL.md answer "is this site wrong?"; a shape question answers
"where are all the sites, and which combinations does nothing handle?". Run one
on a whole codebase as an inventory, not on a diff.

This is a candidate pool, like `general-corpus.md`. A question whose inventory
turns up a real instance graduates into SKILL.md as a class, with its trigger,
test shape and, where possible, a detector. "A lifecycle with no declared
state" graduated this way on 2026-11-11, then CR, OW and WT, then SC, ID and
CA.

## How to run one

1. Pin the commit. Read it detached; change nothing and write nothing live.
2. Search the repo's knowledge base for the question's terms first, and attach
   the ids of past defects to each finding.
3. Enumerate, do not sample: list every site that matches the question's
   "list" line, then judge each.
4. Write the inventory in the standard format below.
5. Every high finding becomes an issue whose acceptance is a red-proofed test
   that produces the failure. The inventory's severity is a judgement until
   that test exists.
6. Name the scanner, if one is possible, and what it cannot see.

## Standard inventory format

One markdown file per question per commit:
`<repo>/tmp/gauntlet/<question-id>-<shortsha>.md`.

Header: repo, commit, date, question id, scope (directories read), and how
"enumerate" was done (grep patterns, AST walk, by hand).

Then one table, ranked high, medium, low:

| Column | Content |
|---|---|
| Sev | high: has bitten, or can corrupt, strand or double-run work. medium: wrong answer or stale view. low: cosmetic or display only |
| Id | question id + number, e.g. SM-C1, SC-3 |
| Entity | the thing, not the file |
| Sites | file:line of every read and write, with the count of writer sites |
| Shape | how it is represented today |
| Unhandled | the combinations or moments no code handles |
| Bitten | issue and KB ids of past defects, or "none" |
| Scannable | the detector that would catch it, or "tier 2: why not" |

Close with "properly done" entries in one line each, so the list is complete,
and the scanner's blind spots.

## The questions

Ids are stable; never renumber.

**SM. Lifecycles.** Does an entity move through stages? List every entity with
a lifecycle and every writer of its stage. Graduated: SKILL.md "A lifecycle
with no declared state".

**VS. Version skew.** Which version of this code is running, and can two run at
once? List every place one release's code drives, checks or reads data written
by another: deploy drivers and the release they install, nodes on different
releases, branches writing a store whose schema is behind them.

**FT. Free-text contracts.** Is a decision carried in prose? List every read
that matches a prefix, substring or regex in a note, detail, reason or log
line to decide what to do.

**SC. Scope.** Is every query scoped to this machine, home, tenant and lane?
List every read of a shared table or directory and the scope columns it
filters on. Graduated: SKILL.md "A shared-store query with no scope, or a
scope with its own idea of NULL".

**ID. Identity.** What identifies this thing, and can that identifier be
reused, recycled or blank? List every identifier used as a key: pids, pgids,
ports, paths, call ids, names. Graduated: SKILL.md "A stored pid trusted as
the same process".

**EF. Empty versus failed.** Does "nothing" look the same as "failed"? List
every read whose empty, None or zero result is acted on, and what else
produces that same result (a deleted row, a store that is down, the wrong
store).

**CR. Crash between writes.** List every operation with more than one durable
write (row, file, symlink, process). For each gap between two writes, what is
left if the process dies there, and what repairs it? Graduated: SKILL.md "Two
durable writes with no repair for the gap".

**OW. Ownership.** List every resource created (process, file, lock, lease,
worktree, row). Who owns it, and who cleans it up when the owner dies?
Graduated: SKILL.md "A child that outlives its owner".

**CA. Caches.** Is this a copy of something? List every derived file, table
or in-memory value, its source, and what invalidates it. Graduated: SKILL.md
"A copy whose key leaves out an input, or that nothing invalidates".

**WT. Waits.** List every poll, retry, wait and pending state. Does each have
a deadline, an owner who is told when it expires, and a named end state?
Graduated: SKILL.md "A wait with no deadline, owner or end state".

**SE. Side effects.** List every external effect (alert, webhook, push,
restart, download, message). Can a test, scratch home or branch reach it?

**WR. Writers and readers.** List every stored value. Does each have both a
live writer and a live reader?

**OR. Call order.** List every "A must happen before B" the code relies on
(env sourcing, migration, connect before backend choice). Is the order
enforced, or only usual?
