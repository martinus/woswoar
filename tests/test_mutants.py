"""Tests for the mutant generator.

Everything here except `TestReadingARealDiff` is pure -- source text in,
mutations out -- so it runs in milliseconds and needs no sandbox, no subprocess
and no suite. That is the whole reason `tools/mutants.py` is a separate module
from the runner.

The generator can be wrong in two directions and they are not equally bad. A
mutant it fails to produce is coverage nobody asked for. A mutant whose *span*
is wrong edits a different part of the file than its label claims, still parses,
and reports `caught` or `SURVIVED` about something nobody chose -- which is the
class of error `tools/mutate.py` exists to prevent, arriving one module earlier.
So the span assertions here are exact, never "it contains".
"""

from __future__ import annotations

import ast
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools import mutants
from tools.mutants import Mutation

from . import support
from .support import requires_git

REPO_ROOT = Path(__file__).resolve().parent.parent


def mutate(
    body: str, lines: set[int] | None = None, operators: list[str] | None = None
) -> list[Mutation]:
    """Every mutant for a snippet, on every line unless told otherwise."""
    source = textwrap.dedent(body).lstrip()
    if lines is None:
        lines = set(range(1, len(source.splitlines()) + 1))
    return mutants.generate(source, "woswoar/thing.py", lines, operators=operators)


class TestReadingAHunkHeader(unittest.TestCase):
    """`git diff --unified=0`, parsed without a repository in sight."""

    def test_a_single_line_hunk(self) -> None:
        diff = "+++ b/woswoar/x.py\n@@ -13 +13 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"woswoar/x.py": {13}})

    def test_a_counted_hunk(self) -> None:
        diff = "+++ b/woswoar/x.py\n@@ -15,2 +15,3 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"woswoar/x.py": {15, 16, 17}})

    def test_an_insertion_names_the_new_lines_only(self) -> None:
        diff = "+++ b/woswoar/x.py\n@@ -58,0 +60,6 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"woswoar/x.py": set(range(60, 66))})

    def test_a_pure_deletion_contributes_nothing(self) -> None:
        """`+9,0` is a removal: there is no new code at line 9 to mutate.

        Taking it as one line -- which "omitted means 1" would, if the zero were
        not checked -- generates mutants for whatever now sits at that number,
        labelled as if the diff had put it there.
        """
        diff = "+++ b/woswoar/x.py\n@@ -10,3 +9,0 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {})

    def test_a_deleted_file_contributes_nothing(self) -> None:
        """And it needs no special case: git spells its hunk `+0,0`.

        The deleted file comes *second* so that a parser which failed to notice
        it would attribute the hunk to `y.py` -- which is the only way this can
        be a test at all. It passes because the count is zero, not because
        `/dev/null` is recognised.
        """
        diff = "+++ b/woswoar/y.py\n@@ -1 +1 @@\n+++ /dev/null\n@@ -1,5 +0,0 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"woswoar/y.py": {1}})

    def test_several_files_in_one_diff(self) -> None:
        diff = "+++ b/woswoar/x.py\n@@ -1 +1 @@\n+++ b/tools/y.py\n@@ -4,2 +4,2 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"woswoar/x.py": {1}, "tools/y.py": {4, 5}})

    def test_a_quoted_path(self) -> None:
        """git C-quotes any path with an odd byte in it, `b/` inside the quotes."""
        diff = '+++ "b/woswoar/odd\\tname.py"\n@@ -1 +1 @@\n'
        self.assertEqual(mutants.parse_hunks(diff), {"woswoar/odd\tname.py": {1}})


class TestWhatMayBeMutated(unittest.TestCase):
    def test_the_product_and_the_tools_are_in(self) -> None:
        self.assertTrue(mutants.mutable("woswoar/sync.py"))
        self.assertTrue(mutants.mutable("tools/mutate.py"))

    def test_the_tests_are_not(self) -> None:
        """Breaking a test proves nothing about the fix, and the run would
        cheerfully report the assertion it had just deleted."""
        self.assertFalse(mutants.mutable("tests/test_sync.py"))

    def test_other_files_are_not(self) -> None:
        self.assertFalse(mutants.mutable("woswoar/shell/woswoar.bash"))
        self.assertFalse(mutants.mutable("docs/architecture.md"))
        self.assertFalse(mutants.mutable("setup.py"))


