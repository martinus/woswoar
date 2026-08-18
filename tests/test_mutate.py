"""Tests for the mutation harness.

The harness exists to answer "does this test actually see the fix?", so the
thing it must never do is answer *wrongly*. A false "caught" would bless a test
that guards nothing; a false "SURVIVED" sends a correct test to be rewritten,
which is what happened before there was a harness to share.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from tools import mutate, reached
from tools.mutate import MEMORY, Mutation, Report, Result, Verdict, confirm, run, verify

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

    def refuses(self, mutation: Mutation) -> None:
        """Every test here asserts the same thing about a different fixture."""
        with self.assertRaises(SystemExit) as refused:
            verify([mutation], baseline=False)
        self.assertIn("broke collection", str(refused.exception))

    def test_a_mutation_that_breaks_the_import_is_refused(self) -> None:
        """A *syntax* error, which is only one of the two ways collection breaks.

        This propagates out of `unittest.loader` entirely, so there is no `Ran`
        line to read. It is the narrow case, and for a long time it was the only
        one here -- which is why the two below could exist unnoticed.
        """
        self.package(guarded=True)
        self.refuses(Mutation("syntax", "mod.py", "def clamp", "def (", "test_mod"))

    def test_an_import_error_is_not_a_catch(self) -> None:
        """The shape the fixture above cannot reach, and it reported `caught`.

        `unittest/loader.py` catches `ImportError` -- and only `ImportError` --
        and turns it into a synthetic `unittest.loader._FailedTest`. So the run
        prints `Ran 1 test`, `FAILED (errors=1)` and exits non-zero, which a
        check reading the exit status and the count cannot tell apart from a test
        noticing the mutation. A SyntaxError takes the other path and produces no
        count at all, so the sibling fixture above passed on code that got this
        one wrong: the two answers differ, and only one of them was ever asked.
        """
        self.package(guarded=True)
        self.write("mod.py", "import json\n" + CLAMP)
        self.refuses(
            Mutation("the import goes bad", "mod.py", "import json", "import nope_xyz", "test_mod")
        )

    def test_a_broken_setupclass_is_not_a_catch(self) -> None:
        """`Ran N` is greater than zero here, and still nothing asserted.

        The first class runs and passes; the second's `setUpClass` dies, which
        `unittest` reports through an `_ErrorHolder` that is not counted in
        `testsRun`. So the count is honest, non-zero, and says nothing about
        whether the mutation was noticed -- the reason the verdict cannot be a
        function of the count alone.
        """
        self.write("mod.py", CLAMP + '\n\ndef helper() -> str:\n    return "ok"\n')
        self.write(
            "test_mod.py",
            """
            import unittest

            import mod


            class A(unittest.TestCase):
                def test_it(self) -> None:
                    self.assertEqual(mod.clamp(-5), 0)


            class B(unittest.TestCase):
                @classmethod
                def setUpClass(cls) -> None:
                    cls.shouted = mod.helper().upper()

                def test_it(self) -> None:
                    self.assertEqual(self.shouted, "OK")
            """,
        )
        self.refuses(
            Mutation("helper returns nothing", "mod.py", 'return "ok"', "return None", "test_mod")
        )

    def test_a_run_that_died_without_answering_is_not_a_survivor(self) -> None:
        """The last route, and the only one the probe cannot report on itself.

        `os._exit` skips every `except` and `finally` there is, so the probe is
        gone before it can say what happened -- which is what a signal, an OOM
        kill or a segfault in a C extension look like from outside. The parent
        has nothing to read, and the honest reading of nothing is `broke`.

        Written because the mutation that removes this branch survived: the probe
        gained an `except BaseException` that writes `loaded: false`, and that
        made every fixture reach the *other* branch. The code was right and the
        fixtures could no longer tell.
        """
        self.package(guarded=True)
        self.write("test_mod.py", "import os\n\nos._exit(3)\n")
        self.refuses(
            Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")
        )

    def test_a_target_holding_no_tests_is_not_a_survivor(self) -> None:
        """The third route to nothing having been asked, and the quietest.

        The target imports, collects, and holds no test methods at all -- so the
        run is green, exits zero, and would read as `SURVIVED`: a report that the
        fix is unguarded, produced by a table pointed at a module that could not
        have guarded anything. Found by mutating this file: the branch existed
        and nothing reached it.
        """
        self.package(guarded=True)
        self.write("test_empty.py", "import unittest\n")
        self.refuses(
            Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_empty")
        )

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


class TestASpanPicksOneOfSeveralIdenticalLines(MutateTestCase):
    """What a generated row needs that a hand-written one never did.

    `if value < 0:` twice in a file is an error in a hand table -- `check`
    refuses it, because the edit could land on either. A generated row means a
    specific one, and says which by character offset. Watched from inside the
    sandbox, because "the right one changed" is not visible from the outcome:
    both mutations make the suite fail, so a run that edited the wrong function
    reports `caught` just as loudly.
    """

    def test_the_second_occurrence_is_the_one_that_changes(self) -> None:
        witness = self.root / "seen.txt"
        body = (
            "def first(value: int) -> int:\n"
            "    if value < 0:\n"
            "        return 0\n"
            "    return value\n"
            "\n"
            "\n"
            "def second(value: int) -> int:\n"
            "    if value < 0:\n"
            "        return 0\n"
            "    return value\n"
        )
        self.write("mod.py", body)
        self.write(
            "test_mod.py",
            f"""
            import unittest
            from pathlib import Path

            import mod

            SEEN = Path({str(witness)!r})


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    SEEN.write_text(f"{{mod.first(-5)}},{{mod.second(-5)}}", encoding="utf-8")
            """,
        )
        # The *second* `if value < 0:`. Both spellings are identical, so nothing
        # but the offsets distinguishes them.
        at = body.index("if value < 0:", body.index("def second"))
        run(
            [
                Mutation(
                    "the second guard is gone",
                    "mod.py",
                    "if value < 0:",
                    "if False:",
                    "test_mod",
                    span=(at, at + len("if value < 0:")),
                )
            ],
            baseline=False,
        )
        self.assertEqual(
            witness.read_text(encoding="utf-8"),
            "0,-5",
            "the mutation landed on the first occurrence, not the one it named",
        )


class TestASpanThatNoLongerHoldsItsText(MutateTestCase):
    """The generated row's half of "refuse a spec that cannot mean anything".

    A hand-written row is refused when its text matches other than once. A
    generated row makes the stronger claim -- the text is *here* -- and the file
    it was generated from can have moved on since: `--list` writes a table, the
    author edits a line above it, and every offset below shifts. Splicing at a
    stale offset produces a file that usually still parses and a verdict about
    whatever now sits there.
    """

    def test_it_is_refused_before_anything_runs(self) -> None:
        self.package(guarded=True)
        before = (self.root / "mod.py").read_text(encoding="utf-8")
        with self.assertRaises(SystemExit) as refused:
            verify(
                [
                    Mutation(
                        "the guard is gone",
                        "mod.py",
                        "if value < 0:",
                        "if False:",
                        "test_mod",
                        # Off by two characters: the text is in the file, just not
                        # at the offsets the row names.
                        span=(
                            before.index("if value < 0:") + 2,
                            before.index("if value < 0:") + 15,
                        ),
                    )
                ],
                baseline=False,
            )
        self.assertIn("no longer holds that text", str(refused.exception))
        self.assertEqual((self.root / "mod.py").read_text(encoding="utf-8"), before)


class TestConfirmingSurvivorsAgainstTheWholeSuite(MutateTestCase):
    """The pass that makes narrow test selection a speed decision, not a wrong one.

    A row run against too few tests survives for the wrong reason, and that error
    points the expensive way: it sends the author to rewrite a test that was
    never weak. So every survivor is re-run against everything before it is
    printed.
    """

    def two_rows_with_one_label(self) -> list[Mutation]:
        """Two mutations that share a label, differ only by span, and differ in
        what the *wider* suite says about them.

        Both must survive the narrow pass, or `confirm` never re-runs them and
        nothing can spread: a first version of this fixture had one row caught
        immediately, so `corrected` was empty and the mutation reverting the fix
        survived the test that was supposed to guard it.

        A shared label is not contrived. `generate` dedupes on `(span, new)`, not
        on the label, so two `.startswith(...)` calls on one line produce this
        exactly -- and on the branch that added the generator, 23 labels repeat.
        """
        body = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
        self.write("mod.py", body)
        # Narrow: sees neither function, so both rows survive the first pass.
        self.write(
            "test_narrow.py",
            """
            import unittest

            import mod


            class Narrow(unittest.TestCase):
                def test_nothing_in_particular(self) -> None:
                    self.assertTrue(hasattr(mod, "first"))
            """,
        )
        # Wide: found only by discovery, and sees `first` alone.
        self.write(
            "test_wide.py",
            """
            import unittest

            import mod


            class Wide(unittest.TestCase):
                def test_first(self) -> None:
                    self.assertEqual(mod.first(), 1)
            """,
        )
        shared = "mod.py:2 in f() -- `1` becomes `9`"
        at_one = body.index("return 1") + len("return ")
        at_two = body.index("return 2") + len("return ")
        return [
            Mutation(shared, "mod.py", "1", "9", "test_narrow", span=(at_one, at_one + 1)),
            Mutation(shared, "mod.py", "2", "9", "test_narrow", span=(at_two, at_two + 1)),
        ]

    def test_a_shared_label_does_not_spread_one_rows_answer_to_another(self) -> None:
        """Keying the corrections by label wrote the caught row's verdict onto the
        other, so a genuine survivor was reported as `caught` -- naming a test
        that had never seen it, and exiting zero. A false `caught` in a pull
        request is the one outcome this whole module exists to make impossible.
        """
        report = run(self.two_rows_with_one_label(), baseline=False, strict=False)
        self.assertEqual(
            [result.verdict.outcome for result in report.results],
            ["survived", "survived"],
            "precondition: both rows must survive, or nothing is re-run",
        )

        confirmed = confirm(report, workers=None, timeout=60.0, memory=MEMORY)
        self.assertEqual(
            [result.verdict.outcome for result in confirmed.results],
            ["caught", "survived"],
            "one row's confirmation was written onto the other",
        )
        self.assertFalse(confirmed.clean)


class TestASubTestIsARealAnswer(MutateTestCase):
    """The regression the first version of this classification shipped with.

    Telling a real test from one of `unittest`'s synthetic carriers by asking
    where its class is defined looks right and is not: `unittest.case._SubTest`
    is a `TestCase` living in `unittest.case`, so a mutation whose only witness
    was an assertion inside `with self.subTest(...)` was filed as "the suite
    broke" -- and, the table being strict, aborted the run while blaming the
    table. This repository uses `subTest` in more than twenty places, including
    `tests/test_credentials.py` and `tests/test_architecture.py`.

    The fix is to classify by protocol: `addSubTest` is handed the *owning* test,
    so recording that is both correct and free of any private name.
    """

    def test_a_mutation_seen_only_inside_a_subtest_is_caught(self) -> None:
        self.write("mod.py", CLAMP)
        self.write(
            "test_mod.py",
            """
            import unittest

            import mod


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    # Every assertion this class makes is inside a subTest, which
                    # is the whole point: there is no plain failure to fall back
                    # on if the subTest route is misfiled.
                    for value in (-5, -1):
                        with self.subTest(value=value):
                            self.assertEqual(mod.clamp(value), 0)
            """,
        )
        survivors = verify(
            [Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")],
            baseline=False,
        )
        self.assertEqual(survivors, 0, "a subTest assertion is a test noticing")


class TestAnUnanswerableRowNeedNotEndTheRun(MutateTestCase):
    """Strict is right for a hand table and wrong for a generated one.

    A spec file is written by someone, so a row that cannot be answered is their
    mistake and stopping is what gets it fixed. A table generated from a diff is
    two hundred rows nobody wrote, and throwing away every answer already paid
    for because one mutant broke an import is a different trade entirely.
    """

    def test_the_run_finishes_and_reports_all_three_outcomes(self) -> None:
        self.package(guarded=True)
        self.write("mod.py", "import json\n" + CLAMP)
        report = run(
            [
                Mutation("the guard goes", "mod.py", "if value < 0:", "if False:", "test_mod"),
                Mutation(
                    "the import goes bad", "mod.py", "import json", "import nope_xyz", "test_mod"
                ),
                Mutation(
                    "the pass-through changes", "mod.py", "return value", "return 99", "test_mod"
                ),
            ],
            baseline=False,
            strict=False,
        )
        self.assertEqual(
            [result.verdict.outcome for result in report.results],
            ["caught", "broke", "survived"],
        )

    def test_strict_is_still_the_default(self) -> None:
        """Because every spec file in the repository's history relies on it."""
        self.package(guarded=True)
        self.write("mod.py", "import json\n" + CLAMP)
        with self.assertRaises(SystemExit):
            run(
                [Mutation("bad", "mod.py", "import json", "import nope_xyz", "test_mod")],
                baseline=False,
            )


