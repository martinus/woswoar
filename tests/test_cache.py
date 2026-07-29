"""Incremental cache behaviour, especially the invalidation paths."""

from __future__ import annotations

import os
import unittest

from woswoar import cache, store
from woswoar.entry import format_line

from .support import MACHINE_ID, WoswoarTestCase, make_entry


def line(ts: int, cmd: str) -> str:
    """One log line, using the shared fixture defaults."""
    return format_line(make_entry(ts, cmd))


def bump_mtime(path: object) -> None:
    """Push mtime forward so a same-size rewrite is still detectable.

    Filesystem timestamp granularity means two writes in the same test can land
    on an identical mtime, which would mask a change the code is supposed to
    catch.
    """
    stat = os.stat(path)  # type: ignore[arg-type]
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))  # type: ignore[arg-type]


class TestCache(WoswoarTestCase):
    def test_builds_from_empty(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        entries = cache.load_entries()
        self.assertEqual([e.cmd for e in entries], ["git status"])

    def test_no_logs_is_not_an_error(self) -> None:
        self.assertEqual(cache.load_entries(), [])

    def test_reads_only_the_appended_tail(self) -> None:
        path = self.write_log(MACHINE_ID, "2026-07-29", [line(100, "first")])
        self.assertEqual(len(cache.load_entries()), 1)

        with path.open("a", encoding="utf-8") as handle:
            handle.write(line(200, "second") + "\n")

        loaded = cache.load()
        before = dict(loaded.meta)
        self.assertTrue(cache.refresh(loaded))
        self.assertEqual([e.cmd for e in loaded.entries()], ["first", "second"])
        # The offset advanced rather than resetting, i.e. this was a tail read.
        relpath = f"hosts/{MACHINE_ID}/2026-07-29.tsv"
        self.assertGreater(loaded.meta[relpath].offset, before[relpath].offset)

    def test_unchanged_file_is_not_reparsed(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()
        loaded = cache.load()
        self.assertFalse(cache.refresh(loaded))

    def test_shrunk_file_is_fully_reread(self) -> None:
        path = self.write_log(
            MACHINE_ID, "2026-07-29", [line(100, "first"), line(200, "second"), line(300, "third")]
        )
        self.assertEqual(len(cache.load_entries()), 3)

        # A rebase or a manual edit can make a file shorter. Trusting the stored
        # byte offset here would parse from the middle of a line.
        path.write_text(line(100, "first") + "\n", encoding="utf-8")
        self.assertEqual([e.cmd for e in cache.load_entries()], ["first"])

    def test_same_size_rewrite_is_detected(self) -> None:
        path = self.write_log(MACHINE_ID, "2026-07-29", [line(100, "aaaa")])
        self.assertEqual([e.cmd for e in cache.load_entries()], ["aaaa"])

        path.write_text(line(100, "bbbb") + "\n", encoding="utf-8")
        bump_mtime(path)
        # Size is identical, so only the head fingerprint can catch this.
        self.assertEqual([e.cmd for e in cache.load_entries()], ["bbbb"])

    def test_partial_trailing_line_is_not_consumed_until_complete(self) -> None:
        path = self.write_log(MACHINE_ID, "2026-07-29", [line(100, "complete")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("1753781234\tsess\t/tmp")  # no newline yet

        self.assertEqual([e.cmd for e in cache.load_entries()], ["complete"])

        with path.open("a", encoding="utf-8") as handle:
            handle.write("\t0\t5\tfinished\n")

        self.assertEqual([e.cmd for e in cache.load_entries()], ["complete", "finished"])

    def test_deleted_file_drops_its_entries(self) -> None:
        path = self.write_log(MACHINE_ID, "2026-07-29", [line(100, "gone")])
        self.write_log(MACHINE_ID, "2026-07-30", [line(200, "stays")])
        self.assertEqual(len(cache.load_entries()), 2)

        path.unlink()
        self.assertEqual([e.cmd for e in cache.load_entries()], ["stays"])

    def test_corrupt_cache_falls_back_to_a_rebuild(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()

        store.cache_file().write_bytes(b"this is not a pickle")
        self.assertEqual([e.cmd for e in cache.load_entries()], ["git status"])

    def test_stale_version_falls_back_to_a_rebuild(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()

        stale = cache.load()
        stale.version = cache.CACHE_VERSION + 1
        cache.save(stale)

        self.assertEqual([e.cmd for e in cache.load_entries()], ["git status"])

    def test_unparseable_lines_are_skipped_not_fatal(self) -> None:
        self.write_log(
            MACHINE_ID, "2026-07-29", [line(100, "good"), "garbage", line(200, "also good")]
        )
        self.assertEqual([e.cmd for e in cache.load_entries()], ["good", "also good"])

    def test_host_is_derived_from_the_path(self) -> None:
        self.write_log("aaaaaaaaaaaaaaaa", "2026-07-29", [line(100, "on a")])
        self.write_log("bbbbbbbbbbbbbbbb", "2026-07-29", [line(200, "on b")])
        hosts = {e.cmd: e.host for e in cache.load_entries()}
        self.assertEqual(hosts, {"on a": "aaaaaaaaaaaaaaaa", "on b": "bbbbbbbbbbbbbbbb"})

    def test_save_is_atomic_and_leaves_no_temp_files(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()
        leftovers = [p.name for p in store.cache_dir().iterdir() if p.name.startswith(".cache-")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