class TestTheOperators(unittest.TestCase):
    """One fixture where each fires, one where it must not."""

    def prose(self, body: str, operators: list[str]) -> list[str]:
        return [row.label.split(" -- ", 1)[1] for row in mutate(body, operators=operators)]

    def test_every_operator_has_a_test_here(self) -> None:
        """The completeness guard, in the shape `test_architecture.py` uses.

        An operator added without a fixture would otherwise ship untested, and
        the generator's own output is the last place a gap is visible.
        """
        named = {operator.name for operator in mutants.OPERATORS}
        self.assertEqual(set(_FIRES), named)
        # Both tables, not just the first. An operator could otherwise ship with
        # a fires-fixture and no must-not-fire fixture, which is half a test.
        self.assertEqual(set(_QUIET), named)

    def test_each_operator_fires_on_its_own_fixture(self) -> None:
        for name, (body, expected) in _FIRES.items():
            with self.subTest(operator=name):
                got = self.prose(body, operators=[name])
                self.assertEqual(got, expected)

    def test_no_operator_fires_on_a_fixture_it_should_not(self) -> None:
        for name, body in _QUIET.items():
            with self.subTest(operator=name):
                self.assertEqual(self.prose(body, operators=[name]), [])


#: Fixture -> the prose every operator should produce on it, in order.
_FIRES: dict[str, tuple[str, list[str]]] = {
    "boundary": ("x = a < b\n", ["`<` becomes `<=`"]),
    "negate": ("x = a is None\n", ["`is` becomes `is not`"]),
    "connector": ("x = a and b\n", ["`and` becomes `or`"]),
    "branch": (
        "if ready:\n    go()\n",
        ["the `if` is always taken", "the `if` is never taken"],
    ),
    "drop-not": ("x = not ready\n", ["the `not` is dropped"]),
    "order": (
        "x = sorted(days)\n",
        ["the ordering is reversed", "`sorted` becomes `list`"],
    ),
    "affix": (
        'x = name.endswith(".age")\n',
        ["the `.endswith(...)` filter accepts everything"],
    ),
    "return-constant": (
        "def f():\n    return True\n",
        ["returns `False` instead of `True`"],
    ),
    "drop-call": ("path.mkdir()\n", ["the call to `path.mkdir(...)` never happens"]),
    "drop-assign": ("self.total = 1\n", ["`self.total` is never assigned"]),
    "drop-kwarg": ("path.mkdir(mode=0o700)\n", ["`mode=448` is dropped"]),
    "off-by-one": ("x = items[1]\n", ["`1` becomes `2`", "`1` becomes `0`"]),
    "return-value": (
        "def f():\n    return compute(x)\n",
        ["returns `None` instead of `compute(x)`"],
    ),
    # Both shapes: an expression and an augmented assignment. With only the
    # first, the mutation cutting `AugAssign` out of `arith` survived.
    "arith": (
        "x = a + b\ncount += 1\n",
        ["`+` becomes `-`", "`+=` becomes `-=`"],
    ),
    "slice": ("x = items[1:]\n", ["the slice takes everything"]),
}

#: Fixture -> a shape the operator must leave alone.
_QUIET: dict[str, str] = {
    # A chained comparison has two operators and no single answer.
    "boundary": "x = a < b < c\n",
    "negate": "x = a < b\n",
    "connector": "x = a + b\n",
    # Already constant: forcing `True` to `True` changes nothing, and a row that
    # changes nothing reports about nothing.
    "branch": "if True:\n    go()\n",
    "drop-not": "x = -value\n",
    "order": "x = sorted(days, reverse=True)\n",
    "affix": "x = name.strip()\n",
    "return-constant": "def f():\n    return value\n",
    # The meter is never the thing being compared.
    "drop-call": "progress.tick()\n",
    # A plain name: deleting it leaves a NameError further down, which reports
    # BROKE -- not an answer, and a whole suite run to learn that.
    "drop-assign": "plain = 3\n",
    # `ok=` is a required field, not an optional guarantee: dropping it is a
    # `TypeError`, which is BROKE rather than an answer.
    "drop-kwarg": 'Check(label="x", ok=True)\n',
    # Big enough that +-1 is arbitrary rather than a boundary.
    "off-by-one": "x = items[97]\n",
    # `return None` is already what the mutation would produce -- and `return
    # True` is left to `return-constant`, which swaps it. `None` is falsy, so
    # asking both would be very nearly the same question at twice the price.
    # With only the `return None` half, dropping the boolean exclusion survived.
    "return-value": "def f():\n    return None\n\n\ndef g():\n    return True\n",
    # String concatenation has no meaningful `-`.
    "arith": "x = a % b\n",
    # `x[:]` has no bound left to widen.
    "slice": "x = items[:]\n",
}


