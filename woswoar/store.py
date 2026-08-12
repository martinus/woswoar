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
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any, NamedTuple

from .entry import Entry, format_line, parse_line

_LOGS = "logs"
_HISTORY = "history"
_HOSTS = "hosts"
_KEYS = "keys"
_MANIFESTS = "manifests"
_LOG_SUFFIX = ".tsv"
_CHUNK_SUFFIX = ".age"
_PUB_SUFFIX = ".pub"
_NAME_FILE = ".name"

RECIPIENTS = "recipients.txt"
GITATTRIBUTES = ".gitattributes"
GITIGNORE = ".gitignore"
README = "README.md"
FORMAT = "FORMAT"

#: The layout of the *repository* -- `recipients.txt`, `hosts/<id>/keys/`,
#: `manifests/`, `<day>/<ts>-<rand>.age`. Every other format woswoar owns already
#: says which one it is: the cache carries `woswoar-cache-2`, a manifest carries
#: `woswoar-manifest-v1` inside the bytes it signs. The repo was the exception,
#: and it is the one an upgrade cannot be retrofitted onto later -- a machine
#: still running the old version would not write the marker, so by the time a
#: layout change needs one it is too late by definition.
#:
#: Deliberately not `woswoar.__version__`. The program version moved sixteen
#: times in a fortnight and this has moved zero times; coupling them means a
#: meaningless bump on every release and no signal on the one release where it
#: matters.
REPO_FORMAT = 1
_FORMAT_PREFIX = "woswoar-repo-"


def format_content(version: int = REPO_FORMAT) -> str:
    """What the marker says for ``version``. The one place the prefix is spelt.

    Takes a version because the tests need to write one this build does not
    understand, and hand-typing `woswoar-repo-2` there would put a second copy
    of the prefix outside this module -- where it would go on parsing after a
    rename, as `None`, and quietly stop testing the refusal.
    """
    return f"{_FORMAT_PREFIX}{version}\n"


FORMAT_CONTENT = format_content()

#: What a stranger finding the repository sees first, and what its owner sees in
#: three years having forgotten what it was. Deliberately says nothing about
#: whose it is: the host directories are opaque hex precisely so the archive does
#: not name machines, and a README that undid that would be worse than none.
#:
#: Written only when absent, unlike `.gitattributes`, which is compared and
#: rewritten to match. Both are written by `init` rather than by `sync`, so the
#: cost of getting this wrong is bounded -- but a machine on a newer version
#: would still rewrite a peer's wording the next time anyone ran `init`, and the
#: older one would put it back. `.gitattributes` earns that because its content
#: is load-bearing: `merge=union` on `recipients.txt` is what makes two machines
#: enrolling at once conflict-free. Prose does not.
README_CONTENT = """# woswoar history

Encrypted shell history, written by [woswoar](https://github.com/martinus/woswoar).

Every `.age` file here is ciphertext. There is nothing readable in this
repository, including the directory names -- they are random hex so that an
archive of it does not publish which machines exist.

## Do not edit this by hand

Chunks are written once and never rewritten, and each machine signs a list of
what it published each day. A file that no signed list accounts for is refused
by every other machine and reported. Editing or deleting one here does not
remove history -- other machines keep their own copies -- and will make them
report the repository as tampered with.

To take a machine's access away, run `woswoar revoke` from another one. To see
what state this repository is in, run `woswoar doctor`.
"""

#: `recipients.txt` is the one file every machine appends to, so it is also the
#: only place a merge conflict could arise. A union merge resolves it: the file
#: is an unordered set of public keys, and keeping both sides is always right.
GITATTRIBUTES_CONTENT = f"{RECIPIENTS} merge=union\n*{_CHUNK_SUFFIX} -diff -merge -text\n"

#: Finder's per-directory metadata file, by name in the one place that owns the
#: layout. It turns up in `logs/` and in the history checkout alike, so both the
#: privacy walk and the repository's ignore rules are talking about this string.
_FINDER_METADATA = ".DS_Store"

