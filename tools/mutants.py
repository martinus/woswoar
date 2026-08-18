"""What to change, and where the diff says it matters. `tools/mutate.py` runs it.

A hand-written table only asks the questions its author thought to ask, and
writing it is the work that remains after #48 removed the loop around it. This
reads `git diff`, generates mutants for the lines the change actually touched,
and hands them to the runner. The author stops writing the table and starts
reading the survivors.

Split from `mutate` along the line `tools/sandbox.py` and `compare.verdict`
already drew: everything here is pure -- `(source, changed lines) -> mutations`
-- so it is testable in milliseconds with no sandbox, no subprocess and no
suite. The one exception is `changed_lines`, which forks `git` and is the only
thing here that touches the world.

**Spans.** A generated mutant is routinely not unique in its file: `if not
path.exists():` appears many times. So a row carries the character offsets it
applies at, and `check` asserts the text *at* those offsets rather than counting
occurrences.

Be clear about what that assertion can and cannot do. `generate` derives `old`
*from* the span it just computed, so for a freshly generated row the check is
true by construction: it catches the file changing between `--list` and the run,
and nothing else. It is **not** a guard against a bad offset. Only tests are,
and there are two traps for them to cover -- `col_offset` is a UTF-8 *byte*
offset into its line (`woswoar/report.py` and `woswoar/archive.py` really do
contain `·`, `✔`, `✘` and `²`), and lines must be counted the way the tokenizer
counts them rather than the way `str.splitlines` does. See `line_starts`.

**What no operator here can generate.** `CONTRIBUTING.md` lists four weak
fixtures that shipped; three are reachable (a reversed walk, a suffix filter
that accepts everything, an `assertFalse` that cannot tell `False` from `None`)
and the fourth is not. "A `source` line searched in the whole rc file instead of
the block" is a *scope widening*, and there is no local AST rewrite that
produces it. That class stays hand-written, which is why the spec-file mode is
not going away -- and why "every generated mutant was caught" must not be read
as "the tests are good".
"""

from __future__ import annotations

import ast
import copy
import re
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import NamedTuple


class Mutation(NamedTuple):
    """One edit that some test is supposed to notice."""

    #: What the mutation does, in the words of whoever might reintroduce it.
    #: Printed, so make it a sentence a reader of the PR would understand.
    label: str
    path: str
    #: For a hand-written row, must appear exactly once in the file: ambiguity is
    #: an error rather than a guess, because replacing the wrong one of two
    #: matches quietly tests nothing. For a generated row `span` pins it instead.
    old: str
    new: str
    #: Whitespace-separated unittest targets, as `python -m unittest` takes them.
    tests: str
    #: Say so when the replacement is meant to *contain* the original -- an
    #: inserted call, an early return in front of code that stays. Otherwise
    #: `check` refuses that shape, because it is overwhelmingly a mistake.
    additive: bool = False
    #: Character offsets, when the text to replace is not unique. `old` must equal
    #: `text[span[0]:span[1]]`, which is a stronger claim than "appears once".
    span: tuple[int, int] | None = None
    #: Which operator produced this. Empty for a hand-written row.
    operator: str = ""


#: Nodes that carry a source position. `ast.AST` does not -- only statements and
#: expressions do -- and every span in this module comes from one of these.
Positioned = ast.expr | ast.stmt


class Edit(NamedTuple):
    """One operator's answer about one node."""

    node: Positioned
    new: str
    #: The clause that goes into the printed label, after `-- `.
    prose: str


#: A line carrying this is left alone. `# pragma: no cover` means something else
#: and is deliberately not honoured: two of its four uses in `woswoar/` mark code
#: that needs a hung subprocess to reach, which is exactly the kind of guard
#: worth mutating if you ever do reach it.
NO_MUTATE = re.compile(r"#\s*pragma:\s*no\s+mutate\b")

#: Only these are worth mutating. Never `tests/**`: breaking a test proves
#: nothing about the fix, and the run would report the assertion it removed.
MUTABLE = ("woswoar/", "tools/")