class TestWhatIsNeverMutated(unittest.TestCase):
    def test_a_docstring_is_left_alone(self) -> None:
        self.assertEqual(mutate('def f():\n    """Words."""\n'), [])

    def test_an_f_string_interior_yields_nothing_at_all(self) -> None:
        """Zero, and the assertion is deliberately not "a correct span".

        On 3.10 and 3.11 the nodes inside a `JoinedStr` carry the *enclosing
        string's* position, so a span computed from one splices bytes somewhere
        else in the file -- and the result usually still parses. The point is not
        that the span would be wrong; it is that it cannot be trusted at all.
        """
        self.assertEqual(mutate('x = f"{a < b}"\n'), [])

    def test_an_annotation_is_left_alone(self) -> None:
        """Every module here has `from __future__ import annotations`, so these
        are never evaluated: a guaranteed equivalent mutant."""
        # The literal inside the annotation must be untouched while the one in
        # the value is not -- a fixture that only had the annotation could not
        # tell "suppressed" from "the operator never fires here".
        rows = mutate("x: Literal[1] = 2\n", operators=["off-by-one"])
        self.assertEqual([row.old for row in rows], ["2", "2"])

    def test_a_type_checking_block_is_left_alone(self) -> None:
        self.assertEqual(mutate("if TYPE_CHECKING:\n    import x\n"), [])

    def test_the_main_guard_is_left_alone(self) -> None:
        self.assertEqual(mutate('if __name__ == "__main__":\n    go()\n'), [])

    def test_an_assert_is_left_alone(self) -> None:
        self.assertEqual(mutate("assert a is None\n"), [])

    def test_a_pragma_line_is_left_alone(self) -> None:
        self.assertEqual(mutate("x = a < b  # pragma: no mutate\n"), [])

    def test_a_pragma_covers_the_whole_construct_not_just_its_first_line(self) -> None:
        """The pragma reads as covering what it sits in, so it must.

        Checked by span rather than by start line. A multi-line `if` was mutated
        regardless while its author could see the pragma sitting there -- silent,
        and in the direction that reads as success, because the run then reports
        `SURVIVED` about code that had been declared out of scope.
        """
        source = (
            "def g(a, b):\n    if (a\n            and b):  # pragma: no mutate\n        return 1\n"
        )
        rows = mutants.generate(source, "w/t.py", {2, 3}, tests="t")
        self.assertEqual([row.label for row in rows], [])

    def test_a_form_feed_above_a_pragma_does_not_move_it(self) -> None:
        """The pragma is numbered the way `ast` numbers, not the way `str` does.

        `str.splitlines` breaks on form feed and the tokenizer does not, so this
        source put the suppression one line below the code it names: the marked
        comparison was mutated and the innocent line above it was skipped. The
        module already owned `line_starts` for exactly this; the pragma was the
        last place not using it. `dedent` eats a form feed, so no helper here.
        """
        source = "def f(a, b):\n    x = a < b\n\x0c\n    y = a < b  # pragma: no mutate\n"
        rows = mutants.generate(source, "w/t.py", {1, 2, 3, 4}, tests="t")
        self.assertEqual([row.label for row in rows], ["w/t.py:2 in f() -- `<` becomes `<=`"])

    def test_a_while_is_never_forced_true(self) -> None:
        """A hang is not a caught mutant: it burns a lane and the timeout to
        report `TIMEOUT`, which is not an answer."""
        self.assertEqual(mutate("while ready:\n    go()\n", operators=["branch"]), [])

    def test_only_the_changed_lines(self) -> None:
        body = "x = a < b\ny = c < d\n"
        self.assertEqual([row.label for row in mutate(body, lines={2})].pop().count(":2"), 1)
        self.assertEqual(len(mutate(body, lines={2})), 1)


