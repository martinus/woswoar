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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from typing import IO

from . import cache, store
from .entry import escape, make_inert, unescape

Scope = Literal["global", "host", "session"]
SCOPES: tuple[Scope, ...] = ("global", "host", "session")

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

#: One display row: when, what, how it ended, and which machine ran it.
Row = tuple[int, str, str, str]

#: Plain red rather than the dim red this started as. Dim was the right choice
#: while the whole line carried it -- a screen of dim red is legible where a
#: screen of bright red is not -- but it now marks the age column alone, and
#: dim red on three characters is close to not being there at all.
_FAILED = "\x1b[31m"
_RESET = "\x1b[0m"


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


#: The four parallel columns a display line is built from.
Columns = tuple[list[str], list[str], list[str], list[str]]


def _columns_for(scope: Scope) -> tuple[cache.Cache, Columns | None]:
    """The rows a scope is about, straight out of the cache's columns.

    Split out of `lines_for` so a timeline can start from exactly the same set
    -- asking the same question twice and getting different answers is how the
    anchor `{n}` names would stop meaning what fzf thinks it means.
    """
    loaded = cache.load_columns()
    if scope == "host":
        return loaded, loaded.display_columns({store.machine().id})
    if scope == "session":
        session = os.environ.get("WOSWOAR_SESSION", "")
        if not session:
            # Not an error: this is what a shell without the hook loaded looks like.
            return loaded, None
        stamps, commands, codes, hosts = loaded.display_columns()
        # The one column that is per entry rather than per file, so it is the
        # one scope that cannot be answered by dropping whole files.
        wanted = [i for i, value in enumerate(loaded.sessions()) if value == session]
        return loaded, (
            [stamps[i] for i in wanted],
            [commands[i] for i in wanted],
            [codes[i] for i in wanted],
            [hosts[i] for i in wanted],
        )
    return loaded, loaded.display_columns()


def _rows_for(columns: Columns, dedup: bool, around: int | None) -> tuple[list[Row], int]:
    """The rows to display, and where the cursor belongs among them.

    The second value is 1-based for fzf's `pos()` and is 0 when there is no
    anchor to sit on -- which is every ordinary list, and also a timeline whose
    anchor has gone.
    """
    stamps, commands, codes, hosts = columns
    ranked = rank_rows(stamps, commands, codes, hosts, dedup=dedup)
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

    `session` is the exception -- it is per entry, not per file -- so that scope
    fetches one more column. `host` needs none: it is a property of the file,
    so the cache filters whole files out before a single row is looked at.
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
    stamps, commands, codes, hosts = columns
    chronological = sorted(
        zip((int(s) for s in stamps), commands, codes, hosts, strict=True),
        key=_STAMP,
    )
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


def rank_rows(
    stamps: list[str],
    commands: list[str],
    codes: list[str],
    hosts: list[str],
    dedup: bool = True,
) -> list[Row]:
    """Newest first, optionally collapsing repeats.

    The exit status and the host ride along so the display can colour by one and
    label by the other. Deduplication
    keeps the *most recent* run, so a command that failed last time is shown as
    failed even if it has succeeded a hundred times before -- which is the way
    round that helps.
    """
    # One `int` per row, here, where the sort needs it -- rather than on every
    # entry of a history at parse time.
    ordered = sorted(
        zip(map(int, stamps), commands, codes, hosts, strict=True), key=_STAMP, reverse=True
    )
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
    single-machine install and the whole of the README's Quick Start -- so the
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


def fzf_supports_transform() -> bool:
    """Whether this fzf has the `transform` action, added in 0.45.

    Needed because an unknown *action* in a `--bind` makes fzf refuse to start,
    so this cannot be offered optimistically -- an older fzf would get no picker
    at all rather than no Ctrl-R cycling. One `fzf --version`, on a path that is
    already forking fzf.
    """
    _, parsed = fzf_version()
    return parsed is not None and parsed >= TRANSFORM_SINCE


