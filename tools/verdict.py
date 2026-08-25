"""Which kind of failure a suite produced -- the question an exit status cannot answer.

`tools/mutate.py` needs to know whether a *test method noticed* the mutation, or
whether the run merely fell over. Both exit non-zero, and both leave a plausible
``Ran N`` behind, so the distinction has to be drawn where the result objects
still exist rather than reconstructed from what `unittest` printed. That is what
this file is.

**It is read, not imported.** `mutate` reads this source out of its *own* tree
and hands it to ``python -c`` in the sandbox. Two properties fall out, and both
are the point:

- the sandbox's copy of ``tools/`` is never consulted, so a mutation to this file
  cannot decide its own verdict -- which matters as soon as `tools/**.py` is
  itself something a generated table mutates;
- being a real module rather than a string constant, `ruff` and `mypy` see it.
  The previous shape was a 35-line string literal that no checker could reach,
  holding the one piece of genuinely new logic in the change.

``-c`` also puts the working directory on ``sys.path`` where a script path would
not, which is how the sandbox's own test modules are importable at all.

**Classification is by protocol, never by class name.** The obvious spelling --
"is this class defined under ``unittest.``?" -- was written first and was wrong
in the direction that matters. `unittest.case._SubTest` is a `TestCase` whose
module is ``unittest.case``, so an assertion failing inside ``with
self.subTest(...)`` was filed as "the suite broke" rather than "a test noticed",
and with a strict table that aborts the run. This repository uses `subTest` in
more than twenty places. So instead:

- ``addSubTest`` is handed the **owning** test, not the `_SubTest` carrier, so
  overriding it records the right name;
- a `setUpClass` or `setUpModule` failure arrives through ``addError`` as
  `unittest.suite._ErrorHolder`, which is deliberately *not* a `TestCase` -- so
  `isinstance` alone separates "a fixture died" from "a test failed", with no
  private name involved;
- a module that will not import is reported by `TestLoader.errors`, a public
  attribute, and the suite is then not run at all. Running it would surface the
  synthetic `unittest.loader._FailedTest` through ``addError`` -- and that one
  *is* a `TestCase`, so it would read as a test noticing.

Nothing here names a private symbol to make a decision. If a future `unittest`
moves `_FailedTest` or `_ErrorHolder`, `loader.errors` and the `isinstance` still
answer, and the failure mode is a name printed oddly rather than `broke`
silently becoming `caught`.
"""

from __future__ import annotations

import io
import json
import resource
import signal
import sys
import time
import traceback
import unittest
from types import TracebackType
from typing import Any

#: What `unittest` hands a result method for a failure, spelled exactly as
#: typeshed spells it -- `mypy --strict` checks these overrides for Liskov and a
#: near-miss here is an error, not a warning.
ExcInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]


