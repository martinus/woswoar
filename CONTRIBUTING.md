# Contributing

Bug reports and patches are welcome. This file is the short version of how the
repository works; [`CLAUDE.md`](CLAUDE.md) is **the same rules written for
agents**, kept as the single source so the two cannot drift into disagreeing.
Where this file is shorter, that one is the detail.

## Before you push

One line, and it is exactly what CI runs:

```sh
ruff check . && ruff format --check . && mypy woswoar tests tools \
  && shellcheck --shell=bash woswoar/shell/woswoar.bash \
  && python -m tools.run_tests
```

`python -m tools.run_tests` is the parallel runner. `python -m unittest discover
-s . -t . -p 'test_*.py'` runs the same tests serially and takes about three
times as long.

There is nothing to install for woswoar itself — it is standard library only,
and the dependency list is empty on purpose. The tools above are the `dev`
extra: `pip install -e '.[dev]'`. The suite also drives real `age`, real `git`,
a real `bash` and a real `fzf`, and it will tell you which one is missing.

## Every fix needs a test that fails when the fix is reverted

Not "a test exists". The bar is that the test **fails** with the fix taken out,
and the only way to know is to try it:

1. Revert the fix in the working tree.
2. Run the suite and confirm the new test fails.
3. Restore the fix and confirm it passes.

[`tools/mutate.py`](tools/mutate.py) is that loop, written down. Give it a table
of edits, run `python -m tools.mutate <spec>.py`, and paste its output into the
pull request. Use it rather than writing the loop by hand: it clears every
`__pycache__` between mutations, because a `.pyc` is validated against
`(mtime, size)` and two edits of the same size inside one second will run each
other's cached bytecode — which reported a *correct* test as decoration here and
nearly got it deleted.

### When a mutation survives, suspect the fixture first

A test that passes either way is decoration, and reading it will not tell you
which kind you have written. Every one of these was written in this repository,
passed review, and guarded nothing:

- two day directories cannot distinguish a sorted walk from a reversed one;
- a directory holding only `.age` files cannot test a suffix filter;
- a day written moments ago is skipped by the racy-timestamp rule regardless, so
  a test about *why else* it might be skipped asserts nothing;
- a marker asserted in a shell's stdout also appears in the harness's echo of the
  line that was typed.

Prefer driving the real thing over asserting on a mock. The suite already runs a
real `bash` for the shell hook and real `age` and `git` for sync; follow that.

## Comments explain *why*

The comment density here is deliberate and is most of the value of the codebase.
The useful comment is the one that says **why an obvious alternative was
rejected** — not what the line does. A patch that strips those as noise will be
asked to put them back; a patch that adds one where a reader would otherwise
reach for the wrong fix is doing the main thing.

## Pull requests

- Branch before the first commit. `main` is protected.
- Say what you measured, if the change is about performance. "Faster" is not a
  number, and a benchmark run by checking out another revision on top of
  uncommitted changes measures the same tree twice — commit first.
- If you found something wrong that does not belong in your change, say so in the
  PR description rather than fixing it quietly or filing an issue nobody will
  do. The bar for filing is in [`CLAUDE.md`](CLAUDE.md) rule 4.
- Every issue carries exactly one priority label, `P0`–`P3`, and they sort
  lexically so the open list reads as an implementation order.

Nothing is merged without the maintainer saying so, including by the maintainer's
own agents.

## Security

Do not report a vulnerability in a public issue. [`SECURITY.md`](SECURITY.md) has
the private channel and what is in scope.
