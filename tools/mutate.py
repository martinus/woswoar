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
  to find them. `tools/verdict.py` now classifies where the result objects still are, and
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
  runner. That last clause used to end "so there is no nested pool to
  oversubscribe", which is true of every file in ``woswoar/`` and false of three
  in ``tools/``: `tests/test_mutate.py` starts this harness twenty-two times and
  `tests/test_run_tests.py` starts the sharded runner, so a lane mutating those
  hosts a pool of its own -- sixteen lanes each hosting sixteen, which is how
  4,340 processes came to be alive at once and how a 63 GiB machine was
  OOM-killed three times (#232). `_share`, `_BUDGET` and `_Lanes` are what that
  cost; the sentence is left here rather than deleted because a comment that
  reassures is worse than none. Measured as
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
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
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

#: Rows a generated table runs before it stops, when nobody said otherwise.
#: Sized for a diff; `--all` resets it, because a cap meant to keep one
#: change's table readable turns a whole-package sweep into 4% of itself.
LIMIT = 200

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
#:
#: What a lane is actually given is `_share`'s answer rather than this, because
#: sixteen lanes promised four gigabytes each is 64 GiB of promises on a 63 GiB
#: machine. This stays the ceiling a lane may *ask* for; `_FLOOR` is the point
#: below which the answer stops being safe to give.
MEMORY = 4 << 30

#: The smallest ceiling `_share` hands a lane before it takes a *lane* away
#: instead. `MEMORY` is what one runaway may reach; this is what an honest run
#: needs, and keeping them as two numbers is the whole fix for #232. Dividing a
#: small machine's budget by the ceiling was the over-restriction #227 removed
#: (a 16 GiB laptop dropped to two lanes); multiplying the lane count back up
#: without lowering the ceiling was the over-commitment that replaced it, at 16
#: lanes x 4 GiB = 64 GiB on a 63 GiB machine. One number cannot answer both.
#:
#: Measured, and by running the thing rather than reasoning about it: one
#: whole-suite probe peaks at 838 MiB of address space here, and survives a cap
#: of 1 GiB but not 768 MiB. Twice the smallest cap that works, so the margin is
#: a stated factor rather than a number that happens to be round.
_FLOOR = 2 << 30

#: How a lane tells a harness it starts itself what it may spend.
#: `tests/test_mutate.py` starts this module 22 times and `tests/test_run_tests.py`
#: starts the sharded runner, so a lane mutating `tools/` hosts a second harness --
#: which, reading the host's memory as its own, sizes itself for a machine it does
#: not have. Sixteen lanes each hosting sixteen is how 4,340 processes came to be
#: alive at once. Carried in the environment because that is what survives
#: ``python -c``, and read back by `_visible_memory` as one limit among the
#: cgroup's.
_BUDGET = "WOSWOAR_MUTATE_BUDGET"

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


def _signalled(returncode: int) -> str:
    """Why a probe that wrote nothing died, when nothing else can say.

    A negative return code is a signal in `subprocess`'s spelling. `SIGKILL` is
    the interesting one: it is what the host OOM killer sends, so it is what a
    lane looks like on a platform where `verdict.cap` is not enforced -- the
    macOS case the cap's own docstring admits to. Naming it is the difference
    between a row that reports nothing and a row that says the machine, not the
    mutation, ended the run.
    """
    if returncode >= 0:
        return f"the probe exited {returncode} without writing a report"
    name = signal.Signals(-returncode).name if -returncode in set(signal.Signals) else "?"
    killed = " -- the host ran out of memory, and this lane was chosen" if name == "SIGKILL" else ""
    return f"the probe was killed by {name}{killed}"


#: How often `_Lanes` counts what the lanes are holding. Seconds rather than
#: minutes: the storm that prompted this reached 26 GB inside a single 20-second
#: gap between two `free` readings. One pass over `/proc` and no fork, so the
#: sample is cheap enough to take at this rate for the length of a sweep.
_SAMPLE = 1.0


class Process(NamedTuple):
    """One row of the process table, in the three fields a lane cares about."""

    parent: int
    group: int
    resident: int


def _processes() -> dict[int, Process]:
    """Every process this user can see, by pid.

    Resident rather than address space, because resident is what the OOM killer
    counts and what a fork storm actually spends. The one that took this machine
    down was 4,340 processes holding 26 GB of RSS between them and 961 GB of
    address space, and only one of those two numbers is a reason to intervene.

    Two readers rather than one, and `/proc` first: reading it forks nothing,
    while `ps` is a fork that can fail at exactly the moment its answer matters.
    macOS has no `/proc`, so there it is `ps` or nothing -- and there it is also
    the *only* one of this module's two guards that works at all, since that
    kernel ignores the `RLIMIT_AS` `verdict.cap` asks for. Both are exercised by
    the tests on every platform for that reason: the fallback must not be
    discovered to be broken on the machine that has nothing else.
    """
    return _from_proc() if Path("/proc/self/stat").exists() else _from_ps()


def _from_proc() -> dict[int, Process]:
    """`_processes` where there is a `/proc`. Reads, never forks."""
    table: dict[int, Process] = {}
    page = os.sysconf("SC_PAGE_SIZE")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            said = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Exited between the listing and the read, or belongs to another
            # user. Both are ordinary, and both are why this is a loop with a
            # `try` in it rather than a comprehension.
            continue
        # Everything after the comm field, which is the one that may itself
        # contain spaces and brackets -- so the split starts past its closing
        # one rather than at the beginning of the line. Counting from `pid`,
        # `ppid` is the fourth field, `pgrp` the fifth and `rss` the
        # twenty-fourth, which is why the indices below are those minus three.
        fields = said.rpartition(") ")[2].split()
        if len(fields) < 22:
            continue
        with suppress(ValueError):
            table[int(entry.name)] = Process(
                parent=int(fields[1]), group=int(fields[2]), resident=int(fields[21]) * page
            )
    return table


def _from_ps() -> dict[int, Process]:
    """`_processes` where there is no `/proc`, which is macOS."""
    table: dict[int, Process] = {}
    listed = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,pgid=,rss="], capture_output=True, text=True, check=False
    )
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 4 and all(field.isdigit() for field in fields):
            pid, parent, group, resident = (int(field) for field in fields)
            # Kibibytes, which POSIX does not promise and both platforms do.
            table[pid] = Process(parent=parent, group=group, resident=resident * 1024)
    return table