class TestLineEndingsThatAreNotNewline(unittest.TestCase):
    """`\r\n` and a bare `\r` end a line for CPython, and so for every span here.

    `line_starts` was written for the form feed -- a character `str.splitlines`
    breaks on and the tokenizer does not -- and it handles the opposite case in
    the same loop: characters the tokenizer *does* break on. That half arrived
    with no fixture, and the tool said so about itself: of the 17 rows that
    survived `--base main --only tools/mutants.py`, 15 were in this branch, and
    the reason was not a subtle one. No source anywhere in the suite contained a
    `\r`, so `if source[at] == "\r"` was never once entered.

    The exactness matters for the same reason the form feed did. A step of 1
    across `\r\n` puts every span below it one character early, which is an edit
    that still parses, in a place the row's label does not name.
    """

    def starts_agree_with_ast(self, source: str) -> list[int]:
        """`ast` is the authority; this asserts against it rather than a constant.

        Written this way because the constant is what a reader checks by hand and
        gets wrong. `get_source_segment` slices with `ast`'s own line accounting,
        so if `line_starts` disagrees the segments come back shifted.
        """
        tree = ast.parse(source)
        for node in tree.body:
            self.assertIsNotNone(ast.get_source_segment(source, node))
        return mutants.line_starts(source)

    def test_crlf_counts_as_one_line_ending(self) -> None:
        source = "a = 1\r\nb = 2\r\n"
        # 7, not 6: the `\n` after the `\r` starts no line of its own.
        self.assertEqual(self.starts_agree_with_ast(source), [0, 7, 14])

    def test_a_bare_cr_ends_a_line_too(self) -> None:
        """The `else 1` arm. A lone `\r` is a terminator for CPython, not junk."""
        source = "a = 1\rb = 2\r"
        self.assertEqual(self.starts_agree_with_ast(source), [0, 6, 12])

    def test_the_three_endings_mixed(self) -> None:
        """One source reaching every arm, because real files are converted badly."""
        source = "a = 1\r\nb = 2\rc = 3\n"
        self.assertEqual(self.starts_agree_with_ast(source), [0, 7, 13, 19])

    def test_a_leading_newline_is_not_skipped(self) -> None:
        """`at` starts at 0, and index 0 is a line ending on a file that opens blank."""
        source = "\na = 1\n"
        self.assertEqual(self.starts_agree_with_ast(source), [0, 1, 7])

    def test_a_file_that_does_not_end_in_a_newline(self) -> None:
        """The last line has no start after it, so its end is the end of the file.

        Every other fixture in this file ends in `\n`, which is why the bound in
        `Offsets._line` survived mutation: `lineno < len(starts)` and
        `lineno <= len(starts)` only differ when the mutated node sits on the
        final line and nothing follows it. Git warns about such a file and
        happily stores one.
        """
        source = "first = 1\nx = a < b"
        self.assertEqual(mutants.line_starts(source), [0, 10])
        row = mutants.generate(source, "w/t.py", {2}, tests="t").pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], "a < b")

    def test_a_span_below_a_crlf_lands_on_its_own_text(self) -> None:
        """The end-to-end claim: one character early is a different edit.

        `\r\n` above the mutated line, and a comparison below it, so a step of 1
        shifts the span into the line ending rather than onto `a < b`.
        """
        source = "first = 1\r\nx = a < b\n"
        row = mutants.generate(source, "w/t.py", {2}, tests="t").pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], "a < b")
        self.assertEqual(row.old, "a < b")


class TestDroppingAKeywordArgument(unittest.TestCase):
    """The operator written for `mkdir(..., mode=0o700)`.

    Deliberately narrow. A blanket "drop any keyword" was measured first: of 281
    keyword arguments in `woswoar/`, the commonest are *required* parameters
    passed by name, and dropping one raises `TypeError` -- `BROKE` rather than
    an answer, at the price of a whole suite run. `_DROPPABLE` inverts the rule
    so that a keyword nobody argued for is never dropped.
    """

    def dropped(self, body: str) -> list[str]:
        rows = mutants.generate(body, "w/t.py", {1}, tests="t", operators=["drop-kwarg"])
        return [row.label.split(" -- ")[-1] for row in rows]

    def test_a_mode_is_droppable(self) -> None:
        """The case it exists for: the directory is still made, still returns,
        and every test that only asks whether the path exists still passes."""
        self.assertEqual(
            self.dropped("path.mkdir(parents=True, mode=0o700)\n"), ["`mode=448` is dropped"]
        )

    def test_check_true_is_droppable(self) -> None:
        self.assertEqual(
            self.dropped("subprocess.run(argv, check=True)\n"), ["`check=True` is dropped"]
        )

    def test_check_false_is_not(self) -> None:
        """It is `subprocess.run`'s own default, so removing it changes nothing
        and the row could only ever be an unkillable survivor."""
        self.assertEqual(self.dropped("subprocess.run(argv, check=False)\n"), [])

    def test_a_keyword_nobody_argued_for_is_left_alone(self) -> None:
        """`ok=` is a required `Check` field: dropping it is a `TypeError`."""
        self.assertEqual(self.dropped('Check(label="x", ok=True)\n'), [])

    def test_a_star_star_spread_is_not_a_keyword_to_drop(self) -> None:
        self.assertEqual(self.dropped("f(**options)\n"), [])

    def test_the_replacement_actually_omits_it(self) -> None:
        """The prose could be right while the edit is not."""
        rows = mutants.generate(
            "p.mkdir(mode=0o700, parents=True)\n",
            "w/t.py",
            {1},
            tests="t",
            operators=["drop-kwarg"],
        )
        self.assertNotIn("mode", rows[0].new)
        self.assertIn("parents", rows[0].new, "only the named keyword goes")

    def test_each_droppable_keyword_is_reachable(self) -> None:
        """Otherwise a name can sit in `_DROPPABLE` spelled wrongly for ever."""
        for name in sorted(mutants._DROPPABLE):
            with self.subTest(keyword=name):
                value = "True" if name in ("check", "exist_ok", "follow_symlinks") else "0"
                self.assertEqual(len(self.dropped(f"f(a, {name}={value})\n")), 1)

    def test_encoding_is_not_droppable(self) -> None:
        """It was, and the first whole-package sweep with this operator said so:
        41 unkillable rows against 3 caught in the entire package.

        Dropping `encoding="utf-8"` is a real defect -- under `LC_ALL=C` the same
        read raises `UnicodeDecodeError` -- but this suite runs in a UTF-8 locale
        everywhere, so `read_text()` and `read_text(encoding="utf-8")` are the
        same call almost everywhere in it. A row that can only ever survive is
        not a question worth asking every run.

        "Almost", since #229: `tests/test_locale.py` drives one round trip in a
        child that prefers ASCII, and the two `encoding=` arguments on that path
        -- the day file's append and `store.host_name`'s read -- are answerable
        now. The operator stays off because the other sites carry ids, keys and
        offsets, which are ASCII by construction: putting it back would restore
        dozens of unkillable rows to buy two killable ones.
        """
        self.assertEqual(self.dropped('p.read_text(encoding="utf-8")\n'), [])


