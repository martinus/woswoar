"""Run the suite in parallel, and refuse to call a partial run green.

The suite is dominated by `tests.test_sync`, which spends about 88% of its wall
clock waiting on real `git`, `age` and `ssh-keygen` -- 5400 git invocations for
one serial run. That is latency, not CPU, so it shards across processes almost
linearly: 21s serially, 6.7s on a four-core runner.

Sharding is by test *class*: classes are the unit that shares `setUpClass`, so
splitting one would run its fixture twice. Classes are then packed into batches,
a few per worker rather than one process per class -- 71 interpreter starts cost
about 5.6s of import CPU between them, which is most of what parallelism had
just bought.

What this adds over `python -m unittest discover` is an accounting check. A
parallel runner has a failure mode a serial one does not: a shard that dies
before it reports, leaving a run that is green because nothing ran. So every id
handed to a batch must come back in that batch's report, by name:

    ids discovered == ids reported

Two honest limits on that. It is a closed loop -- both sides come from the same
`discover()`, so a file renamed out of the `test_*.py` pattern shrinks both and
stays green, exactly as it would under plain unittest. And when a batch dies the
ids come back as never-run, which is the point, but the diagnosis of *why* is in
that batch's own stderr rather than in the summary.

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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _walk(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    """Flatten a suite, surfacing the placeholders unittest uses for load errors.

    A module that raises on import becomes a _FailedTest here rather than
    vanishing. Keeping it means an import error is a red test, not a smaller
    suite -- which is the whole point of this file.
    """
    found: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            found.extend(_walk(item))
        else:
            found.append(item)
    return found


def discover() -> dict[str, list[str]]:
    """Map each test class to the ids it holds, in discovery order."""
    suite = unittest.defaultTestLoader.discover(
        str(ROOT), pattern="test_*.py", top_level_dir=str(ROOT)
    )
    classes: dict[str, list[str]] = {}
    for test in _walk(suite):
        name = f"{type(test).__module__}.{type(test).__qualname__}"
        classes.setdefault(name, []).append(test.id())
    return classes


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


class _Recorder(unittest.TextTestResult):
    """A result that remembers which ids ran, not just how many."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ran: list[str] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.ran.append(test.id())
        super().startTest(test)


def run_batch(names: list[str], out: Path) -> int:
    """Run some classes and write what happened to `out` as JSON."""
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
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
            }
        ),
        encoding="utf-8",
    )
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=0, help="batches at once (default: 2x CPUs)")
    parser.add_argument(
        "--no-skips",
        action="store_true",
        help="treat a skipped test as a failure, for jobs that install every optional tool",
    )
    parser.add_argument("--only", default="", help="run only this module or class")
    parser.add_argument("--worker", nargs="+", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        assert args.out
        return run_batch(args.worker, Path(args.out))

    classes = discover()
    if args.only:
        classes = {name: ids for name, ids in classes.items() if selects(name, args.only)}
        if not classes:
            print(f"::error::no test class matches {args.only!r}")
            return 1
    expected = {tid for ids in classes.values() for tid in ids}

    # Twice the CPUs, because the work is subprocess wait rather than CPU:
    # measured on four cores, jobs=8 beats jobs=4 by ~9%, and jobs=16 regresses.
    cpus = getattr(os, "process_cpu_count", os.cpu_count)  # process_cpu_count is 3.13+
    jobs = args.jobs or (cpus() or 2) * 2
    # More batches than workers, so a batch that runs long is overlapped by the
    # others rather than deciding the wall clock on its own.
    batches = pack(classes, jobs * 2)

    with tempfile.TemporaryDirectory(prefix="woswoar-shards-") as tmp:

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
                print(f"::error::shard died without reporting, exit {proc.returncode}: {names}")
                return {"ran": [], "failures": [], "errors": list(names), "skipped": []}
            loaded: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
            return loaded

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            reports = list(pool.map(run, enumerate(batches)))

    ran = [tid for report in reports for tid in report["ran"]]
    failures = [tid for report in reports for tid in report["failures"]]
    errors = [tid for report in reports for tid in report["errors"]]
    skipped = [pair for report in reports for pair in report["skipped"]]

    print(f"\nRan {len(ran)} tests in {len(batches)} batches on {jobs} workers")
    for label, ids in (("FAIL", failures), ("ERROR", errors)):
        for tid in ids:
            print(f"  {label}: {tid}")  # the traceback is already above, from the batch

    ok = not (failures or errors)
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
