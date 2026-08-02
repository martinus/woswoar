"""`woswoar doctor` has to notice the failures people actually hit.

The one that prompted this: an `age` that runs perfectly and then cannot open a
key, because it is sandboxed. `age --version` succeeds, so doctor reported
nothing at all -- in the exact state, before `init` has worked, where someone
would think to run it.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import unittest

from woswoar import __main__ as main_module
from woswoar import crypto

from . import support
from .support import WoswoarTestCase, requires_age


@requires_age
class TestSelftest(unittest.TestCase):
    def test_a_working_age_reports_nothing_wrong(self) -> None:
        self.assertEqual(crypto.selftest(), "")


class TestDoctorWithABrokenAge(WoswoarTestCase):
    """A stand-in for a confined age: on PATH, runs, refuses to do the work."""

    def setUp(self) -> None:
        super().setUp()
        fake = self.root / "bin"
        fake.mkdir()
        for name in ("age", "age-keygen"):
            script = fake / name
            script.write_text(
                "#!/bin/sh\n"
                f'echo "{name}: error: failed to open input file: permission denied" >&2\n'
                "exit 1\n"
            )
            script.chmod(script.stat().st_mode | stat.S_IXUSR)

        self._path = os.environ["PATH"]
        os.environ["PATH"] = f"{fake}{os.pathsep}{self._path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._path))

    def doctor(self) -> support.Ran:
        return support.run_cli("doctor")

    def test_selftest_reports_what_age_said(self) -> None:
        failure = crypto.selftest()
        self.assertIn("permission denied", failure)

    def test_doctor_fails_and_shows_the_reason(self) -> None:
        ran = self.doctor()
        self.assertNotEqual(ran.code, 0, ran.out)
        self.assertRegex(ran.out, r"\[FAIL\] age", ran.out)
        self.assertIn("permission denied", ran.out)

    def test_doctor_does_not_need_a_repo_to_notice(self) -> None:
        """The whole point. This used to be gated behind `sync.is_repo()`, so
        the machine where `init` had just failed got a clean bill of health."""
        out = self.doctor().out
        self.assertIn("no history repo", out, "precondition: no repo in this sandbox")
        self.assertRegex(out, r"\[FAIL\] age")




@requires_age
class TestDoctorWithASlowAge(WoswoarTestCase):
    """The failure that is hardest to diagnose, because nothing is broken.

    Reported from a real Ubuntu install where `age` came from a snap: `sync` and
    `accept` took "about half a second per day" of history, roughly 100x the
    same work on the same history with a distribution binary. woswoar spawns
    `age` about twice per day-key, so a per-call cost that is invisible at 2 ms
    turns a three-second grant into a six-minute one -- and every check passed.
    """

    #: Comfortably over `crypto.SLOW_MS` without making the suite crawl: five
    #: timed runs at this cost is a third of a second.
    DELAY_MS = 70

    def setUp(self) -> None:
        super().setUp()
        real = shutil.which("age")
        assert real is not None  # @requires_age
        fake = self.root / "bin"
        fake.mkdir()
        script = fake / "age"
        # Delegates to the real age after sleeping, so it is genuinely working
        # and genuinely slow -- which is the combination under test. A stub that
        # only slept would be caught by `selftest` instead, and prove nothing.
        script.write_text(f'#!/bin/sh\nsleep {self.DELAY_MS / 1000}\nexec {real} "$@"\n')
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        self._path = os.environ["PATH"]
        os.environ["PATH"] = f"{fake}{os.pathsep}{self._path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._path))

    def test_the_cost_is_measured(self) -> None:
        self.assertGreaterEqual(crypto.startup_ms(), self.DELAY_MS)

    def test_doctor_fails_and_says_what_it_will_cost(self) -> None:
        ran = support.run_cli("doctor")
        # The age *line*, not the exit code: doctor in a bare temp home already
        # fails on `hook` and `private`, so the exit code is non-zero either way
        # and asserting on it tested nothing about age at all.
        self.assertIn("[FAIL] age", ran.out)
        self.assertNotEqual(ran.code, 0)
        self.assertIn("ms to start", ran.out)
        # Matched either side of the wrap rather than across it: the message is
        # hard-wrapped, so a phrase that spans a line break matches nothing.
        self.assertIn("woswoar runs it about twice per", ran.out)
        self.assertIn("day of recorded history", ran.out)
        self.assertIn("A distribution binary starts", ran.out)

    def test_a_working_age_is_still_the_thing_being_measured(self) -> None:
        """The stand-in really does work, so the FAIL is about speed alone."""
        self.assertEqual(crypto.selftest(), "")


@requires_age
class TestAFastAgeIsNotFlagged(WoswoarTestCase):
    def test_the_real_age_passes(self) -> None:
        """Guards the threshold from being set somewhere that fails everyone."""
        self.assertLess(crypto.startup_ms(), crypto.SLOW_MS)
        self.assertIn("[ok] age", support.run_cli("doctor").out)
        self.assertNotIn("ms to start. woswoar runs it", support.run_cli("doctor").out)
class TestDoctorsMarkers(WoswoarTestCase):
    """A tick for a person, the old text for everything else.

    `[ok]`/`[FAIL]` is what the suite asserts on in a dozen places and what
    `woswoar doctor | grep FAIL` in somebody's script depends on. A glyph only a
    terminal renders must not become the thing those rest on, so the plain form
    is unchanged and the coloured one appears only on a tty.
    """

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def test_a_pipe_gets_the_plain_markers(self) -> None:
        self.assertEqual(main_module._markers(io.StringIO()), main_module._PLAIN_MARKERS)

    def test_a_terminal_gets_a_green_tick_and_a_red_cross(self) -> None:
        markers = main_module._markers(self.Tty())
        self.assertEqual(markers, main_module._COLOUR_MARKERS)
        self.assertIn("\u2714", markers["ok"])
        self.assertIn("\u2718", markers["fail"])
        self.assertTrue(markers["ok"].startswith("\x1b[32m"), "the tick is not green")
        self.assertTrue(markers["fail"].startswith("\x1b[31m"), "the cross is not red")
        self.assertTrue(all(value.endswith("\x1b[0m") for value in markers.values()))

    def test_no_color_is_honoured_on_a_terminal(self) -> None:
        os.environ["NO_COLOR"] = "1"
        self.addCleanup(lambda: os.environ.pop("NO_COLOR", None))
        self.assertEqual(main_module._markers(self.Tty()), main_module._PLAIN_MARKERS)

    def test_the_run_that_tests_and_scripts_see_is_unchanged(self) -> None:
        """Captured output is not a tty, so this is the real regression guard."""
        out = support.run_cli("doctor").out
        self.assertIn("[ok] ", out)
        self.assertNotIn("\x1b[", out)
        self.assertNotIn("\u2714", out)

    def test_the_coloured_markers_are_one_column_wide(self) -> None:
        """Which is why the labels line up, and `[ok]` versus `[FAIL]` never
        did -- four characters against six."""
        widths = {
            len(
                value.replace("\x1b[32m", "")
                .replace("\x1b[31m", "")
                .replace("\x1b[2m", "")
                .replace("\x1b[0m", "")
            )
            for value in main_module._COLOUR_MARKERS.values()
        }
        self.assertEqual(widths, {1})


if __name__ == "__main__":
    unittest.main()