def _lane(leader: int, table: dict[int, Process]) -> set[int]:
    """Every process belonging to the lane `leader` started.

    Two answers unioned, because neither is enough on its own and the gap
    between them is #234:

    - everything in `leader`'s **process group**, which is what `start_new_session`
      buys and covers the `git`, `age` and `bash` a suite forks;
    - every **descendant** of `leader`, which is what catches a grandchild that
      called `setsid` and thereby left that group. A nested `_run` does exactly
      that, and one was found alive eleven minutes into a sweep whose per-row
      bound is 300 seconds, reparented to init and counted by nobody.

    A descendant whose parent has already died is in neither set: init has
    adopted it and the link is gone. That is why callers enumerate *before*
    killing rather than after -- while the lane is alive, the tree is still
    connected.
    """
    kids: dict[int, list[int]] = {}
    for pid, row in table.items():
        kids.setdefault(row.parent, []).append(pid)
    found = {pid for pid, row in table.items() if row.group == leader}
    stack = [leader]
    while stack:
        pid = stack.pop()
        if pid in found and pid != leader:
            continue
        found.add(pid)
        stack.extend(kids.get(pid, ()))
    return found & set(table)


def _end_lane(leader: int, members: Iterable[int]) -> None:
    """Kill the group, then everything that left it.

    The group first because it is one syscall for most of the lane, and the
    stragglers by pid because they are exactly the processes `killpg` cannot
    reach. `members` is enumerated by the caller beforehand: after the first
    kill the tree is no longer connected, so a walk done here would find less
    than a walk done a moment earlier.
    """
    with suppress(OSError):
        os.killpg(leader, signal.SIGKILL)
    for pid in members:
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)


