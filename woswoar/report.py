"""One shape for "here is a thing woswoar checked, and what it found".

Five modules answered that question five ways -- a dataclass of counters, a
`(ok, detail)` pair, a bare string of prose, a `Protocol`. Each was defensible
alone; together there was no answer to "how should a new check report itself?",
and `doctor` fell straight into the gap: four of its lines came from `sync` as
values a test could assert on, and the rest were derived inline in the CLI where
only a test that greps stdout could reach them.

Two shapes live here, and the split is the answer to the question the first half
of #199 left open -- whether `cmd_sync`'s multi-paragraph prose could go through
`Check`. It could not, and forcing it would have put a dead label column and a
meaningless marker on every paragraph:

- `Check` is a **verdict**: one line, a label, pass/fail/info.
- `Notice` is an **explanation**: a paragraph, no label, sometimes a recipe.

The rule for picking, when something new needs reporting: if it fits a line and
can pass or fail, it is a check. Be exact about what is *not* here --
`search.empty_note`, `importer.Result` and `deps.report` are still themselves,
and none of them is shaped like either of these.

So a check is a **value**. Whoever knows the answer builds one; exactly one
place turns it into characters. That is what makes a verdict testable without
capturing output, which is the whole point -- `doctor` is the command people run
when something is wrong, and it was the least directly tested thing in the
program.

The markers live here for the same reason, and they carry a promise. The plain
forms are byte-for-byte what `doctor` printed before it had colour: the suite
asserts on `[FAIL] day keys` in a dozen places and `woswoar doctor | grep FAIL`
is a reasonable thing to have in a script. A tick that only a terminal renders
must never become the thing those depend on.
"""

from __future__ import annotations

import os
import re
import sys
from typing import NamedTuple, TextIO

#: What goes at the front of each line. See the module docstring: the plain
#: forms are an interface, not a fallback.
#:
#: The coloured forms are one column wide rather than four or six, so the labels
#: line up -- which the plain `[ok]`/`[FAIL]` never have.
PLAIN_MARKERS = {"ok": "[ok]", "fail": "[FAIL]", "info": "[--]"}
COLOUR_MARKERS = {
    "ok": "\x1b[32m✔\x1b[0m",
    "fail": "\x1b[31m✘\x1b[0m",
    "info": "\x1b[2m·\x1b[0m",
}

#: What `visible` discounts. Only the SGR form, because that is all
#: `COLOUR_MARKERS` holds and a general terminal-escape parser here would be a
#: guess at input nothing in this package produces.
_SGR = re.compile("\x1b\\[[0-9;]*m")


def visible(text: str) -> int:
    """How many columns `text` occupies once the terminal has eaten its escapes.

    `len` is ten for a coloured tick that draws one column. That does not matter
    where a marker starts the line, which is every caller `doctor` has -- it
    matters to `fleet`, which puts markers in a grid and has to know how wide
    its columns are before it prints the header.
    """
    return len(_SGR.sub("", text))


def centred(text: str, width: int) -> str:
    """`text` centred in `width` columns as a terminal will show it.

    `str.center` and `f"{text:^{width}}"` both count characters, so a table of
    coloured markers built with either loses its alignment exactly when there is
    colour to align -- the padding goes to the escape sequences. Wider than
    `width` is returned unpadded, as the format spec does.
    """
    padding = width - visible(text)
    if padding <= 0:
        return text
    left = padding // 2
    return " " * left + text + " " * (padding - left)


#: How wide the label column is. One number, because the continuation lines in
#: `note` are indented to sit under the detail rather than under the marker.
_LABEL_WIDTH = 12

#: What a `note` line is indented by. Five spaces, which is what the age advice
#: has always used -- wide enough to read as subordinate to the line above.
_NOTE_INDENT = "     "


