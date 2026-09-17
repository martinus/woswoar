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
from tools import verdict as verdict_module
from tools.mutate import (
    MEMORY,
    Mutation,
    Report,
    Result,
    Verdict,
    confirm,
    confirm_timeout,
    run,
    verify,
)

from . import support
from .support import requires_git

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

    def red_for_its_own_reasons(self, at: str) -> None:
        """A failing test the mutation has nothing to do with.

        Deliberately about nothing -- it does not import the mutated module, let
        alone call it -- because what the callers need is that *being red is
        enough*. One that touched the module under mutation would leave a reader
        unable to tell a false correction from a true one.

        The path is the caller's, and under a git fixture it matters: it must be
        untracked and outside `woswoar/`, or `changed_lines` treats it as wholly
        changed and puts it in the diff.

        Here rather than in the two suites that want it, for the reason `CLAMP`
        gives above: written out separately, two copies stay self-consistent
        while drifting into being red for different reasons, and both suites go
        on looking green.
        """
        self.write(
            at,
            """
            import unittest


            class Broken(unittest.TestCase):
                def test_something_else_entirely(self) -> None:
                    self.fail("red for its own reasons")
            """,
        )

    def package(self, guarded: bool, inside: str = "", both: bool = False) -> None:
        """A module with one behaviour, and a test that may or may not see it.

        `inside` puts it in a package, which is what a *generated* table needs:
        `mutants.mutable` only generates for paths under `woswoar/` or `tools/`,
        so a module at the sandbox root is invisible to `--base`.

        `both` asserts the guarded branch *and* the unguarded one, which a
        generated `branch` table needs -- its two mutants are "the guard always
        fires" and "it never does", and catching them takes an assertion each.
        It is a parameter rather than the default because a stronger fixture
        changes what the rest of this file observes: turned on for everyone,
        `TestAnUnanswerableRowNeedNotEndTheRun` stops having a surviving row to
        report, which is most of what that test is for.
        """
        self.write(f"{inside}/mod.py" if inside else "mod.py", CLAMP)
        sees = "self.assertEqual(mod.clamp(-5), 0)" if guarded else ""
        seen = [
            line
            for line in (sees, "self.assertEqual(mod.clamp(5), 5)" if both or not guarded else "")
            if line
        ]
        self.write(
            f"{inside}/../tests/test_mod.py" if inside else "test_mod.py",
            f"""
            import unittest

            {f"from {inside} import mod" if inside else "import mod"}


            class T(unittest.TestCase):
                def test_it(self) -> None:
                    {"; ".join(seen)}
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


class TwoRowsSharingALabel(MutateTestCase):
    """The fixture both confirmation suites below need, held in one place.

    No tests of its own. It is a class rather than a function because it wants
    `write`, and the two suites derive from it rather than copying it because a
    fixture this fiddly -- two rows that must *both* survive the narrow pass,
    one of which the wider suite catches -- drifts the moment there are two of
    it, and a drifted copy is how a confirmation test stops confirming anything.
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


class TestConfirmingSurvivorsAgainstTheWholeSuite(TwoRowsSharingALabel):
    """The pass that makes narrow test selection a speed decision, not a wrong one.

    A row run against too few tests survives for the wrong reason, and that error
    points the expensive way: it sends the author to rewrite a test that was
    never weak. So every survivor is re-run against everything before it is
    printed.
    """

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


class TestConfirmingNeedsAGreenSuiteToo(TwoRowsSharingALabel):
    """#268: the one pass that had no baseline, and what it cost.

    `confirm` re-runs each survivor against everything with `failfast`, so the
    first red test settles the row. On a suite that is already failing for its
    own reasons that test is always reached, and every survivor comes back
    "caught by a test the selection had not run" -- naming a test that has never
    heard of the file under mutation. A false clean sweep, in the report a pull
    request quotes as evidence.

    Found for real: two genuine survivors of a `tools/watch.py` sweep were both
    credited to a shell-hook test, on a container where three tests are red for
    environmental reasons.

    The other half -- that a *green* suite still corrects, without which all of
    this passes on a `confirm` that has stopped correcting anything at all -- is
    `TestConfirmingSurvivorsAgainstTheWholeSuite`'s own test, on this same
    fixture, one class up. It is not repeated here: it asserts strictly more,
    and a second copy is another whole-suite run for an answer already in the
    same file and the same run.
    """

    def with_an_unrelated_failure(self) -> Report:
        """The narrow pass over two survivors, in a tree whose wider suite is red.

        Built on `two_rows_with_one_label`'s fixture because it already has what
        this needs and is hard to get: two rows that *survive* the narrow pass,
        so there is something for `confirm` to widen, one of which the wide
        suite legitimately catches and one of which it does not.

        The failure is deliberately about nothing -- it does not import `mod`,
        let alone call it -- because the point is that being red is enough. A
        broken test that touched the mutated module would leave the reader
        unable to tell a false correction from a true one.
        """
        rows = self.two_rows_with_one_label()
        self.red_for_its_own_reasons("test_broken.py")
        report = run(rows, baseline=False, strict=False)
        self.assertEqual(
            [result.verdict.outcome for result in report.results],
            ["survived", "survived"],
            "precondition: both rows must survive, or nothing is widened",
        )
        return report

    def test_a_red_suite_corrects_nothing(self) -> None:
        """Both rows stand as the narrow pass reported them.

        Including the first, which the wide suite really does catch. Suppressing
        a correction that happens to be true is the price of not publishing the
        one that is false: with the suite red there is no way to tell them apart
        from in here, and a report that is right by luck is the thing this
        module refuses to produce.
        """
        report = self.with_an_unrelated_failure()
        with contextlib.redirect_stdout(io.StringIO()):
            confirmed = confirm(report, workers=None, timeout=60.0, memory=MEMORY)
        self.assertEqual(
            [result.verdict.outcome for result in confirmed.results],
            ["survived", "survived"],
            "a red suite was allowed to correct a survivor",
        )

    def test_it_says_the_suite_is_why(self) -> None:
        """A silently unwidened survivor is a different bug report.

        The reader is deciding whether to go and strengthen a test. "This row
        survived" and "this row survived and nothing could widen it" send them
        to different places, and only one of them is true here.
        """
        report = self.with_an_unrelated_failure()
        with contextlib.redirect_stdout(io.StringIO()) as said:
            confirm(report, workers=None, timeout=60.0, memory=MEMORY)
        spoken = said.getvalue()
        self.assertIn("BASELINE NOT GREEN", spoken)
        # The two halves, each from the function that owns it. `run` must scope
        # its warning -- unscoped it reads as voiding the narrow pass too --
        # and `confirm` must say what it decided. Not "stand as reported" on its
        # own: the unanswerable-row line further down says that too, so a
        # mutation removing this print would have been caught by the wrong one.
        self.assertIn("nothing among the confirmation rows", spoken)
        self.assertIn("No survivor was corrected", spoken)
        self.assertNotIn("caught by a test the selection had not run", spoken)

    def test_the_report_says_the_survivors_were_never_widened(self) -> None:
        """The stdout line is gone the moment the terminal scrolls; the flag is
        what `--json` writes and `tools/reached.py` will read.

        `widened` is false until `confirm` earns it, so this asserts the earning
        did not happen -- and the sibling below asserts it does on a green tree,
        without which a flag hard-coded to false would pass here.
        """
        report = self.with_an_unrelated_failure()
        with contextlib.redirect_stdout(io.StringIO()):
            confirmed = confirm(report, workers=None, timeout=60.0, memory=MEMORY)
        self.assertFalse(confirmed.widened)

    def test_a_completed_pass_says_so(self) -> None:
        report = run(self.two_rows_with_one_label(), baseline=False, strict=False)
        with contextlib.redirect_stdout(io.StringIO()):
            confirmed = confirm(report, workers=None, timeout=60.0, memory=MEMORY)
        self.assertTrue(confirmed.widened)

    def test_an_already_red_report_runs_nothing(self) -> None:
        """The narrow pass's shard is a module inside the whole suite, so a red
        one guarantees a red confirmation -- after paying a whole-suite run per
        survivor to find out. Nothing should run, and the pass should not
        announce itself either."""
        report = self.with_an_unrelated_failure()._replace(baseline_red=True)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            confirmed = confirm(report, workers=None, timeout=60.0, memory=MEMORY)
        self.assertEqual(said.getvalue(), "")
        self.assertFalse(confirmed.widened)
        self.assertEqual(
            [result.verdict.outcome for result in confirmed.results],
            ["survived", "survived"],
        )

    def test_a_clean_report_is_neither_announced_nor_re_run(self) -> None:
        """No survivor, so nothing to widen -- and nothing to say about it.

        The promise holds vacuously, which is why `widened` is true here; a
        clean sweep marked "not confirmed" would be the one report this tool
        exists to produce, carrying a warning about work it correctly skipped.
        Silence is asserted too, because falling through announces "confirming
        0 survivor(s) against the whole suite" about a pass that runs nothing.
        """
        self.package(guarded=True)
        report = run(
            [Mutation("the clamp is gone", "mod.py", "if value < 0:", "if False:", "test_mod")],
            baseline=False,
        )
        self.assertEqual([result.verdict.outcome for result in report.results], ["caught"])
        with contextlib.redirect_stdout(io.StringIO()) as said:
            confirmed = confirm(report, workers=None, timeout=60.0, memory=MEMORY)
        self.assertEqual(said.getvalue(), "")
        self.assertTrue(confirmed.widened)

    def test_a_survivor_the_whole_suite_cannot_see_either_is_still_widened(self) -> None:
        """The healthy-repo case, and the one the other tests skip past.

        A real survivor on a green tree: the pass runs, corrects nothing, and
        must still report that it happened -- otherwise `widened` is false
        exactly when the news is "your test is genuinely weak", which is when a
        reader most needs to trust the row.

        Only the second row, because the first is corrected by the wide suite
        and a non-empty `corrected` takes a different return.
        """
        report = run(self.two_rows_with_one_label(), baseline=False, strict=False)
        genuine = Report(report.results[1:], report.baseline_red)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            confirmed = confirm(genuine, workers=None, timeout=60.0, memory=MEMORY)
        self.assertEqual([result.verdict.outcome for result in confirmed.results], ["survived"])
        self.assertTrue(confirmed.widened)
        # What is *not* said, because both lines below are conditional and a
        # mutation making either unconditional survived every other test here.
        # They would read "0 confirmation(s) could not be answered ... the report
        # is not marked widened" beside a report that is, and "0 of them were
        # caught" after correcting none -- false sentences, in the pass whose
        # whole job is to stop a reader trusting a wrong row.
        self.assertNotIn("could not be answered", said.getvalue())
        self.assertNotIn("were caught by a test the selection had not run", said.getvalue())
        # The merged timings survive the pass. `Killers` budgets the cheap-first
        # prefix from them, so dropping them here costs every later run its
        # ordering -- silently, since nothing else reads the field.
        self.assertTrue(confirmed.times, "confirmation discarded what the run measured")

    def hangs_only_where_the_narrow_pass_cannot_look(self) -> Mutation:
        """One row that survives the narrow pass fast and hangs the wide one.

        The shape #322 reports, reduced to something that fits a test: the
        confirmation probe runs the whole suite, exceeds its bound and comes back
        `TIMEOUT` -- which is neither a survivor nor a catch. The hang is in a
        function the narrow selection never calls, so the narrow row is answered
        in milliseconds and only the widened re-run pays for it.

        A real hang rather than a stubbed clock: what is under test is what the
        bound does to a probe that will not finish, and a stand-in for the probe
        would be a test of the stand-in.
        """
        self.write("mod.py", "def first():\n    return 1\n\n\ndef second():\n    return 2\n")
        self.write(
            "test_narrow.py",
            """
            import unittest

            import mod


            class Narrow(unittest.TestCase):
                def test_first_only(self) -> None:
                    self.assertEqual(mod.first(), 1)
            """,
        )
        # Found by discovery alone, so only the confirmation pass reaches it.
        self.write(
            "test_wide.py",
            """
            import unittest

            import mod


            class Wide(unittest.TestCase):
                def test_second(self) -> None:
                    self.assertEqual(mod.second(), 2)
            """,
        )
        return Mutation(
            "second hangs",
            "mod.py",
            "    return 2",
            '    __import__("time").sleep(600)',
            "test_narrow",
        )

    def test_a_confirmation_that_timed_out_does_not_earn_widened(self) -> None:
        """#322. `widened` is a promise about what ran, not about the pass being
        attempted, and CONTRIBUTING.md states it: true "only when every survivor
        in the file really was re-run against the whole suite".

        A probe that timed out re-ran nothing, and this set the flag anyway --
        so a sweep where every confirmation timed out wrote `"widened": true`
        into its `--json`, which `tools/reached.py` then reads back as a survivor
        that had been checked against everything. The quiet direction: the run
        looks like it answered fewer rows, not like it stopped checking.
        """
        report = run([self.hangs_only_where_the_narrow_pass_cannot_look()], baseline=False)
        self.assertEqual([result.verdict.outcome for result in report.results], ["survived"])
        with contextlib.redirect_stdout(io.StringIO()) as said:
            confirmed = confirm(report, workers=None, timeout=8.0, memory=MEMORY)
        self.assertEqual(
            [result.verdict.outcome for result in confirmed.results],
            ["survived"],
            "precondition: the narrow verdict must stand, unanswered by the widened run",
        )
        self.assertFalse(confirmed.widened, "an unanswered confirmation claimed the promise")
        # And says so, rather than leaving the reader to notice a TIMEOUT row.
        self.assertIn("could not be answered", said.getvalue())
        self.assertIn("not marked widened", said.getvalue())

    def test_a_correction_beside_a_timeout_still_does_not_earn_widened(self) -> None:
        """The mixed run, and the reason it needs its own test.

        `confirm` returns from two places -- one when nothing was corrected, one
        when something was -- and the second is reached only when a survivor is
        promoted. A fixture where everything times out exercises the first and
        leaves the second free to grant the promise, which is what a mutation of
        it proved: `kept` restored to `True` there survived the test above.

        Here one row is caught by the wide suite and the other hangs it, so
        `corrected` is non-empty and `unsure` is one at the same time. The
        promise covers *every* survivor, so one unanswered probe voids it
        however many of its neighbours were answered.
        """
        self.write("mod.py", "def first():\n    return 1\n\n\ndef second():\n    return 2\n")
        self.write(
            "test_narrow.py",
            """
            import unittest

            import mod


            class Narrow(unittest.TestCase):
                def test_neither_is_called(self) -> None:
                    self.assertTrue(hasattr(mod, "first"))
            """,
        )
        # Calls both, so it catches the first row and hangs on the second --
        # each probe mutates only its own row, so neither interferes.
        self.write(
            "test_wide.py",
            """
            import unittest

            import mod


            class Wide(unittest.TestCase):
                def test_both(self) -> None:
                    self.assertEqual(mod.first(), 1)
                    self.assertEqual(mod.second(), 2)
            """,
        )
        rows = [
            Mutation("first returns 99", "mod.py", "    return 1", "    return 99", "test_narrow"),
            Mutation(
                "second hangs",
                "mod.py",
                "    return 2",
                '    __import__("time").sleep(600)',
                "test_narrow",
            ),
        ]
        report = run(rows, baseline=False, strict=False)
        self.assertEqual(
            [result.verdict.outcome for result in report.results],
            ["survived", "survived"],
            "precondition: both rows must survive the narrow pass",
        )
        with contextlib.redirect_stdout(io.StringIO()) as said:
            confirmed = confirm(report, workers=None, timeout=8.0, memory=MEMORY)
        self.assertEqual(
            [result.verdict.outcome for result in confirmed.results],
            ["caught", "survived"],
            "precondition: one row corrected, one left unanswered",
        )
        self.assertIn("were caught by a test the selection had not run", said.getvalue())
        self.assertFalse(
            confirmed.widened,
            "a correction earned the promise for a survivor that was never re-run",
        )

    def test_the_derived_bound_is_the_one_the_probe_actually_gets(self) -> None:
        """The plumbing, and it is the line the rest of #322 rests on.

        Arithmetic tests cover `confirm_timeout`; a mutation restoring
        `timeout=timeout` in the `run` call survived every one of them, because
        a derived number nothing arms is dead code that reads as a fix.

        So this is timed rather than asserted on an argument: the mutant sleeps
        four seconds, the floor is two, and the remembered costs derive twelve.
        Under the floor the probe times out and the row stands as a survivor;
        under the derived bound it finishes, the wide test sees `None` instead of
        2, and the row is corrected. The two answers are opposite, which is the
        only way to tell which bound was armed.
        """
        self.write("mod.py", "def first():\n    return 1\n\n\ndef second():\n    return 2\n")
        self.write(
            "test_narrow.py",
            """
            import unittest

            import mod


            class Narrow(unittest.TestCase):
                def test_first_only(self) -> None:
                    self.assertEqual(mod.first(), 1)
            """,
        )
        self.write(
            "test_wide.py",
            """
            import unittest

            import mod


            class Wide(unittest.TestCase):
                def test_second(self) -> None:
                    self.assertEqual(mod.second(), 2)
            """,
        )
        row = Mutation(
            "second dawdles",
            "mod.py",
            "    return 2",
            '    __import__("time").sleep(4)',
            "test_narrow",
        )
        report = run([row], baseline=False)
        self.assertEqual([result.verdict.outcome for result in report.results], ["survived"])
        with contextlib.redirect_stdout(io.StringIO()) as said:
            confirmed = confirm(
                report,
                workers=None,
                timeout=2.0,
                memory=MEMORY,
                # Sums to 4.0, so `CONFIRM_SLACK` derives 12.0 -- above the
                # sleep, where the floor of 2.0 is below it.
                costs={"a": 2.0, "b": 2.0},
            )
        self.assertIn("12s each", said.getvalue(), "the printed bound is not the derived one")
        self.assertEqual(
            [result.verdict.outcome for result in confirmed.results],
            ["caught"],
            "the probe was bounded by --timeout, so the derivation reaches nothing",
        )
        self.assertTrue(confirmed.widened)

    def test_no_baseline_still_means_no_baseline(self) -> None:
        """The escape hatch keeps working, and the reader gets to see its price.

        `--no-baseline` exists for a tree the author knows is red and wants
        mutation answers about anyway. If the check it turns off ran here
        regardless, the flag would silently stop correcting anything on exactly
        the tree it was reached for -- a worse failure than the one #268 fixed,
        because at least that one printed a reason.

        So with it off this is the old behaviour, false correction and all: the
        second row is a real survivor and the broken test "catches" it.
        """
        report = self.with_an_unrelated_failure()
        with contextlib.redirect_stdout(io.StringIO()):
            confirmed = confirm(report, workers=None, timeout=60.0, memory=MEMORY, baseline=False)
        self.assertEqual(
            [result.verdict.outcome for result in confirmed.results],
            ["caught", "caught"],
            "--no-baseline no longer reaches the confirmation pass",
        )


class TestTheConfirmationBoundIsDerived(unittest.TestCase):
    """#322: a whole-suite probe cannot share the per-mutation bound.

    `--timeout` bounds one mutation against a module or two. A confirmation
    probe is one serial pass over everything, and when the suite grew past the
    bound every probe came back `TIMEOUT` -- 249.7 s of work against 300 s, with
    lanes competing for the same cores. Nobody moved the number; the suite moved
    under it.

    So the bound is derived from what `Killers` remembers each test costing,
    which is refilled on every run. These are the arithmetic, not the plumbing:
    `confirm` passes `killers.cost` and the value reaches `run`'s `timeout`.
    """

    def test_no_measurement_leaves_the_floor_alone(self) -> None:
        """A fresh machine, `--no-killers`, or a file that would not parse. The
        floor is today's behaviour, and a guess dressed up as a derivation would
        be worse -- it would read as measured in the output and be a number
        nobody chose."""
        self.assertEqual(confirm_timeout(300.0, None), 300.0)
        self.assertEqual(confirm_timeout(300.0, {}), 300.0)

    def test_a_measured_suite_raises_the_bound_above_the_floor(self) -> None:
        """The reported case: a ~250 s serial suite under a 300 s bound. The
        derived bound has to clear it with room for lane contention.

        Every cost here is exactly representable, and that is not fussiness.
        The first version used 2.497 and compared against `3.0 * 249.7`, which
        passed on 3.12 and 3.14 and failed on 3.10 with
        `749.100000000002 != 749.0999999999999`: CPython 3.12 gave `sum()`
        compensated summation for floats, so the naive accumulation on 3.10
        lands a few ulps away. Halves sum exactly under either algorithm, so the
        test is about the arithmetic it claims to be about.
        """
        costs = {f"test_{i}": 2.5 for i in range(100)}  # 250.0s, exactly
        self.assertGreater(confirm_timeout(300.0, costs), 300.0)
        self.assertEqual(confirm_timeout(300.0, costs), 750.0)

    def test_the_floor_wins_when_it_is_the_larger(self) -> None:
        """`--timeout` is a floor, not an opinion to be overruled. Someone who
        raises it means "give things longer", and a derived bound that came back
        *shorter* would answer the opposite of what they asked."""
        self.assertEqual(confirm_timeout(9000.0, {"test_one": 1.0}), 9000.0)

    def test_the_bound_follows_the_suite_as_it_grows(self) -> None:
        """The whole point of deriving it. A second constant beside `TIMEOUT`
        would rot exactly as the first one did, and silently -- the failure is a
        row that says `TIMEOUT`, which is neither a survivor nor a catch."""
        small = confirm_timeout(1.0, {"a": 10.0})
        grown = confirm_timeout(1.0, {"a": 10.0, "b": 10.0})
        self.assertEqual(grown, 2 * small)


def spying_on_confirm() -> tuple[Any, list[dict[str, Any]]]:
    """A stand-in for `confirm` that records the keywords it was handed.

    Module level because the two entry points live in two classes, each with the
    fixture it needs -- a spec file here, a git repository further down -- and a
    second copy of this is how one of them would come to check something subtly
    other than the other.
    """
    seen: list[dict[str, Any]] = []

    def recording(report: Report, *args: Any, **kw: Any) -> Report:
        seen.append(kw)
        return report

    return recording, seen


class TestTheRememberedCostsReachConfirm(MutateTestCase):
    """The wiring between `Killers` and `confirm_timeout`, on the spec path.

    `confirm` derives its bound from remembered per-test costs, and every caller
    has to hand them over. Nothing else can see this: a mutation deleting
    `costs=killers.cost` from a call site passed `confirm_timeout`'s arithmetic
    tests and the end-to-end one alike, because the derivation then falls back to
    the floor -- which is the behaviour #322 reported, restored without a word.

    `TestTheGeneratedEntryPoint` carries the same check for the other caller.
    """

    def test_the_spec_path_hands_over_what_it_remembers(self) -> None:
        self.write("mod.py", "def f():\n    return 1\n")
        self.write(
            "test_mod.py",
            """
            import unittest

            import mod


            class T(unittest.TestCase):
                def test_f(self) -> None:
                    self.assertEqual(mod.f(), 1)
            """,
        )
        self.write(
            "spec.py",
            """
            from tools.mutate import Mutation

            MUTATIONS = [Mutation("f returns 2", "mod.py", "return 1", "return 2", "test_mod")]
            """,
        )
        recording, seen = spying_on_confirm()
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(mutate, "confirm", recording),
        ):
            mutate.main(["spec.py", "--no-baseline", "--no-killers"])
        self.assertEqual(len(seen), 1, "confirm was not reached")
        self.assertIn("costs", seen[0], "the spec path derives its bound from nothing")


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


