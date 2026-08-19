"""`woswoar forget`: removing a recorded command, and keeping it removed.

The command exists because `docs/security.md` says the credential filter is
best-effort and that publication is final, and both are true -- but the *local*
half of that gap was answerable all along and had no answer. These tests are
mostly about the ways a delete can quietly undo itself: a watermark left where
it was, a cache still holding the row, a digest never written.

They drive the real CLI through `support.run_cli` rather than calling
`forget.apply`, because everything a user reaches is in between: the dry run,
what the listing says about a published row, and the exit code.
"""

from __future__ import annotations

import json

from woswoar import entry, forget, store

from . import support


def _line(ts: int, cmd: str, session: str = "s1") -> str:
    """One log line exactly as the hook writes it."""
    return entry.format_line(support.make_entry(ts, cmd, session=session))


class ForgetTestCase(support.WoswoarTestCase):
    """A day of this machine's history, and a way to look at it afterwards."""

    DAY = "2026-08-19"

    def setUp(self) -> None:
        super().setUp()
        self.lines = [
            _line(1_755_600_001, "git status"),
            _line(1_755_600_002, "export AWS_SECRET_ACCESS_KEY=wharrgarbl"),
            _line(1_755_600_003, "make -j8"),
        ]
        self.log = self.write_log(support.MACHINE_ID, self.DAY, self.lines)

    def remaining(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def mark_exported(self, through: int) -> None:
        """Say the first ``through`` lines have been sealed into a chunk.

        Spelled in lines and converted to bytes here, because the number
        `state.exported` holds is a byte count and a test that computed one by
        hand would be asserting its own arithmetic.
        """
        sealed = sum(len(line) + 1 for line in self.lines[:through])
        relpath = f"hosts/{support.MACHINE_ID}/{self.DAY}.tsv"
        store.save_json(store.state_file(), {"exported": {relpath: sealed}})

    def exported(self) -> int:
        raw = json.loads(store.state_file().read_text(encoding="utf-8"))
        return int(next(iter(raw["exported"].values())))


class TestADryRunIsTheDefault(ForgetTestCase):
    def test_it_lists_the_match_and_changes_nothing(self) -> None:
        before = self.log.read_bytes()
        ran = support.run_cli("forget", "AWS_SECRET")
        self.assertEqual(ran.code, 0)
        self.assertIn("AWS_SECRET_ACCESS_KEY", ran.out)
        self.assertIn("--yes", ran.out)
        self.assertEqual(self.log.read_bytes(), before)

    def test_it_writes_no_digest(self) -> None:
        """The half that would make a dry run permanent without deleting anything.

        A digest written here suppresses the row on the next merge, so the
        command would have acted while reporting that it had not -- and on a
        machine whose copy came from a peer, that is the whole effect.
        """
        support.run_cli("forget", "AWS_SECRET")
        self.assertFalse(store.forgotten_file().exists())

    def test_nothing_matching_says_so_and_succeeds(self) -> None:
        ran = support.run_cli("forget", "no-such-command")
        self.assertEqual(ran.code, 0)
        self.assertIn("nothing recorded", ran.out)


class TestRemoving(ForgetTestCase):
    def test_the_row_goes_and_the_others_are_untouched(self) -> None:
        ran = support.run_cli("forget", "AWS_SECRET", "--yes")
        self.assertEqual(ran.code, 0)
        self.assertEqual(self.remaining(), [self.lines[0], self.lines[2]])

    def test_the_digest_is_recorded(self) -> None:
        support.run_cli("forget", "AWS_SECRET", "--yes")
        self.assertEqual(
            store.forgotten_file().read_text(encoding="utf-8").split(),
            [forget.digest(self.lines[1])],
        )

    def test_the_parse_cache_is_dropped(self) -> None:
        """Keyed by (file, offset), and both just moved.

        Left in place, the next Ctrl-R reads its cached copy of a row that is no
        longer in the log -- so the command would appear to have done nothing at
        exactly the moment somebody checks.
        """
        store.private_dir(store.cache_dir())
        store.cache_file().write_bytes(b"stale")
        support.run_cli("forget", "AWS_SECRET", "--yes")
        self.assertFalse(store.cache_file().exists())

    def test_a_repeated_forget_does_not_repeat_the_digest(self) -> None:
        support.run_cli("forget", "make", "--yes")
        support.run_cli("forget", "git", "--yes")
        recorded = store.forgotten_file().read_text(encoding="utf-8").split()
        self.assertEqual(len(recorded), len(set(recorded)))

    def test_it_takes_every_match_rather_than_the_first(self) -> None:
        self.write_log(
            support.MACHINE_ID,
            self.DAY,
            [_line(1_755_600_001, "curl -H 'token: abc'"), _line(1_755_600_002, "echo token")],
        )
        support.run_cli("forget", "token", "--yes")
        self.assertEqual(self.remaining(), [])


class TestTheSealedPrefixMovesWithTheFile(ForgetTestCase):
    """`state.exported` is a byte count, so a rewrite that ignores it loses history.

    `sync.export` takes the tail from that offset and its own comment says a
    *truncated* log simply has no tail -- so a file that loses bytes below the
    watermark while the number stays put stops publishing exactly that many
    bytes of real history, silently and for good. Nothing else in the suite
    would notice: the rows are still in `logs/`, and every local command shows
    them.
    """

    def test_removing_a_sealed_row_moves_the_watermark_down_by_its_length(self) -> None:
        self.mark_exported(3)
        support.run_cli("forget", "AWS_SECRET", "--yes")
        self.assertEqual(self.exported(), len(self.lines[0]) + len(self.lines[2]) + 2)

    def test_the_watermark_still_points_at_a_line_boundary(self) -> None:
        """The property the number has to keep, stated without arithmetic.

        Everything up to the watermark is what has been sealed; everything after
        it is what the next sync publishes. If the two no longer meet at a line
        boundary, the next chunk begins mid-record.
        """
        self.mark_exported(2)
        support.run_cli("forget", "git status", "--yes")
        tail, _ = store.read_tail(self.log, self.exported())
        self.assertEqual(
            tail.decode("utf-8"),
            f"{self.lines[2]}\n",
            "the unsealed tail is no longer exactly the lines that were never sealed",
        )

    def test_removing_an_unsealed_row_leaves_the_watermark_alone(self) -> None:
        """Only what was below it moves it. A row still waiting to be published
        was never counted, so subtracting it would re-publish the line before."""
        self.mark_exported(1)
        support.run_cli("forget", "make -j8", "--yes")
        self.assertEqual(self.exported(), len(self.lines[0]) + 1)


class TestWhatItSaysAboutWhatItCannotDo(ForgetTestCase):
    def test_a_published_row_is_named_with_its_day(self) -> None:
        self.mark_exported(3)
        ran = support.run_cli("forget", "AWS_SECRET")
        self.assertIn(self.DAY, ran.out)
        self.assertIn("rotate", ran.out)

    def test_an_unpublished_row_is_not_called_published(self) -> None:
        """The fixture that would make the assertion above vacuous.

        A message printed for every row would satisfy it, so the case that must
        *not* print it is the one that gives it meaning.
        """
        ran = support.run_cli("forget", "AWS_SECRET")
        self.assertNotIn("rotate", ran.out)
        self.assertIn("local only", ran.out)

    def test_another_machines_row_is_published_by_construction(self) -> None:
        """It is here because that host sealed it and pushed it, so there is no
        watermark to consult -- it left its machine before it reached this one."""
        self.write_log("beefcafe", self.DAY, [_line(1_755_600_009, "ssh prod")])
        ran = support.run_cli("forget", "ssh prod")
        self.assertIn("published", ran.out)
        self.assertIn("rotate", ran.out)


class TestTheSelector(ForgetTestCase):
    def test_an_empty_pattern_is_refused(self) -> None:
        """`"" in cmd` is true of every command, so this would take the history.

        A refusal rather than a confirmation prompt: there is no answer to "did
        you mean all of it" that is worth the chance of a yes.
        """
        ran = support.run_cli("forget", "", "--yes")
        self.assertEqual(ran.code, 2)
        self.assertEqual(self.remaining(), self.lines)

    def test_no_pattern_at_all_is_refused(self) -> None:
        ran = support.run_cli("forget", "--yes")
        self.assertEqual(ran.code, 2)
        self.assertEqual(self.remaining(), self.lines)

    def test_it_is_a_substring_and_not_a_regex(self) -> None:
        """`logs/` is the primary copy, so a mistyped `.*` has to match nothing.

        The row here contains the literal characters; a regex reading of the
        same pattern matches every command in the file.
        """
        self.write_log(support.MACHINE_ID, self.DAY, [*self.lines, _line(1_755_600_004, "a.*b")])
        ran = support.run_cli("forget", ".*", "--yes")
        self.assertEqual(ran.code, 0)
        self.assertEqual(len(self.remaining()), 3)

    def test_it_matches_the_command_and_not_the_directory(self) -> None:
        """A person forgetting something is naming what they typed."""
        ran = support.run_cli("forget", "/tmp")
        self.assertIn("nothing recorded", ran.out)

    def test_credentials_selects_what_the_filter_would_have_caught(self) -> None:
        ran = support.run_cli("forget", "--credentials", "--yes")
        self.assertEqual(ran.code, 0)
        self.assertEqual(self.remaining(), [self.lines[0], self.lines[2]])


class TestTheDigest(support.WoswoarTestCase):
    def test_two_runs_of_the_same_command_are_different_rows(self) -> None:
        """Why the digest covers the whole line rather than the command.

        Over the command alone, forgetting one `terraform apply` would suppress
        every future one -- a delete that keeps deleting, arriving later and
        without a prompt.
        """
        first = _line(1_755_600_001, "terraform apply")
        second = _line(1_755_600_099, "terraform apply")
        self.assertNotEqual(forget.digest(first), forget.digest(second))

    def test_a_malformed_line_in_the_file_is_skipped_not_fatal(self) -> None:
        """`sync` reads this file on every run, so an unreadable line must cost
        one row's suppression rather than every future sync on this machine."""
        good = forget.digest(_line(1_755_600_001, "git status"))
        store.private_dir(store.data_dir())
        store.forgotten_file().write_text(f"not-a-digest\n{good}\n", encoding="utf-8")
        self.assertEqual(forget.load_digests(), {good})


class TestSurvivingIsByteExact(support.WoswoarTestCase):
    """What `merge` hands to the log file has to be the peer's bytes exactly.

    A filter that re-joined with the wrong separator, or dropped a final line
    without a newline, would corrupt the day it was meant to clean.
    """

    def test_the_lines_that_stay_are_unchanged(self) -> None:
        lines = [_line(1_755_600_001, "one"), _line(1_755_600_002, "two")]
        plaintext = "".join(f"{line}\n" for line in lines).encode("utf-8")
        kept = forget.surviving(plaintext, {forget.digest(lines[0])})
        self.assertEqual(kept, f"{lines[1]}\n".encode())

    def test_a_block_with_no_final_newline_keeps_its_shape(self) -> None:
        lines = [_line(1_755_600_001, "one"), _line(1_755_600_002, "two")]
        plaintext = f"{lines[0]}\n{lines[1]}".encode()
        kept = forget.surviving(plaintext, {forget.digest(lines[1])})
        self.assertEqual(kept, f"{lines[0]}\n".encode())

    def test_nothing_forgotten_returns_the_same_object(self) -> None:
        """The fast path every machine that has never run `forget` takes, on
        every byte of every peer's history, on a one-minute timer."""
        plaintext = b"anything at all\n"
        self.assertIs(forget.surviving(plaintext, set()), plaintext)
