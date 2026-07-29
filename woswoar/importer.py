"""Import an existing shell history: bash, zsh, or atuin.

Onboarding is the whole point: a history tool that starts empty is useless on
day one. Imports are idempotent, so re-running after a few weeks picks up only
what is new.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, NamedTuple

from . import store
from .entry import Entry, truncate

Kind = Literal["bash", "zsh", "atuin"]
KINDS: tuple[Kind, ...] = ("bash", "zsh", "atuin")

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
    #: (display name, count) per machine, biggest first. Only atuin carries
    #: more than one host. A tuple rather than a dict because Result is a
    #: NamedTuple, and a mutable default would be shared across instances.
    per_host: tuple[tuple[str, int], ...] = ()


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


# ---------------------------------------------------------------------------
# atuin
# ---------------------------------------------------------------------------

#: atuin's own placeholder when it did not know the working directory.
_ATUIN_UNKNOWN_CWD = "unknown"

#: atuin stores nanoseconds; woswoar stores whole seconds and milliseconds.
_NS_PER_SECOND = 1_000_000_000
_NS_PER_MS = 1_000_000

_ATUIN_QUERY = """
    SELECT timestamp, duration, exit, command, cwd, session, hostname
    FROM history
    WHERE deleted_at IS NULL AND trim(command) != ''
    ORDER BY timestamp
