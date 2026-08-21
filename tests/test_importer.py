"""Importing existing bash and zsh histories."""

from __future__ import annotations

import io
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from woswoar import cache, importer, store
from woswoar.__main__ import main

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

    def test_the_record_list_only_grows_at_the_end(self) -> None:
        """The same requirement `parse_zsh` now carries, mirrored here.

        `parse_bash` has always been file-ordered and assigns synthetic stamps
        in place, so it never had #289's defect -- but until now that was prose
        in a comment while zsh had an executable version of it. A future "sort
        both for consistency" would reintroduce the bug on this path, silently,
        and this is what stops it.
        """
        # Real epochs: `#100` is not a plausible timestamp and `parse_bash`
        # reads it as a command, which made the first version of this fixture
        # assert about seven untimed lines rather than the property.
        before = "#1753000000\nalpha\nbare\n#1753000200\ngamma\n"
        first = importer.parse_bash(before, MTIME)
        grown = importer.parse_bash(before + "#1753000100\ndelta\n", MTIME)
        self.assertEqual([r.cmd for r in grown[: len(first)]], [r.cmd for r in first])
        self.assertEqual([r.cmd for r in grown[len(first) :]], ["delta"])

    def test_bash_records_no_timing_so_the_duration_is_unknown(self) -> None:
        """The sentinel's *sign*, which is the whole of what it claims.

        `.bash_history` carries no durations at all, so every imported row says
        "not recorded". Flip the sign and each one claims it took a millisecond
        -- a number, written into `logs/`, which rule 8 calls the primary copy
        and which an import is the one operation that fills from outside.

        `< 0` rather than `== -1`: the claim is "this means unknown", and an
        assertion on the exact number would fail on a deliberate change to `-2`
        that meant the same thing. Found by #276's `sign` operator, which no
        test in this file could see.
        """
        parsed = importer.parse_bash("#1753000000\ngit status\nbare\n", MTIME)
        self.assertEqual(len(parsed), 2)
        for row in parsed:
            self.assertLess(row.duration_ms, 0, row)

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

    def test_a_plain_history_line_has_an_unknown_duration(self) -> None:
        """The *untimed* branch, which is a separate literal from the one
        `test_extended_format` pins. That test covers `: ts:0;cmd` -- a header
        whose elapsed field is zero -- and a zsh history with no headers at all
        takes the other path and was unguarded."""
        parsed = importer.parse_zsh("git status\nls -la\n", MTIME)
        self.assertEqual(len(parsed), 2)
        for row in parsed:
            self.assertLess(row.duration_ms, 0, row)

    def test_the_record_list_only_grows_at_the_end(self) -> None:
        """The property `run`'s watermark rests on, asserted directly.

        Appending to the history must leave every earlier position exactly where
        it was -- that is what makes a count of records a usable mark, and both
        parsers now owe it.
        """
        before = ": 100:0;alpha\nbare\n: 200:0;gamma\n"
        first = importer.parse_zsh(before, MTIME)
        grown = importer.parse_zsh(before + ": 150:0;delta\n", MTIME)
        self.assertEqual([r.cmd for r in grown[: len(first)]], [r.cmd for r in first])
        self.assertEqual([r.cmd for r in grown[len(first) :]], ["delta"])

    def test_it_does_not_sort(self) -> None:
        """File order, not timestamp order, and this is the executable form of
        that requirement.

        `parse_zsh` used to sort and return that, which is what #289 was: `run`
        sliced a positional count into a re-sorted list. Nothing outside tests
        ever wanted the sorted view, so the sort is gone rather than hidden
        behind a second name -- a public parser handing back timestamp order is
        the footgun, whatever it is called.
        """
        rows = importer.parse_zsh(": 200:0;gamma\n: 100:0;alpha\n", MTIME)
        self.assertEqual([r.cmd for r in rows], ["gamma", "alpha"])


class ImportTestCase(WoswoarTestCase):
    """Fixture only. Subclassing a class that *has* tests would re-run them."""

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


