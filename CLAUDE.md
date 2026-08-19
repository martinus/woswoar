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

`python -m tools.mutate --base main` skips the table: it reads the diff,
generates mutants for the lines the change touched, and runs them. Use it first
and write rows by hand only for what it cannot reach — a *scope widening* (the
`source` line searched in the whole rc file rather than the block) has no local
AST rewrite, so "every generated mutant was caught" is not "the tests are good".

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

**One exception, and it follows from that sentence.** An issue that only
*contains* other issues carries `tracking` and no priority at all. Priority is
implementation order, and a container has none — its children have it, and they
are what someone actually picks up. A tracking issue given a `P2` sits in the
ordered list above as a peer of the four issues inside it, so working that list
top-down reaches a card with nothing to do on it. Keep the argument, the order
and the definition of done in the tracking issue; keep the priority on the work.

## 6. Never discard a change you cannot get back

`git checkout -- <file>`, `git restore`, `git stash` and `git reset --hard` have
each destroyed uncommitted work here. There is no reflog for a tree that was
never committed, so the only recovery is to write it again from memory — the
single largest waste of a session so far, twice over.

- Commit a checkpoint before anything that rewrites files in bulk. Not
  `tools/mutate.py`, which since #212 edits only a throwaway copy — this bullet
  said otherwise for months after that stopped being true, and a stale warning
  here is worse than none: believing the tool leaves a mutated tree behind is
  exactly what would justify reaching for `git checkout --` to tidy up, which is
  the operation this rule exists to prevent.
- To undo your own edit, rewrite the text you changed. Do not discard the file.
- Tell subagents explicitly when they may not write. A review agent asked only
  to *read* a diff has reverted the tree on its own initiative; verify the tree
  yourself before believing a report that mentions touching it.

## 7. Branch before the first commit

Not after. Undoing a commit that landed on `main` means branching at `HEAD` and
then `git reset --hard origin/main` — the operation rule 6 is about.

## 8. There is a user now: changing a format means moving what exists

woswoar is installed and recording. Until v0.2.0 this rule said the opposite —
change any format outright, write no upgrade path — because nothing was
deployed. It said to delete itself the moment someone installed, rather than
reason about who might be affected. This is that deletion.

So: a change to the record format, the repo layout, `state.json`, the cache or
the day-key scheme now needs a way for an existing installation to arrive at the
new shape. Not necessarily *code* — a one-line note in the release saying
"delete the cache, it rebuilds" is an upgrade path, and a good one where the
thing is derived. What is no longer acceptable is a change that leaves a running
machine silently wrong.

The distinction that decides how much care a change needs:

- `logs/` is the plaintext history and the **primary copy**. Losing it loses
  data. Nothing may require it to be discarded.
- `history/` is the encrypted git tree, derived from `logs/` and rebuildable.
  `cache.txt` likewise. Telling someone to delete either is cheap.
- `state.json` is progress, not history: losing it costs a re-merge and a
  re-export, never a command. Fields there may be added freely, and `State.load`
  already degrades an unreadable value to a safe default — that is the pattern
  to follow rather than a version field two code paths branch on.

Nothing in the repo records which version wrote it, so a change to the *repo*
format has no marker to hang an upgrade on. That is worth fixing before the
first such change, not during one.

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

- Four tools in `tools/` exist because the same mistake was made more than
  once. Each one's module docstring carries the full argument for why it is
  shaped as it is; reach for them rather than writing the loop again.

  | when | tool |
  |---|---|
  | a fix needs a test that fails without it (rule 3) | [`tools/mutate.py`](tools/mutate.py) |
  | a mutation run left more survivors than you can read | [`tools/reached.py`](tools/reached.py) |
  | a refactor is supposed to change no output | [`tools/compare.py`](tools/compare.py) |
  | a change might have cost time | [`tools/bench.py`](tools/bench.py) |

  ```sh
  python -m tools.mutate --base main        # generated from the diff
  python -m tools.mutate <spec>.py          # a table you wrote
  python -m tools.reached r.json c.json     # which survivors are missing tests
  python -m tools.compare --base main --show .bashrc install doctor status
  python -m tools.bench --importtime woswoar.__main__ --base main
  ```

  **The generated sweep goes last.** Implement, write the hand table, preflight,
  run `/simplify` *and apply it*, and only then `--base main`. The table is
  generated from the lines as they stand, so any edit after it invalidates every
  row — and a review that lands after the sweep always edits something. Three
  sweeps were re-run for that reason in one session, at ten to thirty-five
  minutes each.

  Three things to carry into a pull request, because they are about what you
  write rather than what the tool does:

  - **Paste mutation output verbatim.** Retyping it is how a mutation that was
    never run ended up quoted in a commit message here.
  - **Quote `compare`'s scrub list beside any "byte-identical" claim.** The claim
    is worth exactly what was left unnormalised, and nothing outside that list
    can tell you.
  - **"Below measurement" is a real answer.** When `bench` says *not resolved*,
    write that, not a figure with two decimal places — and say which statistic
    and how many blocks, because a difference of medians and a median of paired
    differences are not the same number.

- Before blaming your branch for a CI failure, measure the same job on `main`,
  enough times to see a one-in-forty flake. Two runs of green proves nothing.
- Moving a name between modules leaves `.mypy_cache` wrong, and it fails as
  `AssertionError: Cannot find component 'X' for 'woswoar.old_module.X'` from
  inside mypy rather than as a type error. `rm -rf .mypy_cache` and re-run; it is
  not your change.
- **Commit before comparing two revisions.** `git checkout main` carries
  uncommitted changes across, so a before-and-after benchmark run that way
  measures the same tree twice and reports no difference. That has twice nearly
  buried a correct change here as "no measurable effect".
- An optimisation sequence converges. When the term you are about to remove is a
  small share of what is left — a millisecond of fifteen — stop and say so,
  rather than filing the next one.
