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
from typing import ClassVar

from woswoar import doctor, install, report, sync
from woswoar.report import Check, Notice

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


class TestNotices(unittest.TestCase):
    """The paragraph sibling of `Check`, and `sync`'s decision about which apply.

    Every one of these was a `if report.X:` block inside `cmd_sync` that printed
    to stderr, so "does this run warn about a changed signer" could only be asked
    by running a sync and grepping. The severity in particular was the *word*
    `WARNING` inside the prose; it is a field now.
    """

    def test_a_warning_says_so_and_a_plain_notice_does_not(self) -> None:
        self.assertEqual(report.paragraphs([Notice("body")]), ["\nbody"])
        self.assertEqual(report.paragraphs([Notice("body", warning=True)]), ["\nWARNING: body"])

    def test_the_blank_line_belongs_to_the_renderer(self) -> None:
        """It used to be the first character of all eleven prose strings, which
        is a fact about printing paragraphs rather than about any of them."""
        for block in report.paragraphs([Notice("a"), Notice("b", warning=True)]):
            self.assertTrue(block.startswith("\n"), block)

    def test_a_clean_run_says_nothing(self) -> None:
        self.assertEqual(sync.Report().notices(), [])

    #: Every kind that produces a notice, in report order, and whether it
    #: shouts. Written out by hand on purpose: `sync.OUTCOMES` carries both
    #: facts now, so reading the severity off the kind and asserting it against
    #: itself would pass whatever the declaration said. This is the second
    #: opinion, and the test below ties the two together so a new kind cannot
    #: arrive without one.
    STATES: ClassVar[dict[str, bool]] = {
        "unreadable": False,
        "stale": False,
        "untrusted": False,
        "unpinned": False,
        "changed_signer": True,
        "unsignable": True,
        "orphaned": True,
        "manifest_missing": True,
        "foreign": False,
        "unauthenticated": True,
    }

    def test_each_kind_produces_exactly_one_notice_at_the_right_volume(self) -> None:
        """Severity is data now, so it can be asserted rather than grepped for.
        The quiet ones are states that are not going wrong -- a machine waiting
        to be accepted -- and the loud ones are the repository disagreeing with
        this machine, or history that could not be published."""
        for kind in sync.OUTCOMES:
            with self.subTest(kind=kind.name):
                report_ = sync.Report()
                report_.record(kind, "host/2026-01-01")
                notices = report_.notices()
                self.assertEqual(len(notices), 1, f"{kind.name} produced the wrong count")
                self.assertIs(notices[0].warning, self.STATES[kind.name])

    def test_every_kind_is_covered_by_the_table_above(self) -> None:
        """Both directions, and both matter. A kind missing from `STATES` is one
        whose severity nobody stated twice; a row left in `STATES` after its kind
        went is a `KeyError` waiting in the loop above rather than here, which is
        a worse place to read it."""
        self.assertEqual([kind.name for kind in sync.OUTCOMES], list(self.STATES))

    def test_the_notices_come_out_in_the_order_the_kinds_are_declared(self) -> None:
        """Recorded backwards on purpose. `Report.outcomes` is a dict, and a dict
        preserves the order things were put into it -- so a `notices` that looped
        over what this run happened to record, rather than over `OUTCOMES`, would
        pass any test that recorded them in the right order to begin with. What
        `cmd_sync` printed was a fixed sequence of `if` blocks, and it should
        stay one.
        """
        every = sync.Report()
        for kind in reversed(sync.OUTCOMES):
            every.record(kind, "host/2026-01-01")
        self.assertEqual(
            [notice.body for notice in every.notices()],
            [kind.body(["host/2026-01-01"]) for kind in sync.OUTCOMES],
        )

    def test_a_revoked_machine_is_told_that_and_nothing_else(self) -> None:
        """Every other line would describe work it did not do: it publishes
        nothing and merges nothing."""
        report_ = sync.Report(revoked=True)
        report_.record(sync.UNREADABLE, "host/2026-01-01")
        report_.record(sync.UNAUTHENTICATED, "host/2026-01-02")
        notices = report_.notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("revocation is permanent", notices[0].body)

    def test_the_days_a_notice_names_are_listed_in_order(self) -> None:
        """Three notices interpolate their set, and a set has no order.

        Twelve days, not two, and that is the point: with two, the set's own
        iteration order is already sorted about half the time, so dropping the
        `sorted()` left this passing -- the fixture trap `CLAUDE.md` rule 3 names
        first, hit exactly. `str` hashing is seed-randomised, so no fixed set can
        make a wrong order *certain*; twelve makes an accidental pass one run in
        a few hundred million, well below any flake worth having.
        """
        days = [f"2026-01-{n:02d}" for n in range(1, 13)]
        report_ = sync.Report()
        for day in days:
            report_.record(sync.ORPHANED, day)
        body = report_.notices()[0].body
        listed = body.split("cannot be published: ")[1].split("\n")[0].split(", ")
        self.assertEqual(listed, sorted(days))


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


