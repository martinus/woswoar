# Working agreements

Rules for AI agents working in this repository. They apply to every task unless
the maintainer says otherwise in the moment.

## 1. Review before opening a pull request, in proportion to the risk

When the work is complete and the tests pass, review it, **apply** what you
find, and re-run the suite. Only then open the PR — not afterwards, because a
review that lands on an open PR means the maintainer has already read code that
was about to change.

How much review depends on what the change can get wrong:

| The change | Review |
|---|---|
| Forgets, prunes, rewrites or replaces stored data; changes a security claim; caches a fact about the world | Full `/simplify`, all four angles. This is where it pays: the prune in #91 introduced a P1 regression, caught before the PR opened and not by its author. |
| Changes behaviour on a path a user reaches | One agent, aimed at the specific hazard — or a careful read of the diff against the issue's own "constraint that makes the obvious fix wrong". |
| Mechanical, with a measured before and after and no new state | Re-read the diff yourself. No agents. |

Four agents asked "what is wrong with this" will always return something. That
rate is a function of how many you run, not of how bad the code is, and it is
how a backlog fills with polish nobody will ever do. Pick the row honestly: a
change that *looks* mechanical but alters what reaches disk is the top row.

Treat "this is only a quality pass" as an assumption to check. Findings here
have included real defects, twice including a regression introduced by the
change under review.

If a finding is wrong or its fix would exceed the task's scope, say so
explicitly in the PR description rather than silently dropping it.

## 2. Never merge without explicit approval

Open the PR, report its CI status, and stop. Wait for the maintainer to say to
merge.

Approval is per-PR and does not carry over. "Merge it" for one PR is not
standing authority for the next one, even when the next one is a direct
follow-up to the same task. Ask again.

## 3. Every fix and every new behaviour gets a regression test

The bar is not "a test exists". It is **the test fails when the fix is
reverted**. Verify that, do not assume it:

1. Revert the fix in the working tree.
2. Run the suite and confirm the new test fails.
3. Restore the fix and confirm it passes.

`tools/mutate.py` is that loop: write the table of edits, run
`python -m tools.mutate <spec>.py`, paste the output into the PR.

A test that passes either way is decoration, and reading it will not tell you
which kind you have written. Tests have been added here that looked correct and
never executed the line they claimed to guard.

Prefer driving the real thing over asserting on a mock. This repository already
tests the shell hook by running a real `bash`, and sync by running real `age`
and `git`; follow that.

The other failure mode is a fixture too weak to tell the two answers apart, and
it is invisible in the test's own text. All of these were written here, passed
review, and guarded nothing:

- two day directories cannot distinguish a sorted walk from a reversed one
- a directory holding only `.age` files cannot test a suffix filter
- a day written moments ago is skipped by the racy-timestamp rule regardless, so
  a test about *why else* it might be skipped asserts nothing
- a marker asserted in a shell's stdout also appears in the harness's echo of
  the line that was typed

So when a mutation survives, suspect the fixture before you suspect the
mutation.

## 4. File an issue only for what someone would actually do

While working you will notice bugs, performance problems, missing tests, and
improvements that do not belong in the change at hand. There are three answers,
and filing is not the default:

- **Fix it here**, if it is small and in code the diff already touches. Say so
  in the PR body.
- **File it**, if it is P2 or above by rule 5 — or if it is a security item, a
  guard that would catch a future regression, or a symptom that actively
  misleads a user. Those three earn a P3.
- **Say it in the task summary and let it go** otherwise.

A backlog of things nobody will do is worse than no record, because it hides the
two items that matter. One triage pass closed six issues unworked — a
millisecond here, an unreproducible flake, some tidiness — and every one had
been dutifully filed under an earlier reading of this rule. Do not silently fix
what you should have filed; do not file what you would close.

Each issue you do file should carry enough for someone to act on it cold:

- what is wrong, and the concrete consequence
- `file:line` for the relevant code
- a reproduction, with measured numbers where the claim is about performance
- a suggested fix, and any constraint that makes the obvious fix wrong

If a finding turns out to be a non-issue on closer inspection, say so in the
task summary instead of filing it.

## 5. Give every issue a priority label

Label each issue you file with exactly one of these. They sort lexically, so
`gh issue list --state open --json number,title,labels` in label order is a
sensible implementation order.

