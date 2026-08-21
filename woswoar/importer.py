"""Import an existing shell history: bash, zsh, or atuin.

Onboarding is the whole point: a history tool that starts empty is useless on
day one. Imports are idempotent, so re-running after a few weeks picks up only
what is new.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Literal, NamedTuple

from . import credentials, store
from .entry import Entry, home_relative, truncate

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
    #: Rows dropped because an identical (second, command) was already taken
    #: during *this* run -- distinct from `skipped`, which means it was already
    #: on disk. Reporting them as one made a first-ever import into an empty
    #: store claim entries were "already present".
    collapsed: int = 0
    #: Dropped because the command looks like it carries a secret. Reported
    #: rather than silent: a user who expects a command to be there needs to
    #: know why it is not, and "the filter ate it" is a different answer from
    #: "your history file did not have it".
    credentials: int = 0
    #: The dropped commands themselves, populated only for a dry run. Dropping
    #: is irreversible and a count alone is unauditable -- "17 skipped" over a
    #: decade of history gives nobody a way to spot a false positive. Never
    #: filled on a real import, so a secret cannot reach a caller that stores
    #: its Result.
    dropped: tuple[str, ...] = ()
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
    """Parse ``.zsh_history``, **in the order the records appear in the file**.

    Extended history looks like ``: <start>:<elapsed>;<command>``. A command
    continues onto the next line when the current one ends in a backslash.
    Plain (non-extended) history has no header and is handled like untimed bash.

    File order is not a detail here, it is the whole of #289. `run` remembers how
    far it got as a **count of records**, and slices that count off the front on
    the next import -- so the order this returns has to be one that only ever
    *grows at the end*. Timestamp order is not: a shell writes its history when
    it exits, so two shells closed in the other order interleave, and a record
    that sorts below the watermark is skipped on that run and on every run after.

    The previous shape had a second way to reorder, which is why this is one loop
    rather than two. Untimed records were collected and appended after the timed
    ones, so a newly timed record pushed every untimed one down a slot -- enough
    on its own to drop a command even without the sort.

    It sorted, too, and returned that. Nothing wanted it: `run` was the only
    caller in shipped code. A public parser handing back timestamp order is the
    footgun that made #289 -- the next caller reaches for the obvious name and
    inherits the ordering the watermark cannot survive -- so both parsers now
    return file order, and a caller that wants time order sorts and says so.
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

    kept = [record for record in records if record.strip()]
    # Counted in a pass of its own because `_synthetic` is told up front how
    # many stamps to mint. The generator is then consumed in encounter order,
    # so every untimed record gets the same stamp it did before #289 -- what
    # changed is where it sits in the list, not what it holds.
    synthetic = _synthetic(mtime, sum(1 for r in kept if _ZSH_HEADER.match(r) is None))

    rows: list[Parsed] = []
    for record in kept:
        match = _ZSH_HEADER.match(record)
        if match is None:
            rows.append(Parsed(ts=next(synthetic), cmd=record, duration_ms=-1))
            continue
        elapsed = int(match.group(2))
        # Continuation backslashes are zsh's line-wrapping, not part of the
        # command.
        cmd = match.group(3).replace("\\\n", "\n")
        rows.append(
            Parsed(
                ts=int(match.group(1)),
                cmd=cmd,
                duration_ms=elapsed * 1000 if elapsed > 0 else -1,
            )
        )
    return rows


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
    # Imported here rather than at module scope so that merely *naming* an
    # importer stays cheap. The CLI has to reach this module to build its
    # `import` subparser, which every Ctrl-R does, and only this one of the
    # three sources needs a database.
    import sqlite3

    if not path.is_file():
        raise FileNotFoundError(f"no atuin database at {path}")

    try:
        db = sqlite3.connect(_readonly_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise FileNotFoundError(f"cannot open {path} read-only: {exc}") from exc

    # atuin has no encoding guarantee -- a mistyped paste or a filename in some
    # other charset lands in the table verbatim, and sqlite3's default strict
    # decode raises mid-iteration. Losing a character beats losing the import,
    # which is the same trade the bash and zsh readers already make.
    db.text_factory = lambda raw: raw.decode("utf-8", errors="replace")

    try:
        try:
            for ts_ns, duration_ns, exit_code, cmd, cwd, session, hostname in db.execute(
                _ATUIN_QUERY
            ):
                yield AtuinRow(
                    ts=int(ts_ns) // _NS_PER_SECOND,
                    # atuin writes -1 when it never saw the command finish,
                    # which happens to be woswoar's own "unknown" value too.
                    duration_ms=int(duration_ns) // _NS_PER_MS if duration_ns >= 0 else -1,
                    exit_code=int(exit_code),
                    cmd=cmd,
                    cwd="" if cwd == _ATUIN_UNKNOWN_CWD else cwd,
                    # Opaque either way, and repeated on every line; 14 hex
                    # chars matches what the shell hook writes.
                    session=hashlib.blake2b(session.encode(), digest_size=7).hexdigest(),
                    hostname=hostname,
                )
        except sqlite3.Error as exc:
            # Wraps the whole loop, not just execute(): sqlite reports a bad
            # schema on execute but a bad *row* only once iteration reaches it.
            raise FileNotFoundError(f"cannot read {path} as an atuin database: {exc}") from exc
    finally:
        db.close()


def _readonly_uri(path: Path) -> str:
    """A sqlite URI that cannot be misread as carrying query parameters.

    Interpolating the path directly is unsafe: a ``#`` or ``?`` in it truncates
    the filename, which drops ``mode=ro`` and makes sqlite happily *create* a
    new writable database at the truncated name -- the exact opposite of the
    guarantee this function exists to provide.
    """
    return f"{path.resolve().as_uri()}?mode=ro"


def display_name(hostname: str) -> str:
    """atuin's ``host:user`` as a woswoar machine label.

    The DNS domain is dropped to match :func:`store.default_machine_name`,
    which uses only the first component of the nodename. Without that, the same
    machine seen as ``mbp.local`` and as ``mbp`` would be two different hosts --
    and one's *own* history would be filed as a foreign machine, invisible to
    ``--scope host`` and never published by sync.
    """
    host, _, user = hostname.partition(":")
    host = host.split(".")[0]
    return f"{user}@{host}" if user else host


def resolve_hosts(hostnames: Iterable[str]) -> dict[str, tuple[str, str]]:
    """Map each atuin hostname to a woswoar (host id, display name).

    Resolved once per distinct hostname rather than per row: there are a
    handful of machines and tens of thousands of rows, and this reads config
    and calls ``uname``.

    Order matters:

    1. This machine keeps its real id, so imported history sits alongside what
       the hook records and syncs normally.
    2. A machine already known locally -- because its history has synced in --
       reuses *its* id. Otherwise the same peer would exist twice, under its
       real random id and under a derived one, with the same label and no way
       to reconcile them.
    3. Anything else gets an id derived from the label, so a second import
       lands in the same place instead of forking a duplicate machine.
    """
    known = store.machine()
    own = {store.default_machine_name(), known.name}
    existing = {name: host_id for host_id, name in store.host_names().items()}

    resolved: dict[str, tuple[str, str]] = {}
    for hostname in hostnames:
        display = display_name(hostname)
        if display in own:
            resolved[hostname] = (known.id, known.name)
        elif display in existing:
            resolved[hostname] = (existing[display], display)
        else:
            resolved[hostname] = (
                hashlib.blake2b(display.encode(), digest_size=8).hexdigest(),
                display,
            )
    return resolved


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
    """The per-source watermarks, or nothing if the file cannot be read as them.

    Degrading rather than raising, as `sync.State.load` does and for the reason
    rule 8 gives: this runs on the way into every import, so one unconvertible
    value in an otherwise valid JSON file would make `woswoar import` traceback
    until somebody deleted the file. Starting from nothing costs a re-read of
    each source, and `(ts, cmd)` deduplication absorbs it -- no command is lost
    and none is duplicated. See #291.
    """
    try:
        return {k: int(v) for k, v in store.load_json(_state_path()).items()}
    except (TypeError, ValueError, AttributeError):
        return {}


def _save_state(state: dict[str, int]) -> None:
    # Atomic: a half-written watermark file would make the next import either
    # duplicate everything or skip entries silently.
    store.save_json(_state_path(), state)


def run_atuin(
    path: Path | None = None, dry_run: bool = False, this_host_only: bool = False
) -> Result:
    """Import atuin's sqlite history, preserving which machine ran what.

    Idempotency is by ``(timestamp, command)`` per host rather than by a
    position watermark: atuin backfills *older* rows whenever it syncs from
    another machine, so "everything after the last row I saw" would silently
    miss them.
    """
    source = path or atuin_db()
    home = str(Path.home())
    own_id = store.machine().id

    by_hostname: dict[str, list[AtuinRow]] = {}
    parsed = 0
    for row in _atuin_rows(source):
        parsed += 1
        by_hostname.setdefault(row.hostname, []).append(row)

    # Once per machine, not once per row.
    resolved = resolve_hosts(by_hostname)

    by_host: dict[str, list[AtuinRow]] = {}
    names: dict[str, str] = {}
    for hostname, rows in by_hostname.items():
        host_id, display = resolved[hostname]
        if this_host_only and host_id != own_id:
            continue
        names[host_id] = display
        by_host.setdefault(host_id, []).extend(rows)

    entries_by_host: dict[str, list[Entry]] = {}
    skipped = 0
    collapsed = 0
    secrets = 0
    dropped: list[str] = []
    extra = credentials.user_pattern()

    for host_id, rows in by_host.items():
        days = {store.day_for(r.ts) for r in rows}
        on_disk = store.existing_keys(host_id, days)
        known = set(on_disk)
        fresh: list[Entry] = []
        for row in rows:
            key = _dedup_key(row.ts, row.cmd)
            if key in known:
                # Two different things wearing the same skip. Already on disk
                # means a previous import; otherwise it is a same-second,
                # same-command row that woswoar's whole-second resolution
                # cannot tell apart from the one already taken this run.
                if key in on_disk:
                    skipped += 1
                else:
                    collapsed += 1
                continue
            # key[1] is the truncated command -- see the note in `run`.
            if credentials.looks_like_credential(key[1], extra):
                secrets += 1
                if dry_run:
                    dropped.append(row.cmd)
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
                    cwd=home_relative(row.cwd, home) if host_id == own_id else row.cwd,
                    exit_code=row.exit_code,
                    duration_ms=row.duration_ms,
                    cmd=row.cmd,
                )
            )
        entries_by_host[host_id] = fresh

    # Deferred, as `sqlite3` is above and for the same reason: `__main__`
    # imports this module at the top for the parser, so a keypress would
    # otherwise pay 295 us of module body for a command it is not running.
    from . import forget

    # The other door into `logs/`. This deduplicates against
    # `store.existing_keys`, which reads the log files -- so a row `forget` took
    # out is no longer seen as already present, and would be written back
    # verbatim by the next run. See `forget.keep` for what it can and cannot
    # recognise.
    entries_by_host = {host: forget.keep(rows) for host, rows in entries_by_host.items()}

    imported = sum(len(v) for v in entries_by_host.values())
    if not dry_run:
        for host_id, entries in entries_by_host.items():
            if entries:
                store.append_entries(host_id, entries)
            # Written even when nothing was imported, so a run interrupted
            # between the append and the label can be repaired by re-importing.
            # Guarded only against overwriting a name learned from a real sync.
            label = store.name_file(host_id)
            if not label.is_file():
                store.write_atomic(label, f"{names[host_id]}\n".encode())

    return Result(
        parsed=parsed,
        imported=imported,
        skipped=skipped,
        collapsed=collapsed,
        credentials=secrets,
        dropped=tuple(dropped),
        source=source,
        per_host=tuple(
            sorted(
                ((names[h], len(e)) for h, e in entries_by_host.items() if e),
                key=lambda pair: -pair[1],
            )
        ),
    )


