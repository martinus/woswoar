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

#: Every display line is ``"<4-char relative time><2 spaces><escaped command>"``.
#: The prefix is a fixed width so recovering the command from what fzf prints
#: back is an exact slice rather than a parse.
_TIME_WIDTH = 4
_PREFIX = _TIME_WIDTH + 2

#: Sort key for a row. In C, rather than a lambda.
_STAMP = itemgetter(0)

#: One display row: when, what, and how it ended.
Row = tuple[int, str, str]

#: Dim red for a command that failed. Dim rather than plain red so a screen of
#: failures does not shout, and so it reads as "this one did not work" rather
#: than as an error message woswoar is producing now.
_FAILED = "\x1b[2;31m"
_RESET = "\x1b[0m"


def relative_time(ts: int, now: int | None = None) -> str:
    """Render a timestamp as an age, at most :data:`_TIME_WIDTH` characters.

    Computed at display time rather than stored, so it never goes stale.
    """
    delta = (int(time.time()) if now is None else now) - ts
    if delta < 0:
        return "now"
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    if delta < 86400 * 30:
        return f"{delta // 86400}d"
    if delta < 86400 * 350:
        # Cut over before the month count could reach 12, which would be both
        # wider than the column and sillier than saying "1y".
        return f"{delta // (86400 * 30)}mo"
    years = max(1, delta // (86400 * 365))
    return f"{years}y" if years < 100 else "old"


def command_from_line(line: str) -> str:
    """Recover the original command from a rendered line."""
    return make_inert(unescape(line[_PREFIX:]))


def lines_for(
    scope: Scope, dedup: bool = True, limit: int | None = None, colour: bool = False
) -> list[str]:
    """The full pipeline: load, filter, rank, render.

    The two columns a line is made of come straight out of the cache, without
    an `Entry` in between. On a real 54,804-command history that is 39 ms of
    parsing rather than 17: five of the seven fields on an entry are never
    displayed, and building namedtuples to hold them was most of what Ctrl-R
    waited for.

    `session` is the exception -- it is per entry, not per file -- so that scope
    fetches one more column. `host` needs none: it is a property of the file,
    so the cache filters whole files out before a single row is looked at.
    """
    if scope == "host":
        loaded = cache.load_columns()
        stamps, commands = loaded.stamps_and_commands({store.machine().id})
        codes = [
            code
            for relpath, flat in loaded.files.items()
            if loaded.meta[relpath].host == store.machine().id
            for code in flat[3::6]
        ]
    elif scope == "session":
        session = os.environ.get("WOSWOAR_SESSION", "")
        if not session:
            # Not an error: this is what a shell without the hook loaded looks like.
            return []
        loaded = cache.load_columns()
        stamps, commands = loaded.stamps_and_commands()
        every = loaded.exit_codes()
        wanted = [i for i, value in enumerate(loaded.sessions()) if value == session]
        stamps = [stamps[i] for i in wanted]
        commands = [commands[i] for i in wanted]
        codes = [every[i] for i in wanted]
    else:
        loaded = cache.load_columns()
        stamps, commands = loaded.stamps_and_commands()
        codes = loaded.exit_codes()

    rows = rank_rows(stamps, commands, codes, dedup=dedup)
    if limit is not None:
        rows = rows[:limit]
    return render_rows(rows, colour=colour)


def rank_rows(
    stamps: list[str], commands: list[str], codes: list[str], dedup: bool = True
) -> list[Row]:
    """Newest first, optionally collapsing repeats.

    The exit status rides along so the display can colour by it. Deduplication
    keeps the *most recent* run, so a command that failed last time is shown as
    failed even if it has succeeded a hundred times before -- which is the way
    round that helps.
    """
    # One `int` per row, here, where the sort needs it -- rather than on every
    # entry of a history at parse time.
    ordered = sorted(zip(map(int, stamps), commands, codes, strict=True), key=_STAMP, reverse=True)
    if not dedup:
        return ordered

    # Sort, then collapse. Collapsing first with a dict -- so the sort sees the
    # 23,797 rows that get shown rather than all 54,804 -- was tried and
    # measured no better: the extra pass costs what the smaller sort saves.
    seen: set[str] = set()
    add = seen.add
    return [row for row in ordered if not (row[1] in seen or add(row[1]))]


def render_rows(rows: list[Row], now: int | None = None, colour: bool = False) -> list[str]:
    """Format ranked rows as display lines.

    With `colour`, a command that exited non-zero is dimmed red. Safe only
    because `make_inert` has already removed every C0 byte -- ESC among them --
    from the command on its way into the cache, so nothing a peer published can
    add escapes of its own. That is also why fzf is given `--ansi` only here:
    without inert text, `--ansi` would turn another machine's history into
    something that can drive this terminal.

    fzf hands back the line with the escapes stripped, so `command_from_line`
    needs to know nothing about any of this.
    """
    if now is None:
        now = int(time.time())
    out = []
    for ts, cmd, code in rows:
        line = f"{relative_time(ts, now):>{_TIME_WIDTH}}  {escape(cmd)}"
        out.append(f"{_FAILED}{line}{_RESET}" if colour and code not in ("0", "") else line)
    return out


def _self_command() -> str:
    """How to re-invoke woswoar from inside fzf's ``reload`` binding."""
    import shutil

    found = shutil.which("woswoar")
    if found:
        return found
    return f"{sys.executable} -m woswoar"


def _fzf_argv(scope: Scope, query: str, dedup: bool) -> list[str]:
    self_cmd = _self_command()
    dedup_flag = "" if dedup else " --no-dedup"

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
        "--header=ctrl-g global | ctrl-h host | ctrl-s session",
    ]
    for key, target in (("ctrl-g", "global"), ("ctrl-h", "host"), ("ctrl-s", "session")):
        argv.append(
            f"--bind={key}:reload({self_cmd} list --colour --scope {target}{dedup_flag})"
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
    process = subprocess.Popen(
        _fzf_argv(scope, query, dedup),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None

    try:
        _feed(process.stdin, lines_for(scope, dedup=dedup, colour=True))
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
    return command_from_line(selected.rstrip("\n"))


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
