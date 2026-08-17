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
import sys
import traceback
import unittest
from types import TracebackType
from typing import Any

#: What `unittest` hands a result method for a failure, spelled exactly as
#: typeshed spells it -- `mypy --strict` checks these overrides for Liskov and a
#: near-miss here is an error, not a warning.
ExcInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]


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
            self.noticed.append(str(test))


def collect(names: list[str], failfast: bool) -> dict[str, Any]:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names)
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
    report, failfast, names = argv[0], argv[1] == "1", argv[2:]
    try:
        written = collect(names, failfast)
    except BaseException:
        # Said, not inferred. The caller used to conclude "the suite could not be
        # loaded" from an absent file, which is also what a typo in this file
        # produces -- two very different problems with byte-identical output, in
        # a tool whose whole thesis is that those must be told apart.
        written = {"loaded": False, "why": traceback.format_exc(limit=4)}
    with open(report, "w", encoding="utf-8") as out:
        json.dump(written, out)


if __name__ == "__main__":
    main(sys.argv[1:])