def mutable(path: str) -> bool:
    return path.endswith(".py") and path.startswith(MUTABLE) and not path.startswith("tests/")


def line_starts(source: str) -> list[int]:
    """Where each line begins, counting lines the way the *tokenizer* does.

    Deliberately not `str.splitlines`, and this is the second trap in this
    module rather than a preference. `str.splitlines` also breaks on form feed,
    vertical tab, the file/group/record separators and U+2028/9; CPython's
    tokenizer treats a form feed as ordinary whitespace. A single `\f` -- the
    Emacs page separator, and legal Python -- therefore shifts every line number
    below it by one for `splitlines` while `ast` keeps counting from the real
    one, so every span below it lands on the wrong line.

    That produces the worst outcome this module has: an edit that still parses,
    in a different place from the one the row's label names, reported as `caught`
    or `SURVIVED` about a line nobody touched.
    """
    starts = [0]
    at = 0
    while at < len(source):
        if source[at] == "\r":
            at += 2 if source[at + 1 : at + 2] == "\n" else 1
            starts.append(at)
        elif source[at] == "\n":
            at += 1
            starts.append(at)
        else:
            at += 1
    return starts


class Offsets:
    """Character offsets for the (line, column) pairs `ast` reports.

    Exists for one reason, and it is not convenience. `col_offset` is a **UTF-8
    byte** offset into its line; `str` indexing is by character. Every line up to
    the first non-ASCII character in a file behaves identically under both, so
    the bug this prevents is invisible in any fixture written in plain English
    and appears only on the files that print things.
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._starts = line_starts(source)

    def _line(self, lineno: int) -> str:
        start = self._starts[lineno - 1]
        end = self._starts[lineno] if lineno < len(self._starts) else len(self._source)
        return self._source[start:end]

    def at(self, lineno: int, col: int) -> int:
        line = self._line(lineno)
        return self._starts[lineno - 1] + len(line.encode("utf-8")[:col].decode("utf-8"))

    def span(self, node: Positioned) -> tuple[int, int]:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return (
            self.at(node.lineno, node.col_offset),
            self.at(node.end_lineno, node.end_col_offset),
        )


def _rewritten(node: ast.AST, change: Callable[[ast.AST], None]) -> str:
    """`node` with `change` applied to a copy, unparsed and parenthesised.

    Parenthesised without exception, which removes every precedence question in
    one rule: a node's own span never includes surrounding parentheses, so a
    parenthesised replacement is legal in every position these operators fire in.
    Unparsing the *copy* rather than the module is what keeps the other ten
    thousand lines of the file byte-identical -- a reformatted sandbox would make
    the `additive` check meaningless and a survivor unreadable.
    """
    clone = copy.deepcopy(node)
    change(clone)
    return f"({ast.unparse(clone)})"


#: Comparison flips, split into two operators because they answer different
#: questions. `boundary` is the off-by-one -- the class a two-item fixture
#: cannot see, and the reason `CONTRIBUTING.md` leads its weak-fixture list with
#: "two day directories cannot distinguish a sorted walk from a reversed one".
_STRICTNESS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}
#: `negate` is the inverted guard. It earns its place on #206: `Check.ok`
#: defaulted to `None`, and `assertFalse(status.ok)` passes for `None` too, so
#: nothing could tell the two answers apart.
_OPPOSITE: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


def _spell(op: ast.cmpop) -> str:
    return {
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Is: "is",
        ast.IsNot: "is not",
        ast.In: "in",
        ast.NotIn: "not in",
    }[type(op)]


def _compare(node: ast.AST, table: dict[type[ast.cmpop], type[ast.cmpop]]) -> Iterator[Edit]:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return
    was = node.ops[0]
    becomes = table.get(type(was))
    if becomes is None:
        return

    def flip(clone: ast.AST) -> None:
        assert isinstance(clone, ast.Compare)
        clone.ops = [becomes()]

    yield Edit(node, _rewritten(node, flip), f"`{_spell(was)}` becomes `{_spell(becomes())}`")


def boundary(node: ast.AST) -> Iterator[Edit]:
    yield from _compare(node, _STRICTNESS)


def negate(node: ast.AST) -> Iterator[Edit]:
    yield from _compare(node, _OPPOSITE)


def connector(node: ast.AST) -> Iterator[Edit]:
    """`and` <-> `or`: the compound guard where only one half is ever exercised."""
    if not isinstance(node, ast.BoolOp):
        return
    becomes = ast.Or if isinstance(node.op, ast.And) else ast.And
    was, now = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")

    def flip(clone: ast.AST) -> None:
        assert isinstance(clone, ast.BoolOp)
        clone.op = becomes()

    yield Edit(node, _rewritten(node, flip), f"`{was}` becomes `{now}`")


def branch(node: ast.AST) -> Iterator[Edit]:
    """The mutation the hand-written tables here already write, now free.

    `while` is deliberately not included. Forcing a loop condition true is a hang,
    and a hang is not a caught mutant -- it burns a lane and 300 seconds of the
    budget to report `TIMEOUT`, which is not an answer.
    """
    if not isinstance(node, ast.If):
        return
    if isinstance(node.test, ast.Constant):
        # Already `if True:`. Forcing it to `True` changes nothing, and a row
        # that changes nothing reports `caught` or `SURVIVED` about nothing.
        return
    yield Edit(node.test, "True", "the `if` is always taken")
    yield Edit(node.test, "False", "the `if` is never taken")


def drop_not(node: ast.AST) -> Iterator[Edit]:
    if not isinstance(node, ast.UnaryOp) or not isinstance(node.op, ast.Not):
        return
    yield Edit(node, f"({ast.unparse(node.operand)})", "the `not` is dropped")


def order(node: ast.AST) -> Iterator[Edit]:
    """Ordering, and the weak fixture `CONTRIBUTING.md` names first.

    "Two day directories cannot distinguish a sorted walk from a reversed one" --
    so this generates the reversed walk and lets the fixture prove it can or
    cannot tell. `sorted(x)` -> `list(x)` is the weaker sibling: it keeps the
    elements and drops only the guarantee.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return
    name = node.func.id
    if name == "sorted" and not any(word.arg == "reverse" for word in node.keywords):

        def backwards(clone: ast.AST) -> None:
            assert isinstance(clone, ast.Call)
            clone.keywords = [*clone.keywords, ast.keyword(arg="reverse", value=ast.Constant(True))]

        yield Edit(node, _rewritten(node, backwards), "the ordering is reversed")

        def unordered(clone: ast.AST) -> None:
            assert isinstance(clone, ast.Call)
            clone.func = ast.Name(id="list", ctx=ast.Load())
            clone.keywords = []

        yield Edit(node, _rewritten(node, unordered), "`sorted` becomes `list`")
    swaps = {"min": "max", "max": "min", "any": "all", "all": "any", "reversed": "list"}
    if name in swaps:

        def renamed(clone: ast.AST, to: str = swaps[name]) -> None:
            assert isinstance(clone, ast.Call)
            clone.func = ast.Name(id=to, ctx=ast.Load())

        yield Edit(node, _rewritten(node, renamed), f"`{name}` becomes `{swaps[name]}`")