class TestWhatASandboxDoesNotCopy(unittest.TestCase):
    """tupferl#32: the sandbox copy must not race a directory something else is
    writing.

    `_sandboxes` copies the working tree per lane. `.hypothesis` is created and
    removed *by Hypothesis while the suite runs*, `tools/run_tests.py` shards
    across twice the usable cores, and this module starts the harness inside one
    of those shards -- so `copytree` scanned `.hypothesis/tmp` and it was gone
    before the copy. It reached CI on tupferl's PR #31, on a diff that touched
    no file in `tools/`, and the traceback named a test that was fine.

    Driven against a real `_sandboxes` rather than by reading `_SKIP`: the
    constant is the mechanism, and a test that asserted its *contents* would
    pass against a `copytree` that had stopped passing it.
    """

    #: Every name `_SKIP` exists to keep out, and one that must survive.
    #: `.hypothesis` and `sweeps` are the two this adds; the rest were already
    #: there and are here so that dropping one is a failure rather than a
    #: silence.
    KEPT_OUT = (".git", "__pycache__", ".mypy_cache", ".ruff_cache", ".hypothesis", "sweeps")
    KEPT = "woswoar"

    def sandbox(self, tree: Path) -> Path:
        """One lane's copy of `tree`, through the real `_sandboxes`."""
        with (
            mock.patch.object(Path, "cwd", return_value=tree),
            mutate._sandboxes(1) as available,
        ):
            borrowed = available.get()
            # Copied out while it is still borrowed: the context manager removes
            # the whole thing on the way out.
            return Path(shutil.copytree(Path(str(borrowed)), tree.parent / "seen"))

    def test_none_of_them_reaches_a_lane(self) -> None:
        with tempfile.TemporaryDirectory(prefix="woswoar-skip-") as box:
            tree = Path(box) / "tree"
            (tree / self.KEPT).mkdir(parents=True)
            (tree / self.KEPT / "__init__.py").write_text("", encoding="utf-8")
            for name in self.KEPT_OUT:
                (tree / name).mkdir()
                (tree / name / "inside").write_text("x", encoding="utf-8")

            copy = self.sandbox(tree)
            for name in self.KEPT_OUT:
                with self.subTest(name=name):
                    self.assertFalse((copy / name).exists(), f"{name} was copied")
            # One file that must survive, or "nothing was copied at all" would
            # satisfy every assertion above.
            self.assertTrue((copy / self.KEPT / "__init__.py").is_file(), "the tree was not copied")

    def test_a_nested_one_is_kept_out_too(self) -> None:
        """`shutil.ignore_patterns` matches the base name at any depth. A pattern
        that only applied at the root would leave this copied, and nothing would
        notice until the next red leg."""
        with tempfile.TemporaryDirectory(prefix="woswoar-skip-") as box:
            tree = Path(box) / "tree"
            deep = tree / self.KEPT / "somewhere" / ".hypothesis"
            deep.mkdir(parents=True)
            (deep / "tmp").write_text("x", encoding="utf-8")

            copy = self.sandbox(tree)
            self.assertTrue((copy / self.KEPT / "somewhere").is_dir(), "the tree was not copied")
            self.assertFalse((copy / self.KEPT / "somewhere" / ".hypothesis").exists())


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
        """Reset, not ignored: someone who says `--limit 50` means it.

        The count as well as the notice, because a cap that says it dropped
        rows and then runs a different number of them is the same wrong answer
        told twice.
        """
        listed = self.listed("--all", "--limit", "50")
        self.assertIn("--limit 50:", listed)
        self.assertEqual(len([line for line in listed.splitlines() if line.startswith("  ")]), 50)

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
            each_test=mutate.EACH_TEST,
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
            each_test=mutate.EACH_TEST,
            memory=mutate.MEMORY,
            all=True,
            batch=True,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            report = mutate.sweep([], args)
        self.assertFalse(report.clean, "a recorded survivor still counts against the run")

    def batch_args(self, where: Path) -> argparse.Namespace:
        return argparse.Namespace(
            json=where,
            no_baseline=True,
            workers=1,
            timeout=30.0,
            each_test=mutate.EACH_TEST,
            memory=mutate.MEMORY,
            all=True,
            batch=True,
        )

    def swept(self, table: list[Mutation], answer: Report) -> tuple[str, mock.Mock]:
        """One sweep with `run` stubbed, returning what was printed and the stub.

        Stubbed for the reason the resume test gives: `sweep`'s job is the
        bookkeeping around `run`, and running a real mutation would test `run`,
        which has its own tests.
        """
        box = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, box, True)
        args = self.batch_args(Path(box) / "out.json")
        with (
            contextlib.redirect_stdout(io.StringIO()) as said,
            mock.patch.object(mutate, "run", return_value=answer) as ran,
        ):
            mutate.sweep(table, args)
        return said.getvalue(), ran

    def test_one_pool_means_one_run_for_the_whole_table(self) -> None:
        """The per-file batches are gone -- tupferl#7's measurement, ported with
        the design: a pool per file serialised its hung rows, ~600s of a 913s
        run with the machine two thirds empty. What replaced them is one `run`
        over every remaining row, so `scope` is gone too: there is one baseline
        again and #271's per-batch wording has nothing left to mislead about."""
        table = [
            Mutation("a.py:1 -- one", "a.py", "a", "b", "test_mod"),
            Mutation("b.py:1 -- two", "b.py", "c", "d", "test_mod"),
        ]
        answer = Report([Result(r, Verdict("caught")) for r in table])
        _, ran = self.swept(table, answer)
        self.assertEqual(1, ran.call_count, "the sweep still runs a pool per file")
        self.assertNotIn("scope", ran.call_args.kwargs)
        self.assertEqual(len(ran.call_args.args[0]), 2, "not every row reached the one run")

    def test_a_red_baseline_voids_the_whole_run_and_says_so(self) -> None:
        """One pool has one baseline, so the count #271 asked for collapses to
        a sentence about everything -- which must still be printed, or a
        `caught` row above it could be believed. That happened, per file, under
        the batched design."""
        table = [Mutation("a.py:1 -- one", "a.py", "a", "b", "test_mod")]
        answer = Report([Result(table[0], Verdict("caught"))], baseline_red=True)
        said, _ = self.swept(table, answer)
        counted = next(line for line in said.splitlines() if "baseline was red" in line)
        self.assertIn("none of the 1 new row(s) means anything", counted)

    def test_a_red_baseline_leaves_no_new_rows_for_a_resume_to_trust(self) -> None:
        """The rows of a red-baseline run are void, and `_recorded` drops the
        red flag while `sweep` skips a recorded file by name -- so a void row
        that reaches `--json` comes back on the next run as a final answer.
        The old rows stay: they were recorded under their own green baselines.
        """
        where = self.persisted(self.rows("a.py:1 -- old"))
        table = [
            Mutation("a.py:1 -- old", "a.py", "a", "b", "test_mod"),
            Mutation("b.py:1 -- fresh", "b.py", "c", "d", "test_mod"),
        ]
        answer = Report([Result(table[1], Verdict("caught"))], baseline_red=True)
        args = self.batch_args(where)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(mutate, "run", return_value=answer),
        ):
            report = mutate.sweep(table, args)
        self.assertTrue(report.baseline_red, "the flag was dropped on the way out")
        on_disk = json.loads(where.read_text(encoding="utf-8"))
        labels = [row["label"] for row in on_disk["results"]]
        self.assertNotIn("b.py:1 -- fresh", labels, "a void row was recorded as final")
        self.assertEqual(["a.py:1 -- old"], labels, "the resume state lost the good rows")

    def test_a_green_sweep_says_nothing_about_it(self) -> None:
        """Without this the sentence could be printed unconditionally."""
        table = [Mutation("a.py:1 -- one", "a.py", "a", "b", "test_mod")]
        said, _ = self.swept(table, Report([Result(table[0], Verdict("caught"))]))
        self.assertNotIn("baseline was red", said)

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


