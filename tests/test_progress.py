"""Progress output: what it says, and the two cases where it must say nothing.

The reason a test file exists for something cosmetic is that both silences are
load-bearing. `woswoar sync` runs from a timer once a minute into the journal,
and `woswoar list` is read by fzf through a pipe; a progress bar in either is
not a cosmetic regression.
"""

from __future__ import annotations

import io
import sys
import unittest
from unittest import mock

from woswoar import progress


class Fake(io.StringIO):
    """A stream that can claim to be a terminal, which StringIO cannot."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def rendered(raw: str) -> list[str]:
    """The lines a terminal would be showing, having been sent `raw`.

    A real cursor, because the bug this exists for is invisible in the byte
    stream: every character of both the counter and the command's output is
    present and correct, and they are simply on the same line. Overwriting
    rather than truncating at `\\r` matters too -- that is how the counter's
    pad-to-previous-width erases a longer line behind a shorter one.
    """
    lines: list[str] = []
    line, column = "", 0
    for char in raw:
        if char == "\r":
            column = 0
        elif char == "\n":
            lines.append(line.rstrip())
            line, column = "", 0
        else:
            line = line[:column] + char + line[column + 1 :]
            column += 1
    if line.strip():
        lines.append(line.rstrip())
    return lines


class TestItStaysQuietWhenItShould(unittest.TestCase):
    def test_a_pipe_gets_nothing(self) -> None:
        """fzf reads `list` through one of these, and the timer's journal too.

        With PATIENCE at its real value this passes whether or not anything
        checks for a terminal, because nothing here waits long enough to print
        -- so it would have guarded nothing. Zero patience leaves the tty check
        as the only reason for the silence, which is the reason under test.
        """
        with mock.patch.object(progress, "PATIENCE", 0.0):
            out = Fake(tty=False)
            with progress.to_terminal(out):
                progress.phase("sealing")
                for i in range(1000):
                    progress.tick(i, 1000, "days")
        self.assertEqual(out.getvalue(), "")

    def test_a_fast_command_gets_nothing_even_on_a_terminal(self) -> None:
        """An idle sync is ~10 ms and runs 1440 times a day."""
        out = Fake(tty=True)
        with progress.to_terminal(out):
            progress.phase("sealing")
            progress.tick(0, 10, "days")
            progress.tick(9, 10, "days")
        self.assertEqual(out.getvalue(), "")

    def test_nothing_is_said_with_no_reporter_installed(self) -> None:
        """Library code calls these unconditionally; `import woswoar.sync` and
        calling `run()` from Python must not print progress at all."""
        progress.phase("sealing")
        progress.tick(1, 2, "days")  # must not raise, must not print


class TestItSpeaksWhenTheWaitIsLong(unittest.TestCase):
    def setUp(self) -> None:
        self.out = Fake(tty=True)
        # A reporter whose clock started long enough ago that the patience has
        # already run out -- rather than sleeping for PATIENCE in a unit test.
        self.reporter = progress._Terminal(self.out, started=-progress.PATIENCE * 10)

    def test_the_phase_and_the_count_reach_the_terminal(self) -> None:
        with progress._install(self.reporter):
            progress.phase("sealing this machine's history")
            # Printing the phase starts the rate limit, so the tick immediately
            # after it is deliberately swallowed -- the phase line is already on
            # screen and saying the same thing. This is about what a tick shows
            # once it is due, not about when it becomes due.
            self.reporter._last = 0.0
            progress.tick(3, 12, "days")
        shown = self.out.getvalue()
        self.assertIn("sealing this machine's history", shown)
        self.assertIn("3/12 days", shown)
        self.assertIn("25%", shown)

    def test_a_shorter_line_does_not_leave_the_last_one_behind(self) -> None:
        """`\\r` alone would leave `...100/1000 days` reading `...9/1000 dayss`."""
        with progress._install(self.reporter):
            progress.phase("x")
            progress.tick(1000, 1000, "days")
            self.reporter._last = 0.0  # the interval is not what is under test
            progress.tick(1, 1, "d")
        last = self.out.getvalue().rsplit("\r", 1)[-1]
        self.assertNotIn("days", last)

    def test_the_line_is_cleared_when_the_command_ends(self) -> None:
        """Otherwise the count stays under whatever the command prints next."""
        with progress._install(self.reporter):
            progress.phase("x")
            progress.tick(1, 2, "days")
        self.assertTrue(self.out.getvalue().endswith("\r"))
        self.assertNotIn("1/2 days", self.out.getvalue().rsplit("\r", 1)[-1])

    def test_no_percentage_is_claimed_for_an_unknown_total(self) -> None:
        with progress._install(self.reporter):
            progress.phase("x")
            progress.tick(0, 0, "days")
        self.assertNotIn("%", self.out.getvalue())

    def test_updates_are_rate_limited(self) -> None:
        """A 20,000-chunk merge must not spend its time on escape codes."""
        with progress._install(self.reporter):
            progress.phase("x")
            for i in range(5000):
                progress.tick(i, 5000, "days")
        # One write got through; the rest were inside the interval.
        self.assertLess(self.out.getvalue().count("days"), 5)


class TestTheCommandsOwnOutputIsNotDamaged(unittest.TestCase):
    """One terminal, written by the counter and by the command at once.

    The counter is rewritten in place and so never ends in a newline, and
    `to_terminal` wraps the whole command -- so before this, the first thing a
    slow command printed landed on the end of the counter:

        merging... 3/5 days (60%)Merged 12 commands from 2 machines

    Reported from a real install as output that "doesn't properly print a
    newline when done". Only the first line is damaged, so it reads as
    intermittent.
    """

    def setUp(self) -> None:
        self.term = Fake(tty=True)
        # Zero, so what these test is where the clearing happens rather than
        # whether the wait was long enough -- which is TestItSpeaksWhenTheWait-
        # IsLong's job. With the real values nothing prints at all and every
        # assertion below would pass against any implementation.
        for name in ("PATIENCE", "INTERVAL"):
            patched = mock.patch.object(progress, name, 0.0)
            patched.start()
            self.addCleanup(patched.stop)
        # The command's stdout *is* the terminal. That sharing is the whole
        # mechanism: two streams, one screen.
        patched_out = mock.patch.object(sys, "stdout", self.term)
        patched_out.start()
        self.addCleanup(patched_out.stop)

    def test_a_line_printed_mid_command_starts_its_own_line(self) -> None:
        with progress.to_terminal(self.term):
            progress.phase("merging other machines")
            progress.tick(3, 5, "days")
            print("Merged 12 commands from 2 machines")
        self.assertEqual(rendered(self.term.getvalue()), ["Merged 12 commands from 2 machines"])

    def test_the_counter_comes_back_after_the_command_has_spoken(self) -> None:
        """Clearing must not end the reporting: `sync` prints what it resealed
        and then carries on merging, and the wait after that needs a counter
        just as much as the wait before it did."""
        with progress.to_terminal(self.term):
            progress.phase("merging")
            progress.tick(1, 5, "days")
            print("Resealed 3 days")
            progress.tick(4, 5, "days")
            shown = rendered(self.term.getvalue())
        self.assertEqual(shown[0], "Resealed 3 days")
        self.assertIn("4/5 days", shown[-1])

    def test_nothing_of_the_counter_survives_under_the_output(self) -> None:
        """The counter is padded to the previous width, so a careless clear
        leaves trailing spaces that a short output line does not cover."""
        with progress.to_terminal(self.term):
            progress.phase("merging a great many other machines")
            progress.tick(1234, 5678, "days")
            print("ok")
        self.assertEqual(rendered(self.term.getvalue()), ["ok"])

    def test_a_warning_on_stderr_starts_its_own_line_too(self) -> None:
        """The counter goes to stderr, so this is where a collision shows most
        plainly -- and `init` prints "joined, but the first sync failed" there
        partway through, which is a slow command by definition."""
        with mock.patch.object(sys, "stderr", self.term), progress.to_terminal(self.term):
            progress.phase("cloning")
            progress.tick(1, 3, "days")
            print("joined, but the first sync failed", file=sys.stderr)
        self.assertEqual(rendered(self.term.getvalue()), ["joined, but the first sync failed"])

    def test_a_reporter_writing_through_the_wrapper_does_not_recurse(self) -> None:
        """A wrapped write calls `clear`, and `clear` writes -- so a reporter
        whose own stream is the wrapper is a loop unless the flag is lowered
        before the write rather than after it.

        `to_terminal` builds its reporter on the raw stream, so today only a
        nested call reaches this. It is a `RecursionError` when it happens,
        which is a stack trace instead of whatever the command was saying.
        """
        with mock.patch.object(sys, "stderr", self.term), progress.to_terminal(self.term):
            progress.phase("outer")
            progress.tick(1, 2, "days")
            with progress.to_terminal():  # picks up the wrapped sys.stderr
                progress.phase("inner")
                progress.tick(1, 2, "days")
                print("still here")
        self.assertIn("still here", rendered(self.term.getvalue()))

    def test_both_streams_are_left_exactly_as_they_were_found(self) -> None:
        out, err = sys.stdout, sys.stderr
        with progress.to_terminal(self.term):
            self.assertIsNot(sys.stdout, out, "precondition: stdout was borrowed")
            self.assertIsNot(sys.stderr, err, "precondition: stderr was borrowed")
        self.assertIs(sys.stdout, out)
        self.assertIs(sys.stderr, err)

    def test_a_pipe_gets_its_stdout_back_untouched(self) -> None:
        """`woswoar list` writes tens of thousands of rows into fzf through
        here, and with no counter on screen there is nothing to clear."""
        with progress.to_terminal(Fake(tty=False)):
            self.assertIs(sys.stdout, self.term)


class TestBorrowingStdoutChangesNothingElse(unittest.TestCase):
    def test_it_still_answers_isatty_for_the_stream_underneath(self) -> None:
        """`doctor`'s tick marks and `list --colour` decide on colour by asking
        stdout (`__main__.py:253`). A wrapper answering for itself would turn
        colour off for exactly the slow commands that install a reporter."""
        for tty in (True, False):
            with self.subTest(tty=tty):
                shared = progress._Shared(Fake(tty=tty))
                self.assertEqual(shared.isatty(), tty)

    def test_what_is_written_arrives_unchanged(self) -> None:
        underneath = Fake(tty=True)
        shared = progress._Shared(underneath)
        shared.write("one\ntwo\n")
        self.assertEqual(underneath.getvalue(), "one\ntwo\n")


if __name__ == "__main__":
    unittest.main()