#: What must never be committed, whoever is browsing the checkout. Finder writes
#: a `.DS_Store` into any directory it is asked to display, and this is a git
#: working tree that lives under the user's home -- so one arrives the first time
#: anybody looks at their history in a file manager.
#:
#: Not a tidiness rule. `sync` stages with `git add -A`, so without this the file
#: is committed and pushed to every peer; two machines whose Finders disagree
#: about it then produce a binary conflict on the rebase that `sync` does, in a
#: file no part of woswoar knows anything about. `.gitattributes` cannot express
#: this -- it marks how a tracked file is treated, not whether it is tracked --
#: so it is a second file.
#:
#: The chunk suffix is deliberately *not* here. A chunk that no signed manifest
#: accounts for is a thing woswoar reports; a chunk git silently ignores is one
#: nobody ever sees.
GITIGNORE_CONTENT = f"{_FINDER_METADATA}\n"


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


#: Every environment variable this module consults when resolving paths. One
#: authoritative list, owned by the module the variables belong to: the test
#: suite redirects all of them into throwaway roots, and `prove` builds its
#: sandbox from them -- a variable added here but missed in a hand-kept copy
#: would silently point a "sandbox" at the real installation, with nothing
#: failing. Three copies of this tuple existed before it moved here.
ENV_KEYS = (
    "WOSWOAR_DIR",
    "WOSWOAR_SESSION",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
)


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
    """The parse cache. Not a pickle: see the note in :mod:`woswoar.cache`."""
    return cache_dir() / "cache.txt"


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


#: Everything woswoar owns is private to its owner. `~/.bash_history` is 0600
#: and woswoar's logs hold strictly more -- the command, the working directory,
#: the exit status, and every other machine's history once sync has run -- so
#: anything looser would make installing woswoar a downgrade. The default umask
#: on most distributions is 022, which is why this cannot be left implicit.
#:
#: The shell hook spells 077 itself, because it is copied verbatim rather than
#: templated; tests/test_shell_hook.py pins the two together.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: Group and other bits. "Readable by someone who is not you", once.
OTHER_BITS = 0o077


def private_dir(path: Path) -> Path:
    """Create ``path`` and any missing parents, owner-only from the moment they exist.

    Each component is created separately because ``Path.mkdir(parents=True,
    mode=...)`` applies the mode to the leaf only and leaves parents at the
    umask default -- which would put a 0755 ``logs/`` above a 0700 host
    directory and defeat the whole thing.

    Walks *up* until it meets something that exists, rather than down from the
    filesystem root: the overwhelmingly common call is "this directory is
    already there", and iterating ``path.parents`` stats every component from
    ``/`` on every write -- 7 stats instead of 1, measured, on a path that
    needed none of them.

    Directories that already exist are left exactly as they are. This is called
    from `write_atomic`, so anything else would make writing a file quietly
    re-permission whatever directory it was pointed at -- including a
    ``history/`` that came from ``git clone``. Re-tightening an old tree is
    :func:`harden`'s job, and keeping the two separate is what lets that
    docstring be true.
    """
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent

    for directory in reversed(missing):
        directory.mkdir(mode=DIR_MODE, exist_ok=True)
    return path


def private_append(path: Path, binary: bool = False) -> IO[Any]:
    """Open ``path`` for appending, creating it owner-only if it is not there.

    ``open(..., "a")`` creates with 0666 masked by the umask -- 0644 on a stock
    install. The mode only matters at creation, so this is the one moment it
    can be got right.

    ``binary`` because the caller that appends another machine's merged log
    already holds bytes. Making it decode to fit a text-only helper would put a
    lossy round trip on data that machine recorded successfully, which is not a
    decision a function about file modes should be making.
    """
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    if binary:
        return os.fdopen(fd, "ab")
    return os.fdopen(fd, "a", encoding="utf-8")


