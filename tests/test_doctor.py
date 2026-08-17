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
from pathlib import Path
from unittest import mock

from woswoar import crypto, install, report, store

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
        self.assertEqual(report.markers(io.StringIO()), report.PLAIN_MARKERS)

    def test_a_terminal_gets_a_green_tick_and_a_red_cross(self) -> None:
        markers = report.markers(self.Tty())
        self.assertEqual(markers, report.COLOUR_MARKERS)
        self.assertIn("\u2714", markers["ok"])
        self.assertIn("\u2718", markers["fail"])
        self.assertTrue(markers["ok"].startswith("\x1b[32m"), "the tick is not green")
        self.assertTrue(markers["fail"].startswith("\x1b[31m"), "the cross is not red")
        self.assertTrue(all(value.endswith("\x1b[0m") for value in markers.values()))

    def test_no_color_is_honoured_on_a_terminal(self) -> None:
        os.environ["NO_COLOR"] = "1"
        self.addCleanup(lambda: os.environ.pop("NO_COLOR", None))
        self.assertEqual(report.markers(self.Tty()), report.PLAIN_MARKERS)

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
            for value in report.COLOUR_MARKERS.values()
        }
        self.assertEqual(widths, {1})


if __name__ == "__main__":
    unittest.main()


class TestAHookLeftBehindByAnUpgrade(WoswoarTestCase):
    """`install` copies the hook; upgrading woswoar does not.

    So a machine can run this version's Python against an older version's shell
    code indefinitely, with every other check passing. That was survivable while
    the hook only recorded commands. Since #134 it also decides when to sync, so
    an un-re-installed machine silently keeps whatever arrangement it had --
    which is the "running machine silently wrong" CLAUDE.md rule 8 is about.
    """

    def hook(self) -> Path:
        path = store.data_dir() / install.HOOKS["bash"]
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_an_older_copy_is_reported_with_the_command_that_fixes_it(self) -> None:
        self.hook().write_text("# woswoar, but from last year\n", encoding="utf-8")
        out = support.run_cli("doctor").out
        self.assertIn("[FAIL] hook", out)
        self.assertIn("woswoar install", out)

    def test_the_shipped_hook_passes(self) -> None:
        """Otherwise the check above would fail for everyone, always -- and the
        comparison is against the packaged bytes, so a stray newline written by
        `install` would do exactly that."""
        support.run_cli("install", "--rcfile", str(self.root / "bashrc"))
        self.assertIn("[ok] hook", support.run_cli("doctor").out)

    def test_a_missing_hook_still_reads_as_missing(self) -> None:
        """Not as "older than this woswoar", which would name the wrong fix."""
        out = support.run_cli("doctor").out
        self.assertIn("[FAIL] hook", out)
        self.assertNotIn("older than this woswoar", out)


