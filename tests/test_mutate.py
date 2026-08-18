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