class Check(NamedTuple):
    """One line of a report, and anything that has to be said under it.

    ``ok`` is deliberately three-valued. ``True`` and ``False`` are a condition
    that passed or failed; ``None`` is context that *cannot* fail -- how many
    log files there are, which remote is configured -- and it prints with its
    own marker so that a reader is never left wondering whether a neutral line
    was a silent pass. `doctor` had that distinction from the start, as
    `check()` and `info()`, and it is worth keeping as data rather than as two
    functions.
    """

    label: str
    detail: str
    #: Required, with no default, and that is worth the nine characters an info
    #: line spends saying `ok=None`. `None` is falsy, so a check that *meant*
    #: `ok=False` and lost it becomes an info line -- still printed, still
    #: reading like a report, and no longer able to fail the command. A mutation
    #: proved that invisible: `assertFalse(status.ok)` passes for both, so the
    #: four `sync` verdicts could have silently stopped failing with the whole
    #: suite green. Requiring it makes the omission a TypeError instead.
    ok: bool | None
    #: Lines printed indented beneath this one. Where a check has to explain
    #: itself at length -- a slow `age` costs six minutes on a year of history,
    #: and why -- the explanation belongs *with* the verdict rather than being
    #: printed by whoever happened to render it. Without somewhere to put this,
    #: moving a check out of the CLI would mean cutting its prose, and the prose
    #: is often the only place a state is ever explained.
    note: str = ""

    @property
    def failed(self) -> bool:
        """Whether this is a condition that did not hold.

        Not ``not self.ok``: an info line has ``ok`` of ``None``, which is
        falsy, and counting one as a failure would make `doctor` exit non-zero
        for saying how many log files there are.
        """
        return self.ok is False


class Notice(NamedTuple):
    """Something a command has to say at length, rather than in a column.

    The sibling `Check` needed and the open question #199 left. A check is one
    line with a label and a marker; several of `sync`'s outcomes are a paragraph
    of five to ten lines with a remedy in them, no label, and nothing a marker
    could usefully say -- `report.orphaned` carries a `git log --diff-filter=D`
    recipe. Stretching `Check.note` to hold that was the other option and it was
    worse: the label column and the marker would have been dead weight on every
    one of them, and `lines()` would have grown a mode.

    So two shapes, and the distinction is real: **a check is a verdict, a notice
    is an explanation.** Anything that fits a line and can pass or fail is a
    `Check`; anything that has to explain a state at length is a `Notice`.

    ``warning`` is data rather than the word being part of the prose. Five of
    `sync`'s eleven said `WARNING:` in their first line and six did not, which is
    a severity a test could previously only find by grepping for the word.

    Two things this deliberately does not share with `Check`. Severity here is a
    bool rather than `Check`'s three-valued ``ok``, because a paragraph is never
    "passing" -- it exists only when something is worth saying. And the prefix is
    plain text rather than going through `markers`: a marker is a column in a
    table of verdicts, and there is no table here. `paragraphs` is a thin
    renderer for that reason, and the prose is the producer's.
    """

    body: str
    #: Worth shouting about: the repository disagrees with what this machine
    #: expects, or history could not be published. The quieter six are ordinary
    #: states -- a machine that has not been granted access yet, a peer nobody
    #: has accepted -- which are not going wrong so much as not finished.
    warning: bool = False


def paragraphs(notices: list[Notice]) -> list[str]:
    """Each notice as the block to print, blank line first.

    The leading newline is here rather than in every one of eleven prose
    strings, which is where it used to be -- a blank line between paragraphs is
    a fact about printing paragraphs, not about any one of them.
    """
    out = []
    for notice in notices:
        prefix = "WARNING: " if notice.warning else ""
        out.append(f"\n{prefix}{notice.body}")
    return out


def markers(stream: TextIO | None = None) -> dict[str, str]:
    """Coloured markers for a terminal, the plain ones for anything else.

    `NO_COLOR` is honoured because it costs one condition and someone always
    has a reason -- a terminal that renders neither colour nor the glyphs, a log
    being captured through a pty, a screen reader.
    """
    out = sys.stdout if stream is None else stream
    watched = bool(getattr(out, "isatty", lambda: False)())
    if watched and not os.environ.get("NO_COLOR"):
        return COLOUR_MARKERS
    return PLAIN_MARKERS


def lines(checks: list[Check], marks: dict[str, str] | None = None) -> list[str]:
    """Every check as the lines to print, in the order given.

    Order is preserved rather than sorted, and that is load-bearing: `doctor`
    reports the shell before the hook before the rc file because that is the
    order someone fixes them in, and a report that reorders itself between runs
    is harder to diff than it needs to be.
    """
    marks = markers() if marks is None else marks
    out: list[str] = []
    for check in checks:
        kind = "info" if check.ok is None else ("ok" if check.ok else "fail")
        out.append(f"{marks[kind]} {check.label:<{_LABEL_WIDTH}} {check.detail}")
        out += [f"{_NOTE_INDENT}{line}" for line in check.note.splitlines()]
    return out


def failed(checks: list[Check]) -> bool:
    """Whether anything a person has to act on did not hold."""
    return any(check.failed for check in checks)