class TestDoctorReportsTheShellsThisMachineUses(WoswoarTestCase):
    """One version line per shell woswoar is responsible for -- and only those.

    Reporting every shell woswoar *could* support would fail a perfectly good
    bash-only machine for not having zsh installed, which is a red `doctor` with
    nothing to fix.
    """

    def test_a_bash_only_machine_is_not_asked_about_zsh(self) -> None:
        (self.home / ".bashrc").write_text("", encoding="utf-8")
        support.run_cli("install")
        out = support.run_cli("doctor").out
        self.assertRegex(out, r"\] bash ")
        self.assertNotIn("] zsh ", out)

    def test_doctor_checks_the_zsh_version_when_zsh_is_installed(self) -> None:
        """The version floor exists because the hook enforces one and switches
        itself off below it -- silently, from the user's side. This is what says
        so before they find out.

        What is asserted is the *line*, not that this machine has a zsh: the
        version reads `not found` where there is none, and reporting the floor
        beside it is exactly as useful there. Requiring a non-space run made this depend
        on the runner instead, which passed in CI -- where `ci.yml` installs zsh
        -- and failed the v0.10.0 release, whose workflow did not.
        """
        (self.home / ".zshrc").write_text("", encoding="utf-8")
        support.run_cli("install")
        out = support.run_cli("doctor").out
        self.assertRegex(out, r"\] zsh +.+ \(5\.0\+ required\)")

    def test_each_installed_shell_gets_its_own_rcfile_line(self) -> None:
        (self.home / ".bashrc").write_text("", encoding="utf-8")
        (self.home / ".zshrc").write_text("", encoding="utf-8")
        support.run_cli("install")
        out = support.run_cli("doctor").out
        self.assertIn(f"{self.home}/.bashrc sources the hook", out)
        self.assertIn(f"{self.home}/.zshrc sources the hook", out)

    def test_a_block_left_by_a_test_install_is_not_reported_as_working(self) -> None:
        """The line pointing *somewhere*, not the marker being present.

        A real `.bashrc` was found sourcing
        `/tmp/woswoar-test-kc71_9n1/data/woswoar.bash` -- a sandbox that a test
        run deleted on the way out. Every interactive shell printed a `No such
        file or directory`, nothing was recorded for months, and `doctor` said
        `bashrc ... sources the hook` the whole time, because the block was
        there. The block being there is not the claim the line makes.
        """
        (self.home / ".bashrc").write_text("", encoding="utf-8")
        support.run_cli("install")
        gone = self.root / "gone" / "woswoar.bash"
        install.write_block(self.home / ".bashrc", gone)

        out = support.run_cli("doctor").out
        self.assertIn("[FAIL] bashrc", out)
        self.assertIn(str(gone), out, "the report does not say where the line points")
        self.assertIn("woswoar install", out, "no remedy named")

    def test_reinstalling_is_a_remedy_that_works(self) -> None:
        """Without this the test above is satisfied by a check that can only
        fail, and the command it tells people to run is untested."""
        (self.home / ".bashrc").write_text("", encoding="utf-8")
        support.run_cli("install")
        install.write_block(self.home / ".bashrc", self.root / "gone" / "woswoar.bash")
        self.assertIn("[FAIL] bashrc", support.run_cli("doctor").out)

        support.run_cli("install")
        self.assertIn("[ok] bashrc", support.run_cli("doctor").out)

    def test_the_line_is_read_the_way_a_shell_reads_it(self) -> None:
        """`$HOME/...` is what `install` writes whenever the hook is under the
        home directory, and it is what a shared dotfiles `.bashrc` carries. A
        check that compared the line to the hook's absolute path as text would
        fail every one of those installs, which is a red `doctor` on a machine
        with nothing wrong -- worse than the false green it replaced.

        The sandbox puts `WOSWOAR_DIR` outside `$HOME`, where `install` writes
        an absolute path, so this has to move it in: without that the fixture
        cannot tell the two spellings apart.
        """
        os.environ["WOSWOAR_DIR"] = str(self.home / ".local/share/woswoar")
        (self.home / ".bashrc").write_text("", encoding="utf-8")
        support.run_cli("install")

        text = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertIn('source "$HOME/', text, "the fixture wrote an absolute path")
        self.assertIn("[ok] bashrc", support.run_cli("doctor").out)

    def test_another_installers_source_line_is_not_mistaken_for_ours(self) -> None:
        """An rc file is full of `source` lines and exactly one is woswoar's.

        Reading the first one in the file would report nvm's path as the hook's
        -- a red `doctor` on a healthy machine, with a remedy that changes
        nothing. The marked block is what makes this answerable at all, and it
        has to be the thing searched rather than merely the thing found.
        """
        rcfile = self.home / ".bashrc"
        rcfile.write_text('source "$HOME/.nvm/nvm.sh"\n', encoding="utf-8")
        support.run_cli("install")

        self.assertTrue(
            rcfile.read_text(encoding="utf-8").index("nvm.sh")
            < rcfile.read_text(encoding="utf-8").index(install.BEGIN),
            "the fixture's other source line has to come first to test anything",
        )
        self.assertIn("[ok] bashrc", support.run_cli("doctor").out)

    def test_a_block_that_sources_nothing_is_not_a_missing_block(self) -> None:
        """Three states, three lines: someone who has never installed, someone
        whose line points at the wrong file, and someone who edited the block
        down to a comment. Telling the third they have no woswoar block sends
        them looking for something that is right in front of them."""
        (self.home / ".bashrc").write_text("", encoding="utf-8")
        support.run_cli("install")
        (self.home / ".bashrc").write_text(
            f"{install.BEGIN}\n# I took this out for a bit\n# <<< woswoar <<<\n",
            encoding="utf-8",
        )
        out = support.run_cli("doctor").out
        self.assertIn("[FAIL] bashrc", out)
        self.assertIn("sources nothing", out)
        self.assertNotIn("has no woswoar block", out)

    def test_a_machine_with_nothing_installed_still_reports_a_shell(self) -> None:
        """`installed_shells()` is empty before the first `install`, and a doctor
        that then printed no shell line at all would be least useful exactly when
        someone is trying to find out what is wrong."""
        out = support.run_cli("doctor").out
        self.assertRegex(out, r"\] bash ")


