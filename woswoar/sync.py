"""Git synchronisation with age-encrypted, write-once chunks.

The shape of this, and why:

- The plaintext logs under ``logs/`` are the working copy and the truth. The git
  tree under ``history/`` holds only ciphertext.
- Every sync seals *only the lines added since last time* into a brand new file.
  Nothing in the repo is ever modified or deleted, which is what makes
  ``git pull --rebase`` structurally unable to conflict and makes repo growth
  exactly the bytes written -- an append-only single file would instead depend
  on git finding good pack deltas for binary blobs.
- Each chunk is encrypted to a per-host-per-day key, which is itself sealed to
  every recipient. That halves the per-chunk header (measured: 200 bytes rather
  than 432 with three SSH recipients) and, more importantly, makes onboarding a
  new machine re-seal a few hundred tiny key files instead of tens of thousands
  of chunks.
- Each chunk is decrypted exactly once, ever. The plaintext lands in ``logs/``
  and the cache picks it up from there.
"""

from __future__ import annotations

import fcntl
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import crypto, store
from .errors import WoswoarError
from .store import Machine

GIT_TIMEOUT = 300

#: Commits carry no useful information -- the content is opaque and the author
#: is always this machine -- so they are uniform. A varying message would only
#: leak activity patterns into a place that is not encrypted.
COMMIT_MESSAGE = "woswoar sync"


class SyncError(WoswoarError):
    pass


@dataclass
class Report:
    chunks_written: int = 0
    lines_exported: int = 0
    chunks_merged: int = 0
    lines_imported: int = 0
    pushed: bool = False
    hosts_seen: set[str] = field(default_factory=set)
    #: "<host>/<day>" entries sealed before this machine was enrolled. Not an
    #: error: it is what a freshly joined machine sees until someone runs
    #: `reencrypt` on a machine that was already a recipient.
    unreadable: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    repo = cwd or store.history_dir()
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT,
    )
    if check and result.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def has_remote() -> bool:
    return bool(git("remote", check=False))


def remote_summary() -> str:
    """One line describing where history is published, for humans."""
    remotes = git("remote", "-v", check=False).splitlines()
    return remotes[0] if remotes else "none (history is local only)"


def list_recipients() -> list[tuple[str, str]]:
    """``(kind, key)`` for every enrolled machine.

    Parsed here rather than in the CLI because this module is the only writer of
    recipients.txt, so the record shape is its to know.
    """
    path = store.recipients_file()
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            kind, _, rest = line.strip().partition(" ")
            entries.append((kind, rest))
    return entries


def is_repo() -> bool:
    return (store.history_dir() / ".git").exists()


# ---------------------------------------------------------------------------
# Local sync state
# ---------------------------------------------------------------------------


@dataclass
class State:
    """Per-machine progress. Deliberately *not* in the repo: it describes what
    this machine has done, not shared history, and syncing it would manufacture
    the conflicts the rest of the design avoids."""

    #: log relpath -> plaintext bytes already sealed into a chunk.
    exported: dict[str, int] = field(default_factory=dict)
    #: "<host>/<day>" -> newest chunk filename already merged into logs/.
    merged: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> State:
        raw = store.load_json(store.state_file())
        exported = raw.get("exported", {})
        merged = raw.get("merged", {})
        if not isinstance(exported, dict) or not isinstance(merged, dict):
            return cls()
        return cls(
            exported={str(k): int(v) for k, v in exported.items()},
            merged={str(k): str(v) for k, v in merged.items()},
        )

    def save(self) -> None:
        store.save_json(store.state_file(), {"exported": self.exported, "merged": self.merged})


@contextmanager
def lock() -> Iterator[None]:
    """Serialise syncs. A prompt-triggered sync and the timer can collide."""
    path = store.data_dir() / "sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SyncError("another woswoar sync is already running") from exc
        yield
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def identity_path(known: Machine) -> Path:
    if not known.identity:
        raise SyncError("no identity configured; run 'woswoar init' first")
    path = Path(known.identity).expanduser()
    if not path.is_file():
        raise SyncError(f"identity {path} is missing; re-run 'woswoar init'")
    return path


class IdentityStatus(NamedTuple):
    ok: bool
    detail: str


