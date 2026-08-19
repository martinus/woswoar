"""Run the suite in parallel, and refuse to call a partial run green.

The suite is dominated by `tests.test_sync`, which spends about 88% of its wall
clock waiting on real `git`, `age` and `ssh-keygen` -- 5400 git invocations for
one serial run. That is latency, not CPU, so it splits across processes almost
linearly: 21s serially, 6.7s on a four-core runner.

Splitting is by test *class*: classes are the unit that shares `setUpClass`, so
splitting one would run its fixture twice. Classes are then packed into batches,
a few per worker rather than one process per class -- 71 interpreter starts cost
about 5.6s of import CPU between them, which is most of what parallelism had
just bought.

What this adds over `python -m unittest discover` is an accounting check. A
parallel runner has a failure mode a serial one does not: a batch that dies
before it reports, leaving a run that is green because nothing ran. So every id
handed to a batch must come back in that batch's report, by name:

    ids discovered == ids reported

Two honest limits on that. It is a closed loop -- both sides come from the same
`discover()`, so a file renamed out of the `test_*.py` pattern shrinks both and
stays green, exactly as it would under plain unittest. And when a batch dies the
ids come back as never-run, which is the point, but the diagnosis of *why* is in
that batch's own stderr rather than in the summary.

There are two splits, and they compose. Batches divide a suite over the
processes of one machine, which is what the paragraphs above are about.
`--shard I/N` divides it over N machines, each of which then batches its own
share. The second exists for macOS: `tests.test_sync` is 17,651 process spawns,
and each costs about four times there what it costs on Linux -- uniformly so
across `git add`, `commit`, `push`, `fetch` and `clone`, which is what says it is
the spawning rather than any one operation. That one suite was 115s of the macOS
job's 145s while taking 38s on Linux. The measurements, and why neither `--jobs`
nor `core.fsync` was the answer, are in `.github/workflows/ci.yml` beside the
job that spends them. Nothing about the accounting changes -- each shard checks
that the ids *it* was given all reported -- and `pack` does both splits, so the
machines are balanced by the rule that already balanced the processes.

`pytest -n auto --dist loadscope` would cover most of this in two dev
dependencies. It is not used because the project carries no runtime dependencies
and three dev ones, all linters -- but that is a maintainer's call, not a
technical impossibility.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parent.parent


def _walk(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    """Flatten a suite, placeholders and all, and let the caller decide.

    A module that raises on import becomes a synthetic `_FailedTest` here rather
    than vanishing, and both callers then drop it -- an import error is reported
    as an unloadable module (#221), not as a test that failed. This function
    keeping it is what makes that choice available: a filter here would leave
    `loader.errors` as the only evidence the module existed at all.
    """
    found: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            found.extend(_walk(item))
        else:
            found.append(item)
    return found


class Found(NamedTuple):
    """What discovery says: the classes to run, and the modules that would not
    import at all."""

    classes: dict[str, list[str]]
    #: Module name -> the first line of the import error, which is the one that
    #: says which module and why.
    unloadable: dict[str, str]


def discover(root: Path = ROOT) -> Found:
    """Map each test class to the ids it holds, in discovery order.

    A module that will not import is *not* one of them, and telling those two
    apart is the whole of #221. `unittest.loader` substitutes a synthetic
    `_FailedTest` for it, which **is** a `TestCase` -- so left alone it packs
    into a batch as an ordinary class, contributes an id to `expected`, runs
    nothing, and surfaces at the end as "1 discovered test never ran" under a
    private class name. Red, which is why nobody was hurt, and wrong about what
    happened: nothing ran because nothing was *there*.

    Told apart through `TestLoader.errors`, a public attribute, exactly as
    `tools/verdict.py` argues at greater length -- no `isinstance` against a
    name `unittest` is free to move.

    `root` is a parameter so this can be pointed at a throwaway tree. It has one
    caller in anger and one in the tests, and the alternative was writing a
    deliberately broken module into `tests/` while the suite that finds it is
    running in 64 parallel workers.
    """
    loader = unittest.TestLoader()
    suite = loader.discover(str(root), pattern="test_*.py", top_level_dir=str(root))
    unloadable = _unloadable(loader)
    classes: dict[str, list[str]] = {}
    for test in _loaded(suite, unloadable):
        name = f"{type(test).__module__}.{type(test).__qualname__}"
        classes.setdefault(name, []).append(test.id())
    return Found(classes, unloadable)


def _unloadable(loader: unittest.TestLoader) -> dict[str, str]:
    """The modules a loader could not import, by name.

    `TestLoader.errors` is public and holds a formatted traceback per module; its
    first line is "Failed to import test module: tests.test_x", so the module is
    the tail of it and the line itself is what a reader needs. Both the surgery
    and the message shape live here rather than at the two call sites, because
    they encode what `unittest` prints and that is one fact.
    """
    return {
        said.splitlines()[0].rsplit(": ", 1)[-1]: said.splitlines()[0]
        for said in (str(error) for error in loader.errors)
    }


def _loaded(suite: unittest.TestSuite, unloadable: dict[str, str]) -> list[unittest.TestCase]:
    """The real tests in `suite`, without the placeholders for `unloadable`.

    A placeholder's id is its class plus the module it could not import, so the
    module is the tail. Matched that way rather than against
    `unittest.loader._FailedTest`: that is the private name `tools/verdict.py`
    refuses to make decisions from, and if `unittest` moves it this still
    answers.
    """
    return [
        test
        for test in _walk(suite)
        if not any(test.id().endswith(f".{name}") for name in unloadable)
    ]


def selects(name: str, only: str) -> bool:
    """Does `only` select the class `name`?

    Anchored at a dot rather than matching any substring: the sync job pins its
    fatal-skip policy with `--only tests.test_sync`, and a later
    `tests/test_sync_chunks.py` would otherwise be dragged under that policy
    without anyone choosing it.
    """
    return name == only or name.startswith(only + ".")


def pack(classes: dict[str, list[str]], bins: int) -> list[list[str]]:
    """Spread classes over batches, largest first onto the lightest batch.

    Test count is a rough proxy for duration -- measured, packing by true
    duration would save about 5% -- but it is free, and it keeps a fat class
    from being scheduled last, which is what actually sets the wall clock.

    Never more batches than classes: an empty batch would start an interpreter
    to run nothing. That clamp is also why no filter is needed afterwards --
    with at most one batch per class, `min(weights)` always finds a batch that
    is still empty, so none is left behind.
    """
    batches: list[list[str]] = [[] for _ in range(max(1, min(bins, len(classes))))]
    weights = [0] * len(batches)
    for name in sorted(classes, key=lambda n: (-len(classes[n]), n)):
        light = weights.index(min(weights))
        batches[light].append(name)
        weights[light] += len(classes[name])
    return batches


def shard_of(spec: str) -> tuple[int, int]:
    """Parse ``I/N`` into a zero-based index and a count.

    Raises `ValueError` with the text a caller should print. Separate from
    `main` so the parsing has a test that does not need a suite to run.
    """
    index, sep, total = spec.partition("/")
    if not sep or not index.strip().isdigit() or not total.strip().isdigit():
        raise ValueError(f"--shard wants I/N, got {spec!r}")
    got, count = int(index), int(total)
    if count < 1 or not 1 <= got <= count:
        raise ValueError(f"--shard {spec} is out of range: I must be 1..N and N at least 1")
    return got - 1, count


class _Recorder(unittest.TextTestResult):
    """A result that remembers which ids ran, not just how many."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ran: list[str] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.ran.append(test.id())
        super().startTest(test)


def run_batch(names: list[str], out: Path) -> int:
    """Run some classes and write what happened to `out` as JSON.

    Discovery in the parent already sets the unloadable modules aside, so this
    check is the belt to that brace: a module can import there and not here --
    a half-written file caught mid-save, an import with a side effect that
    depends on which worker got there first -- and the batch must still run the
    classes that did load. That is why this does not simply reuse
    `tools/verdict.py`, whose answer to `loader.errors` is to run nothing at
    all: the shared thing is the classification, not the control flow.
    """
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names)
    unloadable = _unloadable(loader)
    if unloadable:
        # The whole traceback, once, where a human reads it in context -- the
        # summary carries only the first line, as it does for every other row.
        for error in loader.errors:
            print(error, file=sys.stderr)
        suite = unittest.TestSuite(_loaded(suite, unloadable))
    # Tracebacks go to stderr, where they interleave with the tests' own output
    # and a human reads them in context. Only ids travel back to the parent:
    # shipping the traceback too would print every failure twice.
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1, resultclass=_Recorder).run(
        suite
    )
    assert isinstance(result, _Recorder)
    out.write_text(
        json.dumps(
            {
                "ran": result.ran,
                "failures": [test.id() for test, _ in result.failures],
                "errors": [test.id() for test, _ in result.errors],
                # The reason, unlike a traceback, is not printed by the child at
                # this verbosity, so it would otherwise be lost.
                "skipped": [[test.id(), why] for test, why in result.skipped],
                # A new key rather than a new meaning for `errors`: the parent
                # reads this file as a protocol, and an id that is a module
                # rather than a test would be counted as a test that failed.
                "unloadable": unloadable,
            }
        ),
        encoding="utf-8",
    )
    return 0 if result.wasSuccessful() and not unloadable else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=0, help="batches at once (default: 2x CPUs)")
    parser.add_argument(
        "--no-skips",
        action="store_true",
        help="treat a skipped test as a failure, for jobs that install every optional tool",
    )
    parser.add_argument("--only", default="", help="run only this module or class")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="MODULE_OR_CLASS",
        help="skip this module or class entirely; repeatable, and each pattern must "
        "match. For a suite that cannot run somewhere, so the rest keeps --no-skips",
    )
    parser.add_argument(
        "--shard",
        default="",
        metavar="I/N",
        help="run only shard I of N, for splitting one suite over N machines. "
        "The batches below split a suite over the processes of one machine; this "
        "splits it over the machines, and the two compose",
    )
    parser.add_argument("--worker", nargs="+", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        assert args.out
        return run_batch(args.worker, Path(args.out))

    classes, unloadable = discover()
    if args.only:
        classes = {name: ids for name, ids in classes.items() if selects(name, args.only)}
        # The filter reaches the broken modules too, or `--only tests.test_x` on
        # a module that will not import answers "no test class matches" -- true,
        # unhelpful, and the opposite of what someone running that command is
        # trying to find out.
        unloadable = {name: why for name, why in unloadable.items() if selects(name, args.only)}
        if not classes and not unloadable:
            print(f"::error::no test class matches {args.only!r}")
            return 1
    # One pattern at a time, and each has to match something. Checking only
    # that the *set* shrank would let a renamed class stop being excluded as
    # long as any other pattern still matched -- and the job that needs this
    # passes two, so that is the likely case rather than the exotic one. What it
    # would cost is a red build on the platform the tool is missing from, which
    # is precisely what naming the class was meant to avoid.
    for pattern in args.exclude:
        kept = {name: ids for name, ids in classes.items() if not selects(name, pattern)}
        if len(kept) == len(classes):
            print(f"::error::no test class matches --exclude {pattern!r}")
            return 1
        classes = kept
    if args.shard:
        try:
            index, count = shard_of(args.shard)
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        # `pack` is reused rather than a slice of the sorted names, so the
        # machines get comparable amounts of work for the same reason the
        # processes do -- and so that one balancing rule is tested once.
        #
        # More shards than classes is fatal rather than an empty green run. An
        # empty shard reports "Ran 0 tests" and exits 0, which is precisely the
        # partial run that is green because nothing happened -- the thing this
        # script exists to refuse. It can only come from a matrix that outgrew
        # the suite, so it is the CI config that is wrong, and silence there
        # would spread to every shard as classes were removed.
        if count > len(classes):
            print(f"::error::--shard {args.shard} wants more shards than there are classes")
            return 1
        chosen = pack(classes, count)[index]
        classes = {name: classes[name] for name in chosen}
    expected = {tid for ids in classes.values() for tid in ids}

    # Twice the CPUs, because the work is subprocess wait rather than CPU:
    # measured on four cores, jobs=8 beats jobs=4 by ~9%, and jobs=16 regresses.
    cpus = getattr(os, "process_cpu_count", os.cpu_count)  # process_cpu_count is 3.13+
    jobs = args.jobs or (cpus() or 2) * 2
    # More batches than workers, so a batch that runs long is overlapped by the
    # others rather than deciding the wall clock on its own.
    batches = pack(classes, jobs * 2)

    with tempfile.TemporaryDirectory(prefix="woswoar-batches-") as tmp:

        def run(indexed: tuple[int, list[str]]) -> dict[str, Any]:
            index, names = indexed
            out = Path(tmp) / f"{index}.json"
            proc = subprocess.run(
                [sys.executable, "-m", "tools.run_tests", "--worker", *names, "--out", str(out)],
                cwd=ROOT,
            )
            if not out.exists():
                # Killed by the OOM killer, a segfault in a C extension, a crash
                # in setUpClass: no report, so nothing it would have said can be
                # believed. Its ids come out below as never-run.
                print(f"::error::batch died without reporting, exit {proc.returncode}: {names}")
                return {
                    "ran": [],
                    "failures": [],
                    "errors": list(names),
                    "skipped": [],
                    "unloadable": {},
                }
            loaded: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
            return loaded

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            reports = list(pool.map(run, enumerate(batches)))

    for report in reports:
        unloadable.update(report["unloadable"])
    ran = [tid for report in reports for tid in report["ran"]]
    failures = [tid for report in reports for tid in report["failures"]]
    errors = [tid for report in reports for tid in report["errors"]]
    skipped = [pair for report in reports for pair in report["skipped"]]

    where = f" (shard {args.shard})" if args.shard else ""
    print(f"\nRan {len(ran)} tests in {len(batches)} batches on {jobs} workers{where}")
    for label, ids in (("FAIL", failures), ("ERROR", errors)):
        for tid in ids:
            print(f"  {label}: {tid}")  # the traceback is already above, from the batch

    for name in sorted(unloadable):
        # Its own line and its own wording: this is not a test that failed, and
        # printing it as one is what sent a reader looking for an assertion.
        print(f"::error::could not import {name}: {unloadable[name]}")
    ok = not (failures or errors or unloadable)
    if missing := expected - set(ran):
        # The accounting check this script exists for.
        print(f"::error::{len(missing)} discovered tests never ran")
        for tid in sorted(missing):
            print(f"  never ran: {tid}")
        ok = False
    if duplicated := len(ran) - len(set(ran)):
        print(f"::error::{duplicated} tests ran more than once")
        ok = False
    if skipped:
        for tid, why in skipped:
            print(f"  skipped: {tid} ({why})")
        if args.no_skips:
            print(f"::error::{len(skipped)} tests were skipped - an optional tool is missing")
            ok = False

    print(
        f"{'OK' if ok else 'FAILED'} ({len(failures)} failures, {len(errors)} errors, "
        f"{len(skipped)} skipped)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