class TestReadingThePackagedHook(unittest.TestCase):
    def test_the_fast_path_and_the_fallback_agree(self) -> None:
        """`install.hook_bytes` reads the file beside the module and only falls back to
        `importlib.resources` when there is none -- 8.7 ms saved on a sync that
        now runs once a minute. Two routes to one file is two chances to read a
        different one, and the fallback is exercised by nothing else here: it is
        for a zipapp, which nothing in CI builds.
        """
        from importlib import resources

        via_resources = (resources.files("woswoar") / "shell" / install.HOOKS["bash"]).read_bytes()
        self.assertEqual(install.hook_bytes("bash"), via_resources)
        self.assertTrue(via_resources.startswith(b"# woswoar"), "that is not the hook")


class TestDoctorReportsWhatFzfCanDo(WoswoarTestCase):
    """Not just that fzf is there, but which keys it can offer.

    An fzf below 0.45 gets a working picker with no Ctrl-R cycling and no
    Ctrl-T timeline. That is correct and deliberate -- an unknown action in a
    `--bind` makes fzf refuse to start, so the keys cannot be offered
    optimistically -- but it is also silent, and indistinguishable from a bug
    unless something says so. Reported from a fleet running 0.73 on one machine
    and Debian's 0.44.1 on another.
    """

    def fzf_line(self, said: str, parsed: tuple[int, int] | None) -> str:
        from woswoar import search

        with (
            mock.patch.object(search, "fzf_version", return_value=(said, parsed)),
            mock.patch.object(
                search,
                "fzf_supports_transform",
                return_value=parsed is not None and parsed >= search.TRANSFORM_SINCE,
            ),
        ):
            out = support.run_cli("doctor").out
        return next(line for line in out.splitlines() if " fzf " in line)

    def test_a_new_enough_fzf_just_shows_its_version(self) -> None:
        line = self.fzf_line("0.73.1 (Fedora)", (0, 73))
        self.assertIn("0.73.1", line)
        self.assertNotIn("need", line, "a capable fzf was told what it is missing")

    def test_an_old_fzf_is_told_exactly_what_it_is_missing(self) -> None:
        """Every key the gate withholds, spelled here by hand rather than read
        out of `search.GATED_KEYS` -- a test that built the sentence the way the
        code does would agree with it about a missing key. The count is asserted
        against that tuple instead, so a fourth gated key fails here until
        someone decides whether this line should name it."""
        from woswoar import search

        line = self.fzf_line("0.44.1 (debian)", (0, 44))
        self.assertIn("0.44.1", line)
        for named in ("ctrl-r", "ctrl-t", "ctrl-/", "0.45+"):
            self.assertIn(named, line, f"{named} is not mentioned")
        self.assertEqual(len(search.GATED_KEYS), 3, "a gated key with nothing saying so")

    def test_an_old_fzf_is_not_reported_as_a_failure(self) -> None:
        """Search works perfectly there. A red cross would say the machine is
        broken, and the next one would be read as noise too."""
        self.assertIn("[ok]", self.fzf_line("0.44.1 (debian)", (0, 44)))

    def test_a_version_it_cannot_parse_does_not_go_blank(self) -> None:
        line = self.fzf_line("", None)
        self.assertIn("version unknown", line)

    def test_a_missing_fzf_still_says_how_to_get_one(self) -> None:
        """The other branch, and the one that is an actual problem."""
        with mock.patch.object(shutil, "which", return_value=None):
            out = support.run_cli("doctor").out
        line = next(line for line in out.splitlines() if " fzf " in line)
        self.assertIn("[FAIL]", line)
        self.assertIn("not found", line)