def identity_status(known: Machine) -> IdentityStatus:
    """Whether this machine could actually decrypt during an unattended sync.

    Lives here rather than in `doctor` so the judgement is testable and stated
    once. The passphrase case is the reason it exists: age does not use
    ssh-agent, so such a key works perfectly by hand and fails forever from a
    timer -- a failure mode nobody notices without being told.
    """
    if not known.identity:
        return IdentityStatus(False, "no identity recorded - run 'woswoar init'")

    path = Path(known.identity).expanduser()
    if not path.is_file():
        return IdentityStatus(False, f"identity {path} is missing - re-run 'woswoar init'")
    if not crypto.available():
        return IdentityStatus(True, f"identity {path} (age missing, cannot verify)")
    if not crypto.usable(path):
        return IdentityStatus(
            False, f"{path} needs a passphrase - try 'woswoar init --new-identity'"
        )
    return IdentityStatus(True, f"identity {path}")


def day_public_key(known: Machine, day: str) -> str:
    """The public half of this host's key for ``day``, creating it if needed.

    One key per host per day. Kept in the clear beside the sealed private half
    so writing a chunk never has to open the sealed key first.
    """
    pub_path = store.day_key_public(known.id, day)
    if pub_path.is_file():
        return pub_path.read_text(encoding="utf-8").strip()

    identity = crypto.generate_identity()
    sealed = crypto.encrypt_to_recipients(identity.secret.encode("utf-8"), store.recipients_file())
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    store.write_atomic(store.day_key(known.id, day), sealed)
    store.write_atomic(pub_path, (identity.public + "\n").encode("utf-8"))
    return identity.public


def open_day_key(known: Machine, host_id: str, day: str) -> str:
    """Recover a day's private identity so its chunks can be read."""
    sealed_path = store.day_key(host_id, day)
    try:
        sealed = sealed_path.read_bytes()
    except OSError as exc:
        raise SyncError(f"missing day key {sealed_path}") from exc
    return crypto.decrypt_with_file(sealed, identity_path(known)).decode("utf-8")


# ---------------------------------------------------------------------------
# Export: plaintext tail -> sealed chunk
# ---------------------------------------------------------------------------


def export(known: Machine, state: State, report: Report, now: int) -> None:
    """Seal each log file's new lines into a fresh chunk."""
    for log in store.iter_log_files():
        if log.host_id != known.id:
            continue  # other hosts' logs arrived decrypted; never re-export them
        data, new_offset = store.read_tail(log.path, state.exported.get(log.relpath, 0))
        if not data:
            continue

        day = store.day_of_log(log.relpath)
        sealed = crypto.encrypt_to(data, day_public_key(known, day))
        chunk = store.new_chunk(known.id, day, now)
        chunk.parent.mkdir(parents=True, exist_ok=True)
        store.write_atomic(chunk, sealed)

        state.exported[log.relpath] = new_offset
        report.chunks_written += 1
        report.lines_exported += data.count(b"\n")


# ---------------------------------------------------------------------------
# Import: sealed chunk -> plaintext log
# ---------------------------------------------------------------------------


def merge(known: Machine, state: State, report: Report) -> None:
    """Decrypt every chunk from other hosts that we have not merged yet."""
    for host_id in store.repo_hosts():
        if host_id == known.id:
            continue  # our own plaintext is already the source of truth
        report.hosts_seen.add(host_id)
        _merge_host(known, host_id, state, report)
        _merge_name(known, host_id)


