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

There is nothing to install for woswoar itself — it is standard library only,
and the dependency list is empty on purpose. The tools above are the `dev`
extra: `pip install -e '.[dev]'`. The suite also drives real `age`, real `git`,
a real `bash` and a real `fzf`, and it will tell you which one is missing.

```bash
python -m tools.run_tests                               # 301 tests, sharded, ~6s
python -m unittest discover -s . -t . -p 'test_*.py'    # the same suite, serially
WOSWOAR_BENCH=1 python -m unittest tests.test_perf      # latency on 52k entries
```

The suite is about 88% subprocess wait — it drives real `age`, `git` and
`ssh-keygen` rather than mocking them — so sharding it across processes takes it
from ~19s to ~6s. Both commands run the same tests; the runner additionally
fails if any test it discovered never reported back, which is a way a parallel
run can be green that a serial one cannot.

CI runs lint, tests on Python 3.10/3.12/3.14, the shell-hook and fork-free
checks, a two-machine end-to-end sync against real `age` and real `git`,
immutability and repo-growth assertions, and an install smoke test.

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

## Cutting a release

The version lives in `woswoar/__init__.py` and nowhere else — `pyproject.toml`
reads it from there, so the two cannot disagree.

```bash
# 1. bump __version__, open a PR, merge it (main is protected)
# 2. tag the merged commit:
git tag v0.2.0 && git push origin v0.2.0
```

Everything after that is automatic. `.github/workflows/release.yml` refuses the
tag unless it matches `__version__` and sits on `main`, re-runs the whole suite
at that exact commit, builds the sdist and wheel, attests both, uploads them to
PyPI over Trusted Publishing — no token, an identity minted for that one run —
publishes a GitHub release with generated notes, and fast-forwards `stable`,
which is what the `git+https://…@stable` form tracks. The `stable` push is not
forced, so tagging an older commit fails loudly rather than moving everyone
backwards.

PyPI is the one step that cannot be undone: a version can be yanked but never
reused. It runs before the GitHub release for that reason, and
`.github/workflows/publish-testpypi.yml` is the rehearsal, run by hand against
TestPyPI whenever the packaging changes.

## Security

Do not report a vulnerability in a public issue. [`SECURITY.md`](SECURITY.md) has
the private channel and what is in scope.
