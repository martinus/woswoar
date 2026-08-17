"""The check value and its one renderer.

These are the tests that could not be written before. Every verdict `doctor`
reaches used to be a `print` inside `cmd_doctor`, so the only way to ask "is this
condition judged correctly" was to run the whole command and grep its output --
which meant a check could be wrong in a way no assertion would catch as long as
*something* was printed. Now a check is a value, and this file asserts on the
values.

The rendering half is here too, because the plain markers are an interface: the
suite asserts on `[FAIL] day keys` in a dozen places and `woswoar doctor | grep
FAIL` is a reasonable thing to have in a script.
"""

from __future__ import annotations

import io
import unittest

from woswoar import doctor, report
from woswoar.report import Check

from . import support
from .support import WoswoarTestCase


class TestTheCheckValue(unittest.TestCase):
    def test_a_verdict_is_asked_for_exactly(self) -> None:
        """`assertFalse` is not enough here and a mutation proved it: `None` is
        falsy, so a check that lost its `ok=False` still passed a test written
        that way while no longer being able to fail the command. `Check.ok` has
        no default now, but the assertions say `assertIs` for the same reason."""
        self.assertIs(Check("age", "broken", ok=False).ok, False)
        self.assertIs(Check("age", "fine", ok=True).ok, True)
        self.assertIsNone(Check("logs", "3 file(s)", ok=None).ok)

    def test_an_info_line_is_not_a_failure(self) -> None:
        """The three-valued `ok` is the whole reason `failed` is a property and
        not `not check.ok`. `doctor` reports how many log files there are and
        which remote is configured; counting either as a failure would make the
        command exit non-zero on a perfectly healthy machine."""
        self.assertFalse(Check("logs", "3 file(s)", ok=None).failed)
        self.assertFalse(Check("logs", "3 file(s)", ok=None).failed)
        self.assertFalse(Check("age", "fine", ok=True).failed)
        self.assertTrue(Check("age", "broken", ok=False).failed)

    def test_failed_ignores_info_lines(self) -> None:
        self.assertFalse(report.failed([Check("a", "x", ok=None), Check("b", "y", ok=True)]))
        self.assertTrue(report.failed([Check("a", "x", ok=None), Check("b", "y", ok=False)]))

    def test_an_empty_report_has_not_failed(self) -> None:
        self.assertFalse(report.failed([]))


class TestRendering(unittest.TestCase):
    def test_the_three_markers_are_the_ones_scripts_grep_for(self) -> None:
        rendered = report.lines(
            [Check("ok", "d", ok=True), Check("bad", "d", ok=False), Check("note", "d", ok=None)],
            report.PLAIN_MARKERS,
        )
        self.assertTrue(rendered[0].startswith("[ok] "))
        self.assertTrue(rendered[1].startswith("[FAIL] "))
        self.assertTrue(rendered[2].startswith("[--] "))

    def test_the_label_column_is_padded_so_details_line_up(self) -> None:
        rendered = report.lines(
            [Check("a", "detail", ok=None), Check("a-much-longer-label", "detail", ok=None)],
            report.PLAIN_MARKERS,
        )
        self.assertIn("a            detail", rendered[0])
        # Over-long labels push their detail out rather than being cut: a
        # truncated label is worse than a ragged column.
        self.assertIn("a-much-longer-label detail", rendered[1])

    def test_a_note_is_indented_under_the_line_it_belongs_to(self) -> None:
        """`note` is what lets a check carry its own explanation. Without it,
        moving a check out of the CLI would have meant cutting the prose -- and
        for several of these the prose is the only place a state is explained."""
        rendered = report.lines(
            [Check("age", "slow", ok=False, note="first line\nsecond line")], report.PLAIN_MARKERS
        )
        self.assertEqual(len(rendered), 3)
        self.assertEqual(rendered[1], "     first line")
        self.assertEqual(rendered[2], "     second line")

    def test_an_empty_note_adds_no_line(self) -> None:
        self.assertEqual(
            len(report.lines([Check("a", "b", ok=None, note="")], report.PLAIN_MARKERS)), 1
        )

    def test_order_is_preserved(self) -> None:
        """`doctor` reports the shell before the hook before the rc file because
        that is the order somebody fixes them in."""
        labels = ["z", "a", "m"]
        rendered = report.lines([Check(x, "d", ok=None) for x in labels], report.PLAIN_MARKERS)
        self.assertEqual([line.split()[1] for line in rendered], labels)

    def test_a_pipe_gets_the_plain_markers(self) -> None:
        self.assertEqual(report.markers(io.StringIO()), report.PLAIN_MARKERS)


