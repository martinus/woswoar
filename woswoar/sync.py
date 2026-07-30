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
import zlib
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
    #: `grant` on a machine that was already a recipient.
    unreadable: set[str] = field(default_factory=set)
    #: "<host>/<day>" entries that could not be authenticated, and so were
    #: refused rather than merged.
    #:
    #: One category on purpose: a missing tag and a wrong tag are the same
    #: answer. Splitting them on the shape of the bytes was tried and reverted,
    #: because an attacker omits the tag too -- so the split told the reassuring
    #: story for exactly the case that most needed reporting.
    unauthenticated: set[str] = field(default_factory=set)
    #: True when this machine cannot open the repo key yet, so it can neither
    #: publish nor read. Recording carries on regardless; `grant` unblocks it.
    needs_grant: bool = False


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


#: Separates a recipient from the human label woswoar appends after it. age
#: never sees the label, because woswoar parses this file itself rather than
#: handing age the path -- which is what makes labelling possible at all.
_LABEL_SEP = " # "


def _recipient_lines() -> list[str]:
    path = store.recipients_file()
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def recipients() -> list[str]:
    """Every enrolled machine's public key, in the form age wants on `-r`.

    Read here rather than handed to age as a path: `crypto` never names a file
    in $HOME, because a sandboxed age cannot open one.

    Deduplicated by key. `.gitattributes` marks this file ``merge=union``, so a
    machine that appends a labelled line where another has the same key unlabelled
    leaves both, and age rejects a repeated recipient.
    """
    seen: dict[str, None] = {}
    for line in _recipient_lines():
        seen.setdefault(line.split(_LABEL_SEP, 1)[0].strip(), None)
    return list(seen)


def reader_labels() -> list[tuple[str, str]]:
    """``(key, label)`` for every enrolled machine, for showing to a human.

    Nobody can meaningfully consent to granting `age1ejf3l4f0nhnp9...` access to
    their history. The label is woswoar's own trailing comment when present, the
    SSH key's comment field otherwise, and an abbreviated key as a last resort.
    """
    out: list[tuple[str, str]] = []
    for line in _recipient_lines():
        key, _, label = line.partition(_LABEL_SEP)
        key = key.strip()
        if any(key == existing for existing, _ in out):
            continue
        if not label:
            fields = key.split(None, 2)
            label = fields[2] if len(fields) > 2 else f"{key[:12]}...{key[-6:]}"
        out.append((key, label.strip()))
    return out


def list_recipients() -> list[tuple[str, str]]:
    """``(kind, key)`` for every enrolled machine.

    Parsed here rather than in the CLI because this module is the only writer of
    recipients.txt, so the record shape is its to know.
    """
    return [(kind, rest) for kind, _, rest in (line.partition(" ") for line in recipients())]


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
    reason = crypto.why_unusable(path)
    if reason:
        # No hint appended here: crypto already puts the right advice in the
        # reason, and picking it by grepping this string for "passphrase" made
        # sync depend on crypto's exact wording.
        return IdentityStatus(False, f"identity {path} {reason}")
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
    sealed = crypto.encrypt_to_recipients(identity.secret.encode("utf-8"), recipients())
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    store.write_atomic(store.day_key(known.id, day), sealed)
    store.write_atomic(pub_path, (identity.public + "\n").encode("utf-8"))
    return identity.public


def mac_key(known: Machine) -> bytes:
    """The repo's authentication key, creating it if this is a fresh repo.

    Sealed to every recipient, so holding it is exactly "being one of the
    enrolled machines" -- which is the property every chunk is checked against.
    Someone who can push to the repo but holds no enrolled identity cannot open
    it, and so cannot produce a tag any machine will accept.
    """
    path = store.mac_key_file()
    if path.is_file():
        return crypto.decrypt_with_file(path.read_bytes(), identity_path(known))

    key = crypto.new_mac_key()
    store.write_atomic(path, crypto.encrypt_to_recipients(key, recipients()))
    return key


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