def affix(node: ast.AST) -> Iterator[Edit]:
    """`x.endswith(s)` -> `True`, and the reason it is `True` and not `False`.

    The weak fixture is "a directory holding only `.age` files cannot test a
    suffix filter". A filter that accepts *everything* is precisely the answer
    such a directory cannot distinguish from a filter that works; one that
    accepts nothing empties the result and any assertion notices.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return
    if node.func.attr not in ("startswith", "endswith"):
        return
    yield Edit(node, "True", f"the `.{node.func.attr}(...)` filter accepts everything")


def return_constant(node: ast.AST) -> Iterator[Edit]:
    """Predicates whose tests only ever assert the happy direction."""
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Constant):
        return
    was = node.value.value
    if was is True or was is False:
        yield Edit(node.value, str(not was), f"returns `{not was}` instead of `{was}`")


def drop_call(node: ast.AST) -> Iterator[Edit]:
    """A call whose value is discarded, deleted.

    The "the write never happened" class. This product writes files and forks
    `git`, so a test that cannot notice a missing side effect is the expensive
    kind of decoration. `pass` rather than deletion because the span is one
    statement and an empty body is a syntax error.
    """
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return
    called = node.value.func
    shown = ast.unparse(called)
    if shown.split(".")[0] in ("logging", "progress", "log"):
        # The meter is explicitly never the thing being compared -- see
        # `tools/sandbox.py`'s argument for pinning it out of a comparison.
        return
    # `print` is *not* excluded, though every off-the-shelf tool excludes it by
    # default. Here the CLI's printed output is the product, and
    # `tests/test_report.py`, `test_doctor.py` and `test_cli_help.py` assert on it.
    yield Edit(node, "pass", f"the call to `{shown}(...)` never happens")


def off_by_one(node: ast.AST) -> Iterator[Edit]:
    """Small integer literals, moved by one. Index and threshold arithmetic."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, int):
        return
    if isinstance(node.value, bool) or abs(node.value) > 2:
        return
    for now in (node.value + 1, node.value - 1):
        yield Edit(node, repr(now), f"`{node.value}` becomes `{now}`")