def pin_the_machine(case: unittest.TestCase) -> None:
    """Make the budget arithmetic below depend only on what each test patches.

    Two ambient facts otherwise decide it, and both are legitimate settings
    rather than accidents:

    - **who owns the machine.** On a CI runner `dedicated()` answers "this
      run", and every `// 2` below is then asserting a rule that did not apply.
    - **`WOSWOAR_MUTATE_TOTAL`.** `_budget` reads it *before* `_visible_memory`,
      so an operator who exported it -- or a sweep launched with it, which is
      exactly what the variable is for -- makes these tests read the ambient
      number instead of the one they patched in.

    The second was measured the expensive way: a 394-mutant sweep launched with
    `WOSWOAR_MUTATE_TOTAL` set went red on its baseline at
    `test_a_small_machine_still_gets_a_lane` (`AssertionError: 14 != 1`), which
    voided all 329 rows it had answered. The variable reaches every probe
    through the environment `_run` hands down, so the tests guarding the budget
    code were exactly the tests a legitimate use of that code broke.

    `TestWhoOwnsTheMachine` owns both of these questions and clears the whole
    environment itself, so it does not use this.
    """
    pinned = mock.patch.object(mutate, "dedicated", lambda: "")
    pinned.start()
    case.addCleanup(pinned.stop)
    # `clear=False` with the one name removed: the probe needs the rest of the
    # environment (PATH, HOME, PYTHONPATH) to run at all.
    without = mock.patch.dict(os.environ, {}, clear=False)
    without.start()
    case.addCleanup(without.stop)
    os.environ.pop(mutate._TOTAL, None)


class TestHowManyLanesFitInMemory(unittest.TestCase):
    """The bound that stops the per-row cap from being multiplied by the lanes.

    Untested when it was written, and two of the three things asserted here were
    wrong at that point: the budget was divided by the 4 GiB ceiling rather than
    by what a lane uses, and the host's memory was read without asking whether a
    container had said otherwise.
    """

    def setUp(self) -> None:
        pin_the_machine(self)

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

    def setUp(self) -> None:
        pin_the_machine(self)

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

    def test_the_probe_does_not_inherit_the_operators_machine_total(self) -> None:
        """`WOSWOAR_MUTATE_TOTAL` is a knob for the machine, and a lane's answer
        is its share of it.

        `_budget` short-circuits `_visible_memory` when the variable is set, so
        a probe that inherited it would size itself for the **outer** total and
        ignore the per-lane `WOSWOAR_MUTATE_BUDGET` beside it -- which
        `_budget`'s own docstring already called wrong, without the code
        preventing it.

        Found by a sweep rather than by reading. Launched with the variable
        exported, every probe answered `_budget()` with the whole machine, the
        two suites that patch `_visible_memory` to assert the arithmetic went
        red, and the baseline they broke voided all 394 rows -- twice, because
        the first fix was to the tests rather than to this line.

        Driven through a real probe, and it asserts *both* names: a fix that
        dropped the whole environment would pass a test that only looked for
        the absence of one.
        """
        share = 1 << 30
        self.write(
            "test_budget.py",
            f"""
            import os
            import unittest


            class T(unittest.TestCase):
                def test_the_lane_got_its_share_and_not_the_machine(self) -> None:
                    self.assertIsNone(os.environ.get("WOSWOAR_MUTATE_TOTAL"))
                    self.assertEqual(os.environ.get("WOSWOAR_MUTATE_BUDGET"), "{share}")
            """,
        )
        with mock.patch.dict(os.environ, {mutate._TOTAL: str(64 << 30)}):
            verdict = mutate._run(["test_budget"], self.root, memory=share)
        self.assertEqual(verdict.outcome, "survived", verdict.detail)


