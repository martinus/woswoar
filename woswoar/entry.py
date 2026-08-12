"""The on-disk record format.

This module is the single source of truth for how a history line looks. Both
shell hooks under ``woswoar/shell/`` reimplement :func:`escape` in pure shell so
that recording stays fork-free; ``tests/test_shell_hook.py`` pins every one of
them to this one, by driving the real hook and parsing its output here.

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
#:
#: The Linux claim is deliberately not widened to APFS. It very likely holds
#: there too, but "very likely" is not what a claim like this is for, and no
#: written guarantee was found -- so macOS is one of the filesystems the second
#: half of that sentence covers rather than the first. What it costs if it does
#: not hold is bounded and known: `parse_line` returns None for a torn line, so
#: an interleave loses one entry rather than corrupting the file, and every
#: other line in it still reads.
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


def home_relative(path: str, home: str) -> str:
    """``path`` written the way a record stores it: ``~/src/woswoar`` under ``home``.

    Anchored, and that is the whole subtlety. ``${PWD/#$HOME/~}`` rewrites
    ``/home/martinuscopy`` into ``~copy`` when ``$HOME`` is ``/home/martinus``,
    which is why the hook spells the same rule out in shell rather than using it.

    Three callers have to agree on this string: the hook writes it, the importer
    applies it to this machine's own rows, and `search`'s ``dir`` scope rebuilds
    it to compare against. It lives here because this module is what the format
    means -- and because `search` must not import `importer`, where it used to
    be: `credentials` documents pulling that module onto the scope-switch path
    as a measured cost.

    An empty ``home`` leaves the path alone. That is not a guess -- it is a
    machine whose ``$HOME`` is unset, where every path is honestly absolute.
    """
    if not path or not home:
        return path
    if path == home:
        return "~"
    if path.startswith(f"{home}/"):
        return "~" + path[len(home) :]
    return path


def escape(value: str) -> str:
    """Make ``value`` safe to store in a tab-separated field.

    Backslash must be replaced first, otherwise the escapes introduced by the
    later replacements would themselves be escaped.

    The membership test up front is not premature: on a real history only about
    4% of commands contain any of these characters, and this runs once per line
    written or imported. One scan that usually fails beats four replacements
    that usually find nothing -- measured, 5.6ms against 9.9ms without it over
    52,000 commands.

    This is the *storage* escape and nothing more. Making a command safe to put
    on a screen is `make_inert`, which the cache applies once on the way in.
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


#: C0 plus DEL, minus tab. Everything left either ends a command, forges a line
#: in a record file, or moves a terminal cursor around. Tab is excluded
#: deliberately: it is ordinary inside a command -- `awk -F'\t'` is written with
#: a real one -- and does none of those.
_CONTROL = frozenset(chr(code) for code in [*range(0x20), 0x7F]) - {"\t"}

#: Drop every control character, except the two `_ESCAPES` can render as
#: something readable. Reading the replacement out of `_ESCAPES` rather than
#: restating it: `search` recovers a command as ``make_inert(unescape(line))``,
#: so a second literal for the newline would send that round trip silently out
#: of step. `None` is what deletes a character.
_INERT_TABLE = str.maketrans({char: _ESCAPES.get(char) for char in _CONTROL})


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

    The guard is exact, not a heuristic: `str.isprintable` is False for every
    character the table maps -- all of C0 and DEL -- so a printable string
    cannot contain one, and the translate would return it unchanged. It matters
    because this runs over every line the cache reads and about 96% of real
    commands are clean: measured across 52,000 lines, `parse_line` costs 55.3ms
    without the guard and 43.6ms with it, against 40.0ms for no sanitising at
    all.
    """
    return text if text.isprintable() else text.translate(_INERT_TABLE)


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


def _same(text: str) -> str:
    """Identity, so `parse_line` picks a function once rather than branching twice."""
    return text


def parse_line(line: str, host: str, inert: bool = False) -> Entry | None:
    """Parse one log line, or return ``None`` if it is not a usable record.

    Returning ``None`` rather than raising is deliberate: a partially written
    final line (a shell killed mid-append) or a line from a future format
    version should cost us that one entry, never the whole file.

    ``inert`` applies :func:`make_inert` to the three fields whose contents this
    machine did not choose -- session, cwd and command. A flag rather than a
    second pass because the caller that wants it --
    :mod:`woswoar.cache`, on behalf of everything that displays history -- reads
    the whole log, and rebuilding each entry afterwards measured 82ms against
    59ms for 52,000 lines. The caller that does *not* want it is
    `store.existing_keys`, which compares against commands as the importer read
    them: sanitising there would stop an already-imported entry matching itself
    and re-import the whole file.

    `session` joined the other two when the preview pane started showing it. It
    is a `<hex>-<hex>` token as *this* machine writes it, and free text as far
    as the format is concerned -- a peer's chunk can put anything in that field,
    and until it had a display site nothing would have noticed. Sanitising it
    here rather than at that site is the rule `cache` states: this is the one
    door a peer's history comes through, and a rule about remembering to clean
    at each display had already been forgotten once before it moved here.
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

    clean = make_inert if inert else _same
    return Entry(
        ts=ts,
        host=host,
        session=clean(session),
        cwd=clean(unescape(cwd)),
        exit_code=exit_code,
        duration_ms=duration_ms,
        # Clamped on the way in as well as on the way out. `format_line`
        # truncates what this machine writes, but a line can also arrive from
        # another machine's chunk, where the only thing bounding its length is
        # whatever wrote it.
        cmd=clean(truncate(unescape(cmd))),
    )