def _merge_host(known: Machine, host_id: str, state: State, report: Report) -> None:
    #: None marks a day whose key we already failed to open. Caching the failure
    #: matters as much as caching the success: without it, a machine that has
    #: not been granted access yet retries the same doomed `age -d` once per
    #: chunk rather than once per day -- tens of thousands of subprocess spawns
    #: instead of hundreds, on precisely the first sync a new machine runs.
    day_keys: dict[str, str | None] = {}
    pending: dict[str, list[bytes]] = {}

    for chunk in store.iter_chunks(host_id):
        key = f"{host_id}/{chunk.day}"
        # Chunk names are zero-padded timestamps, so "newer than the watermark"
        # is a plain string comparison.
        if chunk.name <= state.merged.get(key, ""):
            continue

        if chunk.day not in day_keys:
            try:
                day_keys[chunk.day] = open_day_key(known, host_id, chunk.day)
            except (crypto.AgeError, SyncError):
                # Sealed before this machine was enrolled, and no one has run
                # `reencrypt` yet. Skip it without advancing the watermark so a
                # later sync picks it up -- and above all without aborting, or
                # one unreadable day would block this machine's own export too.
                day_keys[chunk.day] = None
        secret = day_keys[chunk.day]
        if secret is None:
            report.unreadable.add(key)
            continue

        plaintext = crypto.decrypt_with_secret(chunk.path.read_bytes(), secret)
        pending.setdefault(chunk.day, []).append(plaintext)
        state.merged[key] = chunk.name
        report.chunks_merged += 1

    for day, blocks in pending.items():
        payload = b"".join(blocks)
        target = store.log_file(host_id, day)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab") as handle:
            handle.write(payload)
        report.lines_imported += payload.count(b"\n")


def _merge_name(known: Machine, host_id: str) -> None:
    """Learn another machine's friendly name, so search can label its entries."""
    local = store.name_file(host_id)
    if local.is_file():
        return
    sealed = store.name_seal(host_id)
    if not sealed.is_file():
        return
    try:
        name = crypto.decrypt_with_file(sealed.read_bytes(), identity_path(known))
    except (crypto.AgeError, SyncError, OSError):
        # A name we cannot open is cosmetic; the opaque id still works.
        return
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(name)


# ---------------------------------------------------------------------------
# The sync itself
# ---------------------------------------------------------------------------


def run(push: bool = True, now: int | None = None) -> Report:
    """Export, exchange with the remote, import. Safe to run concurrently."""
    crypto.require()
    if not is_repo():
        raise SyncError("no history repo yet; run 'woswoar init' first")

    known = store.machine()
    if not store.recipients_file().is_file():
        raise SyncError("no recipients.txt in the history repo; run 'woswoar init'")

    _ensure_repo_config()
    report = Report()
    remote = push and has_remote()

    with lock():
        state = State.load()

        # Take the remote's view *before* exporting. Creating a day key seals it
        # to whatever recipients.txt says right now, so exporting against a
        # stale list would produce a key that machines enrolled since the last
        # sync can never open -- silently, and permanently, because the repo is
        # append-only.
        if remote:
            _fetch_and_rebase(report)

        export(known, state, report, int(time.time()) if now is None else now)
        _commit()

        if remote:
            _push(report)

        merge(known, state, report)
        state.save()

    return report


def _branch() -> str:
    """The current branch, including before the first commit exists.

    `rev-parse --abbrev-ref HEAD` cannot answer on an unborn HEAD, which is
    exactly the state `init` is in when it enrols the first machine.
    """
    return git("branch", "--show-current")


def _fetch_and_rebase(report: Report) -> None:
    """Adopt the remote's history under ours.

    Fetch-then-rebase rather than ``git pull --rebase`` because the tracking ref
    may legitimately not exist: cloning an *empty* remote configures a branch
    pointing at nothing, which is the normal state for the first machine to
    enrol. Checking the fetched ref directly handles that and the ordinary case
    with one code path.

    The rebase cannot conflict over chunks -- every machine only ever adds files
    below its own host id. ``recipients.txt`` is the single shared file, and
    ``.gitattributes`` marks it ``merge=union`` so both sides' keys survive.
    """
    branch = _branch()
    git("fetch", "--quiet", "origin")
    if not git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False):
        return
    try:
        git("rebase", "--autostash", f"origin/{branch}")
    except SyncError:
        git("rebase", "--abort", check=False)
        raise


def _push(report: Report) -> None:
    """Publish, retrying once if someone else pushed while we worked."""
    branch = _branch()
    try:
        git("push", "--quiet", "-u", "origin", branch)
    except SyncError:
        _fetch_and_rebase(report)
        git("push", "--quiet", "-u", "origin", branch)
    report.pushed = True


#: Commit metadata is one of the few things in the repo that is *not* encrypted,
#: so it is pinned to a fixed identity rather than inheriting the user's real
#: name and email from their global gitconfig. Set on the repo so that rebase
#: and commit alike pick it up, including on a machine with no gitconfig at all.
COMMIT_NAME = "woswoar"
COMMIT_EMAIL = "woswoar@localhost"


