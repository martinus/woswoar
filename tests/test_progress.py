"""Progress output: what it says, and the two cases where it must say nothing.

The reason a test file exists for something cosmetic is that both silences are
load-bearing. `woswoar sync` runs from a timer once a minute into the journal,
and `woswoar list` is read by fzf through a pipe; a progress bar in either is
not a cosmetic regression.
"""

from __future__ import annotations

import io
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


if __name__ == "__main__":
    unittest.main()