class TestImportRun(ImportTestCase):
    def _commands(self) -> set[str]:
        return {e.cmd for e in cache.load_entries()}

    def test_imports_and_is_visible_to_search(self) -> None:
        source = self._source("hist", "#1753000000\ngit status\n#1753000100\nls\n")
        result = importer.run("bash", source)
        self.assertEqual(result.imported, 2)
        self.assertEqual(self._commands(), {"git status", "ls"})

    def test_an_imported_command_has_an_unknown_exit_code(self) -> None:
        """The third sentinel, and the one whose flip is worst.

        A shell history records what was *run*, never how it ended, so an
        imported row cannot know. `1` is not a missing value: it is the code for
        a command that failed, so every line a user ever imported would read as
        having failed -- in the history they search, from the primary copy.

        Asserted on what reaches the store rather than on `Parsed`, because
        `run` builds the `Entry` itself and this literal is nowhere near the two
        the parsers carry.
        """
        importer.run("bash", self._source("hist", "#1753000000\ngit status\n"))
        codes = [entry.exit_code for entry in cache.load_entries()]
        self.assertEqual(len(codes), 1)
        for code in codes:
            self.assertLess(code, 0, codes)

    def test_a_zsh_record_with_an_earlier_timestamp_is_still_imported(self) -> None:
        """#289. The watermark counts records, so the list it counts into must
        only ever grow at the end -- and timestamp order does not.

        A shell writes its history when it exits, so two zsh sessions closed in
        the other order interleave. The appended command sorted below the
        watermark and was skipped on this run and on every run afterwards, with
        a successful import reported each time.
        """
        source = self._source("hist", ": 100:0;alpha\n: 200:0;gamma\n")
        importer.run("zsh", source)
        self._rewrite(source, ": 100:0;alpha\n: 200:0;gamma\n: 150:0;delta\n")
        result = importer.run("zsh", source)
        self.assertEqual(result.imported, 1)
        self.assertIn("delta", self._commands())

    def test_an_untimed_neighbour_does_not_push_a_new_record_under_the_mark(self) -> None:
        """The second way the order moved, and the one a timestamp fixture
        misses entirely.

        Untimed records used to be collected and appended *after* the timed
        ones, so a newly timed record pushed every untimed one down a slot --
        enough to drop a command with no out-of-order timestamp anywhere. Here
        `delta`'s stamp is the highest of the three real ones, so a test that
        only varied timestamps would pass against the old code.
        """
        source = self._source("hist", ": 100:0;alpha\nbare\n: 200:0;gamma\n")
        importer.run("zsh", source)
        self._rewrite(source, ": 100:0;alpha\nbare\n: 200:0;gamma\n: 300:0;delta\n")
        importer.run("zsh", source)
        self.assertIn("delta", self._commands())

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

        # Log rotation. Since #294 the anchor resolves this before the
        # `(ts, cmd)` guard has to: the last imported command is found at its new
        # position and the count is corrected to it, so `b` is not re-examined
        # and `skipped` is 0 rather than 1. The outcome is what it always was --
        # nothing imported, nothing duplicated -- and the guard below still
        # covers the case the anchor cannot find.
        self._rewrite(source, "#1753000100\nb\n")
        result = importer.run("bash", source)
        self.assertEqual(result.imported, 0)
        self.assertEqual(len(cache.load_entries()), 2)

    def test_a_savehist_trim_plus_a_larger_append_loses_nothing(self) -> None:
        """#294. The file grew overall, so the shrink guard never fired.

        zsh rewrites `.zsh_history` wholesale when trimming to `SAVEHIST`, and
        again under `HIST_EXPIRE_DUPS_FIRST`, dropping records from the *front*.
        `run`'s only staleness guard is `already > len(parsed)`, which fires when
        the file gets shorter -- so a trim of two plus an append of three leaves
        it longer, nothing fires, and the slice begins two records too late.

        The lost commands are not merely re-read later: they are below the mark
        on this run and on every run after, and each import reports success.

        The append is deliberately larger than the trim. A fixture where the
        file shrinks passes against the old code, because that is the one case
        the existing guard already handled.
        """
        source = self._source("hist", ": 100:0;one\n: 200:0;two\n: 300:0;three\n")
        importer.run("zsh", source)
        self.assertEqual(self._commands(), {"one", "two", "three"})

        # Trimmed to the last one, then three more arrive: 4 records where there
        # were 3, so `already > len(parsed)` is false and the count stands.
        self._rewrite(
            source,
            ": 300:0;three\n: 400:0;four\n: 500:0;five\n: 600:0;six\n",
        )
        importer.run("zsh", source)
        self.assertEqual(
            self._commands(),
            {"one", "two", "three", "four", "five", "six"},
            "records below the stale mark were skipped",
        )

    def test_a_wholly_replaced_source_still_falls_to_the_dedup_guard(self) -> None:
        """The path the anchor cannot rescue, kept covered.

        When the last imported command is nowhere in the new file, there is no
        position to re-anchor to. The count then behaves as it always did, and
        `(ts, cmd)` is what stops a duplicate -- which is why that guard stays.
        """
        source = self._source("hist", "#1753000000\na\n#1753000100\nb\n")
        importer.run("bash", source)

        self._rewrite(source, "#1753000000\na\n")
        result = importer.run("bash", source)
        self.assertEqual(result.imported, 0)
        self.assertEqual(result.skipped, 1, "the anchor missed, so dedup carried it")
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


