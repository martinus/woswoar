# Working agreements

Rules for AI agents working in this repository. They apply to every task unless
the maintainer says otherwise in the moment.

## 1. Run `/simplify` before opening a pull request

When the work is complete and the tests pass, invoke the `simplify` skill,
**apply** what it finds, and re-run the suite. Only then open the PR.

Not afterwards: a review that lands on an open PR means the maintainer has
already read code that was about to change. Its findings routinely include real
defects, not just style — treat "this is only a quality pass" as an assumption
to check, never as a reason to skip it.

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

A test that passes either way is decoration, and reading it will not tell you
which kind you have written. Tests have been added here that looked correct and
never executed the line they claimed to guard.

Prefer driving the real thing over asserting on a mock. This repository already
tests the shell hook by running a real `bash`, and sync by running real `age`
and `git`; follow that.

## 4. File an issue for anything you find outside the current scope

While working you will notice bugs, performance problems, missing tests, or
worthwhile improvements that do not belong in the change at hand. Do not silently
fix them, and do not drop them on the floor. File a GitHub issue.

Each issue should carry enough for someone to act on it cold:

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

## Repository conventions worth matching

- Comments explain **why**, especially why an obvious alternative was rejected.
  Match that density; it is deliberate.
- The shell hook must not fork on the per-command path, and CI asserts it.
- Claims in `docs/security.md` are backed by tests, not prose. If you change
  what the code guarantees, change the claim and the test together.
- Preflight before pushing — the same checks CI runs, in one line:

  ```sh
  ruff check . && ruff format --check . && mypy woswoar tests \
    && shellcheck --shell=bash woswoar/shell/woswoar.bash \
    && python -m unittest discover -s . -t . -p 'test_*.py'
  ```

- Run mutation tests with `python -B` **and** delete `woswoar/__pycache__`
  between mutations. A `.pyc` is validated against `(mtime_seconds, size)`, so
  two mutations that change a file by the same number of bytes inside one second
  run each other's cached bytecode — which reports a test as surviving when it
  does not, and has sent a correct test to be rewritten as "decoration".
- Before blaming your branch for a CI failure, measure the same job on `main`,
  enough times to see a one-in-forty flake. Two runs of green proves nothing.