class TestCountingWhatALaneHolds(unittest.TestCase):
    """Both readers, on every platform, because one of them is macOS's only guard."""

    def held(self, table: dict[int, mutate.Process]) -> int:
        ours = os.getpgrp()
        return sum(row.resident for row in table.values() if row.group == ours)

    def test_ps_sees_this_process_group(self) -> None:
        self.assertGreater(self.held(mutate._from_ps()), 1 << 20)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "no /proc on this platform")
    def test_proc_sees_this_process_group(self) -> None:
        self.assertGreater(self.held(mutate._from_proc()), 1 << 20)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "no /proc on this platform")
    def test_the_two_readers_agree_about_this_group(self) -> None:
        """Not to the byte -- `ps` and `/proc` count shared pages differently and
        the suite allocates between the two calls -- but within a factor, which
        is what a ceiling comparison actually needs."""
        from_proc, from_ps = self.held(mutate._from_proc()), self.held(mutate._from_ps())
        self.assertLess(max(from_proc, from_ps) / min(from_proc, from_ps), 4)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "no /proc on this platform")
    def test_the_two_readers_agree_about_who_is_whose_parent(self) -> None:
        """`_lane` walks parents, so the field `ps` reports has to mean what the
        one `/proc` reports means -- and only one of the two is ever read on the
        machine this is developed on."""
        self.assertEqual(mutate._from_ps()[os.getpid()].parent, os.getppid())
        self.assertEqual(mutate._from_proc()[os.getpid()].parent, os.getppid())


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

    def setUp(self) -> None:
        # Same reason as `TestHowManyLanesFitInMemory`'s: this patches
        # `_visible_memory` and asserts what the machine could afford, which an
        # ambient `WOSWOAR_MUTATE_TOTAL` decides instead. `_run` no longer hands
        # that variable to a probe, so a sweep cannot break this any more; a
        # developer with it exported still can, which is what this is for.
        super().setUp()
        pin_the_machine(self)

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

    Every line real `ps` prints here is four numbers, so nothing in the suite
    distinguishes the check from no check at all -- a sweep of this file
    reported five surviving mutants on that one line. Junk is what it is for:
    a header, a truncated line, a `?` where a number was expected.
    """

    def reading(self, said: str) -> dict[int, mutate.Process]:
        listed = subprocess.CompletedProcess([""], 0, stdout=said, stderr="")
        with mock.patch.object(subprocess, "run", return_value=listed):
            return mutate._from_ps()

    def test_only_lines_of_four_numbers_are_counted(self) -> None:
        self.assertEqual(
            self.reading(
                "  PID  PPID  PGID   RSS\n"  # a header, if the `=` suffixes stop working
                "  501   500   501  1024\n"
                "  502   501     ?    12\n"  # a field `ps` could not answer
                "  503   501\n"  # truncated
                "  504   501   501  4096 stray\n"  # more than was asked for
                "\n"
                "  505   501   501  2048\n"
            ),
            {
                501: mutate.Process(parent=500, group=501, resident=1024 * 1024),
                505: mutate.Process(parent=501, group=501, resident=2048 * 1024),
            },
        )

    def test_kibibytes_are_not_bytes(self) -> None:
        """The unit `ps` answers in, which is the difference between killing a
        lane at its share and killing it at a thousandth of it."""
        self.assertEqual(
            self.reading("  700   699   700  2048\n"),
            {700: mutate.Process(parent=699, group=700, resident=2 << 20)},
        )


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


class TestALaneThatLeavesItsOwnGroup(MutateTestCase):
    """#234: a process group is not the whole lane.

    `start_new_session` is what makes a lane's group cover the `git`, `age` and
    `bash` a suite forks -- and a *nested* `_run` gives its own probes the same
    treatment, which takes them out of the outer lane's group. Those were
    counted by nobody and reached by no `killpg`: one was found alive eleven
    minutes into a sweep whose per-row bound is 300 seconds, reparented to
    init, with no process left that knew it was meant to stop.

    Both halves are asserted here, because they fail apart. Counting an escapee
    is what makes the lane go over its share at all; reaching it is what the
    kill has to do afterwards.
    """

    def escaping(self, holds: int, sleeps: int, children: int = 1) -> Path:
        """A lane whose children put themselves in sessions of their own."""
        pids = self.root / "pids.txt"
        self.write(
            "test_escape.py",
            f"""
            import subprocess
            import sys
            import unittest
            from pathlib import Path

            PIDS = Path({str(pids)!r})


            class T(unittest.TestCase):
                def test_it_escapes(self) -> None:
                    # `start_new_session`, exactly as `mutate._run` does it for
                    # its own probes -- which is what a lane mutating `tools/`
                    # ends up hosting, and why this is not a contrived shape.
                    kids = [
                        subprocess.Popen(
                            [
                                sys.executable,
                                "-c",
                                "import time; held = bytearray({holds}); time.sleep({sleeps})",
                            ],
                            start_new_session=True,
                        )
                        for _ in range({children})
                    ]
                    PIDS.write_text("\\n".join(str(kid.pid) for kid in kids), encoding="utf-8")
                    for kid in kids:
                        kid.wait()
            """,
        )
        return pids

    def escapees(self, pids: Path) -> list[int]:
        found = [int(said) for said in pids.read_text(encoding="utf-8").split()]
        for pid in found:
            self.addCleanup(reap, pid)
        return found

    def test_what_an_escapee_holds_is_still_the_lanes_problem(self) -> None:
        # Three at 200 MiB is 600 MiB against a 512 MiB share, and every one of
        # them is outside the group -- so counting the group alone leaves the
        # lane looking idle while the machine is not.
        pids = self.escaping(holds=200 << 20, sleeps=120, children=3)
        verdict = mutate._run(["test_escape"], self.root, memory=512 << 20, timeout=25.0)
        self.assertEqual(verdict.outcome, "broke", verdict.detail)
        self.assertIn("held", verdict.detail)
        self.assertEqual(outliving(self.escapees(pids), 5.0), [], "an escapee outlived the kill")

    def orphaning(self, holds: int, children: int) -> Path:
        """A lane whose grandchildren outlive the child that started them.

        The mirror of `escaping`, and the reason a lane is the *union* of two
        answers: these stay in the group and stop being anyone's descendant, so
        a walk down the tree misses exactly the ones `killpg` catches, and a
        group sum misses exactly the ones the walk catches. A suite that
        backgrounds a job in a shell that then exits produces this shape without
        trying.

        The launcher writes the pids to a file rather than to a pipe, and that
        is not incidental: the processes it starts inherit its stdout, so a
        `communicate()` on that pipe waits for *them* and the fixture hangs for
        as long as the holders sleep. Same trap `_run` documents for its own
        stderr, met again one level down.
        """
        pids = self.root / "pids.txt"
        holder = f"import time; held = bytearray({holds}); time.sleep(120)"
        launcher = (
            "import subprocess, sys\n"
            f"kids = [subprocess.Popen([sys.executable, '-c', {holder!r}],\n"
            "        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"        for _ in range({children})]\n"
            f"open({str(pids)!r}, 'w').write('\\n'.join(str(kid.pid) for kid in kids))\n"
        )
        self.write(
            "test_orphan.py",
            f"""
            import subprocess
            import sys
            import time
            import unittest


            class T(unittest.TestCase):
                def test_it_orphans(self) -> None:
                    subprocess.run([sys.executable, "-c", {launcher!r}], check=True, timeout=30)
                    # The launcher has exited, so what it started belongs to
                    # init now -- and still to this lane's process group.
                    time.sleep(60)
            """,
        )
        return pids

    def test_an_orphan_that_stayed_in_the_group_is_still_the_lanes(self) -> None:
        pids = self.orphaning(holds=200 << 20, children=3)
        verdict = mutate._run(["test_orphan"], self.root, memory=512 << 20, timeout=25.0)
        self.assertEqual(verdict.outcome, "broke", verdict.detail)
        self.assertEqual(outliving(self.escapees(pids), 5.0), [], "an orphan outlived the kill")

    def test_a_timeout_reaches_them_too(self) -> None:
        """The other route in, and the one that produced the eleven-minute
        orphan: nothing here is over its share, the row simply does not finish."""
        pids = self.escaping(holds=1 << 20, sleeps=120)
        verdict = mutate._run(["test_escape"], self.root, timeout=5.0)
        self.assertEqual(verdict.outcome, "timeout", verdict.detail)
        self.assertEqual(outliving(self.escapees(pids), 10.0), [], "an escapee outlived the lane")


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


@unittest.skipUnless(
    support.memory_caps_apply(), "this kernel does not enforce the address-space limit"
)
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


@unittest.skipUnless(
    support.memory_caps_apply(), "this kernel does not enforce the address-space limit"
)
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

    Gated on `support.memory_caps_apply` for the same reason as its sibling, which CI taught
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


def cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """`python -m tools.mutate` against `root`, as someone would type it.

    `PYTHONPATH` is the repository, so the harness under test is this tree's
    while the code it mutates is the sandbox's -- the same split `_run` makes
    for the probe, and the reason a fixture may shadow `woswoar` only if it
    provides the whole package (`tools.sandbox` imports `store`).
    """
    return subprocess.run(
        [sys.executable, "-m", "tools.mutate", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
    )


@requires_git
class TestTheGeneratedEntryPoint(MutateTestCase):
    """`--base`, `--json`, `--no-confirm` and the exit status (#235).

    Everything in `TestTheScriptEntryPoint` drives a *spec file*, which is the
    other branch of `main` -- so the branch a maintainer actually types was
    executed by no test at all. The first full sweep of `tools/` found 24
    surviving mutants in it, including `--json` never written and the exit
    status inverted. `--all`'s reset of the diff-sized `--limit` is not here:
    `TestAllDropsTheDiffSizedCap` already drives it through `main`, and `--all`
    consults no git at all, so a repository fixture would be scaffolding this
    class needs and that question does not.

    The fixture is a real git repository, because `_SKIP` keeps `.git` out of a
    sandbox and a diff needs one. It also holds a *copy* of `woswoar/`: a
    mutable path has to start with `woswoar/` or `tools/`, and a fixture package
    that shadows either without providing it breaks the harness's own imports.
    """

    def repo(self, guarded: bool) -> None:
        shutil.copytree(
            REPO_ROOT / "woswoar",
            self.root / "woswoar",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.write("tests/__init__.py", "")
        self.package(guarded, inside="woswoar", both=True)
        moved = self.root / "woswoar" / "mod.py"
        source = moved.read_text(encoding="utf-8")
        moved.unlink()
        support.git(self.root, "init", "--quiet", "--template=")
        support.git(self.root, "add", ".")
        support.git(self.root, "commit", "--quiet", "-m", "base")
        # The module lands *after* that commit, and only then: `changed_lines`
        # treats every untracked mutable file as wholly changed, so a `woswoar/`
        # left out of it puts the whole package in the diff. It did, on the
        # first run of this fixture -- 200 rows against `sync.py` for a test
        # about `mod.py`.
        moved.write_text(source, encoding="utf-8")

    def test_a_diff_generates_rows_and_a_clean_run_exits_zero(self) -> None:
        self.repo(guarded=True)
        finished = cli(self.root, "--base", "HEAD", "--operator", "branch")
        self.assertIn("caught", finished.stdout)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)

    def test_a_survivor_makes_the_exit_status_one(self) -> None:
        """The half a reader believes without checking: a run that found
        something and said so is not a run that succeeded."""
        self.repo(guarded=False)
        finished = cli(self.root, "--base", "HEAD", "--operator", "branch")
        self.assertIn("SURVIVED", finished.stdout)
        self.assertEqual(finished.returncode, 1, finished.stdout + finished.stderr)

    def test_json_holds_every_row_that_was_printed(self) -> None:
        self.repo(guarded=True)
        report = self.root / "rows.json"
        finished = cli(self.root, "--base", "HEAD", "--operator", "branch", "--json", str(report))
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        written = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(written["baseline_red"])
        # The claim the file makes about itself, and the only place it survives
        # the terminal: every survivor here was re-run against the whole suite.
        self.assertTrue(written["widened"])
        # As many rows as were printed, and about the file the diff touched.
        # What each row must *hold* is `TestTheJsonReport`'s question, asked
        # once there rather than again here.
        self.assertEqual(
            len(written["results"]), finished.stdout.count("  caught  "), finished.stdout
        )
        self.assertEqual({row["path"] for row in written["results"]}, {"woswoar/mod.py"})

    def test_a_finished_run_takes_its_pidfile_away(self) -> None:
        """The run writes its own pid because the caller could not.

        Removed at the end, because by then it names a process that is gone --
        and a stale pid read as live is the exact false answer `tools/watch.py`
        exists to refuse.
        """
        self.repo(guarded=True)
        report = self.root / "rows.json"
        cli(self.root, "--base", "HEAD", "--operator", "branch", "--json", str(report))
        self.assertFalse(mutate._pidfile(report).exists())

    def test_the_generated_path_hands_over_what_it_remembers(self) -> None:
        """The other half of `TestTheRememberedCostsReachConfirm`, here because
        `repo` is here: `confirm` must be given the remembered costs it derives
        its bound from, or #322 is back with the output unchanged."""
        self.repo(guarded=True)
        recording, seen = spying_on_confirm()
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(mutate, "confirm", recording),
        ):
            mutate.main(["--base", "HEAD", "--operator", "branch", "--no-killers"])
        self.assertEqual(len(seen), 1, "confirm was not reached")
        self.assertIn("costs", seen[0], "the generated path derives its bound from nothing")

    def test_the_pid_is_there_while_the_run_is_going(self) -> None:
        """Observed from inside, because it is gone by the time `main` returns
        -- which is the same reason the stale-marker test looks from in here."""
        self.repo(guarded=True)
        report = self.root / "rows.json"
        seen: list[str] = []

        def watching(rows: Sequence[Mutation], args: Any, **kw: Any) -> Report:
            seen.append(mutate._pidfile(report).read_text(encoding="utf-8").strip())
            return Report([])

        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(mutate, "_run_generated", watching),
        ):
            mutate.main(["--base", "HEAD", "--operator", "branch", "--json", str(report)])
        self.assertEqual(seen, [str(os.getpid())], "the run did not name itself")

    def test_a_finished_run_leaves_a_marker(self) -> None:
        """#275. `--json` says a batch landed; this says the run is over.

        `tools/watch.py --done` needs a file whose only meaning is "finished",
        because under `--batch` the report is written after every file and a
        watcher pointed at it announced a finish nine minutes early.
        """
        self.repo(guarded=True)
        report = self.root / "rows.json"
        finished = cli(self.root, "--base", "HEAD", "--operator", "branch", "--json", str(report))
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertTrue(mutate._marker(report).exists(), finished.stdout)

    def test_the_marker_is_written_even_when_the_news_is_bad(self) -> None:
        """It means *over*, not *clean*. A watcher that kept waiting on a run
        that found survivors would be the same silence, from a third side."""
        self.repo(guarded=False)
        report = self.root / "rows.json"
        finished = cli(self.root, "--base", "HEAD", "--operator", "branch", "--json", str(report))
        self.assertEqual(finished.returncode, 1)
        self.assertTrue(mutate._marker(report).exists())

    def test_listing_a_table_does_not_retract_an_earlier_marker(self) -> None:
        """`--list` runs nothing, so it has no finish to report and no earlier
        one to withdraw. The clear sits after that return for this reason."""
        self.repo(guarded=True)
        report = self.root / "rows.json"
        mutate._marker(report).write_text("", encoding="utf-8")
        cli(self.root, "--base", "HEAD", "--operator", "branch", "--json", str(report), "--list")
        self.assertTrue(mutate._marker(report).exists())

    def test_a_stale_marker_is_gone_while_the_run_is_still_going(self) -> None:
        """The half that makes the marker worth anything.

        A resumed sweep points `--json` at a part-written report from the run
        that was interrupted -- and at the marker that run never removed. Written
        only at the end, a watcher would read the *previous* run's marker and
        call this one finished before it started.

        Observed from inside the run rather than after it, because by the time
        `main` returns the marker is back and the two cases look identical.
        """
        self.repo(guarded=True)
        report = self.root / "rows.json"
        mutate._marker(report).write_text("", encoding="utf-8")
        seen: list[bool] = []

        def watching(rows: Sequence[Mutation], args: Any, **kw: Any) -> Report:
            seen.append(mutate._marker(report).exists())
            return Report([])

        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(mutate, "_run_generated", watching),
        ):
            mutate.main(["--base", "HEAD", "--operator", "branch", "--json", str(report)])
        self.assertEqual(seen, [False], "the previous run's marker outlived its report")
        self.assertTrue(mutate._marker(report).exists(), "and the new one was never written")

    def test_no_confirm_says_what_it_did_not_do(self) -> None:
        """A survivor that was never re-run against the whole suite may simply
        have been run against tests that cannot see it, and that is the
        expensive error -- it sends someone to rewrite a test that was fine."""
        self.repo(guarded=False)
        finished = cli(self.root, "--base", "HEAD", "--operator", "branch", "--no-confirm")
        self.assertIn("were not re-run against the whole suite", finished.stdout)
        self.assertEqual(finished.returncode, 1)

    def test_a_red_suite_stops_the_confirmation_correcting(self) -> None:
        """#268 through the command line, which is where it was wired.

        The two mutants that survived this change's own sweep were both at this
        call site -- `if not args.no_confirm` never taken, and the `not` dropped
        from `baseline=not args.no_baseline`. Neither is visible to a test that
        calls `confirm` directly, so the fix could have been wired to the wrong
        flag, or to nothing, with every unit test still green.
        """
        self.repo(guarded=False)
        self.red_for_its_own_reasons("tests/test_broken.py")
        finished = cli(self.root, "--base", "HEAD", "--operator", "branch")
        self.assertIn("No survivor was corrected", finished.stdout, finished.stderr)
        self.assertNotIn("caught by a test the selection had not run", finished.stdout)
        self.assertEqual(finished.returncode, 1, finished.stdout + finished.stderr)

    def test_no_baseline_reaches_the_confirmation_pass(self) -> None:
        """The other direction, without which the test above passes on a call
        site wired to `baseline=args.no_baseline` -- inverted, and asserting the
        inversion. Same red tree, flag on: the check is skipped, so the run
        corrects as it did before #268 and says nothing about a baseline."""
        self.repo(guarded=False)
        self.red_for_its_own_reasons("tests/test_broken.py")
        finished = cli(self.root, "--base", "HEAD", "--operator", "branch", "--no-baseline")
        self.assertNotIn("No survivor was corrected", finished.stdout, finished.stderr)

    def test_the_two_ways_of_asking_for_nothing_are_refused(self) -> None:
        for args, said in (
            (("--all", "--base", "HEAD"), "Not both"),
            ((), "give a spec file"),
        ):
            with self.subTest(args=args):
                finished = cli(self.root, *args)
                self.assertEqual(finished.returncode, 2, finished.stderr)
                self.assertIn(said, finished.stderr)


