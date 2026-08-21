"""The two round trips that carry a command into `logs/`, and the split above them.

`logs/` is the primary copy, and everything in it went through `escape` and
`format_line` on the way in and comes back through `unescape` and `parse_line`.
A command that does not survive that pair is a command the user cannot search
for, and nothing else in the system would notice -- the line is still
well-formed, it just says something other than what was typed.

That is the shape a property test is for. The examples beside these pin the
cases someone thought of: a tab, a newline, a backslash. This asks the same
question of text nobody chose, including the combinations that only look
interesting once you see them fail.
"""

from __future__ import annotations

import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from woswoar import codec, entry

#: Deliberately the whole of text. Commands arrive from a shell, so they hold
#: whatever a person typed or a script generated: tabs, which are the field
#: separator; newlines, which are the record separator; backslashes, which are
#: the escape character; lone surrogates, which are what a mis-decoded byte
#: becomes. Narrowing this to "printable ASCII" would be choosing the inputs
#: that already work.
TEXT = st.text()

#: 400 rather than the default 100: at the default this file passed, and the
#: unescaped `session` field only fell out on a longer search. Cheap enough to
#: keep -- the whole module runs in about two seconds.
settings.register_profile("woswoar", max_examples=400, deadline=None)
settings.load_profile("woswoar")

ENTRIES = st.builds(
    entry.Entry,
    ts=st.integers(min_value=0, max_value=2**40),
    host=st.text(min_size=1, max_size=16),
    session=TEXT,
    cwd=TEXT,
    exit_code=st.integers(min_value=-(2**31), max_value=2**31),
    duration_ms=st.integers(min_value=-1, max_value=2**40),
    cmd=TEXT,
)


class TestEscaping(unittest.TestCase):
    @given(TEXT)
    def test_a_round_trip_returns_what_went_in(self, text: str) -> None:
        self.assertEqual(entry.unescape(entry.escape(text)), text)

    @given(TEXT)
    def test_an_escaped_value_holds_neither_separator(self, text: str) -> None:
        """The point of escaping, stated as the property it exists for: a value
        may contain a tab or a newline, and a *field* may not, or the record
        splits into the wrong number of pieces."""
        escaped = entry.escape(text)
        self.assertNotIn("\t", escaped)
        self.assertNotIn("\n", escaped)


class TestTruncation(unittest.TestCase):
    @given(TEXT)
    def test_it_is_idempotent(self, cmd: str) -> None:
        """`_dedup_key` truncates before comparing, so a key built from an
        already-truncated command has to equal the key built from the original.
        Without this, a long command re-imports on every run -- which is the bug
        `_dedup_key`'s docstring records, found on a real atuin database."""
        once = entry.truncate(cmd)
        self.assertEqual(entry.truncate(once), once)


class TestTheLogLine(unittest.TestCase):
    @given(ENTRIES)
    def test_a_record_survives_the_round_trip(self, original: entry.Entry) -> None:
        """Everything but the command, which truncation may shorten -- and the
        host, which is not in the line at all: it comes from the directory the
        file sits in, so `parse_line` is told rather than reading it."""
        line = entry.format_line(original)
        back = entry.parse_line(line, original.host)
        assert back is not None, f"a line we wrote did not parse: {line!r}"
        self.assertEqual(back.ts, original.ts)
        self.assertEqual(back.session, original.session)
        self.assertEqual(back.cwd, original.cwd)
        self.assertEqual(back.exit_code, original.exit_code)
        self.assertEqual(back.duration_ms, original.duration_ms)
        self.assertEqual(back.cmd, entry.truncate(original.cmd))

    @given(ENTRIES)
    def test_a_record_is_one_line(self, original: entry.Entry) -> None:
        """A log is newline-delimited, so a record holding a newline would be
        read back as two -- one of them malformed and dropped, the other a
        command the user never ran."""
        self.assertNotIn("\n", entry.format_line(original))


class TestChunkBytes(unittest.TestCase):
    @given(st.binary(max_size=4096))
    def test_pack_round_trips(self, data: bytes) -> None:
        self.assertEqual(codec.unpack(codec.pack(data)), data)

    @given(st.binary(max_size=2048), st.integers(min_value=1, max_value=64))
    def test_the_pieces_rebuild_the_whole(self, data: bytes, limit: int) -> None:
        """`split_for_export` is size-bounded chunking, and the bound is the
        easy half. Losing or duplicating a byte at a boundary is the half that
        would put a mangled record in the archive for ever, since chunks are
        append-only and never rewritten."""
        self.assertEqual(b"".join(codec.split_for_export(data, limit)), data)

    @given(st.binary(max_size=2048), st.integers(min_value=1, max_value=64))
    def test_every_piece_but_the_last_ends_a_line(self, data: bytes, limit: int) -> None:
        """A chunk is parsed line by line, so a piece cut mid-record would be
        dropped by one reader and never seen by any. The last piece is exempt
        only because the data itself may not end in a newline."""
        pieces = list(codec.split_for_export(data, limit))
        for piece in pieces[:-1]:
            self.assertTrue(piece.endswith(b"\n"), f"cut mid-record: {piece!r}")

    @given(st.binary(max_size=2048), st.integers(min_value=1, max_value=64))
    def test_no_piece_is_empty(self, data: bytes, limit: int) -> None:
        """An empty chunk is a file, a seal and a manifest entry that carry
        nothing -- and the loop that produces them is the kind that can emit one
        for ever."""
        for piece in codec.split_for_export(data, limit):
            self.assertTrue(piece, "an empty piece would be a chunk holding nothing")


if __name__ == "__main__":
    unittest.main()