class TestWidthAsATerminalSeesIt(unittest.TestCase):
    """`fleet` puts markers in a grid, which is the one place a marker is not
    the first thing on its line -- and the first thing on a line needs no
    padding, so nothing measured a marker until there was a table of them.

    A coloured tick is ten characters that draw one column. Every ordinary way
    of padding counts the ten, so a table laid out with `str.center` or a format
    spec loses its columns exactly when there is colour to align them by, and is
    perfectly aligned in the pipe a test captures. That is why these assert on
    `COLOUR_MARKERS` rather than on a string of the test's own.
    """

    def test_a_coloured_marker_measures_the_column_it_draws(self) -> None:
        self.assertEqual(report.visible(report.COLOUR_MARKERS["ok"]), 1)
        self.assertEqual(report.visible(report.PLAIN_MARKERS["fail"]), len("[FAIL]"))

    def test_text_with_no_escapes_is_its_own_length(self) -> None:
        self.assertEqual(report.visible("(unverified)"), len("(unverified)"))

    def test_padding_is_counted_in_columns_not_characters(self) -> None:
        """The bug this exists to prevent, stated as the difference it makes:
        the format spec pads a coloured marker by nothing at all, because it is
        already ten characters wide by its own reckoning."""
        marker = report.COLOUR_MARKERS["ok"]
        self.assertEqual(report.visible(report.centred(marker, 5)), 5)
        self.assertEqual(f"{marker:^5}", marker, "sanity: what the format spec does instead")

    def test_the_padding_is_split_with_the_odd_column_on_the_right(self) -> None:
        """Which side the odd space goes is arbitrary; that it is *decided* is
        not. A centring that rounded the other way in one branch and this way in
        another would ripple through a table as a column that moves by one."""
        self.assertEqual(report.centred("x", 4), " x  ")
        self.assertEqual(report.centred("x", 5), "  x  ")

    def test_something_wider_than_the_column_is_left_alone(self) -> None:
        """As the format spec does: a cell that does not fit is better ragged
        than cut, which is the same call `report.lines` makes for a long label."""
        self.assertEqual(report.centred("[FAIL]", 2), "[FAIL]")


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
        from woswoar import store

        support.run_cli("install")
        hook = store.data_dir() / install.HOOKS["bash"]
        hook.write_bytes(b"# an older woswoar wrote this\n")
        stale = [c for c in install.hook_checks() if c.label == "hook"]
        self.assertTrue(stale, "no hook check was produced")
        self.assertFalse(stale[0].ok)
        self.assertIn("woswoar install", stale[0].detail)

    def test_a_current_hook_passes(self) -> None:
        """Without this the test above passes on a check that always fails."""

        support.run_cli("install")
        hook = [c for c in install.hook_checks() if c.label == "hook"]
        self.assertTrue(hook[0].ok, hook[0].detail)

    def test_the_shell_check_names_the_shell_and_its_floor(self) -> None:

        checks = install.shell_checks()
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