def cap(limit: int) -> None:
    """Bound this process's address space, so a runaway mutant dies alone.

    A mutation does not have to loop forever to hold a lane -- it can loop
    forever *while appending*, and then the timeout is the wrong instrument
    because the machine is gone before it fires. Measured, on the mutant that
    prompted this: `line_starts` reduced to `at -= ...` never advances (a
    negative index wraps in Python rather than raising, so the `while` condition
    stays true), and three lanes reached 15.5 GB, 7.6 GB and 6.4 GB of resident
    memory 73 seconds in. The host OOM-killed the session twice before the 300 s
    timeout had any chance to speak.

    `RLIMIT_AS` rather than a cgroup, and the reasons are not interchangeable:

    - it is set *here*, in the child, so it needs no `preexec_fn`. `run` drives
      its lanes from a `ThreadPoolExecutor`, and `preexec_fn` is documented as
      unsafe in the presence of threads -- the one mechanism that must not
      itself be a source of rare, undebuggable failures;
    - `systemd-run --scope -p MemoryMax=` is Linux-only, and CI runs a macOS
      job. A guard that silently does not exist on one platform is worse than
      the guard being visible everywhere;
    - no root, no delegated cgroup, nothing to configure before the tool works.

    Two honest limits. These bound *address space* and the data segment, not
    resident memory, so the number is necessarily looser than the RSS it
    protects; and a platform may enforce neither, in which case this silently
    does nothing. That is not hypothetical -- macOS ignores `RLIMIT_AS`, and CI
    is what said so. `tests.test_mutate` therefore asks whether the limit is
    enforced *here* by trying it, rather than deciding from `sys.platform`, so
    the guarantee is tested wherever it actually holds and skipped where it does
    not. Neither weakens the Linux case, which is where the crash happened.

    `resource` is imported unguarded: it is Unix-only, and so is everything
    else here -- the product is a shell hook for bash and zsh, and CI runs
    Linux and macOS. A `try: import` around it only bought an unreachable
    branch that `mypy` correctly refused.

    The limit is inherited by the `git`, `age` and `bash` the suite really
    forks. That is intended -- they are equally capable of running away -- and
    it is why `mutate.MEMORY`, which supplies `limit`, is a measured margin over
    an observed whole-suite peak rather than a round number.

    **It bounds one process, and inheritance is not a total.** Each of those
    children gets its own allowance of this size rather than a share of one, so
    a mutation that spawns processes is unbounded here however small `limit` is:
    the crash that prompted `mutate._Lanes` was 4,340 of them holding 26 GB
    between them, none within two orders of magnitude of this ceiling. Nothing
    in this function can see that, which is why the guard against it is a
    sampler one level up rather than another rlimit here.
    """
    if limit <= 0:
        # 0 is "no cap", as `--memory 0` promises and as `--limit 0` beside it
        # already means. Guarded here as well as at the CLI because `cap` is
        # reachable from a spec file, and `setrlimit(..., 0)` would make the
        # sandbox fail every row for a reason no output would explain.
        return
    # Both, because neither is enforced everywhere and they cost the same to
    # ask for. Linux honours `RLIMIT_AS`; macOS largely does not, and CI proved
    # it rather than the docs -- the first version of this passed on Linux and
    # failed the macOS job with the runaway allocation simply succeeding.
    # `RLIMIT_DATA` covers anonymous `mmap` on Linux since 4.7 and is the one
    # macOS is likelier to apply, so asking for both is how this stays a guard
    # on more than one platform without branching on a platform name.
    for which in (resource.RLIMIT_AS, resource.RLIMIT_DATA):
        try:
            soft, hard = resource.getrlimit(which)
        except (OSError, ValueError):  # pragma: no cover - platform without it
            continue
        # Never raise an existing ceiling: a caller who already sandboxed us
        # meant it.
        ceiling = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        if soft != resource.RLIM_INFINITY and soft <= ceiling:
            continue
        try:
            resource.setrlimit(which, (ceiling, hard))
        except (OSError, ValueError):  # pragma: no cover - refused
            continue


def _exhausted(err: ExcInfo | None) -> bool:
    """Whether this traceback is the memory cap firing rather than an assertion.

    `issubclass` rather than `is`, because the cap can also surface as a
    subclass raised by an extension module; `err[0]` rather than the instance,
    because building the instance is itself an allocation that may not have
    succeeded.
    """
    return err is not None and err[0] is not None and issubclass(err[0], MemoryError)


def _carrier(test: object, err: ExcInfo | None, each: float) -> str:
    """Why this error is not an answer, or "" when it is one.

    Both limits in one place, because both are the same mistake waiting to
    happen: they raise *inside* a real `TestCase`, so they arrive at `addError`
    indistinguishable by protocol from that test noticing the mutation, and
    filed as answers they credit a test that asserted nothing. Written out
    twice, a refinement to one copy leaves the other crediting -- and the
    `addSubTest` copy had no test at all.
    """
    if err is None or err[0] is None:
        return ""
    if issubclass(err[0], Hung):
        return f"{test} did not finish within {each:g}s"
    return f"{test} ran out of memory" if _exhausted(err) else ""


class Hung(BaseException):
    """A test that ran past its share of the clock.

    `BaseException`, not `Exception`, so that a test doing `except Exception` --
    to assert on a message, which tests here do -- cannot swallow the alarm and
    hang anyway. `unittest` catches `BaseException` around a test, so it is still
    reported rather than escaping the runner.
    """


def _ring(signum: int, frame: object) -> None:
    """Raise, rather than set a flag.

    The whole mechanism turns on this. PEP 475 makes Python *retry* a syscall
    interrupted by a signal, so a handler that recorded the alarm and returned
    would be swallowed by exactly the blocking `read()` a hung test sits in.
    Raising propagates instead of resuming. Measured in tupferl, where this was
    written, against a fifo read and a `subprocess.run`, both interrupted at
    0.5s.
    """
    raise Hung("this test ran past the per-test limit")


