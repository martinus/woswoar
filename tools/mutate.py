"""Revert a fix, watch its test fail, restore it -- the loop CLAUDE.md rule 3 asks for.

A test that passes whether or not the fix is present is decoration, and reading
it will not tell you which kind you have. The only way to know is to break the
code and watch. This does that, for a table of edits:

    from tools.mutate import Mutation

    MUTATIONS = [
        Mutation(
            "the guard is gone",
            "woswoar/sync.py",
            "if stamp is not None and settled:",
            "if True:",
            "tests.test_sync.TestSkippingAnUnchangedDay",
        ),
    ]

Run it with ``python -m tools.mutate <script>``. The *table* is the new work and
everything below is not. Paths are relative to the working directory, so run it
from the repo root, as with `tools.run_tests`.

``MUTATIONS`` at module level is the shape, and this example used to show a
`verify(...)` call instead -- which works, and then exited non-zero with "defines
no MUTATIONS" printed *above* its own green results (#213). Calling `verify`
yourself is still supported and still correct; it is simply no longer what the
documentation teaches, because the two shapes in one file run the table twice.

Four things it does that a hand-rolled loop forgets, all of which have cost real
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
- **"caught" means a test method noticed, and nothing else.** A mutation that
  turns a working import into a failing one exits non-zero and leaves ``Ran 1
  test`` behind, exactly like a test catching it -- so a check reading the exit
  status and the count reported ``caught`` about a test that never executed.
  That shipped. The only fixture guarding it used a *syntax* error, which
  ``unittest.loader`` does not wrap and which therefore takes a different path
  entirely, so the check passed while the case it was named for went unasked --
  the too-weak fixture CLAUDE.md rule 3 warns about, in the harness that exists
  to find them. `_PROBE` now classifies where the result objects still are, and
  a run that could not put the question says ``BROKE`` rather than either answer.
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
import json
import os
import queue
import runpy
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, NamedTuple

from tools import mutants
from tools.mutants import Mutation, check
from tools.sandbox import usable_cpus

#: Re-exported, because every spec file and every pasted pull-request output in
#: this repository's history says `from tools.mutate import Mutation`. The
#: definition lives with the generator that produces most of them now.
__all__ = ["Mutation", "Report", "Result", "Verdict", "run", "verify"]

#: Seconds one mutation may take before the run gives up on it. Generous, because
#: `tests.test_sync` alone is tens of seconds and a loaded machine is slower
#: still; the point is only that a mutant which never terminates cannot hold a
#: lane for the rest of the table.
TIMEOUT = 300.0

#: The most lanes worth running, whatever the machine reports.
_LANES = 16

#: Address space one mutant may occupy, handed to `verdict.cap`.
#:
#: `TIMEOUT` answers "this mutation never finishes"; this answers "this mutation
#: never finishes *while allocating*", which is a different failure and the more
#: dangerous one -- a timeout cannot fire on a machine that is already gone.
#: A generated `at -= ...` in `mutants.line_starts` reached 15.5 GB in 73 s and
#: OOM-killed the machine twice while this branch was being written, well
#: inside the 300 s above.
#:
#: 4 GiB is measured rather than chosen: one honest whole-suite run peaks at
#: 317 MB resident and 1537 MB of address space, so this leaves a 2.7x margin
#: over the thing it must never interrupt. If a legitimate suite ever reports
#: `ran out of memory`, raise it -- the message names the test on purpose.
MEMORY = 4 << 30

#: Names never copied into a mutation's sandbox. ``.git`` because it is large and
#: nothing under test reads it; the caches because a stale one is the trap this
#: module documents, and the cheapest way to not have it is to not copy it.
_SKIP = shutil.ignore_patterns(
    ".git", "__pycache__", ".mypy_cache", ".ruff_cache", ".venv", "*.egg-info"
)


#: What one run of the suite under one mutation is allowed to conclude.
#:
#: `broke` and `timeout` are not answers. They say the run could not ask the
#: question, and they must never be folded into either real verdict -- a
#: `broke` counted as `caught` blesses a test that never executed, which is the
#: failure this module's own docstring calls indistinguishable from a real one.
Outcome = Literal["caught", "survived", "broke", "timeout"]


class Verdict(NamedTuple):
    outcome: Outcome
    #: The first test that noticed, or the reason nothing could. Printed for
    #: everything except a plain `caught`, where the label already says it.
    detail: str = ""

    @property
    def answered(self) -> bool:
        """Was a question put at all?

        One definition, because three sites need it and the run, the summary and
        the exit status each spelled it differently -- and one of the three said
        something subtly other than the other two.
        """
        return self.outcome in ("caught", "survived")


class Result(NamedTuple):
    mutation: Mutation
    verdict: Verdict


class Report(NamedTuple):
    results: list[Result]
    #: Nothing in `results` means anything when this is set: a suite that already
    #: fails catches every mutation, including the ones no test can see.
    baseline_red: bool = False

    @property
    def clean(self) -> bool:
        """Every row caught, and the baseline green. The definition of done.

        Here rather than in the CLI helper that used to own it, because `verify`
        and any generated-table driver need the same answer and only one of them
        had a way to ask. A row that broke or timed out is not clean: it is a
        question the run failed to put, and calling it green would report the
        table as smaller than it was while claiming it was complete.
        """
        return not self.baseline_red and all(
            result.verdict.outcome == "caught" for result in self.results
        )


#: Every table `run` has finished in this process, in order.
#:
#: Only `main` reads it, and only to tell "the script defined nothing" apart from
#: "the script did the work itself". Before this existed, a spec written exactly
#: as the docstring showed it ran correctly and *then* exited 1 with
#: "defines no MUTATIONS" -- printed above its own green results, because stderr
#: is not line-buffered against stdout (#213).
_RUNS: list[Report] = []

#: Nine wide, as before. `caught` stays lowercase and everything else shouts,
#: because the eye scanning a pasted table is looking for the rows that are not
#: the good news.
_HEADLINE: dict[str, str] = {
    "caught": "caught",
    "survived": "SURVIVED",
    "broke": "BROKE",
    "timeout": "TIMEOUT",
}


def _probe() -> str:
    """`tools/verdict.py`, read from *this* tree rather than the sandbox's copy.

    Handed to ``python -c``, which is what puts the sandbox on ``sys.path`` so its
    test modules import at all. Reading it from `__file__`'s directory is the
    whole isolation property: `tools/**.py` is itself something a table may
    mutate, and a verdict loaded out of the copy would let a mutation grade its
    own exam. See that module's docstring for why the classification lives there
    and not in a string constant here.
    """
    return (Path(__file__).resolve().with_name("verdict.py")).read_text(encoding="utf-8")


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


def _run(
    tests: Sequence[str],
    root: Path,
    *,
    failfast: bool = False,
    timeout: float = TIMEOUT,
    memory: int = MEMORY,
) -> Verdict:
    """What the suite concluded about one mutation, and by which route.

    The verdict used to be "the exit status was non-zero, and `Ran N` said N was
    not zero". That is wrong in the direction a reader believes, and it shipped:
    a mutation that turns a working import into a failing one leaves `Ran 1
    test`, `errors=1` and a non-zero exit, exactly like a test noticing. The only
    fixture guarding it used a *syntax* error, which takes a different path
    through `unittest.loader` and produces no count at all -- so the check passed
    while the case it named went unasked. `_PROBE` is the answer: classify where
    the result objects still exist.

    A timeout is its own outcome rather than an exception. A generated mutant can
    turn a loop bound into one that never fires, and with no limit here that
    holds a lane for the rest of the run.
    """
    _clear_bytecode(root)
    # Both files land outside the sandbox on purpose: the copy is what the
    # mutation edits, and a report written into it is one `open()` away from
    # being the mutation's to write.
    with tempfile.TemporaryDirectory(prefix="woswoar-verdict-") as box:
        report = Path(box) / "verdict.json"
        noise = Path(box) / "stderr.txt"
        try:
            with noise.open("w", encoding="utf-8") as spill:
                subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        _probe(),
                        str(report),
                        "1" if failfast else "0",
                        str(memory),
                        *tests,
                    ],
                    cwd=root,
                    # `-B` covers this process; the variable covers the real
                    # `age`, `git` and `bash` the suite spawns, which would
                    # otherwise leave a `.pyc` in a sandbox that a later mutation
                    # reuses.
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    # A file rather than a pipe, and this is not a style choice.
                    # On a timeout `subprocess.run` kills the child and then
                    # drains the pipes with `communicate()` and *no* limit --
                    # and the suite's `python -m woswoar` grandchildren inherit
                    # the write end, so that drain waits for them. A hung
                    # grandchild would hold the lane long past `timeout`, which
                    # is the one thing this outcome exists to prevent.
                    stdout=subprocess.DEVNULL,
                    stderr=spill,
                    check=False,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            return Verdict("timeout", f"no answer within {timeout:g}s")

        try:
            written = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The probe was killed before it could write anything at all. Rare
            # and always worth the tail, because nothing else says why.
            return Verdict("broke", _tail(noise))
        if not written["loaded"]:
            return Verdict("broke", _tail(noise) or str(written.get("why", "")).strip())

    if written["broke"]:
        return Verdict("broke", str(written["broke"][0]))
    if written["noticed"]:
        return Verdict("caught", str(written["noticed"][0]))
    if not written["ran"]:
        return Verdict("broke", "the targets held no tests")
    return Verdict("survived")


def _tail(noise: Path) -> str:
    """The last line the run managed to say. Empty when it said nothing."""
    try:
        spoken = noise.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return spoken[-1].strip() if spoken else ""


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


def _borrow(
    available: queue.Queue[Path], tests: Sequence[str], timeout: float, memory: int
) -> Verdict:
    """Run ``tests`` unmutated in a borrowed sandbox. One shard of the baseline.

    Never `failfast`: a red baseline is a thing you want the whole of, and a
    green one never stops early anyway, so there is nothing to buy.
    """
    root = available.get()
    try:
        return _run(tests, root, timeout=timeout, memory=memory)
    finally:
        available.put(root)


def _applied(original: str, mutation: Mutation) -> str:
    """The file with one mutation in it.

    Two ways, because the two kinds of row promise different things. A
    hand-written `old` is unique in the file -- `check` refused it otherwise --
    so `replace` cannot land anywhere else. A generated one usually is *not*
    unique (`if not path.exists():` appears many times), so it carries the
    offsets it applies at and the text is spliced there. `check` has already
    confirmed those offsets still hold the text the row quotes.
    """
    if mutation.span is None:
        return original.replace(mutation.old, mutation.new)
    start, end = mutation.span
    return original[:start] + mutation.new + original[end:]


def _attempt(
    mutation: Mutation,
    available: queue.Queue[Path],
    failfast: bool,
    timeout: float,
    memory: int,
) -> Verdict:
    """Apply one mutation in a borrowed sandbox and report what the suite said."""
    root = available.get()
    try:
        source = root / mutation.path
        original = source.read_text(encoding="utf-8")
        source.write_text(_applied(original, mutation), encoding="utf-8")
        try:
            return _run(
                mutation.tests.split(), root, failfast=failfast, timeout=timeout, memory=memory
            )
        finally:
            # Into the sandbox, not the working tree. Only so the next mutation
            # to borrow this copy starts from clean source.
            source.write_text(original, encoding="utf-8")
    finally:
        available.put(root)


def _affordable(memory: int) -> int:
    """How many lanes fit in memory, which is a different question from cores.

    `verdict.cap` bounds one lane; it does not bound their product, and the
    product is what the host feels. Sixteen lanes at 4 GiB is 64 GiB on a 62 GiB
    machine, so a table can still drive it into swap with every individual cap
    respected -- and a *generated* table is the shape that does it, because it
    walks a file in order and its runaway rows are therefore adjacent. When this
    crash was diagnosed, three of four lanes were running away simultaneously,
    all on the same source line.

    Half of physical memory, because the other half belongs to whoever is using
    the machine. Mutation testing is a background chore and has no claim on the
    whole box; an explicit `--workers` still overrides this, as it overrides
    every other bound below.
    """
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):  # pragma: no cover - not POSIX
        return _LANES
    return max(1, total // 2 // memory)


def run(
    mutations: Iterable[Mutation],
    baseline: bool = True,
    workers: int | None = None,
    *,
    strict: bool = True,
    failfast: bool = False,
    timeout: float = TIMEOUT,
    memory: int = MEMORY,
    summarise: bool = True,
) -> Report:
    """Apply each mutation in its own copy of the tree; report what each answered.

    Prints one line per mutation, in the order the table gives them and not the
    order they finish, so that the output is the same every run and can be pasted
    into a pull request as it stands.

    With ``baseline``, also confirms the untouched tree is green -- without which
    "caught" means nothing, because a suite that is already failing catches
    everything. Its targets are sharded and queued alongside the mutations rather
    than run as one serial pass, because a single pass over the union of every
    target was measured at two thirds of the wall clock.

    ``strict`` decides what an unanswerable row does. For a hand-written table it
    should stop the run: a row that breaks collection is a mistake in the table,
    and the other rows will still be there once it is fixed. For a generated one
    it must not, because a single non-viable mutant out of two hundred would
    throw away every answer already paid for.
    """
    table = list(mutations)
    if not table:
        return Report([])
    for mutation in table:
        check(mutation)

    # One shard per target rather than one serial run over the union. Measured on
    # four pinned cores with eight `tests.test_sync` classes: the mutation phase
    # took 2.35 s across eight lanes while a single-run baseline took 6.75 s, so
    # the check meant to cost nothing was two thirds of the wall clock and got
    # worse as the table grew -- mutations fan out, a union sums.
    shards = sorted({mutation.tests for mutation in table})
    # Twice the usable cores, which is what `tools/run_tests.py` measured for this
    # same subprocess-wait-bound work (jobs=8 beat jobs=4 by ~9%, jobs=16
    # regressed). An earlier `cpu // 2` here gave two lanes on a four-core runner
    # and was 1.76x slower than this for eight runs. A sandbox is ~1 ms, so a lane
    # is nearly free.
    lanes = workers or min(len(table) + len(shards), usable_cpus() * 2, _LANES, _affordable(memory))

    results: list[Result] = []
    red = False
    with _sandboxes(lanes) as available, ThreadPoolExecutor(max_workers=lanes) as pool:
        checking = (
            [pool.submit(_borrow, available, shard.split(), timeout, memory) for shard in shards]
            if baseline
            else []
        )
        futures = [
            pool.submit(_attempt, mutation, available, failfast, timeout, memory)
            for mutation in table
        ]
        for mutation, future in zip(table, futures, strict=True):
            verdict = future.result()
            results.append(Result(mutation, verdict))
            print(f"  {_HEADLINE[verdict.outcome]:9} {mutation.label}")
            if verdict.answered:
                continue
            if strict:
                # Cancel first, or the `with` block's `shutdown(wait=True)` runs
                # every remaining row to completion and discards its verdict
                # unprinted -- so "stopping" would cost the whole table it was
                # meant to save. Only the rows not yet started can go; the
                # `lanes` already in flight are paid for either way.
                for pending in futures:
                    pending.cancel()
                raise SystemExit(
                    f"{mutation.label}: this mutation broke collection rather than being "
                    f"noticed, so neither 'caught' nor 'SURVIVED' would mean anything "
                    f"about {mutation.tests}. {verdict.detail}"
                )
            # Not indented under the row by accident: an unanswered row must not
            # be skimmable as one of the two real verdicts.
            print(f"           -- {verdict.detail}")
        # After the rows, not before, so a red baseline never costs the reader the
        # results they were waiting for -- they are simply told to disbelieve them.
        for future in checking:
            baseline_verdict = future.result()
            if baseline_verdict.outcome != "survived":
                # `survived` is the untouched suite passing, which is the one
                # place the mutation vocabulary reads backwards. A shard that
                # broke or timed out is red too, and its reason is the only clue
                # to why -- the old wording asserted a failure that may not have
                # happened.
                print(
                    f"  BASELINE NOT GREEN ({baseline_verdict.outcome}) -- the suite does not "
                    f"pass untouched, so nothing above means anything: {baseline_verdict.detail}"
                )
                red = True
                break

    if not red and summarise:
        _summarise(results)
    report = Report(results, red)
    _RUNS.append(report)
    return report


def _summarise(results: Sequence[Result]) -> None:
    """The part a pull request quotes when the news is bad."""
    survivors = [result for result in results if result.verdict.outcome == "survived"]
    unanswered = [result for result in results if not result.verdict.answered]
    if survivors:
        print(f"\n{len(survivors)} survived. A test that cannot see the fix removed is decoration:")
        for result in survivors:
            print(f"  - {result.mutation.label} ({result.mutation.tests})")
        print("Suspect the fixture before the mutation -- see CLAUDE.md rule 3.")
    if unanswered:
        # Counted separately and never as survivors: these rows asked nothing, and
        # rolling them into either verdict is the error this module exists to
        # avoid, one level up.
        print(
            f"\n{len(unanswered)} asked nothing, so the table is that much smaller than it looks:"
        )
        for result in unanswered:
            print(f"  - {result.mutation.label}: {result.verdict.detail}")


def verify(mutations: Iterable[Mutation], baseline: bool = True, workers: int | None = None) -> int:
    """`run`, reduced to the number of survivors. The shape spec files call.

    Strict, because a spec file is hand-written: a row that cannot be answered is
    a mistake in the table, and stopping is what gets it fixed.
    """
    report = run(mutations, baseline, workers)
    if report.baseline_red:
        return len(report.results)
    return sum(1 for result in report.results if result.verdict.outcome == "survived")


#: What a row runs when selection could not name a target: everything. Empty
#: rather than `"tests"`, because a package name is not discovery -- `unittest`
#: imports the package, finds no tests in it, and reports a green run of nothing.
#: Not a fallback of convenience either: see `mutants.targets_for`, and the row
#: says out loud when it happens.
WHOLE_SUITE = ""


def confirm(report: Report, workers: int | None, timeout: float, memory: int) -> Report:
    """Re-run every survivor against the whole suite, and correct the ones caught.

    This is what makes per-file test selection a *speed* decision rather than a
    correctness one. A survivor reported from a narrow target may simply have
    been run against the wrong tests, and that error points the expensive way:
    it sends the author to rewrite a test that was never weak, which is the
    failure rule 3 exists to prevent, inverted.

    Only survivors, because they are the minority and the only ones whose answer
    can be wrong in that direction -- a `caught` row was caught by a real test,
    and no wider suite makes that less true.
    """
    # Positions, not labels. Two generated rows share a label whenever they touch
    # the same line with the same operator -- `generate` dedupes on `(span, new)`,
    # and `mutable()` alone has two `.startswith(...)` calls on one line. Keying
    # the corrections by label wrote one row's `caught` onto every row spelled the
    # same way, so a genuine survivor was reported as caught, naming a test that
    # had never seen it. On this branch's own diff, 23 labels are duplicated.
    #
    # `run` appends in table order, and with `strict=False` there is no early
    # exit, so `again.results` is positionally aligned with `widened`.
    survivors = [
        (where, result)
        for where, result in enumerate(report.results)
        if result.verdict.outcome == "survived"
    ]
    if not survivors:
        return report
    print(f"\nconfirming {len(survivors)} survivor(s) against the whole suite...")
    widened = [result.mutation._replace(tests=WHOLE_SUITE) for _, result in survivors]
    again = run(
        widened,
        baseline=False,
        workers=workers,
        strict=False,
        timeout=timeout,
        memory=memory,
        summarise=False,
    )

    # Only `caught` corrects a survivor. A confirmation that broke or timed out
    # answered nothing, and folding it in would print "caught by a test the
    # selection had not run" about a run in which no test ran at all -- the
    # exact false `caught` this module was rewritten to make impossible, one
    # level up. It happened here on the first end-to-end run.
    corrected = {
        where: found.verdict
        for (where, _), found in zip(survivors, again.results, strict=True)
        if found.verdict.outcome == "caught"
    }
    unsure = sum(1 for found in again.results if not found.verdict.answered)
    if unsure:
        print(f"{unsure} confirmation(s) could not be answered; those rows stand as reported.")
    if not corrected:
        return report
    print(f"{len(corrected)} of them were caught by a test the selection had not run.")
    return Report(
        [
            Result(result.mutation, corrected.get(where, result.verdict))
            for where, result in enumerate(report.results)
        ],
        report.baseline_red,
    )


def generated(args: argparse.Namespace) -> list[Mutation]:
    """The table the diff implies, printed about before any of it runs."""
    root = Path.cwd()
    touched = mutants.changed_lines(args.base, root)
    if args.only:
        touched = {
            path: lines
            for path, lines in touched.items()
            if any(wanted in path for wanted in args.only)
        }
    if not touched:
        raise SystemExit(
            f"nothing mutable changed against {args.base}. Only woswoar/**.py and "
            f"tools/**.py are generated from; a change to tests/ is not a fix to test."
        )

    index = mutants.importers(root)
    table: list[Mutation] = []
    for path in sorted(touched):
        tests = mutants.targets_for(path, root, index) or WHOLE_SUITE
        table.extend(
            mutants.generate(
                (root / path).read_text(encoding="utf-8"),
                path,
                touched[path],
                tests=tests,
                operators=args.operator or None,
            )
        )
        if tests == WHOLE_SUITE:
            print(f"note: nothing imports {path}, so its rows run the whole suite.")

    print(
        f"{len(touched)} file(s), {sum(len(lines) for lines in touched.values())} changed "
        f"lines -> {len(table)} mutants"
    )
    kept, dropped = mutants.cap(table, args.limit)
    if dropped:
        # Said out loud, because a silent cap reads as "everything was covered"
        # and the count would look right either way -- CLAUDE.md is explicit.
        share: dict[str, int] = {}
        for row in dropped:
            share[row.path] = share.get(row.path, 0) + 1
        listed = ", ".join(f"{path} {count}" for path, count in sorted(share.items()))
        print(f"--limit {args.limit}: {len(dropped)} not run ({listed}).")
        print("Counts below are out of what ran, not out of what the diff implies.")
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.mutate",
        description="Run a table of mutations -- from a spec file, or from a diff.",
    )
    parser.add_argument(
        "script", nargs="?", help="a Python file defining MUTATIONS: list[Mutation]"
    )
    parser.add_argument(
        "--base", help="generate mutants for every line changed against this revision"
    )
    parser.add_argument(
        "--list", action="store_true", help="print the generated table and run nothing"
    )
    parser.add_argument(
        "--only", action="append", default=[], metavar="PATH", help="restrict to matching paths"
    )
    parser.add_argument(
        "--operator", action="append", default=[], metavar="NAME", help="use only this operator"
    )
    parser.add_argument("--limit", type=int, default=200, help="cap the table (0 for no cap)")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per mutation")
    parser.add_argument(
        "--memory",
        type=int,
        default=MEMORY,
        help="bytes of address space one mutation may occupy",
    )
    parser.add_argument("--workers", type=int, help="lanes to run in parallel")
    parser.add_argument("--no-baseline", action="store_true", help="skip the untouched-suite check")
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="do not re-run survivors against the whole suite",
    )
    args = parser.parse_args(argv)

    if bool(args.script) == bool(args.base):
        parser.error("give a spec file or --base, not both and not neither")

    if args.base:
        table = generated(args)
        if args.list:
            for row in table:
                print(f"  {row.operator:16} {row.label}")
            return 0
        report = run(
            table,
            baseline=not args.no_baseline,
            workers=args.workers,
            # A generated row that breaks collection is not a mistake anyone
            # made; discarding two hundred paid-for answers because of it would
            # be. The row is reported and the run goes on.
            strict=False,
            summarise=False,
            # Worth having here and not for a hand table: `caught` is the
            # expected outcome for most generated rows, and without this each
            # one runs the rest of its target after the answer is known. An
            # average, not a bound -- `unittest` runs classes alphabetically, so
            # a mutant caught only by the last of them still pays for nearly all.
            failfast=True,
            timeout=args.timeout,
            memory=args.memory,
        )
        if not args.no_confirm:
            report = confirm(report, args.workers, args.timeout, args.memory)
        else:
            print("\n--no-confirm: survivors below were not re-run against the whole suite,")
            print("so one may simply have been run against tests that cannot see it.")
        _summarise(report.results)
        return 0 if report.clean else 1

    already = len(_RUNS)
    namespace = runpy.run_path(args.script)
    # Every table the script ran, not just the last: a spec calling `verify`
    # twice would otherwise have its exit status decided by the second, so a
    # first table full of survivors followed by a clean one exits zero. That is
    # #213's own symptom -- a run reported as the opposite of what it was --
    # reintroduced by the fix for it.
    mine = _RUNS[already:]
    ran_itself = bool(mine)
    mutations = namespace.get("MUTATIONS")

    if mutations and ran_itself:
        # Both shapes in one file. Re-running the table would cost the same
        # minutes for the same answer and print it twice with nothing to say
        # which pass a reader is looking at, so take the one that already ran.
        print(
            f"{args.script} defines MUTATIONS *and* calls verify(); the results above "
            f"are the run it did itself. Delete one of the two.",
            file=sys.stderr,
        )
    if ran_itself:
        return 0 if all(report.clean for report in mine) else 1
    if mutations:
        return 0 if run(mutations).clean else 1
    raise SystemExit(
        f"{args.script} defines no MUTATIONS and never called verify(), so there was "
        f"nothing to run. The shape this takes is `MUTATIONS = [Mutation(...), ...]` "
        f"at module level."
    )


if __name__ == "__main__":
    # Deliberately not `main()`. `python -m tools.mutate` runs this file as
    # `__main__`, and the spec it then executes does `from tools.mutate import
    # verify` -- which imports the file a *second* time, as a different module
    # object with its own `_RUNS`. The local `main` would look at a list the
    # spec's `verify` never appended to, and report a script that had just run a
    # green table as one that defined nothing. Which is #213 again, by a
    # different route.
    from tools.mutate import main as _main

    raise SystemExit(_main())
