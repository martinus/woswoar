"""The on-disk record format.

This module is the single source of truth for how a history line looks. The bash
hook in ``woswoar/shell/woswoar.bash`` reimplements :func:`escape` in pure shell
so that recording stays fork-free; ``tests/test_shell_hook.py`` pins the two
implementations together by driving the real hook and parsing its output here.

Format v1, six tab-separated fields, command last::

    ts <TAB> session <TAB> cwd <TAB> exit <TAB> duration_ms <TAB> command

``cwd`` and ``command`` are escaped so a field can never contain a literal tab
or newline; every other field is numeric or an opaque token. Keeping the command
last means a parser only needs ``split("\\t", 5)``.

The host is *not* a field. It is derived from the path the line was read from,
which keeps every line shorter and makes a file trivially attributable.

Two fields are stored compactly, because they repeat on every single line and
sync commits every byte to git permanently:

``cwd``
    Written home-relative as ``~/src/woswoar`` when the directory was under the
    recording user's home. The ``~`` therefore means *that machine's* home, not
    the home of whoever later reads the file, so it is deliberately left
    unexpanded -- two synced machines can have different usernames.

``session``
    ``<start second>-<pid>``, both hex. Unique per host: two shells cannot share
    a pid at one instant, and a reused pid necessarily starts in a later second.
"""

from __future__ import annotations

from typing import NamedTuple

#: Sanity bound against pathological pastes. Linux serialises O_APPEND writes to
#: a regular file under the inode lock, so concurrent shells appending do not
#: interleave regardless of size -- this cap exists to keep one runaway paste
#: from bloating a day file, and to stay well-behaved on filesystems (NFS) that
#: make no such promise.
MAX_CMD_CHARS = 8000

TRUNCATION_MARKER = "...[truncated]"

_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
_UNESCAPES = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}


class Entry(NamedTuple):
    """One recorded command.

    A ``NamedTuple`` rather than a ``dataclass(slots=True)``: identical
    attribute access, but a plain tuple underneath, so the cache can build
    52,000 of them with ``tuple.__new__`` and never run Python per field. That
    matters because the whole history is loaded on every Ctrl-R.
    """

    ts: int
    host: str
    session: str
    cwd: str
    exit_code: int
    duration_ms: int
    cmd: str


def escape(value: str) -> str:
    """Make ``value`` safe to store in a tab-separated field.

    Backslash must be replaced first, otherwise the escapes introduced by the
    later replacements would themselves be escaped.

    The membership test up front is not premature: on a real history only about
    4% of commands contain any of these characters, and this runs once per
    displayed line on the Ctrl-R path. One scan that usually fails beats four
    replacements that usually find nothing.
    """
    for raw in _ESCAPES:
        if raw in value:
            break
    else:
        return value

    for raw, encoded in _ESCAPES.items():
        value = value.replace(raw, encoded)
    return value


def unescape(value: str) -> str:
    """Inverse of :func:`escape`.

    Implemented as a single left-to-right scan rather than chained
    ``str.replace`` calls: replacing ``\\\\t`` before ``\\\\\\\\`` (or vice versa)
    corrupts input like a literal backslash followed by the letter ``t``.
    """
    if "\\" not in value:
        return value

    out: list[str] = []
    i = 0
    end = len(value)
    while i < end:
        char = value[i]
        if char == "\\" and i + 1 < end:
            decoded = _UNESCAPES.get(value[i + 1])
            if decoded is not None:
                out.append(decoded)
                i += 2
                continue
        out.append(char)
        i += 1
    return "".join(out)


#: The two control characters worth keeping as something readable rather than
#: dropping. Taken from `_ESCAPES` rather than restated: `search` recovers a
#: command as ``make_inert(unescape(line))``, so if `escape` ever rendered a
#: newline differently, a second literal here would send the picker's round trip
#: silently out of step.
_INERT = {char: _ESCAPES[char] for char in ("\n", "\r")}

#: C0 plus DEL, minus tab. Everything left either ends a command, forges a line
#: in a record file, or moves a terminal cursor around. Tab is excluded
#: deliberately: it is ordinary inside a command -- `awk -F'\t'` is written with
#: a real one -- and does none of those.
_CONTROL = frozenset(chr(code) for code in [*range(0x20), 0x7F]) - {"\t"}


def make_inert(text: str) -> str:
    """``text`` with no raw C0 control character left in it.

    Leaving the fzf picker, this is what keeps a recalled command one command:
    `escape` maps newlines for *display*, and without this `unescape` would undo
    that exactly as the text lands in the shell's edit buffer, where bash runs a
    multi-line buffer as several commands on a single Enter. fzf clips a long
    line, so the picker can genuinely show one command while handing over two.

    Around a recipient label it does the same job for a different reader: it
    stops free text that anyone with push access can write from forging a line
    in ``recipients.txt``, or from driving the terminal during the `grant`
    confirmation -- ``\\x1b[1A\\x1b[2K`` erases the line above it, which is one
    way to hide a machine from the list approving it.

    C0 and no further, deliberately: widening it would mangle the UTF-8 in a
    recalled command, which is the caller with the stronger claim. What that
    leaves -- a bidi override, say -- is not dangerous in an edit buffer, and is
    handled where it is dangerous, by `sync.Reader.display_name`.

    Never applied to what is stored in a log: the history keeps the command as
    it was typed. A recalled multi-line command therefore comes back with a
    visible ``\\n`` in it -- wrong to run as-is, but obviously wrong, which beats
    silently running a command the picker never showed.
    """
    return "".join(_INERT.get(char, "") if char in _CONTROL else char for char in text)


def truncate(cmd: str) -> str:
    """Clamp an over-long command, mirroring what the shell hook does."""
    if len(cmd) <= MAX_CMD_CHARS:
        return cmd
    return cmd[:MAX_CMD_CHARS] + TRUNCATION_MARKER


def format_line(entry: Entry) -> str:
    """Render an entry as one log line, without the trailing newline."""
    return "\t".join(
        (
            str(entry.ts),
            entry.session,
            escape(entry.cwd),
            str(entry.exit_code),
            str(entry.duration_ms),
            escape(truncate(entry.cmd)),
        )
    )


def parse_line(line: str, host: str) -> Entry | None:
    """Parse one log line, or return ``None`` if it is not a usable record.

    Returning ``None`` rather than raising is deliberate: a partially written
    final line (a shell killed mid-append) or a line from a future format
    version should cost us that one entry, never the whole file.
    """
    line = line.rstrip("\n")
    if not line:
        return None

    fields = line.split("\t", 5)
    if len(fields) != 6:
        return None

    raw_ts, session, cwd, raw_exit, raw_duration, cmd = fields
    try:
        ts = int(raw_ts)
        exit_code = int(raw_exit)
        duration_ms = int(raw_duration)
    except ValueError:
        return None

    return Entry(
        ts=ts,
        host=host,
        session=session,
        cwd=unescape(cwd),
        exit_code=exit_code,
        duration_ms=duration_ms,
        # Clamped on the way in as well as on the way out. `format_line`
        # truncates what this machine writes, but a line can also arrive from
        # another machine's chunk, where the only thing bounding its length is
        # whatever wrote it.
        cmd=truncate(unescape(cmd)),
    )
