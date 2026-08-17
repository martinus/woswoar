"""One shape for "here is a thing woswoar checked, and what it found".

Five modules answered that question five ways -- a dataclass of counters, a
`(ok, detail)` pair, a bare string of prose, a `Protocol`. Each was defensible
alone; together there was no answer to "how should a new check report itself?",
and `doctor` fell straight into the gap: four of its lines came from `sync` as
values a test could assert on, and the rest were derived inline in the CLI where
only a test that greps stdout could reach them.

`Check` replaced two of the five: the `(ok, detail)` pair `sync` used for its
four status functions, and the inline deriving. Be exact about what is left,
because the docstring that overclaims is the one nobody corrects --
`sync.Report`'s seventeen fields, `search.empty_note`, `importer.Result` and
`deps.report` are all still themselves, and `lines()` below is `doctor`'s
renderer. `cmd_sync`'s prose is multi-paragraph and has no label column, so
whether `note` stretches to hold it or this module grows a second renderer is
genuinely open. See #199.

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
