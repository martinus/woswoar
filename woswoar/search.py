"""Scope filtering, ranking, and the fzf front end.

Division of labour, per the design: Python is the search engine (loading,
filtering, sorting, formatting) and fzf is purely the UI (fuzzy match, display,
select).
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from typing import IO

from . import cache, store
from .entry import escape, home_relative, make_inert, unescape

Scope = Literal["global", "host", "session", "dir"]
#: The order Ctrl-R visits them in, and the order the header names them. `dir`
#: is appended rather than slotted in "by narrowing": inserting it would change
#: what an existing user's second press does, and muscle memory is a format in
#: the sense CLAUDE.md rule 8 means.
SCOPES: tuple[Scope, ...] = ("global", "host", "session", "dir")

#: The key that jumps straight to each scope, without the `ctrl-` prefix. One
#: table, because the picker's `--bind` and the header that advertises it are
#: otherwise two hand-written copies of the same fact -- and the way that fails
#: is a header naming a key nothing binds, which nothing but a reader notices.
#:
#: `g`, `h` and `s` are the initials of their scopes; `o` is not, and is chosen
#: because fzf binds nothing to `ctrl-o`. `ctrl-d` is its `delete-char/eof` and
#: *aborts the picker* on an empty query; `alt-d` is `kill-word` and is two keys,
#: which breaks the shape of the other three.
KEYS: dict[Scope, str] = {"global": "g", "host": "h", "session": "s", "dir": "o"}

#: The key that shows the preview. fzf's own convention -- `man fzf` binds
#: `ctrl-/` in its `change-preview-window` example -- and what it displaces is
#: `toggle-wrap-word`, which is the thing here worth losing.
PREVIEW_KEY = "ctrl-/"

#: Every display line is ``"<7-char relative time><2 spaces><escaped command>"``.
#: The prefix is a fixed width so recovering the command from what fzf prints
#: back is an exact slice rather than a parse. Seven fits the widest age
#: `relative_time` can produce (``11mo29d``, ``99y11mo``); recent commands --
#: most of any screen -- still render two or three characters and the rest is
#: padding, so the width is only actually spent on rows old enough to need it.
_TIME_WIDTH = 7
_PREFIX = _TIME_WIDTH + 2

#: Sort key for a row. In C, rather than a lambda.
_STAMP = itemgetter(0)

#: One row: when, what, how it ended, and which machine ran it -- and, when the
#: preview pane asked for them, the three recorded fields there is no screen
#: width for, appended after those four.
#:
#: Loosely typed because the length varies with the caller, and there is no way
#: to say better on this floor: the shape is
#: ``tuple[int, str, str, str, Unpack[tuple[str, ...]]]``, and `typing.Unpack` is
#: 3.11, where `pyproject` says 3.10 and the dependency list is empty on purpose.
#:
#: So the invariant is prose and a test rather than a type: the first four never
#: move, because the sort reads ``[0]``, the dedup reads ``[1]`` and
#: `render_rows` unpacks all four. `test_show_reports_the_row_the_list_shows`
#: pins it by asking both sides about every index of a list built from all seven,
#: with a distinct value per field, so a swapped pair shows up as a wrong answer
#: rather than a short list.
Row = tuple[Any, ...]

#: Plain red rather than the dim red this started as. Dim was the right choice
#: while the whole line carried it -- a screen of dim red is legible where a
#: screen of bright red is not -- but it now marks the age column alone, and
#: dim red on three characters is close to not being there at all.
_FAILED = "\x1b[31m"
_RESET = "\x1b[0m"

#: The rest of the palette, used only by the preview pane. Green, red and dim,
#: which is what `doctor` already paints with -- a fourth colour would be a new
#: thing for a reader to learn, and these three already mean "fine", "not fine"
#: and "structure, not content" everywhere else in the program.
_OK = "\x1b[32m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"


def is_failure(code: str) -> bool:
    """Whether a recorded exit code is a *known* failure.

    Unknown is not failure, and that distinction is the whole function.
    `~/.bash_history` and `~/.zsh_history` record no exit codes whatsoever, so
    `importer` stores -1 for "never knew" -- and "anything but 0" painted every
    imported command red, which on a freshly imported history is the entire
    screen. Reported as "I think bash import marks all lines as red?".

    Parsed rather than string-compared against a list of the codes seen so far:
    the same bug would come back for any other sentinel, and the cache holds
    these as text only because it is columnar.
    """
    try:
        return int(code) > 0
    except ValueError:
        return False


def relative_time(ts: int, now: int | None = None) -> str:
    """Render a timestamp as an age, at most :data:`_TIME_WIDTH` characters.

    Computed at display time rather than stored, so it never goes stale.

    Two adjacent units from hours upward -- ``3h42m``, ``12d5h``, ``6mo12d``,
    ``1y3mo`` -- because one unit is off by up to half of itself, and a bare
    ``6mo`` hiding a fortnight was reported as too coarse to be useful. Below
    an hour a single unit is already within a minute, and those rows are most
    of any screen, so they stay as narrow as they were. A second unit of zero
    is dropped rather than written: ``3h``, not ``3h0m``.
    """
    delta = (int(time.time()) if now is None else now) - ts
    if delta < 0:
        return "now"
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        big, small = divmod(delta // 60, 60)
        units = "h", "m"
    elif delta < 86400 * 30:
        big, small = divmod(delta // 3600, 24)
        units = "d", "h"
    elif delta < 86400 * 360:
        # Cut over exactly where the month count would reach 12, which would
        # be both wider than the column and sillier than saying "1y".
        big, small = divmod(delta // 86400, 30)
        units = "mo", "d"
    else:
        big = max(1, delta // (86400 * 365))
        if big >= 100:
            return "old"
        # Clamped: 360-364 days is short of one year but says "1y" rather than
        # inventing a negative month, and day 364 of any year would otherwise
        # round to a twelfth month the column has no room for.
        small = min(11, max(0, (delta // 86400 - big * 365) // 30))
        units = "y", "mo"
    if small == 0:
        return f"{big}{units[0]}"
    return f"{big}{units[0]}{small}{units[1]}"


#: Widest a machine label may be. Generous, because it is only ever as wide as
#: the longest label actually present, and a column of ellipses distinguishes
#: nothing -- which is what a tight cap produced: `martinleitnerank` and
#: `martinus@DT-24YY`, both cut, from two machines that share neither.
_HOST_WIDTH = 20

#: Cut here rather than at the end. A name is `user@host`, and the *host* is
#: what tells two of your machines apart -- it is also the part a long username
#: pushes off the end.
_ELLIPSIS = "\u2026"


def host_label(host_id: str) -> str:
    """What to show for one machine, in a form safe to put on a coloured line.

    `make_inert` because this is *another machine's* text: `_merge_name` writes
    whatever decrypted out of its `name.age` straight to disk, and sealing one
    needs no secret, so anyone who can push chooses it. Harmless while the
    picker was escape-free; with `--ansi` it is a way to drive the terminal, so
    it is neutralised here rather than trusted to have been neutralised
    somewhere upstream.

    Falls back to a short slice of the opaque id, which is at least stable and
    is what the host directory is called.
    """
    name = make_inert(store.host_name(host_id)).strip()
    return name or host_id[:8]


def host_labels(hosts: set[str]) -> dict[str, str]:
    """A short label per machine, chosen so that they stay *different*.

    Names are `user@host` by default, and on one person's machines the user is
    usually the same and the host is what differs -- so the host half is tried
    first. It is dropped only if two machines would then look alike, because a
    column that cannot tell them apart is worse than a long one.

    Reported: two machines showing as `martinleitnerank` and
    `martinus@DT-24YY`, both cut at sixteen characters, neither saying which
    machine it was.
    """
    full = {host: host_label(host) for host in hosts}
    short = {host: label.rsplit("@", 1)[-1] or label for host, label in full.items()}
    chosen = short if len(set(short.values())) == len(short) else full
    return {host: _clip(label) for host, label in chosen.items()}


def _clip(label: str) -> str:
    """Trim to `_HOST_WIDTH`, keeping the end -- see `_ELLIPSIS`."""
    if len(label) <= _HOST_WIDTH:
        return label
    return _ELLIPSIS + label[-(_HOST_WIDTH - 1) :]


def command_from_line(line: str, host_width: int = 0) -> str:
    """Recover the original command from a rendered line.

    ``host_width`` must be the one the line was rendered with. It is passed
    explicitly rather than recomputed because the two happen in different
    processes -- the picker's reload runs `woswoar list` -- and a machine
    arriving in between would otherwise change the answer and slice somebody's
    recalled command in the wrong place.
    """
    return make_inert(unescape(line[_PREFIX + (host_width + 2 if host_width else 0) :]))


def _same_directory(path: str) -> bool:
    """Whether ``path`` names the directory this process is actually in."""
    try:
        return os.path.samefile(path, ".")
    except OSError:
        return False


def _here() -> tuple[str, ...]:
    """The current directory, in every form a recorded ``cwd`` might take.

    Both forms, always. The tilde form is what the hook writes and what the
    importer stores for this machine's own rows; the absolute form is what the
    importer deliberately leaves alone for a *peer's* rows, because another
    machine's home directory is a guess. So a history imported from two atuin
    hosts holds ``~/src/x`` for one and ``/home/you/src/x`` for the other, and a
    single key would silently miss half of it -- in exactly the multi-machine
    case woswoar exists for.

    `$PWD` before `os.getcwd()`, because they are different answers after a `cd`
    through a symlink: `$PWD` is the logical path you typed, `getcwd` resolves
    it. The hook records `$PWD`, so anything else here makes every command run
    in a symlinked directory invisible to this scope. But `$PWD` is inherited
    and so is whatever the parent process left there: it is believed only when
    it is absolute *and* `stat` says it is the same directory as `.`.

    Made inert because the cached `cwd` was: a directory whose name carries a
    control byte is stored with that byte removed, and a key still carrying it
    would never match anything.
    """
    try:
        physical = os.getcwd()
    except OSError:
        # The directory was removed out from under this process, so there is no
        # `.` left to check `$PWD` against and no resolved path to fall back to.
        # `rm -rf` of the directory you are standing in, then Ctrl-R, is not
        # exotic; a traceback there would be.
        physical = ""

    logical = os.environ.get("PWD", "")
    trusted = os.path.isabs(logical) and (not physical or _same_directory(logical))
    path = logical if trusted else physical
    if not path:
        return ()

    home = os.environ.get("HOME", "")
    tilde = make_inert(home_relative(path, home))
    absolute = make_inert(path)
    # Standing anywhere outside home makes the two spellings the same string,
    # and one key is then all there is to look for. Tilde first either way:
    # that is the one `_here_label` shows a person.
    return (tilde,) if tilde == absolute else (tilde, absolute)


def _here_label() -> str:
    """How to name the current directory to a person -- as a record would spell it."""
    keys = _here()
    return keys[0] if keys else "this directory"


def empty_note(scope: Scope) -> str | None:
    """What to tell someone whose scope matched nothing, if anything.

    `dir` alone. An empty `global` is a machine that has recorded nothing and
    explains itself; an empty `dir` is indistinguishable from a broken feature,
    and the one fact that separates them is *which* directory was searched --
    which is exactly what the `$PWD` rule in `_here` can get wrong.

    Here rather than in `__main__` because which scope owes an explanation is a
    property of the scopes, and this module already owns the prose on this path:
    `interactive` prints the missing-fzf report and what to run instead. What
    stays with the caller is the part that is genuinely its own -- whether
    anyone is looking at a terminal.
    """
    if scope != "dir":
        return None
    return f"woswoar: no commands recorded in {_here_label()} or below"


def _under(cwds: list[str], keys: tuple[str, ...]) -> list[int]:
    """Which rows were recorded in one of ``keys`` or below it.

    Equal to a key, or below it *across a separator* -- never a bare prefix
    test. That is the `/home/martinuscopy` trap `entry.home_relative` already
    solves, one directory further down: without the `/`, standing in
    `~/src/foo` also matches everything in `~/src/foobar`.
    """
    if "/" in keys:
        # The root is every recorded directory, including the `~` ones -- which
        # do not start with `/` at all, so the anchored test below would match
        # none of them, and the prefix it built would be `//`.
        return list(range(len(cwds)))
    # A tuple of prefixes rather than `any(... for key in keys)`, because the
    # generator is re-entered per row: 10.9 ms against 2.6 ms over 54,600 rows.
    # `str.startswith` takes the tuple and does the loop in C.
    below = tuple(f"{key}/" for key in keys)
    return [index for index, value in enumerate(cwds) if value in keys or value.startswith(below)]


#: The parallel columns a display line is built from -- stamps, commands, exit
#: codes and hosts, plus whatever `Cache.display_columns` was asked to append
#: after them. Variadic for the same reason `Row` is: the extras ride along
#: through the sort and the dedup, and only the first four are ever displayed.
Columns = tuple[list[str], ...]


def _columns_for(scope: Scope, extra: bool = False) -> tuple[cache.Cache, Columns | None]:
    """The rows a scope is about, straight out of the cache's columns.

    Split out of `lines_for` so a timeline can start from exactly the same set
    -- asking the same question twice and getting different answers is how the
    anchor `{n}` names would stop meaning what fzf thinks it means. `detail`
    takes the same route for the same reason, one step further: it is handed an
    index into a list another process rendered.

    ``extra`` is passed straight through to the cache, so the preview's three
    additional columns are narrowed by exactly the same scope logic as the four
    below them -- rather than being fetched separately and lined up by hope.
    """
    loaded = cache.load_columns()
    if scope == "host":
        return loaded, loaded.display_columns({store.machine().id}, extra=extra)
    if scope == "session":
        session = os.environ.get("WOSWOAR_SESSION", "")
        if not session:
            # Not an error: this is what a shell without the hook loaded looks like.
            return loaded, None
        # `session` and `dir` are the two columns that are per entry rather than
        # per file, so they are the scopes that cannot be answered by dropping
        # whole files the way `host` is.
        wanted = [i for i, value in enumerate(loaded.sessions()) if value == session]
        return loaded, _gather(loaded.display_columns(extra=extra), wanted)
    if scope == "dir":
        # Deliberately every machine, not this one. `~/src/woswoar` on either
        # box is the same project, and asking that across machines is what
        # woswoar is for; the scopes do not compose, so a `dir` that implied
        # `host` would remove the only way to ask it. Its failure mode is an
        # extra row with the machine column naming where it came from, against a
        # missing row that looks like a broken feature.
        wanted = _under(loaded.cwds(), _here())
        return loaded, _gather(loaded.display_columns(extra=extra), wanted)
    return loaded, loaded.display_columns(extra=extra)


def _gather(columns: Columns, wanted: list[int]) -> Columns:
    """Every column, narrowed to the rows at ``wanted``."""
    return tuple([column[i] for i in wanted] for column in columns)


def _zipped(columns: Columns) -> list[Row]:
    """The columns as rows, with the timestamp converted once.

    One `int` per row, here, where the sort needs it -- rather than on every
    entry of a history at parse time.
    """
    stamps, *rest = columns
    return list(zip(map(int, stamps), *rest, strict=True))


def _rows_for(columns: Columns, dedup: bool, around: int | None) -> tuple[list[Row], int]:
    """The rows to display, and where the cursor belongs among them.

    The second value is 1-based for fzf's `pos()` and is 0 when there is no
    anchor to sit on -- which is every ordinary list, and also a timeline whose
    anchor has gone.
    """
    ranked = rank_rows(columns, dedup=dedup)
    if around is None:
        return ranked, 0
    anchor = ranked[around] if 0 <= around < len(ranked) else None
    return _window_around(anchor, columns)


def lines_for(
    scope: Scope,
    dedup: bool = True,
    limit: int | None = None,
    colour: bool = False,
    host_width: int | None = None,
    around: int | None = None,
) -> list[str]:
    """The full pipeline: load, filter, rank, render.

    With `around`, the answer is not a ranked list at all but the *timeline*
    either side of one of its rows -- see `_window_around`. The row is named by
    its position in the ranked list because that is what fzf can tell us: `{n}`
    is the index of the highlighted item, and the list it indexes into is the
    one this same function produced a moment earlier.

    The two columns a line is made of come straight out of the cache, without
    an `Entry` in between. On a real 54,804-command history that is 39 ms of
    parsing rather than 17: five of the seven fields on an entry are never
    displayed, and building namedtuples to hold them was most of what Ctrl-R
    waited for.

    `session` and `dir` are the exceptions -- both filter on a column that is
    per entry rather than per file -- so each fetches one more column of its
    own. `host` needs none: it is a property of the file, so the cache filters
    whole files out before a single row is looked at.
    """
    loaded, columns = _columns_for(scope)
    if columns is None:
        return []
    rows, _ = _rows_for(columns, dedup=dedup, around=around)
    if limit is not None:
        rows = rows[:limit]
    if host_width is None:
        host_width = host_width_for({meta.host for meta in loaded.meta.values() if meta.host})
    return render_rows(rows, colour=colour, host_width=host_width)


def _exit_field(code: str) -> tuple[str | None, str]:
    """What the ``exit`` row shows, and what colour it is.

    ``None`` is `importer`'s "never knew": `~/.bash_history` records no exit
    codes whatsoever, so it stores -1, and a literal ``exit -1`` would invent a
    failure that never happened -- the same misreading that painted a freshly
    imported history entirely red, one screen further in.

    Parsed rather than compared against the string ``"-1"``, for the reason
    `is_failure` gives two screens up: the same bug comes back for any other
    sentinel, and ``-01`` is the same number written differently. An
    unparseable code is handed over exactly as it was stored and painted
    nothing, because at that point the honest thing is to show what the file
    says and claim nothing about what it means.

    Three answers, so `is_failure` -- which has two, and deliberately treats
    unknown as "not a failure" -- cannot serve: the pane distinguishes a clean
    exit from an unknowable one, where a display line only ever asks whether to
    paint the age red.
    """
    try:
        parsed = int(code)
    except ValueError:
        return code, ""
    if parsed < 0:
        return None, ""
    return code, _OK if parsed == 0 else _FAILED


def _duration_label(raw: str) -> str | None:
    """How long the command took, or ``None`` when nothing was recorded.

    ``None`` rather than "0 ms": an imported history has no durations at all,
    and a whole column of zeroes is a claim, where `_field`'s "not recorded" is
    the truth. Unparseable is treated the same way -- a damaged field should
    cost this one line, never the block.

    At most two units, and no more precision than anyone reads off a pane they
    opened to see where a command ran: ``87 ms``, ``1.2 s``, ``2m 3s``, ``1h 2m``.
    """
    try:
        ms = int(raw)
    except ValueError:
        return None
    if ms < 0:
        return None
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    minutes, seconds = divmod(ms // 1000, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


#: Wide enough for ``session``, which is the longest label. The names are the
#: picker's own -- `host`, `session` and `dir` are three of its four scopes -- so
#: the pane says which key would narrow the list to the thing it is pointing at.
_LABEL_WIDTH = 7


def _field(label: str, value: str | None, colour: bool, tint: str = "") -> str:
    """One ``label  value`` row of the pane's table.

    Every label is always present, including the ones with nothing to say. The
    first version left those lines out and read as a bug in the preview rather
    than as a fact about the row -- a block that is six lines here and three
    there gives a reader nothing to scan against, and "not recorded" is an
    answer where a missing line is a question. An imported history has no
    directory, no exit code, no duration and no session, so on a fresh import
    that is four of the six.

    The label is dim rather than the value bright: it is the same six words on
    every row, so it should be the part the eye skips.
    """
    if value is None:
        value, tint = "not recorded", _DIM
    if not colour:
        return f"{label:<{_LABEL_WIDTH}}  {value}"
    return f"{_DIM}{label:<{_LABEL_WIDTH}}{_RESET}  {tint}{value}{_RESET if tint else ''}"


def _block(row: Row, now: int | None = None, colour: bool = False) -> str:
    """The three unseen fields, plus the two the line already shows, as a block.

    Nothing here neutralises anything, and that is deliberate rather than an
    omission: every field comes out of the cache, which is the one door a peer's
    history enters through and which applies `make_inert` on the way in --
    `session` included, since this is what gave it a display site. `host` is the
    exception, because it is not stored with the row at all: the name is a file
    a peer wrote, so it goes through `host_label`.

    `host_label` and not `host_labels`, which is what the display line uses.
    The pane names one machine and has no column to fit, so it shows the whole
    `user@host` where the line shows the abbreviation that keeps two machines
    apart -- more, not different, and there is no host *set* here to compute the
    abbreviation from anyway.

    The command is printed as it was recorded rather than `escape`d, because
    this is the one place with room to show it whole: the picker's line is
    clipped at the window edge, and `--preview-window=wrap` is not. It goes
    *last*, and below the table rather than in it: last because it is the one
    field with no bound on its length, and a pane is a fixed number of rows --
    put it first and a `find` invocation with forty arguments pushes everything
    the pane exists to show off the bottom. Below the table because a value that
    wraps cannot sit in a label column; fzf continues a wrapped line at column
    zero.

    With `colour`, the labels are dim, the exit code is green or red, and the
    command is bold. Safe for the same reason `render_rows` is: `make_inert` has
    already taken every C0 byte, ESC among them, off everything that came out of
    the cache, so nothing a peer published can add escapes of its own. `host` is
    the exception and `host_label` is why it is not one.
    """
    ts, cmd, code, host, cwd, duration, session = row
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    age = relative_time(ts, now)
    # "now" is what `relative_time` says for a stamp in the future, which is
    # another machine's clock running ahead. "(now ago)" would be nonsense.
    ago = f"({age})" if age == "now" else f"({age} ago)"
    stamp = f"{when}  {_DIM}{ago}{_RESET}" if colour else f"{when}  {ago}"
    shown, tint = _exit_field(code)

    return "\n".join(
        [
            _field("when", stamp, colour),
            _field("dir", cwd or None, colour),
            _field("host", host_label(host), colour),
            _field("session", session or None, colour),
            _field("exit", shown, colour, tint),
            _field("took", _duration_label(duration), colour),
            "",
            f"{_BOLD}{cmd}{_RESET}" if colour else cmd,
        ]
    )


def detail(
    index: int,
    scope: Scope,
    dedup: bool = True,
    around: int | None = None,
    colour: bool = False,
) -> str | None:
    """The preview block for one row of what `lines_for` would print, or ``None``.

    ``index`` is fzf's `{n}`, so this has to reproduce the very list the picker
    is showing -- which is why it takes the same `scope`, `dedup` and `around`
    the reload that filled it was given. Get `around` wrong and the preview
    describes a different command from the highlighted one, confidently and with
    nothing on screen to contradict it; that is the whole hazard of this feature
    and it is why the timeline's binding repoints the preview as well as the
    list.

    ``None`` for an index that is not there, which is the same race
    `_window_around` guards: a background sync can merge new commands between
    the picker opening and the cursor moving.
    """
    _, columns = _columns_for(scope, extra=True)
    if columns is None:
        return None
    rows, _ = _rows_for(columns, dedup=dedup, around=around)
    if not 0 <= index < len(rows):
        return None
    return _block(rows[index], colour=colour)


#: How many commands a timeline shows either side of the one it is centred on.
#: A screenful each way: enough that the thing you were looking for is usually
#: already on it, few enough that the picker still opens instantly.
TIMELINE_SPAN = 40


def _window_around(anchor: Row | None, columns: Columns) -> tuple[list[Row], int]:
    """Everything either side of `anchor`, oldest first.

    Newest first, like every other list here. This started out reversed, on
    the theory that a timeline should read downwards into the future -- and
    that was wrong in use: every other screen in woswoar puts the recent thing
    at the top, and one screen that does not is a screen you have to stop and
    reorient on. Reported as "I think it's bad that it reverses order".

    Never deduplicated, also unlike every other list. A timeline with the
    repeats collapsed is not a timeline -- running `cargo test` four times
    while fixing something is the shape of what happened, and it is exactly
    what you came here to see.

    An anchor that is no longer in the list gives an empty window rather than a
    wrong one. The index comes from fzf, and a background sync can merge new
    commands between the picker opening and the key being pressed; showing the
    wrong point in history confidently is worse than showing none.
    """
    if anchor is None:
        return [], 0
    chronological = sorted(_zipped(columns), key=_STAMP)
    try:
        # By identity of the whole row, not by timestamp: two machines can
        # record in the same second, and `sorted` is stable but not meaningful
        # between them.
        at = chronological.index(anchor)
    except ValueError:
        return [], 0
    start = max(0, at - TIMELINE_SPAN)
    window = chronological[start : at + TIMELINE_SPAN + 1]
    # Reversed at the end rather than sorted that way, because the window is
    # defined by what surrounds the anchor *in time* -- taking the neighbours
    # first and the direction second keeps those two decisions apart.
    offset = at - start
    return window[::-1], len(window) - offset


def anchor_position(index: int, scope: Scope, dedup: bool = True) -> int:
    """Where fzf should put the cursor after unfolding a timeline.

    Its own entry point because the answer has to reach fzf *before* the
    reload it applies to: `transform` runs a shell command and prints the
    actions to perform, so the position has to be known while composing
    `reload(...)+pos(...)`, not after.
    """
    _, columns = _columns_for(scope)
    if columns is None:
        return 0
    return _rows_for(columns, dedup=dedup, around=index)[1]


def rank_rows(columns: Columns, dedup: bool = True) -> list[Row]:
    """Newest first, optionally collapsing repeats.

    Takes the whole `Columns` tuple rather than four named lists, so the
    preview's extras are ranked by the *same* function and cannot be ordered
    differently from what the picker shows -- and so that this reads like its
    neighbours `_gather`, `_zipped` and `_window_around`, which all take one.
    The exit status and the host ride along so the
    display can colour by one and label by the other. Deduplication
    keeps the *most recent* run, so a command that failed last time is shown as
    failed even if it has succeeded a hundred times before -- which is the way
    round that helps.
    """
    ordered = sorted(_zipped(columns), key=_STAMP, reverse=True)
    if not dedup:
        return ordered

    # Sort, then collapse. Collapsing first with a dict -- so the sort sees the
    # 23,797 rows that get shown rather than all 54,804 -- was tried and
    # measured no better: the extra pass costs what the smaller sort saves.
    seen: set[str] = set()
    add = seen.add
    return [row for row in ordered if not (row[1] in seen or add(row[1]))]


def render_rows(
    rows: list[Row],
    now: int | None = None,
    colour: bool = False,
    host_width: int = 0,
) -> list[str]:
    """Format ranked rows as display lines.

    With `colour`, a command that exited non-zero is dimmed red. Safe only
    because `make_inert` has already removed every C0 byte -- ESC among them --
    from the command on its way into the cache, so nothing a peer published can
    add escapes of its own. That is also why fzf is given `--ansi` only here:
    without inert text, `--ansi` would turn another machine's history into
    something that can drive this terminal. `host_label` does the same job for
    the machine name, which arrives by the same route and is *not* made inert
    where it is stored.

    With `host_width`, each line names the machine that ran the command. Which
    is also how you filter by one: fzf matches from the second field on, so the
    name is matched and the relative time still is not.

    fzf hands back the line with the escapes stripped, so `command_from_line`
    only has to know the width.
    """
    if now is None:
        now = int(time.time())
    labels = host_labels({row[3] for row in rows}) if host_width else {}

    out = []
    # Unpacked, not sliced, even though a `Row` can be seven fields wide: the
    # wide ones are built by `detail` and go to `_block`, and nothing routes
    # them here -- `lines_for` is the only caller and it asks `_columns_for` for
    # four. A `[:4]` that tolerated them would be a guard against a call that
    # does not exist, and would read as if one did.
    for ts, cmd, code, host in rows:
        when = f"{relative_time(ts, now):>{_TIME_WIDTH}}"
        if colour and is_failure(code):
            # The age column only. Colouring the whole line was the first
            # version and was reported as "a bit much" -- on a history with any
            # real proportion of failures it is most of the screen in red, and a
            # mark that covers half the lines has stopped marking anything.
            #
            # Wrapped *after* the padding, so the escapes are outside the width
            # and every column still lines up. fzf renders them as zero-width
            # and strips them from what it hands back, so `command_from_line`
            # slices the same fixed prefix it always did.
            when = f"{_FAILED}{when}{_RESET}"
        if host_width:
            line = f"{when}  {labels[host]:<{host_width}}  {escape(cmd)}"
        else:
            line = f"{when}  {escape(cmd)}"
        out.append(line)
    return out


def host_width_for(hosts: set[str]) -> int:
    """How wide the machine column should be, or 0 for no column at all.

    Nothing to say on a machine whose history is all its own, which is every
    single-machine install and the whole of the README's install section -- so the
    column simply is not there, rather than being there and empty.
    """
    if len(hosts) < 2:
        return 0
    return max(len(label) for label in host_labels(hosts).values())


def _self_command() -> str:
    """How to re-invoke woswoar from inside fzf's ``reload`` binding."""
    import shutil

    found = shutil.which("woswoar")
    if found:
        return found
    return f"{sys.executable} -m woswoar"


