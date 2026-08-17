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
  rewritten because of it. Every run is ``-B`` and every ``__pycache__`` is
  removed between mutations.
- **An edit that adds without removing is refused.** A replacement that still
  contains the text it replaced leaves the code under test exactly as it was, so
  the run reports "caught" or "SURVIVED" about nothing. That is the easy way to
  write a *move* wrongly -- put the whole span in ``old``, including the line you
  mean to relocate -- and it shipped three times in one session before this
  check. Pass ``additive=True`` for the rare edit that really does mean to insert
  in front of code that stays.
- **The tree is restored even when interrupted.** A ``finally`` is not enough on
  its own -- a kill leaves the source mutated, which is exactly the state
  CLAUDE.md rule 6 is about -- so the original text is also written to a
  recovery file, and its path is printed if anything goes wrong.
"""

from __future__ import annotations

import argparse
import runpy
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple


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


def _clear_bytecode() -> None:
    for cache in Path().glob("*/__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _run(tests: Sequence[str]) -> bool:
    """Whether the suite failed, which for a mutation is the good answer."""
    _clear_bytecode()
    finished = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", *tests],
        capture_output=True,
        text=True,
        check=False,
    )
    return finished.returncode != 0


def verify(mutations: Iterable[Mutation], baseline: bool = True) -> int:
    """Apply each mutation in turn; return how many were *not* caught.

    Prints one line per mutation, in the shape a PR body wants. With
    ``baseline``, also confirms the untouched tree is green -- without which
    "caught" means nothing, because a suite that is already failing catches
    everything.
    """
    survivors: list[Result] = []
    for mutation in mutations:
        source = Path(mutation.path)
        original = source.read_text(encoding="utf-8")
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

        # Written somewhere findable before the file is touched, so an
        # interrupted run leaves a way back that does not depend on this process
        # reaching its `finally`.
        rescue = Path(tempfile.gettempdir()) / f"woswoar-mutate-{source.name}"
        rescue.write_text(original, encoding="utf-8")
        try:
            source.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8")
            caught = _run(mutation.tests.split())
        finally:
            source.write_text(original, encoding="utf-8")
            _clear_bytecode()
            rescue.unlink(missing_ok=True)

        print(f"  {'caught' if caught else 'SURVIVED':9} {mutation.label}")
        if not caught:
            survivors.append(Result(mutation, caught))

    if baseline:
        every = sorted({test for mutation in mutations for test in mutation.tests.split()})
        if _run(every):
            print("  BASELINE RED -- the suite fails untouched, so nothing above means anything")
            return len(list(mutations)) or 1

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