def _private_paths() -> Iterator[tuple[Path, bool]]:
    """``(path, is_dir)`` for everything woswoar owns whose mode is its business.

    Walked rather than listed, so a file added under the data directory later
    is covered by default instead of by someone remembering.

    ``os.walk`` rather than ``rglob`` because ``history/`` has to be *pruned*,
    not filtered: it is a git checkout that reaches tens of thousands of
    objects, and ``rglob`` descends into it and then discards the results, so
    the cost is proportional to the size of the archive -- exactly what
    excluding it was supposed to avoid. Measured on a two-year tree with 20k
    loose git objects: 238 ms filtering, 1.4 ms pruning.

    It also yields whether each path is a directory, which the walk already
    knows and which the callers would otherwise re-stat.

    `.DS_Store` is skipped, and that is a decision rather than an oversight. It
    is Finder's file, not woswoar's: it records icon positions and view settings
    for a directory somebody opened, holds no command and no path outside the
    one it sits in, and is recreated at whatever the ambient umask says the
    moment that directory is browsed again. Both callers would otherwise be
    wrong about it -- `harden` would keep re-tightening a file that keeps coming
    back, and `readable_by_others` would report an exposure that no action by
    the user can durably clear, which is a `doctor` that stays red for something
    nobody can fix. That teaches people to stop reading `doctor`, which is worth
    more than the mode of this file.
    """
    history = history_dir().name
    for root in (data_dir(), config_dir(), cache_dir()):
        if not root.is_dir():
            continue
        yield root, True
        for parent, dirs, files in os.walk(root):
            here = Path(parent)
            if here == data_dir() and history in dirs:
                dirs.remove(history)
            for name in dirs:
                yield here / name, True
            for name in files:
                if name == _FINDER_METADATA:
                    continue
                yield here / name, False


def harden() -> None:
    """Re-tighten an installation that predates :data:`DIR_MODE`.

    Separate from :func:`private_dir` on purpose: creating a directory should
    not silently re-permission one that was already there, but a migration
    should. `install` is where this runs, because that is the command people
    re-run to upgrade.
    """
    for path, is_dir in _private_paths():
        with contextlib.suppress(OSError):
            path.chmod(DIR_MODE if is_dir else FILE_MODE)