class TestSkippingAnOperator(unittest.TestCase):
    """`--skip-operator`, the escape hatch a pragma cannot serve.

    A pragma suppresses a *line*; this suppresses a *kind*, which is what an
    equivalent mutant produced by one operator over and over needs.
    """

    def test_it_subtracts_from_the_default_set(self) -> None:
        every = mutants.generate("x = a < b\n", "w/t.py", {1}, tests="t")
        fewer = mutants.generate("x = a < b\n", "w/t.py", {1}, tests="t", skip=["boundary"])
        self.assertTrue(any(row.operator == "boundary" for row in every))
        self.assertFalse(any(row.operator == "boundary" for row in fewer))
        self.assertLess(len(fewer), len(every))

    def test_an_unknown_name_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            mutants.generate("x = a < b\n", "w/t.py", {1}, skip=["boundry"])
        self.assertIn("no such operator: boundry", str(refused.exception))

    def test_skipping_everything_is_refused_rather_than_silent(self) -> None:
        """An empty selection generates nothing and would exit 0 -- a run that
        asked nothing reading as a run that found nothing."""
        with self.assertRaises(SystemExit) as refused:
            mutants.generate(
                "x = a < b\n", "w/t.py", {1}, operators=["boundary"], skip=["boundary"]
            )
        self.assertIn("every operator was skipped", str(refused.exception))


class TestEveryLine(unittest.TestCase):
    """What `--all` means, enumerated rather than diffed against the first commit."""

    def test_it_finds_the_mutable_files_and_all_their_lines(self) -> None:
        found = mutants.every_line(REPO_ROOT)
        self.assertIn("tools/mutants.py", found)
        self.assertIn("woswoar/sync.py", found)
        body = (REPO_ROOT / "woswoar/sync.py").read_text(encoding="utf-8").splitlines()
        self.assertEqual(max(found["woswoar/sync.py"]), len(body))
        self.assertEqual(min(found["woswoar/sync.py"]), 1)

    def test_it_holds_nothing_that_is_not_mutable(self) -> None:
        for path in mutants.every_line(REPO_ROOT):
            self.assertTrue(mutants.mutable(path), path)

    def test_tests_are_not_swept(self) -> None:
        self.assertFalse(any(p.startswith("tests/") for p in mutants.every_line(REPO_ROOT)))


class TestChoosingOperators(unittest.TestCase):
    """A run that asked nothing must not read as a run that found nothing."""

    def test_an_unknown_operator_is_refused(self) -> None:
        """`--operator boundry` used to generate zero rows and exit 0.

        Which is indistinguishable, in a pull request, from a change nothing
        could mutate -- the same failure `--limit` avoids by saying out loud how
        many rows it dropped.
        """
        with self.assertRaises(SystemExit) as refused:
            mutants.generate("x = a < b\n", "w/t.py", {1}, operators=["boundry"])
        self.assertIn("no such operator: boundry", str(refused.exception))
        self.assertIn("boundary", str(refused.exception))

    def test_a_known_operator_still_selects(self) -> None:
        rows = mutants.generate("x = a < b\n", "w/t.py", {1}, operators=["boundary"])
        self.assertEqual([row.label for row in rows], ["w/t.py:1 -- `<` becomes `<=`"])