def pack(data: bytes) -> bytes:
    """Compress a chunk's lines before they are sealed.

    This is the only moment compression is possible. age does not compress, and
    ciphertext is incompressible by definition, so once the bytes are sealed
    neither git's packfile nor anything else can ever shrink them again -- and
    the repo is append-only, so there is no second chance. Shell history is
    extremely repetitive, and the measured effect on repo size is in
    docs/woswoar_design_summary.md.

    Deliberately unconditional. An earlier version tagged each payload raw-or-
    deflated and stored whichever was smaller, on the theory that a very short
    chunk would inflate. On real line shapes it does not: a single-line chunk
    is 42 bytes raw and 35 deflated, so the tag saved a byte exactly once and
    cost one on every chunk after that.
    """
    return zlib.compress(data, 9)


def unpack(blob: bytes) -> bytes:
    """Inverse of :func:`pack`.

    Not defensive on purpose: zlib rejects anything that is not a deflate
    stream, so a payload written by some future format fails here rather than
    being appended to a log as garbage. Callers treat that like any other
    unreadable chunk.
    """
    return zlib.decompress(blob)


def export(known: Machine, state: State, report: Report, now: int, mac: bytes) -> None:
    """Seal each log file's new lines into a fresh chunk, and tag it.

    The tag covers the sealed bytes, so a reader authenticates a chunk before
    decrypting it.
    """
    for log in store.iter_log_files():
        if log.host_id != known.id:
            continue  # other hosts' logs arrived decrypted; never re-export them
        data, new_offset = store.read_tail(log.path, state.exported.get(log.relpath, 0))
        if not data:
            continue

        day = store.day_of_log(log.relpath)
        sealed = crypto.encrypt_to(pack(data), day_public_key(known, day))
        chunk = store.new_chunk(known.id, day, now)
        chunk.parent.mkdir(parents=True, exist_ok=True)
        store.write_atomic(chunk, store.frame_chunk(sealed, crypto.tag(mac, sealed)))

        state.exported[log.relpath] = new_offset
        report.chunks_written += 1
        report.lines_exported += data.count(b"\n")


# ---------------------------------------------------------------------------
# Import: sealed chunk -> plaintext log
# ---------------------------------------------------------------------------


def merge(known: Machine, state: State, report: Report, mac: bytes) -> None:
    """Decrypt every chunk from other hosts that we have not merged yet."""
    for host_id in store.repo_hosts():
        if host_id == known.id:
            continue  # our own plaintext is already the source of truth
        report.hosts_seen.add(host_id)
        _merge_host(known, host_id, state, report, mac)
        _merge_name(known, host_id)


def _merge_host(known: Machine, host_id: str, state: State, report: Report, mac: bytes) -> None:
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
                # `grant` yet. Skip it without advancing the watermark so a
                # later sync picks it up -- and above all without aborting, or
                # one unreadable day would block this machine's own export too.
                day_keys[chunk.day] = None
        secret = day_keys[chunk.day]
        if secret is None:
            report.unreadable.add(key)
            continue

        # Authenticated before it is decrypted or decompressed, so age, zlib and
        # the parser only ever see bytes one of this user's own machines wrote.
        # Deliberately *below* the day-key bail above rather than before it: a
        # day whose key cannot be opened never advances the watermark, so its
        # chunks come round on every sync, and this runs from a one-minute
        # timer. Opening a day key touches no chunk bytes, so nothing is
        # decrypted any earlier for being ordered this way.
        try:
            blob, expected = store.split_chunk(chunk.path.read_bytes())
        except (OSError, ValueError):
            report.unauthenticated.add(key)
            continue
        if not crypto.tag_matches(mac, blob, expected):
            report.unauthenticated.add(key)
            continue

        try:
            sealed = crypto.decrypt_with_secret(blob, secret)
            plaintext = unpack(sealed)
        except (crypto.AgeError, zlib.error, OSError):
            # Same judgement as an unopenable day key above: a chunk we cannot
            # consume -- damaged, or written by a woswoar that packs it some
            # other way -- must not abort the sync. Aborting would block this
            # machine's own export and every other host's readable chunks, on
            # this run and every run after it.
            report.unreadable.add(key)
            continue

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
            _fetch_and_rebase()

        # Opened once per sync, before anything uses it. A machine enrolled
        # since the last `grant` cannot open it, which is the same state -- and
        # has the same remedy -- as history it cannot decrypt, so it is reported
        # that way rather than as a distinct kind of failure.
        try:
            mac = mac_key(known)
        except crypto.AgeError:
            # Enrolled, but nobody has run `grant` yet, so this machine holds no
            # key it can tag or check with. Reported rather than raised: the
            # shell hook keeps recording into logs/, the backlog exports whole
            # on the first sync after `grant`, and a timer firing every minute
            # must not turn a normal waiting state into a stream of failures.
            report.needs_grant = True
            return report

        export(known, state, report, int(time.time()) if now is None else now, mac)
        _commit()

        if remote:
            _push()
            report.pushed = True

        merge(known, state, report, mac)
        state.save()

    return report


