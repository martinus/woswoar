"""The export watermark: what `read_tail` and `line_start` promise a log file.

These two are the only things standing between a log file and an append-only
encrypted repository. `export` seals from the watermark to the end of the file
and commits it, so a watermark that is wrong by one byte publishes a record with
its head missing -- to every peer, for ever, in a history that cannot be
rewritten. #263 is that bug: `forget` shortens a log, a crash lands between the
rewrite and the `state.save`, and the stale watermark now points into the middle
of a record.

Examples cover the shapes someone thought of. What this asks instead is the
question the whole design is an answer to -- *can a fragment ever reach a
chunk?* -- against sequences of appends, forgets and crashes nobody chose.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from woswoar import store

settings.register_profile("woswoar-export", max_examples=300, deadline=None)
settings.load_profile("woswoar-export")

#: A record as it sits in a log. Bytes, not text: these two functions are the
#: layer below the record format and never decode anything, and a text strategy
#: encoded strictly cannot even generate the mis-decoded byte that
#: `entry`'s ``surrogateescape`` exists to carry through here intact.
RECORD = st.binary(min_size=1, max_size=30).map(lambda row: row.replace(b"\n", b"x"))


def joined(records: list[bytes]) -> bytes:
    return b"".join(row + b"\n" for row in records)


#: What a log file can hold, and the reason it is not just `st.binary`.
#:
#: A newline is one byte in 256, so a random 80-byte string holds one about a
#: third of the time and two almost never -- and every claim here is about where
#: the newlines are. A mutation making `line_start` rewind to the *first*
#: boundary in its window instead of the last survived a run against
#: `st.binary` alone, not because the property was wrong but because nearly no
#: generated body had two boundaries to tell apart.
#:
#: Both arms are kept: real logs are the first, and the second is where a file
#: that is not one at all gets its answer.
#: A record's contents, with no ``<``: the machine tags each record with one and
#: leans on payloads never containing it. See `ExportPublishesWholeRecords.ever`.
PAYLOAD = st.binary(max_size=24).map(lambda row: row.replace(b"\n", b"x").replace(b"<", b"y"))

BODIES = st.one_of(
    st.builds(
        lambda rows, tail: joined(rows) + tail,
        st.lists(RECORD, max_size=6),
        st.binary(max_size=8).map(lambda row: row.replace(b"\n", b"x")),
    ),
    st.binary(max_size=80),
)


class Tempfiles:
    """A directory of scratch log files, torn down with the test."""

    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="woswoar-export-prop-")
        self.addCleanup(box.cleanup)  # type: ignore[attr-defined]
        self.box = Path(box.name)
        self.made = 0

    def written(self, body: bytes) -> Path:
        """A file holding ``body``.

        A new name each time: Hypothesis reuses one fixture across every example
        of a method, and a reused path would leave the previous example's bytes
        behind whenever this one is shorter.
        """
        self.made += 1
        path = self.box / f"log{self.made}"
        path.write_bytes(body)
        return path


class TestReadingTheTail(Tempfiles, unittest.TestCase):
    @given(BODIES, st.integers(0, 220))
    def test_it_returns_whole_lines_or_nothing(self, body: bytes, offset: int) -> None:
        """The single claim `export` rests on. Whatever is returned is sealed
        into a chunk, and a chunk not ending on a record boundary is a record
        published in halves.
        """
        path = self.written(body)
        data, _ = store.read_tail(path, offset)
        self.assertTrue(data == b"" or data.endswith(b"\n"))

    @given(BODIES, st.integers(0, 220))
    def test_the_new_watermark_is_where_the_data_ended(self, body: bytes, offset: int) -> None:
        """The offset is saved and the data is committed; if they disagree the
        next export either repeats bytes or skips them, and skipping loses a
        command with nothing to notice."""
        path = self.written(body)
        data, mark = store.read_tail(path, offset)
        self.assertEqual(mark, offset + len(data))

    @given(BODIES, st.integers(0, 220))
    def test_reading_past_the_end_consumes_nothing(self, body: bytes, offset: int) -> None:
        """`export` leans on this: it stats first and skips the call when the
        file cannot have grown, which is only sound if the call it skipped
        would have been a no-op."""
        path = self.written(body)
        if offset < len(body):
            return
        self.assertEqual(store.read_tail(path, offset), (b"", offset))

    @given(st.lists(RECORD, max_size=8))
    def test_successive_reads_tile_the_file(self, records: list[bytes]) -> None:
        """No byte read twice, none skipped. Reading to exhaustion from zero has
        to reproduce the file's complete-line prefix exactly."""
        body = joined(records)
        path = self.written(body)
        seen, mark = b"", 0
        # Bounded, not ``while True``: a `read_tail` that ignored the watermark
        # would return the whole file for ever, and an unbounded loop turns that
        # into a hung test rather than a failing one. It did -- the mutation run
        # for this file reported TIMEOUT on exactly that edit, which says
        # nothing about whether the property noticed.
        for _ in range(len(records) + 2):
            data, mark = store.read_tail(path, mark)
            if not data:
                break
            seen += data
        else:
            self.fail("read_tail never reached the end of the file")
        self.assertEqual(seen, body)
        self.assertEqual(mark, len(body))

    @given(st.lists(RECORD, min_size=1, max_size=6), RECORD)
    def test_a_half_written_record_waits(self, records: list[bytes], partial: bytes) -> None:
        """A shell killed mid-append leaves a line with no newline. Sealing it
        would publish a fragment, so it is left for the writer to finish."""
        whole = joined(records)
        path = self.written(whole + partial)
        data, mark = store.read_tail(path, 0)
        self.assertEqual(data, whole)
        self.assertEqual(mark, len(whole))