class TestTheLabelNamesAScope(unittest.TestCase):
    """`in C()` named a call that does not exist, on the string a reviewer reads."""

    def test_a_class_body_is_not_written_as_a_call(self) -> None:
        rows = mutants.generate("class C:\n    LIMIT = 1\n", "w/t.py", {2}, tests="t")
        self.assertEqual(rows[0].label, "w/t.py:2 in C -- `1` becomes `2`")

    def test_a_method_still_is(self) -> None:
        source = "class C:\n    def m(self, a, b):\n        return a < b\n"
        rows = mutants.generate(source, "w/t.py", {3}, tests="t")
        self.assertEqual(rows[0].label, "w/t.py:3 in C.m() -- `<` becomes `<=`")

    def test_a_function_in_a_function_is_not_written_as_two_calls(self) -> None:
        """`outer().inner()` is what baking the parentheses into the name gives."""
        source = "def outer():\n    def inner(a, b):\n        return a < b\n    return inner\n"
        rows = mutants.generate(source, "w/t.py", {3}, tests="t")
        self.assertEqual(rows[0].label, "w/t.py:3 in outer.inner() -- `<` becomes `<=`")


class TestTheSpanIsExact(unittest.TestCase):
    def test_the_span_holds_the_text_the_row_quotes(self) -> None:
        source = "x = a < b\n"
        row = mutate(source).pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], row.old)
        self.assertEqual(row.old, "a < b")

    def test_a_line_with_non_ascii_before_it(self) -> None:
        """`col_offset` is a UTF-8 *byte* offset; `str` slicing is by character.

        `woswoar/report.py` and `woswoar/archive.py` really do contain `·`, `✔`
        and `²`, so this is the live case and not a hypothetical. Every fixture
        written in plain English passes under either arithmetic, which is what
        makes the bug invisible without this test.
        """
        # On the *same line*, before the column. A fixture with the non-ASCII on
        # an earlier line passes under either arithmetic -- the line starts are
        # counted in characters and are right either way -- so the first version
        # of this test proved nothing, and the mutation that removes the
        # conversion survived it.
        source = 'x = "✔ ✘ ²" if a < b else "·"\n'
        row = mutate(source, lines={1}).pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], "a < b")
        self.assertEqual(row.old, "a < b")

    def test_a_span_on_a_later_line(self) -> None:
        """`lineno` indexes a list, and a one-line fixture cannot see it slip.

        `_lines[lineno - 1]` against `_lines[lineno - 2]` picks the same string
        whenever the file has one line, and the same *offset* whenever every
        earlier line is the same length. Both mutations survived a fixture set
        that had drifted to single-line sources -- including this class's own
        UTF-8 case, which had to become one line to test the byte conversion.
        So: three lines, deliberately of different lengths, mutating the third.

        The line *above* the mutated one carries a multi-byte character inside
        the first four bytes, which is the only arrangement that can tell the two
        apart: `line.encode()[:col]` gives four characters on any ASCII line, so
        a slip onto a neighbouring plain-English line is invisible.
        """
        source = "a = 1\n# ·x\nx = a < b\n"
        row = mutate(source, lines={3}).pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], "a < b")
        self.assertEqual(source.index("a < b", source.index("x =")), start)

    def test_a_form_feed_does_not_shift_the_lines_below_it(self) -> None:
        """`str.splitlines` and the tokenizer disagree, and only one is right.

        `splitlines` breaks on form feed, vertical tab, the file/group/record
        separators and U+2028/9; CPython treats a form feed as whitespace. A
        single `\\f` -- the Emacs page separator, and legal Python -- made
        `splitlines` see seven lines where `ast` saw six, so every span below it
        landed one line early. The mutant then edited `limit` on line 4 while its
        label named `return True` on line 5, and the result still parsed.

        The span assertion cannot be `source[start:end] == row.old`: `generate`
        derives `old` from the span, so that identity holds however wrong the
        offsets are. It has to be the text this test names independently.
        """
        source = (
            "def f(values, limit):\n"
            "    total = sum(values)\n"
            "\x0c\n"
            "    if total < limit:\n"
            "        return True\n"
            "    return False\n"
        )
        # Not through `mutate`: `textwrap.dedent` counts `\x0c` as whitespace and
        # rewrites the line, so the helper would hand the generator a different
        # string from the one asserted against here.
        rows = mutants.generate(source, "woswoar/thing.py", {5}, operators=["return-constant"])
        self.assertEqual(len(rows), 1)
        start, end = rows[0].span or (0, 0)
        self.assertEqual(source[start:end], "True")
        self.assertEqual(start, source.index("True"))

    def test_a_repeated_line_gets_distinct_spans(self) -> None:
        """The reason spans exist: `str.replace` cannot tell these apart."""
        source = (
            "def f():\n    if not ready:\n        return 1\n\n\n"
            "def g():\n    if not ready:\n        return 2\n"
        )
        rows = mutate(source, operators=["drop-not"])
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].span, rows[1].span)
        for row in rows:
            start, end = row.span or (0, 0)
            self.assertEqual(source[start:end], row.old)


