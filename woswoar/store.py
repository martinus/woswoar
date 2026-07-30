"""Filesystem layout, machine identity, and log file access.

The plaintext logs under ``$WOSWOAR_DIR/logs`` are the working copy and the only
thing milestone 1 touches. ``history/`` (the git working tree, ciphertext only)
and ``state.json`` belong to sync and mirror this same ``hosts/<id>/`` shape,
which is why the layout is defined here in one place.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

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


def read_tail(path: Path, offset: int) -> tuple[bytes, int]:
    """Bytes from ``offset`` onwards, truncated to the last complete line.

    Returns the data and the new consumed offset. A partially written final
    line -- a shell killed mid-append -- is deliberately left unconsumed, so it
    is picked up correctly once its writer finishes it.

    Shared by the two pipelines that read these same physical files: the parse
    cache, and sync's export. They must agree on where a file "ends", and for
    sync the stakes are higher -- a half-sealed record would be committed to an
    append-only repo where it could never be fixed.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return b"", offset

    cut = data.rfind(b"\n")
    if cut < 0:
        return b"", offset
    return data[: cut + 1], offset + cut + 1


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read a small JSON state file, tolerating absence and corruption.

    These files are progress markers, never the source of truth: losing one
    costs redundant work, so falling back beats raising.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default or {})
    return raw if isinstance(raw, dict) else dict(default or {})


def save_json(path: Path, payload: object) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    write_atomic(path, text.encode("utf-8"))


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


def signing_key_file() -> Path:
    """This machine's private signing key. Never leaves the machine."""
    return config_dir() / "signing_key"


def signer_public(machine_id: str) -> Path:
    """A host's signing *public* key, published in the repo.

    Being in the repo, this is only a claim -- anyone who can push can rewrite
    it. What makes it trustworthy is that each machine pins the value it was
    shown when it first trusted that host; see ``sync.State.signers``.
    """
    return repo_host_dir(machine_id) / "signer.pub"


#: ssh-keygen's armour is self-delimiting and ends with this line, so a chunk
#: can carry its signature in front of the ciphertext and still be split apart
#: without a length field or a format version.
_SIGNATURE_END = b"-----END SSH SIGNATURE-----\n"


def frame_chunk(sealed: bytes, signature: str) -> bytes:
    """One chunk file: the signature over ``sealed``, then ``sealed`` itself.

    A header rather than a sibling ``.sig`` file, which is what this was first
    written as. The sibling broke everything that answers "what is a chunk":
    `is_chunk_path` classified it as rewritable, ``*.age`` in `GITATTRIBUTES`
    did not match it, and the CI invariant that no chunk is ever modified
    filtered on ``.age`` and so stopped covering half the committed bytes. It
    also doubled the file count that `sync.compact` exists to hold down, for a
    signature that is *larger* than a typical chunk -- 306 bytes against 260.

    Framing costs nothing the sibling was buying: `split_chunk` slices the
    ciphertext back out in memory, so verification still happens before a byte
    reaches age or zlib.
    """
    return signature.encode("utf-8") + sealed


def split_chunk(blob: bytes) -> tuple[bytes, str]:
    """Inverse of :func:`frame_chunk`: ``(sealed, signature)``.

    Raises :class:`ValueError` on anything that is not framed, which callers
    treat exactly like a signature that does not verify -- an unsigned chunk
    and an unparseable one are the same refusal.
    """
    marker = blob.find(_SIGNATURE_END)
    if marker < 0:
        raise ValueError("chunk carries no signature")
    cut = marker + len(_SIGNATURE_END)
    return blob[cut:], blob[:cut].decode("utf-8", errors="replace")


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
    """``hosts/<id>/2026-07-29`` -- one directory per day.

    Sharding by day keeps any directory to a day's worth of chunks, but the
    date is *one* path component rather than three. Every commit rewrites the
    tree object for each level it touches, so nesting ``2026/07/29`` costs two
    extra tree objects on every single sync forever, for a directory that holds
    exactly as many entries either way. Magnitudes are in
    docs/woswoar_design_summary.md, which is re-measured as a whole; repeating
    them here is how they go stale.
    """
    return repo_host_dir(machine_id) / day


def new_chunk(machine_id: str, day: str, ts: int) -> Path:
    """A chunk path that has never existed before.

    Zero-padded seconds sort chronologically as strings, which is what lets the
    merge watermark be a plain string comparison.

    Uniqueness is a guarantee, not a probability. A collision would silently
    overwrite an already-sealed chunk, destroying committed history in a design
    whose whole premise is that chunks are written once and never modified --
    entropy alone left that to chance, which
    tests/test_sync.py::TestChunkNaming now pins. Checking the filesystem is
    sound because both writers of a host's chunks, `sync.run` and
    `sync.compact`, hold the same lock.
    """
    directory = chunk_dir(machine_id, day)
    while True:
        path = directory / f"{ts:010d}-{secrets.token_hex(3)}{_CHUNK_SUFFIX}"
        if not path.exists():
            return path


class Chunk(NamedTuple):
    day: str
    #: Filename only. Sorts chronologically, and is what the watermark stores.
    name: str
    path: Path


def iter_chunks(machine_id: str) -> Iterator[Chunk]:
    """Every sealed chunk belonging to one host, oldest first."""
    root = repo_host_dir(machine_id)
    if not root.is_dir():
        return
    for day_dir in sorted(p for p in root.iterdir() if p.is_dir() and _is_day(p.name)):
        for path in sorted(day_dir.glob(f"*{_CHUNK_SUFFIX}")):
            yield Chunk(day=day_dir.name, name=path.name, path=path)


def _is_day(name: str) -> bool:
    """``2026-07-29``. Distinguishes day directories from ``keys``."""
    parts = name.split("-")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


def iter_day_keys(machine_id: str) -> Iterator[Path]:
    """Every sealed day key belonging to one host.

    Exists so callers never have to spell out the keys directory or the chunk
    suffix themselves -- those are this module's to know, and a hand-typed copy
    would silently stop matching rather than fail loudly if the layout changed.
    """
    keys = repo_host_dir(machine_id) / _KEYS
    if not keys.is_dir():
        return
    yield from sorted(keys.glob(f"*{_CHUNK_SUFFIX}"))


def is_chunk_path(relpath: str) -> bool:
    """Whether a repo-relative path is a write-once chunk.

    Key material and name seals live outside this shape and are deliberately
    rewritable, so telling them apart is a property of the layout rather than
    something a caller should pattern-match for itself.
    """
    parts = relpath.split("/")
    return (
        len(parts) == 4
        and parts[0] == _HOSTS
        and _is_day(parts[2])
        and parts[3].endswith(_CHUNK_SUFFIX)
    )


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