"""


def atuin_db() -> Path:
    return Path.home() / ".local/share/atuin/history.db"


class AtuinRow(NamedTuple):
    ts: int
    duration_ms: int
    exit_code: int
    cmd: str
    cwd: str
    session: str
    #: atuin's ``hostname:username``.
    hostname: str


def _atuin_rows(path: Path) -> Iterator[AtuinRow]:
    """Stream atuin's history table.

    Opened read-only through a URI, because this is very likely the live
    database of a running atuin: woswoar has no business writing to it, and
    read-only also means we cannot trigger WAL recovery on someone else's file.
    """
    if not path.is_file():
        raise FileNotFoundError(f"no atuin database at {path}")

    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise FileNotFoundError(f"cannot open {path} read-only: {exc}") from exc

    try:
        try:
            cursor = db.execute(_ATUIN_QUERY)
        except sqlite3.Error as exc:
            raise FileNotFoundError(f"{path} does not look like an atuin database: {exc}") from exc

        for ts_ns, duration_ns, exit_code, cmd, cwd, session, hostname in cursor:
            yield AtuinRow(
                ts=int(ts_ns) // _NS_PER_SECOND,
                # atuin writes -1 when it never saw the command finish, which
                # happens to be woswoar's own "unknown" value too.
                duration_ms=int(duration_ns) // _NS_PER_MS if duration_ns >= 0 else -1,
                exit_code=int(exit_code),
                cmd=cmd,
                cwd="" if cwd == _ATUIN_UNKNOWN_CWD else cwd,
                # Opaque either way, and repeated on every line; 14 hex chars
                # matches what the shell hook writes.
                session=hashlib.blake2b(session.encode(), digest_size=7).hexdigest(),
                hostname=hostname,
            )
    finally:
        db.close()


def _host_for(hostname: str) -> tuple[str, str]:
    """Map an atuin ``hostname:username`` to a woswoar (host id, display name).

    atuin syncs every machine's history into one database, so an import can
    carry commands from many hosts. Flattening them into this machine would
    make `--scope host` and `stats` lie, so each gets its own host directory.

    The id is derived from the name rather than random, so re-importing lands
    in the same place. Commands from *this* machine merge into its real id, so
    they sit alongside what the hook records and get synced normally.
    """
    host, _, user = hostname.partition(":")
    display = f"{user}@{host}" if user else host

    if display == store.default_machine_name():
        known = store.machine()
        return known.id, known.name
    return hashlib.blake2b(hostname.encode(), digest_size=8).hexdigest(), display


def _home_relative(cwd: str, home: str) -> str:
    """Match the hook's storage convention for this machine's own paths."""
    if not cwd or not home:
        return cwd
    if cwd == home:
        return "~"
    if cwd.startswith(f"{home}/"):
        return "~" + cwd[len(home) :]
    return cwd


def _dedup_key(ts: int, cmd: str) -> tuple[int, str]:
    """The identity an entry will have *once written*.

    Over-long commands are truncated on the way to disk, so a key built from
    the original text can never match what is already stored -- and the entry
    would be re-imported, and appended again, on every single run. Found by
    importing a real atuin database: three of 54,943 commands were long enough
    to trip it, and they came back every time.
    """
    return (ts, truncate(cmd))


def _state_path() -> Path:
    return store.config_dir() / "imported.json"


def _load_state() -> dict[str, int]:
    return {k: int(v) for k, v in store.load_json(_state_path()).items()}


def _save_state(state: dict[str, int]) -> None:
    # Atomic: a half-written watermark file would make the next import either
    # duplicate everything or skip entries silently.
    store.save_json(_state_path(), state)


def run_atuin(path: Path | None = None, dry_run: bool = False) -> Result:
    """Import atuin's sqlite history, preserving which machine ran what.

    Idempotency is by ``(timestamp, command)`` per host rather than by a
    position watermark: atuin backfills *older* rows whenever it syncs from
    another machine, so "everything after the last row I saw" would silently
    miss them.
    """
    source = path or atuin_db()
    home = str(Path.home())

    by_host: dict[str, list[AtuinRow]] = {}
    names: dict[str, str] = {}
    own_id = store.machine().id
    parsed = 0

    for row in _atuin_rows(source):
        parsed += 1
        host_id, display = _host_for(row.hostname)
        names[host_id] = display
        by_host.setdefault(host_id, []).append(row)

    entries_by_host: dict[str, list[Entry]] = {}
    skipped = 0

    for host_id, rows in by_host.items():
        days = {store.day_for(r.ts) for r in rows}
        known = store.existing_keys(host_id, days)
        fresh: list[Entry] = []
        for row in rows:
            key = _dedup_key(row.ts, row.cmd)
            if key in known:
                skipped += 1
                continue
            known.add(key)
            fresh.append(
                Entry(
                    ts=row.ts,
                    host=host_id,
                    session=row.session,
                    # Only rewrite paths for this machine: another host's home
                    # directory is a guess, and a wrong ~ is worse than a long
                    # but honest absolute path.
                    cwd=_home_relative(row.cwd, home) if host_id == own_id else row.cwd,
                    exit_code=row.exit_code,
                    duration_ms=row.duration_ms,
                    cmd=row.cmd,
                )
            )
        entries_by_host[host_id] = fresh

    imported = sum(len(v) for v in entries_by_host.values())
    if not dry_run:
        for host_id, entries in entries_by_host.items():
            if not entries:
                continue
            store.append_entries(host_id, entries)
            # So search labels these entries with a machine name rather than
            # the opaque id. Never overwrite a name learned from a real sync.
            label = store.name_file(host_id)
            if not label.is_file():
                label.parent.mkdir(parents=True, exist_ok=True)
                label.write_text(f"{names[host_id]}\n", encoding="utf-8")

    return Result(
        parsed=parsed,
        imported=imported,
        skipped=skipped,
        source=source,
        per_host=tuple(
            sorted(
                ((names[h], len(e)) for h, e in entries_by_host.items() if e),
                key=lambda pair: -pair[1],
            )
        ),
    )


def run(kind: Kind, path: Path | None = None, dry_run: bool = False) -> Result:
    """Import a history file, skipping anything already imported.

    Idempotency uses two independent guards. A per-source count handles the
    untimed case, where synthesised timestamps shift as the source file grows
    and so cannot identify an entry. A ``(ts, cmd)`` check handles everything
    else, and covers the count going stale if the source is rotated or trimmed.
    """
    if kind == "atuin":
        return run_atuin(path, dry_run=dry_run)

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
        # Not `key`: that name already holds the per-source state key below.
        seen = _dedup_key(item.ts, item.cmd)
        if seen in known:
            skipped += 1
            continue
        known.add(seen)
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
