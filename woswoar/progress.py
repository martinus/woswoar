"""Say what a long command is doing, while it is still doing it.

A first sync of two years of history takes about six and a half seconds and a
`grant` over the same history about three, and until this both printed their
first character *after* all of them. Reported from a real setup as "some
commands take a long time and do not print anything, so I don't know if they are
doing something or hanging" -- which is the correct thing to wonder, because
nothing on screen distinguished the two.

Three rules, and they are the whole design:

**Silent until it is slow.** Nothing appears for the first `PATIENCE` seconds,
so the sync that runs every minute and finishes in ten milliseconds is as quiet
as it ever was. A progress bar that fires on fast operations is noise, and noise
is what people learn to stop reading.

**Only when a human is watching.** `stderr`, and only when it is a terminal.
The systemd timer runs `sync` a thousand times a day into the journal; `list`
is read by fzf through a pipe. Both must stay exactly as they are, and both do,
because neither is a tty.

**stderr, not stdout.** `woswoar list` is piped into fzf and `stats` is read by
people with `grep`. Progress is not part of either answer.

Library code calls `phase` and `tick` unconditionally and they do nothing until
a reporter is installed. That keeps the signatures of `sync.run` and its
half-dozen helpers free of a parameter threaded through purely to be passed on
-- at the price of module state, which is deliberate and is the reason
`recording` exists: the tests install a reporter and read back what was said,
rather than grepping a terminal.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol, TextIO, cast

#: How long a command may run before it owes the user a word. Below this,
#: printing costs more attention than it saves: an idle sync is ~10 ms, and the
#: timer runs one every minute.
PATIENCE = 0.7

#: How often the counter may be rewritten once it is showing. A 20,000-chunk
#: merge would otherwise spend real time on escape codes, and nobody can read
#: more than a few updates a second anyway.
INTERVAL = 0.1


class Reporter(Protocol):
    """Where progress goes. Two implementations: a terminal, and a test."""

    def phase(self, text: str) -> None:
        """Start a named step. Ends whatever was on the line before."""

    def tick(self, done: int, total: int, noun: str) -> None:
        """Report position within the current phase."""

    def clear(self) -> None:
        """Give the line back, without ending the run.

        Called before anything else writes to the terminal, and possibly many
        times. A later `tick` may redraw.
        """

    def finish(self) -> None:
        """Leave the line as it was found."""


class _Terminal:
    """Rewrites one line in place, and only after the wait is long enough."""

    def __init__(self, stream: TextIO, started: float | None = None) -> None:
        self._stream = stream
        # The clock starts when the *command* does, not when the first tick
        # arrives: an export that spends four seconds before its first day is
        # exactly the case this exists for.
        self._started = time.monotonic() if started is None else started
        self._last = 0.0
        self._label = ""
        self._showing = False
        self._width = 0

    def _due(self, now: float) -> bool:
        return now - self._started >= PATIENCE and now - self._last >= INTERVAL

    def _write(self, text: str) -> None:
        # `\r` and a pad to the previous width: shorter text must not leave the
        # tail of longer text behind it. `\x1b[K` would be neater and is not
        # portable to every terminal woswoar might meet.
        self._stream.write("\r" + text.ljust(self._width) + "\r" + text)
        self._stream.flush()
        self._width = len(text)
        self._showing = True

    def phase(self, text: str) -> None:
        self._label = text
        now = time.monotonic()
        if now - self._started >= PATIENCE:
            self._last = now
            self._write(f"  {text}...")

    def tick(self, done: int, total: int, noun: str) -> None:
        now = time.monotonic()
        if not self._due(now):
            return
        self._last = now
        # No percentage when the total is unknown or zero: "0%" for a while and
        # then "100%" is worse than a plain count.
        share = f" ({done * 100 // total}%)" if total > 0 else ""
        self._write(f"  {self._label}... {done}/{total} {noun}{share}")

    def clear(self) -> None:
        if not self._showing:
            return
        # Lowered before the write, not after: the write goes to a stream that
        # `_sharing_the_screen` may have wrapped, and a wrapped write calls back
        # in here. Clearing the flag first makes that second call a no-op rather
        # than an unbounded recursion.
        self._showing = False
        self._stream.write("\r" + " " * self._width + "\r")
        self._stream.flush()

    def finish(self) -> None:
        self.clear()


class _Recorder:
    """Everything said, in order, for tests to assert on."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def phase(self, text: str) -> None:
        self.said.append(f"phase:{text}")

    def tick(self, done: int, total: int, noun: str) -> None:
        self.said.append(f"tick:{done}/{total} {noun}")

    def clear(self) -> None:
        """Nothing to give back: a recorder never held the line.

        Deliberately unrecorded. It fires on every write to stdout, so recording
        it would bury what a test is actually asserting on under one entry per
        printed line.
        """

    def finish(self) -> None:
        self.said.append("finish")