class TestTheLabel(unittest.TestCase):
    def test_it_names_the_place_and_the_change(self) -> None:
        # Pinned to one operator: `return-value` also fires on this line, and a
        # `.pop()` off an unpinned list asserts whichever sorted last.
        row = mutate(
            "class C:\n    def m(self):\n        return a < b\n", operators=["boundary"]
        ).pop()
        self.assertEqual(row.label, "woswoar/thing.py:3 in C.m() -- `<` becomes `<=`")

    def test_at_module_scope_there_is_no_function(self) -> None:
        self.assertEqual(
            mutate("x = a < b\n").pop().label, "woswoar/thing.py:1 -- `<` becomes `<=`"
        )


class TestTheCap(unittest.TestCase):
    def rows(self, path: str, count: int) -> list[Mutation]:
        return [Mutation(f"{path}:{n}", path, "a", "b", "t", span=(n, n + 1)) for n in range(count)]

    def test_it_spreads_across_files_rather_than_taking_a_prefix(self) -> None:
        """`[:limit]` would cover the first file exhaustively and the largest not
        at all -- and the printed count would look right either way."""
        table = self.rows("a.py", 10) + self.rows("z.py", 10)
        kept, dropped = mutants.cap(table, 6)
        self.assertEqual(len(kept), 6)
        self.assertEqual({row.path for row in kept}, {"a.py", "z.py"})
        self.assertEqual(len(dropped), 14)

    def test_everything_is_accounted_for(self) -> None:
        table = self.rows("a.py", 5) + self.rows("z.py", 7)
        kept, dropped = mutants.cap(table, 4)
        self.assertEqual(len(kept) + len(dropped), len(table))

    def test_no_cap_keeps_everything(self) -> None:
        table = self.rows("a.py", 5)
        self.assertEqual(mutants.cap(table, 0), (table, []))


class TestThePackageInitIsIndexedAsThePackage(unittest.TestCase):
    """`tests/__init__.py` is `tests`, not `tests.__init__`, which nothing imports.

    Driven against a built tree rather than the real one, and that is the point:
    the repository's own `tests/__init__.py` is empty, so against the real tree
    both spellings give the same answer and a mutation reverting this survives
    for want of anything to distinguish them. `module_of` already carries this
    exact special case for `woswoar/__init__.py`, argued at length; this is the
    other half of the same mapping, and the failure it prevents is silent --
    anything ever added to `tests/__init__.py` drops out of the closure.
    """

    def test_a_helper_in_the_package_init_still_carries_its_imports(self) -> None:
        with tempfile.TemporaryDirectory() as box:
            root = Path(box)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text(
                "from woswoar import store\n", encoding="utf-8"
            )
            # `from . import helper` is what a test module writes, and it is the
            # package -- `tests` -- that the import index has to be keyed on.
            (root / "tests" / "test_a.py").write_text("from . import helper\n", encoding="utf-8")
            index = mutants.importers(root)
        self.assertEqual(index.get("woswoar.store"), {"tests.test_a"})


