"""What `forget` promises, as properties rather than as examples.

`forget` is the one command that takes something *out* of `logs/`, and history
is append-only: a chunk keeps a forgotten row for ever, so the only place it can
be stopped is on its way back out, every time a peer's chunk is merged. That
makes `surviving` a filter that runs on every line of every chunk, for the life
of the installation, and gives it the shape a property test is for -- a small
pure function whose failure is a command reappearing, or a command that was
never forgotten disappearing.

The second is the one worth the effort. A row that comes back is visible and can
be forgotten again; a row that vanishes for nobody's reason is the failure the
`digest` docstring calls "a line that vanishes from somebody's history for no
reason they could ever find".
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.test_record_properties import ENTRIES
from woswoar import entry, forget, store

settings.load_profile("woswoar")  # registered beside `ENTRIES`, which this imports.

#: Lines as they sit in a log. Bytes rather than text, because that is what
#: `surviving` is handed -- a decrypted chunk straight off a peer -- and because
#: the module hashes with ``surrogateescape`` precisely so that a row no strict
#: decoder would accept still gets a name. A strategy built on `st.text` and
#: encoded strictly would refuse to generate exactly those rows.
LINE = st.binary(max_size=24).map(lambda row: row.replace(b"\n", b"x"))

#: A digest no generated row can carry. ``b"absent"`` was the first spelling
#: and it is wrong: `LINE` is unrestricted binary, so it *can* produce those six
#: bytes, and a property asserting "nothing here is suppressed" would then fail
#: for a reason that has nothing to do with the code -- rarely, and only for
#: whoever happened to run it. A newline cannot appear in a row, because it is
#: what separates them.
NO_ROW = forget._of(b"\n")

#: A chunk's plaintext, with and without the trailing newline -- the shape
#: `surviving`'s docstring promises to preserve.
BLOCKS = st.builds(
    lambda lines, final: b"\n".join(lines) + (b"\n" if final and lines else b""),
    st.lists(LINE, max_size=8),
    st.booleans(),
)


def lines_of(block: bytes) -> list[bytes]:
    """The rows of a block, without their separators.

    Not `bytes.splitlines`, which also splits on ``\r`` and on the Unicode
    separators -- `surviving` splits on ``\n`` alone, and a helper that
    disagreed with it would turn one row into two and read as a bug in the
    function under test.
    """
    if not block:
        return []
    return block.split(b"\n")[:-1] if block.endswith(b"\n") else block.split(b"\n")


def sandbox(case: unittest.TestCase) -> None:
    """A fresh `WOSWOAR_HOME` for one test method.

    `setUp` runs once per test *method*, not once per example, so without
    `fresh` below the digest file accumulates across a method's examples and
    whatever an earlier example forgot stays forgotten. That is not a
    theoretical tidiness: it failed "drops the forgotten and only those" on the
    second run, because an all-default `Entry` is a shape Hypothesis generates
    often and one example had already forgotten the line another one expected
    to keep.
    """
    box = tempfile.TemporaryDirectory(prefix="woswoar-forget-prop-")
    case.addCleanup(box.cleanup)
    root = Path(box.name).resolve()
    (root / "home").mkdir()
    patched = mock.patch.dict(os.environ, store.sandbox_environ(root, root / "home"), clear=True)
    patched.start()
    case.addCleanup(patched.stop)


def fresh() -> None:
    """Start an example with nothing forgotten.

    Called first in every property that touches the file, so each example is
    independent and its assertions can be exact. Unlinking rather than a new
    `TemporaryDirectory` per example: `store.sandbox_environ` is read by every
    caller through the environment, and re-patching it mid-method would race the
    ``addCleanup`` stack rather than nest inside it.
    """
    store.forgotten_file().unlink(missing_ok=True)


class TestSurvivingAChunk(unittest.TestCase):
    @given(BLOCKS)
    def test_suppressing_nothing_changes_nothing(self, block: bytes) -> None:
        """And the docstring claims more than equality: the argument itself
        comes back, so a machine that never ran `forget` pays one truth test
        per chunk rather than a copy of it."""
        self.assertIs(forget.surviving(block, frozenset()), block)

    @given(BLOCKS)
    def test_a_chunk_holding_none_of_them_comes_back_untouched(self, block: bytes) -> None:
        """The second half of the same claim, and the half an equality
        assertion cannot see. The docstring gives the identity return two
        reasons, and this is the one that applies to a machine that *has* run
        `forget`: almost every chunk it merges holds none of what it forgot, and
        rebuilding one that lost nothing measured 4.4 ms and a second copy of a
        4 MB block. ``assertEqual`` passes against a rebuild; only ``assertIs``
        says the copy was not made.
        """
        self.assertIs(forget.surviving(block, {NO_ROW}), block)

    @given(BLOCKS)
    def test_forgetting_every_line_leaves_nothing(self, block: bytes) -> None:
        every = {forget._of(line) for line in lines_of(block)}
        self.assertEqual(forget.surviving(block, every), b"")

    @given(BLOCKS)
    def test_a_line_nobody_forgot_survives(self, block: bytes) -> None:
        """The failure that cannot be undone. A row that comes back can be
        forgotten again; a row that vanishes for no reason is gone from a
        history whose owner has no way to find out why."""
        rows = lines_of(block)
        if not rows:
            return
        doomed = forget._of(rows[0])
        kept = lines_of(forget.surviving(block, {doomed}))
        for row in rows[1:]:
            if forget._of(row) != doomed:
                self.assertIn(row, kept)

    @given(BLOCKS)
    def test_it_invents_nothing(self, block: bytes) -> None:
        """Every surviving row was an input row. A filter that can rewrite a
        line is a filter that can put a command in somebody's history."""
        rows = set(lines_of(block))
        survivors = lines_of(forget.surviving(block, {NO_ROW}))
        for row in survivors:
            self.assertIn(row, rows)

    @given(BLOCKS)
    def test_it_keeps_the_order(self, block: bytes) -> None:
        rows = lines_of(block)
        if not rows:
            return
        doomed = {forget._of(rows[0])}
        expected = [row for row in rows if forget._of(row) not in doomed]
        self.assertEqual(lines_of(forget.surviving(block, doomed)), expected)

    @given(BLOCKS)
    def test_it_is_idempotent(self, block: bytes) -> None:
        rows = lines_of(block)
        doomed = {forget._of(rows[0])} if rows else {NO_ROW}
        once = forget.surviving(block, doomed)
        self.assertEqual(forget.surviving(once, doomed), once)

    @given(BLOCKS)
    def test_the_last_line_gets_no_newline_it_did_not_have(self, block: bytes) -> None:
        """The docstring's third claim, and only as far as it goes: it is about
        the *final* row. Stated any wider it is false for an honest reason --
        drop the last row of ``b"a\\nb"`` and what is left is ``b"a\\n"``, which
        ends in a newline because that newline was always ``a``'s. Nothing in
        the bytes says whether a trailing newline terminates the last row or
        merely separates it from a row that is no longer there.
        """
        rows = lines_of(block)
        if not rows or block.endswith(b"\n"):
            return
        # Suppress something that is *not* the last row, so the last row is
        # what decides the ending. A fixture where every row is forgotten
        # cannot tell an invented newline from an absent one.
        doomed = {forget._of(rows[0])}
        if forget._of(rows[-1]) in doomed:
            return
        out = forget.surviving(block, doomed)
        self.assertFalse(out.endswith(b"\n"), "a trailing newline was invented")

    @given(BLOCKS, st.integers(0, 7))
    def test_it_is_the_surviving_rows_and_nothing_else(self, block: bytes, which: int) -> None:
        """The whole contract in one line, which the four properties above are
        each a readable corner of: the result is the surviving rows, in order,
        carrying exactly the bytes they carried in the input. Written on its own
        it would pass while saying nothing about *which* rows survive; written
        alongside them it pins the bytes.
        """
        rows = lines_of(block)
        if not rows:
            return
        doomed = {forget._of(rows[which % len(rows)])}
        last = len(rows) - 1
        expected = b"".join(
            row + (b"" if index == last and not block.endswith(b"\n") else b"\n")
            for index, row in enumerate(rows)
            if forget._of(row) not in doomed
        )
        self.assertEqual(forget.surviving(block, doomed), expected)


