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
from typing import Protocol, TextIO

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

    def finish(self) -> None:
        if self._showing:
            self._stream.write("\r" + " " * self._width + "\r")
            self._stream.flush()
            self._showing = False


class _Recorder:
    """Everything said, in order, for tests to assert on."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def phase(self, text: str) -> None:
        self.said.append(f"phase:{text}")

    def tick(self, done: int, total: int, noun: str) -> None:
        self.said.append(f"tick:{done}/{total} {noun}")

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
def to_terminal(stream: TextIO | None = None) -> Iterator[None]:
    """Report progress, if there is a terminal to report it to.

    Wrapped around the *whole* command rather than each slow part, so the
    patience clock measures what the user is waiting for rather than what any
    one loop is doing.
    """
    out = sys.stderr if stream is None else stream
    watched = out.isatty()
    with _install(_Terminal(out) if watched else None):
        yield


@contextmanager
def recording() -> Iterator[list[str]]:
    """Collect what would have been shown. For tests."""
    recorder = _Recorder()
    with _install(recorder):
        yield recorder.said
