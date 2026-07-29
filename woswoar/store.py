"""Filesystem layout, machine identity, and log file access.

The plaintext logs under ``$WOSWOAR_DIR/logs`` are the working copy and the only
thing milestone 1 touches. ``history/`` (the git working tree, ciphertext only)
and ``state.json`` belong to sync and mirror this same ``hosts/<id>/`` shape,
which is why the layout is defined here in one place.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from .entry import Entry, format_line, parse_line

_LOGS = "logs"
_HISTORY = "history"
_HOSTS = "hosts"
_KEYS = "keys"
_LOG_SUFFIX = ".tsv"
_CHUNK_SUFFIX = ".age"
_NAME_FILE = ".name"

RECIPIENTS = "recipients.txt"
GITATTRIBUTES = ".gitattributes"

#: `recipients.txt` is the one file every machine appends to, so it is also the
#: only place a merge conflict could arise. A union merge resolves it: the file
#: is an unordered set of public keys, and keeping both sides is always right.
GITATTRIBUTES_CONTENT = f"{RECIPIENTS} merge=union\n*{_CHUNK_SUFFIX} -diff -merge -text\n"


class Machine(NamedTuple):
    """This machine's identity in the history repo."""

    #: Opaque random hex. Used as the directory name so that a synced repo does
    #: not publish usernames and hostnames in cleartext path components -- the
    #: file *contents* are encrypted, but paths never are.
    id: str
    #: Human-readable label, shown in search results. Local only; shared with
    #: other machines as an encrypted ``name.age``.
    name: str
    #: Identity used to open anything sealed to this machine. Either an existing
    #: SSH private key or a dedicated age identity; chosen once by `init`,
    #: because age cannot prompt for a passphrase during an unattended sync.
    identity: str = ""


def _xdg(env_var: str, default: str) -> Path:
    value = os.environ.get(env_var)
    base = Path(value) if value else Path.home() / default
    return base / "woswoar"


def data_dir() -> Path:
    """Where logs (and later the git tree) live."""
    override = os.environ.get("WOSWOAR_DIR")
    if override:
        return Path(override)
    return _xdg("XDG_DATA_HOME", ".local/share")


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def cache_file() -> Path:
    return cache_dir() / "cache.pickle"


def logs_dir() -> Path:
    return data_dir() / _LOGS


def host_dir(machine_id: str) -> Path:
    return logs_dir() / _HOSTS / machine_id


def name_file(machine_id: str) -> Path:
    """Local, plaintext friendly name for a host. Learned from ``name.age``."""
    return host_dir(machine_id) / _NAME_FILE


def day_for(ts: int) -> str:
    """The ``YYYY-MM-DD`` bucket a timestamp belongs to.

    Local time, deliberately: the shell hook uses ``printf '%(%F)T'``, which is
    also local. The two must agree or a day would be split across two files.
    """
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def log_file(machine_id: str, day: str) -> Path:
    return host_dir(machine_id) / f"{day}{_LOG_SUFFIX}"


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path``'s contents in one step.

    Every file woswoar owns outside the append-only logs is small, rewritten
    whole, and load-bearing if it survives half-written: the parse cache, the
    import watermarks, and the sync state. They all go through here so
    a crash mid-write leaves the previous version rather than a corrupt one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        # Never leave the scratch file next to the real one.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_kv(path: Path) -> dict[str, str]:
    """Parse a trivial ``key=value`` file, tolerating a missing one."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def machine() -> Machine:
    """Return this machine's identity, generating it on first use.

    The id is written once and never changes; renaming the host later changes
    only the display name, so history stays attributed to the same machine.
    """
    values = _read_kv(machine_file())
    machine_id = values.get("id")
    name = values.get("name")
    identity = values.get("identity", "")

    if machine_id and name:
        return Machine(id=machine_id, name=name, identity=identity)

    known = Machine(
        id=machine_id or secrets.token_hex(8),
        name=name or default_machine_name(),
        identity=identity,
    )
    save_machine(known)
    return known


def machine_file() -> Path:
    return config_dir() / "machine"


def save_machine(known: Machine) -> None:
    path = machine_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"id={known.id}", f"name={known.name}"]
    if known.identity:
        lines.append(f"identity={known.identity}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Mirrored next to the logs so search can label entries without reading
    # config, and so sync has a single file to seal and share.
    name_file = host_dir(known.id) / _NAME_FILE
    name_file.parent.mkdir(parents=True, exist_ok=True)
    name_file.write_text(f"{known.name}\n", encoding="utf-8")


def default_machine_name() -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
    host = os.uname().nodename.split(".")[0]
    return f"{user}@{host}"


def host_names() -> dict[str, str]:
    """Map machine id -> friendly name for every host present in the logs.

    Falls back to the opaque id when no ``.name`` file has arrived yet, which is
    the normal state for a freshly cloned machine until it syncs.
    """
    names: dict[str, str] = {}
    root = logs_dir() / _HOSTS
    if not root.is_dir():
        return names

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        name_file = entry / _NAME_FILE
        try:
            names[entry.name] = name_file.read_text(encoding="utf-8").strip() or entry.name
        except OSError:
            names[entry.name] = entry.name
    return names


class LogFile(NamedTuple):
    #: Path relative to ``logs/``, e.g. ``hosts/7f3a9c21/2026-07-29.tsv``.
    #: Used as the cache key, so it must stay stable and platform-independent.
    relpath: str
    host_id: str
    path: Path


def iter_log_files() -> Iterator[LogFile]:
    """Yield every plaintext log file, sorted for reproducible cache builds."""
    root = logs_dir() / _HOSTS
    if not root.is_dir():
        return

    for host in sorted(root.iterdir()):
        if not host.is_dir():
            continue
        for path in sorted(host.glob(f"*{_LOG_SUFFIX}")):
            yield LogFile(
                relpath=f"{_HOSTS}/{host.name}/{path.name}",
                host_id=host.name,
                path=path,
            )


# ---------------------------------------------------------------------------
# The history repo: ciphertext only, and every file in it is write-once.
# ---------------------------------------------------------------------------


def history_dir() -> Path:
    """The git working tree. Contains sealed chunks and nothing readable."""
    return data_dir() / _HISTORY


def recipients_file() -> Path:
    return history_dir() / RECIPIENTS


def state_file() -> Path:
    """Local sync watermarks. Deliberately outside the repo -- it is per-machine
    progress, not shared history, and syncing it would create conflicts."""
    return data_dir() / "state.json"


def repo_host_dir(machine_id: str) -> Path:
    return history_dir() / _HOSTS / machine_id


def name_seal(machine_id: str) -> Path:
    """Sealed friendly name, so other machines can label this host's entries."""
    return repo_host_dir(machine_id) / "name.age"