def readable_by_others() -> list[Path]:
    """Everything woswoar owns that another account on this machine could read.

    Named for the check it performs -- the mask is group *and* other, which
    ``world_readable`` would have misdescribed for anyone adding a caller.
    """
    return [p for p, _ in _private_paths() if p.stat().st_mode & OTHER_BITS]


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path``'s contents in one step.

    Every file woswoar owns outside the append-only logs is small, rewritten
    whole, and load-bearing if it survives half-written: the parse cache, the
    import watermarks, and the sync state. They all go through here so
    a crash mid-write leaves the previous version rather than a corrupt one.
    """
    # Imported here rather than at module scope: `tempfile` pulls `shutil` and
    # costs ~3 ms of interpreter startup, and this module is imported by every
    # Ctrl-R -- which does not write anything unless the cache has drifted far
    # enough to be worth saving. Measured on a 55k-entry history: 3.1 ms off a
    # search that never reaches this function.
    import tempfile

    private_dir(path.parent)
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

    An ``offset`` at or past the end returns ``(b"", offset)``, and `export`
    leans on that: it stats first and skips this call entirely when the file
    cannot have grown. Anything that made reading past the end mean something
    else -- returning the partial final line, say -- has to change that too.
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
    lines = [f"id={known.id}", f"name={known.name}"]
    if known.identity:
        lines.append(f"identity={known.identity}")
    # Through write_atomic, which creates via mkstemp at 0600. This file names
    # the identity key's path; it is not secret, but it is nobody else's.
    write_atomic(machine_file(), ("\n".join(lines) + "\n").encode("utf-8"))

    # Mirrored next to the logs so search can label entries without reading
    # config, and so sync has a single file to seal and share.
    write_atomic(host_dir(known.id) / _NAME_FILE, f"{known.name}\n".encode())


def default_machine_name() -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
    host = os.uname().nodename.split(".")[0]
    return f"{user}@{host}"


def host_name(machine_id: str) -> str:
    """A host's friendly name, or ``""`` if none has arrived here yet.

    The one place that knows where a name is kept. `sync.name_for` wants the
    empty answer so it can say "(unnamed)"; `host_names` wants the id instead,
    so the fallback belongs to the callers rather than here.
    """
    try:
        return name_file(machine_id).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
        names[entry.name] = host_name(entry.name) or entry.name
    return names


class LogFile(NamedTuple):
    #: Path relative to ``logs/``, e.g. ``hosts/7f3a9c21/2026-07-29.tsv``.
    #: Used as the cache key, so it must stay stable and platform-independent.
    relpath: str
    host_id: str
    path: Path


def iter_log_files(host_id: str | None = None) -> Iterator[LogFile]:
    """Yield plaintext log files, sorted for reproducible cache builds.

    ``host_id`` lists that host's directory alone; without it, every host's.
    """
    root = logs_dir() / _HOSTS
    if not root.is_dir():
        return

    hosts = [root / host_id] if host_id is not None else sorted(root.iterdir())
    for host in hosts:
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


def format_file() -> Path:
    """Which shape this repository is, in plaintext at its root.

    Plaintext, and it has to be: a machine reads this *before* it knows how to
    find the day keys, so a version of it sealed to a key is a fact you can only
    learn by already understanding the layout it describes.

    It says nothing an observer could not get from the directory listing anyway
    -- `docs/security.md` has always put the shape and size of the repository
    outside what encryption hides.
    """
    return history_dir() / FORMAT


def repo_format() -> int | None:
    """The version the repository claims, or ``None`` when nothing legible does.

    ``None`` is "the shape that predates the marker", which is this shape -- so
    an absent file reads as compatible and every existing installation goes on
    working untouched. That is the whole migration.

    Unreadable *content* answers ``None`` too, rather than refusing. The file
    sits in a repository anyone with push access can write, so treating garbage
    as a fatal disagreement would hand any of them a way to stop every machine
    syncing by writing four bytes -- while a legible number is a statement made
    deliberately by a newer woswoar, and is worth stopping for. `State.load`
    degrades the same way, for the same reason.
    """
    try:
        text = format_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith(_FORMAT_PREFIX):
        return None
    try:
        return int(text[len(_FORMAT_PREFIX) :])
    except ValueError:
        # `int` in a `try`, and not `str.isdigit` before it: those are different
        # questions, and the gap between them is this function's whole promise.
        # `"²".isdigit()` is True and `int("²")` raises, so a marker holding
        # `woswoar-repo-²` crashed every sync with an unhandled ValueError --
        # four bytes, writable by anyone with push access, exactly the wedge the
        # docstring above says cannot happen.
        return None


def state_file() -> Path:
    """Local sync watermarks. Deliberately outside the repo -- it is per-machine
    progress, not shared history, and syncing it would create conflicts."""
    return data_dir() / "state.json"


def sync_stamp_file() -> Path:
    """When a sync was last *started*, as decimal seconds on one line.

    Written by the shell hook, which is the only thing that reads it, and read
    with bash's `read` builtin -- so it holds the time rather than relying on
    its own mtime, which bash cannot ask for without forking `stat`.

    "Started", not "finished": the hook stamps it before it forks. A sync that
    dies immediately -- `woswoar` not on PATH, a half-finished install -- must
    still cost one attempt a minute rather than one per command.
    """
    return data_dir() / "sync-stamp"


def sync_failure_file() -> Path:
    """The last unattended sync's failure, or absent if the last one worked.

    A sync fired from the prompt is detached and its output goes nowhere, so
    without this a sync that has been failing for a week says nothing at all.
    The systemd timer had the journal, which was already only a theoretical
    improvement -- nobody reads that either.
    """
    return data_dir() / "sync-failure.json"


def repo_host_dir(machine_id: str) -> Path:
    return history_dir() / _HOSTS / machine_id


def name_seal(machine_id: str) -> Path:
    """Sealed friendly name, so other machines can label this host's entries."""
    return repo_host_dir(machine_id) / "name.age"