class Verdicts(unittest.TextTestResult):
    """Keeps the tests that asserted apart from the carriers that did not."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Real test methods that failed. This is what `caught` means.
        self.noticed: list[str] = []
        #: The same tests, as `python -m unittest` takes them back --
        #: `module.Class.method`, where `noticed` holds `method (module.Class.
        #: method)` for a human. Recorded rather than parsed back out of the
        #: display string, because `mutate` feeds these straight to a loader and
        #: a display format is not an API.
        self.killers: list[str] = []
        #: Fixtures that died before any assertion ran. Not an answer.
        self.broke: list[str] = []
        #: The formatted traceback of the first test that noticed.
        #:
        #: A mutation run does not want this -- `caught` is the whole answer and
        #: 200 tracebacks is noise. The *baseline* does: a red baseline voids
        #: every verdict above it, and until this was recorded the only thing
        #: said about one was the failing test's name. Diagnosing it meant
        #: reproducing the shard by hand, and in tupferl -- where this field was
        #: added -- five reproductions of a red baseline all came back green
        #: because the sixth thing that differed was never guessed. See
        #: `mutate.run`'s baseline branch, which is the one reader.
        self.reasons: list[str] = []
        #: Seconds one test may take. 0 disables the alarm, which is what a
        #: platform without `SIGALRM` gets.
        self.each: float = 0.0
        #: What each test that ran cost, by id. `mutate.Killers` accumulates
        #: these so it can order the cheap high-yield tests first; the *baseline*
        #: runs are where they mostly come from, since those alone run a whole
        #: selection to the end without `failfast` cutting it short.
        self.times: dict[str, float] = {}
        self._began = 0.0

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._began = time.perf_counter()
        if self.each:
            signal.setitimer(signal.ITIMER_REAL, self.each)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.times[test.id()] = time.perf_counter() - self._began
        # Cancelled here rather than only on the next `startTest`, or a fast test
        # would be charged for the timer its predecessor set and the alarm would
        # land in whatever ran next.
        if self.each:
            signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)

    def _answered(self, test: unittest.TestCase, err: ExcInfo | None = None) -> None:
        """Record one test that noticed, every way round."""
        self.noticed.append(str(test))
        self.killers.append(test.id())
        if err is not None and not self.reasons:
            # The first only. A `failfast` run stops here anyway, and a baseline
            # -- which never uses `failfast` -- is diagnosed from the first
            # failure just as well as from forty.
            # `traceback.format_exception`, not `TestResult._exc_info_to_string`:
            # the latter is private, untyped in typeshed, and the version that
            # differs between releases -- three reasons the one lint that only
            # fails in CI would have found here.
            self.reasons.append("".join(traceback.format_exception(*err)))

    def addFailure(self, test: unittest.TestCase, err: ExcInfo) -> None:
        super().addFailure(test, err)
        self._answered(test, err)

    def addError(self, test: unittest.TestCase, err: ExcInfo) -> None:
        super().addError(test, err)
        # The two limits, classified by type rather than by protocol, because by
        # protocol they are a test noticing. `cap` makes a runaway mutation
        # raise `MemoryError` and the alarm makes a hung one raise `Hung`, both
        # inside whichever real `TestCase` happened to be running. Left alone
        # either would report `caught`, naming a test that asserted nothing and
        # crediting it with a guard it does not have -- precisely the lie this
        # module was written to remove. See `_carrier`.
        if reason := _carrier(test, err, self.each):
            self.broke.append(reason)
            return
        # `_ErrorHolder` -- a dead `setUpClass`, `setUpModule` or `tearDown` --
        # is not a `TestCase`, and that is the whole check. It carries a
        # traceback for something that happened *around* the tests, so no
        # assertion in it was ever evaluated.
        # Kept as a ternary over the *list*, rather than an `if`/`else` around
        # two statements. `test` is annotated `TestCase` because that is what
        # typeshed says `addError` takes, so mypy proves an `else:` branch
        # unreachable and `warn_unreachable` rejects it -- while at runtime the
        # branch is exactly the case this check exists for. `target is` is a
        # fact about a list mypy cannot argue with.
        target = self.noticed if isinstance(test, unittest.TestCase) else self.broke
        if target is self.noticed:
            # `_answered`, not a re-spelling of its body: the killer id and the
            # first-only traceback were written out here a second time once,
            # and `_carrier`'s docstring names "a refinement to one copy leaves
            # the other" as exactly this file's hazard.
            self._answered(test, err)
        else:
            target.append(str(test))

    def addSubTest(
        self, test: unittest.TestCase, subtest: unittest.TestCase, err: ExcInfo | None
    ) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            # `test` is the owning case; `subtest` is the `_SubTest` carrier that
            # the base class files into `failures`. Recording the owner is what
            # keeps a `subTest` assertion a real answer.
            if reason := _carrier(test, err, self.each):
                self.broke.append(reason)
            else:
                # The *owner*, not the `_SubTest` carrier: its `id()` carries the
                # parameters in brackets and `unittest` cannot load that back.
                self._answered(test)


def each_test(seconds: float) -> float:
    """Arm the per-test alarm, and say what was actually armed.

    0 where `SIGALRM` does not exist -- Windows, and any non-main thread. The
    product is a shell hook for bash and zsh, so Windows is not a platform this
    runs on, and `collect` runs in the main thread: this is a guard rather than
    a supported path. The returned value is what the `Verdicts` instances are
    given, so a run with no alarm armed reports every test at `0s` in its
    `broke` messages rather than quoting a bound that was never in force.
    """
    if not seconds or not hasattr(signal, "SIGALRM"):
        return 0.0
    try:
        signal.signal(signal.SIGALRM, _ring)
    except ValueError:
        return 0.0
    return seconds


def collect(
    names: list[str], failfast: bool, each: float = 0.0, first: list[str] | None = None
) -> dict[str, Any]:
    loader = unittest.TestLoader()
    # No names means the whole suite, and it has to be `discover` rather than
    # the package name: `loadTestsFromNames(["tests"])` imports the package and
    # finds nothing in it, so the run comes back green having executed zero
    # tests. That is reported as `broke` -- correctly, and confusingly, since the
    # request was for everything.
    chosen = (
        loader.loadTestsFromNames(names)
        if names
        else loader.discover(".", pattern="test_*.py", top_level_dir=".")
    )
    # `first` in its own argument rather than pushed onto `names`, and that is
    # not tidiness. An empty `names` *means* the whole suite -- `mutate`'s
    # `WHOLE_SUITE` -- and the fall-through above is how. Prepending to the list
    # makes it non-empty, so "run everything" quietly became "run these three",
    # and `confirm` builds exactly that row: it widens a survivor's selection to
    # `WHOLE_SUITE` while the cheap prefix is still attached. The pass that
    # promises every survivor was re-run against the whole suite would have run
    # eight tests and said so.
    suite = unittest.TestSuite([loader.loadTestsFromNames(first), chosen]) if first else chosen
    if loader.errors:
        # Public API, and checked before running: the suite `loadTestsFromNames`
        # returns for an unimportable module holds a synthetic `_FailedTest`
        # which *is* a `TestCase`, so running it would report a test noticing.
        return {
            "loaded": True,
            "ran": 0,
            "noticed": [],
            "killers": [],
            "reasons": [],
            "times": {},
            # `str()` because typeshed types `errors` as exception classes while
            # `unittest` actually appends formatted tracebacks; the first line is
            # the "Failed to import test module: x" that says which.
            "broke": [str(error).splitlines()[0] for error in loader.errors],
        }
    armed = each_test(each)

    def build(*args: Any, **kwargs: Any) -> Verdicts:
        made = Verdicts(*args, **kwargs)
        made.each = armed
        return made

    result = unittest.TextTestRunner(
        stream=io.StringIO(), verbosity=0, failfast=failfast, resultclass=build
    ).run(suite)
    assert isinstance(result, Verdicts)
    return {
        "loaded": True,
        "ran": result.testsRun,
        "noticed": result.noticed,
        "killers": result.killers,
        "reasons": result.reasons,
        "times": result.times,
        "broke": result.broke,
    }


def main(argv: list[str]) -> None:
    report, failfast = argv[0], argv[1] == "1"
    first, names = [n for n in argv[4].split() if n], argv[5:]
    # Before the suite loads, not after: `discover` imports every test module,
    # and a mutation to something imported at module scope can run away there.
    cap(int(argv[2]))
    try:
        written = collect(names, failfast, float(argv[3]), first)
    except BaseException:
        # Said, not inferred. The caller used to conclude "the suite could not be
        # loaded" from an absent file, which is also what a typo in this file
        # produces -- two very different problems with byte-identical output, in
        # a tool whose whole thesis is that those must be told apart.
        # `limit=4` keeps the tail readable; `format_exc` is itself an
        # allocation, so a `MemoryError` here may leave nothing at all -- which
        # the caller already reads as `broke` from the absent report.
        written = {"loaded": False, "why": traceback.format_exc(limit=4)}
    with open(report, "w", encoding="utf-8") as out:
        json.dump(written, out)


if __name__ == "__main__":
    main(sys.argv[1:])