class TestTheJsonReport(unittest.TestCase):
    """The contract `tools/reached.py` reads, tested from this side of it.

    Both tools would otherwise agree only by coincidence: `reached` parses a
    shape nothing here promises, and a renamed key would break the analysis
    with a `KeyError` in a different file at the end of a run measured in
    hours.
    """

    def report(self) -> Report:
        rows = [
            Mutation(
                "mod.py:7 in f() -- `<` becomes `<=`",
                "mod.py",
                "a",
                "b",
                "test_mod",
                operator="boundary",
            ),
            Mutation("written by hand", "mod.py", "c", "d", "test_mod"),
        ]
        return Report(
            [
                Result(rows[0], Verdict("survived")),
                Result(rows[1], Verdict("broke", "it fell over")),
            ]
        )

    def written(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as box:
            where = Path(box) / "out.json"
            with contextlib.redirect_stdout(io.StringIO()):
                mutate._persist(self.report(), where)
            loaded: dict[str, Any] = json.loads(where.read_text(encoding="utf-8"))
        return loaded

    def test_every_key_reached_needs_is_present(self) -> None:
        first = self.written()["results"][0]
        self.assertEqual(first["path"], "mod.py")
        self.assertEqual(first["line"], 7, "the line is parsed out of the label")
        self.assertEqual(first["outcome"], "survived")
        self.assertEqual(first["operator"], "boundary")

    def test_a_hand_written_row_has_no_line_rather_than_a_wrong_one(self) -> None:
        """`reached` skips these. Inventing a line would file a spec row under
        whichever source line happened to be first."""
        self.assertIsNone(self.written()["results"][1]["line"])

    def test_outcomes_that_asked_nothing_are_kept(self) -> None:
        """`broke` is not a survivor and not a catch, and `reached` needs to see
        it to leave it out of the partition rather than guess."""
        self.assertEqual(self.written()["results"][1]["outcome"], "broke")

    def test_it_round_trips_through_reached(self) -> None:
        """The actual contract: what one writes, the other reads."""
        rows = reached.rows_from(self.written())
        self.assertEqual([r.line for r in rows], [7], "only positioned rows survive the read")
        self.assertTrue(rows[0].survived)


class TestAllDropsTheDiffSizedCap(unittest.TestCase):
    """`--limit`'s default is sized for one change's table.

    Left in place it turned the documented `--all` into 200 rows of several
    thousand -- and, because the cap spreads across files, into batches of about
    seven, so batching, incremental `--json` and resume all did nothing on the
    one command line anybody would type. Driven through `main` rather than the
    parser, because the reset happens after parsing and `--list` runs nothing.
    """

    def listed(self, *argv: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mutate.main([*argv, "--list"])
        return out.getvalue()

    def test_all_runs_the_whole_table(self) -> None:
        self.assertNotIn("not run", self.listed("--all"))

    def test_an_explicit_limit_is_still_honoured(self) -> None:
        """Reset, not ignored: someone who says `--limit 50` means it."""
        self.assertIn("--limit 50:", self.listed("--all", "--limit", "50"))

    def test_a_diff_run_keeps_the_default(self) -> None:
        """The default exists for this shape and must not be collateral."""
        self.assertEqual(mutate.LIMIT, 200)


class TestResumingASweep(unittest.TestCase):
    """What a resumed sweep must keep, not just what it may skip.

    `sweep` had no test at all, and that is exactly why it shipped losing data:
    a resume rewrote `--json` with only the batches it had run, deleting the
    answers it had decided to skip. Recovery eating the thing it recovers is
    invisible unless something drives the round trip.
    """

    def rows(self, *labels: str) -> list[Result]:
        return [
            Result(
                Mutation(label, label.split(":")[0], "a", "b", "test_mod", operator="boundary"),
                Verdict("survived"),
            )
            for label in labels
        ]

    def persisted(self, results: list[Result]) -> Path:
        box = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, box, True)
        where = Path(box) / "out.json"
        with contextlib.redirect_stdout(io.StringIO()):
            mutate._persist(Report(results), where)
        return where

    def test_a_recorded_row_comes_back_whole(self) -> None:
        """Not just its label: `confirm` needs `old` and `new` to re-run it."""
        back = mutate._recorded(self.persisted(self.rows("mod.py:1 -- x")))
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].mutation.old, "a")
        self.assertEqual(back[0].mutation.new, "b")
        self.assertEqual(back[0].verdict.outcome, "survived")

    def test_a_resumed_sweep_keeps_what_it_skipped(self) -> None:
        """The bug this class exists for. Sweep a second file against a report
        that already holds the first, and the first must still be there."""
        where = self.persisted(self.rows("a.py:1 -- one", "a.py:2 -- two"))
        # `a.py` is in the table too, or there would be nothing to skip and the
        # test would pass against a sweep that simply never saw it.
        table = [
            Mutation("a.py:1 -- one", "a.py", "a", "b", "test_mod"),
            Mutation("b.py:1 -- three", "b.py", "c", "d", "test_mod"),
        ]
        args = argparse.Namespace(
            json=where,
            no_baseline=True,
            workers=1,
            timeout=30.0,
            memory=mutate.MEMORY,
            all=True,
            batch=True,
        )
        # `run` is stubbed: `sweep`'s job is the bookkeeping around it -- which
        # files to skip, what to keep, what to write -- and running a real
        # mutation would test `run`, which has its own tests above.
        answered = Report([Result(table[1], Verdict("caught"))])
        with (
            contextlib.redirect_stdout(io.StringIO()) as said,
            mock.patch.object(mutate, "run", return_value=answered),
        ):
            report = mutate.sweep(table, args)

        self.assertIn("a.py: already recorded, skipping", said.getvalue())
        paths = sorted({result.mutation.path for result in report.results})
        self.assertEqual(paths, ["a.py", "b.py"], "the skipped file left the report")
        on_disk = json.loads(where.read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["results"]), 3, "a resume must not shorten the report")

    def test_the_skipped_rows_still_reach_the_summary(self) -> None:
        """A resume whose new batches are all caught would otherwise print a
        clean summary and exit 0 while recorded survivors went unmentioned."""
        where = self.persisted(self.rows("a.py:1 -- survivor"))
        args = argparse.Namespace(
            json=where,
            no_baseline=True,
            workers=1,
            timeout=30.0,
            memory=mutate.MEMORY,
            all=True,
            batch=True,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            report = mutate.sweep([], args)
        self.assertFalse(report.clean, "a recorded survivor still counts against the run")

    def test_a_missing_file_is_not_an_error(self) -> None:
        """The first run of a sweep has nothing to resume from."""
        with tempfile.TemporaryDirectory() as box:
            self.assertEqual(mutate._recorded(Path(box) / "absent.json"), [])

    def test_no_file_asked_for_at_all(self) -> None:
        self.assertEqual(mutate._recorded(None), [])

    def test_a_corrupt_report_resumes_from_nothing_rather_than_dying(self) -> None:
        """A half-written file is what a crash mid-write leaves, and re-running
        everything is the safe reading of it."""
        with tempfile.TemporaryDirectory() as box:
            where = Path(box) / "half.json"
            where.write_text('{"results": [{"label": "one",', encoding="utf-8")
            self.assertEqual(mutate._recorded(where), [])

    def test_a_report_missing_the_rebuild_fields_resumes_from_nothing(self) -> None:
        """An older report has no `old`/`new`. Re-running is right: a row that
        cannot be rebuilt cannot be confirmed either."""
        with tempfile.TemporaryDirectory() as box:
            where = Path(box) / "old.json"
            where.write_text(
                json.dumps({"results": [{"label": "x", "path": "a.py", "outcome": "caught"}]}),
                encoding="utf-8",
            )
            self.assertEqual(mutate._recorded(where), [])


class TestHowManyLanesFitInMemory(unittest.TestCase):
    """The bound that stops the per-row cap from being multiplied by the lanes.

    Untested when it was written, and two of the three things asserted here were
    wrong at that point: the budget was divided by the 4 GiB ceiling rather than
    by what a lane uses, and the host's memory was read without asking whether a
    container had said otherwise.
    """

    def test_a_small_machine_still_gets_a_lane(self) -> None:
        """`max(1, ...)` is load-bearing: `ThreadPoolExecutor(max_workers=0)`
        raises `ValueError`, so a machine too small to afford one lane would
        crash rather than run slowly."""
        with mock.patch.object(mutate, "_visible_memory", return_value=64 << 20):
            self.assertEqual(mutate._affordable(), 1)

    def test_it_scales_with_memory_rather_than_with_the_ceiling(self) -> None:
        """An ordinary laptop must not be cut to the pathological case.

        Dividing by `MEMORY` gave a 16 GiB machine two lanes. `MEMORY` is what
        one runaway may reach before it is killed; `_LANE` is what an honest
        lane occupies, and that is what the budget divides by.
        """
        with mock.patch.object(mutate, "_visible_memory", return_value=16 << 30):
            self.assertEqual(mutate._affordable(), 8)
        self.assertLess(mutate._LANE, mutate.MEMORY, "the ceiling is not the expected use")

    def test_half_the_memory_is_left_for_whoever_is_using_the_machine(self) -> None:
        with mock.patch.object(mutate, "_visible_memory", return_value=32 * mutate._LANE):
            self.assertEqual(mutate._affordable(), 16)

    def test_a_container_limit_beats_the_host_total(self) -> None:
        """The mistake `usable_cpus` documents for CPUs, one resource over.

        Driven through a real file rather than a mock of the read, because the
        thing being tested is that the cgroup file is consulted at all.
        """
        with tempfile.TemporaryDirectory() as box:
            limit = Path(box) / "memory.max"
            limit.write_text("2147483648\n", encoding="utf-8")
            real = Path.read_text

            def reading(self: Path, *args: object, **kwargs: object) -> str:
                if str(self) == "/sys/fs/cgroup/memory.max":
                    return real(limit)
                return real(self, *args, **kwargs)  # type: ignore[arg-type]

            with mock.patch.object(Path, "read_text", reading):
                self.assertEqual(mutate._visible_memory(), 2 << 30)

    def test_no_cgroup_file_falls_back_to_the_host(self) -> None:
        def missing(self: Path, *args: object, **kwargs: object) -> str:
            raise OSError("no cgroup here")

        with mock.patch.object(Path, "read_text", missing):
            self.assertGreater(mutate._visible_memory(), 0)


class TestTheShareOneLaneGets(unittest.TestCase):
    """Lanes and ceiling are one decision, because the machine feels the product.

    #232: sixteen lanes at a 4 GiB ceiling is 64 GiB of promises on a 63 GiB
    machine, and the kernel collected on them three times. Each half was
    defensible alone -- which is why what is asserted here is the product, and
    why the class above it, asserting the lane count on its own, was green
    throughout.
    """

    def share(self, visible: int, wanted: int = 16, memory: int = MEMORY) -> mutate.Share:
        with mock.patch.object(mutate, "_visible_memory", return_value=visible):
            return mutate._share(wanted, memory)

    def test_the_product_stays_inside_the_budget(self) -> None:
        # From a small CI runner to a large server. Every size below has room
        # for at least one lane at the floor, so the budget is a real bound
        # here; the machine that has not is the test after this one.
        for gib in (8, 16, 32, 63, 128, 512):
            with self.subTest(gib=gib):
                lanes, memory = self.share(gib << 30)
                self.assertLessEqual(lanes * memory, (gib << 30) // 2)

    def test_a_machine_too_small_for_one_honest_lane_still_gets_one(self) -> None:
        """And gets the floor, over budget, on purpose.

        The alternative is a ceiling under what an honest suite needs, which
        does not fail as "too small to run": it fails as `ran out of memory`
        against whichever test was running, which reads as a finding.
        """
        lanes, memory = self.share(2 << 30)
        self.assertEqual(lanes, 1)
        self.assertEqual(memory, mutate._FLOOR)

    def test_the_ceiling_gives_way_before_the_lanes_do(self) -> None:
        """A big machine keeps its parallelism and stops over-promising.

        Sixteen lanes is what this box ran before #232 and what it runs after;
        the difference is the ceiling each was told it could reach.
        """
        lanes, memory = self.share(63 << 30)
        self.assertGreaterEqual(lanes, 8)
        self.assertLess(memory, MEMORY)

    def test_a_laptop_is_not_cut_to_the_pathological_case(self) -> None:
        """#227's complaint, which the first fix for this would have brought back.

        Dividing the budget by `MEMORY` gave a 16 GiB machine two lanes, to
        bound a case the ceiling already truncates within seconds.
        """
        lanes, _ = self.share(16 << 30)
        self.assertGreater(lanes, 2)

    def test_the_ceiling_never_falls_under_the_floor(self) -> None:
        for gib in (2, 8, 16, 63, 512):
            with self.subTest(gib=gib):
                self.assertGreaterEqual(self.share(gib << 30).memory, mutate._FLOOR)

    def test_no_cap_is_left_uncapped(self) -> None:
        """`--memory 0` is a promise the flag makes; there is no product to bound."""
        self.assertEqual(self.share(63 << 30, memory=0).memory, 0)

    def test_an_explicit_ceiling_under_the_floor_is_the_callers_own(self) -> None:
        """Reproducing a small machine on purpose is a real reason to ask."""
        lanes, memory = self.share(63 << 30, memory=256 << 20)
        self.assertEqual(memory, 256 << 20)
        self.assertLessEqual(lanes * memory, (63 << 30) // 2)

    def test_asking_for_more_lanes_does_not_conjure_memory(self) -> None:
        lanes, memory = self.share(8 << 30, wanted=64)
        self.assertLessEqual(lanes * memory, (8 << 30) // 2)

    def test_a_pinned_lane_count_is_kept_and_the_ceiling_gives_way(self) -> None:
        """`--workers` is a count a caller has reasons to fix, and this cannot
        see them. It is also what the class above uses to assert that mutations
        overlap at all -- lowering it there would have turned a test of the pool
        into a test of the machine, which is the failure its own comment names.

        A pinned run is not unbounded: `_Lanes` measures what the lanes hold
        rather than predicting it, and that is the guard a wrong prediction
        leaves standing.
        """
        with mock.patch.object(mutate, "_visible_memory", return_value=2 << 30):
            pinned = mutate._share(4, MEMORY, pinned=True)
            asked = mutate._share(4, MEMORY)
        self.assertEqual(pinned, mutate.Share(4, mutate._FLOOR))
        self.assertEqual(asked.lanes, 1, "unpinned, the machine decides")


class TestAHarnessRunningInsideALane(unittest.TestCase):
    """`tests/test_mutate.py` starts this module, so a lane mutating `tools/`
    hosts a second harness -- which sized itself for the whole host, sixteen
    lanes deep, until the budget was passed down."""

    def test_an_inherited_budget_is_a_limit_like_the_cgroup(self) -> None:
        with mock.patch.dict(os.environ, {mutate._BUDGET: str(3 << 30)}):
            self.assertEqual(mutate._visible_memory(), 3 << 30)

    def test_it_can_only_lower(self) -> None:
        """A lane cannot be given more memory than the machine has by saying so."""
        with mock.patch.dict(os.environ, {mutate._BUDGET: str(1 << 60)}):
            self.assertLess(mutate._visible_memory(), 1 << 60)

    def test_nonsense_is_ignored_rather_than_raised(self) -> None:
        for said in ("", "lots", "-1", "0"):
            with self.subTest(said=said), mock.patch.dict(os.environ, {mutate._BUDGET: said}):
                self.assertGreater(mutate._visible_memory(), 0)


class TestALaneCarriesItsShareIntoTheProbe(MutateTestCase):
    """The wire between the two halves: `_share` decides, and the probe has to
    be *told*, or an inner harness reads the host's memory and sizes itself for
    a machine it does not have.

    The variable is spelled out here rather than taken from `mutate._BUDGET`
    on purpose. It is a name two processes agree on, so a test that reads it
    from the same constant the code does would pass through any rename --
    including one that changed only the half that writes it.
    """

    def test_the_probe_is_told_what_it_may_spend(self) -> None:
        share = 1 << 30
        self.write(
            "test_budget.py",
            f"""
            import os
            import unittest


            class T(unittest.TestCase):
                def test_the_lane_said_so(self) -> None:
                    self.assertEqual(os.environ.get("WOSWOAR_MUTATE_BUDGET"), "{share}")
            """,
        )
        verdict = mutate._run(["test_budget"], self.root, memory=share)
        self.assertEqual(verdict.outcome, "survived", verdict.detail)


class TestCountingWhatALaneHolds(unittest.TestCase):
    """Both readers, on every platform, because one of them is macOS's only guard."""

    def test_ps_sees_this_process_group(self) -> None:
        self.assertGreater(mutate._from_ps().get(os.getpgrp(), 0), 1 << 20)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "no /proc on this platform")
    def test_proc_sees_this_process_group(self) -> None:
        self.assertGreater(mutate._from_proc().get(os.getpgrp(), 0), 1 << 20)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "no /proc on this platform")
    def test_the_two_readers_agree_about_this_group(self) -> None:
        """Not to the byte -- `ps` and `/proc` count shared pages differently and
        the suite allocates between the two calls -- but within a factor, which
        is what a ceiling comparison actually needs."""
        ours = os.getpgrp()
        from_proc, from_ps = mutate._from_proc()[ours], mutate._from_ps()[ours]
        self.assertLess(max(from_proc, from_ps) / min(from_proc, from_ps), 4)


def reap(pid: int) -> None:
    """Kill `pid` if it is still there.

    For cleanups: a test whose complaint is that a process survived must not
    leave that process behind when it makes it.
    """
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def outliving(pids: Sequence[int], seconds: float) -> list[int]:
    """Which of `pids` are still there after up to `seconds`.

    Polled rather than slept, and the polling is the point: a `killpg` and the
    kernel reaping what it killed are not the same instant, so a fixed sleep
    guesses at a race in both directions -- too short and it reports survivors
    that are already gone, too long and every green run pays for it.
    """
    deadline = time.monotonic() + seconds
    while True:
        left = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:  # pragma: no cover - the id belongs elsewhere now
                pass
            left.append(pid)
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(0.1)


class TestNoCapMeansNoWatcher(MutateTestCase):
    """`--memory 0` promises no cap, and a ceiling of zero is the opposite of one.

    Registered rather than skipped, a lane at zero is over its share the moment
    it has a page, so every row of a `--memory 0` run would be killed and
    reported unanswered -- the flag doing precisely what it says it does not.
    """

    def test_a_lane_with_no_ceiling_is_not_killed(self) -> None:
        self.write("mod.py", CLAMP)
        self.write(
            "test_mod.py",
            """
            import time
            import unittest

            import mod


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    # Longer than one sample, or a lane that finishes before the
                    # watcher has looked once proves nothing about being watched.
                    time.sleep(2)
                    self.assertEqual(mod.clamp(-5), 0)
            """,
        )
        verdict = mutate._run(["test_mod"], self.root, memory=0, timeout=60.0)
        self.assertEqual(verdict.outcome, "survived", verdict.detail)


class TestItSaysWhatItGaveTheLanes(MutateTestCase):
    """A share lowered in silence reads as a slow tool rather than a bounded one,
    and it is what makes a later `ran out of memory` row inexplicable. Same
    argument as `--limit` printing what it dropped."""

    def running(self, visible: int, workers: int | None = None) -> str:
        self.package(guarded=True)
        said = io.StringIO()
        with (
            mock.patch.object(mutate, "_visible_memory", return_value=visible),
            contextlib.redirect_stdout(said),
        ):
            run(
                [Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")],
                baseline=False,
                workers=workers,
            )
        return said.getvalue()

    def test_a_machine_that_cannot_afford_what_was_asked_is_told_so(self) -> None:
        said = self.running(2 << 30)
        self.assertIn("lane(s) at", said)
        self.assertIn("_share", said)

    def test_a_machine_with_room_is_not_lectured(self) -> None:
        self.assertNotIn("lane(s) at", self.running(512 << 30))

    def test_a_pinned_run_hears_about_its_ceiling(self) -> None:
        """The half that only a pinned run reaches: the lane count is exactly
        what was asked for and the *ceiling* is what gave way, so a condition
        that wants both to have changed says nothing at all."""
        self.assertIn("lane(s) at", self.running(2 << 30, workers=1))


class TestTheSamplerAndTheLanesItHasLeft(unittest.TestCase):
    """What a mutation table found unguarded: `_Lanes`' own bookkeeping.

    The storm test above drives one lane from beginning to end, and a sweep of
    this file showed what that cannot see -- a `release` that forgets to
    unregister, or that stops the sampler while other lanes are still running,
    leaves every assertion in that test passing on a harness that has silently
    stopped guarding anything after the first row.
    """

    def holding(self, bytes_held: int) -> subprocess.Popen[bytes]:
        """A process in a session of its own, holding `bytes_held`, for 30s.

        Its own session because that is what a lane is: `_Lanes` counts by
        process group, so a fixture sharing this one's group would be asking a
        different question -- and would put this suite's own pid in the table.
        """
        held = subprocess.Popen(
            [sys.executable, "-c", f"import time; held = bytearray({bytes_held}); time.sleep(30)"],
            start_new_session=True,
        )
        self.addCleanup(reap, held.pid)
        self.addCleanup(mutate._WATCHED.release, held.pid)
        return held

    def test_releasing_one_lane_leaves_the_others_watched(self) -> None:
        over = self.holding(64 << 20)
        under = self.holding(1 << 20)
        mutate._WATCHED.watch(under.pid, 512 << 20)
        mutate._WATCHED.watch(over.pid, 8 << 20)
        mutate._WATCHED.release(under.pid)
        self.assertKilled(over, "the sampler stopped when the first lane was released")
        self.assertGreater(mutate._WATCHED.release(over.pid), 8 << 20)

    def test_the_sampler_comes_back_after_the_last_lane_goes(self) -> None:
        """It is stopped when nothing is left to watch, so the next lane has to
        start it again -- and a run is many lanes, one after another."""
        first = self.holding(1 << 20)
        mutate._WATCHED.watch(first.pid, 512 << 20)
        mutate._WATCHED.release(first.pid)

        later = self.holding(64 << 20)
        mutate._WATCHED.watch(later.pid, 8 << 20)
        self.assertKilled(later, "no lane was watched after the sampler had stopped")
        self.assertGreater(mutate._WATCHED.release(later.pid), 8 << 20)

    def assertKilled(self, held: subprocess.Popen[bytes], why: str) -> None:
        """Waited on rather than polled with `os.kill(pid, 0)`.

        These children have this process as their parent, so a killed one is a
        zombie until it is reaped -- and a zombie answers signal 0 exactly like
        a running process. `outliving` is right for the tests above it, whose
        children are orphaned by the kill and reaped by init; here it would
        report a killed child as alive for ever.
        """
        try:
            held.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail(why)
        self.assertEqual(held.returncode, -signal.SIGKILL, why)


class TestReadingPsOutputThatIsNotWhatItExpects(unittest.TestCase):
    """`ps` is the reader macOS depends on, and the only one with a parser.

    Every line real `ps` prints here is two numbers, so nothing in the suite
    distinguishes the check from no check at all -- a sweep of this file
    reported five surviving mutants on that one line. Junk is what it is for:
    a header, a truncated line, a `?` where a number was expected.
    """

    def reading(self, said: str) -> dict[int, int]:
        listed = subprocess.CompletedProcess([""], 0, stdout=said, stderr="")
        with mock.patch.object(subprocess, "run", return_value=listed):
            return mutate._from_ps()

    def test_only_lines_of_two_numbers_are_counted(self) -> None:
        self.assertEqual(
            self.reading(
                "  PGID   RSS\n"  # a header, if the `=` suffixes ever stop working
                "   501  1024\n"
                "     ?    12\n"  # a field `ps` could not answer
                "   502\n"  # truncated
                "   503  4096 stray\n"  # more than was asked for
                "\n"
                "   501  2048\n"  # a second process in a group already seen
            ),
            {501: 3072 * 1024},
        )

    def test_kibibytes_are_not_bytes(self) -> None:
        """The unit `ps` answers in, which is the difference between killing a
        lane at its share and killing it at a thousandth of it."""
        self.assertEqual(self.reading("  700  2048\n"), {700: 2 << 20})


class TestALaneThatSpawnsProcesses(MutateTestCase):
    """The failure a per-process ceiling cannot see, at the scale that killed it.

    Sixteen children holding 64 MiB each is a gibibyte between them, and every
    one of them is far inside the ceiling the lane is given -- exactly the
    kernel log of #232's last crash, where 4,340 processes held 26 GB and none
    was within two orders of magnitude of its own limit. So `verdict.cap` cannot
    fire here by construction: if the lane is not killed, nothing is guarding.
    """

    def storm(self, children: int, held: int, sleeps: int) -> Path:
        """A lane spawning `children`, each holding `held` bytes for `sleeps`
        seconds. Returns the file they list themselves in, because whether they
        *died* is a question the verdict alone cannot answer."""
        pids = self.root / "pids.txt"
        self.write(
            "test_storm.py",
            f"""
            import subprocess
            import sys
            import unittest
            from pathlib import Path

            PIDS = Path({str(pids)!r})


            class T(unittest.TestCase):
                def test_it_spawns(self) -> None:
                    # `sleeps` is what makes the guard the only thing that can
                    # end this early, and it is load-bearing. At twenty seconds
                    # the children outlived the sample and exited on their own,
                    # so the row came back `broke` from the bookkeeping alone:
                    # deleting the `killpg` survived a table with every
                    # assertion below still passing.
                    kids = [
                        subprocess.Popen(
                            [sys.executable, "-c",
                             "import time; held = bytearray({held}); time.sleep({sleeps})"]
                        )
                        for _ in range({children})
                    ]
                    PIDS.write_text("\\n".join(str(kid.pid) for kid in kids), encoding="utf-8")
                    for kid in kids:
                        kid.wait()
            """,
        )
        return pids

    def test_the_lane_is_killed_whole_and_says_why(self) -> None:
        pids = self.storm(children=16, held=64 << 20, sleeps=120)
        # The timeout is the fail-safe here, not the mechanism: a working guard
        # ends this in about a second, and a broken one reports `timeout` rather
        # than holding a gibibyte for two minutes.
        verdict = mutate._run(["test_storm"], self.root, memory=512 << 20, timeout=20.0)
        self.assertEqual(verdict.outcome, "broke", verdict.detail)
        self.assertIn("held", verdict.detail)
        self.assertIn("share", verdict.detail)

        # And they are *gone*, which the verdict above cannot tell you. Deleting
        # the `killpg` and keeping the bookkeeping leaves every assertion before
        # this one passing on a run that still takes the machine -- a mutation
        # table said exactly that, and this block is what it bought.
        children = [int(said) for said in pids.read_text(encoding="utf-8").split()]
        for child in children:
            self.addCleanup(reap, child)
        self.assertEqual(outliving(children, 5.0), [], "children outlived the killed lane")

    def test_a_lane_that_stays_inside_its_share_is_left_alone(self) -> None:
        """The other half, and the one that fails if the ceiling is read as a
        threshold to kill at rather than a ceiling to stay under."""
        self.storm(children=2, held=8 << 20, sleeps=1)
        verdict = mutate._run(["test_storm"], self.root, memory=512 << 20, timeout=90.0)
        self.assertEqual(verdict.outcome, "survived", verdict.detail)


class TestATimeoutEndsTheWholeSession(MutateTestCase):
    """A killed lane must not leave its children behind.

    `subprocess.run(timeout=...)` kills the process it holds a handle to, and
    the suite forks `git`, `age`, `bash` and `python -m woswoar`. Those are
    reparented to init, hold what they held, and are invisible to the run that
    started them -- the slow half of #232, where a sweep with enough timeouts
    accumulates them until the machine is gone.
    """

    def test_a_child_does_not_outlive_the_lane(self) -> None:
        ledger = self.root / "child.pid"
        self.write(
            "test_hang.py",
            f"""
            import subprocess
            import sys
            import time
            import unittest
            from pathlib import Path


            class T(unittest.TestCase):
                def test_it_hangs(self) -> None:
                    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
                    Path({str(ledger)!r}).write_text(str(kid.pid), encoding="utf-8")
                    time.sleep(120)
            """,
        )
        verdict = mutate._run(["test_hang"], self.root, timeout=5.0)
        self.assertEqual(verdict.outcome, "timeout", verdict.detail)
        orphan = int(ledger.read_text(encoding="utf-8"))
        # Whatever this test concludes, it leaves no sleeping process behind --
        # including when it fails, which is the case where there is one.
        self.addCleanup(reap, orphan)
        self.assertEqual(
            outliving([orphan], 10.0), [], "the child outlived the lane that started it"
        )


def enforced() -> bool:
    """Whether this kernel applies the limits `verdict.cap` asks for.

    Asked by trying it, in a subprocess, rather than by reading `sys.platform`.
    macOS ignores `RLIMIT_AS` -- which CI discovered, not the documentation --
    and a `skipIf(darwin)` would encode today's answer to a question that is
    really "does this kernel enforce it", going green on a platform that
    silently protects nothing and staying skipped if macOS ever starts.

    It sets the limits *directly* rather than calling `verdict.cap`, and that is
    not a stylistic choice -- mutation testing rejected the first version, which
    did call it. A skip condition computed from the code under test cannot
    detect that code being reverted: neutering `cap` made the probe report "not
    enforced", the test skipped, and the row came back `SURVIVED` from a suite
    that had simply declined to run it. A guard that switches itself off when
    the thing it guards breaks is worse than no guard, because it is green.
    """
    probe = (
        "import resource\n"
        "for which in (resource.RLIMIT_AS, resource.RLIMIT_DATA):\n"
        "    soft, hard = resource.getrlimit(which)\n"
        "    try:\n"
        "        resource.setrlimit(which, (256 * 1024 * 1024, hard))\n"
        "    except (OSError, ValueError):\n"
        "        pass\n"
        "try:\n"
        "    bytearray(768 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('enforced')\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    return "enforced" in done.stdout


@unittest.skipUnless(enforced(), "this kernel does not enforce the address-space limit")
class TestAMutantThatEatsMemory(MutateTestCase):
    """The failure `timeout` cannot catch, because the machine goes first.

    A generated mutation does not have to loop forever to hold a lane -- it can
    loop forever *while appending*. `mutants.line_starts` reduced to `at -= ...`
    never advances, because a negative index wraps in Python instead of raising,
    and three lanes reached 15.5 GB, 7.6 GB and 6.4 GB seventy-three seconds in.
    The 300 s timeout never got to speak: the host OOM-killed the session twice.

    The fixture allocates a *bounded* 800 MiB rather than looping, and that is
    deliberate. An unbounded fixture would reproduce the crash in the suite that
    is supposed to prevent it, and -- worse for rule 3 -- it could not be run
    with the fix reverted, so nobody could check that this test fails without it.
    Bounded, both states are safe and they differ:

    - with the cap, the allocation raises `MemoryError` and the row is `broke`;
    - with `cap` reverted, 800 MiB is simply allocated, `clamp(-5)` returns
      838860800 rather than 0, the assertion fails, and the row reads `caught`;
    - with `cap` kept but `_exhausted` reverted, the `MemoryError` arrives at
      `addError` inside a real `TestCase` and reads `caught` as well.

    Both reverts produce the same wrong answer, and it is the dangerous one: a
    test credited with a guard it does not have.
    """

    def test_it_is_broke_rather_than_a_test_noticing(self) -> None:
        self.package(guarded=True)
        report = run(
            [
                Mutation(
                    "clamp allocates 800 MiB",
                    "mod.py",
                    "        return 0",
                    "        return len(bytearray(800 * 1024 * 1024))",
                    "test_mod",
                ),
                Mutation("the guard goes", "mod.py", "if value < 0:", "if False:", "test_mod"),
            ],
            baseline=False,
            strict=False,
            # Comfortably above what the two-file fixture suite needs and
            # comfortably below the 800 MiB above, so neither side is a
            # near-miss that a different Python could tip over.
            memory=512 << 20,
        )
        self.assertEqual([result.verdict.outcome for result in report.results], ["broke", "caught"])
        self.assertIn("ran out of memory", report.results[0].verdict.detail)


@unittest.skipUnless(enforced(), "this kernel does not enforce the address-space limit")
class TestASubTestThatRunsOutOfMemory(MutateTestCase):
    """The intersection of the two cases above, which neither of them covers.

    `TestASubTestIsARealAnswer` drives an assertion inside `with
    self.subTest(...)`; `TestAMutantThatEatsMemory` drives a `MemoryError` in a
    plain test. `Verdicts.addSubTest` has a branch for both at once, and the
    whole-package sweep of `tools/` found it unreached -- the only real gap that
    sweep turned up, and it is in the code that decides what `caught` means.

    Reverted, a memory-exhausted subtest is filed as `noticed` and the row reads
    `caught`: a test credited with a guard it does not have, which is the exact
    lie #220 removed one level down. This repository uses `subTest` in more than
    twenty places, so the intersection is reachable rather than theoretical.

    Gated on `enforced()` for the same reason as its sibling, which CI taught
    twice: macOS does not apply `RLIMIT_AS`, so the 800 MiB simply succeeds
    there, `clamp` returns a large number, the subtest notices it and the row
    reads `caught`. The first version of this test copied the fixture and not
    the gate.
    """

    def subtesting_package(self) -> None:
        """`CLAMP`, and a test whose every assertion is inside a `subTest`."""
        self.write("mod.py", CLAMP)
        self.write(
            "test_mod.py",
            """
            import unittest

            import mod


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    for value in (-5, -1):
                        with self.subTest(value=value):
                            self.assertEqual(mod.clamp(value), 0)
            """,
        )

    def test_it_is_broke_rather_than_the_subtest_noticing(self) -> None:
        self.subtesting_package()
        report = run(
            [
                Mutation(
                    "clamp allocates 800 MiB inside a subTest",
                    "mod.py",
                    "        return 0",
                    "        return len(bytearray(800 * 1024 * 1024))",
                    "test_mod",
                ),
                # The sibling row proves the fixture's subTest route really does
                # report a catch, so "broke" above is not simply a broken fixture.
                Mutation("the guard goes", "mod.py", "if value < 0:", "if False:", "test_mod"),
            ],
            baseline=False,
            strict=False,
            memory=512 << 20,
        )
        self.assertEqual([result.verdict.outcome for result in report.results], ["broke", "caught"])
        self.assertIn("ran out of memory", report.results[0].verdict.detail)


class TestAMutantThatNeverFinishes(MutateTestCase):
    """A generated mutant can turn a loop bound into one that never fires.

    With no limit, that holds a lane for the rest of the table and the printout
    stops at its row, because results are reported in table order. The answer is
    an outcome, not an exception: the other rows still have something to say.
    """

    def test_it_times_out_instead_of_holding_the_lane(self) -> None:
        self.package(guarded=True)
        report = run(
            [
                # The *guarded* branch, because that is the one the fixture's test
                # actually reaches: a loop behind `return 0` is never entered by
                # `clamp(-5)`, and the row came back `survived` in milliseconds.
                Mutation(
                    "clamp never returns",
                    "mod.py",
                    "        return 0",
                    "        while True:\n            pass",
                    "test_mod",
                ),
                Mutation("the guard goes", "mod.py", "if value < 0:", "if False:", "test_mod"),
            ],
            baseline=False,
            strict=False,
            # Long enough that a healthy row is never mistaken for a hang -- the
            # sibling row below runs in well under a second -- and short enough
            # that this test is not the slowest in the file.
            timeout=5.0,
        )
        self.assertEqual(
            [result.verdict.outcome for result in report.results], ["timeout", "caught"]
        )


class TestFailfastStopsAtTheFirstTestThatNoticed(MutateTestCase):
    """Worth having because a caught mutant is the common case.

    Not asserted on wall clock, which would be the same claim with a flake
    attached: each test records that it ran, and the count says how far the run
    got. Note this is an average, not a bound -- `unittest` runs classes in
    alphabetical order, so a mutant caught only by the last of them still pays
    for nearly all of the module.
    """

    def counting(self) -> Path:
        ledger = self.root / "ledger.txt"
        self.write("mod.py", CLAMP)
        self.write(
            "test_mod.py",
            f"""
            import unittest
            from pathlib import Path

            import mod

            LEDGER = Path({str(ledger)!r})


            class T(unittest.TestCase):
                def record(self) -> None:
                    with LEDGER.open("a", encoding="utf-8") as log:
                        log.write("ran\\n")

                def test_a(self) -> None:
                    self.record()
                    self.assertEqual(mod.clamp(-5), 0)

                def test_b(self) -> None:
                    self.record()

                def test_c(self) -> None:
                    self.record()
            """,
        )
        return ledger

    def ran_under(self, failfast: bool) -> int:
        ledger = self.counting()
        run(
            [Mutation("the guard goes", "mod.py", "if value < 0:", "if False:", "test_mod")],
            baseline=False,
            failfast=failfast,
        )
        return len(ledger.read_text(encoding="utf-8").split())

    def test_it_stops_after_the_failure(self) -> None:
        self.assertEqual(self.ran_under(failfast=True), 1)

    def test_without_it_the_whole_class_runs(self) -> None:
        """The other half, and the one that makes the number above mean something:
        a ledger showing 1 proves nothing if 1 is all the class ever writes."""
        self.assertEqual(self.ran_under(failfast=False), 3)


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
        finished = self.drive("spec.py")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("caught", finished.stdout)

    def drive(self, name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "tools.mutate", str(self.root / name)],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
        )

    def test_a_script_that_calls_verify_itself_is_not_a_failure(self) -> None:
        """#213: it ran the table, printed `caught`, and then exited 1.

        The message said the file defined nothing, and because stderr is not
        buffered against stdout it landed *above* the results it was contradicting.
        Met while verifying #201, where the fix was to rewrite the spec into the
        shape the docstring did not show.
        """
        self.package(guarded=True)
        self.write(
            "spec.py",
            """
            from tools.mutate import Mutation, verify

            verify(
                [Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")]
            )
            """,
        )
        finished = self.drive("spec.py")
        self.assertIn("caught", finished.stdout)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertNotIn("defines no MUTATIONS", finished.stderr)

    def test_two_tables_are_both_the_verdict(self) -> None:
        """The half of #213 its own test could not see.

        `main` reduces a spec's exit status over *every* table the script ran,
        and the guard for that is `all(...)` rather than the last report. A spec
        with one table cannot tell the two apart, and one table is what the test
        above it writes -- so the row `all` becomes `any` came back SURVIVED
        from the first full sweep of `tools/`, on the line whose comment says
        reverting it is #213's own symptom reintroduced.

        Survivors first and clean second, because that is the order that exits
        zero when the reduction is wrong. Reversed, both readings agree.
        """
        self.package(guarded=False)  # a test that cannot see its mutation
        self.write(
            "test_seen.py",
            """
            import unittest

            import mod


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    self.assertEqual(mod.clamp(-5), 0)
            """,
        )
        self.write(
            "spec.py",
            """
            from tools.mutate import Mutation, verify

            gone = "if value < 0:"
            verify([Mutation("nothing sees this", "mod.py", gone, "if False:", "test_mod")])
            verify([Mutation("but this is caught", "mod.py", gone, "if False:", "test_seen")])
            """,
        )
        finished = self.drive("spec.py")
        self.assertIn("SURVIVED", finished.stdout)
        self.assertIn("caught", finished.stdout)
        self.assertEqual(
            finished.returncode, 1, f"the clean second table decided it: {finished.stdout}"
        )

    def test_a_script_that_does_both_runs_the_table_once(self) -> None:
        """The quieter half of #213: minutes of wall clock, silently doubled."""
        self.package(guarded=True)
        self.write(
            "spec.py",
            """
            from tools.mutate import Mutation, verify

            MUTATIONS = [
                Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")
            ]
            verify(MUTATIONS)
            """,
        )
        finished = self.drive("spec.py")
        self.assertEqual(finished.stdout.count("the clamp is gone"), 1, finished.stdout)
        self.assertIn("Delete one of the two", finished.stderr)

    def test_a_survivor_exits_nonzero(self) -> None:
        self.package(guarded=False)
        self.write(
            "spec.py",
            """
            from tools.mutate import Mutation

            MUTATIONS = [
                Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")
            ]
            """,
        )
        finished = self.drive("spec.py")
        self.assertIn("SURVIVED", finished.stdout)
        self.assertEqual(finished.returncode, 1)

    def test_a_row_that_asked_nothing_is_not_clean(self) -> None:
        """A `BROKE` row is not a pass. Calling it clean would report the table as
        complete while it was quietly that much smaller than it looks."""
        mutation = Mutation("x", "mod.py", "a", "b", "test_mod")
        caught = Result(mutation, Verdict("caught", "test_mod.T.test_it"))
        self.assertTrue(Report([caught]).clean)
        self.assertFalse(Report([Result(mutation, Verdict("broke", "why"))]).clean)
        self.assertFalse(Report([Result(mutation, Verdict("timeout", "why"))]).clean)
        self.assertFalse(Report([Result(mutation, Verdict("survived"))]).clean)
        self.assertFalse(Report([caught], baseline_red=True).clean)

    def test_a_script_that_does_neither_still_says_so(self) -> None:
        self.package(guarded=True)
        self.write("spec.py", "x = 1\n")
        finished = self.drive("spec.py")
        self.assertNotEqual(finished.returncode, 0)
        self.assertIn("never called verify()", finished.stderr)


if __name__ == "__main__":
    unittest.main()