def signing_key_file() -> Path:
    """This machine's signing key. Local, and never published or synced.

    In the config directory beside the age identity, not in the repo: the public
    half is what other machines need, and the private half is the one thing that
    makes a revoked machine's signatures worthless to it -- so it is also the one
    thing that must never be sealed to anybody, however trusted.
    """
    return config_dir() / "signing_key"


def signer_public(machine_id: str) -> Path:
    """A host's published verify key, and the recipient that owns the host.

    Two lines, and the second is why this file exists at all rather than the
    verify key living in `recipients.txt`: revocation names an *age recipient*,
    while chunks live under a *host id*, and nothing else in the repo joins the
    two. It could not go in `recipients.txt` either -- an age recipient may
    itself be an SSH key, `ssh-ed25519 AAAA... comment`, so a second key in that
    field could not be told from the first one's comment.

    Anyone who can push can rewrite this, so nothing here is believed. It is
    what `trust` *shows* a human, once; what is believed afterwards is the copy
    pinned in local state.
    """
    return repo_host_dir(machine_id) / "signer.pub"


def day_manifest(machine_id: str, day: str) -> Path:
    """The signed list of a host-day's chunks.

    Deliberately not one signature per chunk, which is what made the first
    attempt at this unusable: a signature costs a subprocess, and a machine
    waiting for `grant` re-checks everything it has not merged on every timer
    tick. One manifest per host-day turns that from once per chunk into once per
    day -- about 3.6 s over a year of three machines, against nearly two minutes.

    Rewritten as the day goes on, so it lives outside the `hosts/<id>/<day>/`
    chunk directories that `is_chunk_path` marks write-once. Only the owning
    host ever writes it, so it cannot conflict, and it needs no merge driver.
    """
    return repo_host_dir(machine_id) / _MANIFESTS / day


def day_key(machine_id: str, day: str) -> Path:
    """The day's identity, sealed to every recipient."""
    return repo_host_dir(machine_id) / _KEYS / f"{day}{_CHUNK_SUFFIX}"


def day_key_public(machine_id: str, day: str) -> Path:
    """The day's public key, in the clear.

    Public keys are not secret, and keeping this alongside means writing a chunk
    never has to open the sealed key first.
    """
    return repo_host_dir(machine_id) / _KEYS / f"{day}{_PUB_SUFFIX}"


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

    Zero-padded seconds sort chronologically as strings, so a day's chunks are
    walked oldest-first. That ordering is a convenience, not a guarantee: two
    chunks written in the same second differ only by the random suffix, which is
    why what a machine has merged is recorded as a *set* of names rather than a
    high-water mark.

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
    #: Filename only. Sorts chronologically, and is what `State.merged` records.
    name: str
    path: Path


def chunk_names(machine_id: str, day: str) -> list[str]:
    """The chunk filenames in one host-day, sorted. Empty if there are none.

    The unit the layout is actually asked about: what counts as a chunk is
    decided here, so a partial write or an editor's leftover in a day directory
    is not one. Callers that want a single day must not reach it by walking the
    host -- `has_chunks` did, and turned a 0.09 ms question into a 4.6 ms one on
    an archive of 20k chunks, once per orphaned day.

    Listing is what walking an archive costs now that nothing builds a `Path`
    per file: 4.96 ms for one peer's 20k chunks, against 0.18 ms to stat its 40
    day directories, which is why `day_stamp` exists.

    `os.scandir` swallows the same errors `Path.glob` did: a day directory that
    has gone away, or that cannot be read, has no chunks to offer.
    """
    try:
        with os.scandir(chunk_dir(machine_id, day)) as entries:
            return sorted(e.name for e in entries if e.is_file() and e.name.endswith(_CHUNK_SUFFIX))
    except OSError:
        return []


def chunk_days(machine_id: str) -> list[str]:
    """The day directories one host publishes into, in order.

    One `scandir` of the host, and no listing of the days themselves -- which is
    what lets a caller decide *per day* whether it needs the names at all.
    """
    try:
        with os.scandir(repo_host_dir(machine_id)) as entries:
            return sorted(e.name for e in entries if e.is_dir() and _is_day(e.name))
    except OSError:
        return []