class _Lanes:
    """Kills a lane whose process *tree* outgrows its share, which no rlimit can.

    `verdict.cap` bounds one process's address space, and that is the wrong
    shape for the failure that actually took this machine down. From the kernel
    log of the last one: 4,340 python processes alive at once, six megabytes of
    resident memory each, 26 GB between them -- and not one of them within two
    orders of magnitude of its own 4 GiB ceiling. Nothing ran away. There were
    simply thousands of them, because a lane mutating `tools/` hosts a harness
    that starts lanes of its own.

    A per-process limit cannot see that, and cannot be made to: the mutation
    under test is what decides how many processes the lane starts, so a limit it
    inherits is beside the point. `_BUDGET` shrinks an honest nested harness;
    this is what answers a mutant that ignores it.

    What counts as the lane is `_lane`: its process group, plus any descendant
    that left that group by starting a session of its own. The group alone was
    the first shape and it left a hole (#234) -- a nested `_run` gives its own
    probes their own sessions, so those escaped both the count and the kill, and
    one was found alive eleven minutes into a sweep whose per-row bound is 300
    seconds. Counting the union is also what makes an escaped probe's memory
    the lane's problem rather than nobody's.

    Sampling rather than a cgroup, for the reasons `verdict.cap` gives for
    `RLIMIT_AS` over `systemd-run --scope -p MemoryMax=`: no root, no delegated
    controller, nothing to configure, and it exists on macOS. What sampling buys
    over both is that it needs no cooperation from the code under test.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: What each watched group may hold.
        self._ceilings: dict[int, int] = {}
        #: What a group was holding when this killed it, until `release` says it.
        self._killed: dict[int, int] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def watch(self, group: int, ceiling: int) -> None:
        """Count `group` against `ceiling` from now on. ``0`` is no cap."""
        if ceiling <= 0:
            return
        with self._lock:
            self._ceilings[group] = ceiling
            if self._thread is None:
                # One sampler for every lane, started with the first and stopped
                # with the last. Per-lane threads would each walk the whole
                # process table, and the table is longest exactly when a storm
                # makes the walk expensive.
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._sample, args=(self._stop,), name="mutate-lanes", daemon=True
                )
                self._thread.start()

    def release(self, group: int) -> int:
        """Stop counting, and say what it held if this is what killed it."""
        with self._lock:
            self._ceilings.pop(group, None)
            held = self._killed.pop(group, 0)
            if not self._ceilings and self._thread is not None:
                self._stop.set()
                self._thread = None
        return held

    def _sample(self, stop: threading.Event) -> None:
        """Every `_SAMPLE` seconds until the last lane is released."""
        while not stop.wait(_SAMPLE):
            with self._lock:
                watched = dict(self._ceilings)
            if not watched:
                continue
            table = _processes()
            for leader, ceiling in watched.items():
                members = _lane(leader, table)
                held = sum(table[pid].resident for pid in members)
                if held <= ceiling:
                    continue
                with self._lock:
                    # Re-checked under the lock: `release` may have run while
                    # the process table was being read, and a kill after that
                    # names ids the kernel is free to have reused.
                    if leader not in self._ceilings:
                        continue
                    self._killed[leader] = held
                _end_lane(leader, members)


#: One per process, because `_run` is what registers and it has no run-wide
#: object to hang this on -- `run` is a function, and a lane borrowed by
#: `verify` or by a test goes through the same door.
_WATCHED = _Lanes()


def _end(probe: subprocess.Popen[bytes]) -> None:
    """End a lane: the whole session, not just the process this holds a handle to.

    `subprocess.run(timeout=...)` kills the child and returns, which for a suite
    that forks `git`, `age`, `bash` and `python -m woswoar` leaves the
    grandchildren running -- reparented to init, holding whatever they held, and
    invisible to the run that started them. A sweep that times out often enough
    accumulates them until the machine is gone, which is the slow half of #232;
    the fast half is `_Lanes`.

    The walk happens *before* the first kill, which is #234: a grandchild that
    started a session of its own is outside the group, so `killpg` never reaches
    it -- and once its parent is dead it is no longer a descendant either, so a
    walk done afterwards would not find it. Between those two moments is the
    only time anything can see it.
    """
    _end_lane(probe.pid, _lane(probe.pid, _processes()))
    with suppress(OSError):
        probe.kill()  # if the session is somehow gone, at least reap this one
    with suppress(subprocess.TimeoutExpired):
        probe.wait(timeout=_SAMPLE * 5)


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
    while the case it named went unasked. `tools/verdict.py` is the answer: classify where
    the result objects still exist.

    A timeout is its own outcome rather than an exception. A generated mutant can
    turn a loop bound into one that never fires, and with no limit here that
    holds a lane for the rest of the run.

    Three limits, and each answers a failure the other two cannot see. `timeout`
    is "this mutation never finishes"; `memory`, through `verdict.cap`, is "this
    process never stops allocating"; and `_Lanes`, through the session started
    here, is "this mutation spawns processes" -- which is neither of the others
    and is the one that reached the OOM killer.
    """
    _clear_bytecode(root)
    # Both files land outside the sandbox on purpose: the copy is what the
    # mutation edits, and a report written into it is one `open()` away from
    # being the mutation's to write.
    with tempfile.TemporaryDirectory(prefix="woswoar-verdict-") as box:
        report = Path(box) / "verdict.json"
        noise = Path(box) / "stderr.txt"
        with noise.open("w", encoding="utf-8") as spill:
            probe = subprocess.Popen(
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
                # `-B` covers this process; `PYTHONDONTWRITEBYTECODE` covers the
                # real `age`, `git` and `bash` the suite spawns, which would
                # otherwise leave a `.pyc` in a sandbox that a later mutation
                # reuses. `_BUDGET` covers the *harness* the suite may spawn: a
                # row mutating `tools/` runs tests that start this module again,
                # and without it the inner one sizes itself for the whole host.
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    _BUDGET: str(memory),
                },
                # A file rather than a pipe, and this is not a style choice. The
                # suite's `python -m woswoar` grandchildren inherit the write
                # end, so anything that drains a pipe before reaping -- which is
                # what `subprocess.run` does on a timeout -- waits for *them*
                # rather than for the probe. With a file there is nothing to
                # drain, so `_end` can kill the session and be done.
                stdout=subprocess.DEVNULL,
                stderr=spill,
                # Its own session, so this pid is also the group id and every
                # process the suite forks is inside it -- which is what makes
                # both `_Lanes`' count and `_end`'s kill cover the tree rather
                # than the one process. `start_new_session` rather than a
                # `preexec_fn` doing the same thing: `run` drives its lanes from
                # threads, and this one is done in C between fork and exec.
                start_new_session=True,
            )
        _WATCHED.watch(probe.pid, memory)
        try:
            probe.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _end(probe)
            return Verdict("timeout", f"no answer within {timeout:g}s")
        finally:
            # In a `finally` so that no path leaves a group registered: a
            # `KeyboardInterrupt` here would otherwise leave the sampler holding
            # a pid the kernel is free to hand to something else.
            held = _WATCHED.release(probe.pid)
        if held:
            # Before the report is read, not after. A killed lane may well have
            # written one -- the kill lands on whichever process is running, and
            # the probe's own `except BaseException` may have got there first --
            # and reading it would report a verdict about a run that was
            # stopped, which is the "broke counted as an answer" failure this
            # module exists to prevent.
            return Verdict(
                "broke",
                f"this lane's processes held {held >> 20} MiB between them, over the "
                f"{memory >> 20} MiB share it was given, so the whole session was killed",
            )

        try:
            written = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The probe was killed before it could write anything at all. Rare
            # and always worth the tail -- but a process killed by a signal
            # writes no stderr either, and that row used to print with no reason
            # at all. It is exactly the case a host OOM-kill produces, which is
            # the failure this file now has a limit for, so it must not be the
            # one that says nothing.
            return Verdict("broke", _tail(noise) or _signalled(probe.returncode))
        if not written["loaded"]:
            # The recorded traceback first. `verdict.main` writes it deliberately
            # and `_tail` is whatever happened to reach stderr, so preferring the
            # tail let a stray import-time warning outrank the reason the probe
            # took the trouble to record.
            return Verdict("broke", str(written.get("why", "")).strip() or _tail(noise))

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


