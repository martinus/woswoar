"""Import as a state machine, driven by generated sequences of edits.

The example tests beside this one each pin one story: append, re-import, expect
these commands. A property test states the *rule* those stories are instances of
and lets the machine look for a sequence that breaks it:

    every command in the history at the moment of an import ends up in `logs/`

Then it searches. Where a hand-written fixture asks the two or three questions
its author thought of, this asks a few hundred nobody did -- and when one fails
it *shrinks*, cutting the failing sequence down to the shortest one that still
breaks, which is usually small enough to read as a bug report.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from woswoar import cache, importer, store


class ImportKeepsEverything(RuleBasedStateMachine):
    """A zsh history that grows, gets trimmed, and is imported, in any order.

    The three rules are the three things that happen to a real
    `.zsh_history`: a shell appends when it exits, zsh rewrites the file from
    the front when it trims to `SAVEHIST`, and `woswoar import` reads it.
    Hypothesis picks the order and the sizes.

    Commands are unique and numbered, which is the fixture doing one job well:
    with repeated text, "is it in `logs/`" stops distinguishing *this* command
    from an earlier identical one, and the invariant would pass for the wrong
    reason.
    """

    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory(prefix="woswoar-prop-")
        self.root = Path(self._tmp.name).resolve()
        (self.root / "home").mkdir()
        self._env = mock.patch.dict(
            os.environ, store.sandbox_environ(self.root, self.root / "home"), clear=True
        )
        self._env.start()

        self.source = self.root / "hist"
        self.source.write_text("", encoding="utf-8")
        #: The records in the file, newest last.
        self.lines: list[str] = []
        #: Every command the file held at the moment of some import. This is
        #: what `logs/` is owed, and it never shrinks -- trimming the source
        #: after an import does not un-import anything.
        self.owed: set[str] = set()
        self.made = 0

    def teardown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def write(self) -> None:
        self.source.write_text("".join(self.lines), encoding="utf-8")
        os.utime(self.source, (1_700_000_000, 1_700_000_000))

    @rule(count=st.integers(min_value=1, max_value=4))
    def append(self, count: int) -> None:
        """A shell exiting: new records on the end, timestamps of their own.

        Deliberately not always increasing. Two shells opened together and
        closed in the other order interleave, which is ordinary and is what
        #289 was about.
        """
        for _ in range(count):
            self.made += 1
            stamp = 1_700_000_000 + (self.made * 7919) % 1000
            self.lines.append(f": {stamp}:0;cmd-{self.made}\n")
        self.write()

    @rule(count=st.integers(min_value=1, max_value=3))
    def trim(self, count: int) -> None:
        """zsh rewriting the file to fit `SAVEHIST`: records leave the front."""
        del self.lines[:count]
        self.write()

    @rule()
    def do_import(self) -> None:
        self.owed |= {line.split(";", 1)[1].strip() for line in self.lines}
        importer.run("zsh", self.source)

    @invariant()
    def nothing_the_import_saw_is_missing(self) -> None:
        if not self.owed:
            return
        have = {entry.cmd for entry in cache.load_entries()}
        missing = self.owed - have
        assert not missing, f"never imported: {sorted(missing)}"


ImportKeepsEverything.TestCase.settings = settings(
    max_examples=250,
    stateful_step_count=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestImportKeepsEverything = ImportKeepsEverything.TestCase
# Hypothesis names the generated case after the machine, so `tools/run_tests.py`
# -- which records a class by name and loads it back by name to shard the run --
# looked for `ImportKeepsEverything` and found the state machine, which is not a
# `TestCase`. Renaming it is what makes the two agree.
TestImportKeepsEverything.__name__ = "TestImportKeepsEverything"
TestImportKeepsEverything.__qualname__ = "TestImportKeepsEverything"
# `__module__` as well, and it is the one that actually bit: the generated class
# is defined in `hypothesis.stateful`, so the runner built
# `hypothesis.stateful.TestImportKeepsEverything` and could not load it back.
TestImportKeepsEverything.__module__ = __name__
