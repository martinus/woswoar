"""The shape of the package, pinned where it is otherwise only a convention.

Two facts about this codebase are load-bearing and, until this file, were held
up by nothing but comments scattered across the modules that happen to obey
them.

**The layering.** ``entry`` is the record format and imports nothing; ``store``
is the filesystem below it; ``cache`` and ``search`` sit on those; ``sync``,
``importer`` and ``prove`` are domains that nothing else may import. The graph
has no cycles, which is what makes any of the boxes movable one at a time.
Nothing enforced it, so the first accidental edge would have been found by
somebody trying to move a module, months later.

**The Ctrl-R budget.** ``docs/woswoar_design_summary.md`` measures a search at
~105 ms end to end, of which ~29 ms is importing the CLI module. That number
holds because the expensive things are imported lazily, at the call site, each
with a measured comment beside it -- ``sync`` inside each of the fourteen
commands in `__main__` that need it, ``tempfile`` in `store.write_atomic`
(3.1 ms), ``importlib.resources`` in `install.hook_bytes` (8.7 ms), and
``dataclasses`` avoided outright in `cache.Cache` (~4 ms, via ``inspect``).
Every one of those is a comment asking a future reader to remember something.
`tests/test_perf.py` measures the *result* and would catch a regression as a
blown budget with no indication of the cause; this catches the cause and names
it.

Each check starts a fresh interpreter, and has to: this suite imports almost the
whole package before any test runs -- ``tests/support.py`` alone pulls in
``__main__``, ``crypto`` and ``store`` -- so ``sys.modules`` in this process
answers a question about the test runner rather than about the package.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Every intra-package import each module pulls in, transitively, at import
#: time. Measured rather than read off the ``import`` lines, because a lazy
#: import inside a function is not an edge and a re-export is.
#:
#: Compared for equality, not containment. An edge that is genuinely wanted is a
#: one-line edit here and a sentence in the pull request, which is the review
#: this is for; a table that only caught *additions* would silently rot as
#: modules lost dependencies and stop describing the package it claims to pin.
#:
#: The five leaves are leaves on purpose. `errors` holds the exceptions every
#: layer raises; `deps`, `progress` and `report` are asked things by the layers
#: above without knowing anything about them -- so each of them stays importable
#: from anywhere without dragging a domain along. `report` in particular has to
#: be free: a check is a value any module might hand back, and a value type that
#: cost you an import of the CLI would not be used.
LAYERS: dict[str, set[str]] = {
    "entry": set(),
    "errors": set(),
    "deps": set(),
    "progress": set(),
    "report": set(),
    "credentials": {"errors"},
    "crypto": {"errors"},
    "store": {"entry"},
    "archive": {"entry", "store"},
    #: The installer decides and the CLI prints, so this reaches `report` for
    #: `Check` and nothing else above `store`. `subprocess` is asked for inside
    #: `shell_version`, because `__main__` imports this module at the top for the
    #: parser and a keypress must not pay for a fork it will not make.
    "install": {"entry", "errors", "report", "store"},
    "cache": {"entry", "store"},
    "search": {"cache", "entry", "store"},
    "importer": {"credentials", "entry", "errors", "store"},
    #: `importer` and `sync` are deliberately absent: the wizard asks about them
    #: inside the two functions that need them.
    "setup": {"entry", "errors", "install", "report", "store"},
    #: The two layers that came out of `sync` in #201, and the reason they are
    #: the two: each reaches strictly *below* it. `manifest` is the authenticity
    #: layer -- crypto, an `archive` path, an atomic write -- and `gitrepo` is
    #: every fork woswoar makes, which needs `store` for the repo path and
    #: `progress` to announce a fetch. Neither knows what a sync is, so neither
    #: can grow an edge back to one.
    "gitrepo": {"entry", "errors", "progress", "store"},
    "manifest": {"archive", "crypto", "entry", "errors", "store"},
    "sync": {
        "archive",
        "crypto",
        "entry",
        "errors",
        "gitrepo",
        "manifest",
        "progress",
        "report",
        "store",
    },
    #: `sync` is deliberately absent: `doctor` reaches it inside the two checks
    #: that need it, so asking what is wrong with an installation does not pay
    #: to import the whole sync protocol first.
    "doctor": {"cache", "crypto", "deps", "entry", "errors", "report", "search", "store"},
    #: `gitrepo` for the commit boilerplate it must not mistake for a leaked
    #: username; `manifest` only through `sync`. Both arrived when #201 turned
    #: two of sync's internal layers into modules, which is a name for an edge
    #: that already existed rather than a new one.
    "prove": {
        "archive",
        "crypto",
        "deps",
        "entry",
        "errors",
        "gitrepo",
        "manifest",
        "progress",
        "report",
        "store",
        "sync",
    },
    "__main__": {
        "cache",
        "credentials",
        "entry",
        "errors",
        "importer",
        #: `install` and not `setup`: the parser reads `install.HOOKS` for the
        #: `--shell` choices, so a keypress pays for that module whatever it
        #: does. Nothing needs `setup` until a command runs, so the four
        #: functions that want it say so themselves -- 90 us that Ctrl-R does
        #: not spend. Measured while reviewing #202, which had it at the top
        #: with a comment claiming the parser needed it.
        "install",
        "report",
        "search",
        "store",
    },
}

#: What a keypress must not pay for. Each is deferred deliberately somewhere,
#: with the measurement in the comment that defers it, and each is reachable
#: from `__main__` by one careless top-level ``import``.
#:
#: ``sqlite3`` is here for a different reason from the rest: nothing measured it,
#: but it is the atuin importer's, it is large, and the only thing keeping it off
#: this path is that `importer` opens the database inside a function.
DEFERRED_ON_THE_SEARCH_PATH = (
    "subprocess",
    "tempfile",
    "shutil",
    "dataclasses",
    "inspect",
    "importlib.resources",
    "sqlite3",
)


def _imported_by(module: str) -> tuple[set[str], set[str]]:
    """``(woswoar modules, stdlib modules)`` importing ``module`` pulls in.

    A fresh interpreter per call, and the difference against what was already
    loaded rather than the whole of ``sys.modules``: ``site`` and whatever a
    distribution puts in ``sitecustomize`` are not this package's doing, and a
    test that counted them would fail on somebody else's machine for a reason
    that has nothing to do with the change they made.

    Run from the checkout, so the ``woswoar`` being measured is the one in this
    working tree. Without ``cwd`` it would be whichever copy the subprocess found
    first -- which for a non-editable install is the one in site-packages, so the
    test would pass on a tree whose imports had changed and not been installed.
    """
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        f"import woswoar.{module}\n"
        "after = set(sys.modules) - before\n"
        "print(' '.join(sorted(m for m in after if m.startswith('woswoar.'))))\n"
        "print(' '.join(sorted(m for m in after if not m.startswith('woswoar'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"importing woswoar.{module} failed:\n{done.stderr}")
    package, stdlib = done.stdout.splitlines()
    return (
        {name.removeprefix("woswoar.") for name in package.split()} - {module},
        set(stdlib.split()),
    )


class TestTheLayering(unittest.TestCase):
    def test_every_module_imports_exactly_what_the_table_says(self) -> None:
        """The whole graph at once, so a new edge is a failure naming both ends.

        Subtests rather than one assertion over the whole dict: a wrong edge in
        `sync` should not hide a wrong edge in `search`, and the module name is
        what makes the failure readable.
        """
        for module, expected in LAYERS.items():
            with self.subTest(module=module):
                package, _ = _imported_by(module)
                self.assertEqual(package, expected)

    def test_the_table_covers_every_module(self) -> None:
        """Otherwise a module added without an entry is pinned by nothing, and
        the file above still passes -- which is the way this check dies."""
        source = REPO / "woswoar"
        present = {p.stem for p in source.glob("*.py")} - {"__init__"}
        self.assertEqual(present, set(LAYERS))

    def test_the_graph_has_no_cycles(self) -> None:
        """Stated directly rather than left implied by the table.

        The table would catch a cycle, but only as two edges a reader has to put
        together themselves -- and "no cycles" is the property that makes the
        layers mean anything, so it is worth a failure that says so. Computed
        from `LAYERS`, which the test above ties to the real imports.
        """
        for module in LAYERS:
            for dependency in LAYERS[module]:
                self.assertNotIn(
                    module,
                    LAYERS[dependency],
                    f"{module} and {dependency} import each other",
                )


class TestTheSearchPathStaysCheap(unittest.TestCase):
    """Ctrl-R runs `woswoar list` in a fresh interpreter, so every module the CLI
    imports at the top level is paid for on every keypress.

    The design document budgets ~29 ms for that import against ~105 ms for the
    whole search. `tests/test_perf.py` guards the total; these guard the reasons
    it holds, which is what a person reading a failure needs.
    """

    def test_the_cli_does_not_import_sync_crypto_or_prove(self) -> None:
        """`sync` alone was measured at ~2 ms of the CLI module's import, and it
        drags `crypto` and `progress` behind it. Every command that needs it says
        `from . import sync` inside the function instead -- fourteen call sites,
        any one of which could have been written at the top of the file with
        nothing to notice."""
        package, _ = _imported_by("__main__")
        self.assertEqual(package & {"sync", "crypto", "prove"}, set())

    def test_the_cli_does_not_import_the_expensive_stdlib(self) -> None:
        """Each of these is deferred at a call site with its cost written beside
        it -- `tempfile` 3.1 ms in `store.write_atomic`, `importlib.resources`
        8.7 ms in `install.hook_bytes`, `dataclasses` ~4 ms avoided in
        `cache.Cache`. A top-level import of any of them is invisible in review
        and shows up as a slower keypress."""
        _, stdlib = _imported_by("__main__")
        self.assertEqual(sorted(stdlib.intersection(DEFERRED_ON_THE_SEARCH_PATH)), [])

    def test_search_reaches_none_of_the_domains(self) -> None:
        """`search` is the module Ctrl-R is made of. It may see `cache`, `store`
        and `entry` and nothing else -- notably not `importer`, which is where
        `home_relative` used to live and whose move into `entry` is documented
        there as a measured cost of pulling that module onto this path."""
        package, _ = _imported_by("search")
        self.assertEqual(package, {"cache", "entry", "store"})


if __name__ == "__main__":
    unittest.main()