class TestTheCompletionMarker(unittest.TestCase):
    def test_the_pidfile_sits_beside_the_report_too(self) -> None:
        self.assertEqual(mutate._pidfile(Path("/tmp/r.json")), Path("/tmp/r.json.pid"))

    def test_it_sits_beside_the_report(self) -> None:
        """A sibling name rather than a suffix swap, so a reader sorting a
        directory finds the two together and `.json` keeps its meaning."""
        self.assertEqual(mutate._marker(Path("/tmp/r.json")), Path("/tmp/r.json.done"))

    def test_a_report_without_a_suffix_still_gets_one(self) -> None:
        self.assertEqual(mutate._marker(Path("/tmp/rows")), Path("/tmp/rows.done"))


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
        return cli(self.root, str(self.root / name))

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


#: A test id that certainly resolves, used where the point is "a real one is
#: kept". This module's own name, so it cannot go stale without this file being
#: edited -- and if it is renamed, the test that depends on it is right here
#: rather than somewhere that would fail mysteriously.
REAL = "tests.test_mutate.TestRememberingWhatCaughtEachMutation.test_a_remembered_test_runs_first"

#: A deliberate bug the suite notices, cheaply: `tests.test_entry` is twenty
#: tests in well under a second, and its clamp test fails the moment `truncate`
#: stops clamping. Measured before it was trusted -- `caught`, with
#: `tests.test_entry.TestParsedCommandsAreBounded.test_an_over_long_line_is_clamped_on_the_way_in`
#: recorded as the killer.
CLAMP_GUARD = Mutation(
    "an over-long command is no longer clamped",
    "woswoar/entry.py",
    "if len(cmd) <= MAX_CMD_CHARS:",
    "if True:",
    "tests.test_entry",
)

#: An edit the same selection cannot see: the cap moves by one character, and
#: `tests.test_entry` asserts around the symbol rather than the number. Also
#: measured -- `survived`. Used where a fixture needs a row that does not turn
#: into a whole-suite confirmation run.
CAP_NUDGE = Mutation(
    "the cap moves by one character",
    "woswoar/entry.py",
    "MAX_CMD_CHARS = 8000",
    "MAX_CMD_CHARS = 8001",
    "tests.test_entry",
)


def cached_row(
    path: str = "woswoar/sync.py",
    old: str = "a",
    new: str = "b",
    label: str = "x:1 in f()",
) -> Mutation:
    """One generated-shaped mutation, with only the fields the killer cache reads."""
    return Mutation(label, path, old, new, "tests.test_sync", operator="branch")


class TestRememberingWhatCaughtEachMutation(unittest.TestCase):
    """`Killers`: run the test that worked last time, first.

    The claim is about the *selection* the harness builds, which is what
    `failfast` then walks in order -- so that is what these assert on. Driving a
    whole sweep to observe it would take minutes and tell us the same thing.
    """

    def cache(self, known: dict[str, str]) -> mutate.Killers:
        with tempfile.TemporaryDirectory() as box:
            where = Path(box) / "killers.json"
            where.write_text(json.dumps(known), encoding="utf-8")
            return mutate.Killers(where)

    def test_a_remembered_test_runs_first(self) -> None:
        """In front, and the original selection untouched behind it."""
        one = cached_row()
        cached = self.cache({mutate._key(one): REAL})
        (ahead,) = cached.ahead_of([one])
        self.assertEqual(REAL, ahead.first)
        self.assertEqual("tests.test_sync", ahead.tests)

    def test_the_whole_selection_is_kept_behind_it(self) -> None:
        """The safety argument, asserted rather than assumed. Substituting the
        remembered test for the selection would make every stale entry a
        `caught` that nothing verified -- flattering the tests, which is the
        direction every bug in this class errs."""
        one = cached_row()._replace(tests="tests.test_sync tests.test_store")
        cached = self.cache({mutate._key(one): REAL})
        (ahead,) = cached.ahead_of([one])
        self.assertEqual("tests.test_sync tests.test_store", ahead.tests)
        self.assertEqual(REAL, ahead.first)

    def test_the_selection_stays_identical_across_rows(self) -> None:
        """`run` shards the baseline check by distinct `tests` string, so a
        killer folded into `tests` gives every row its own shard -- in tupferl,
        one baseline run of a 42-row file's selection became 42 of them, and
        that was the whole of a 372s -> 730s regression. It is invisible in
        every functional assertion above: the verdicts were identical. Only the
        clock saw it."""
        rows = [cached_row(old=f"a{n}") for n in range(5)]
        cached = self.cache({mutate._key(r): REAL for r in rows})
        ahead = cached.ahead_of(rows)
        self.assertEqual(1, len({r.tests for r in ahead}), "the baseline would shard per row")

    def test_a_test_that_no_longer_exists_is_dropped(self) -> None:
        """A renamed test leaves its module in place, so `unittest`'s loader
        records an error rather than raising -- and an error there is classified
        `broke`, which would turn every mutant that remembered it into a
        non-answer. One rename would produce a wall of them."""
        one = cached_row()
        cached = self.cache({mutate._key(one): "tests.test_mutate.NoSuchClass.no_such_test"})
        with contextlib.redirect_stdout(io.StringIO()):
            (ahead,) = cached.ahead_of([one])
        self.assertEqual("", ahead.first)

    def test_a_module_that_no_longer_exists_is_dropped_too(self) -> None:
        one = cached_row()
        cached = self.cache({mutate._key(one): "tests.test_gone.Class.test_x"})
        with contextlib.redirect_stdout(io.StringIO()):
            (ahead,) = cached.ahead_of([one])
        self.assertEqual("", ahead.first)

    def test_a_row_nothing_is_remembered_about_is_left_alone(self) -> None:
        (ahead,) = mutate.Killers(None).ahead_of([cached_row()])
        self.assertEqual("", ahead.first)
        self.assertEqual("tests.test_sync", ahead.tests)


class TestTheKeyIsContentNotPosition(unittest.TestCase):
    """A line number is invalidated by any edit above it -- which is every edit,
    so a position-keyed cache would be empty exactly when it was most wanted."""

    def test_the_same_edit_at_a_different_line_is_the_same_key(self) -> None:
        moved = cached_row(label="woswoar/sync.py:900 in f()")._replace(span=(9000, 9001))
        self.assertEqual(mutate._key(cached_row()), mutate._key(moved))

    def test_a_different_edit_at_the_same_line_is_a_different_key(self) -> None:
        """Otherwise two operators' rows on one line would share an entry, and
        the second would run a test chosen for the first."""
        self.assertNotEqual(mutate._key(cached_row()), mutate._key(cached_row(new="c")))
        self.assertNotEqual(
            mutate._key(cached_row()), mutate._key(cached_row(path="woswoar/store.py"))
        )
        self.assertNotEqual(
            mutate._key(cached_row()), mutate._key(cached_row()._replace(operator="return-value"))
        )