def _cycle_binding(self_cmd: str, dedup_flag: str, width: str) -> str:
    """Ctrl-R inside the picker moves to the next scope.

    fzf holds no variables, so the current scope is read back out of the prompt
    it is already showing -- which is why `change-prompt` is not cosmetic. The
    order matches the header: global, host, session, round again.
    """
    reload = f"{self_cmd} list --colour{width} --scope"
    script = (
        'case "$FZF_PROMPT" in '
        "*global*) n=host ;; "
        "*host*) n=session ;; "
        "*) n=global ;; "
        "esac; "
        f'printf "reload({reload} $n{dedup_flag})+change-prompt(woswoar ($n) )"'
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

    Ctrl-R and Ctrl-T are listed only where they work: both need `transform`,
    which is fzf 0.45+, and advertising a key that does nothing is worse than
    staying quiet about it.

    Ordered by how far each takes you from an ordinary search: change what is
    listed, then change the *kind* of list, then narrow the one you have.
    """
    if fzf_supports_transform():
        # Naming the scopes in the order Ctrl-R visits them is what lets the
        # three direct keys collapse into one segment: `g`, `h` and `s` are the
        # initials of the words right beside them, so spelling each out again
        # was three quarters of the line saying the same thing twice.
        #
        # `ctrl-` in full rather than the usual `^r`, because `^` already means
        # something else on this line: `^name` is fzf's anchor, not a key.
        hints = ["ctrl-r global \u2192 host \u2192 session, or ctrl-g/h/s", "ctrl-t timeline"]
    else:
        # Neither key exists here, and with no cycle to name the scopes the
        # three that do have to introduce themselves.
        hints = ["ctrl-g global", "ctrl-h host", "ctrl-s session"]
    if host_width:
        hints.append("^name one machine")
    return " | ".join(hints)


def _timeline_binding(self_cmd: str, dedup_flag: str, width: str) -> str:
    """Ctrl-T shows what happened either side of the highlighted command.

    `{n}` is fzf's index of that item, and the list it indexes into is the one
    `woswoar list` just produced -- so the anchor needs nothing added to the
    line and the display format is untouched.

    The scope is read back out of the prompt, exactly as `_cycle_binding` does
    and for the same reason: fzf holds no variables. It is carried into the
    timeline's own prompt (`woswoar (timeline host)`) so that pressing this
    again recentres without silently changing scope, and so that ctrl-g/h/s/r
    still read a scope out of it on the way back out.
    """
    reload = f"{self_cmd} list --colour{width} --scope"
    script = (
        'case "$FZF_PROMPT" in '
        "*global*) s=global ;; "
        "*host*) s=host ;; "
        "*session*) s=session ;; "
        "*) s=global ;; "
        "esac; "
        # One extra invocation, on a keypress rather than on the prompt path:
        # `pos` has to be composed into the same action string as the `reload`
        # it applies to, so it cannot be read out of the reload's own output.
        f"p=$({reload} $s{dedup_flag} --around {{n}} --print-anchor); "
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
        f'printf "clear-query+reload-sync({reload} $s{dedup_flag} --around {{n}})'
        '+pos($p)+change-prompt(woswoar (timeline $s) )"'
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
        # Safe here and only here: `render_rows` writes the escapes and
        # `make_inert` guarantees no command can contain any of its own.
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
    if fzf_supports_transform():
        argv.append(_cycle_binding(self_cmd, dedup_flag, width))
        argv.append(_timeline_binding(self_cmd, dedup_flag, width))
    for key, target in (("ctrl-g", "global"), ("ctrl-h", "host"), ("ctrl-s", "session")):
        argv.append(
            f"--bind={key}:reload({self_cmd} list --colour{width} --scope {target}{dedup_flag})"
            f"+change-prompt(woswoar ({target}) )"
        )
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
