"""Tests for the mutation harness.

The harness exists to answer "does this test actually see the fix?", so the
thing it must never do is answer *wrongly*. A false "caught" would bless a test
that guards nothing; a false "SURVIVED" sends a correct test to be rewritten,
which is what happened before there was a harness to share.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tools.mutate import Mutation, verify

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The module every fixture here mutates. One spelling: three fixtures wrote it
#: out separately, so a change to one left the others self-consistent but no
#: longer testing the module the rest of the file targets.
CLAMP = """
def clamp(value: int) -> int:
    if value < 0:
        return 0
    return value
"""


class MutateTestCase(unittest.TestCase):
    """Each test builds a tiny package and mutates *that*, not woswoar."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self._cwd = Path.cwd()
        self.addCleanup(lambda: os.chdir(self._cwd))

        # The harness resolves paths against the working directory, so the
        # sandbox becomes the repo for the duration.
        os.chdir(self.root)

    def write(self, name: str, body: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        return path

    def package(self, guarded: bool) -> None:
        """A module with one behaviour, and a test that may or may not see it."""
        self.write("mod.py", CLAMP)
        assertion = (
            "self.assertEqual(mod.clamp(-5), 0)" if guarded else "self.assertEqual(mod.clamp(5), 5)"
        )
        self.write(
            "test_mod.py",
            f"""
            import unittest

            import mod


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    {assertion}
            """,
        )


class TestItReportsWhatTheTestCanSee(MutateTestCase):
    def test_a_guarded_fix_is_caught(self) -> None:
        self.package(guarded=True)
        survivors = verify(
            [Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")]
        )
        self.assertEqual(survivors, 0)

    def test_a_test_that_cannot_see_it_is_reported(self) -> None:
        """The answer that matters: decoration must not read as a guard."""
        self.package(guarded=False)
        survivors = verify(
            [Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")]
        )
        self.assertEqual(survivors, 1)


class TestItRefusesToGuess(MutateTestCase):
    def test_text_that_appears_twice_is_an_error(self) -> None:
        """Replacing one of two matches quietly tests something else."""
        self.package(guarded=True)
        self.write(
            "mod.py",
            """
            def clamp(value: int) -> int:
                if value < 0:
                    return 0
                return value


            def clamp2(value: int) -> int:
                if value < 0:
                    return 0
                return value
            """,
        )
        with self.assertRaises(SystemExit) as refused:
            verify([Mutation("x", "mod.py", "if value < 0:", "if False:", "test_mod")])
        self.assertIn("not once", str(refused.exception))

    def test_text_that_appears_nowhere_is_an_error(self) -> None:
        self.package(guarded=True)
        with self.assertRaises(SystemExit) as refused:
            verify([Mutation("x", "mod.py", "no such text", "other", "test_mod")])
        self.assertIn("0 times", str(refused.exception))


class TestItRefusesAnEditThatOnlyAdds(MutateTestCase):
    """The mistake this catches shipped three times in one session.

    Writing a *move* as a mutation is easy to get wrong: put only the first half
    of the span in ``old`` and the replacement ends up containing the line you
    meant to relocate, so it is still there underneath the edit. The code under
    test never changes, and the run prints "caught" or "SURVIVED" about nothing
    -- with no way to tell from the output which of the two you are looking at.
    """

    def test_a_replacement_containing_the_original_is_refused(self) -> None:
        self.package(guarded=True)
        with self.assertRaises(SystemExit) as refused:
            verify(
                [
                    Mutation(
                        "the guard is hoisted rather than removed",
                        "mod.py",
                        "    if value < 0:\n        return 0",
                        "    print('noise')\n    if value < 0:\n        return 0",
                        "test_mod",
                    )
                ]
            )
        self.assertIn("survives verbatim", str(refused.exception))

    def test_additive_says_the_insertion_is_the_point(self) -> None:
        """The escape hatch, and it has to exist: inserting a call in front of
        code that stays is how you test the *order* of two steps, which is a real
        mutation with a real answer."""
        self.package(guarded=True)
        survivors = verify(
            [
                Mutation(
                    "an early return is inserted in front of the guard",
                    "mod.py",
                    "    if value < 0:",
                    "    return 99\n    if value < 0:",
                    "test_mod",
                    additive=True,
                )
            ],
            baseline=False,
        )
        self.assertEqual(survivors, 0, "the inserted return should have been caught")

    def test_the_refusal_happens_before_the_file_is_touched(self) -> None:
        """Otherwise the check would trade one wasted run for a mutated tree,
        which is the failure `CLAUDE.md` rule 6 is about."""
        self.package(guarded=True)
        before = (self.root / "mod.py").read_text(encoding="utf-8")
        with self.assertRaises(SystemExit):
            verify([Mutation("x", "mod.py", "return value", "return value  # same", "test_mod")])
        self.assertEqual((self.root / "mod.py").read_text(encoding="utf-8"), before)


class TestTheWorkingTreeIsNeverTouched(MutateTestCase):
    """The stronger claim the sandbox design makes, and the one worth testing.

    The two tests below it check the tree is intact *afterwards*, which was true
    of the earlier design too -- it mutated the source and restored it in a
    `finally`. What that could not survive was a kill in between, which is the
    state CLAUDE.md rule 6 is about and which cost real work here. So this
    watches from inside: the suite a mutation runs reports what it could see of
    the original file at the moment it was running.
    """

    def witnessing(self, witness: Path) -> None:
        """A package whose test records the state of the *original* tree.

        The absolute path is baked in, so the test reads the working tree no
        matter which directory it is running from -- which is the whole question.
        """
        self.write("mod.py", CLAMP)
        original = (self.root / "mod.py").resolve()
        self.write(
            "test_mod.py",
            f"""
            import os
            import unittest
            from pathlib import Path

            import mod


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    Path({str(witness)!r}).write_text(
                        os.getcwd() + "\\n" + Path({str(original)!r}).read_text(),
                        encoding="utf-8",
                    )
                    self.assertEqual(mod.clamp(-5), 0)
            """,
        )

    def test_the_suite_runs_somewhere_else_entirely(self) -> None:
        witness = self.root / "witness.txt"
        self.witnessing(witness)
        verify([Mutation("x", "mod.py", "if value < 0:", "if False:", "test_mod")], baseline=False)
        where, _ = witness.read_text(encoding="utf-8").split("\n", 1)
        self.assertNotEqual(
            Path(where).resolve(),
            self.root.resolve(),
            "the mutation ran in the working tree rather than a copy of it",
        )

    def test_the_original_file_is_unmutated_while_the_suite_runs(self) -> None:
        """Not just restored afterwards. A `finally` gives you the second; only a
        copy gives you the first, and the difference is what happens when the run
        is killed."""
        witness = self.root / "witness.txt"
        self.witnessing(witness)
        verify([Mutation("x", "mod.py", "if value < 0:", "if False:", "test_mod")], baseline=False)
        _, seen = witness.read_text(encoding="utf-8").split("\n", 1)
        self.assertIn("if value < 0:", seen)
        self.assertNotIn("if False:", seen)


class TestItRunsThemInParallel(MutateTestCase):
    """Independent by construction, and mostly waiting on a subprocess.

    Asserted by occupancy rather than by wall clock: each mutation's test drops a
    marker, counts how many markers exist at that moment, and records the count.
    A serial run can never see more than one. A threshold on elapsed time would
    be the same claim with a flake attached.
    """

    def test_more_than_one_mutation_is_in_flight_at_once(self) -> None:
        markers = self.root / "markers"
        markers.mkdir()
        counts = self.root / "counts.txt"
        self.write("mod.py", CLAMP)
        self.write(
            "test_mod.py",
            f"""
            import os
            import time
            import unittest
            from pathlib import Path

            import mod

            MARKERS = Path({str(markers)!r})
            COUNTS = Path({str(counts)!r})


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    mine = MARKERS / str(os.getpid())
                    mine.write_text("here", encoding="utf-8")
                    # Long enough that a parallel run overlaps and a serial one
                    # cannot, without being long enough to matter to the suite.
                    time.sleep(0.4)
                    with COUNTS.open("a", encoding="utf-8") as log:
                        log.write(f"{{len(list(MARKERS.iterdir()))}}\\n")
                    mine.unlink()
                    self.assertEqual(mod.clamp(-5), 0)
            """,
        )
        verify(
            [
                # Not `return value + {index}`: that contains the text it
                # replaces, so the additive guard refuses it -- which it did, to
                # this fixture, within minutes of the guard existing.
                Mutation(f"row {index}", "mod.py", "return value", f"return {index}", "test_mod")
                for index in range(1, 5)
            ],
            baseline=False,
            # Explicit, because the default is derived from the core count and a
            # two-core CI runner would resolve it to a serial run -- so this test
            # would assert the machine rather than the mechanism.
            workers=4,
        )
        seen = [int(line) for line in counts.read_text(encoding="utf-8").split()]
        self.assertTrue(seen, "no mutation reported its occupancy")
        self.assertGreater(max(seen), 1, f"every mutation ran alone: {seen}")


class TestItRestoresTheTree(MutateTestCase):
    def test_the_source_is_unchanged_afterwards(self) -> None:
        self.package(guarded=True)
        before = (self.root / "mod.py").read_text(encoding="utf-8")
        verify([Mutation("x", "mod.py", "if value < 0:", "if False:", "test_mod")])
        self.assertEqual((self.root / "mod.py").read_text(encoding="utf-8"), before)

    def test_it_restores_even_when_the_run_raises(self) -> None:
        """CLAUDE.md rule 6: an interrupted run must not leave the tree mutated."""
        self.package(guarded=True)
        before = (self.root / "mod.py").read_text(encoding="utf-8")
        with self.assertRaises(SystemExit):
            verify(
                [
                    Mutation("first", "mod.py", "if value < 0:", "if False:", "test_mod"),
                    Mutation("second", "mod.py", "not there", "x", "test_mod"),
                ]
            )
        self.assertEqual((self.root / "mod.py").read_text(encoding="utf-8"), before)


class TestTheBytecodeTrap(MutateTestCase):
    """The reason a shared harness is worth more than the loop it replaces.

    A `.pyc` is validated against `(mtime_seconds, size)`. Two mutations that
    change a file by the *same number of bytes* inside the *same second* leave
    the second one running the first one's cached bytecode -- so the second is
    tested against code it does not contain. That reported a correct test as
    decoration once here, and nearly got it rewritten.
    """

    def test_two_same_sized_edits_in_one_second_are_each_tested(self) -> None:
        self.write(
            "mod.py",
            """
            def which() -> str:
                return "aaa"
            """,
        )
        self.write(
            "test_mod.py",
            """
            import unittest

            import mod


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    self.assertEqual(mod.which(), "aaa")
            """,
        )

        # Same length, so the file's size never changes; run back to back, so
        # its mtime second very likely does not either.
        started = time.time()
        survivors = verify(
            [
                Mutation("returns bbb", "mod.py", 'return "aaa"', 'return "bbb"', "test_mod"),
                Mutation("returns ccc", "mod.py", 'return "aaa"', 'return "ccc"', "test_mod"),
            ]
        )
        self.assertEqual(survivors, 0, "a mutation ran against another one's bytecode")
        self.assertLess(
            time.time() - started, 60, "precondition: the two runs shared a wall-clock second"
        )


class TestAMutationThatBreaksTheSuiteIsNotCaught(MutateTestCase):
    """The one hazard here that lies in the direction a reader believes.

    `_run` used to answer "the exit status was non-zero", and a mutation that
    makes the module unimportable exits non-zero too -- so it printed `caught`
    while the test named in the row never ran. That is the inverse of the
    decoration failure rule 3 is about, and it is worse, because a false `caught`
    in a pull request is indistinguishable from a real one.
    """

    def test_a_mutation_that_breaks_the_import_is_refused(self) -> None:
        self.package(guarded=True)
        with self.assertRaises(SystemExit) as refused:
            verify(
                [Mutation("syntax", "mod.py", "def clamp", "def (", "test_mod")],
                baseline=False,
            )
        self.assertIn("ran no tests", str(refused.exception))

    def test_a_real_mutation_is_still_caught(self) -> None:
        """The other half: the check must not refuse an ordinary mutation, which
        also exits non-zero but does so after running the test."""
        self.package(guarded=True)
        self.assertEqual(
            verify(
                [Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")],
                baseline=False,
            ),
            0,
        )


class TestOneLaneIsNotADeadlock(MutateTestCase):
    """The narrowest configuration, and the one that hung three CI jobs.

    With a single lane and a baseline to check, the baseline used to take the only
    borrowable sandbox on the main thread and never give it back, so every
    mutation blocked forever on an empty queue. `workers` defaults to a value
    derived from the core count, which is 16 on the machine this was written on
    and 1 on a two-core runner -- so it passed locally and hung there.

    Driven as a subprocess with a timeout because the failure is a hang. A test
    that waits forever reports nothing, which is the same reason
    `tests/test_setup.py` has an `input` that raises rather than blocks.
    """

    def test_a_single_lane_with_a_baseline_finishes(self) -> None:
        self.package(guarded=True)
        self.write(
            "spec.py",
            """
            from tools.mutate import Mutation, verify

            verify(
                [Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")],
                baseline=True,
                workers=1,
            )
            """,
        )
        try:
            finished = subprocess.run(
                [sys.executable, str(self.root / "spec.py")],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            self.fail("one lane plus a baseline deadlocked: the run never finished")
        self.assertIn("caught", finished.stdout, finished.stderr)


class TestTheScriptEntryPoint(MutateTestCase):
    def test_it_runs_a_table_from_a_file(self) -> None:
        self.package(guarded=True)
        self.write(
            "spec.py",
            """
            from tools.mutate import Mutation

            MUTATIONS = [
                Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")
            ]
            """,
        )
        finished = subprocess.run(
            [sys.executable, "-m", "tools.mutate", str(self.root / "spec.py")],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("caught", finished.stdout)


if __name__ == "__main__":
    unittest.main()