def _branch() -> str:
    """The current branch, including before the first commit exists.

    `rev-parse --abbrev-ref HEAD` cannot answer on an unborn HEAD, which is
    exactly the state `init` is in when it enrols the first machine.
    """
    return git("branch", "--show-current")


def _fetch_and_rebase() -> None:
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


def _push() -> None:
    """Publish, retrying once if someone else pushed while we worked."""
    branch = _branch()
    try:
        git("push", "--quiet", "-u", "origin", branch)
    except SyncError:
        _fetch_and_rebase()
        git("push", "--quiet", "-u", "origin", branch)


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
    # `--verbose` prints one "add '<path>'"/"remove '<path>'" line per staged
    # path and nothing at all when the index already matches, so the same
    # command that stages also answers "is there anything to commit". The
    # obvious `status --porcelain` afterwards costs a second full stat of a
    # working tree that is tens of thousands of chunks after a couple of years,
    # on a timer that now fires every minute.
    if not git("add", "-A", "--verbose"):
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
        reason = crypto.why_unusable(path)
        if reason:
            # The reason, not a guess at it. Reporting every failure as
            # "cannot decrypt without a terminal" is what sent someone with a
            # perfectly good unencrypted key looking for a passphrase problem.
            raise SyncError(f"{path} {reason}")
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

    # Adopt the remote's state before enrolling, so we append to the real
    # recipients.txt rather than creating a competing one.
    publishing = has_remote()
    if publishing:
        _fetch_and_rebase()

    if _write_repo_metadata(known, chosen):
        _commit()

    # After the recipient list exists, so the key is sealed to a list that
    # includes this machine. On a repo that already has one this does nothing;
    # opening it is `grant`'s job, not enrolment's.
    if not store.mac_key_file().is_file() and recipients():
        mac_key(known)
        _commit()

    # Publish immediately. Until this machine's public key is on the remote,
    # `grant` run elsewhere cannot include it, so onboarding would appear to
    # succeed and then silently fail to grant access to any older history.
    if publishing:
        _push()

    return known, chosen


def _write_repo_metadata(known: Machine, identity: Path) -> bool:
    """Ensure this machine is enrolled. Returns whether anything changed."""
    changed = False

    attrs = store.history_dir() / store.GITATTRIBUTES
    current = attrs.read_text(encoding="utf-8") if attrs.is_file() else ""
    if current != store.GITATTRIBUTES_CONTENT:
        store.write_atomic(attrs, store.GITATTRIBUTES_CONTENT.encode("utf-8"))
        changed = True

    if add_recipient(crypto.recipient_for(identity), label=known.name):
        changed = True

    seal = store.name_seal(known.id)
    if not seal.is_file():
        seal.parent.mkdir(parents=True, exist_ok=True)
        store.write_atomic(
            seal,
            crypto.encrypt_to_recipients(f"{known.name}\n".encode(), recipients()),
        )
        changed = True
    return changed


class ReencryptReport(NamedTuple):
    resealed: int
    #: Keys this machine cannot open, so cannot re-seal for anyone else.
    skipped: int
    pushed: bool


def readers() -> list[str]:
    """Who would be able to read the whole history if `grant` ran now.

    Fetches first, because the point of granting is to include a machine that
    enrolled since this one last looked -- reporting the pre-fetch list would
    show fewer machines than the operation is about to authorise, which is the
    one direction a security confirmation must never be wrong in.
    """
    with lock():
        if has_remote():
            _fetch_and_rebase()
        return recipients()