class TestRewindingAWatermark(Tempfiles, unittest.TestCase):
    @given(BODIES, st.integers(0, 220))
    def test_it_never_moves_forward(self, body: bytes, offset: int) -> None:
        """Rewinding re-publishes, which a peer's dedup absorbs. Advancing
        skips, which loses a record -- so the direction is not symmetric and
        this is the half worth pinning."""
        path = self.written(body)
        self.assertLessEqual(store.line_start(path, offset), offset)

    @given(BODIES, st.integers(0, 220))
    def test_it_lands_on_a_boundary(self, body: bytes, offset: int) -> None:
        """Zero, or immediately after a newline. Anything else is the #263
        failure with extra steps."""
        path = self.written(body)
        mark = store.line_start(path, offset)
        if mark > 0:
            self.assertEqual(body[mark - 1 : mark], b"\n")

    @given(BODIES, st.integers(0, 220))
    def test_it_rewinds_to_the_nearest_boundary(self, body: bytes, offset: int) -> None:
        """Not merely *a* boundary -- the closest one at or before the mark.

        Rewinding further is still safe, which is why "lands on a boundary"
        cannot see the difference: it re-publishes records that were already
        sealed, and the peer's dedup absorbs them. It is a cost rather than a
        bug, and the cost is paid in a chunk committed to an append-only
        repository, so it is worth pinning exactly.

        Exact only because these bodies are far shorter than `_LINE_WINDOW`; a
        log with no newline in the last 64 KiB is defined to start over, and
        this format cannot produce one.
        """
        path = self.written(body)
        if offset <= 0:
            return
        self.assertEqual(store.line_start(path, offset), body.rfind(b"\n", 0, offset) + 1)

    @given(BODIES, st.integers(0, 220))
    def test_it_is_idempotent(self, body: bytes, offset: int) -> None:
        """`export` calls it on every grown file on a one-minute timer, so a
        watermark it already rewound must be left alone."""
        path = self.written(body)
        once = store.line_start(path, offset)
        self.assertEqual(store.line_start(path, once), once)

    @given(st.lists(RECORD, max_size=8))
    def test_a_watermark_already_on_a_boundary_does_not_move(self, records: list[bytes]) -> None:
        """The case that always happens, and the one the single-byte read
        exists for: an ordinary sync must not pay to rediscover a boundary."""
        body = joined(records)
        path = self.written(body)
        mark = 0
        for line in records:
            self.assertEqual(store.line_start(path, mark), mark)
            mark += len(line) + 1


