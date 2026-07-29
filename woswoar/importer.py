"""Import an existing bash or zsh history.

Onboarding is the whole point: a history tool that starts empty is useless on
day one. Imports are idempotent, so re-running after a few weeks picks up only
what is new.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, NamedTuple

from . import store
from .entry import Entry

Kind = Literal["bash", "zsh"]
KINDS: tuple[Kind, ...] = ("bash", "zsh")

SESSION = "import"

_BASH_TIMESTAMP = re.compile(r"^#(\d{9,})$")
_ZSH_HEADER = re.compile(r"^: (\d+):(\d+);(.*)$", re.DOTALL)

#: Spacing for synthesised timestamps, in seconds. See :func:`_synthetic`.
_SYNTHETIC_STEP = 1


def _synthetic(mtime: int, count: int) -> Iterator[int]:
    """``count`` timestamps a step apart, the last landing on ``mtime``.

    Used for history files that carry no timestamps at all. The absolute values
    are a fiction -- they compress years into hours -- but the *order* is real,
    and order is what ranking and deduplication depend on.
    """
    return (mtime - (count - 1 - i) * _SYNTHETIC_STEP for i in range(count))


class Parsed(NamedTuple):
    ts: int
    cmd: str
    duration_ms: int


class Result(NamedTuple):
    parsed: int
    imported: int
    skipped: int
    source: Path


def default_path(kind: Kind) -> Path:
    return Path.home() / (".bash_history" if kind == "bash" else ".zsh_history")


def _read(path: Path) -> str:
    # Histories routinely contain bytes that are not valid UTF-8 (a mistyped
    # paste, a binary filename). Losing one character beats losing the import.
    return path.read_text(encoding="utf-8", errors="replace")


def parse_bash(text: str, mtime: int) -> list[Parsed]:
    """Parse ``.bash_history``.

    With ``HISTTIMEFORMAT`` set, bash writes a ``#<epoch>`` line before each
    command. Without it there are no timestamps at all, so :func:`_synthetic`
    supplies them.
    """
    commands: list[tuple[int | None, str]] = []
    pending_ts: int | None = None

    for line in text.splitlines():
        match = _BASH_TIMESTAMP.match(line)
        if match:
            pending_ts = int(match.group(1))
            continue
        if not line.strip():
            continue
        commands.append((pending_ts, line))
        pending_ts = None

    synthetic = _synthetic(mtime, sum(1 for ts, _ in commands if ts is None))
    return [
        Parsed(ts=next(synthetic) if ts is None else ts, cmd=cmd, duration_ms=-1)
        for ts, cmd in commands
    ]


def parse_zsh(text: str, mtime: int) -> list[Parsed]:
    """Parse ``.zsh_history``.

    Extended history looks like ``: <start>:<elapsed>;<command>``. A command
    continues onto the next line when the current one ends in a backslash.
    Plain (non-extended) history has no header and is handled like untimed bash.
    """
    records: list[str] = []
    current: list[str] = []

    for line in text.splitlines():
        if current:
            current.append(line)
        else:
            current = [line]

        if line.endswith("\\"):
            continue
        records.append("\n".join(current))
        current = []

    if current:
        records.append("\n".join(current))

    parsed: list[Parsed] = []
    untimed: list[str] = []

    for record in records:
        if not record.strip():
            continue
        match = _ZSH_HEADER.match(record)
        if match is None:
            untimed.append(record)
            continue
        elapsed = int(match.group(2))
        # Continuation backslashes are zsh's line-wrapping, not part of the
        # command.
        cmd = match.group(3).replace("\\\n", "\n")
        parsed.append(
            Parsed(
                ts=int(match.group(1)),
                cmd=cmd,
                duration_ms=elapsed * 1000 if elapsed > 0 else -1,
            )
        )

    synthetic = _synthetic(mtime, len(untimed))
    parsed.extend(Parsed(ts=next(synthetic), cmd=cmd, duration_ms=-1) for cmd in untimed)

    parsed.sort(key=lambda p: p.ts)
    return parsed


def _state_path() -> Path:
    return store.config_dir() / "imported.json"


def _load_state() -> dict[str, int]:
    return {k: int(v) for k, v in store.load_json(_state_path()).items()}


def _save_state(state: dict[str, int]) -> None:
    # Atomic: a half-written watermark file would make the next import either
    # duplicate everything or skip entries silently.
    store.save_json(_state_path(), state)


def run(kind: Kind, path: Path | None = None, dry_run: bool = False) -> Result:
    """Import a history file, skipping anything already imported.

    Idempotency uses two independent guards. A per-source count handles the
    untimed case, where synthesised timestamps shift as the source file grows
    and so cannot identify an entry. A ``(ts, cmd)`` check handles everything
    else, and covers the count going stale if the source is rotated or trimmed.
    """
    source = path or default_path(kind)
    if not source.is_file():
        raise FileNotFoundError(f"no history file at {source}")

    mtime = int(source.stat().st_mtime)
    text = _read(source)
    parsed = parse_bash(text, mtime) if kind == "bash" else parse_zsh(text, mtime)

    state = _load_state()
    key = str(source)
    already = state.get(key, 0)
    if already > len(parsed):
        # Source was rotated or truncated; the count means nothing now.
        already = 0

    fresh = parsed[already:]
    machine_id = store.machine().id
    days = {store.day_for(p.ts) for p in fresh}
    known = store.existing_keys(machine_id, days)

    entries: list[Entry] = []
    skipped = 0
    for item in fresh:
        if (item.ts, item.cmd) in known:
            skipped += 1
            continue
        known.add((item.ts, item.cmd))
        entries.append(
            Entry(
                ts=item.ts,
                host=machine_id,
                session=SESSION,
                cwd="",
                exit_code=-1,
                duration_ms=item.duration_ms,
                cmd=item.cmd,
            )
        )

    if not dry_run:
        store.append_entries(machine_id, entries)
        state[key] = len(parsed)
        _save_state(state)

    return Result(parsed=len(parsed), imported=len(entries), skipped=skipped, source=source)