def _restated(node: Positioned, change: Callable[[ast.AST], None]) -> str:
    """Like `_rewritten`, but for a *statement*, which cannot be parenthesised.

    `ast.unparse` emits no leading indentation and a statement's span begins
    after its indent, so splicing at that offset lines up.
    """
    clone = copy.deepcopy(node)
    change(clone)
    return ast.unparse(clone)


def return_value(node: ast.AST) -> Iterator[Edit]:
    """`return X` -> `return None`: the function that silently returns nothing.

    Always a real answer, never a `BROKE`: no name goes missing and nothing
    raises, so the suite either notices or does not. That is what makes this the
    cheapest operator here per row.

    Booleans are left to `return-constant`, which swaps them -- `None` is falsy,
    so turning `return True` into `return None` asks very nearly the same
    question twice and costs a second suite run to do it.
    """
    if not isinstance(node, ast.Return) or node.value is None:
        return
    if isinstance(node.value, ast.Constant) and (
        node.value.value is None or isinstance(node.value.value, bool)
    ):
        return
    shown = ast.unparse(node.value)
    trimmed = shown if len(shown) <= 30 else shown[:29] + "\N{HORIZONTAL ELLIPSIS}"
    yield Edit(node.value, "None", f"returns `None` instead of `{trimmed}`")


#: Arithmetic that a fixture with one small number cannot tell apart from itself.
_ARITH: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
}
_ARITH_SPELLING = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//"}


def arith(node: ast.AST) -> Iterator[Edit]:
    if isinstance(node, ast.BinOp):
        becomes = _ARITH.get(type(node.op))
        if becomes is None:
            return

        # The instance is built here and bound as a default: mypy's narrowing
        # from the `is None` check above does not follow into a nested function.
        swapped = becomes()

        def flip(clone: ast.AST, to: ast.operator = swapped) -> None:
            assert isinstance(clone, ast.BinOp)
            clone.op = to

        was, now = _ARITH_SPELLING[type(node.op)], _ARITH_SPELLING[becomes]
        yield Edit(node, _rewritten(node, flip), f"`{was}` becomes `{now}`")
        return
    if isinstance(node, ast.AugAssign):
        becomes = _ARITH.get(type(node.op))
        if becomes is None:
            return

        swapped = becomes()

        def flip_aug(clone: ast.AST, to: ast.operator = swapped) -> None:
            assert isinstance(clone, ast.AugAssign)
            clone.op = to

        was, now = _ARITH_SPELLING[type(node.op)], _ARITH_SPELLING[becomes]
        yield Edit(node, _restated(node, flip_aug), f"`{was}=` becomes `{now}=`")