class TestTheDigestFile(unittest.TestCase):
    """`remember` and `load_digests` against a real file, since that is the pair
    that has to survive a restart."""

    def setUp(self) -> None:
        sandbox(self)

    @given(st.lists(st.text(max_size=20), max_size=6))
    def test_what_was_remembered_comes_back(self, texts: list[str]) -> None:
        fresh()
        names = [forget.digest(text) for text in texts]
        forget.remember(names)
        self.assertEqual(forget.load_digests(), set(names))

    @given(st.lists(st.text(max_size=20), min_size=1, max_size=6))
    def test_remembering_twice_is_remembering_once(self, texts: list[str]) -> None:
        """`remember` reads back before appending, so a repeated `forget` must
        not grow the file with names it already holds."""
        fresh()
        names = [forget.digest(text) for text in texts]
        forget.remember(names)
        self.assertEqual(
            # Append-only, so a duplicate is not cosmetic: it is a line this
            # file carries for the life of the installation, and `remember`
            # deduplicates within its own argument as well as against the file.
            len(store.forgotten_file().read_text(encoding="utf-8").split()),
            len(set(names)),
        )
        first = store.forgotten_file().read_text(encoding="utf-8")
        forget.remember(names)
        self.assertEqual(store.forgotten_file().read_text(encoding="utf-8"), first)

    @given(st.lists(st.text(max_size=20), min_size=1, max_size=4))
    def test_a_malformed_line_costs_only_itself(self, texts: list[str]) -> None:
        """Skipping is deliberate: one row coming back on a re-merge is
        visible and fixable, where raising stops `sync` on every machine that
        ever ran this command."""
        fresh()
        names = [forget.digest(text) for text in texts]
        forget.remember(names)
        with store.private_append(store.forgotten_file()) as out:
            out.write("not-a-digest\n\n  \n")
        # Exact, not ``<=``: a subset assertion passes just as happily when
        # `load_digests` has *also* loaded the malformed line, which is half of
        # what this claims. It was written that way first and a mutation that
        # dropped the `_DIGEST` guard survived it.
        self.assertEqual(forget.load_digests(), set(names))