class TestAMalformedImportStateDoesNotStopTheImport(unittest.TestCase):
    """#291's other half. `_load_state` had the same bare `int(v)`, and it runs
    on the way into every `woswoar import`."""

    def loaded(self, raw: object) -> dict[str, int | str]:
        with mock.patch.object(store, "load_json", return_value=raw):
            return importer._load_state()

    def test_a_null_watermark_starts_from_nothing(self) -> None:
        self.assertEqual(self.loaded({"/some/hist": None}), {})

    def test_a_good_file_is_still_read(self) -> None:
        """The half that keeps the guard honest: swallowing everything would
        pass the test above and re-import the world on every run."""
        self.assertEqual(self.loaded({"/some/hist": 7}), {"/some/hist": 7})

    def test_a_file_that_is_not_a_mapping_starts_from_nothing(self) -> None:
        """`.items()` on a list is an `AttributeError`, which is a third
        exception type and the reason the guard names it."""
        self.assertEqual(self.loaded([1, 2, 3]), {})


class TestImportDropsCredentials(ImportTestCase):
    """Issue #52: import read years of history that no filter had ever seen.

    The hook filters what you type from now on. Import brings in a
    `~/.bash_history` recorded before woswoar existed -- which is the file most
    likely to contain a secret, and it flows straight to logs/, into an
    encrypted chunk, into git, and out to every machine. `docs/security.md`
    says publication cannot be taken back.
    """

    HISTORY = (
        "#1753000000\ngit status\n"
        "#1753000100\nexport AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI\n"
        "#1753000200\ncurl -u admin:hunter2 https://api.example.com\n"
        "#1753000300\ndeploy.sh AKIAIOSFODNN7EXAMPLE\n"
        "#1753000400\nninja -C build\n"
    )

    def test_a_credential_shaped_line_is_not_imported(self) -> None:
        result = importer.run("bash", self._source("h", self.HISTORY))
        self.assertEqual(
            sorted(e.cmd for e in cache.load_entries()), ["git status", "ninja -C build"]
        )
        self.assertEqual(result.credentials, 3)
        self.assertEqual(result.imported, 2)

    def test_the_secret_never_reaches_the_log_file(self) -> None:
        """The assertion that matters: not just absent from the API, absent from disk."""
        importer.run("bash", self._source("h", self.HISTORY))
        logs = (Path(os.environ["WOSWOAR_DIR"]) / "logs").rglob("*.tsv")
        written = "".join(p.read_text(encoding="utf-8") for p in logs)
        self.assertNotIn("wJalrXUtnFEMI", written)
        self.assertNotIn("hunter2", written)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", written)
        self.assertIn("git status", written)

    def test_a_dry_run_reports_what_it_would_drop(self) -> None:
        result = importer.run("bash", self._source("h", self.HISTORY), dry_run=True)
        self.assertEqual(result.credentials, 3)
        self.assertEqual(cache.load_entries(), [])

    def test_dropped_lines_do_not_come_back_on_a_second_import(self) -> None:
        """Idempotency is by count and by (ts, cmd); a filtered line is in neither.

        It must not be re-offered, and must not shift the watermark such that a
        later real command is skipped instead.
        """
        source = self._source("h", self.HISTORY)
        importer.run("bash", source)
        again = importer.run("bash", source)
        self.assertEqual(again.imported, 0)
        self.assertEqual(
            sorted(e.cmd for e in cache.load_entries()), ["git status", "ninja -C build"]
        )

    def test_a_repeated_secret_is_counted_as_a_secret_each_time(self) -> None:
        """The filter runs before the duplicate bookkeeping, and the count says so.

        If a filtered line were added to the seen-set first, the second copy
        would be reported as a collapsed duplicate rather than as a credential.
        Both were credential-shaped; the number the user is shown should say
        that, because it is the number they will judge the filter by.
        """
        twice = "#1753000100\nexport AWS_SECRET_ACCESS_KEY=x\n" * 2
        result = importer.run("bash", self._source("h", twice))
        self.assertEqual(result.credentials, 2)
        self.assertEqual(result.collapsed, 0)
        self.assertEqual(result.imported, 0)

    def test_a_huge_pasted_command_does_not_stall_the_import(self) -> None:
        """The filter must see the truncated command, not the raw one.

        Ten of the rules scan with `[^|;&]*`, which is quadratic in the length
        of the line. A 97 KB pasted script measured 636ms on its own before
        this was fixed, and a history can hold many. Those bytes can never be
        published either -- `truncate` cuts the command at MAX_CMD_CHARS on the
        way to disk -- so scanning them was pure waste.
        """
        paste = "curl -X POST https://api.example.com/v1/things " * 2000
        source = self._source("h", f"#1753000000\n{paste}\n#1753000100\ngit status\n")
        start = time.monotonic()
        importer.run("bash", source)
        elapsed = time.monotonic() - start

        # 9ms healthy, 870ms scanning the raw command: two orders of magnitude
        # of headroom either side of this bound.
        self.assertLess(elapsed, 0.2, "the import filter is scanning untruncated commands")
        self.assertIn("git status", [e.cmd for e in cache.load_entries()])

    def test_the_users_own_extra_pattern_applies_to_imports_too(self) -> None:
        os.environ["WOSWOAR_IGNORE_EXTRA"] = "ninja"
        self.addCleanup(os.environ.pop, "WOSWOAR_IGNORE_EXTRA", None)
        result = importer.run("bash", self._source("h", self.HISTORY))
        self.assertEqual([e.cmd for e in cache.load_entries()], ["git status"])
        self.assertEqual(result.credentials, 4)