class TestWhatTheCacheLearns(unittest.TestCase):
    def learned(self, outcome: mutate.Outcome, killer: str) -> dict[str, str]:
        one = cached_row()
        with tempfile.TemporaryDirectory() as box:
            cache = mutate.Killers(Path(box) / "killers.json")
            cache.known = {mutate._key(one): "tests.test_old.C.t"}
            cache.learn(mutate.Report([mutate.Result(one, mutate.Verdict(outcome, "", killer))]))
            return cache.known

    def test_a_catch_is_remembered(self) -> None:
        self.assertEqual({mutate._key(cached_row()): REAL}, self.learned("caught", REAL))

    def test_a_survivor_forgets_whatever_used_to_catch_it(self) -> None:
        """Keeping it would put a test that cannot help at the front of every
        future run of this row, for ever."""
        self.assertEqual({}, self.learned("survived", ""))

    def test_a_run_that_asked_nothing_changes_nothing(self) -> None:
        """`broke` and `timeout` are not answers, so they are not evidence that
        the remembered test stopped working."""
        outcomes: tuple[mutate.Outcome, ...] = ("broke", "timeout")
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    {mutate._key(cached_row()): "tests.test_old.C.t"}, self.learned(outcome, "")
                )


class TestTheKillerIsRecordedAtAll(unittest.TestCase):
    """The cache is worth nothing if `Verdict.killer` is empty, and it comes
    from `tools/verdict.py` through a JSON report -- so this drives the real
    thing rather than asserting on a field."""

    def test_a_caught_mutation_names_the_test_in_a_form_unittest_takes_back(self) -> None:
        found = run([CLAMP_GUARD], baseline=False, workers=1, summarise=False)
        (result,) = found.results
        self.assertEqual("caught", result.verdict.outcome)
        self.assertTrue(result.verdict.killer, "nothing recorded the killing test")
        # The claim: it loads. `str(test)` -- "method (dotted.id)" -- does not.
        self.assertEqual({result.verdict.killer}, mutate._loadable([result.verdict.killer]))


class TestTheAlarmArming(unittest.TestCase):
    """`verdict.each_test`'s two answers, in process.

    The behaviour of an *armed* alarm is `tests/test_verdict.py`'s subject,
    driven through the real probe. What is asserted here is only the arming
    contract `mutate._run` builds its argv from: zero arms nothing, anything
    else comes back as given.
    """

    def test_zero_arms_nothing(self) -> None:
        before = signal.getsignal(signal.SIGALRM)
        self.addCleanup(signal.signal, signal.SIGALRM, before)
        self.assertEqual(0.0, verdict_module.each_test(0))
        self.assertEqual(2.0, verdict_module.each_test(2))


class TestTheCheapPrefix(unittest.TestCase):
    """`Killers.prefix`: cheap tests that between them catch a lot, first.

    The remembered killer (above) helps a row that has been seen. This helps
    every row, including one seen for the first time -- which is exactly the
    case a per-row cache cannot serve.

    Greedy on rows-newly-caught-per-second, which is the 4-approximation for
    Min-Sum Set Cover and the best any polynomial algorithm gets unless P=NP.
    """

    def cache(self, known: dict[str, str], cost: dict[str, float]) -> mutate.Killers:
        made = mutate.Killers(None)
        made.known, made.cost = known, cost
        return made

    def test_a_cheap_test_that_catches_a_lot_comes_first(self) -> None:
        """The ordering is by *rate*, not by either half alone: `slow` catches
        the most and `rare` is the cheapest, and neither is the answer."""
        cache = self.cache(
            {f"k{n}": "tests.m.C.slow" for n in range(50)}
            | {f"c{n}": "tests.m.C.quick" for n in range(20)}
            | {"r0": "tests.m.C.rare"},
            {"tests.m.C.slow": 0.40, "tests.m.C.quick": 0.02, "tests.m.C.rare": 0.001},
        )
        self.assertEqual(["tests.m.C.quick", "tests.m.C.rare", "tests.m.C.slow"], cache.prefix())

    def test_it_stops_at_the_budget(self) -> None:
        """Every row pays this up front, so it is bounded in seconds rather
        than in tests -- tests do not all cost the same."""
        cache = self.cache(
            {f"k{n}": f"tests.m.C.t{n}" for n in range(20)},
            {f"tests.m.C.t{n}": 0.2 for n in range(20)},
        )
        chosen = cache.prefix()
        self.assertLessEqual(sum(0.2 for _ in chosen), mutate.PREFIX)
        self.assertLess(len(chosen), 20)

    def test_a_test_with_no_measured_cost_is_not_guessed_at(self) -> None:
        """A killer recorded before costs existed has no denominator, and
        inventing one would put it anywhere at all in the order."""
        cache = self.cache({"k": "tests.m.C.unmeasured"}, {})
        self.assertEqual([], cache.prefix())

    def test_nothing_is_covered_twice(self) -> None:
        """Greedy credits a test only with rows nothing before it caught, or
        the second-best test rides on the first one's coverage and the prefix
        fills with duplicates."""
        cache = self.cache(
            {"a": "tests.m.C.one", "b": "tests.m.C.one", "c": "tests.m.C.two"},
            {"tests.m.C.one": 0.01, "tests.m.C.two": 0.02},
        )
        self.assertEqual(["tests.m.C.one", "tests.m.C.two"], cache.prefix())


class TestTheCacheLearnsFromARealRun(unittest.TestCase):
    """The plumbing, not the algorithm.

    `TestTheCheapPrefix` sets `cost` by hand, so every one of its assertions
    would pass while the harness recorded **zero** costs -- in tupferl, `sweep`
    re-wrapped the report without `times` and the prefix quietly ordered
    nothing. A test that builds its own inputs cannot see a data path that
    never delivers them.
    """

    #: One real run for the class, not one per test: each is a tree copy plus
    #: two subprocess suite runs, and the second test's claim is proven just as
    #: well against the first run's report.
    found: mutate.Report

    @classmethod
    def setUpClass(cls) -> None:
        cls.found = run([CAP_NUDGE], baseline=True, workers=1, summarise=False)

    def test_a_run_measures_the_tests_it_ran(self) -> None:
        times = self.found.times or {}
        self.assertTrue(times, "the run recorded no test timings at all")
        # `tests.test_entry` is CAP_NUDGE's whole selection, so its tests are
        # exactly what should have been measured.
        self.assertTrue(
            any(name.startswith("tests.test_entry.") for name in times), sorted(times)[:5]
        )
        self.assertTrue(all(seconds >= 0 for seconds in times.values()))

    def test_they_reach_the_cache(self) -> None:
        cache = mutate.Killers(None)
        cache.learn(self.found)
        self.assertEqual(self.found.times or {}, cache.cost)


class TestWhichRowsGetThePrefix(unittest.TestCase):
    """Which rows the cheap prefix reaches, and the two it used to be cut out of.

    Exact beats general: a row with a remembered killer runs that instead. For
    the rest, tupferl's first version compared module names -- which dropped the
    prefix in the two places it was worth most, and neither is visible in a
    `woswoar/` sweep: every file here has an importer, so every row names
    modules and matches. It is the `tools/` sweeps, and any new file nothing
    imports yet, that hit them.
    """

    def cache(self) -> mutate.Killers:
        made = mutate.Killers(None)
        made.cost = {REAL: 0.001}
        made.known = {"a-row-that-is-not-this-one": REAL}
        return made

    def ahead(self, tests: str) -> Mutation:
        with contextlib.redirect_stdout(io.StringIO()):
            (ahead,) = self.cache().ahead_of([cached_row()._replace(tests=tests)])
        return ahead

    def test_a_row_with_a_remembered_killer_does_not_pay_for_it(self) -> None:
        """That test is known to catch *this* row, so the prefix would only be
        work in front of the answer."""
        cache = self.cache()
        one = cached_row()._replace(tests="tests.test_mutate")
        cache.known = {mutate._key(one): REAL}
        (ahead,) = cache.ahead_of([one])
        self.assertEqual(REAL, ahead.first)

    def test_a_row_with_nothing_remembered_gets_the_prefix(self) -> None:
        self.assertEqual(REAL, self.ahead("tests.test_mutate").first)

    def test_the_prefix_is_cut_to_what_the_row_can_reach(self) -> None:
        """A test in a module that does not import the mutated file cannot see
        the mutation, so running it would be pure cost."""
        self.assertEqual("", self.ahead("tests.test_deps").first)

    def test_a_row_that_runs_everything_gets_the_whole_prefix(self) -> None:
        """`WHOLE_SUITE` is the empty string -- what a file nothing imports
        gets, so its rows run the entire suite. Cutting the prefix to "modules
        named in an empty selection" left them with nothing, which is the most
        expensive row in the table paying the most for the omission.

        Safe only because `first` reaches the probe as its own argument. Merged
        into the selection it would make an empty list non-empty, and "run
        everything" would become "run the prefix" -- see `verdict.collect`.
        """
        self.assertEqual(REAL, self.ahead(mutate.WHOLE_SUITE).first)

    def test_a_prefix_test_in_one_of_several_named_modules_is_kept(self) -> None:
        """`any`, not `all`: a row's selection may name several modules, and a
        prefix test only has to be reachable from *one* of them.

        Every other fixture here names a single module, where `any` and `all`
        agree -- the two-symmetric-inputs weakness, and the sweep found it:
        turning `any` into `all` survived the whole class. With two modules and
        the prefix reachable from one, the two answers differ.
        """
        self.assertEqual(REAL, self.ahead("tests.test_deps tests.test_mutate").first)

    def test_a_selection_naming_a_class_still_matches_its_tests(self) -> None:
        """`tests.test_mutate.TestX` selects `tests.test_mutate.TestX.test_y`.
        Comparing module names made this never match, so any row selected at
        class granularity silently lost the prefix."""
        klass = REAL.rsplit(".", 1)[0]
        self.assertEqual(REAL, self.ahead(klass).first)