class TestKeepingEntries(unittest.TestCase):
    """`keep` is the other door. `surviving` stops a forgotten row arriving from
    a peer; `keep` stops one arriving from a re-import of the same machine's own
    shell history, which is the case its docstring is about -- once `forget` has
    removed the row, `store.existing_keys` no longer recognises it as present.
    """

    def setUp(self) -> None:
        sandbox(self)

    @given(st.lists(ENTRIES, max_size=6))
    def test_forgetting_nothing_keeps_everything(self, entries: list[entry.Entry]) -> None:
        fresh()
        self.assertIs(forget.keep(entries), entries)

    @given(st.lists(ENTRIES, min_size=1, max_size=6), st.integers(0, 5))
    def test_it_drops_the_forgotten_and_only_those(
        self, entries: list[entry.Entry], which: int
    ) -> None:
        fresh()
        doomed = entry.format_line(entries[which % len(entries)])
        forget.remember([forget.digest(doomed)])
        expected = [item for item in entries if entry.format_line(item) != doomed]
        self.assertEqual(forget.keep(entries), expected)

    @given(st.lists(ENTRIES, min_size=1, max_size=6))
    def test_it_is_idempotent(self, entries: list[entry.Entry]) -> None:
        fresh()
        forget.remember([forget.digest(entry.format_line(entries[0]))])
        once = forget.keep(entries)
        self.assertEqual(forget.keep(once), once)

    @given(ENTRIES)
    def test_a_forgotten_command_does_not_come_back_through_a_re_import(
        self, item: entry.Entry
    ) -> None:
        """The headline claim, and the one that spans two modules. A digest
        names a *stored line*, so the row only stays forgotten while the record
        round trip is exact. #309 found a `session` field that came back
        unescaped: under that bug this entry re-imports to a line with a
        different digest, and a command the user deleted reappears -- which is
        the failure `forget` exists to prevent and no test inside `entry` would
        have called by that name.
        """
        fresh()
        forget.remember([forget.digest(entry.format_line(item))])
        # The host is the directory the file sits in, not a field of the line.
        again = entry.parse_line(entry.format_line(item), item.host)
        self.assertIsNotNone(again)
        assert again is not None
        self.assertEqual(forget.keep([again]), [])


if __name__ == "__main__":
    unittest.main()
