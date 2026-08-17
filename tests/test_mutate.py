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
        self.write(
            "mod.py",
            """
            def clamp(value: int) -> int:
                if value < 0:
                    return 0
                return value
            """,
        )
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