def run(
    kind: Kind,
    path: Path | None = None,
    dry_run: bool = False,
    this_host_only: bool = False,
) -> Result:
    """Import a history file, skipping anything already imported.

    Idempotency uses two independent guards. A per-source count handles the
    untimed case, where synthesised timestamps shift as the source file grows
    and so cannot identify an entry. A ``(ts, cmd)`` check handles everything
    else, and covers the count going stale if the source is rotated or trimmed.
    """
    if kind == "atuin":
        return run_atuin(path, dry_run=dry_run, this_host_only=this_host_only)

    source = path or default_path(kind)
    if not source.is_file():
        raise FileNotFoundError(f"no history file at {source}")

    mtime = int(source.stat().st_mtime)
    text = _read(source)
    # Both parsers return file order and neither sorts, which is not a
    # coincidence but the contract the watermark below rests on. See #289.
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
    on_disk = store.existing_keys(machine_id, days)
    known = set(on_disk)

    entries: list[Entry] = []
    skipped = 0
    collapsed = 0
    secrets = 0
    dropped: list[str] = []
    extra = credentials.user_pattern()
    for item in fresh:
        # Not `key`: that name already holds the per-source state key below.
        seen = _dedup_key(item.ts, item.cmd)
        if seen in known:
            if seen in on_disk:
                skipped += 1
            else:
                collapsed += 1
            continue
        # Before the dedup bookkeeping is committed but after the cheap checks:
        # a credential-shaped line must never become an Entry, because from
        # there it goes to logs/, into an encrypted chunk, into git, and out to
        # every machine -- and docs/security.md says publication is final.
        #
        # `seen[1]`, not `item.cmd`: the truncated text is what would be
        # published, and several rules scan with `[^|;&]*`, which is quadratic
        # in the length of the line. A 97 KB pasted script measured 636ms
        # untruncated and 5ms truncated -- and `_dedup_key` computed the
        # truncation on the line above regardless, so this is free.
        if credentials.looks_like_credential(seen[1], extra):
            secrets += 1
            if dry_run:
                dropped.append(item.cmd)
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

    # As in `run_atuin` above, deferred for the same reason: `forget` has to
    # survive a re-import, and this is the other writer into `logs/`.
    from . import forget

    entries = forget.keep(entries)

    if not dry_run:
        store.append_entries(machine_id, entries)
        state[key] = len(parsed)
        _save_state(state)

    return Result(
        parsed=len(parsed),
        imported=len(entries),
        skipped=skipped,
        collapsed=collapsed,
        credentials=secrets,
        dropped=tuple(dropped),
        source=source,
    )