| Label | Meaning |
|---|---|
| `P0` | Exploitable now, loses data, or breaks a documented guarantee. Do before anything else. |
| `P1` | Real defect with a plausible trigger, or a fix that unblocks other work. Do next. |
| `P2` | Worth fixing; no user is hurt today. |
| `P3` | Cleanup, polish, nice to have. |

Priority is *implementation order*, not just severity. When ranking, weigh:

- **impact** — how bad is it when it happens, and who is exposed
- **reachability** — can a stranger trigger it, or does it need local access
  and bad luck
- **dependency** — does another issue become easier or safer once this lands
- **cost** — a five-line fix that removes a whole class of bug outranks a
  rewrite that removes one instance

State the reasoning in the issue when the ranking is not obvious from the title.
If new information changes the picture, relabel the issue and say why.

## 6. Never discard a change you cannot get back

`git checkout -- <file>`, `git restore`, `git stash` and `git reset --hard` have
each destroyed uncommitted work here. There is no reflog for a tree that was
never committed, so the only recovery is to write it again from memory — the
single largest waste of a session so far, twice over.

- Commit a checkpoint before anything that rewrites files in bulk. Mutation
  testing especially: it edits sources in a loop and restores them in a
  `finally`, and an interrupted run leaves the tree mutated.
- To undo your own edit, rewrite the text you changed. Do not discard the file.
- Tell subagents explicitly when they may not write. A review agent asked only
  to *read* a diff has reverted the tree on its own initiative; verify the tree
  yourself before believing a report that mentions touching it.

## 7. Branch before the first commit

Not after. Undoing a commit that landed on `main` means branching at `HEAD` and
then `git reset --hard origin/main` — the operation rule 6 is about.

## 8. Write no migration code: nothing is deployed yet

woswoar has no users. Nobody has installed it, so there is no history in the
field and no repository built by an older version.

Change the record format, the repo layout, `state.json`, the cache, the day-key
scheme — outright. Do **not** write an upgrade path, a version field two code
paths branch on, an "if this is the old shape" fallback, or a deprecation
period. A clean cut is the norm here, not something to argue for.

Two things this does not license:

- **Say so in the PR body, with the rebuild command**, in case the maintainer's
  own machine is on the old shape:
  `rm -rf ~/.local/share/woswoar/history ~/.local/share/woswoar/state.json`,
  then `woswoar init <url>` and `woswoar grant`.
- `logs/` is the plaintext history and the primary copy; `history/` is derived
  from it and can always be rebuilt. Discarding the derived tree is cheap;
  discarding `logs/` loses data. That distinction outlives this rule.

**This expires the moment woswoar is published and someone installs it.** Delete
the rule then, rather than reasoning about who might be affected. Both errors
cost: assuming a user base that does not exist wastes work — issue #54 was filed
at P1 and closed for exactly that — and assuming forever that there is none
corrupts somebody's history.

## Repository conventions worth matching

- Comments explain **why**, especially why an obvious alternative was rejected.
  Match that density; it is deliberate.
- The shell hook must not fork on the per-command path, and CI asserts it.
- Claims in `docs/security.md` are backed by tests, not prose. If you change
  what the code guarantees, change the claim and the test together.
- Preflight before pushing — the same checks CI runs, in one line:

  ```sh
  ruff check . && ruff format --check . && mypy woswoar tests tools \
    && shellcheck --shell=bash woswoar/shell/woswoar.bash \
    && python -m tools.run_tests
  ```

  `python -m unittest discover -s . -t . -p 'test_*.py'` runs the same tests
  serially, and takes about three times as long.

- Mutation-test through `tools/mutate.py` rather than writing the loop again. It
  runs `-B`, clears every `__pycache__` between mutations, refuses an edit that
  matches other than exactly once, checks the baseline is green, and restores
  the tree even when interrupted. The trap it exists for: a `.pyc` is validated
  against `(mtime_seconds, size)`, so two mutations that change a file by the
  same number of bytes inside one second run each other's cached bytecode —
  which reported a *correct* test as decoration here, and nearly got it
  rewritten.
- Before blaming your branch for a CI failure, measure the same job on `main`,
  enough times to see a one-in-forty flake. Two runs of green proves nothing.
- **Commit before comparing two revisions.** `git checkout main` carries
  uncommitted changes across, so a before-and-after benchmark run that way
  measures the same tree twice and reports no difference. That has twice nearly
  buried a correct change here as "no measurable effect".
- An optimisation sequence converges. When the term you are about to remove is a
  small share of what is left — a millisecond of fifteen — stop and say so,
  rather than filing the next one.
