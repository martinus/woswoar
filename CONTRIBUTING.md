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
python -m tools.run_tests                               # 993 tests, sharded, ~5s
python -m unittest discover -s . -t . -p 'test_*.py'    # the same suite, serially
WOSWOAR_BENCH=1 python -m unittest tests.test_perf      # latency on 52k entries
```

The suite is about 88% subprocess wait — it drives real `age`, `git` and
`ssh-keygen` rather than mocking them — so sharding it across processes takes it
from ~60s to ~5s. Both commands run the same tests; the runner additionally
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
pull request — verbatim, rather than retyping it, which is how a mutation that
was never run ended up quoted in a commit message here.

Most of the time you should not write the table at all:

```sh
python -m tools.mutate --base main          # generate from the diff and run
python -m tools.mutate --base main --list   # print the table, run nothing
python -m tools.mutate --all --json out.json  # every line of every mutable file
```

`--all` is the whole package rather than a diff — several thousand mutants and
hours — so it batches by file and writes `--json` as each one lands: a crash
costs one file, and re-running with the same `--json` skips what is already
recorded. There is no `--resume` flag; the report *is* the resume state.
`--batch` asks for the same behaviour on a large `--base` diff. `--all` also
drops the default `--limit`, which is sized for a diff and would otherwise run
a twentieth of the table.

Confirmation is pooled to the end rather than run per batch, because per batch
a file with one survivor pays a whole suite run with fifteen lanes idle.

It reads `git diff --merge-base main` — working tree included, so uncommitted
work counts — and generates mutants for the changed lines of `woswoar/**.py` and
`tools/**.py` only. Each row runs against the test modules that name or import
the file, and **every survivor is then re-run against the whole suite** before it
is reported, so a narrow selection can cost time but cannot produce a false
`SURVIVED`. On the change that became #216 that pass corrected nine of eleven.

That pass takes its own baseline, and needs it: correcting a survivor is the
claim *a test the selection had not run noticed this*, and on a suite that is
already failing the claim is free — the re-run stops at the first red test,
whatever it was about. So if the whole suite is not green, nothing is corrected
and the run says so; the narrow pass's own verdicts still stand. `--no-baseline`
turns that off along with every other untouched-suite check, which is what it is
for, and what it costs.

Either way the `--json` report says which happened: `widened` is true only when
every survivor in the file really was re-run against the whole suite. It is
false after `--no-confirm`, after a suppressed confirmation, and in a
part-written file from an interrupted sweep — the cases where the paragraph
above promises something the file cannot deliver.

Fifteen operators, and `--skip-operator NAME` drops one — the escape hatch for
an equivalent mutant a whole operator keeps producing, where `# pragma: no
mutate` only suppresses a line. Twelve rewrite an expression (`<` to `<=`, `and`
to `or`, `sorted(x)` to `sorted(x, reverse=True)`, `.endswith(...)` to `True`, an
`if` forced both ways, `+` to `-`, a slice's bounds dropped, a small integer
moved by one, `return X` to `return None`); two delete a statement, and one
drops a keyword argument.

Deletion is deliberately narrow. `drop-call` removes a call whose value is
discarded — `path.mkdir()`, `store.flush()` — which is the "the write never
happened" class, and `drop-assign` removes an assignment **through an attribute
or a key** — `self.total = ...`, `cache[k] = ...`. Plain `name = ...` is left
alone on purpose: deleting it leaves a `NameError` further down, which reports
`BROKE` rather than an answer and costs a whole suite run to find that out.
`logging` and `progress` calls are never deleted; `print` is, because here the
printed output is the product.

`drop-kwarg` removes a keyword argument, and only from a named list —
`mode`, `check=True`, `exist_ok`, `follow_symlinks`. Blanket
dropping was measured first and is mostly noise: of 281 keyword arguments in
`woswoar/`, the commonest are *required* parameters passed by name, where
dropping one is a `TypeError` and so `BROKE` rather than an answer. The list holds the ones whose absence silently weakens a guarantee *and that a
test can notice* — it is the only way to generate `mkdir(..., mode=0o700)`
without its mode, which is what `docs/security.md` rests on. `encoding` fails
the second half and was removed: dropping it is a real defect under a non-UTF-8
locale, and unkillable in a suite that never runs under one.

A generated mutant can also fail to *stop*, and there are three ways rather than
one. `--timeout` (300 s) answers a mutation that never finishes. `--memory`
(4 GiB of address space, per process) answers one that never finishes *while
allocating* — not a refinement of the first, because a timeout cannot fire on a
machine that is already out of memory: an `at -= …` generated for this
repository's own `line_starts` reached 15.5 GB in 73 seconds and OOM-killed the
session twice before 300 s was anywhere in sight.

The third is a mutation that spawns *processes*, which neither of the others can
see. `tools/mutate.py` and `tools/run_tests.py` are themselves things a sweep
mutates, and a mutant in either decides how many processes its lane starts. The
sweep that found this had 4,340 alive at once holding 26 GB between them, six
megabytes each and none within two orders of magnitude of its own ceiling, and
it took the machine down three times (#232). So each probe now runs in its own
session, its whole process group is counted once a second against the share
`_share` gave it, and a group over that share is killed whole and reported
`BROKE`. Lanes and the ceiling are chosen together for the same reason: what the
machine feels is their product, and sixteen lanes promised 4 GiB each is 64 GiB
of promises on a 63 GiB box.

A survivor list is not a finding, and reading one unaided is how two confident
wrong triages happened here. Cross it with coverage:

```sh
python -m tools.mutate --base main --json results.json
coverage run --source=woswoar -m unittest discover -s . -t . -p 'test_*.py'
coverage json -o coverage.json
python -m tools.reached results.json coverage.json --list
```

That splits survivors in two, and the halves mean opposite things. A survivor on
a line **no test executes** is a missing test. A survivor on a line the suite
**does** execute is a weak fixture or an equivalent mutant — rule 3's "suspect
the fixture", and much the larger half: 590 of 751 on the run this was built
for. Conflating them is what makes a survivor list read as hundreds of bugs.

Both inputs must come from the same tree; a fix that shifts line numbers
silently unmatches them. `coverage` is not a dependency and is not imported —
`tools/reached.py` only reads the JSON. It also folds *caught* mutants back into
the map, because a caught mutation proves its line ran and in-process coverage
cannot see the lines this suite reaches by running a real `bash`.

Running out of memory is reported `BROKE`, never `caught`. It arrives as a
`MemoryError` inside whichever test was running, which by protocol looks exactly
like that test noticing — and crediting a test with a guard it does not have is
the failure this whole tool exists to prevent.

Write rows by hand for what no operator can reach. Of the four weak fixtures
listed below, `order` generates the reversed walk and `affix` the suffix filter
that accepts everything. The class that is out of reach is *scope widening* — a
`source` line searched in the whole rc file instead of the block, from
[`CLAUDE.md`](CLAUDE.md) rule 3 — because no local rewrite changes which region
a search covers. So "every generated mutant was caught" is not the same claim as
"the tests are good".

Three verdicts, not two. `caught` means a **test method** noticed; `SURVIVED`
means none did; `BROKE` and `TIMEOUT` mean the run never got to ask, and are
counted apart from both. That third category exists because a mutation which
makes a module unimportable also exits non-zero with a plausible `Ran N` — so it
read as `caught` while the test named in the row never executed, which is a false
pass in a pull request and indistinguishable from a real one.

Use it rather than writing the loop by hand, for four reasons it has learned:

- **It never edits your working tree.** Each mutation goes into a throwaway copy,
  so an interrupted run cannot leave mutated source behind.
- **It classifies where the result objects are**, not by reading what `unittest`
  printed. A dead `setUpClass`, an import that stopped working and a real
  assertion failure all exit non-zero; only the last of them is an answer.
- **It runs the table in parallel**, which the copies are what make safe: 197 s
  down to 51 s for four mutations against `tests.test_sync`.
- **It defuses the bytecode cache.** A `.pyc` is validated against
  `(mtime, size)`, so two edits of the same size inside one second will run each
  other's cached bytecode — which reported a *correct* test as decoration here and
  nearly got it deleted. The copies start with no cache and the runs are told not
  to write one.

It also refuses a replacement that still contains the text it replaced, since
that leaves the code under test unchanged and the result means nothing either
way. Pass `additive=True` when inserting in front of code that stays is genuinely
the point — testing the *order* of two steps needs it.

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

**Four reasons a row survives, and the tell for each.** "Suspect the fixture" is
true and not actionable; these are the four that keep happening, each drawn from
a row that survived here. Check them in this order — the first two are the ones
that look most like a passing test.

| the row survived because | the tell |
|---|---|
| the test drives the **guard**, not its call site | the guard's own test passes while deleting the call from the caller survives. `_refuse_a_real_store` had a test; `build` calling it did not, and the call is the whole protection. |
| the fixture has a **second way** to produce the same observable | the assertion holds for a reason you did not intend. Storm children exited on their own after twenty seconds, so "the lane was killed" and "the lane finished" looked identical, and deleting the `killpg` survived. |
| the hostile input **never reaches** the code | the row is about a path the fixture does not enter. A forged `accepts.age` from a host the reader had *pinned* was refused for its signature before the check under test ran; and a host that has recorded nothing is not a row at all, so the file was never read. |
| the mutation is **equivalent by construction** | no input can distinguish the two. `bytes.decode()` is UTF-8 whatever the locale says, so mutating a locale-sensitive read that decodes bytes proves nothing; `<` against `<=` on a byte-exact comparison is the same. Say so in the pull request rather than inventing a fixture. |

A fifth, rarer: a mutation that removes one half of a **union** survives when the
fixture cannot separate the halves. `_lane` is a process group *plus* the
descendants that left it, and every child in the first fixtures was both — it
took a grandchild orphaned into the group to tell them apart.

**Guards about isolation need both directions.** A sandbox that carries
*nothing* satisfies every "the developer's variables are gone" assertion. Assert
what must arrive as well as what must not: `PATH` with the value it had outside,
and a tool the suite really runs still findable. That one cost a red macOS CI
run against a green Linux one, because `shutil.which` falls back to
`confstr("CS_PATH")` and found an apt-installed `age` where Homebrew's was
invisible.

## Before moving code

[`docs/architecture.md`](docs/architecture.md) is the map: which module may
import which, the two costs that shape most of the odd-looking decisions, and the
five shapes the codebase keeps reusing. The layering is not a convention —
[`tests/test_architecture.py`](tests/test_architecture.py) holds it against the
real import graph, so a new edge between modules is a deliberate one-line edit
and a sentence in the pull request.

When the move is supposed to change nothing a user sees, show that rather than
asserting it — [`tools/compare.py`](tools/compare.py) runs the same commands
against both revisions in a throwaway `$HOME` and diffs stdout, stderr and exit
status together:

```sh
python -m tools.compare --base main --show .bashrc install doctor status
```

Only `$HOME` and the tree path are normalised unless you ask for more with
`--scrub`; whatever is active is printed above the verdict, and that list belongs
in the pull request beside the claim.

If the move might cost time, [`tools/bench.py`](tools/bench.py) is the A/B:

```sh
python -m tools.bench --importtime woswoar.__main__ --base main
```

Both tools' docstrings explain the traps they encode — where the two checkouts
sit, the order readings are taken in, how they are summarised — and it is worth
reading `bench.py`'s before quoting a number from it. When it says *not
resolved*, that is the answer: write "below measurement".

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
  lexically so the open list reads as an implementation order. The exception is
  an issue that only contains other issues: it carries `tracking` and no
  priority, because priority *is* the implementation order and a container has
  none.

Nothing is merged without the maintainer saying so, including by the maintainer's
own agents — said per pull request, or once for a run that works the whole open
issue list.

## Re-recording the demo

`docs/demo.gif` is generated, not captured by hand:

```sh
tools/demo/record.sh          # rebuilds the sandbox, records, shrinks, writes docs/demo.gif
```

It needs [VHS](https://github.com/charmbracelet/vhs), `ttyd`, `ffmpeg` and
`gifsicle`. [`tools/demo/demo.tape`](tools/demo/demo.tape) is the screenplay and
[`tools/demo/seed.py`](tools/demo/seed.py) builds the history it searches — a
throwaway `$HOME` with 54,000 generated commands from three machines, so nobody's
real history is ever on screen. Change the picker, re-run the script; that is the
whole reason the tape is checked in rather than the recording alone.

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