_current: Reporter | None = None


def phase(text: str) -> None:
    """Name the step now starting. A no-op unless someone is listening."""
    if _current is not None:
        _current.phase(text)


def tick(done: int, total: int, noun: str) -> None:
    """Position within the current phase. A no-op unless someone is listening."""
    if _current is not None:
        _current.tick(done, total, noun)


def clear() -> None:
    """Take the progress line off the screen before something else uses it."""
    if _current is not None:
        _current.clear()


class _Shared:
    """An output stream, with the progress line cleared out of the way first.

    The counter is rewritten in place and so ends in no newline, and
    `to_terminal` wraps the *whole* command -- so a command that prints while it
    is still working prints onto the counter:

        merging other machines... 3/5 days (60%)Merged 12 commands

    Reported from a real install as output that "doesn't properly print a
    newline when done". Only the first such line is damaged, which is what makes
    it look intermittent: after it the cursor is on a fresh line again.

    Done here rather than by calling `clear` before each `print` because the
    commands that print mid-run are not a closed set -- `sync` reports resealing
    and newcomers, `doctor` prints a line per check -- and a rule every future
    command has to remember is one a future command will forget.

    Both streams are wrapped, not just stdout. The counter goes to stderr, so
    stderr is where a collision is most obvious, and `init` prints "joined, but
    the first sync failed" there (`__main__.py:489`) partway through exactly the
    kind of slow command that has a counter running.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        # Empty writes reach here from `print`'s separator handling, and must
        # not disturb a counter that is mid-redraw.
        if text:
            clear()
        return self._stream.write(text)

    def __getattr__(self, name: str) -> Any:
        # `isatty` above all: `doctor`'s tick marks and `list --colour` decide
        # on colour by asking stdout, and a wrapper that answered for itself
        # would turn the colour off whenever progress was installed.
        return getattr(self._stream, name)


@contextmanager
def _install(reporter: Reporter | None) -> Iterator[None]:
    global _current
    before = _current
    _current = reporter
    try:
        yield
    finally:
        if reporter is not None:
            reporter.finish()
        _current = before


@contextmanager
def _sharing_the_screen() -> Iterator[None]:
    """Route both output streams through `_Shared` for the duration.

    The reporter keeps the raw stream it was built with, so its own writes do
    not recurse back through here.
    """
    before_out, before_err = sys.stdout, sys.stderr
    sys.stdout = cast(TextIO, _Shared(before_out))
    sys.stderr = cast(TextIO, _Shared(before_err))
    try:
        yield
    finally:
        sys.stdout, sys.stderr = before_out, before_err


@contextmanager
def to_terminal(stream: TextIO | None = None) -> Iterator[None]:
    """Report progress, if there is a terminal to report it to.

    Wrapped around the *whole* command rather than each slow part, so the
    patience clock measures what the user is waiting for rather than what any
    one loop is doing. Which is also why stdout is borrowed for that whole time:
    the command keeps printing while progress is showing.
    """
    out = sys.stderr if stream is None else stream
    watched = out.isatty()
    with _install(_Terminal(out) if watched else None):
        if not watched:
            # Nothing holds the line, so nothing has to give it back -- and
            # `list` piped into fzf writes every row through here.
            yield
        else:
            with _sharing_the_screen():
                yield


@contextmanager
def recording() -> Iterator[list[str]]:
    """Collect what would have been shown. For tests."""
    recorder = _Recorder()
    with _install(recorder):
        yield recorder.said
