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
import sys
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
    """
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


class Verdicts(unittest.TextTestResult):
    """Keeps the tests that asserted apart from the carriers that did not."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Real test methods that failed. This is what `caught` means.
        self.noticed: list[str] = []
        #: Fixtures that died before any assertion ran. Not an answer.
        self.broke: list[str] = []

    def addFailure(self, test: unittest.TestCase, err: ExcInfo) -> None:
        super().addFailure(test, err)
        self.noticed.append(str(test))

    def addError(self, test: unittest.TestCase, err: ExcInfo) -> None:
        super().addError(test, err)
        if _exhausted(err):
            # The one exception classified by type rather than by protocol, and
            # it has to be. `cap` makes a runaway mutation raise `MemoryError`
            # inside whichever test happened to be running -- a real `TestCase`,
            # reached through `addError`, indistinguishable by protocol from
            # that test having *noticed* something. Left alone it would report
            # `caught`, naming a test that asserted nothing and crediting it
            # with a guard it does not have. That is precisely the lie this
            # module was written to remove, so the guard against running out of
            # memory must not reintroduce it one level up.
            self.broke.append(f"{test} ran out of memory")
            return
        # `_ErrorHolder` -- a dead `setUpClass`, `setUpModule` or `tearDown` --
        # is not a `TestCase`, and that is the whole check. It carries a
        # traceback for something that happened *around* the tests, so no
        # assertion in it was ever evaluated.
        target = self.noticed if isinstance(test, unittest.TestCase) else self.broke
        target.append(str(test))

    def addSubTest(
        self, test: unittest.TestCase, subtest: unittest.TestCase, err: ExcInfo | None
    ) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            # `test` is the owning case; `subtest` is the `_SubTest` carrier that
            # the base class files into `failures`. Recording the owner is what
            # keeps a `subTest` assertion a real answer.
            if _exhausted(err):
                self.broke.append(f"{test} ran out of memory")
            else:
                self.noticed.append(str(test))


def collect(names: list[str], failfast: bool) -> dict[str, Any]:
    loader = unittest.TestLoader()
    # No names means the whole suite, and it has to be `discover` rather than
    # the package name: `loadTestsFromNames(["tests"])` imports the package and
    # finds nothing in it, so the run comes back green having executed zero
    # tests. That is reported as `broke` -- correctly, and confusingly, since the
    # request was for everything.
    suite = (
        loader.loadTestsFromNames(names)
        if names
        else loader.discover(".", pattern="test_*.py", top_level_dir=".")
    )
    if loader.errors:
        # Public API, and checked before running: the suite `loadTestsFromNames`
        # returns for an unimportable module holds a synthetic `_FailedTest`
        # which *is* a `TestCase`, so running it would report a test noticing.
        return {
            "loaded": True,
            "ran": 0,
            "noticed": [],
            # `str()` because typeshed types `errors` as exception classes while
            # `unittest` actually appends formatted tracebacks; the first line is
            # the "Failed to import test module: x" that says which.
            "broke": [str(error).splitlines()[0] for error in loader.errors],
        }
    result = unittest.TextTestRunner(
        stream=io.StringIO(), verbosity=0, failfast=failfast, resultclass=Verdicts
    ).run(suite)
    assert isinstance(result, Verdicts)
    return {
        "loaded": True,
        "ran": result.testsRun,
        "noticed": result.noticed,
        "broke": result.broke,
    }


def main(argv: list[str]) -> None:
    report, failfast, names = argv[0], argv[1] == "1", argv[3:]
    # Before the suite loads, not after: `discover` imports every test module,
    # and a mutation to something imported at module scope can run away there.
    cap(int(argv[2]))
    try:
        written = collect(names, failfast)
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