def slice_widened(node: ast.AST) -> Iterator[Edit]:
    """`x[1:]`, `x[:-1]`, `x[a:b]` -> `x[:]`: the bound stops bounding.

    One replacement rather than one per end, because the fixture that can tell
    `x[1:]` from `x[:]` can almost always tell it from `x[2:]` too, and two rows
    per slice doubles the cost of the commonest shape in `cache.py`.
    """
    if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
        return
    cut = node.slice
    if cut.lower is None and cut.upper is None and cut.step is None:
        return

    def widen(clone: ast.AST) -> None:
        assert isinstance(clone, ast.Subscript)
        clone.slice = ast.Slice(lower=None, upper=None, step=None)

    yield Edit(node, _rewritten(node, widen), "the slice takes everything")


def drop_assign(node: ast.AST) -> Iterator[Edit]:
    """`self.x = ...` and `d[k] = ...`, deleted. The field that is never set.

    Attribute and subscript targets **only**, and the restriction is the whole
    design. Deleting `x = f()` leaves a `NameError` further down, which reports
    `BROKE` -- not an answer, and a full suite run to find that out. Assigning
    through an attribute or a key cannot make a name disappear, so every row here
    produces a real verdict.
    """
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    else:
        return
    if len(targets) != 1 or not isinstance(targets[0], (ast.Attribute, ast.Subscript)):
        return
    yield Edit(node, "pass", f"`{ast.unparse(targets[0])}` is never assigned")


class Operator(NamedTuple):
    name: str
    fire: Callable[[ast.AST], Iterator[Edit]]


OPERATORS: tuple[Operator, ...] = (
    Operator("boundary", boundary),
    Operator("negate", negate),
    Operator("connector", connector),
    Operator("branch", branch),
    Operator("drop-not", drop_not),
    Operator("order", order),
    Operator("affix", affix),
    Operator("return-constant", return_constant),
    Operator("drop-call", drop_call),
    Operator("drop-assign", drop_assign),
    Operator("off-by-one", off_by_one),
    Operator("return-value", return_value),
    Operator("arith", arith),
    Operator("slice", slice_widened),
)


# --- what may be mutated at all -------------------------------------------

#: Whole subtrees no operator sees.
#:
#: `JoinedStr` is the one that would produce a *wrong* answer rather than a
#: useless one: on 3.10 and 3.11 the nodes inside an f-string carry the enclosing
#: string's position, so a span computed from them splices the wrong bytes into
#: a file that then still parses. `requires-python` is `>=3.10`.
#:
#: The rest are equivalent mutants by construction. `assert` in `tools/` is
#: mostly mypy narrowing; mutating an import produces a `BROKE` row, which is not
#: an answer and costs a full suite run to find out.
_SKIP_SUBTREE = (ast.JoinedStr, ast.Assert, ast.Import, ast.ImportFrom)

#: Fields not descended into. Annotations are never evaluated -- every module
#: here has `from __future__ import annotations` -- so a mutant inside one cannot
#: change behaviour, and `mypy --strict` already holds them.
_SKIP_FIELDS: dict[type, frozenset[str]] = {
    ast.AnnAssign: frozenset({"annotation"}),
    ast.FunctionDef: frozenset({"returns", "decorator_list"}),
    ast.AsyncFunctionDef: frozenset({"returns", "decorator_list"}),
    ast.ClassDef: frozenset({"decorator_list"}),
    ast.arg: frozenset({"annotation"}),
}


