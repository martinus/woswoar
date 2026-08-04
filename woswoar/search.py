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

#: One display row: when, what, how it ended, and which machine ran it.
Row = tuple[int, str, str, str]

#: Dim red for a command that failed. Dim rather than plain red so a screen of
#: failures does not shout, and so it reads as "this one did not work" rather
#: than as an error message woswoar is producing now.
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


def lines_for(
    scope: Scope,
    dedup: bool = True,
    limit: int | None = None,
    colour: bool = False,
    host_width: int | None = None,
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
    loaded = cache.load_columns()
    if scope == "host":
        stamps, commands, codes, hosts = loaded.display_columns({store.machine().id})
    elif scope == "session":
        session = os.environ.get("WOSWOAR_SESSION", "")
        if not session:
            # Not an error: this is what a shell without the hook loaded looks like.
            return []
        stamps, commands, codes, hosts = loaded.display_columns()
        # The one column that is per entry rather than per file, so it is the
        # one scope that cannot be answered by dropping whole files.
        wanted = [i for i, value in enumerate(loaded.sessions()) if value == session]
        stamps = [stamps[i] for i in wanted]
        commands = [commands[i] for i in wanted]
        codes = [codes[i] for i in wanted]
        hosts = [hosts[i] for i in wanted]
    else:
        stamps, commands, codes, hosts = loaded.display_columns()

    rows = rank_rows(stamps, commands, codes, hosts, dedup=dedup)
    if limit is not None:
        rows = rows[:limit]
    if host_width is None:
        host_width = host_width_for({meta.host for meta in loaded.meta.values() if meta.host})
    return render_rows(rows, colour=colour, host_width=host_width)


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


def fzf_supports_transform() -> bool:
    """Whether this fzf has the `transform` action, added in 0.45.

    Needed because an unknown *action* in a `--bind` makes fzf refuse to start,
    so this cannot be offered optimistically -- an older fzf would get no picker
    at all rather than no Ctrl-R cycling. One `fzf --version`, on a path that is
    already forking fzf.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["fzf", "--version"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    digits = out.strip().split()[0].split(".") if out.strip() else []
    try:
        major, minor = int(digits[0]), int(digits[1])
    except (IndexError, ValueError):
        return False
    return (major, minor) >= (0, 45)


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
        "--header=ctrl-r cycles | ctrl-g global | ctrl-h host | ctrl-s session",
    ]
    if fzf_supports_transform():
        argv.append(_cycle_binding(self_cmd, dedup_flag, width))
    else:
        # Say what is on offer, rather than advertising a key that does nothing.
        argv[-1] = "--header=ctrl-g global | ctrl-h host | ctrl-s session"
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