#: What one lane is measured to occupy, as opposed to what `MEMORY` lets it
#: reach. One whole-suite run peaks at 317 MB resident; a gibibyte is three
#: times that, and resident is the number the OOM killer counts.
_LANE = 1 << 30


def _visible_memory() -> int:
    """Memory this process may actually use, container limits included.

    Not `SC_PHYS_PAGES` alone, which reports the host's and ignores a cgroup --
    so in a 2 GiB container on a 62 GiB host it answers 62 and the container is
    OOM-killed with every per-lane cap respected. `tools/sandbox.py`'s
    `usable_cpus` makes exactly this argument for CPUs; this is the same mistake
    with the same shape, one resource over.

    Left here rather than beside `usable_cpus` because it has one caller.
    `usable_cpus` moved into `sandbox` when it got a second, and that is the
    threshold this repository uses.
    """
    limits = []
    for where in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            said = Path(where).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        # cgroup v2 writes `max` for "no limit"; v1 writes a number so large it
        # means the same thing, and comparing against the host total is the only
        # way to tell that from a real limit.
        if said.isdigit():
            limits.append(int(said))
    # A lane's share, when this harness is itself running inside one. Same
    # question as the cgroup file above -- "how much may *this* process use" --
    # and the same answer shape, which is why it is a limit here rather than a
    # branch somewhere in `run`.
    inherited = os.environ.get(_BUDGET, "")
    if inherited.isdigit() and int(inherited) > 0:
        limits.append(int(inherited))
    with suppress(AttributeError, OSError, ValueError):  # not POSIX
        limits.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    return min(limits) if limits else _LANES * _LANE


