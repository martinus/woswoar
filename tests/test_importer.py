"""Importing existing bash and zsh histories."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from woswoar import cache, importer

from .support import WoswoarTestCase

MTIME = 1_700_000_000


class TestParseBash(unittest.TestCase):
    def test_with_histtimeformat(self) -> None:
        text = "#1753000000\ngit status\n#1753000100\nninja -C build\n"
        parsed = importer.parse_bash(text, MTIME)
        self.assertEqual(
            [(p.ts, p.cmd) for p in parsed],
            [(1753000000, "git status"), (1753000100, "ninja -C build")],
        )

    def test_without_timestamps_preserves_order(self) -> None:
        parsed = importer.parse_bash("first\nsecond\nthird\n", MTIME)
        self.assertEqual([p.cmd for p in parsed], ["first", "second", "third"])
        # Order is what ranking depends on; the absolute values are synthetic.
        self.assertEqual([p.ts for p in parsed], sorted(p.ts for p in parsed))
        self.assertEqual(parsed[-1].ts, MTIME)

    def test_mixed_timestamped_and_bare_lines(self) -> None:
        parsed = importer.parse_bash("#1753000000\ntimed\nbare\n", MTIME)
        self.assertEqual([p.cmd for p in parsed], ["timed", "bare"])
        self.assertEqual(parsed[0].ts, 1753000000)
        self.assertEqual(parsed[1].ts, MTIME)

    def test_blank_lines_ignored(self) -> None:
        self.assertEqual(len(importer.parse_bash("a\n\n\nb\n", MTIME)), 2)

    def test_a_comment_is_not_mistaken_for_a_timestamp(self) -> None:
        parsed = importer.parse_bash("#!/bin/sh\n#not-a-number\n", MTIME)
        self.assertEqual([p.cmd for p in parsed], ["#!/bin/sh", "#not-a-number"])


class TestParseZsh(unittest.TestCase):
    def test_extended_format(self) -> None:
        text = ": 1753000000:0;git status\n: 1753000100:12;make -j8\n"
        parsed = importer.parse_zsh(text, MTIME)
        self.assertEqual(
            [(p.ts, p.cmd, p.duration_ms) for p in parsed],
            [(1753000000, "git status", -1), (1753000100, "make -j8", 12000)],
        )

    def test_backslash_continuation_is_one_command(self) -> None:
        text = ": 1753000000:0;for i in 1 2; do\\\ntrue\\\ndone\n"
        parsed = importer.parse_zsh(text, MTIME)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].cmd, "for i in 1 2; do\ntrue\ndone")

    def test_plain_history_without_headers(self) -> None:
        parsed = importer.parse_zsh("git status\nls -la\n", MTIME)
        self.assertEqual([p.cmd for p in parsed], ["git status", "ls -la"])

    def test_output_is_sorted_by_time(self) -> None:
        text = ": 1753000100:0;later\n: 1753000000:0;earlier\n"
        parsed = importer.parse_zsh(text, MTIME)
        self.assertEqual([p.cmd for p in parsed], ["earlier", "later"])


class TestImportRun(WoswoarTestCase):
    def _source(self, name: str, content: str) -> Path:
        path = self.root / name
        return self._rewrite(path, content)

    @staticmethod
    def _rewrite(path: Path, content: str) -> Path:
        # Pin the mtime: synthesised timestamps are derived from it, so letting
        # it drift would make these tests depend on wall-clock time.
        path.write_text(content, encoding="utf-8")
        os.utime(path, (MTIME, MTIME))
        return path

    def _commands(self) -> set[str]:
        return {e.cmd for e in cache.load_entries()}

    def test_imports_and_is_visible_to_search(self) -> None:
        source = self._source("hist", "#1753000000\ngit status\n#1753000100\nls\n")
        result = importer.run("bash", source)
        self.assertEqual(result.imported, 2)
        self.assertEqual(self._commands(), {"git status", "ls"})

    def test_rerun_imports_nothing_new(self) -> None:
        source = self._source("hist", "#1753000000\ngit status\n")
        importer.run("bash", source)
        again = importer.run("bash", source)
        self.assertEqual(again.imported, 0)
        self.assertEqual(len(cache.load_entries()), 1)

    def test_rerun_picks_up_appended_commands(self) -> None:
        source = self._source("hist", "#1753000000\ngit status\n")
        importer.run("bash", source)

        self._rewrite(source, "#1753000000\ngit status\n#1753000100\nnew command\n")
        result = importer.run("bash", source)
        self.assertEqual(result.imported, 1)
        self.assertEqual(len(cache.load_entries()), 2)

    def test_untimed_rerun_does_not_duplicate(self) -> None:
        # The hard case: synthesised timestamps shift when the source grows, so
        # a (ts, cmd) check alone would re-import everything. The per-source
        # count is what makes this work.
        source = self._source("hist", "one\ntwo\n")
        importer.run("bash", source)

        self._rewrite(source, "one\ntwo\nthree\n")
        result = importer.run("bash", source)
        self.assertEqual(result.imported, 1)
        # load_entries() groups per file rather than sorting globally; ordering
        # is search.rank()'s job, so compare as a set.
        self.assertEqual(self._commands(), {"one", "two", "three"})

    def test_truncated_source_reimports_without_duplicating(self) -> None:
        source = self._source("hist", "#1753000000\na\n#1753000100\nb\n")
        importer.run("bash", source)

        # Log rotation: the count is now meaningless, so the (ts, cmd) guard has
        # to carry it.
        self._rewrite(source, "#1753000100\nb\n")
        result = importer.run("bash", source)
        self.assertEqual(result.imported, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(len(cache.load_entries()), 2)

    def test_dry_run_writes_nothing(self) -> None:
        source = self._source("hist", "#1753000000\ngit status\n")
        result = importer.run("bash", source, dry_run=True)
        self.assertEqual(result.imported, 1)
        self.assertEqual(cache.load_entries(), [])

    def test_missing_file_is_reported_clearly(self) -> None:
        with self.assertRaises(FileNotFoundError):
            importer.run("bash", self.root / "nope")

    def test_invalid_utf8_does_not_abort_the_import(self) -> None:
        path = self.root / "hist"
        path.write_bytes(b"#1753000000\nls \xff\xfe caf\xc3\xa9\n")
        os.utime(path, (MTIME, MTIME))
        result = importer.run("bash", path)
        self.assertEqual(result.imported, 1)
        self.assertIn("café", cache.load_entries()[0].cmd)

    def test_commands_with_tabs_survive_the_import(self) -> None:
        source = self._source("hist", "#1753000000\nawk -F'\t' '{print $1}'\n")
        importer.run("bash", source)
        self.assertEqual(cache.load_entries()[0].cmd, "awk -F'\t' '{print $1}'")


if __name__ == "__main__":
    unittest.main()