class TestImportReporting(ImportTestCase):
    """What the user is told. A silent drop is its own bug: someone looking for
    a command they know they ran needs to learn that the filter took it, not be
    left thinking woswoar lost it."""

    def run_main(self, *argv: str) -> tuple[int, str]:
        """Both streams merged, and the exit code, which two of these need.

        Named `run_main` rather than `run_cli`: `tests/test_sync.py` already has
        a `run_cli` that returns stdout only, deliberately, so that "it warned"
        assertions cannot pass on stderr. Two helpers with one name and
        different contracts is worse than two names.
        """
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_the_dropped_count_is_reported(self) -> None:
        source = self._source("h", TestImportDropsCredentials.HISTORY)
        _, text = self.run_main("import", "bash", "--file", str(source))
        self.assertIn("3 skipped as credential-shaped", text)

    def test_nothing_is_said_when_nothing_was_dropped(self) -> None:
        source = self._source("h", "#1753000000\ngit status\n")
        _, text = self.run_main("import", "bash", "--file", str(source))
        self.assertNotIn("credential-shaped", text)

    def test_a_dry_run_lists_what_it_would_drop(self) -> None:
        """A count alone is unauditable: 17 over a decade tells nobody which 17.

        Dropping is irreversible, so there has to be one mode where a false
        positive can be seen before it happens.
        """
        source = self._source("h", TestImportDropsCredentials.HISTORY)
        _, text = self.run_main("import", "bash", "--file", str(source), "--dry-run")
        self.assertIn("would skip as credential-shaped", text)
        self.assertIn("AWS_SECRET_ACCESS_KEY", text)

    def test_a_real_import_never_lists_them(self) -> None:
        """The listing exists to be read once, not to be piped into a file."""
        source = self._source("h", TestImportDropsCredentials.HISTORY)
        _, text = self.run_main("import", "bash", "--file", str(source))
        self.assertNotIn("would skip", text)
        self.assertNotIn("wJalrXUtnFEMI", text)

    def test_a_listed_command_cannot_drive_the_terminal(self) -> None:
        """It is untrusted text from a history file, printed to a real tty."""
        source = self._source("h", "#1753000000\nexport TOKEN=\x1b[31mred\x07\n")
        _, text = self.run_main("import", "bash", "--file", str(source), "--dry-run")
        self.assertIn("would skip as credential-shaped", text)
        self.assertNotIn("\x1b", text)

    def test_an_uncompilable_extra_pattern_stops_the_import(self) -> None:
        """Importing unfiltered would be the opposite of what the user asked for."""
        os.environ["WOSWOAR_IGNORE_EXTRA"] = "([unclosed"
        self.addCleanup(os.environ.pop, "WOSWOAR_IGNORE_EXTRA", None)
        source = self._source("h", TestImportDropsCredentials.HISTORY)
        code, text = self.run_main("import", "bash", "--file", str(source))
        self.assertEqual(code, 1)
        self.assertIn("WOSWOAR_IGNORE_EXTRA", text)
        self.assertEqual(cache.load_entries(), [], "history was imported unfiltered anyway")


if __name__ == "__main__":
    unittest.main()