class TestChoosingTheTests(unittest.TestCase):
    """Driven against the real repository, because that is the map it describes."""

    index: dict[str, set[str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.index = mutants.importers(REPO_ROOT)

    def targets(self, path: str) -> set[str]:
        return set(mutants.targets_for(path, REPO_ROOT, self.index).split())

    def test_the_name_match(self) -> None:
        self.assertIn("tests.test_sync", self.targets("woswoar/sync.py"))

    def test_siblings_of_the_name_match(self) -> None:
        self.assertLessEqual(
            {"tests.test_importer", "tests.test_importer_atuin"},
            self.targets("woswoar/importer.py"),
        )

    def test_a_module_with_no_test_of_its_own_is_found_by_import(self) -> None:
        """`woswoar/gitrepo.py` has no `test_gitrepo.py`; `tests/test_sync.py`
        imports it. A name heuristic alone would report it as untested."""
        self.assertIn("tests.test_sync", self.targets("woswoar/gitrepo.py"))

    def test_the_name_match_does_not_short_circuit_the_index(self) -> None:
        """Measured on #216: taking `test_install` alone because the name matched
        reported nine mutants as survivors that `tests.test_doctor` catches."""
        found = self.targets("woswoar/install.py")
        self.assertIn("tests.test_install", found)
        self.assertIn("tests.test_doctor", found)

    def test_a_helper_carries_its_imports_to_the_tests_that_use_it(self) -> None:
        """Through `tests/support.py`, which every suite imports relatively.

        `woswoar/crypto.py` is the case that tells the two answers apart:
        `tests/test_deps.py` imports `support` and nothing else that reaches
        `crypto`, so it appears here only if the closure runs. An assertion about
        `store` cannot -- `tests/test_sync.py` imports that directly, so it was
        satisfied whether or not the helper mechanism worked at all, which is how
        the mechanism came to be inert without any test noticing.
        """
        self.assertIn("tests.test_deps", self.targets("woswoar/crypto.py"))

    def test_every_mutable_file_resolves_or_says_it_cannot(self) -> None:
        """The completeness pass. An empty answer is allowed -- it means "run
        everything" -- but it must be a small, named set, or the selection has
        quietly stopped working and every row would cost a whole suite."""
        homeless = set()
        for source in sorted(REPO_ROOT.glob("woswoar/**/*.py")):
            path = source.relative_to(REPO_ROOT).as_posix()
            if mutants.mutable(path) and not self.targets(path):
                homeless.add(path)
        self.assertEqual(homeless, set(), "these no longer resolve to any test module")


@requires_git
class TestReadingARealDiff(unittest.TestCase):
    """A real `git init`, because that is what the repository does elsewhere.

    `tests/test_sync.py` drives real `git` and real `age`; `tests/test_shell_hook.py`
    drives a real `bash`. Asserting on a canned diff string is already covered
    above; this is about the argv.

    `requires_git` from `tests/support.py` rather than a local `shutil.which`,
    for the reason stated where it is defined: a check that has to grow later
    must not be updatable in one file and forgotten in the other. Without it
    `check=True` turns "this machine has no git" into an error rather than a
    skip. The cost is real and worth naming -- importing `support` enrols this
    module in the import index for everything `support` pulls in, so a mutation
    in `store` or `crypto` now also runs this file. That is 0.13 s a row, and
    these tests are the cheapest in the suite.
    """

    def git(self, *argv: str) -> str:
        """`support.git`, bound to this fixture's root. The hardening it carries
        was written here and moved there when a second fixture needed it."""
        return support.git(self.root, *argv)

    def write(self, name: str, body: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

        self.git("init", "--initial-branch=main", "-q")
        self.write("woswoar/thing.py", "one = 1\ntwo = 2\nthree = 3\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def test_it_sees_the_branch_and_the_working_tree_but_not_the_base(self) -> None:
        """The single most testable decision here: `--merge-base`, not two dots.

        A two-dot diff would include whatever landed on `main` after this branch
        started and generate mutants for it, labelled as if they belonged to the
        change under review.
        """
        self.git("checkout", "-qb", "work")
        self.write("woswoar/thing.py", "one = 1\ntwo = 22\nthree = 3\n")
        self.git("commit", "-qam", "on the branch")

        self.git("checkout", "-q", "main")
        self.write("woswoar/later.py", "later = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "landed on main afterwards")

        self.git("checkout", "-q", "work")
        # Uncommitted on top, which is the situation the tool is used in.
        self.write("woswoar/thing.py", "one = 1\ntwo = 22\nthree = 33\n")

        found = mutants.changed_lines("main", self.root)
        self.assertEqual(found, {"woswoar/thing.py": {2, 3}})
        self.assertNotIn("woswoar/later.py", found)

    def test_an_untracked_file_counts_entirely(self) -> None:
        """`git diff` cannot see it, and "no mutants for the new module" reads
        exactly like "the new module is covered"."""
        self.write("woswoar/fresh.py", "a = 1\nb = 2\n")
        self.assertEqual(mutants.changed_lines("main", self.root)["woswoar/fresh.py"], {1, 2})

    def test_a_change_outside_the_mutable_paths_is_ignored(self) -> None:
        self.write("tests/test_thing.py", "x = 1\n")
        self.write("docs/notes.md", "words\n")
        self.assertEqual(mutants.changed_lines("main", self.root), {})


if __name__ == "__main__":
    unittest.main()