def _is_guard(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:` and `if __name__ == "__main__":`, neither worth asking about."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def _walk(node: ast.AST, where: str = "") -> Iterator[tuple[ast.AST, str]]:
    """Every node an operator may be asked about, with its enclosing qualname.

    Docstrings need no suppression and used to have one. No operator matches a
    bare `Expr(Constant)`, so the machinery that skipped them changed no output;
    a mutation removing it survived, and the honest reading was that the code was
    redundant rather than the fixture weak.
    """
    if isinstance(node, _SKIP_SUBTREE):
        return
    if isinstance(node, ast.If) and _is_guard(node.test):
        return
    yield node, where
    ignored = _SKIP_FIELDS.get(type(node), frozenset())
    for field, value in ast.iter_fields(node):
        if field in ignored:
            continue
        for child in value if isinstance(value, list) else [value]:
            if not isinstance(child, ast.AST):
                continue
            deeper = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                deeper = f"{where}.{child.name}" if where else child.name
            yield from _walk(child, deeper)


def _touches(node: ast.AST, lines: set[int]) -> bool:
    """Does any line of `node` fall inside the diff?

    *Any*, not *all*. A single edited line inside a wrapped call means the
    enclosing statement starts above the hunk and ends below it, and requiring
    full containment would silently skip the commonest real edit there is.
    """
    start = getattr(node, "lineno", None)
    if start is None:
        return False
    end = getattr(node, "end_lineno", None) or start
    return any(line in lines for line in range(start, end + 1))


def _pragma_lines(source: str) -> set[int]:
    return {
        number for number, line in enumerate(source.splitlines(), start=1) if NO_MUTATE.search(line)
    }


def label(path: str, line: int, where: str, prose: str) -> str:
    """One line, and it is the product.

    A generated row still has to read as a sentence in a pull request, because
    that is what the reader of the PR gets. `--list` exists so the mechanical
    version can be edited into a better one before it is run.
    """
    place = f"{path}:{line}"
    return f"{place} in {where}() -- {prose}" if where else f"{place} -- {prose}"


def generate(
    source: str,
    path: str,
    lines: set[int],
    tests: str = "",
    operators: Sequence[str] | None = None,
) -> list[Mutation]:
    """Every mutant the operators can make on the changed lines of one file."""
    tree = ast.parse(source)
    offsets = Offsets(source)
    blocked = _pragma_lines(source)
    wanted = {name for name in (operators or [op.name for op in OPERATORS])}

    seen: set[tuple[tuple[int, int], str]] = set()
    out: list[Mutation] = []
    for node, where in _walk(tree):
        if not _touches(node, lines):
            continue
        for operator in OPERATORS:
            if operator.name not in wanted:
                continue
            for edit in operator.fire(node):
                if edit.node.lineno in blocked:
                    continue
                span = offsets.span(edit.node)
                old = source[span[0] : span[1]]
                # No `old == new` check: every operator either parenthesises its
                # replacement or emits a different token, so the no-op it would
                # catch cannot be produced. A mutation removing it survived, and
                # a guard nothing can reach is a guard nobody can trust.
                if (span, edit.new) in seen:
                    continue
                seen.add((span, edit.new))
                out.append(
                    Mutation(
                        label=label(path, edit.node.lineno, where, edit.prose),
                        path=path,
                        old=old,
                        new=edit.new,
                        tests=tests,
                        span=span,
                        operator=operator.name,
                    )
                )
    out.sort(key=lambda row: (row.span or (0, 0), row.operator))
    return out


def cap(mutations: Sequence[Mutation], limit: int) -> tuple[list[Mutation], list[Mutation]]:
    """Keep `limit` of them, round-robin across files; return what was dropped too.

    Round-robin rather than `[:limit]`, which would cover the alphabetically
    first file exhaustively and the largest one not at all -- and the printed
    summary would not show it, because the count would be right either way.
    """
    if limit <= 0 or len(mutations) <= limit:
        return list(mutations), []
    queues: dict[str, list[Mutation]] = {}
    for row in mutations:
        queues.setdefault(row.path, []).append(row)
    kept: list[Mutation] = []
    while len(kept) < limit and any(queues.values()):
        for path in sorted(queues):
            if queues[path] and len(kept) < limit:
                kept.append(queues[path].pop(0))
    dropped = [row for queue in queues.values() for row in queue]
    kept.sort(key=lambda row: (row.path, row.span or (0, 0)))
    return kept, dropped


def check(mutation: Mutation) -> None:
    """Refuse a row that cannot mean anything, reading the real file.

    Two shapes, because the two kinds of row make different promises. A
    hand-written one says "this text is unique", and a second match means the
    edit could land anywhere. A generated one says "this text is *here*", which
    is the stronger claim -- and checking it is what stands between the
    byte-versus-character offset trap and a mutation that silently edits the
    wrong span of a file that still parses.
    """
    original = Path(mutation.path).read_text(encoding="utf-8")
    if mutation.span is None:
        found = original.count(mutation.old)
        if found != 1:
            raise SystemExit(
                f"{mutation.label}: {mutation.path} contains the text to replace "
                f"{found} times, not once. A mutation that matches nothing tests "
                f"nothing, and one that matches twice tests something else."
            )
    else:
        start, end = mutation.span
        if original[start:end] != mutation.old:
            raise SystemExit(
                f"{mutation.label}: {mutation.path} no longer holds that text at "
                f"{start}..{end}, so the row would edit something else. The file "
                f"has changed since the table was generated -- regenerate it."
            )

    if not mutation.additive and mutation.old in mutation.new:
        raise SystemExit(
            f"{mutation.label}: the text to replace survives verbatim inside "
            f"the replacement, so the code under test does not change and "
            f"'caught' would mean nothing. If the point really is to insert "
            f"something in front of code that stays, pass additive=True."
        )


# --- where the diff says it matters ---------------------------------------

_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")


def _git(argv: Sequence[str], root: Path) -> str:
    """A local one, deliberately not `woswoar.gitrepo.git`.

    That one defaults `cwd` to the history directory, raises the product's
    `SyncError` and carries a 300 s timeout, and importing it would drag `store`
    and `progress` into a development tool. `tools/sandbox.py` already builds its
    own `git` argv for the same reason.
    """
    finished = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, check=False)
    if finished.returncode != 0:
        raise SystemExit(f"git {' '.join(argv)} failed: {finished.stderr.strip()}")
    return finished.stdout


def _unquote(target: str) -> str:
    """`+++ b/path`, including the C-quoted form git uses for odd bytes."""
    if target.startswith('"') and target.endswith('"'):
        target = target[1:-1].encode("ascii", "backslashreplace").decode("unicode_escape")
    return target[2:] if target.startswith(("a/", "b/")) else target


def parse_hunks(diff: str) -> dict[str, set[int]]:
    """Which lines of which files the diff *adds*. Pure, so a test can drive it.

    Only the `+c,d` half of each header is read. `d` omitted means one line; `d`
    of zero is a pure deletion and contributes nothing at all, because there is
    no new code there to mutate -- and taking it as one line would generate
    mutants for whatever happens to sit at that number now.
    """
    found: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            # No `/dev/null` special case, and the absence is the documentation:
            # git spells a deleted file's hunk `+0,0`, so the count check below
            # already drops it -- and a guard that nothing can reach is a guard
            # nobody can trust. A mutation removing the special case survived,
            # which is how it was found.
            current = _unquote(line[4:].strip())
            continue
        header = _HUNK.match(line)
        if header is None or current is None:
            continue
        start = int(header.group(1))
        count = 1 if header.group(2) is None else int(header.group(2))
        if count:
            found.setdefault(current, set()).update(range(start, start + count))
    return found


def changed_lines(base: str, root: Path) -> dict[str, set[int]]:
    """Every mutable line this branch has touched, working tree included."""
    top = _git(["rev-parse", "--show-toplevel"], root).strip()
    if Path(top).resolve() != root.resolve():
        raise SystemExit(
            f"run this from the repository root: paths in a diff are relative to {top}, not {root}."
        )
    _git(["rev-parse", "--verify", base], root)

    # `--merge-base` with no second commit, so the right-hand side is the
    # *working tree*: staged and unstaged changes included. That is the situation
    # the tool is used in, and the same reason `_sandboxes` copies rather than
    # running `git worktree add`. It also excludes commits that landed on the
    # base after this branch started, which a two-dot diff would drag in and
    # generate mutants for.
    diff = _git(["diff", "--unified=0", "--merge-base", base, "--", *MUTABLE], root)
    changed = {path: lines for path, lines in parse_hunks(diff).items() if mutable(path)}

    # A brand-new module is invisible to `git diff` until it is added, and "no
    # mutants for the new file" reads exactly like "the new file is covered".
    for name in _git(["ls-files", "--others", "--exclude-standard", "--", *MUTABLE], root).split():
        if mutable(name):
            body = (root / name).read_text(encoding="utf-8").splitlines()
            changed.setdefault(name, set()).update(range(1, len(body) + 1))
    return changed


# --- which tests to run it against ----------------------------------------


def module_of(path: str) -> str:
    """`woswoar/sync.py` -> `woswoar.sync`, and a package's `__init__` -> the package.

    The `__init__` case is not cosmetic: nothing anywhere writes
    `import woswoar.__init__`, so without it `woswoar/__init__.py` resolves to no
    tests at all and every mutant in it would run the whole suite.
    """
    dotted = path[: -len(".py")].replace("/", ".")
    return dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted


def _imported(node: ast.AST, package: str = "") -> Iterator[str]:
    """Every dotted name an import statement pulls in, relative ones included.

    The relative half is not a nicety. `tests/` is a package and its own style is
    `from . import support` / `from .support import requires_age` -- so an index
    that only understood absolute imports attributed nothing from any helper, and
    the closure below ran over an empty set. Found by mutation testing: removing
    the closure changed no answer, because it had never produced one.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
        return
    if not isinstance(node, ast.ImportFrom):
        return
    if node.level:
        if not package:
            return
        base = f"{package}.{node.module}" if node.module else package
    elif node.module:
        base = node.module
    else:
        return
    yield base
    for alias in node.names:
        yield f"{base}.{alias.name}"


def importers(root: Path) -> dict[str, set[str]]:
    """Which test modules import which module, computed rather than kept by hand.

    `woswoar/gitrepo.py`, `archive.py`, `store.py` and `__main__.py` have no
    same-named test module, so the name heuristic alone would report them as
    having no tests at all -- and a hand-kept table would rot towards that
    silently, which is the argument `tools/sandbox.py` makes about `ENV_KEYS`.

    Helper modules under `tests/` are followed one level: `tests/support.py`
    imports `store` and `cache`, and every test module that imports *it* reaches
    them too.
    """
    direct: dict[str, set[str]] = {}
    helpers: dict[str, set[str]] = {}
    uses_helper: dict[str, set[str]] = {}
    for source in sorted((root / "tests").glob("*.py")):
        name = f"tests.{source.stem}"
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        pulled = {module for node in ast.walk(tree) for module in _imported(node, "tests")}
        if source.name.startswith("test_"):
            for module in pulled:
                direct.setdefault(module, set()).add(name)
            for module in pulled:
                if module.startswith("tests."):
                    uses_helper.setdefault(module, set()).add(name)
        else:
            helpers[name] = pulled

    for helper, pulled in helpers.items():
        for module in pulled:
            direct.setdefault(module, set()).update(uses_helper.get(helper, set()))
    return direct


def targets_for(path: str, root: Path, index: dict[str, set[str]] | None = None) -> str:
    """The unittest targets a mutant in `path` should be run against.

    A heuristic, and it is allowed to be one *only* because a survivor is
    re-run against the whole suite before it is reported. That confirmation is
    what turns "the selection might have missed the test that catches this" from
    a wrong answer into a slower one.

    An empty answer means "run everything", and the caller says so in the row.
    It is not a fallback of convenience: `tests/test_demo.py` drives
    `tools/demo/seed.py` by spawning `python -m tools.demo.seed`, and
    `tests/test_shell_hook.py` reaches half of `woswoar/` through a real `bash`
    -- neither imports what it exercises, so no static index can see them. The
    unsafe answer would be to call that "no tests" and skip the row, which reads
    as coverage nobody has.
    """
    stem = Path(path).stem
    named = {
        f"tests.{found.stem}"
        for found in (root / "tests").glob("test_*.py")
        if found.stem == f"test_{stem}" or found.stem.startswith(f"test_{stem}_")
    }
    imported = (index if index is not None else importers(root)).get(module_of(path), set())
    # The union, not the name match short-circuiting the index. Measured on
    # #216: taking `tests/test_install.py` alone because the name matched left
    # nine mutants reported as survivors that `tests.test_doctor` catches --
    # `doctor` calls `rcfile_check`, and no name heuristic can see that. The
    # confirmation pass corrected all nine, so the answer was right either way,
    # but it paid a whole-suite run per row to get there.
    return " ".join(sorted(named | imported))