def grant(confirmed: list[str] | None = None) -> ReencryptReport:
    """Re-seal every key file to the current recipient list, and publish it.

    Named for what it does to the user's history rather than to the files:
    afterwards, every machine in `recipients.txt` can read *everything*,
    including days recorded before it existed. `reencrypt` described the
    mechanism and hid that.

    ``confirmed`` is the list a human agreed to. If the fetch below turns up a
    different one -- someone enrolled a machine in the meantime -- this refuses
    rather than granting access to a machine nobody approved.

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
    _ensure_repo_config()
    remote = has_remote()
    resealed = skipped = 0

    # The same lock sync takes: this rewrites files in the working tree, and a
    # timer-driven sync must not be reading them halfway through.
    with lock():
        if remote:
            _fetch_and_rebase()

        # Read *after* the fetch, never before. The whole point of this command
        # is to seal to a machine that enrolled since the last sync, so taking
        # the list from a pre-fetch checkout would re-seal everything to the old
        # recipients and report full success. Reading it once here also saves
        # re-parsing the file for every one of a few thousand key files.
        keys = recipients()
        if confirmed is not None and sorted(keys) != sorted(confirmed):
            raise SyncError(
                "the set of machines changed while you were deciding; "
                "run 'woswoar grant' again to see the current list"
            )

        # The repo key first: it is what a newly enrolled machine needs before
        # it can authenticate anything at all, and unlike the per-host keys
        # there is exactly one of it.
        everything: list[Path] = []
        if store.mac_key_file().is_file():
            everything.append(store.mac_key_file())
        for host_id in store.repo_hosts():
            everything.extend(store.iter_day_keys(host_id))
            seal = store.name_seal(host_id)
            if seal.is_file():
                everything.append(seal)

        for path in everything:
            try:
                plain = crypto.decrypt_with_file(path.read_bytes(), identity)
            except (crypto.AgeError, OSError):
                # Not ours to re-seal: keys sealed before this machine
                # joined, or left behind by one since removed.
                skipped += 1
                continue
            store.write_atomic(path, crypto.encrypt_to_recipients(plain, keys))
            resealed += 1

        _commit()
        if remote:
            _push()

    return ReencryptReport(resealed, skipped, pushed=remote)


def compact(before: str) -> tuple[int, int]:
    """Merge a host's own chunks for completed days into one chunk per day.

    Write-once chunks trade bytes for inodes: a 5-minute timer produces roughly
    40 files a day. This is the escape hatch, and it is opt-in precisely because
    it is the only routine operation that *deletes* files -- which is the
    property the rest of the design leans on. Only this host's own chunks are
    touched, so it still cannot conflict.

    Holds the same lock `run` does, and must: this is the one operation that
    unlinks chunks, and the timer fires every minute. It is also what makes
    `store.new_chunk`'s uniqueness check sound, since that check assumes no
    other process is creating chunks for this host concurrently.

    Returns (days compacted, chunks replaced).
    """
    crypto.require()
    known = store.machine()

    with lock():
        mac = mac_key(known)
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
            plain = b"".join(
                unpack(
                    crypto.decrypt_with_secret(store.split_chunk(c.path.read_bytes())[0], secret)
                )
                for c in chunks
            )
            merged = store.new_chunk(known.id, day, int(chunks[-1].name.split("-")[0]))
            sealed = crypto.encrypt_to(pack(plain), crypto.public_of(secret))
            store.write_atomic(merged, store.frame_chunk(sealed, crypto.tag(mac, sealed)))
            for chunk in chunks:
                chunk.path.unlink()
            days += 1
            replaced += len(chunks)

        if days:
            _commit()

    return days, replaced


def add_recipient(recipient: str, label: str = "") -> bool:
    """Append a public key to recipients.txt if its key is not already listed.

    The label is what `grant` shows a human before widening who can read the
    history; without it the prompt lists opaque age keys and cannot be consented
    to in any real sense.
    """
    key = recipient.strip()
    if key in recipients():
        return False
    path = store.recipients_file()
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not existing or existing.endswith("\n") else "\n"
    line = f"{key}{_LABEL_SEP}{label}" if label else key
    store.write_atomic(path, f"{existing}{separator}{line}\n".encode())
    return True