def _ensure_repo_config() -> None:
    if git("config", "user.name", check=False) != COMMIT_NAME:
        git("config", "user.name", COMMIT_NAME)
    if git("config", "user.email", check=False) != COMMIT_EMAIL:
        git("config", "user.email", COMMIT_EMAIL)


def _commit() -> None:
    git("add", "-A")
    if not git("status", "--porcelain"):
        return
    git("commit", "-q", "-m", COMMIT_MESSAGE)


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

#: Tried in order when no identity is configured. An existing SSH key is
#: preferred because it means no new secret exists to lose or leak.
SSH_CANDIDATES = ("id_ed25519", "id_rsa")


def choose_identity(new_identity: bool = False, explicit: Path | None = None) -> Path:
    """Pick the key this machine will use to open things sealed to it.

    Resolved once, at init, and recorded -- not re-detected per sync. The check
    is a real encrypt/decrypt round trip rather than an inspection of the key
    file, because what matters is whether an *unattended* sync will work: age
    does not use ssh-agent, so a passphrase-protected key that works fine in a
    terminal fails from a systemd timer.
    """
    if explicit is not None:
        path = explicit.expanduser()
        if not crypto.usable(path):
            raise SyncError(f"{path} cannot decrypt without a terminal; see 'woswoar doctor'")
        return path

    if not new_identity:
        for name in SSH_CANDIDATES:
            candidate = Path.home() / ".ssh" / name
            if candidate.is_file() and crypto.usable(candidate):
                return candidate

    dedicated = store.config_dir() / "identity"
    if dedicated.is_file() and crypto.usable(dedicated):
        return dedicated

    identity = crypto.generate_identity()
    dedicated.parent.mkdir(parents=True, exist_ok=True)
    store.write_atomic(dedicated, identity.secret.encode("utf-8"))
    dedicated.chmod(0o600)
    return dedicated


def initialise(
    remote: str | None = None,
    new_identity: bool = False,
    identity: Path | None = None,
) -> tuple[Machine, Path]:
    """Create or clone the history repo and enrol this machine in it."""
    crypto.require()

    chosen = choose_identity(new_identity=new_identity, explicit=identity)
    known = store.machine()._replace(identity=str(chosen))
    store.save_machine(known)

    history = store.history_dir()
    if not is_repo():
        history.parent.mkdir(parents=True, exist_ok=True)
        if remote and not any(history.glob("*")):
            git("clone", "--quiet", remote, str(history), cwd=history.parent)
        else:
            history.mkdir(parents=True, exist_ok=True)
            git("init", "--quiet", "-b", "main")
    if remote and not has_remote():
        git("remote", "add", "origin", remote)

    _ensure_repo_config()
    report = Report()

    # Adopt the remote's state before enrolling, so we append to the real
    # recipients.txt rather than creating a competing one.
    publishing = has_remote()
    if publishing:
        _fetch_and_rebase(report)

    if _write_repo_metadata(known, chosen):
        _commit()

    # Publish immediately. Until this machine's public key is on the remote,
    # `reencrypt` run elsewhere cannot include it, so onboarding would appear to
    # succeed and then silently fail to grant access to any older history.
    if publishing:
        _push(report)

    return known, chosen


def _write_repo_metadata(known: Machine, identity: Path) -> bool:
    """Ensure this machine is enrolled. Returns whether anything changed."""
    changed = False

    attrs = store.history_dir() / store.GITATTRIBUTES
    current = attrs.read_text(encoding="utf-8") if attrs.is_file() else ""
    if current != store.GITATTRIBUTES_CONTENT:
        store.write_atomic(attrs, store.GITATTRIBUTES_CONTENT.encode("utf-8"))
        changed = True

    if add_recipient(crypto.recipient_for(identity)):
        changed = True

    seal = store.name_seal(known.id)
    if not seal.is_file():
        seal.parent.mkdir(parents=True, exist_ok=True)
        store.write_atomic(
            seal,
            crypto.encrypt_to_recipients(f"{known.name}\n".encode(), store.recipients_file()),
        )
        changed = True
    return changed