class TestDoctorDecidesWithoutPrinting(WoswoarTestCase):
    """The checks `doctor` owns, asked directly.

    Each of these was previously reachable only by running the command and
    matching its output, so a wrong verdict and a wrong *message* were the same
    failure. They are different questions and these ask the first one.
    """

    def test_machine_check_passes_when_the_file_is_there(self) -> None:
        # `WoswoarTestCase` writes one into the sandbox.
        check = doctor.machine_check()
        self.assertTrue(check.ok)
        self.assertEqual(check.label, "machine")

    def test_machine_check_fails_when_it_is_not(self) -> None:
        from woswoar import store

        store.machine_file().unlink()
        self.assertFalse(doctor.machine_check().ok)

    def test_asking_does_not_create_an_identity(self) -> None:
        """`store.machine()` *generates* a machine file, so a check that asked
        the wrong way would make itself pass on the second run. That was already
        the reason `cmd_doctor` read the path before anything else touched it,
        and moving the check is exactly when such a thing gets lost."""
        from woswoar import store

        store.machine_file().unlink()
        doctor.machine_check()
        doctor.identity_check()
        self.assertFalse(store.machine_file().exists(), "a check created the machine file")

    def test_identity_is_reported_as_absent_rather_than_broken(self) -> None:
        """No identity yet is an ordinary state on a machine that has not run
        `init`, so it is an info line and must not fail the command."""
        check = doctor.identity_check()
        self.assertIsNone(check.ok)
        self.assertIn("woswoar init", check.detail)

    def test_local_checks_report_the_logs_and_the_permissions(self) -> None:
        labels = [check.label for check in doctor.local_checks()]
        self.assertEqual(labels, ["logs", "private", "cache", "session"])

    def test_a_readable_path_fails_the_privacy_check(self) -> None:
        """The one check in `local_checks` that can fail, and the one worth
        having: recorded history holds more than ~/.bash_history does."""
        from woswoar import store

        store.logs_dir().mkdir(parents=True, exist_ok=True)
        loose = store.logs_dir() / "loose.tsv"
        loose.write_text("", encoding="utf-8")
        loose.chmod(0o644)
        private = next(c for c in doctor.local_checks() if c.label == "private")
        self.assertFalse(private.ok)
        self.assertIn("other users can read", private.detail)

    def test_a_stale_hook_is_a_failure_with_the_command_that_fixes_it(self) -> None:
        """The installer's checks are values now too, and this is the one worth
        asserting on: a machine running an older woswoar's shell code keeps
        recording and looks healthy, while whatever sync arrangement that
        version had is what it still does. It was reachable only through stdout.
        """
        from woswoar import __main__ as main_module
        from woswoar import store

        support.run_cli("install")
        hook = store.data_dir() / main_module.HOOKS["bash"]
        hook.write_bytes(b"# an older woswoar wrote this\n")
        stale = [c for c in main_module._hook_checks() if c.label == "hook"]
        self.assertTrue(stale, "no hook check was produced")
        self.assertFalse(stale[0].ok)
        self.assertIn("woswoar install", stale[0].detail)

    def test_a_current_hook_passes(self) -> None:
        """Without this the test above passes on a check that always fails."""
        from woswoar import __main__ as main_module

        support.run_cli("install")
        hook = [c for c in main_module._hook_checks() if c.label == "hook"]
        self.assertTrue(hook[0].ok, hook[0].detail)

    def test_the_shell_check_names_the_shell_and_its_floor(self) -> None:
        from woswoar import __main__ as main_module

        checks = main_module._shell_checks()
        self.assertTrue(checks, "no shell was reported")
        self.assertIn("required", checks[0].detail)

    def test_no_repo_is_an_info_line_not_a_failure(self) -> None:
        checks = doctor.repo_checks()
        sync_line = next(c for c in checks if c.label == "sync")
        self.assertIsNone(sync_line.ok)
        self.assertIn("no history repo", sync_line.detail)
        self.assertFalse(report.failed(checks))


if __name__ == "__main__":
    unittest.main()