def _budget() -> int:
    """What this run may spend in total.

    Half of visible memory, because the other half belongs to whoever is using
    the machine. Mutation testing is a background chore and has no claim on the
    whole box.
    """
    return _visible_memory() // 2


def _affordable() -> int:
    """How many lanes fit in memory, which is a different question from cores.

    `verdict.cap` bounds one lane; it does not bound their product, and the
    product is what the host feels. Sixteen lanes at 4 GiB is 64 GiB on a 62 GiB
    machine, so a table can still drive it into swap with every individual cap
    respected -- and a *generated* table is the shape that does it, because it
    walks a file in order and its runaway rows are therefore adjacent. When this
    crash was diagnosed, three of four lanes were running away simultaneously,
    all on the same source line.

    Divided by `_LANE` -- what a lane is measured to *use* -- and not by
    `MEMORY`, which is the ceiling on what one may reach before being killed.
    Dividing by the ceiling was the first shape and it assumed every lane is
    simultaneously pathological: an ordinary 16 GiB laptop dropped from sixteen
    lanes to two, and a 7 GiB CI runner to one, to bound a case that the ceiling
    already truncates within seconds. The ceiling stops a runaway; this number
    decides how many honest lanes fit.

    It is half the answer. `_share` is the other half, and the reason this one
    is no longer wrong on its own: what the machine feels is lanes *times*
    ceiling, and nothing here can see the ceiling.
    """
    return max(1, _budget() // _LANE)


class Share(NamedTuple):
    """How many lanes run at once, and what each one may occupy."""

    lanes: int
    memory: int


def _share(wanted: int, memory: int, pinned: bool = False) -> Share:
    """Pick both together, so that their product is something the machine has.

    `verdict.cap` bounds one lane and `_affordable` counts lanes, and for one
    release each was defensible while the pair was not: sixteen lanes at a 4 GiB
    ceiling is 64 GiB on a 63 GiB machine, so a table could drive the host into
    the OOM killer with every individual limit respected. That is #232, and it
    happened three times.

    The order of concessions is the argument:

    - lanes first, from `_affordable`, because a lane's *measured* use is the
      honest cost of running one and CPU is what the wall clock is made of;
    - then the ceiling, lowered to the share that many lanes leaves, because a
      ceiling is headroom for a pathological row rather than something an honest
      one spends;
    - and only when that share would fall under `_FLOOR` -- below which honest
      runs start being killed -- are lanes given up instead.

    So a big machine keeps its sixteen lanes and simply stops promising each of
    them four gigabytes it cannot deliver, and a small one loses lanes rather
    than reliability.

    ``pinned`` is an explicit `--workers`, and it keeps the lane count it asked
    for. The ceiling still shrinks around it, which is the part worth having; it
    is the *count* that a caller has reasons to fix that this cannot see.
    `tests.test_mutate.TestItRunsThemInParallel` is the case that made this
    concrete -- it pins four lanes to assert that mutations overlap at all, and
    on a machine with too little memory to afford four it would otherwise assert
    the machine rather than the mechanism, which is what its own comment says it
    is pinned to avoid. The bound that still holds for a pinned run is `_Lanes`,
    which measures rather than predicts.

    ``memory <= 0`` is `--memory 0`, "no cap", and it passes straight through.
    There is no product to bound once one factor is infinite, and quietly
    imposing one would be the flag lying.
    """
    if memory <= 0:
        return Share(max(1, wanted), memory)
    budget = _budget()
    # An explicit `--memory` under the floor is the caller's call, not a mistake
    # to correct: they may be reproducing a small machine on purpose. It still
    # sets how many lanes fit.
    floor = min(memory, _FLOOR)
    lanes = max(1, wanted if pinned else min(wanted, _affordable(), budget // floor))
    return Share(lanes, min(memory, max(floor, budget // lanes)))


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
    asked = memory
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
    wanted = workers or min(len(table) + len(shards), usable_cpus() * 2, _LANES)
    lanes, memory = _share(wanted, memory, pinned=workers is not None)
    if lanes != wanted or memory != asked:
        # Said out loud, for the reason `--limit` says what it dropped: a run
        # that quietly took fewer lanes than the machine has cores reads as a
        # slow tool rather than as a bounded one, and a ceiling lowered in
        # silence is how the row that reports `ran out of memory` becomes
        # inexplicable.
        print(
            f"{lanes} lane(s) at {memory >> 20} MiB each, from {_budget() >> 20} MiB "
            f"of usable memory -- see tools.mutate._share."
        )

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
        # As the narrow pass does, and for the same reason: the first test that
        # notices settles the row, and this pass runs the *whole* suite per
        # survivor -- the one place where not stopping costs the most.
        failfast=True,
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
    touched = mutants.every_line(root) if args.all else mutants.changed_lines(args.base, root)
    if args.only:
        touched = {
            path: lines
            for path, lines in touched.items()
            if any(wanted in path for wanted in args.only)
        }
    if not touched:
        raise SystemExit(
            "no mutable files at all under woswoar/ or tools/."
            if args.all
            else f"nothing mutable changed against {args.base}. Only woswoar/**.py and "
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
                skip=args.skip_operator or None,
            )
        )
        if tests == WHOLE_SUITE:
            print(f"note: nothing imports {path}, so its rows run the whole suite.")

    counted = sum(len(lines) for lines in touched.values())
    print(
        f"{len(touched)} file(s), {counted} {'' if args.all else 'changed '}lines "
        f"-> {len(table)} mutants"
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


def _bytes(said: str) -> int:
    """A byte count for `--memory`, refusing the two values that fail silently.

    `0` means no cap, spelled the way `--limit 0` next to it already means it.
    A negative number is refused rather than accepted: `RLIM_INFINITY` is `-1`
    on Linux, so `--memory -1` used to read as "no limit" through a flag whose
    whole purpose is to impose one -- a typo that removes the guard and says
    nothing.
    """
    try:
        value = int(said)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a byte count: {said}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(
            f"negative memory limit: {said}. Use 0 for no cap; -1 is not infinity here."
        )
    return value


def _persist(report: Report, where: Path) -> None:
    """The run's answers, as `tools/reached.py` reads them.

    Written because a survivor list is not the end of the analysis. Crossing
    these outcomes with a coverage map is what separates "no test reaches this"
    from "a test reaches it and asserts nothing", and that cannot be done from
    the printed table -- which is prose, in table order, with no line numbers.

    `line` is parsed back out of the label rather than carried on `Mutation`,
    because a hand-written row has no position at all, and the label is the one
    place both kinds already agree on how to say where they are.
    """
    rows = []
    for result in report.results:
        found = re.match(r"\S+?:(\d+) ", result.mutation.label)
        rows.append(
            {
                "label": result.mutation.label,
                "path": result.mutation.path,
                "line": int(found.group(1)) if found else None,
                "tests": result.mutation.tests,
                "operator": result.mutation.operator,
                "outcome": result.verdict.outcome,
                "detail": result.verdict.detail,
                # Enough to rebuild the row, not just to read about it. Without
                # `old`/`new` a resumed sweep could skip a file but never
                # re-confirm its survivors, and `CONTRIBUTING.md` promises every
                # survivor is re-run against the whole suite before it is
                # reported. A resume must not quietly drop that guarantee.
                "old": result.mutation.old,
                "new": result.mutation.new,
                "span": list(result.mutation.span) if result.mutation.span else None,
            }
        )
    where.write_text(
        json.dumps({"baseline_red": report.baseline_red, "results": rows}, indent=1),
        encoding="utf-8",
    )
    print(f"\nwrote {len(rows)} row(s) to {where}")


def _run_generated(rows: Sequence[Mutation], args: argparse.Namespace) -> Report:
    """One batch of generated rows. Both the whole table and `sweep` use this.

    `strict=False`: a generated row that breaks collection is not a mistake
    anyone made, and discarding two hundred paid-for answers because of it would
    be. The row is reported and the run goes on.

    `failfast=True`: worth having for a generated table and not for a hand
    table, because `caught` is the expected outcome for most generated rows and
    without it each one runs the rest of its target after the answer is known.
    An average, not a bound -- `unittest` runs classes alphabetically, so a
    mutant caught only by the last of them still pays for nearly all.
    """
    return run(
        rows,
        baseline=not args.no_baseline,
        workers=args.workers,
        strict=False,
        summarise=False,
        failfast=True,
        timeout=args.timeout,
        memory=args.memory,
    )


def _recorded(where: Path | None) -> list[Result]:
    """Every row a previous `--json` report holds, rebuilt.

    Rebuilt rather than merely counted, because `sweep` has to do three things
    with them and only one is "skip". They must stay in the file it rewrites --
    the first version returned labels only, so a resumed run persisted just the
    batches it had run and *deleted* the answers it had decided to skip, which
    is the recovery mechanism eating the thing it recovers. They must reach
    `confirm`, or a resumed sweep reports a survivor that was never re-run
    against the whole suite. And they must reach `_summarise` and the exit
    status, or a resume whose new batches are all caught exits 0 while recorded
    survivors go unmentioned.

    A half-written file resumes as nothing: re-running everything is the safe
    reading of a crash mid-write.
    """
    if where is None or not where.is_file():
        return []
    try:
        saved = json.loads(where.read_text(encoding="utf-8"))
        return [
            Result(
                Mutation(
                    row["label"],
                    row["path"],
                    row["old"],
                    row["new"],
                    row["tests"],
                    span=(row["span"][0], row["span"][1]) if row.get("span") else None,
                    operator=row.get("operator", ""),
                ),
                Verdict(row["outcome"], row.get("detail", "")),
            )
            for row in saved.get("results", [])
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def sweep(table: Sequence[Mutation], args: argparse.Namespace) -> Report:
    """Run a large table a file at a time, writing answers out as they land.

    One table of three thousand rows reports nothing until it ends, and the run
    it was written for took 151 minutes and was killed twice by an out-of-memory
    machine before the guard in #223 existed. A crash then cost the afternoon.
    Batched by file, a crash costs one file, and re-running with the same
    `--json` skips what is already recorded -- there is no separate flag.

    Confirmation is pooled to the end rather than run per batch, and that is not
    an optimisation detail: per batch, a file with a single survivor pays a whole
    suite run with fifteen lanes idle. Measured on the first sweep here,
    `credentials.py` spent 68 s on 13 mutants, almost all of it confirming one
    row.
    """
    collected = _recorded(args.json)
    # Keyed by file, not by row: `_persist` writes after a whole batch, so a
    # file is recorded entirely or not at all, and a label is not unique anyway
    # -- 7 are duplicated in the `--all` table, for the reason `confirm`'s
    # docstring gives.
    done = {result.mutation.path for result in collected}
    by_file: dict[str, list[Mutation]] = {}
    for row in table:
        by_file.setdefault(row.path, []).append(row)

    red = False
    order = sorted(by_file, key=lambda path: len(by_file[path]))
    for at, path in enumerate(order, start=1):
        if path in done:
            print(f"[{at}/{len(order)}] {path}: already recorded, skipping")
            continue
        rows = by_file[path]
        print(f"\n[{at}/{len(order)}] {path}: {len(rows)} mutant(s)")
        batch = _run_generated(rows, args)
        red |= batch.baseline_red
        collected.extend(batch.results)
        if args.json:
            _persist(Report(collected, red), args.json)
    return Report(collected, red)


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
    parser.add_argument(
        "--all",
        action="store_true",
        help="every line of every mutable file, rather than a diff",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="run a file at a time, writing --json as each lands (implied by --all)",
    )
    parser.add_argument(
        "--skip-operator",
        action="append",
        default=[],
        metavar="NAME",
        help="run every operator but this one",
    )
    parser.add_argument("--limit", type=int, default=LIMIT, help="cap the table (0 for no cap)")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per mutation")
    parser.add_argument(
        "--memory",
        type=_bytes,
        default=MEMORY,
        help="bytes of address space one mutation may occupy (0 for no cap)",
    )
    parser.add_argument("--workers", type=int, help="lanes to run in parallel")
    parser.add_argument("--no-baseline", action="store_true", help="skip the untouched-suite check")
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="do not re-run survivors against the whole suite",
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="PATH",
        help="write the outcomes here, for `python -m tools.reached`",
    )
    args = parser.parse_args(argv)

    if args.all and args.base:
        parser.error("--all is every line; --base is what changed. Not both.")
    if args.all:
        # Downstream only asks "generated or a spec file?", and `--all` is the
        # generated path with a wider net rather than a third kind of run.
        args.base = "--all"
        if args.limit == LIMIT:
            # The default cap is sized for a diff. Left alone it turned the
            # documented `--all` into 200 of 4451 rows -- and, because the cap
            # spreads across files, into batches of seven, so batching,
            # incremental `--json` and resume all did nothing on the one command
            # line anybody would type. An explicit `--limit` is still honoured.
            args.limit = 0
    if bool(args.script) == bool(args.base):
        parser.error("give a spec file, --base or --all")

    if args.base:
        table = generated(args)
        if args.list:
            for row in table:
                print(f"  {row.operator:16} {row.label}")
            return 0
        report = sweep(table, args) if args.all or args.batch else _run_generated(table, args)
        if not args.no_confirm:
            report = confirm(report, args.workers, args.timeout, args.memory)
        else:
            print("\n--no-confirm: survivors below were not re-run against the whole suite,")
            print("so one may simply have been run against tests that cannot see it.")
        _summarise(report.results)
        if args.json:
            _persist(report, args.json)
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
