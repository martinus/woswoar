"""Tests for the A/B timing harness.

Every one of these is about the harness reaching a *wrong* conclusion, because
that is the only failure that matters here: a benchmark nobody trusts gets rerun,
and a benchmark that is confidently wrong gets quoted in a pull request. Both
have happened -- the number that prompted this tool was inflated by a base
worktree sitting on tmpfs while the tree it was compared against sat on btrfs,
and then summarised with a statistic that threw the pairing away.

The timings here are synthetic on purpose. A test that actually ran two trees
would measure this machine rather than the arithmetic, and could not state what
the right answer was.
"""

from __future__ import annotations

import unittest

from tools.bench import BLOCKS, Pair, comparable, sign_p, summarise
from tools.bench import _pairs_from as pairs_from


def drifting(blocks: int, drift: float, offset: float = 0.0) -> list[Pair]:
    """Pairs from a machine getting steadily slower, with a known real effect.

    ``drift`` is added per measurement slot, whichever side happens to occupy it;
    ``offset`` is added to the working tree's readings only, and is therefore the
    answer the harness is supposed to find.
    """
    out: list[Pair] = []
    slot = 0
    for index in range(blocks):
        block = BLOCKS[index % len(BLOCKS)]
        samples = []
        for letter in block:
            value = 10.0 + drift * slot + (offset if letter == "B" else 0.0)
            samples.append(value)
            slot += 1
        out += pairs_from(block, samples)
    return out


class TestTheBlockDesignCancelsDrift(unittest.TestCase):
    """The reason the order is ABBA/BAAB and not ABAB."""

    def test_a_steady_slowdown_is_not_reported_as_a_difference(self) -> None:
        summary = summarise(drifting(blocks=20, drift=0.5))
        self.assertAlmostEqual(summary.median, 0.0, places=9)
        self.assertFalse(summary.resolved, "drift alone was reported as a real effect")

    def test_the_naive_alternation_would_have_reported_it(self) -> None:
        """What the design is protecting against, stated as a test so that anyone
        tempted to simplify the block order can see the cost.

        In A,B,A,B the working tree always occupies the later slot of its pair, so
        every single difference carries one step of the drift and the harness
        resolves a slowdown that does not exist.
        """
        drift = 0.5
        naive = [
            Pair(base=10.0 + drift * (2 * i), tree=10.0 + drift * (2 * i + 1), tree_first=False)
            for i in range(40)
        ]
        summary = summarise(naive)
        self.assertAlmostEqual(summary.median, drift, places=9)
        self.assertTrue(summary.resolved, "the fixture failed to reproduce the bias")

    def test_each_block_uses_both_orders(self) -> None:
        """A counterbalance nobody checks is a claim rather than a control."""
        for block in BLOCKS:
            with self.subTest(block=block):
                pairs = pairs_from(block, [1.0, 2.0, 3.0, 4.0])
                self.assertEqual(sorted(pair.tree_first for pair in pairs), [False, True])

    def test_both_readings_of_a_pair_are_adjacent_in_time(self) -> None:
        """Pairing the two ends of a block against each other would leave two
        slots of drift inside every difference, which is the bias this is for."""
        for block in BLOCKS:
            with self.subTest(block=block):
                for pair in pairs_from(block, [1.0, 2.0, 3.0, 4.0]):
                    self.assertEqual(abs(pair.base - pair.tree), 1.0)


class TestItFindsARealDifference(unittest.TestCase):
    def test_an_offset_larger_than_the_drift_is_resolved(self) -> None:
        summary = summarise(drifting(blocks=20, drift=0.1, offset=1.0))
        self.assertAlmostEqual(summary.median, 1.0, places=9)
        self.assertTrue(summary.resolved)
        self.assertEqual(summary.tree_slower, summary.pairs)

    def test_the_direction_is_reported_the_way_a_reader_expects(self) -> None:
        """Positive means the working tree is slower. Getting this backwards
        would invert every conclusion the tool is used for."""
        faster = summarise(drifting(blocks=10, drift=0.0, offset=-2.0))
        self.assertLess(faster.median, 0)
        self.assertEqual(faster.tree_slower, 0)


class TestTheSidesMustHaveDoneTheSameWork(unittest.TestCase):
    """The hole the three documented traps left open.

    A revision where the flag did not exist yet exits in 40 ms with a usage error
    while the working tree does 300 ms of work, and a harness that only times
    them calls that a resolved slowdown.
    """

    def test_agreeing_statuses_are_reported_not_refused(self) -> None:
        self.assertIn("exited 0", comparable({0}))

    def test_a_consistently_failing_command_is_still_comparable(self) -> None:
        """`doctor` exits 1 whenever it has something to report, so refusing every
        non-zero status would make the most interesting command unbenchmarkable."""
        self.assertIn("exited 1", comparable({1}))

    def test_disagreement_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            comparable({0, 2})
        self.assertIn("did not", str(refused.exception))

    def test_no_readings_is_not_a_crash(self) -> None:
        self.assertIn("exited 0", comparable(set()))


class TestTheCounterbalanceIsCounted(unittest.TestCase):
    def test_the_report_line_comes_from_the_pairs_and_not_arithmetic(self) -> None:
        """It used to be derived as `pairs // 2`, which prints the same string
        whatever the pairs contain -- so the line claiming both orders were used
        was itself the unchecked claim its comment warned about."""
        summary = summarise(drifting(blocks=10, drift=0.0))
        self.assertEqual(summary.tree_first, summary.pairs // 2)

        lopsided = [Pair(1.0, 2.0, tree_first=True) for _ in range(4)]
        self.assertEqual(summarise(lopsided).tree_first, 4)


class TestTheSignTest(unittest.TestCase):
    def test_an_even_split_is_the_least_significant_thing_possible(self) -> None:
        self.assertEqual(sign_p(15, 30), 1.0)

    def test_a_clean_sweep_is_significant(self) -> None:
        self.assertLess(sign_p(30, 30), 0.001)

    def test_it_is_two_sided(self) -> None:
        """A tree that is consistently *faster* is just as much a result, and a
        one-sided test would call it noise."""
        self.assertEqual(sign_p(0, 20), sign_p(20, 20))

    def test_a_handful_of_pairs_cannot_resolve_anything(self) -> None:
        """Guards against the tool being run with `--blocks 1` and believed: with
        two pairs even a unanimous split is one coin flip in two."""
        self.assertGreater(sign_p(2, 2), 0.05)

    def test_no_pairs_is_not_a_division_by_zero(self) -> None:
        self.assertEqual(sign_p(0, 0), 1.0)


if __name__ == "__main__":
    unittest.main()
