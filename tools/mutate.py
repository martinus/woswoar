"""Revert a fix, watch its test fail, restore it -- the loop CLAUDE.md rule 3 asks for.

A test that passes whether or not the fix is present is decoration, and reading
it will not tell you which kind you have. The only way to know is to break the
code and watch. This does that, for a table of edits:

    from tools.mutate import Mutation, verify

    verify(
        [
            Mutation(
                "the guard is gone",
                "woswoar/sync.py",
                "if stamp is not None and settled:",
                "if True:",
                "tests.test_sync.TestSkippingAnUnchangedDay",
            ),
        ]
    )

Run it with ``python -m tools.mutate <script>``, or import `verify` from a
throwaway script of your own. Either way the point is that the *table* is the
new work and everything below is not. Paths are relative to the working
directory, so run it from the repo root, as with `tools.run_tests`.

Two things it does that a hand-rolled loop forgets, both of which have cost real
time here:

- **The bytecode cache lies.** A ``.pyc`` is validated against
  ``(mtime_seconds, size)``, so two mutations that change a file by the same
  number of bytes inside one second run each other's cached bytecode. That
  reported a *correct* test as decoration once, and the test was nearly
  rewritten because of it. Three things now make it impossible rather than
  merely unlikely: each copy of the tree starts with no ``__pycache__``, the
  runs are ``-B`` *and* carry ``PYTHONDONTWRITEBYTECODE`` so the real ``age``
  and ``git`` subprocesses the suite spawns cannot write one either, and a
  reused copy is swept between mutations anyway.
- **An edit that adds without removing is refused.** A replacement that still
  contains the text it replaced leaves the code under test exactly as it was, so
  the run reports "caught" or "SURVIVED" about nothing. That is the easy way to
  write a *move* wrongly -- put the whole span in ``old``, including the line you
  mean to relocate -- and it shipped three times in one session before this
  check. Pass ``additive=True`` for the rare edit that really does mean to insert
  in front of code that stays.
- **Your working tree is never edited.** Each mutation is applied to a throwaway
  copy, so a kill at the wrong moment cannot leave a mutated source behind --
  the state CLAUDE.md rule 6 is about, and one this used to guard against with a
  ``finally`` and a rescue file in ``/tmp``. A copy is 2 MB and takes
  milliseconds; the earlier design paid for its cheapness with the one failure
  mode the whole rule exists to prevent.

  That is also what makes the mutations run **in parallel**. They are
  independent by construction, each is mostly waiting on a subprocess, and the
  suite a mutation runs is plain serial ``unittest`` rather than the sharded
  runner -- so there is no nested pool to oversubscribe. Measured as
  ``workers=1`` against the default, same design either way: four mutations
  against ``tests.test_sync`` went 197.5 s to 51.5 s, and eleven against the
  faster modules 2.4 s to 0.5 s. Both figures are serial-versus-parallel, not
  before-and-after this module was rewritten -- the old design also ran in the
  working tree rather than a copy, so no measurement here separates those two
  changes and none is quoted as if it did.

**What a sandbox cannot check.** ``.git`` is not copied, and the copy lands
wherever ``$TMPDIR`` points -- tmpfs on most machines. So a test that needs a
repository to check a revision out of, or that distinguishes two filesystems,
skips in here and its guard cannot be mutation-verified through this module.
`tests/test_sandbox.py` is the case that exists; those guards were verified by
reverting them in the working tree by hand. Worth knowing before concluding that
a surviving mutation means a weak test.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from tools.sandbox import usable_cpus

#: Names never copied into a mutation's sandbox. ``.git`` because it is large and
#: nothing under test reads it; the caches because a stale one is the trap this
#: module documents, and the cheapest way to not have it is to not copy it.
_SKIP = shutil.ignore_patterns(
    ".git", "__pycache__", ".mypy_cache", ".ruff_cache", ".venv", "*.egg-info"
)


class Mutation(NamedTuple):
    """One edit that some test is supposed to notice."""

    #: What the mutation does, in the words of whoever might reintroduce it.
    #: Printed, so make it a sentence a reader of the PR would understand.
    label: str
    path: str
    #: Must appear exactly once in the file. Ambiguity is an error rather than a
    #: guess: replacing the wrong one of two matches quietly tests nothing.
    old: str
    new: str
    #: Whitespace-separated unittest targets, as `python -m unittest` takes them.
    tests: str
    #: Say so when the replacement is meant to *contain* the original -- an
    #: inserted call, an early return in front of code that stays. Otherwise
    #: `verify` refuses that shape, because it is overwhelmingly a mistake: see
    #: the check for why, and what it cost before it existed.
    additive: bool = False


class Result(NamedTuple):
    mutation: Mutation
    caught: bool


def _clear_bytecode(root: Path) -> None:
    """The sweep the trap in the module docstring needs, for a real reason.

    Not belt and braces: `tests/test_search.py`'s `picker_env` builds its own
    environment from literals to drive the pty, so it carries no
    ``PYTHONDONTWRITEBYTECODE``, and its ``PYTHONPATH`` points at the tree running
    the suite -- the sandbox. Those `python -m woswoar` children do write a
    ``__pycache__`` into a copy that a later mutation reuses, which is exactly the
    ``(mtime, size)`` collision this module exists to avoid. Measured at 0.6 ms on
    the full tree and 12 directories in a sandbox, against a multi-second run.
    """
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _run(tests: Sequence[str], root: Path) -> bool:
    """Whether the suite failed, which for a mutation is the good answer.

    A non-zero exit is not enough on its own, and this is the one hazard here
    that produces a wrong answer in the direction a reader believes. A mutation
    that makes the module unimportable, or breaks a `setUpClass`, also exits
    non-zero -- so it would print `caught` while the test whose name is in the row
    never ran. So the count `unittest` prints is checked too: no "Ran N" line, or
    "Ran 0", means the mutation broke collection rather than being noticed.
    """
    _clear_bytecode(root)
    finished = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", *tests],
        cwd=root,
        # `-B` covers this process; the variable covers the real `age`, `git` and
        # `bash` the suite spawns, which would otherwise leave a `.pyc` in a
        # sandbox that a later mutation reuses.
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    ran = re.search(r"^Ran (\d+) tests? in ", finished.stderr, re.MULTILINE)
    if ran is None or ran.group(1) == "0":
        raise SystemExit(
            f"{' '.join(tests)} ran no tests under this mutation, so 'caught' would "
            f"mean 'the suite could not start' rather than 'a test noticed'. The "
            f"mutation probably broke an import.\n{finished.stderr[-2000:]}"
        )
    return finished.returncode != 0


def _check(mutation: Mutation) -> None:
    """Refuse a spec that cannot mean anything, reading the real file.

    Every mutation is checked before any of them runs -- see `verify`. A typo in
    the last row of a table used to be found after the first four had each cost a
    suite run.
    """
    original = Path(mutation.path).read_text(encoding="utf-8")
    found = original.count(mutation.old)
    if found != 1:
        raise SystemExit(
            f"{mutation.label}: {mutation.path} contains the text to replace "
            f"{found} times, not once. A mutation that matches nothing tests "
            f"nothing, and one that matches twice tests something else."
        )

    # The replacement still contains everything it replaced, so the line the
    # test is supposed to miss is still in the file and the run cannot mean
    # anything. Always a mistake when the intent was to *move* something --
    # write the whole span in `old`, including what follows it, or the
    # original stays put underneath the edit.
    #
    # Three of these went out in one session before this check existed. Each
    # cost a full suite run and read as "caught" when nothing had changed;
    # the tell is that a mutation meant to break an invariant reports the
    # same result as one that does nothing, and there is no way to see which
    # from the output.
    if not mutation.additive and mutation.old in mutation.new:
        raise SystemExit(
            f"{mutation.label}: the text to replace survives verbatim inside "
            f"the replacement, so the code under test does not change and "
            f"'caught' would mean nothing. If the point really is to insert "
            f"something in front of code that stays, pass additive=True."
        )


@contextmanager
def _sandboxes(count: int) -> Iterator[queue.Queue[Path]]:
    """``count`` borrowable copies of the working tree, lent out one at a time.

    A queue rather than one copy per mutation: a copy is 1.9 MB of 87 files and
    measures about a millisecond, while a test run costs seconds -- but there is
    still no reason to make five hundred of them for a table of five hundred rows.
    Borrow, mutate, restore, return.

    **Every** borrower is a pool task, which is what keeps this deadlock-free. An
    earlier version handed the baseline a copy taken on the *main* thread and
    never returned it, so with a single lane the baseline held the only sandbox
    and every mutation waited on an empty queue for ever. A task always returns
    what it borrowed in a `finally`; the main thread must never take one.

    Copied rather than ``git worktree add``, which would check out *HEAD* and so
    quietly test committed code -- the mutation is meant to apply to the tree as
    it stands, uncommitted changes and all, which is the whole situation this is
    used in.
    """
    with tempfile.TemporaryDirectory(prefix="woswoar-mutate-") as holder:
        available: queue.Queue[Path] = queue.Queue()
        for index in range(count):
            root = Path(holder) / f"tree{index}"
            shutil.copytree(Path.cwd(), root, ignore=_SKIP, symlinks=True)
            available.put(root)
        yield available


def _borrow(available: queue.Queue[Path], tests: Sequence[str]) -> bool:
    """Run ``tests`` unmutated in a borrowed sandbox. One shard of the baseline."""
    root = available.get()
    try:
        return _run(tests, root)
    finally:
        available.put(root)


def _attempt(mutation: Mutation, available: queue.Queue[Path]) -> bool:
    """Apply one mutation in a borrowed sandbox and report whether it was caught."""
    root = available.get()
    try:
        source = root / mutation.path
        original = source.read_text(encoding="utf-8")
        source.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8")
        try:
            return _run(mutation.tests.split(), root)
        finally:
            # Into the sandbox, not the working tree. Only so the next mutation
            # to borrow this copy starts from clean source.
            source.write_text(original, encoding="utf-8")
    finally:
        available.put(root)


def verify(mutations: Iterable[Mutation], baseline: bool = True, workers: int | None = None) -> int:
    """Apply each mutation in its own copy of the tree; return how many survived.

    Prints one line per mutation, in the order the table gives them and not the
    order they finish, so that the output is the same every run and can be pasted
    into a pull request as it stands.

    With ``baseline``, also confirms the untouched tree is green -- without which
    "caught" means nothing, because a suite that is already failing catches
    everything. Its targets are sharded and queued alongside the mutations rather
    than run as one serial pass, because a single pass over the union of every
    target was measured at two thirds of the wall clock.
    """
    table = list(mutations)
    if not table:
        return 0
    for mutation in table:
        _check(mutation)

    # One shard per target rather than one serial run over the union. Measured on
    # four pinned cores with eight `tests.test_sync` classes: the mutation phase
    # took 2.35 s across eight lanes while a single-run baseline took 6.75 s, so
    # the check meant to cost nothing was two thirds of the wall clock and got
    # worse as the table grew -- mutations fan out, a union sums.
    shards = sorted({test for mutation in table for test in mutation.tests.split()})
    # Twice the usable cores, which is what `tools/run_tests.py` measured for this
    # same subprocess-wait-bound work (jobs=8 beat jobs=4 by ~9%, jobs=16
    # regressed). An earlier `cpu // 2` here gave two lanes on a four-core runner
    # and was 1.76x slower than this for eight runs. A sandbox is ~1 ms, so a lane
    # is nearly free.
    lanes = workers or min(len(table) + len(shards), usable_cpus() * 2, 16)

    survivors: list[Result] = []
    with _sandboxes(lanes) as available, ThreadPoolExecutor(max_workers=lanes) as pool:
        checking = (
            [pool.submit(_borrow, available, [shard]) for shard in shards] if baseline else []
        )
        futures = [pool.submit(_attempt, mutation, available) for mutation in table]
        for mutation, future in zip(table, futures, strict=True):
            caught = future.result()
            print(f"  {'caught' if caught else 'SURVIVED':9} {mutation.label}")
            if not caught:
                survivors.append(Result(mutation, caught))
        if any(future.result() for future in checking):
            print("  BASELINE RED -- the suite fails untouched, so nothing above means anything")
            return len(table)

    if survivors:
        print(f"\n{len(survivors)} survived. A test that cannot see the fix removed is decoration:")
        for result in survivors:
            print(f"  - {result.mutation.label} ({result.mutation.tests})")
        print("Suspect the fixture before the mutation -- see CLAUDE.md rule 3.")
    return len(survivors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.mutate",
        description="Run a script that defines MUTATIONS, and report which are caught.",
    )
    parser.add_argument("script", help="a Python file defining MUTATIONS: list[Mutation]")
    args = parser.parse_args(argv)

    namespace = runpy.run_path(args.script)
    mutations = namespace.get("MUTATIONS")
    if not mutations:
        raise SystemExit(f"{args.script} defines no MUTATIONS")
    return 1 if verify(mutations) else 0


if __name__ == "__main__":
    raise SystemExit(main())