#: The fzf that gained the `transform` action, which Ctrl-R cycling and the
#: Ctrl-T timeline are both built on.
TRANSFORM_SINCE = (0, 45)


def fzf_version() -> tuple[str, tuple[int, int] | None]:
    """What `fzf --version` says, and the (major, minor) parsed out of it.

    Both halves, because they answer different questions: `doctor` shows a
    person the string fzf printed, and the gate below compares numbers. Deriving
    one from the other at each call site is how they would come to disagree.

    The version is `None` when fzf is absent or says something unparseable, and
    that reads as "too old" everywhere -- the safe direction, since an unknown
    *action* in a `--bind` makes fzf refuse to start, so a wrong guess costs the
    picker entirely rather than one key.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["fzf", "--version"], capture_output=True, text=True, timeout=5, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "", None
    digits = out.split()[0].split(".") if out else []
    try:
        return out, (int(digits[0]), int(digits[1]))
    except (IndexError, ValueError):
        return out, None


#: What an fzf below `TRANSFORM_SINCE` silently does not get. One list, because
#: `_fzf_argv` withholds them and `doctor` has to name them, and the way those
#: two part company is a machine being told it is missing two keys while three
#: are gone -- which is exactly what happened when the preview joined the gate.
#: Written as phrases rather than key names because `doctor` reads them out in a
#: sentence and that is the only place they are rendered.
GATED_KEYS: tuple[str, ...] = (
    "ctrl-r cycling",
    "the ctrl-t timeline",
    f"the {PREVIEW_KEY} details pane",
)


def fzf_supports_transform() -> bool:
    """Whether this fzf has the `transform` action, added in 0.45.

    Needed because an unknown *action* in a `--bind` makes fzf refuse to start,
    so this cannot be offered optimistically -- an older fzf would get no picker
    at all rather than no Ctrl-R cycling. One `fzf --version`, on a path that is
    already forking fzf.
    """
    _, parsed = fzf_version()
    return parsed is not None and parsed >= TRANSFORM_SINCE


def _scope_case(var: str, answer: dict[Scope, Scope], fallback: str = SCOPES[0]) -> str:
    """An ``sh`` ``case`` that reads the current scope out of fzf's prompt.

    fzf holds no variables, so both keys that need to know the scope read it
    back out of the prompt fzf is already showing -- which is why
    `change-prompt` is not cosmetic. One emitter for both, because the arms are
    exactly `SCOPES` and enumerating them by hand is how one goes missing: the
    cycle's `session` arm was absent and *correct anyway*, falling through the
    catch-all to `global`, right up until `dir` was appended after it. Nothing
    would have said so -- an arm that is missing looks exactly like an arm whose
    answer happens to be the default.

    The catch-all is `SCOPES[0]` for the two keys that *reload*: an unset or
    unrecognised prompt then shows the global list, which is a list you can see
    and whose prompt says what it is. The preview passes ``fallback=""`` instead,
    because there the same guess is unfalsifiable -- a block describing the wrong
    row looks exactly like a block describing the right one. `FZF_PROMPT` is
    read here on the assumption that fzf exports it, and no version is known to
    hang that on, so the failure has to be the visible kind.

    **The prompt these read must stay exactly `woswoar (<scope>) `.** The
    patterns are substring matches over the whole of it, so a prompt naming the
    directory -- `woswoar (dir ~/src/session-manager)` -- matches `*session*`
    first and silently answers with the wrong scope. It costs nothing to put a
    path in a prompt and there is no error when it goes wrong, which is why it
    is written down here and pinned by a test.
    """
    arms = "".join(f"*{scope}*) {var}={answer[scope]} ;; " for scope in SCOPES)
    return f'case "$FZF_PROMPT" in {arms}*) {var}={fallback} ;; esac; '


#: Where each scope goes when Ctrl-R is pressed: on to the next, and round.
#: Appending to `SCOPES` therefore extends the cycle and changes no existing
#: step of it.
_NEXT: dict[Scope, Scope] = {
    scope: SCOPES[(index + 1) % len(SCOPES)] for index, scope in enumerate(SCOPES)
}


#: Hidden until asked for, and that is not a default worth revisiting: the
#: preview forks an interpreter and reads the whole cache on every cursor move.
#: Measured on the 52,000-command history in `tests/test_perf.py`, one render is
#: **79 ms** against 85 ms for the whole of `woswoar list` -- it skips the render
#: of 23,000 lines and pays for everything else, and neither term is
#: optimisable here: one is process startup and the other is reading a 5 MB
#: cache. That is paid *per row the cursor passes over*. fzf coalesces to the
#: latest request, so an always-on pane degrades to visible lag under a held
#: arrow key rather than to a queue -- but a user who never presses
#: `PREVIEW_KEY` should pay nothing at all, and hidden is how they do.
#:
#: `wrap` because this is the one place with room to show a long command whole;
#: the picker's own line is clipped at the window edge.
PREVIEW_WINDOW = "--preview-window=hidden,down,45%,border-top,wrap"

#: What the preview says when it could not tell which list it is looking at.
#: See `_scope_case`: the alternative is a confidently wrong record.
PREVIEW_UNKNOWN = "preview needs a newer fzf"

#: fzf substitutes every `{...}` in the command it is about to run, and a
#: `transform` *is* such a command -- so a `{n}` that is meant for the preview
#: the transform prints has to survive that pass. `man fzf`: "you can escape a
#: placeholder pattern by prepending a backslash". The backslash is consumed
#: there, a bare `{n}` reaches `change-preview`, and it stays a live template
#: that fzf re-expands on every cursor move.
#:
#: Measured against fzf 0.73.1 rather than taken from the manual, which
#: documents the escape but not what becomes of it inside a transform's output:
#: `TestThePreviewUnderARealFzf` drives a real picker through a pty and watches
#: the pane renumber itself as the cursor moves.
_ESCAPED_N = r"\{n}"

#: Read the scope back and answer with itself. `_NEXT`'s counterpart, named for
#: the same reason: the two callers that want "whatever is on screen, unchanged"
#: were spelling the dict inline.
_SAME: dict[Scope, Scope] = {scope: scope for scope in SCOPES}


def _preview_command(
    self_cmd: str, scope: str, dedup_flag: str, row: str = "{n}", around: str = ""
) -> str:
    """The command that renders one row's block into the preview pane.

    ``scope`` is a literal for the four per-scope key binds, which know it up
    front, and a shell variable `_scope_case` set for the three that cannot --
    the `--preview` the picker opens with, and the two `transform` keys, all of
    which have to keep meaning the right thing after a switch they did not make.
    It arrives quoted (``"$s"``) for the first of those, because that one is a
    bare command line where the shell would word-split an empty value; inside
    the two `printf`s it is already inside quotes.

    ``row`` is fzf's index of the highlighted item and is left as a template on
    purpose; see `_ESCAPED_N` for the callers that have to spell it differently.

    ``around`` is what makes the preview agree with a timeline. It is the same
    flag `lines_for` takes, and for the same reason: under a timeline the list
    fzf holds is a *window*, so `row` indexes that window and resolving it
    against the ranked list would describe some entirely unrelated command.

    ``--colour`` is the same flag the display line is rendered with, meaning the
    same thing: a terminal is watching. It is passed rather than assumed because
    `--show` is a command line like any other, and `woswoar list --show 0 | cat`
    should no more contain escapes than `woswoar list` does.
    """
    return f"{self_cmd} list --colour --show {row} --scope {scope}{dedup_flag}{around}"


def _preview_binding(self_cmd: str, dedup_flag: str) -> str:
    """The pane showing what the display line has no width for.

    `cwd`, `duration_ms` and `session` are recorded, encrypted, synced and
    cached, and until this there was no way for anyone to read them back. They
    do not fit on the line -- it is a fixed-width prefix that `command_from_line`
    slices rather than parses -- and a pane costs no width at all, because it
    only ever describes the one row under the cursor.
    """
    show = _preview_command(self_cmd, '"$s"', dedup_flag)
    script = (
        _scope_case("s", _SAME, fallback="")
        + f'if [ -n "$s" ]; then {show}; else printf "%s\\n" "{PREVIEW_UNKNOWN}"; fi'
    )
    return f"--preview={script}"


def _list_command(self_cmd: str, scope: str, dedup_flag: str, width: str, around: str = "") -> str:
    """The `woswoar list` the picker reloads itself from.

    One spelling, because both sides of the picker have to lay a line out
    identically: `command_from_line` slices at a fixed width decided when it
    opened, so a binding that reloaded without `--colour` or with a different
    `--host-width` would cut somebody's recalled command in the wrong place.
    Three bindings and one key loop write this, and `--host-width` was already
    once a flag that had to be added to each of them by hand.
    """
    return f"{self_cmd} list --colour{width} --scope {scope}{dedup_flag}{around}"


def _switch_to(
    self_cmd: str,
    scope: str,
    dedup_flag: str,
    width: str,
    row: str = "{n}",
    preview: bool = True,
) -> str:
    """Every action a key needs to move the picker to one scope, in one string.

    The list, the prompt and the preview are three statements of a single fact,
    and the prompt is not decoration: `_scope_case` reads the scope back *out*
    of it, so a key that repainted only two of the three would leave the next
    keypress reading a scope the screen no longer shows. Emitting them together
    is what stops them disagreeing.

    The preview is included because these keys are also the way *out* of a
    timeline, whose pane is pinned to one `--around` window; left alone it would
    keep describing rows of a list that is no longer there -- and describing
    them plausibly, which is the whole hazard of this feature.

    ``row`` is `_ESCAPED_N` for a caller whose output fzf expands first.

    ``preview`` is False on an fzf that was never offered a pane. Not only
    because there would be nothing to repoint: `change-preview` arrived in fzf
    0.31 and `transform` in 0.45, so a version below the gate may not know the
    *action name* either -- and an unknown action in a `--bind` makes fzf refuse
    to start, which costs the picker rather than the key.
    """
    actions = (
        f"reload({_list_command(self_cmd, scope, dedup_flag, width)})"
        f"+change-prompt(woswoar ({scope}) )"
    )
    if preview:
        actions += f"+change-preview({_preview_command(self_cmd, scope, dedup_flag, row=row)})"
    return actions


def _cycle_binding(self_cmd: str, dedup_flag: str, width: str) -> str:
    """Ctrl-R inside the picker moves to the next scope.

    The order is `SCOPES`, which is the order the header names them in.

    `$n` rather than a literal, and `_ESCAPED_N` rather than `{n}`: this is the
    same switch the four direct keys make, but composed by a shell fzf runs
    first, so the scope is a variable and the preview's placeholder has to
    survive that pass.
    """
    script = _scope_case("n", _NEXT) + (
        f'printf "{_switch_to(self_cmd, "$n", dedup_flag, width, row=_ESCAPED_N)}"'
    )
    return f"--bind=ctrl-r:transform:{script}"


def _header(host_width: int) -> str:
    """The line above the prompt: what the keys do, and how to pick a machine.

    `^box` restricts a search to the machine called `box`, and has since the
    column existed -- `--nth=2..` starts the searched region at the machine
    name, and fzf anchors `^` to the start of that region rather than of the
    line. Nothing said so, which made it a feature only its author knew about:
    reported as "I have a host named box and that is short enough to also be
    part of a few commands", which is the exact case it solves.

    Only with a column to filter on. On a single-machine install there is no
    machine name in the line and the hint would describe nothing.

    Ctrl-R, Ctrl-T and the preview key are listed only where they work: all
    three ride on `transform`, which is fzf 0.45+, and advertising a key that
    does nothing is worse than staying quiet about it.

    Ordered by how far each takes you from an ordinary search: change what is
    listed, then change the *kind* of list, then narrow the one you have, then
    look harder at the one row you are on.
    """
    if fzf_supports_transform():
        # Naming the scopes in the order Ctrl-R visits them is what lets the
        # first three direct keys collapse into one segment: `g`, `h` and `s`
        # are the initials of the words right beside them, so spelling each out
        # again was three quarters of the line saying the same thing twice.
        #
        # `ctrl-o` is the odd one out and so is spelled: `o` is not the initial
        # of `dir`, and a reader who has to work out which key belongs to which
        # word has lost more than the four characters this costs. Written by
        # hand for that reason -- the compaction is editorial, and a fifth scope
        # may well want a differently shaped sentence rather than another comma.
        #
        # `ctrl-` in full rather than the usual `^r`, because `^` already means
        # something else on this line: `^name` is fzf's anchor, not a key.
        hints = [
            "ctrl-r global \u2192 host \u2192 session \u2192 dir, or ctrl-g/h/s, ctrl-o dir",
            "ctrl-t timeline",
            # Last, because it is the only one that changes nothing about the
            # list -- and because a key nobody presses costs nothing, which is
            # the whole reason the pane starts hidden.
            f"{PREVIEW_KEY} details",
        ]
    else:
        # Neither key exists here, and with no cycle to name the scopes the
        # four that do have to introduce themselves. Derived from `KEYS`, unlike
        # the sentence above: this one is a plain rendering of the bindings, and
        # writing it out again is how a header comes to advertise a key that
        # nothing binds.
        hints = [f"ctrl-{KEYS[scope]} {scope}" for scope in SCOPES]
    if host_width:
        hints.append("^name one machine")
    return " | ".join(hints)


def _timeline_binding(self_cmd: str, dedup_flag: str, width: str) -> str:
    """Ctrl-T shows what happened either side of the highlighted command.

    `{n}` is fzf's index of that item, and the list it indexes into is the one
    `woswoar list` just produced -- so the anchor needs nothing added to the
    line and the display format is untouched.

    The scope is read back out of the prompt by the same emitter the cycle uses,
    which here answers with the scope it found rather than the next one. It is
    carried into the timeline's own prompt (`woswoar (timeline host)`) so that
    pressing this again recentres without silently changing scope, and so that
    ctrl-g/h/s/o/r still read a scope out of it on the way back out.

    The preview is repointed at the same window in the same breath, and it is
    the one part of this that is silently wrong if forgotten: fzf would go on
    running a preview command that resolves `{n}` against the *ranked* list
    while the screen shows a timeline, so the pane would describe a command
    forty rows from the highlighted one and look entirely plausible doing it.
    """
    # Not `_switch_to`: every one of its three actions is different here --
    # `reload-sync` rather than `reload`, a prompt that says `timeline`, and a
    # preview pinned to this window. What they have in common is only the shape.
    window = _list_command(self_cmd, "$s", dedup_flag, width, around=" --around {n}")
    preview = _preview_command(self_cmd, "$s", dedup_flag, row=_ESCAPED_N, around=" --around {n}")
    script = (
        _scope_case("s", _SAME)
        # One extra invocation, on a keypress rather than on the prompt path:
        # `pos` has to be composed into the same action string as the `reload`
        # it applies to, so it cannot be read out of the reload's own output.
        + f"p=$({window} --print-anchor); "
        # `reload-sync`, not `reload`: a plain reload is asynchronous, so `pos`
        # ran against the list that was still on screen and the cursor was
        # wherever the new one happened to leave it. Reported as "it seems to
        # jump to the bottom always and not to the selection".
        #
        # `clear-query` because the query that found the command is not the one
        # you want against its neighbours -- leaving it filters the timeline
        # down to the same match you started from, which is an empty gesture.
        # Asked for directly: "it should clear the filter, that way it's
        # filtering the timeline".
        + f'printf "clear-query+reload-sync({window})'
        f"+pos($p)+change-prompt(woswoar (timeline $s) )"
        f'+change-preview({preview})"'
    )
    return f"--bind=ctrl-t:transform:{script}"


def _fzf_argv(scope: Scope, query: str, dedup: bool, host_width: int) -> list[str]:
    self_cmd = _self_command()
    dedup_flag = "" if dedup else " --no-dedup"
    # Passed to the reloads rather than recomputed by them. Both sides must lay
    # the line out identically or the recalled command is sliced in the wrong
    # place, and a peer's history arriving between the picker opening and a
    # scope switch would otherwise change one side's answer and not the other's.
    width = f" --host-width {host_width}"

    argv = [
        "fzf",
        # Safe here and only here: `render_rows` and `_block` write the escapes
        # and `make_inert` guarantees no command can contain any of its own.
        # It covers the pane as well as the list -- verified against a real fzf
        # rather than assumed, since the manual describes this flag in terms of
        # the input stream: dim and bold both arrive at the terminal, re-emitted
        # by fzf as its own SGR.
        "--ansi",
        "--height=60%",
        "--layout=reverse",
        "--border",
        "--no-multi",
        # Match against the command only. Without this, typing "3d" would match
        # the relative-time column and surface unrelated entries.
        "--nth=2..",
        # Preserve our newest-first order when match scores tie. `begin` used to
        # come first here, and it did not preserve anything: for a query like
        # "sync" every candidate scores the same, so `begin` -- not recency --
        # decided the whole list, ranking a year-old `sudo sync; echo 3 > ...`
        # above a three-minute-old `woswoar sync` purely because its match
        # starts three characters earlier in the line.
        #
        # It also leaked the time column into the ranking. fzf scores `begin` as
        # (match offset - leading whitespace), and the column is right-aligned,
        # so a two-character age is padded with one more space than a
        # three-character one: `1y  atuin sync -h` outranked `10h  atuin sync`
        # with the offsets otherwise identical. `index` is what the intent was.
        "--tiebreak=index",
        f"--query={query}",
        f"--prompt=woswoar ({scope}) ",
        f"--header={_header(host_width)}",
    ]
    # Asked once and reused, because it forks `fzf --version`.
    transform = fzf_supports_transform()
    if transform:
        argv.append(_cycle_binding(self_cmd, dedup_flag, width))
        argv.append(_timeline_binding(self_cmd, dedup_flag, width))
        # The preview rides on the same gate, though nothing in it needs
        # `transform` as such. Two reasons, and the second is the one that
        # decides it: it reads `$FZF_PROMPT`, which no version is known to have
        # introduced, so an fzf old enough to lack it would get the fail-closed
        # message on every row rather than a preview -- and an unknown key name
        # in a `--bind` makes fzf refuse to start, so offering `ctrl-/` to a
        # version that predates it costs the picker entirely, not the key.
        argv.append(_preview_binding(self_cmd, dedup_flag))
        argv.append(PREVIEW_WINDOW)
        argv.append(f"--bind={PREVIEW_KEY}:toggle-preview")
    for target in SCOPES:
        # The same switch the cycle makes, with the scope known up front rather
        # than read out of the prompt -- and `{n}` unescaped, because this is a
        # plain action and not a `transform` whose command line fzf expands
        # first. Without `transform` there is no pane to repoint, so the
        # `change-preview` is dropped rather than pointed at nothing.
        actions = _switch_to(self_cmd, target, dedup_flag, width, preview=transform)
        argv.append(f"--bind=ctrl-{KEYS[target]}:{actions}")
    return argv


def interactive(scope: Scope, query: str = "", dedup: bool = True) -> str | None:
    """Run the picker. Returns the chosen command, or ``None`` if cancelled."""
    # `woswoar list` is the fzf reload target and runs on every scope switch, so
    # it must not pay for subprocess/shutil at import time just because its
    # module also contains the interactive path.
    import shutil
    import subprocess

    if shutil.which("fzf") is None:
        from . import deps

        print(deps.report([deps.FZF]), file=sys.stderr)
        print("\nMeanwhile 'woswoar list' prints the same history as plain text.", file=sys.stderr)
        return None

    # Nothing recorded at all -- a fresh install before the first command. The
    # check is two stats on a machine that *has* history, because the cache is
    # the first thing it finds, and it is what keeps "Ctrl-R does nothing yet"
    # from becoming "Ctrl-R opens an empty picker you have to escape out of".
    if not store.cache_file().exists() and not any(store.iter_log_files()):
        return None

    # fzf is started *before* the history is built, which is the whole point:
    # it paints its frame and prompt in about two milliseconds, and then reads
    # stdin as it arrives -- exactly how `find | fzf` behaves. Building the
    # lines first meant the screen stayed empty for the entire load-parse-sort-
    # render, measured at 125 ms on a real 55k-command history. The work is the
    # same either way; what changes is that you are looking at the picker while
    # it happens instead of at nothing.
    host_width = host_width_for(set(store.host_names()))
    process = subprocess.Popen(
        _fzf_argv(scope, query, dedup, host_width),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None

    try:
        _feed(
            process.stdin,
            lines_for(scope, dedup=dedup, colour=True, host_width=host_width),
        )
    finally:
        # Both closes can raise the same way, and neither is a failure: it only
        # means fzf is already gone. Selecting the first line the moment it
        # appears, or pressing Esc, does exactly that.
        with contextlib.suppress(BrokenPipeError, OSError):
            process.stdin.close()

    selected = process.stdout.read()
    process.stdout.close()
    returncode = process.wait()

    # 1 = no match, 130 = interrupted with Esc or Ctrl-C. Both are ordinary
    # outcomes, not failures.
    if returncode != 0 or not selected.strip():
        return None
    return command_from_line(selected.rstrip("\n"), host_width)


#: Lines per write into fzf. Large enough that the write syscalls are not the
#: cost, small enough that a `BrokenPipeError` from an early selection is
#: noticed rather than buffered behind a megabyte.
_CHUNK = 2000


def _feed(stream: IO[str], lines: list[str]) -> None:
    """Write the display lines into fzf, tolerating it leaving early."""
    with contextlib.suppress(BrokenPipeError):
        for start in range(0, len(lines), _CHUNK):
            stream.write("\n".join(lines[start : start + _CHUNK]))
            stream.write("\n")
        stream.flush()