class TestConfirmationReallyRunsTheWholeSuite(unittest.TestCase):
    """`CONTRIBUTING.md` promises every survivor is re-run against the whole
    suite before it is reported, and `Report.widened` is the flag that claims
    it.

    `WHOLE_SUITE` is the *empty* selection -- `verdict.collect` falls through to
    `discover` only when the list is empty -- so a remembered test still
    attached to a widened row is the one thing that could quietly turn
    "everything" into "only this". That the probe's protocol keeps a `first` in
    front of a discovery rather than in place of it is
    `tests.test_verdict.TestWhichTestsGetRun`'s subject; what is asserted here
    is the rows `confirm` actually builds.
    """

    def test_a_widened_row_carries_no_remembered_test(self) -> None:
        survivor = mutate.Result(cached_row()._replace(first=REAL), mutate.Verdict("survived"))
        seen: list[Mutation] = []

        def watch(rerun: Any, *args: Any, **kwargs: Any) -> mutate.Report:
            seen.extend(rerun)
            return mutate.Report([mutate.Result(row, mutate.Verdict("survived")) for row in seen])

        with (
            mock.patch.object(mutate, "run", watch),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            confirm(mutate.Report([survivor]), None, 30.0, MEMORY, baseline=False)
        (row,) = seen
        self.assertEqual("", row.first, "the remembered test rode into the whole-suite pass")
        self.assertEqual(mutate.WHOLE_SUITE, row.tests)


class TestARowActuallyRunsWithItsPrefix(unittest.TestCase):
    """The gap that let tupferl's `WHOLE_SUITE` defect through.

    Every other test here asserts on what `ahead_of` *returns*. None drove
    `_attempt`, so nothing noticed that the argv it built turned "discover
    everything" into "run these three" -- the review found it by reading. These
    drive a real mutation with a real `first`.
    """

    def test_a_prefix_that_catches_it_still_reports_caught(self) -> None:
        one = CLAMP_GUARD._replace(
            first=(
                "tests.test_entry.TestParsedCommandsAreBounded"
                ".test_an_over_long_line_is_clamped_on_the_way_in"
            )
        )
        found = run([one], baseline=False, workers=1, summarise=False)
        self.assertEqual(["caught"], [r.verdict.outcome for r in found.results])

    def test_a_prefix_that_misses_falls_through_to_the_selection(self) -> None:
        """The safety argument, driven rather than asserted on a data
        structure: a prefix that cannot see the mutation must cost one test,
        not the answer."""
        one = CLAMP_GUARD._replace(first="tests.test_deps.TestInstaller")
        found = run([one], baseline=False, workers=1, summarise=False)
        self.assertEqual(["caught"], [r.verdict.outcome for r in found.results])


#: The spec-file spelling of `CLAMP_GUARD`, generated from its fields so the
#: two cannot disagree when `woswoar/entry.py` changes -- only the one
#: real-run `--json` test would catch a drifted literal; the mocked-`run`
#: tests would not.
SPEC_TABLE = (
    "from tools.mutants import Mutation\n"
    "MUTATIONS = [\n"
    "    Mutation(\n"
    f"        label={CLAMP_GUARD.label!r},\n"
    f"        path={CLAMP_GUARD.path!r},\n"
    f"        old={CLAMP_GUARD.old!r},\n"
    f"        new={CLAMP_GUARD.new!r},\n"
    f"        tests={CLAMP_GUARD.tests!r},\n"
    "    )\n"
    "]\n"
)


def spec_file(box: Path) -> Path:
    where = box / "spec.py"
    where.write_text(SPEC_TABLE, encoding="utf-8")
    return where


class TestASpecFileGetsTheFlagsItWasGiven(unittest.TestCase):
    """A `MUTATIONS` table honours the command line it was run with.

    It did not. The dispatch was `run(mutations)` -- no arguments -- so
    `argparse` accepted `--workers`, `--memory`, `--timeout`, `--each-test`,
    `--no-baseline`, `--no-confirm` and `--json`, and every one of them was
    dropped on the floor. Asking for one lane got two. Asking for a report got
    no file, which reads as the run having failed to write one rather than as
    the flag never having been consulted -- an hour of tupferl's time went to
    diagnosing exactly that misreading.

    Each test below fails against `run(mutations)`, because that call cannot
    carry the value being asserted.
    """

    #: `SPEC_TABLE` is a real row against a real file, because the `--json`
    #: test below drives the actual run rather than a stub: `check` refuses a
    #: path that is not there, and a report of nothing would not tell us the
    #: flag was honoured. One that is *caught*, deliberately. With a surviving
    #: row, a mutant that forces confirmation on sends the `--json` test into a
    #: whole-suite re-run. Nothing here asserts on the survivor count, so the
    #: cheaper row costs the tests nothing and gives the sweep two answers back.

    def asked(self, *flags: str) -> dict[str, Any]:
        """Run a spec file with `flags` and return the kwargs `run` received.

        `run` is replaced rather than driven, because what is under test is the
        wiring between the parser and the call -- not what a mutation does,
        which every other class here covers and which would cost a suite run
        per flag.
        """
        seen: dict[str, Any] = {}

        def watch(table: Any, *args: Any, **kwargs: Any) -> mutate.Report:
            seen.update(kwargs)
            seen["positional"] = args
            return mutate.Report([])

        with tempfile.TemporaryDirectory(prefix="woswoar-spec-") as name:
            box = Path(name)
            with mock.patch.object(mutate, "run", watch):
                mutate.main([str(spec_file(box)), "--no-confirm", "--no-killers", *flags])
        return seen

    def test_workers_reaches_the_run(self) -> None:
        self.assertEqual(1, self.asked("--workers", "1")["workers"])

    def test_memory_reaches_the_run(self) -> None:
        self.assertEqual(0, self.asked("--memory", "0")["memory"])

    def test_the_timeout_reaches_the_run(self) -> None:
        self.assertEqual(7.0, self.asked("--timeout", "7")["timeout"])

    def test_the_per_test_alarm_reaches_the_run(self) -> None:
        self.assertEqual(3.0, self.asked("--each-test", "3")["each"])

    def test_no_baseline_reaches_the_run(self) -> None:
        self.assertFalse(self.asked("--no-baseline")["baseline"])

    def test_the_baseline_is_on_by_default(self) -> None:
        """The other half: a wiring that hard-coded `False` would pass the test
        above and quietly stop checking the untouched tree."""
        self.assertTrue(self.asked()["baseline"])

    def test_json_is_written_and_marked_done(self) -> None:
        """Not through the `run` stub -- this one drives the real thing,
        because the report and its `.done` marker are what a watcher reads and
        a stub cannot produce them."""
        with tempfile.TemporaryDirectory(prefix="woswoar-spec-") as name:
            box = Path(name)
            report = box / "out.json"
            with contextlib.redirect_stdout(io.StringIO()):
                mutate.main(
                    [
                        str(spec_file(box)),
                        "--no-baseline",
                        "--no-confirm",
                        "--no-killers",
                        "--json",
                        str(report),
                    ]
                )
            self.assertTrue(report.is_file(), "--json wrote nothing")
            self.assertIn("results", json.loads(report.read_text(encoding="utf-8")))
            self.assertTrue(
                report.with_suffix(".json.done").is_file(), "the run left no done marker"
            )


class TestASpecFilesSurvivorsAreConfirmed(unittest.TestCase):
    """The spec path keeps `CONTRIBUTING.md`'s promise about survivors now.

    It never did before: survivors from a hand-written table were reported
    without the whole-suite re-run, so `--no-confirm` was doubly inert -- it
    turned off something that was not happening. The exit-status halves of the
    spec path are `TestTheScriptEntryPoint`'s; these cover only the
    confirmation wiring that used not to exist.
    """

    def status(self, *flags: str) -> tuple[int, dict[str, Any], list[bool]]:
        seen: dict[str, Any] = {}
        called: list[bool] = []

        def watch(report: Any, *args: Any, **kwargs: Any) -> Any:
            called.append(True)
            seen.update(kwargs)
            return report

        quiet = contextlib.redirect_stdout(io.StringIO())
        with tempfile.TemporaryDirectory(prefix="woswoar-exit-") as name, quiet:
            box = Path(name)
            with mock.patch.object(mutate, "confirm", watch):
                code = mutate.main([str(spec_file(box)), "--no-baseline", "--no-killers", *flags])
        return code, seen, called

    def test_survivors_are_confirmed_against_the_whole_suite_by_default(self) -> None:
        _, _, called = self.status()
        self.assertEqual([True], called, "survivors were reported without being confirmed")

    def test_confirmation_is_told_what_the_run_was_told(self) -> None:
        """Its `baseline` comes from `--no-baseline` like the run's does. A
        wiring that passed the flag through un-negated -- checking a baseline
        the caller asked to skip -- would pass every test above."""
        _, seen, _ = self.status()
        self.assertFalse(seen["baseline"], "confirmation re-checked a skipped baseline")

    def test_no_confirm_really_turns_it_off(self) -> None:
        """The precondition for the two above: if `confirm` ran either way,
        their assertions would hold against a wiring that ignored the flag."""
        _, _, called = self.status("--no-confirm")
        self.assertEqual([], called, "--no-confirm confirmed anyway")


class TestASpecFileGetsTheRestOfTheMachinery(unittest.TestCase):
    """The killer cache, `--baseline-only`, and the `--json` lifecycle reach the
    spec path too.

    `_run_spec` exists because flags were silently dropped there once already;
    its first fix stopped at the flags it knew about, and `--killers`,
    `--baseline-only` and half the `--json` lifecycle were silently inert in
    exactly the way it complains about.
    """

    def test_a_remembered_killer_runs_first_on_a_spec_row_too(self) -> None:
        seen: list[Mutation] = []

        def watch(table: Any, *args: Any, **kwargs: Any) -> mutate.Report:
            seen.extend(table)
            return mutate.Report([])

        with tempfile.TemporaryDirectory(prefix="woswoar-spec-") as name:
            box = Path(name)
            remembered = box / "killers.json"
            remembered.write_text(json.dumps({mutate._key(CLAMP_GUARD): REAL}), encoding="utf-8")
            with mock.patch.object(mutate, "run", watch):
                mutate.main(
                    [
                        str(spec_file(box)),
                        "--no-confirm",
                        "--killers",
                        str(remembered),
                    ]
                )
        (row,) = seen
        self.assertEqual(REAL, row.first, "the cache never reached the spec path")

    def test_baseline_only_runs_no_mutation(self) -> None:
        """The question and nothing else -- `run` being reached would mean the
        flag fell through to a full table run."""

        def refuse(*args: Any, **kwargs: Any) -> mutate.Report:
            raise AssertionError("--baseline-only ran the table")

        with (
            tempfile.TemporaryDirectory(prefix="woswoar-spec-") as name,
            mock.patch.object(mutate, "run", refuse),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = mutate.main(
                [str(spec_file(Path(name))), "--baseline-only", "--no-killers", "--workers", "1"]
            )
        self.assertEqual(0, code, "a green baseline did not exit zero")

    def test_a_stale_done_marker_is_cleared_before_the_run(self) -> None:
        """A `.done` left by a previous run of the same spec would tell a
        watcher this run finished before it began -- the false finish the
        generated path already guards against. The pid is written first, so a
        watcher started alongside has something to read."""
        seen: dict[str, Any] = {}

        with tempfile.TemporaryDirectory(prefix="woswoar-spec-") as name:
            box = Path(name)
            report = box / "out.json"
            marker = mutate._marker(report)
            marker.touch()

            def watch(table: Any, *args: Any, **kwargs: Any) -> mutate.Report:
                seen["marker"] = marker.exists()
                seen["pid"] = mutate._pidfile(report).read_text(encoding="utf-8").strip()
                return mutate.Report([])

            with mock.patch.object(mutate, "run", watch):
                mutate.main(
                    [
                        str(spec_file(box)),
                        "--no-confirm",
                        "--no-killers",
                        "--json",
                        str(report),
                    ]
                )
            self.assertFalse(seen["marker"], "the stale marker outlived the start of the run")
            self.assertEqual(str(os.getpid()), seen["pid"])
            self.assertTrue(marker.exists(), "the finished run earned no marker")
            self.assertFalse(mutate._pidfile(report).exists(), "the dead pid was left behind")


class TestTheBaselineShards(unittest.TestCase):
    """`_shards`: what the baseline runs, and what it no longer re-runs.

    The remembered-`first` shard exists for the killer `confirm` records from a
    whole-suite run -- a test *outside* every row's selection, which red on the
    untouched tree would hand a row `caught` over a green-looking baseline. The
    prefix's names, by contrast, are cut to each row's own selection, so a
    shard repeating them is pure cost -- the whole bill on `--baseline-only`.
    """

    def test_a_remembered_test_outside_every_selection_gets_its_shard(self) -> None:
        row = cached_row()._replace(tests="tests.test_sync", first="tests.test_deps.T.test_x")
        self.assertIn("tests.test_deps.T.test_x", mutate._shards([row]))

    def test_one_already_covered_by_a_selection_does_not(self) -> None:
        row = cached_row()._replace(tests="tests.test_sync", first="tests.test_sync.T.test_x")
        self.assertEqual(["tests.test_sync"], mutate._shards([row]))

    def test_a_whole_suite_row_covers_everything(self) -> None:
        rows = [
            cached_row()._replace(tests=mutate.WHOLE_SUITE, first="tests.test_deps.T.test_x"),
            cached_row(old="a2")._replace(tests="tests.test_sync"),
        ]
        self.assertEqual([mutate.WHOLE_SUITE, "tests.test_sync"], mutate._shards(rows))


class TestWhatLandsWhileTheRunIsStillGoing(unittest.TestCase):
    """`landed` is how `sweep` writes answers out as they arrive, and the one
    thing it must never hand over is a row from a red-baseline run.

    Driven with a real `run` rather than a stub, which is the gap the sweep
    found: every other test of this fires `sweep` with `mutate.run` replaced,
    so the callback itself -- the whole mechanism -- never executed. A resume
    trusts what reached `--json`, so "nothing void was handed over" is the
    claim, and a stub cannot make it.
    """

    def landings(self, table: list[Mutation], red: bool) -> list[str]:
        """Labels handed to `landed`, from a real run of `table`.

        The baseline is made red by failing the *shard*, not by breaking the
        tree: `_borrow` is what runs a baseline shard, and a `Verdict` from it
        that is not `survived` is exactly what a red untouched suite looks
        like one layer up. That keeps the fixture to one seam and leaves the
        code under test -- `run`'s collection order and its `not red` guard --
        real.
        """
        seen: list[str] = []
        real = mutate._borrow

        def borrow(*args: Any, **kwargs: Any) -> mutate.Verdict:
            if not red:
                return real(*args, **kwargs)
            return mutate.Verdict("caught", "tests.test_entry.T.test_x", why="a traceback")

        with (
            mock.patch.object(mutate, "_borrow", borrow),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mutate.run(
                table,
                baseline=True,
                workers=2,
                summarise=False,
                landed=lambda result: seen.append(result.mutation.label),
            )
        return seen

    def test_every_row_is_handed_over_as_it_lands(self) -> None:
        table = [CLAMP_GUARD, CAP_NUDGE]
        self.assertEqual([row.label for row in table], self.landings(table, red=False))

    def test_a_red_baseline_hands_over_nothing(self) -> None:
        """The headline of this branch. `_recorded` drops the red flag on read
        and `sweep` skips a recorded file by name, so a void row that reaches
        `--json` comes back on the next run as a final answer."""
        self.assertEqual([], self.landings([CLAMP_GUARD, CAP_NUDGE], red=True))


class TestASweepWritesEachFileOutAsItFinishes(unittest.TestCase):
    """`sweep`'s bookkeeping around a real run: count a file down, say so, and
    persist what is complete.

    Real again rather than stubbed, for the reason its neighbour gives. Both
    rows here name the same file, so one "complete" line is the whole of it.
    """

    def swept(self, args_extra: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        box = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, box, True)
        report = Path(box) / "out.json"
        args = argparse.Namespace(
            json=report,
            no_baseline=True,
            workers=2,
            timeout=mutate.TIMEOUT,
            each_test=mutate.EACH_TEST,
            memory=mutate.MEMORY,
            all=True,
            batch=True,
            **(args_extra or {}),
        )
        with contextlib.redirect_stdout(io.StringIO()) as said:
            mutate.sweep([CLAMP_GUARD, CAP_NUDGE], args)
        return said.getvalue(), json.loads(report.read_text(encoding="utf-8"))

    def test_the_file_is_counted_down_and_its_rows_recorded(self) -> None:
        said, written = self.swept()
        self.assertIn(f"-- {CLAMP_GUARD.path} complete", said)
        self.assertEqual(
            sorted(row["label"] for row in written["results"]),
            sorted([CLAMP_GUARD.label, CAP_NUDGE.label]),
        )

    def test_a_finished_file_is_on_disk_before_the_run_ends(self) -> None:
        """The whole point of writing per file: a crash costs one file, not the
        afternoon. Both assertions above are satisfied by `sweep`'s *final*
        write, so neither can see the incremental one -- measured, by a spec
        table: deleting `finished`'s `_persist` survived them both.

        So this looks while the run is still going. The second file's row is
        held until the first file's rows have been observed on disk; if the
        report is only written at the end, the wait times out and the run
        deadlocks rather than passing quietly -- which is why the bound is
        short and its expiry is the failure.
        """
        import threading

        held, seen = threading.Event(), threading.Event()
        box = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, box, True)
        report = Path(box) / "out.json"
        # Two files, so one can finish while the other is still in flight.
        # `_attempt` is replaced rather than the tree mutated: what is under
        # test is when `sweep` *writes*, not what a mutation does.
        slow = Mutation(
            "a second file",
            "woswoar/codec.py",
            "MAX_EXPORT_BYTES = 8 * 1024 * 1024",
            "MAX_EXPORT_BYTES = 9 * 1024 * 1024",
            "tests.test_codec",
        )

        # The two bounds must not be equal, and that is the whole of the
        # fixture. The watcher gives up after LOOKING, then releases the held
        # row; the row's own wait is far longer, so the release only ever comes
        # from the watcher. Written with both at 30s the two raced -- `sweep`'s
        # *final* write landed while the watcher was still polling, so deleting
        # the incremental one passed anyway. Measured: that spelling reported
        # SURVIVED against the narrow selection and `caught` only under the
        # whole suite, which is the shape of a fixture that decides by timing.
        LOOKING, RELEASED = 10.0, 60.0

        def attempt(mutation: Mutation, *args: Any, **kwargs: Any) -> mutate.Verdict:
            if mutation.path == slow.path:
                held.wait(timeout=RELEASED)
            return mutate.Verdict("caught", "tests.test_entry.T.test_x")

        def watch() -> None:
            deadline = time.monotonic() + LOOKING
            while time.monotonic() < deadline:
                if report.is_file() and json.loads(report.read_text(encoding="utf-8"))["results"]:
                    seen.set()
                    break
                time.sleep(0.05)
            held.set()

        args = argparse.Namespace(
            json=report,
            no_baseline=True,
            workers=2,
            timeout=mutate.TIMEOUT,
            each_test=mutate.EACH_TEST,
            memory=mutate.MEMORY,
            all=True,
            batch=True,
        )
        looking = threading.Thread(target=watch)
        looking.start()
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(mutate, "_attempt", attempt),
        ):
            mutate.sweep([CAP_NUDGE._replace(path=CLAMP_GUARD.path), slow], args)
        looking.join()
        self.assertTrue(seen.is_set(), "nothing was written until the whole run had finished")

    def test_the_answers_are_the_real_ones(self) -> None:
        """The pair was measured before it was trusted: the clamp guard is
        caught by `tests.test_entry`, the one-character cap nudge is not. A
        sweep that recorded rows without running them would pass the test above
        and fail this one."""
        _, written = self.swept()
        by_label = {row["label"]: row["outcome"] for row in written["results"]}
        self.assertEqual("caught", by_label[CLAMP_GUARD.label])
        self.assertEqual("survived", by_label[CAP_NUDGE.label])


class TestWhoOwnsTheMachine(unittest.TestCase):
    """`_budget` halves a shared machine and does not halve a dedicated one.

    The halving was unconditional, and on a machine with nobody else on it that
    is waste rather than thrift: measured in tupferl on a 16 GiB four-core
    container, it gave a budget of 8037 MiB, which `_share` turned into
    **three** lanes where the cores wanted eight -- 283.7s against 154.7s on
    one interleaved pair of the same 17-mutant table.
    """

    #: Bigger than `_SPARE` and than `_FLOOR`, so "leave a gibibyte" and "never
    #: go under the floor" are both visible rather than clipping each other.
    VISIBLE = 16 << 30

    def budget(self, **environment: str) -> int:
        seen = mock.patch.object(mutate, "_visible_memory", lambda: self.VISIBLE)
        with seen, mock.patch.dict(os.environ, environment, clear=True):
            return mutate._budget()

    def test_a_shared_machine_keeps_half_for_the_person_using_it(self) -> None:
        with mock.patch.object(mutate, "_confined", lambda: 0):
            self.assertEqual(self.VISIBLE // 2, self.budget())

    def test_a_ci_runner_is_not_shared(self) -> None:
        """Nobody is waiting for their editor on a CI runner, and every CI
        system sets this.

        The gibibyte is written out rather than taken from `mutate._SPARE`:
        against the constant this assertion changes with the code it checks and
        holds for any value of it -- the copy-of-the-code fixture by name. In
        tupferl's sweep both mutations of `_SPARE` survived the symbolic
        spelling.
        """
        self.assertEqual(self.VISIBLE - (1 << 30), self.budget(CI="true"))

    def test_a_cgroup_limit_means_the_share_is_already_carved_out(self) -> None:
        """Halving a cgroup limit double-counts the same reservation: the
        container has no other half to leave, because nobody else is in it."""
        with mock.patch.object(mutate, "_confined", lambda: 1 << 30):
            self.assertEqual(self.VISIBLE - (1 << 30), self.budget())

    def test_being_told_beats_both(self) -> None:
        asked = 12 << 30
        self.assertEqual(asked, self.budget(**{mutate._TOTAL: str(asked)}))
        self.assertEqual(asked, self.budget(CI="true", **{mutate._TOTAL: str(asked)}))

    def test_nonsense_in_the_variable_is_ignored_rather_than_obeyed(self) -> None:
        with mock.patch.object(mutate, "_confined", lambda: 0):
            for said in ("", "0", "-1", "lots"):
                with self.subTest(said=said):
                    self.assertEqual(self.VISIBLE // 2, self.budget(**{mutate._TOTAL: said}))

    def test_a_tiny_dedicated_machine_never_drops_under_the_floor(self) -> None:
        """Otherwise subtracting `_SPARE` hands it less than one lane's ceiling
        and it gets *fewer* lanes than the shared rule would have given -- the
        opposite of the point."""
        tiny = mock.patch.object(mutate, "_visible_memory", lambda: (1 << 30) + (1 << 20))
        with tiny, mock.patch.dict(os.environ, {"CI": "true"}, clear=True):
            self.assertEqual(mutate._FLOOR, mutate._budget())

    def test_the_run_says_which_rule_it_used(self) -> None:
        """A lane count nobody can account for is what sent tupferl's author
        reading `_share` in the first place."""
        alone = mock.patch.object(mutate, "_confined", lambda: 0)
        with mock.patch.dict(os.environ, {}, clear=True), alone:
            self.assertIn("shared", mutate._why())
        with mock.patch.dict(os.environ, {"CI": "true"}, clear=True):
            self.assertIn("dedicated", mutate._why())
        with mock.patch.dict(os.environ, {mutate._TOTAL: "123"}, clear=True):
            # `mutate._TOTAL`, not the literal. In tupferl this asserted
            # "--budget" and passed for a release, naming a flag the parser has
            # never had -- so someone reading the printed line and typing it
            # got "unrecognized arguments". A test that pins prose has to pin
            # it against the thing it describes.
            self.assertEqual(mutate._TOTAL, mutate._why())


class TestReadingACgroupLimit(unittest.TestCase):
    """`_confined` tells a real limit from the two ways of saying "no limit"."""

    HOST = 16 << 30

    def confined(self, written: str) -> int:
        def reads(where: str, **kwargs: object) -> str:
            del where, kwargs
            return written

        # Both patched by name rather than through `mutate.<attr>`: the module
        # imports `os` and `Path`, it does not re-export them, and mypy is
        # right to say so.
        host = mock.patch(
            "os.sysconf", lambda name: {"SC_PAGE_SIZE": 1, "SC_PHYS_PAGES": self.HOST}[name]
        )
        with mock.patch("pathlib.Path.read_text", reads), host:
            return mutate._confined()

    def test_a_real_limit_below_the_host_total_counts(self) -> None:
        self.assertEqual(2 << 30, self.confined(str(2 << 30)))

    def test_cgroup_v2_writes_max_for_no_limit(self) -> None:
        self.assertEqual(0, self.confined("max"))

    def test_cgroup_v1_writes_a_sentinel_near_two_to_the_sixty_three(self) -> None:
        """9223372036854771712 is what a v1 `memory.limit_in_bytes` holds for
        "no limit". Read as a limit it would look like the largest dedicated
        machine ever built."""
        self.assertEqual(0, self.confined("9223372036854771712"))


class TestWhenTheMachineCannotSayHowBigItIs(unittest.TestCase):
    """`_confined`'s answers when the question cannot be asked.

    In tupferl every one of these lines survived the first sweep of the budget
    change: the tests above mock `_confined` wholesale, which proves what
    `_budget` does with its answer and nothing about how the answer is reached.
    """

    def test_no_host_total_means_no_limit_can_be_judged(self) -> None:
        """`sysconf` is not POSIX everywhere. Without a host total there is
        nothing to compare a cgroup file against, and calling any number a
        limit would be guessing -- a v1 sentinel would read as a machine with
        eight exabytes."""
        with mock.patch("os.sysconf", side_effect=OSError):
            self.assertEqual(0, mutate._confined())

    def test_no_cgroup_file_means_the_host_bounds_us(self) -> None:
        with mock.patch("pathlib.Path.read_text", side_effect=OSError):
            self.assertEqual(0, mutate._confined())

    def test_a_shared_machine_is_reported_as_the_empty_string(self) -> None:
        """`dedicated` returns a *reason*, and "no reason" has to be falsy for
        `_budget` to read it. `None` would work there and break the line that
        prints it."""
        alone = mock.patch.object(mutate, "_confined", lambda: 0)
        with mock.patch.dict(os.environ, {}, clear=True), alone:
            self.assertEqual("", mutate.dedicated())

    def test_a_budget_of_one_byte_is_still_a_budget(self) -> None:
        """The boundary on `int(said) > 0`. A fixture using a comfortable
        number cannot tell that from `> 1`."""
        with mock.patch.dict(os.environ, {mutate._TOTAL: "1"}, clear=True):
            self.assertEqual(1, mutate._budget())
