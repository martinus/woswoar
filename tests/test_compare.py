"""Tests for the output-comparison harness.

One failure mode matters: saying two revisions agree when they do not. The
second, subtler one is saying they agree *after normalising away the part that
differed* -- which reads identically in a pull request and means nothing. So the
report has to name its scrubs, and that is asserted here rather than left to
whoever writes the next comparison to remember.

`verdict` is the half these reach. Recording a tree needs a git worktree and a
subprocess per command, and testing that would measure git.
"""

from __future__ import annotations

import unittest

from tools.compare import verdict


class TestItAgreesOnlyWhenItShould(unittest.TestCase):
    def test_identical_recordings_agree(self) -> None:
        agreed, report = verdict("same\noutput\n", "same\noutput\n", "main (abc123)", 2)
        self.assertTrue(agreed)
        self.assertIn("identical", report)

    def test_one_changed_character_disagrees(self) -> None:
        """The whole point. A refactor meant to change nothing has to fail here
        for a single byte, or the check is a formality."""
        agreed, report = verdict("logs    : x\n", "LOGS    : x\n", "main (abc123)", 1)
        self.assertFalse(agreed)
        self.assertIn("-logs    : x", report)
        self.assertIn("+LOGS    : x", report)

    def test_a_difference_in_trailing_whitespace_is_still_a_difference(self) -> None:
        """Output is compared as bytes, not as words: trailing space is exactly
        the kind of thing a rewritten print loop changes silently."""
        agreed, _ = verdict("line\n", "line \n", "main (abc123)", 1)
        self.assertFalse(agreed)

    def test_reordered_output_is_a_difference(self) -> None:
        """`doctor` reports in a fixed order on purpose, and `report.lines` says
        so. A comparison that sorted first would bless a reordering."""
        agreed, _ = verdict("a\nb\n", "b\na\n", "main (abc123)", 1)
        self.assertFalse(agreed)


class TestTheReportSaysWhatWasNormalised(unittest.TestCase):
    """ "Byte-identical" is only worth what the scrub list next to it is worth."""

    def test_no_scrubs_says_so_out_loud(self) -> None:
        """Rather than an empty heading, which reads as though the question was
        never asked."""
        _, report = verdict("x", "x", "main (abc123)", 1)
        self.assertIn("(none)", report)

    def test_every_scrub_is_named(self) -> None:
        scrubs = [(r"\d+ ms", "N ms"), (r"[0-9a-f]{16}", "ID")]
        _, report = verdict("x", "x", "main (abc123)", 1, scrubs)
        for pattern, replacement in scrubs:
            self.assertIn(f"{pattern} -> {replacement}", report)

    def test_the_scrubs_are_named_on_a_failure_too(self) -> None:
        """The case where it matters most: a reader looking at a diff needs to
        know what was already normalised out of it."""
        _, report = verdict("a", "b", "main (abc123)", 1, [(r"\d+", "N")])
        self.assertIn(r"\d+ -> N", report)

    def test_the_revision_is_named_in_both_the_header_and_the_diff(self) -> None:
        _, report = verdict("a", "b", "main (abc123)", 1)
        self.assertIn("main (abc123)", report.splitlines()[0])
        self.assertIn("--- main (abc123)", report)


if __name__ == "__main__":
    unittest.main()
