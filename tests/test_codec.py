"""The chunk codec: compression in, and a refusal on the way out.

Its own file since #214 lifted `woswoar/codec.py` out of `sync.py`, and that is
the point of the slice rather than a side effect: every test here is `zlib` and
two bounds, with no sandbox, no age and no git, where reaching the same
behaviour used to mean driving `sync.run()`.

What stays in `tests/test_sync.py` is the half that needs a repository -- a bomb
arriving in a real chunk, and the flushing that keeps a host's plaintext from
being held all at once. Those are about what `merge` does with a refusal, not
about the refusal.
"""

from __future__ import annotations

import tracemalloc
import unittest
import zlib

from woswoar import codec


class TestChunkPayload(unittest.TestCase):
    """The chunk encoding, without needing age or git."""

    def test_round_trip(self) -> None:
        for data in (b"", b"x", b"a line\n", b"repeated line\n" * 500, bytes(range(256))):
            self.assertEqual(codec.unpack(codec.pack(data)), data)

    def test_repetitive_input_shrinks(self) -> None:
        # Shell history is repetitive by nature, and this is the one moment it
        # can be compressed: once sealed, ciphertext is incompressible forever.
        data = b"1700000000\ts1\t~/src\t0\t5\tgit status\n" * 200
        self.assertLess(len(codec.pack(data)), len(data) // 10)

    def test_a_tiny_chunk_costs_only_a_few_bytes(self) -> None:
        """Deflate has a floor, and one very short line can land above it.

        This is what `pack` used to avoid by tagging each payload raw-or-
        deflated and keeping the smaller. It was not worth it: the tag cost a
        byte on every chunk that *did* compress, which is all but the shortest,
        and the worst case it bought back is the handful of bytes below --
        against a sealed chunk that already carries a 200-byte age header.
        """
        data = b"1700000000\ts1\t~\t0\t5\tls\n"
        self.assertLess(len(codec.pack(data)) - len(data), 8)

    def test_a_payload_we_cannot_read_is_refused(self) -> None:
        # Loudly, rather than decoding into garbage that then gets appended to
        # a log file and cached. `_merge_host` turns this into an unreadable
        # chunk rather than letting it abort the sync.
        truncated = zlib.compress(b"x" * 5000)[:20]
        for blob in (b"\x7fanything", b"", b"1700000000\ts1\t~\t0\t5\tls\n", truncated):
            with self.assertRaises(zlib.error):
                codec.unpack(blob)


class TestSplitForExport(unittest.TestCase):
    """The splitter on its own, where the edges are cheap to state."""

    def test_it_is_lossless_and_line_aligned(self) -> None:
        data = b"".join(b"line %d\n" % i for i in range(50))
        for limit in (1, 7, 8, 20, 1000):
            with self.subTest(limit=limit):
                pieces = list(codec.split_for_export(data, limit))
                self.assertEqual(b"".join(pieces), data)
                for piece in pieces:
                    self.assertTrue(piece.endswith(b"\n"))

    def test_a_tail_that_fits_is_one_piece(self) -> None:
        data = b"one\ntwo\n"
        self.assertEqual(list(codec.split_for_export(data, 1000)), [data])

    def test_a_line_longer_than_the_budget_is_kept_whole(self) -> None:
        """Splitting a record would drop it from every reader, not just one."""
        long_line = b"y" * 100 + b"\n"
        pieces = list(codec.split_for_export(long_line + b"short\n", 10))
        self.assertEqual(pieces[0], long_line)
        self.assertEqual(b"".join(pieces), long_line + b"short\n")

    def test_the_budget_is_under_what_a_reader_accepts(self) -> None:
        """The invariant the whole change exists for, stated once."""
        self.assertLess(codec.MAX_EXPORT_BYTES, codec.MAX_CHUNK_BYTES)


class TestADecompressionBomb(unittest.TestCase):
    """One machine must not decide how much memory the others spend.

    deflate reaches about 1030:1, so an unbounded decompress turned a 204 KB
    commit into 200 MiB of log and 420 MiB of RSS -- on a timer that fires every
    minute and asks nobody. Signing (#38) means the chunk has to come from a
    machine you accepted, which is why this is not a P0; it is still one
    compromised machine against every other one.
    """

    def test_a_bomb_is_refused_rather_than_expanded(self) -> None:
        bomb = zlib.compress(b"\n" * (codec.MAX_CHUNK_BYTES * 2), 9)
        self.assertLess(len(bomb), 1024 * 1024, "the fixture should be small to be a bomb")
        with self.assertRaises(zlib.error):
            codec.unpack(bomb)

    def test_the_boundary_is_where_it_says_it_is(self) -> None:
        """Exactly at the cap is legitimate; one byte past it is not.

        An off-by-one here either refuses a chunk a machine legitimately sent or
        leaves the last doubling unbounded, and neither shows up in a test that
        only tries a 200 MiB bomb.
        """
        limit = codec.MAX_CHUNK_BYTES
        self.assertEqual(len(codec.unpack(zlib.compress(b"x" * limit, 9))), limit)
        with self.assertRaises(zlib.error):
            codec.unpack(zlib.compress(b"x" * (limit + 1), 9))

    def test_the_refusal_costs_a_bounded_allocation(self) -> None:
        """`decompressobj` stops at the limit; `decompress` allocates first.

        Refusing *after* materialising the payload would leave the memory
        exhaustion in place and merely decline to write the file, so this
        measures what `codec.unpack` actually allocates rather than asserting a
        property of the standard library -- which is what it did first, and
        which passed perfectly well with the fix reverted.
        """
        bomb = zlib.compress(b"\n" * (codec.MAX_CHUNK_BYTES * 4), 9)
        tracemalloc.start()
        try:
            with self.assertRaises(zlib.error):
                codec.unpack(bomb)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # The bomb expands to four times the cap, so unbounded this peaks at
        # 256 MiB; bounded it peaks around 128 MiB -- the 64 MiB buffer plus the
        # doubling zlib does while filling it. Three times the cap sits in the
        # gap with room on both sides, which a tighter bound did not.
        self.assertLess(peak, codec.MAX_CHUNK_BYTES * 3)