def day_stamp(machine_id: str, day: str) -> int | None:
    """When this day directory last gained or lost a file. ``None`` if absent.

    A directory's mtime moves when an entry is added to or removed from it,
    which is exactly what a fetch bringing in a chunk does -- so a day whose
    stamp is unchanged since it was last merged has nothing new in it, without
    listing it -- see `chunk_names` for what that saves.

    Nanoseconds, and compared for equality rather than order: a directory
    restored from a backup can be older than the stamp that was recorded, and
    "older" still means "different, look again".
    """
    try:
        return os.stat(chunk_dir(machine_id, day)).st_mtime_ns
    except OSError:
        return None


def iter_chunk_days(machine_id: str) -> Iterator[tuple[str, list[str]]]:
    """``(day, chunk filenames)`` for one host: days in order, names sorted.

    Each day appears exactly once, with all of its chunks, and days with none at
    all are not days. `sync._merge_host` reaches the same two halves separately
    -- `chunk_days` then, per day it has to look at, `chunk_names` -- because it
    can decide from the day alone that it needs no names.
    """
    for day in chunk_days(machine_id):
        # A day directory with nothing in it is not a day with no new chunks --
        # it is not a day at all, and saying so keeps every caller from having
        # to ask.
        if names := chunk_names(machine_id, day):
            yield day, names


def iter_chunks(machine_id: str) -> Iterator[Chunk]:
    """Every sealed chunk belonging to one host, oldest first.

    The flat view of `iter_chunk_days`, for the callers that want the paths.
    """
    root = repo_host_dir(machine_id)
    for day, names in iter_chunk_days(machine_id):
        for name in names:
            yield Chunk(day=day, name=name, path=root / day / name)


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


def day_key_days(machine_id: str) -> tuple[set[str], set[str]]:
    """``(sealed, public)`` day strings for one host, from a single listing.

    One readdir rather than a `glob` plus a stat per key: the caller asks this
    for every host in the repo, so the cost grows with the archive forever --
    measured on a two-year, five-machine tree, 1.9ms against 36ms.

    Both sets from one pass because the only question worth asking is how they
    differ, and pulling them apart is what let two callers drift over whether a
    missing sealed half mattered.
    """
    sealed: set[str] = set()
    public: set[str] = set()
    try:
        names = os.listdir(repo_host_dir(machine_id) / _KEYS)
    except OSError:
        return sealed, public
    for name in names:
        if name.endswith(_CHUNK_SUFFIX):
            sealed.add(name.removesuffix(_CHUNK_SUFFIX))
        elif name.endswith(_PUB_SUFFIX):
            public.add(name.removesuffix(_PUB_SUFFIX))
    return sealed, public


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


def day_of_key(path: Path) -> str:
    """``hosts/<id>/keys/2026-07-29.age`` -> ``2026-07-29``.

    The inverse of `day_key`, and here rather than at the caller for the same
    reason `iter_day_keys` is: the suffix is this module's to know.
    """
    return path.name.removesuffix(_CHUNK_SUFFIX)


def append_entries(machine_id: str, entries: list[Entry]) -> dict[str, int]:
    """Append entries to their day files, returning lines written per file.

    Used by the importer. Recording goes through the shell hook instead, which
    never calls into Python.
    """
    by_day: dict[str, list[Entry]] = {}
    for item in entries:
        by_day.setdefault(day_for(item.ts), []).append(item)

    # Once, not once per day: every day file in this loop shares one parent, and
    # re-resolving it measured 17% of a two-year import.
    private_dir(host_dir(machine_id))

    written: dict[str, int] = {}
    for day, group in sorted(by_day.items()):
        path = log_file(machine_id, day)
        group.sort(key=lambda e: e.ts)
        payload = "".join(f"{format_line(e)}\n" for e in group)
        with private_append(path) as handle:
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
