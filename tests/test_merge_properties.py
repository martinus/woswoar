"""`_Day`: the decision that turns a peer's chunks into a log file.

Scope first, because the honest name for this file is narrower than "merge
convergence". `sync.merge` runs `git`, `age` and signature verification, and a
Hypothesis machine over the real thing would be thousands of subprocesses per
run. What is here is the piece that actually decides what a day's file ends up
containing: `_Day`, which settles "is this day being rewritten" once, holds the
plaintext, and writes it either as an append or as one atomic replacement.

That narrowing is not a way of avoiding the hard part -- it *is* the hard part.
Both of the failures its comments record were decided here and were silent:

- a single chunk appended after a compaction "rewrote the day down to just that
  chunk and discarded everything before it, on every peer, silently"
- a rewrite that ran on a partial read, where "five commands became one, and the
  lines it lost were in that chunk and nowhere else"

Neither is visible in the file afterwards. A day that lost four of five lines is
a perfectly well-formed log, and the machine that wrote it has already recorded
the chunks as merged, so it will never read them again. That is what makes this
worth a property rather than an example: the failure has no symptom to assert
on, only an invariant to violate.

What is *not* covered, and should be said plainly: end-to-end convergence across
peers, signature verification, and the `refused` guard in `_merge_host` that
decides whether `flush` is called at all. This tests what `_Day` guarantees to
that caller, not the caller.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from woswoar import manifest, store, sync

settings.register_profile("woswoar-merge", max_examples=300, deadline=None)
settings.load_profile("woswoar-merge")

HOST = "aaaabbbbccccdddd"
DAY = "2026/08/21"

#: Chunk names, as the manifest holds them.
NAME = st.builds("chunk-{}".format, st.integers(0, 5))

#: A chunk's decrypted plaintext: whole lines, since that is what a sealed chunk
#: holds and what `_line_count` counts.
BLOCK = st.lists(
    st.binary(max_size=12).map(lambda row: row.replace(b"\n", b"x")), min_size=1, max_size=4
).map(lambda rows: b"".join(row + b"\n" for row in rows))


def listing(names: list[str], compacted: list[str]) -> dict[str, manifest.ManifestEntry]:
    """A manifest listing, where ``compacted`` names carry a `subsumes`."""
    return {
        # The name is the dict key, not a field: `ManifestEntry` is the *value*
        # side of the listing.
        name: manifest.ManifestEntry(
            digest="0" * 64,
            subsumes=("older",) if name in compacted else (),
        )
        for name in names
    }


class DayCase(unittest.TestCase):
    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="woswoar-merge-prop-")
        self.addCleanup(box.cleanup)
        root = Path(box.name).resolve()
        (root / "home").mkdir()
        patched = mock.patch.dict(
            os.environ, store.sandbox_environ(root, root / "home"), clear=True
        )
        patched.start()
        self.addCleanup(patched.stop)

    def path(self) -> Path:
        return store.log_file(HOST, DAY)

    def existing(self, body: bytes) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def content(self) -> bytes:
        try:
            return self.path().read_bytes()
        except OSError:
            return b""


class TestWhetherTheDayIsRewritten(DayCase):
    """`rewrite` is settled in `__init__` and never revisited, which is the
    whole reason the class exists. So it has to be a function of what is known
    then, and of nothing else."""

    @given(st.lists(NAME, max_size=4), st.lists(NAME, max_size=4), st.lists(NAME, max_size=4))
    def test_it_is_exactly_the_two_documented_reasons(
        self, names: list[str], compacted: list[str], merged: list[str]
    ) -> None:
        """A compacted chunk this machine has not taken in, or a manifest that
        went backwards. Nothing else, and in particular not "the day has a
        compacted chunk at all" -- that reading rebuilt the whole day on every
        sync, which the comment measures as quadratic over a day."""
        listed = listing(names, compacted)
        already = frozenset(merged)
        day = sync._Day(HOST, DAY, listed, already)
        expected = bool(
            ({n for n in listed if listed[n].subsumes} - already) or (already - set(listed))
        )
        self.assertEqual(day.rewrite, expected)

    @given(st.lists(NAME, min_size=1, max_size=4), st.lists(NAME, min_size=1, max_size=4))
    def test_a_compaction_already_merged_does_not_rebuild_again(
        self, names: list[str], compacted: list[str]
    ) -> None:
        """The second reading of the same fact, and the expensive one. Once the
        compacted chunk is in `already`, the day must stop rewriting -- a
        `subsumes` stays in the signed manifest for the life of the day."""
        listed = listing(names, compacted)
        already = frozenset(listed)
        self.assertFalse(sync._Day(HOST, DAY, listed, already).rewrite)


class TestWhatReachesTheFile(DayCase):
    @given(st.lists(BLOCK, min_size=1, max_size=4), BLOCK, st.booleans())
    def test_an_append_keeps_what_was_already_there(
        self, blocks: list[bytes], before: bytes, eager: bool
    ) -> None:
        """Both flush schedules. An append *may* be written in pieces as it
        goes, and the result has to be the same bytes either way -- that is
        what makes flushing early safe for an append and not for a rewrite."""
        self.existing(before)
        day = sync._Day(HOST, DAY, listing(["chunk-0"], []), frozenset())
        self.assertFalse(day.rewrite)
        report = sync.Report()
        with mock.patch.object(sync, "FLUSH_BYTES", 0 if eager else sync.FLUSH_BYTES):
            for block in blocks:
                day.add(block, report)
            day.flush(report)
        self.assertEqual(self.content(), before + b"".join(blocks))

    @given(st.lists(BLOCK, min_size=1, max_size=4), BLOCK)
    def test_a_rewrite_replaces_the_day_with_everything_it_was_given(
        self, blocks: list[bytes], before: bytes
    ) -> None:
        """Everything it was given -- the caller's job is to give it every
        chunk the manifest lists. Rebuilding from the subset that happened to
        be new is the failure the class comment records, and it is a property
        of the *caller*; what `_Day` owes is that nothing it was handed is
        dropped and nothing else survives."""
        self.existing(before)
        day = sync._Day(HOST, DAY, listing(["chunk-0"], ["chunk-0"]), frozenset())
        self.assertTrue(day.rewrite)
        report = sync.Report()
        for block in blocks:
            day.add(block, report)
        day.flush(report)
        self.assertEqual(self.content(), b"".join(blocks))

    @given(st.lists(BLOCK, min_size=1, max_size=6))
    def test_a_rewriting_day_touches_nothing_until_it_is_flushed(self, blocks: list[bytes]) -> None:
        """The invariant that makes `_merge_host`'s refusal able to protect
        anything. A rewrite is one atomic replacement, so a partial read must
        still be able to abandon it -- and it can only abandon what has not
        been written. `add` flushes early on `FLUSH_BYTES`, and the guard that
        keeps it from doing so here is `not self.rewrite`.

        `FLUSH_BYTES` is lowered to zero for the duration, and without that
        this property asserts nothing: the real value is 8 MiB, these blocks
        are a few dozen bytes, so the early-flush branch it is about never
        runs. It was written that way first and the mutation removing the
        guard survived it. The constant says of itself that it is "not a
        correctness bound", which is what makes moving it legitimate -- the
        branch it guards is the same branch either way.
        """
        self.existing(b"keep me\n")
        day = sync._Day(HOST, DAY, listing(["chunk-0"], ["chunk-0"]), frozenset())
        report = sync.Report()
        with mock.patch.object(sync, "FLUSH_BYTES", 0):
            for block in blocks:
                day.add(block, report)
                self.assertEqual(self.content(), b"keep me\n", "a rewrite wrote before its flush")

    @given(BLOCK)
    def test_flushing_nothing_leaves_the_day_alone(self, before: bytes) -> None:
        """A day whose manifest verified but whose chunks all failed to open
        reaches `flush` with no blocks. Replacing the file with nothing there
        would delete a day this machine already held."""
        self.existing(before)
        day = sync._Day(HOST, DAY, listing(["chunk-0"], ["chunk-0"]), frozenset())
        self.assertTrue(day.rewrite)
        day.flush(sync.Report())
        self.assertEqual(self.content(), before)


class TestWhatIsReported(DayCase):
    @given(st.lists(BLOCK, min_size=1, max_size=4), BLOCK)
    def test_a_rewrite_reports_only_the_growth(self, blocks: list[bytes], before: bytes) -> None:
        """ "A peer that already held the whole day and then received its
        compaction gained nothing, and saying it merged five thousand lines
        would be a lie in the reassuring direction." So the count is the growth
        and never the payload."""
        self.existing(before)
        day = sync._Day(HOST, DAY, listing(["chunk-0"], ["chunk-0"]), frozenset())
        report = sync.Report()
        for block in blocks:
            day.add(block, report)
        day.flush(report)
        growth = self.content().count(b"\n") - before.count(b"\n")
        self.assertEqual(report.lines_imported, max(0, growth))

    @given(st.lists(BLOCK, min_size=1, max_size=4), BLOCK)
    def test_an_append_reports_what_it_appended(self, blocks: list[bytes], before: bytes) -> None:
        self.existing(before)
        day = sync._Day(HOST, DAY, listing(["chunk-0"], []), frozenset())
        report = sync.Report()
        for block in blocks:
            day.add(block, report)
        day.flush(report)
        self.assertEqual(report.lines_imported, b"".join(blocks).count(b"\n"))


if __name__ == "__main__":
    unittest.main()