class ReencryptReport(NamedTuple):
    resealed: int
    #: Keys this machine cannot open, so cannot re-seal for anyone else.
    skipped: int
    pushed: bool


def reencrypt() -> ReencryptReport:
    """Re-seal every key file to the current recipient list, and publish it.

    This is what makes a newly onboarded machine able to read *old* history.
    Chunks are encrypted to per-day keys, so only those small key files -- and
    the name seals -- have to be rewritten; the tens of thousands of chunks are
    untouched. Any machine already enrolled can do it for every host, because
    all hosts seal their keys to the same recipient list.

    Only a machine that is *already* a recipient can do this: re-sealing means
    opening the existing key first. That is the point rather than a limitation
    -- if a machine nobody had granted access to could re-seal old keys, the
    encryption would not be worth anything. So the new machine cannot onboard
    itself, and one that cannot open a key skips it silently.

    Fetching first is not optional, for the same reason `run` exports after
    fetching: the recipient list this re-seals to is a *file in the working
    tree*, and the whole purpose of the operation is that a machine enrolled
    since the last sync appears in it. Re-sealing against a stale checkout
    reports full success and grants exactly nothing.

    This is one of only two operations that rewrite an existing file. It is
    deliberately explicit rather than part of sync.
    """
    crypto.require()
    if not is_repo():
        raise SyncError("no history repo yet; run 'woswoar init' first")

    known = store.machine()
    identity = identity_path(known)
    recipients = store.recipients_file()
    _ensure_repo_config()
    git_report = Report()
    remote = has_remote()
    resealed = skipped = 0

    # The same lock sync takes: this rewrites files in the working tree, and a
    # timer-driven sync must not be reading them halfway through.
    with lock():
        if remote:
            _fetch_and_rebase(git_report)

        for host_id in store.repo_hosts():
            candidates = list(store.iter_day_keys(host_id))
            seal = store.name_seal(host_id)
            if seal.is_file():
                candidates.append(seal)

            for path in candidates:
                try:
                    plain = crypto.decrypt_with_file(path.read_bytes(), identity)
                except (crypto.AgeError, OSError):
                    # Not ours to re-seal: keys sealed before this machine
                    # joined, or left behind by one since removed.
                    skipped += 1
                    continue
                store.write_atomic(path, crypto.encrypt_to_recipients(plain, recipients))
                resealed += 1

        _commit()
        if remote:
            _push(git_report)

    return ReencryptReport(resealed, skipped, git_report.pushed)


def compact(before: str) -> tuple[int, int]:
    """Merge a host's own chunks for completed days into one chunk per day.

    Write-once chunks trade bytes for inodes: a 5-minute timer produces roughly
    40 files a day. This is the escape hatch, and it is opt-in precisely because
    it is the only routine operation that *deletes* files -- which is the
    property the rest of the design leans on. Only this host's own chunks are
    touched, so it still cannot conflict.

    Returns (days compacted, chunks replaced).
    """
    crypto.require()
    known = store.machine()
    by_day: dict[str, list[store.Chunk]] = {}
    for chunk in store.iter_chunks(known.id):
        if chunk.day < before:
            by_day.setdefault(chunk.day, []).append(chunk)

    days = 0
    replaced = 0
    for day, chunks in sorted(by_day.items()):
        if len(chunks) < 2:
            continue
        secret = open_day_key(known, known.id, day)
        plain = b"".join(crypto.decrypt_with_secret(c.path.read_bytes(), secret) for c in chunks)
        merged = store.new_chunk(known.id, day, int(chunks[-1].name.split("-")[0]))
        store.write_atomic(merged, crypto.encrypt_to(plain, crypto.public_of(secret)))
        for chunk in chunks:
            chunk.path.unlink()
        days += 1
        replaced += len(chunks)

    if days:
        _commit()
    return days, replaced


def add_recipient(recipient: str) -> bool:
    """Append a public key to recipients.txt if it is not already listed."""
    path = store.recipients_file()
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if recipient.strip() in {line.strip() for line in existing.splitlines()}:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not existing or existing.endswith("\n") else "\n"
    store.write_atomic(path, f"{existing}{separator}{recipient.strip()}\n".encode())
    return True
