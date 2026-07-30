"""Scope filtering, ranking, and the fzf front end.

Division of labour, per the design: Python is the search engine (loading,
filtering, sorting, formatting) and fzf is purely the UI (fuzzy match, display,
select).
"""

from __future__ import annotations

import os
import sys
import time
from operator import attrgetter
from typing import Literal

from . import cache, store
from .entry import Entry, escape, make_inert, unescape

Scope = Literal["global", "host", "session"]
SCOPES: tuple[Scope, ...] = ("global", "host", "session")

#: Every display line is ``"<4-char relative time><2 spaces><escaped command>"``.
#: The prefix is a fixed width so recovering the command from what fzf prints
#: back is an exact slice rather than a parse.
_TIME_WIDTH = 4
_PREFIX = _TIME_WIDTH + 2


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


def filter_scope(entries: list[Entry], scope: Scope) -> list[Entry]:
    """Narrow entries to the requested scope."""
    if scope == "global":
        return entries
    if scope == "host":
        host_id = store.machine().id
        return [e for e in entries if e.host == host_id]

    session = os.environ.get("WOSWOAR_SESSION", "")
    if not session:
        # Not an error: this is what a shell without the hook loaded looks like.
        return []
    return [e for e in entries if e.session == session]


def rank(entries: list[Entry], dedup: bool = True) -> list[Entry]:
    """Sort newest first, optionally collapsing repeats.

    Deduplication keeps the most recent occurrence of each command. It is on by
    default because a real history is mostly repetition -- it is the difference
    between scrolling past twenty ``git status`` lines and seeing one.
    """
    # attrgetter rather than a lambda: it is the same key, resolved in C, and
    # this sorts the whole history on every Ctrl-R.
    ordered = sorted(entries, key=attrgetter("ts"), reverse=True)
    if not dedup:
        return ordered

    seen: set[str] = set()
    unique: list[Entry] = []
    for item in ordered:
        if item.cmd in seen:
            continue
        seen.add(item.cmd)
        unique.append(item)
    return unique


def render(entries: list[Entry], now: int | None = None) -> list[str]:
    """Format entries as fzf input lines.

    The command is re-escaped so one entry is always exactly one line, even if
    the recorded command spanned several.
    """
    if now is None:
        now = int(time.time())
    return [f"{relative_time(e.ts, now):>{_TIME_WIDTH}}  {escape(e.cmd)}" for e in entries]


def command_from_line(line: str) -> str:
    """Recover the original command from a rendered line."""
    return make_inert(unescape(line[_PREFIX:]))


def lines_for(scope: Scope, dedup: bool = True, limit: int | None = None) -> list[str]:
    """The full pipeline: load, filter, rank, render."""
    entries = rank(filter_scope(cache.load_entries(), scope), dedup=dedup)
    if limit is not None:
        entries = entries[:limit]
    return render(entries)


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
        "--height=60%",
        "--layout=reverse",
        "--border",
        "--no-multi",
        # Match against the command only. Without this, typing "3d" would match
        # the relative-time column and surface unrelated entries.
        "--nth=2..",
        # Preserve our newest-first order when match scores tie.
        "--tiebreak=begin,index",
        f"--query={query}",
        f"--prompt=woswoar ({scope}) ",
        "--header=ctrl-g global | ctrl-h host | ctrl-s session",
    ]
    for key, target in (("ctrl-g", "global"), ("ctrl-h", "host"), ("ctrl-s", "session")):
        argv.append(
            f"--bind={key}:reload({self_cmd} list --scope {target}{dedup_flag})"
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

    lines = lines_for(scope, dedup=dedup)
    if not lines:
        return None

    result = subprocess.run(
        _fzf_argv(scope, query, dedup),
        input="\n".join(lines),
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    # 1 = no match, 130 = interrupted with Esc or Ctrl-C. Both are ordinary
    # outcomes, not failures.
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return command_from_line(result.stdout.rstrip("\n"))