def day_key(machine_id: str, day: str) -> Path:
    """The day's identity, sealed to every recipient."""
    return repo_host_dir(machine_id) / _KEYS / f"{day}{_CHUNK_SUFFIX}"


def day_key_public(machine_id: str, day: str) -> Path:
    """The day's public key, in the clear.

    Public keys are not secret, and keeping this alongside means writing a chunk
    never has to open the sealed key first.
    """
    return repo_host_dir(machine_id) / _KEYS / f"{day}.pub"


def chunk_dir(machine_id: str, day: str) -> Path:
    """``hosts/<id>/2026/07/29`` -- sharded so no directory holds tens of
    thousands of entries after a couple of years."""
    year, month, mday = day.split("-")
    return repo_host_dir(machine_id) / year / month / mday


def new_chunk(machine_id: str, day: str, ts: int) -> Path:
    """A chunk path that has never existed before.

    Zero-padded seconds sort chronologically as strings, which is what lets the
    merge watermark be a plain string comparison. The random suffix keeps two
    syncs in the same second from colliding.
    """
    return chunk_dir(machine_id, day) / f"{ts:010d}-{secrets.token_hex(2)}{_CHUNK_SUFFIX}"


class Chunk(NamedTuple):
    host_id: str
    day: str
    #: Filename only. Sorts chronologically, and is what the watermark stores.
    name: str
    path: Path


def iter_chunks(machine_id: str) -> Iterator[Chunk]:
    """Every sealed chunk belonging to one host, oldest first."""
    root = repo_host_dir(machine_id)
    if not root.is_dir():
        return
    for year in sorted(p for p in root.iterdir() if p.is_dir() and p.name.isdigit()):
        for month in sorted(p for p in year.iterdir() if p.is_dir()):
            for mday in sorted(p for p in month.iterdir() if p.is_dir()):
                day = f"{year.name}-{month.name}-{mday.name}"
                for path in sorted(mday.glob(f"*{_CHUNK_SUFFIX}")):
                    yield Chunk(host_id=machine_id, day=day, name=path.name, path=path)


def repo_hosts() -> list[str]:
    """Machine ids present in the history repo."""
    root = history_dir() / _HOSTS
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def day_of_log(relpath: str) -> str:
    """``hosts/<id>/2026-07-29.tsv`` -> ``2026-07-29``."""
    return relpath.rsplit("/", 1)[-1].removesuffix(_LOG_SUFFIX)


def append_entries(machine_id: str, entries: list[Entry]) -> dict[str, int]:
    """Append entries to their day files, returning lines written per file.

    Used by the importer. Recording goes through the shell hook instead, which
    never calls into Python.
    """
    by_day: dict[str, list[Entry]] = {}
    for item in entries:
        by_day.setdefault(day_for(item.ts), []).append(item)

    written: dict[str, int] = {}
    for day, group in sorted(by_day.items()):
        path = log_file(machine_id, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        group.sort(key=lambda e: e.ts)
        payload = "".join(f"{format_line(e)}\n" for e in group)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
        written[path.name] = len(group)
    return written


def existing_keys(machine_id: str, days: set[str]) -> set[tuple[int, str]]:
    """``(ts, cmd)`` pairs already recorded on the given days.

    Lets the importer stay idempotent without loading the whole history.
    """
    keys: set[tuple[int, str]] = set()
    for day in days:
        path = log_file(machine_id, day)
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parsed = parse_line(line, machine_id)
                if parsed is not None:
                    keys.add((parsed.ts, parsed.cmd))
    return keys