class ExportPublishesWholeRecords(RuleBasedStateMachine):
    """A log being appended to, forgotten from, and exported, in any order.

    The machine models the three things that touch a log file and the one thing
    that reads it, including the interleaving #263 is about: `forget` shortens
    the file, the watermark is *not* updated -- the crash -- and the next
    `export` runs against a mark that is now past the middle of a record.

    Two claims, and only the second is absolute. A record may be published more
    than once, because rewinding re-publishes and that is the direction chosen
    to be wrong in. A record may never be published in pieces.
    """

    def __init__(self) -> None:
        super().__init__()
        box = tempfile.TemporaryDirectory(prefix="woswoar-export-machine-")
        self.box = box
        self.path = Path(box.name) / "log"
        self.path.write_bytes(b"")
        self.mark = 0
        #: Every record the file has ever held, by its exact bytes. A fragment
        #: is a byte string that is not one of these, which is how the invariant
        #: recognises a half-published record without knowing where it was cut.
        #:
        #: That only works because a record's *tail* can never be another
        #: record -- see `append` for the tag that guarantees it. Without one,
        #: `b"\x00"` and `b"\x00\x00"` are both records Hypothesis will
        #: generate, the second cut in half is the first, and the invariant
        #: passes on a published fragment. That is a fixture that cannot tell
        #: the two answers apart, which CLAUDE.md rule 3 says to suspect before
        #: the code.
        self.ever: set[bytes] = set()
        self.made = 0
        self.live: list[bytes] = []
        #: Bytes on disk with no newline after them yet. Whatever is appended
        #: next joins them into a single line, because that is what the file
        #: physically holds -- see `append`.
        self.pending = b""
        self.published: list[bytes] = []

    def teardown(self) -> None:
        self.box.cleanup()

    @rule(payloads=st.lists(PAYLOAD, min_size=1, max_size=3))
    def append(self, payloads: list[bytes]) -> None:
        # Each record is tagged with a number no other record carries, and no
        # payload may contain the ``<`` that opens a tag. A proper suffix of a
        # record therefore holds no tag but the one it was cut out of, so it can
        # equal no other record -- which is what lets the invariant below say
        # "not a record" and mean "a fragment".
        records = []
        for payload in payloads:
            self.made += 1
            records.append(b"<%d>%s" % (self.made, payload))
        # The subtlety this machine got wrong first. A shell killed before its
        # newline leaves bytes that the *next* append runs straight into, so the
        # file holds one line made of both -- and exporting that whole line is
        # correct, not a published fragment. woswoar's answer to the resulting
        # torn record is `parse_line` returning None for it, one layer up.
        rows = [self.pending + records[0], *records[1:]]
        self.pending = b""
        self.ever.update(rows)
        self.live.extend(rows)
        with self.path.open("ab") as out:
            out.write(joined(records))

    @rule(tail=PAYLOAD)
    def half_write(self, tail: bytes) -> None:
        """A shell killed between the write and its newline."""
        # Tagless too, so a torn tail cannot forge a record boundary.
        if not tail:
            return
        self.pending += tail
        with self.path.open("ab") as out:
            out.write(tail)

    @rule(which=st.integers(0, 20))
    def forget(self, which: int) -> None:
        """Shorten the log without touching the watermark -- the crash in #263.

        Whole records only, because that is what `forget.surviving` writes; the
        damage is entirely in the watermark left behind.
        """
        if not self.live:
            return
        del self.live[which % len(self.live)]
        # The rewrite emits whole records, so any torn tail goes with it.
        self.pending = b""
        self.path.write_bytes(joined(self.live))

    @rule()
    def export(self) -> None:
        mark = store.line_start(self.path, self.mark)
        data, self.mark = store.read_tail(self.path, mark)
        if data:
            self.published.extend(data.split(b"\n")[:-1])

    @invariant()
    def nothing_is_published_in_halves(self) -> None:
        for row in self.published:
            # A machine is not a TestCase, so this is a bare assert: `assertIn`
            # would be an AttributeError reported as the property failing.
            assert row in self.ever, f"a fragment reached a chunk: {row!r}"

    @invariant()
    def the_watermark_never_names_a_partial_record(self) -> None:
        body = self.path.read_bytes()
        mark = store.line_start(self.path, self.mark)
        assert mark == 0 or body[mark - 1 : mark] == b"\n", "watermark inside a record"


ExportPublishesWholeRecords.TestCase.settings = settings(
    max_examples=200, stateful_step_count=20, deadline=None
)
TestExportPublishesWholeRecords = ExportPublishesWholeRecords.TestCase
# `run_tests` loads a class by ``f"{__module__}.{__qualname__}"``, and Hypothesis
# builds this one inside `hypothesis.stateful` -- so without these it looks for
# it there and cannot find it.
TestExportPublishesWholeRecords.__name__ = "TestExportPublishesWholeRecords"
TestExportPublishesWholeRecords.__qualname__ = "TestExportPublishesWholeRecords"
TestExportPublishesWholeRecords.__module__ = __name__


if __name__ == "__main__":
    unittest.main()
