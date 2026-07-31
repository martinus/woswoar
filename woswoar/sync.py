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
- Each host signs a manifest of its own chunks, per day, with a key only it
  holds. That is what makes a chunk attributable to one machine rather than to
  the fleet, and so what lets `revoke` stop a machine publishing as well as
  reading. See `_manifest_body` and `_trusted_signer`.
"""

from __future__ import annotations

import fcntl
import hashlib
import subprocess
import time
import zlib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import NamedTuple

from . import crypto, store
from .entry import make_inert
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
    #: This machine's history is on the remote. A state, not an event: a run
    #: that was already level with the remote and committed nothing sets it
    #: without sending anything, because there was nothing to send.
    pushed: bool = False
    hosts_seen: set[str] = field(default_factory=set)
    #: "<host>/<day>" entries sealed before this machine was enrolled. Not an
    #: error: it is what a freshly joined machine sees until someone runs
    #: `grant` on a machine that was already a recipient.
    unreadable: set[str] = field(default_factory=set)
    #: "<host>/<day>" entries that could not be authenticated, and so were
    #: refused rather than merged.
    #:
    #: One category on purpose: a manifest that is missing, malformed, signed by
    #: the wrong key, or signed for another day are the same answer, as is a
    #: chunk whose digest it does not list. Splitting them on the shape of the
    #: bytes was tried and reverted, because an attacker can produce any of those
    #: shapes at will -- so the split told the reassuring story for exactly the
    #: case that most needed reporting.
    unauthenticated: set[str] = field(default_factory=set)
    #: Hosts publishing history under a signing key nobody here has accepted.
    #: Not an error and usually not an attack: it is what a machine enrolled
    #: since this one last looked is *supposed* to look like. `woswoar trust`.
    untrusted: set[str] = field(default_factory=set)
    #: Hosts now publishing under a different key than the one pinned here.
    #: Never resolved silently: it is either a re-enrolled machine or someone
    #: rewriting the repo, and nothing available here can tell which.
    changed_signer: set[str] = field(default_factory=set)
    #: Hosts whose pin was dropped because a tombstone withdrew them.
    unpinned: set[str] = field(default_factory=set)
    #: Days of this machine's own history it could not extend, because the
    #: manifest already there is one it cannot verify. Nothing is published for
    #: them until the manifest is put right, which beats signing a replacement
    #: that silently disowns everything published earlier that day.
    unsignable: set[str] = field(default_factory=set)
    #: Days of this machine's own history whose sealed day key has gone while
    #: chunks sealed to its public half remain. Nothing further is written for
    #: them, because a new key cannot rescue what the old one sealed.
    orphaned: set[str] = field(default_factory=set)
    #: Days this machine has published before whose signed manifest has gone.
    #: Signing a fresh one would list only what this run writes, disowning
    #: everything published earlier -- so nothing is written for them either.
    manifest_missing: set[str] = field(default_factory=set)
    #: "<day>/<name>" chunks sitting under *this* machine's own id that it never
    #: published. Nothing else would ever look at them -- `merge` skips our own
    #: id -- and they are deliberately not swept into a manifest we sign.
    foreign: set[str] = field(default_factory=set)
    #: True when this machine's own key was withdrawn. It publishes nothing --
    #: nobody would accept it -- and reads nothing new. Recording carries on;
    #: `grant` cannot undo it, and saying otherwise would send someone to another
    #: machine to run a command that cannot help.
    #:
    #: There used to be a `needs_grant` beside this, for a machine that could not
    #: open the shared authentication key and so could neither publish nor read.
    #: With per-machine signing there is no such state: publishing needs nothing
    #: from anyone, and a machine still waiting for `grant` simply reports the
    #: days it cannot read, like any other unreadable history.
    revoked: bool = False


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
    """For the callers that want only this. The definition lives in `Repo`."""
    return read_repo().has_remote


def remote_summary() -> str:
    """One line describing where history is published, for humans."""
    remotes = git("remote", "-v", check=False).splitlines()
    return remotes[0] if remotes else "none (history is local only)"


#: Separates a key from the text after it. Only two things ever wrote such
#: text: `revoke`, which marks a tombstone, and -- until #22 -- an enrolment
#: label holding `$USER@$(uname -n)`, published in cleartext in the one file of
#: the repo that is not encrypted. Names come from the sealed `name.age` now,
#: so what follows a key is a constant or nothing, and `_parse` still strips
#: whatever a file written by an older woswoar left here.
_LABEL_SEP = " # "

#: Marks a line as a *withdrawal* of the key that follows it rather than an
#: enrolment of it.
#:
#: A tombstone rather than deleting the line, because `.gitattributes` marks
#: this file ``merge=union`` -- the thing that makes two machines enrolling at
#: once conflict-free. Union keeps both sides of every difference, so a line
#: deleted here and still present in any peer's checkout comes straight back on
#: the next rebase. Dropping ``merge=union`` to make deletion stick would
#: reintroduce exactly the conflicts the append-only design exists to avoid, on
#: the one file every machine writes.
#:
#: ``-`` cannot collide with a key: age recipients start ``age1`` and SSH keys
#: with their type. `add_recipient` refuses one that would, so this stays a
#: decision about the first character rather than a guess.
_REVOKED = "-"


def _recipient_lines() -> list[str]:
    path = store.recipients_file()
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Line(NamedTuple):
    """One parsed line of recipients.txt. The file's grammar, stated once.

    Both passes over the file go through this. Parsing the key inline in one of
    them and through a helper in the other is how the two drift: a field added
    to the line format then has to be added to two parsers that must agree, and
    nothing fails when only one of them is updated.
    """

    withdrawn: bool
    key: str
    #: Whatever followed the key. Nothing reads it: enrolment labels are gone
    #: and a tombstone's is a constant. Kept because `_parse` has to know the
    #: grammar in order to strip it from lines older versions wrote.
    label: str


def _parse(line: str) -> _Line:
    """One line of recipients.txt, as a key this machine can compare.

    The key is normalised here, not only where one is produced, and that is what
    makes the property `crypto.recipient_for` claims actually hold. A line
    written before SSH comments were stripped still carries `user@host`, so a
    producer-side strip alone enrols the same key a second time -- same
    fingerprint, listed twice in `grant`, and a tombstone matching only one of
    the two spellings. Normalising what is read makes enrolment, deduplication
    and withdrawal agree by construction, including with the file's own past.
    """
    key, _, label = line.removeprefix(_REVOKED).partition(_LABEL_SEP)
    return _Line(line.startswith(_REVOKED), crypto.without_comment(key.strip()), label.strip())


def _revoked_in(lines: list[str]) -> set[str]:
    parsed = (_parse(line) for line in lines)
    return {entry.key for entry in parsed if entry.withdrawn}


def revoked_keys() -> set[str]:
    """Every key withdrawn by a tombstone, whether or not it is still listed."""
    return _revoked_in(_recipient_lines())


def _append_recipient_line(line: str) -> None:
    """Add one line to recipients.txt, keeping it one line per record.

    The only writer. Both callers -- enrolling a key and withdrawing one --
    need the same three things right: append rather than rewrite, do not run
    two records together when the file has no trailing newline, and replace the
    file atomically. `revoke` grew its own copy of that first, which put the
    newline rule in two places on the one file every machine in the fleet
    writes and that ``merge=union`` makes unforgiving about stray lines.
    """
    path = store.recipients_file()
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    store.write_atomic(path, f"{existing}{separator}{line}\n".encode())


def _own_recipient(known: Machine) -> str:
    return crypto.recipient_for(identity_path(known)).strip()


def _this_machine_revoked(known: Machine) -> bool:
    """Whether this machine is the one that was withdrawn.

    Answerable locally and without any key: `recipients.txt` is plaintext, a
    revoked machine can still fetch it, and its own public key is on disk. Which
    is the point -- the machine that most needs to be told is the one that can
    no longer decrypt anything to be told with.
    """
    try:
        return _own_recipient(known) in revoked_keys()
    except (WoswoarError, OSError):
        return False


def recipients() -> list[str]:
    """Every enrolled machine's public key, in the form age wants on `-r`.

    Deduplicated, because `.gitattributes` marks the file ``merge=union``, so
    two machines appending the same key leaves both lines and age rejects a
    repeated recipient.

    Tombstones are collected in a pass of their own first, not skipped as they
    are met: union merges interleave two machines' appends in whatever order the
    rebase produces, so a withdrawal can perfectly well land *above* the
    enrolment it withdraws. Subtracting in one pass would then depend on line
    order, which nothing guarantees.

    Withdrawal is deliberately permanent: a key that reappears below its own
    tombstone stays out. Re-enrolling a machine means giving it a new identity,
    which is the cheap half of the operation -- whereas "whoever the revocation
    was aimed at can un-revoke themselves by appending the line again" is not a
    property worth having, and they have push access by assumption.

    Read here rather than handed to age as a path: `crypto` never names a file
    in $HOME, because a sandboxed age cannot open one.
    """
    lines = _recipient_lines()
    revoked = _revoked_in(lines)

    out: dict[str, None] = {}
    for line in lines:
        # Tombstones need no case of their own: their key is in `revoked` by
        # construction, so the same test that drops the enrolment drops them.
        key = _parse(line).key
        if key and key not in revoked:
            out.setdefault(key, None)
    return list(out)


#: Stands in for a machine whose name this one has not learned yet -- it has
#: enrolled but its `name.age` has not been merged here. Not a name, which is
#: why `Reader.shares_name` ignores it: three machines waiting to be named do
#: not share anything, and saying they do turns the one signal that means
#: "look closely" into noise on an ordinary first sync.
UNNAMED = "(unnamed)"


def _host_owners() -> dict[str, str]:
    """``owning recipient -> host id``, from what each host publishes.

    Built once per prompt rather than rescanned per recipient: `read_signer`
    opens a file per host, so asking "which host owns this key?" separately for
    each of them is quadratic -- 0.35 ms at five machines, 89 ms at a hundred.

    Untrusted, like everything else `signer.pub` says: it decides a *label*, and
    a label is shown quoted beside a fingerprint that cannot be chosen.
    """
    owners: dict[str, str] = {}
    for host_id in store.repo_hosts():
        signer = read_signer(host_id)
        if signer is not None:
            owners.setdefault(signer.owner, host_id)
    return owners


def name_of(recipient: str) -> str:
    """A human name for an enrolled machine, for a prompt to show.

    Not read from `recipients.txt`, and that is the point of #22. The label
    woswoar used to write there was `$USER@$(uname -n)`, published in cleartext
    in the one file of the repo that is deliberately not encrypted -- while the
    host directories beside it are opaque hex precisely so a leaked archive does
    not name the machines. The name was already being published *sealed*, in
    `hosts/<id>/name.age`, so it is taken from there instead.

    Resolved through the local mirror `_merge_name` writes, so this costs no
    subprocess and works for a machine that has synced. One that has not shows
    ``(unnamed)`` and its fingerprint, which is the identity anyway -- a name is
    a convenience and was never the thing being consented to.
    """
    return name_for(_host_owners().get(recipient)).text


class Name(NamedTuple):
    """A machine's name, and whether it is one.

    ``known`` is a field rather than a comparison against `UNNAMED` because the
    placeholder is a string a *peer* can write: sealing a `name.age` needs no
    secret, so anything spelled here can be spelled there. Telling the two apart
    by their text would let a machine call itself `(unnamed)` and slip out of
    the duplicate-name warning it is the point of.
    """

    text: str
    known: bool


def name_for(host_id: str | None) -> Name:
    """The name a host publishes, or the placeholder if this machine has none.

    Read from the local mirror `_merge_name` writes rather than
    `store.host_names()`, whose fallback is the opaque id -- an id beside a
    fingerprint is two unreadable strings where one would do.
    """
    if host_id is None:
        return Name(UNNAMED, known=False)
    name = store.host_name(host_id)
    return Name(make_inert(name), known=True) if name else Name(UNNAMED, known=False)


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
    #: "<host>/<day>" -> the chunk filenames already merged into logs/.
    #:
    #: A set, not a high-water mark. A single "highest name seen" cannot say
    #: "all of these except that one", and chunk names are only loosely ordered:
    #: two written in the same wall-clock second share a timestamp and differ by
    #: a random suffix. So a mark got it wrong in both directions -- a chunk that
    #: failed while a later-named one succeeded was skipped for good, and a
    #: chunk that failed while being the newest was re-read every minute forever.
    #:
    #: A set here and a sorted list in the file. JSON has no set, and converting
    #: at that boundary is the only place that knows about the file -- `merge`
    #: kept its own mirror to get a fast membership test, which was one more
    #: thing to keep in step and rebuilt itself once per chunk.
    merged: dict[str, set[str]] = field(default_factory=dict)
    #: Keys a human at this machine last approved in `grant`. Local like the
    #: rest of State, and here that is the whole point: "which machines are new
    #: since *you* last agreed to this?" is a question only this machine can
    #: answer, and a record kept in the repo could be edited by exactly the
    #: attacker the confirmation exists to catch.
    granted: list[str] = field(default_factory=list)
    #: host id -> the signing key this machine accepts history from that host
    #: under. *The* trust anchor, and local for the reason the rest of this class
    #: is: everything in the repo can be rewritten by anyone who can push,
    #: `hosts/<id>/signer.pub` included, so the key a host is held to has to be
    #: remembered somewhere the remote cannot reach.
    signers: dict[str, str] = field(default_factory=dict)
    #: "<host>/<day>" -> the compacted chunks the local copy of that day was last
    #: rebuilt from, sorted.
    #:
    #: A compacted chunk keeps its `subsumes` list in the signed manifest for
    #: good, so "does this day contain a compacted chunk" stays true forever and
    #: cannot mean "rebuild it". Keyed on the *set* rather than a count or a
    #: flag: compacting twice replaces one compacted chunk with another, and
    #: neither the count nor a flag would change.
    rebuilt: dict[str, list[str]] = field(default_factory=dict)
    #: What was on disk when this was loaded, so `save` can tell whether there
    #: is anything to write. Not part of the state itself.
    _loaded: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def load(cls) -> State:
        raw = store.load_json(store.state_file())
        exported = raw.get("exported", {})
        merged = raw.get("merged", {})
        granted = raw.get("granted", [])
        signers = raw.get("signers", {})
        rebuilt = raw.get("rebuilt", {})
        if not isinstance(exported, dict) or not isinstance(merged, dict):
            return cls()
        state = cls(
            exported={str(k): int(v) for k, v in exported.items()},
            # Guarded per value, not just on `merged` being a dict. A state
            # file from before this became a set holds one *string* per day, and
            # a string is iterable: without this, every day's record turns into
            # its own characters, every chunk looks unmerged, and the first sync
            # after upgrading duplicates every peer's history wholesale -- which
            # is the bug this change exists to fix.
            merged={
                str(k): {str(name) for name in v} for k, v in merged.items() if isinstance(v, list)
            },
            # A malformed list costs the prompt its memory, which shows every
            # machine as new -- the safe direction to be wrong in.
            granted=[str(k) for k in granted] if isinstance(granted, list) else [],
            # And a malformed map costs every pin, which refuses every host until
            # a human re-runs `trust` -- also the safe direction, and loud,
            # because `sync` reports the untrusted hosts rather than skipping on.
            signers={str(k): str(v) for k, v in signers.items()}
            if isinstance(signers, dict)
            else {},
            # A malformed record costs one rebuild of that day, which re-reads
            # chunks it already had. Wrong in the direction that repeats work
            # rather than the one that drops history.
            rebuilt={
                str(k): [str(name) for name in v] for k, v in rebuilt.items() if isinstance(v, list)
            }
            if isinstance(rebuilt, dict)
            else {},
        )
        state._loaded = state.as_json()
        return state

    def as_json(self) -> dict[str, object]:
        """This state as the file holds it.

        Every container is rebuilt rather than shared, so the snapshot `save`
        compares against cannot be mutated from underneath by whoever holds the
        `State`.
        """
        return {
            "exported": dict(self.exported),
            "merged": {key: sorted(names) for key, names in self.merged.items()},
            "granted": list(self.granted),
            "signers": dict(self.signers),
            "rebuilt": {key: list(names) for key, names in self.rebuilt.items()},
        }

    def save(self) -> None:
        """Write, unless nothing changed since it was loaded.

        Recording every merged chunk name rather than a high-water mark made
        this file two orders of magnitude bigger -- about 1 MiB at 35k chunks --
        and `run` saves at the end of every sync, on a one-minute timer. Writing
        a megabyte a minute to say nothing happened is a lot of disk for no
        information, and an idle sync is the overwhelmingly common one. `cache`
        makes the same call for the same reason.
        """
        current = self.as_json()
        if current == self._loaded:
            return
        store.save_json(store.state_file(), current)
        self._loaded = current


@contextmanager
def lock() -> Iterator[None]:
    """Serialise syncs. A prompt-triggered sync and the timer can collide."""
    path = store.private_dir(store.data_dir()) / "sync.lock"
    # Through the same helper as everything else. The file is empty and holds
    # nothing secret, but `doctor` walks the data directory and a stray 0644
    # here would report an exposure that is not one -- an alarm that is wrong
    # in the harmless direction still teaches people to ignore it.
    handle = store.private_append(path)
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


def signing_status(known: Machine) -> IdentityStatus:
    """Whether this machine can sign what it publishes, for `doctor` to report.

    Lives here rather than in `doctor` for the same reason `identity_status`
    does: the judgement is testable and stated once. Worth surfacing because a
    machine that cannot sign publishes history no peer will ever accept -- it
    keeps recording, looks healthy, and says so only on a timer's stderr.

    Checked by actually signing and verifying rather than by looking at the
    file, the same argument `why_unusable` makes about age: what matters is
    whether an unattended sync will work, not what the key file looks like.
    """
    if not crypto.signing_available():
        return IdentityStatus(False, "ssh-keygen is not installed - history cannot be signed")
    path = store.signing_key_file()
    if not path.is_file():
        return IdentityStatus(False, f"{path} is missing - it is created by 'woswoar init'")
    try:
        verify_key = crypto.signing_public(path)
        probe = b"woswoar signing selftest"
        if not crypto.verify(
            probe, crypto.sign(probe, path, _MANIFEST_MAGIC), verify_key, _MANIFEST_MAGIC
        ):
            return IdentityStatus(False, f"{path} signs, but the signature does not verify")
    except (WoswoarError, OSError) as exc:
        return IdentityStatus(False, f"{path} cannot sign: {exc}")
    return IdentityStatus(True, f"{path} ({crypto.fingerprint(verify_key)})")


def trust_status(known: Machine) -> IdentityStatus:
    """How many machines publishing here this one accepts, for `doctor`.

    Beside `signing_status` and `identity_status` for the reason their
    docstrings give: the judgement is testable and stated once, rather than
    derived inline in the CLI where only a test that greps stdout could reach
    it. Withdrawn hosts are subtracted, so a revoked machine never reads as
    accepted just because no sync has pruned the pin yet.
    """
    if not is_repo():
        return IdentityStatus(True, "no history repo yet")
    accepted = accepted_hosts(State.load())
    others = [host for host in store.repo_hosts() if host != known.id]
    waiting = [host for host in others if host not in accepted]
    detail = f"{len(others) - len(waiting)} of {len(others)} other machine(s) accepted"
    if waiting:
        detail += " - run 'woswoar trust' to accept the rest"
    return IdentityStatus(not waiting, detail)


def orphaned_day_key(host_id: str, day: str) -> bool:
    """A public half published with no sealed private half beside it.

    Anything sealed to that public key is unreadable by every machine including
    the one that wrote it, permanently -- the secret only ever existed inside
    the `.age` file. So this is the state to notice before writing more, not
    after.

    It is reachable by a stranger: `keys/` is deliberately rewritable, because
    `grant` and `revoke` re-seal every file in it, so anyone with push access can
    delete one and the deletion arrives with the next fetch. `.age` is written
    before `.pub` below, which keeps woswoar's own crash window on the harmless
    side of this -- a sealed key with no public half is simply re-minted.
    """
    return (
        store.day_key_public(host_id, day).is_file() and not store.day_key(host_id, day).is_file()
    )


def has_chunks(host_id: str, day: str) -> bool:
    """Whether anything is already sealed to this host's key for ``day``.

    What turns a half-written key pair from a nuisance into a loss: with no
    chunks the next `export` simply mints a new pair over it and nothing is
    ever noticed, so reporting that state would be a false alarm.
    """
    directory = store.chunk_dir(host_id, day)
    return directory.is_dir() and any(directory.iterdir())


def orphaned_days() -> list[tuple[str, str]]:
    """``(host, day)`` for every day key in the repo whose sealed half is gone.

    Cheap -- a listing of `keys/`, no decryption -- and worth doing on demand:
    the state is silent otherwise, and every sync that passes over such a day
    without noticing makes it look fine. Reported for *every* host, not just
    this one, because a machine that can still open the key is the only place a
    copy can come from.

    Pairs rather than a formatted string, so the caller decides how to show
    them and a test can assert on the day rather than search for it.
    """
    found = []
    for host_id in store.repo_hosts():
        sealed, public = store.day_key_days(host_id)
        for day in sorted(public - sealed):
            if has_chunks(host_id, day):
                found.append((host_id, day))
    return found


def manifest_gone(host_id: str, day: str) -> bool:
    """No signed list for a day, said once so both callers ask it the same way.

    Meaningless on its own -- a day never published has no manifest either --
    so every caller pairs it with "did this machine publish this day", which is
    its own export watermark.
    """
    return not store.day_manifest(host_id, day).exists()


def days_missing_a_manifest() -> list[str]:
    """Days this machine published and whose signed manifest is no longer there.

    `export` reports the same state, but only for a day that still has lines to
    publish -- a day that was fully exported before the manifest went is never
    looked at again, and stays silently unusable by every peer that had not
    already merged it.

    Keyed on this machine's own export watermark rather than on the presence of
    chunks, for the reason `export` gives: a chunk under our id proves only that
    somebody wrote one.
    """
    known = store.machine()
    # Every key here is a day this machine exported: the watermark is written
    # only after a chunk was sealed for it, so its presence is the claim and
    # there is no zero to filter out.
    return [
        day
        for day in sorted(store.day_of_log(relpath) for relpath in State.load().exported)
        if manifest_gone(known.id, day)
    ]


def day_public_key(known: Machine, day: str) -> str:
    """The public half of this host's key for ``day``, creating it if needed.

    One key per host per day. Kept in the clear beside the sealed private half
    so writing a chunk never has to open the sealed key first.

    Both halves are required to reuse a key. Checking only the public one meant
    a day whose sealed half had gone kept handing out a public key nobody held
    the secret for, and every chunk written afterwards was lost on the spot.
    Minting over the top is safe only because `export` refuses first for any day
    that already has chunks -- those are sealed to the old key, and a new one
    cannot help them.
    """
    pub_path = store.day_key_public(known.id, day)
    if pub_path.is_file() and store.day_key(known.id, day).is_file():
        return pub_path.read_text(encoding="utf-8").strip()

    identity = crypto.generate_identity()
    sealed = crypto.encrypt_to_recipients(identity.secret.encode("utf-8"), recipients())
    # Sealed half first: see `orphaned_day_key`.
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
# Who a host claims to be. Untrusted: anyone who can push can rewrite this.
# ---------------------------------------------------------------------------


class Signer(NamedTuple):
    """What ``hosts/<id>/signer.pub`` says about a host.

    Both halves matter and neither is believed on sight. ``verify_key`` is what
    `trust` shows a human and pins; ``owner`` is the age recipient that claims
    this host directory, and it exists so a tombstone -- which names a recipient
    -- can be turned into "stop accepting this host's chunks". Nothing else in
    the repo joins a host id to a recipient.
    """

    verify_key: str
    owner: str


def read_signer(host_id: str) -> Signer | None:
    try:
        lines = store.signer_public(host_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        return None
    return Signer(lines[0].strip(), lines[1].strip())


def publish_signer(known: Machine) -> bool:
    """Make sure this host's published signer file says what is true.

    Guarded, and the guard is not only tidiness: working out the owning
    recipient means reading an identity, which for a dedicated age key is an
    `age-keygen` subprocess. This runs on a one-minute timer, and the file
    changes about once in a machine's life. Returns whether it wrote.
    """
    verify_key = signing_public()
    current = read_signer(known.id)
    if current is not None and current.verify_key == verify_key:
        return False
    store.write_atomic(
        store.signer_public(known.id),
        f"{verify_key}\n{_own_recipient(known)}\n".encode(),
    )
    return True


def signing_public() -> str:
    """This machine's verify key, creating the signing key on first use.

    One spelling of "our own verify key". `export`, `compact` and
    `signing_status` each had their own, which is three places to change if the
    key ever moves.
    """
    path = store.signing_key_file()
    if not path.is_file():
        return crypto.generate_signing_key(path)
    return crypto.signing_public(path)


class Candidate(NamedTuple):
    """A machine `trust` could accept, as a human needs to see it.

    Shaped like `Reader`, and for the same reason: everything a confirmation
    says about a machine is decided here rather than at the print, so a test can
    reach it without grepping stdout, and so printing the attacker-written name
    unquoted is not reachable by forgetting a ``!r``.
    """

    host_id: str
    verify_key: str
    #: Free text from the recipient line. A name, not an identity.
    label: str
    #: Derived from the key and so not chooseable. This is the identity.
    fingerprint: str
    #: What this machine currently accepts from that host, if anything. Set and
    #: different from `verify_key` means the key changed, which is never
    #: resolved without a human saying which one is right.
    pinned: str | None
    #: The fingerprint of that, for a prompt that has to show both.
    pinned_fingerprint: str

    @property
    def changed(self) -> bool:
        return self.pinned is not None and self.pinned != self.verify_key

    def display_name(self) -> str:
        """The label, in a form that cannot rearrange the line it is on.

        `Reader.display_name` explains why this is a method rather than a rule
        the caller has to remember.
        """
        return repr(self.label)


def trust_candidates() -> list[Candidate]:
    """Every other machine publishing here, with what this one accepts from it.

    Fetches first, for the same reason `readers` does: the whole point is a
    machine that appeared since this one last looked.
    """
    with lock():
        repo = read_repo()
        if repo.has_remote:
            _fetch_and_rebase(repo)

        known = store.machine()
        state = State.load()
        live = set(recipients())

        out: list[Candidate] = []
        for host_id in store.repo_hosts():
            if host_id == known.id:
                continue
            signer = read_signer(host_id)
            if signer is None:
                continue
            # Only hosts whose owning recipient is *live* are offered. That
            # covers both cases in one test, because `recipients` already
            # subtracts tombstoned keys: a withdrawn machine is not in
            # `live`, and neither is a host nothing ties to a recipient -- which
            # must also stay unacceptable, or the tombstone that would stop it
            # later would have nothing to act on.
            if signer.owner not in live:
                continue
            pinned = state.signers.get(host_id)
            out.append(
                Candidate(
                    host_id=host_id,
                    verify_key=signer.verify_key,
                    label=name_for(host_id).text,
                    fingerprint=crypto.fingerprint(signer.verify_key),
                    pinned=pinned,
                    pinned_fingerprint=crypto.fingerprint(pinned) if pinned else "",
                )
            )
        return out


def trust(candidates: list[Candidate]) -> None:
    """Accept these machines here. Local only: no repo write, no push.

    The one operation that *adds* trust, and it is deliberately unable to touch
    the repository. What a machine accepts is the one thing that cannot live in
    a place anyone with push access can rewrite, so it lives in `state.json` and
    is put there by a human sitting at the machine it applies to.
    """
    state = State.load()
    for candidate in candidates:
        state.signers[candidate.host_id] = candidate.verify_key
    state.save()


def withdrawn_hosts(state: State) -> set[str]:
    """Pinned hosts whose owning recipient has since been tombstoned.

    Asked here rather than left to whoever remembers: `doctor` read the pins
    directly and reported a withdrawn machine as accepted right up until the
    next sync happened to prune it.
    """
    withdrawn = revoked_keys()
    if not withdrawn:
        return set()
    out = set()
    for host_id in state.signers:
        signer = read_signer(host_id)
        if signer is not None and signer.owner in withdrawn:
            out.add(host_id)
    return out


def accepted_hosts(state: State) -> set[str]:
    """The hosts this machine accepts history from, right now."""
    return set(state.signers) - withdrawn_hosts(state)


def apply_withdrawals(state: State, report: Report) -> None:
    """Drop the pin of every host whose owning recipient has been tombstoned.

    The one place repo state is allowed to change what this machine trusts, and
    it may only ever *remove*. That asymmetry is what makes `revoke` work across
    a fleet without a human on each machine: adding trust needs a person,
    because a repo anyone can push to could otherwise introduce a machine; but
    dropping it can only ever cause a refusal, never an injection, so a
    withdrawal anybody publishes is safe to act on immediately.

    Sticky, because the pin is local: deleting the tombstone afterwards does not
    put the pin back, and `trust` refuses a withdrawn key outright.
    """
    for host_id in sorted(withdrawn_hosts(state)):
        del state.signers[host_id]
        report.unpinned.add(host_id)


# ---------------------------------------------------------------------------
# Manifests: which chunks a host says are its own, signed so nobody else can say
# it. The whole of chunk authenticity rests on this.
# ---------------------------------------------------------------------------

#: First token of a manifest body, and the ssh-keygen signature namespace, which
#: are deliberately the same string. It is *inside the signed bytes* and also the
#: domain the signature is made in, so a future manifest shape gets one new value
#: and old signatures stop verifying against it twice over -- rather than a
#: version check somebody has to remember to write.
_MANIFEST_MAGIC = "woswoar-manifest-v1"

#: Separates the armoured signature from the bytes it covers. The signature is
#: in the same file as the body, not beside it, so `write_atomic` makes the two
#: impossible to disagree -- a detached pair has a window where one has landed
#: and the other has not, and during that window a real chunk looks forged.
_MANIFEST_SEPARATOR = "\n\n"


def digest_of(data: bytes) -> str:
    """The digest a manifest records for a chunk."""
    return hashlib.sha256(data).hexdigest()


def _manifest_header(host_id: str, day: str) -> str:
    """The line that binds a manifest to one host and one day.

    Built in one place and compared in another, so the round trip cannot be
    broken from one side only.
    """
    return f"{_MANIFEST_MAGIC} {host_id} {day}"


class Entry(NamedTuple):
    """One line of a manifest: a chunk, and what it replaced.

    ``subsumes`` is empty for an ordinary chunk and holds the names `compact`
    merged into this one otherwise. It is in the *signed* bytes because it
    decides what a peer does with the chunk, and anything that decides that has
    to be something only the publishing machine can say.
    """

    digest: str
    subsumes: tuple[str, ...] = ()


def _manifest_body(host_id: str, day: str, entries: dict[str, Entry]) -> str:
    """The signed part of a manifest: what this host claims it wrote that day.

    The header names the host and the day, so a genuine manifest cannot be
    lifted to another host's directory or another date and still verify --
    ssh-keygen's own principal matching cannot do that job (see
    `crypto.verify`), so it is done here, in the bytes the signature covers.

    Sorted, so the same set of chunks always produces byte-identical output and
    a sync that adds nothing rewrites nothing.
    """
    lines = [_manifest_header(host_id, day)]
    for name in sorted(entries):
        entry = entries[name]
        lines.append(" ".join([name, entry.digest, *entry.subsumes]))
    return "\n".join(lines) + "\n"


def write_manifest(known: Machine, day: str, entries: dict[str, Entry]) -> None:
    """Sign this host's chunk list for ``day`` and write it."""
    body = _manifest_body(known.id, day, entries)
    signature = crypto.sign(body.encode("utf-8"), store.signing_key_file(), _MANIFEST_MAGIC)
    blob = signature.decode("utf-8").strip() + _MANIFEST_SEPARATOR + body
    store.write_atomic(store.day_manifest(known.id, day), blob.encode("utf-8"))


def read_manifest(host_id: str, day: str, verify_key: str) -> dict[str, Entry]:
    """The digests ``host_id`` signed for ``day``, or ``{}`` if it did not.

    Empty rather than an exception for an unsigned, malformed, mis-signed or
    mis-addressed manifest, because callers do the same thing with all of them:
    every chunk of that day goes unverified and is refused. Distinguishing them
    would be distinguishing a truthful failure from an attacker's, which is
    exactly the split #29 tried for chunk tags and reverted -- an attacker can
    produce any of these shapes at will.
    """
    try:
        blob = store.day_manifest(host_id, day).read_text(encoding="utf-8")
    except OSError:
        return {}

    signature, separator, body = blob.partition(_MANIFEST_SEPARATOR)
    if not separator:
        return {}
    if not crypto.verify(
        body.encode("utf-8"), signature.encode("utf-8"), verify_key, _MANIFEST_MAGIC
    ):
        return {}

    lines = body.splitlines()
    # Checked *after* the signature, so this only ever parses bytes the host's
    # own key vouched for. The header binding is the point: without it a
    # manifest signed for one day would verify for every other day.
    if not lines or lines[0] != _manifest_header(host_id, day):
        return {}

    entries: dict[str, Entry] = {}
    for line in lines[1:]:
        name, _, rest = line.partition(" ")
        digest, _, subsumed = rest.partition(" ")
        if name and digest:
            entries[name] = Entry(digest, tuple(subsumed.split()))
    return entries


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


#: The most a single chunk may decompress to.
#:
#: deflate reaches about 1030:1, so an unbounded `zlib.decompress` turns a
#: 204 KB commit into 200 MiB of log and 420 MiB of RSS -- measured -- and a
#: 10 MB one into roughly 10 GB, on a timer that fires every minute and asks
#: nobody. The cap is what stops one machine deciding how much memory every
#: other machine spends.
#:
#: Sized against what a real chunk holds, measured on generated history:
#:
#:     a typical day                        0.03 MiB
#:     a very heavy day                     0.35 MiB
#:     an entire bash_history, imported     4.43 MiB
#:
#: so 64 MiB is about fifteen times the largest legitimate case anyone has --
#: importing a whole shell history in one go -- and still two hundred times
#: smaller than what the same bytes could otherwise expand to. The case it does
#: refuse is a single chunk holding tens of thousands of *maximum-length*
#: commands, which is 383 MiB of plaintext; that is legal but has never
#: happened, and refusing it is reported rather than silent.
MAX_CHUNK_BYTES = 64 * 1024 * 1024


#: Most plaintext this machine will put in one chunk.
#:
#: `MAX_CHUNK_BYTES` is what a *reader* refuses. Nothing used to stop a writer
#: exceeding it: `read_tail` is bounded by "everything since the last export",
#: not by size, so a machine importing a decade of history in one go could seal
#: a chunk every peer would then refuse -- silently and permanently, because
#: `state.exported` has already moved past those bytes and its own copy in
#: `logs/` looks fine.
#:
#: Eight times under the reader's cap rather than just below it, so the two
#: numbers can move independently and the writer never has to reason about
#: compression -- this bounds the plaintext, which is exactly what the reader
#: measures. An entire imported bash_history is 4.43 MiB, so nothing anyone
#: actually has is split at all; this is the shape of the failure, not its
#: likelihood, and one-sided invariants are the ones that stay true.
MAX_EXPORT_BYTES = 8 * 1024 * 1024


def split_for_export(data: bytes, limit: int = MAX_EXPORT_BYTES) -> Iterator[bytes]:
    """``data`` in pieces of at most ``limit`` bytes, split only at line ends.

    A piece has to be whole lines: a chunk is decrypted, decompressed and parsed
    line by line, so a record cut in half would be dropped by one reader and
    never seen by any.

    A single line longer than the limit is yielded whole rather than split,
    which is why the limit is "at most" only for lines that fit. It cannot
    happen today -- `entry.MAX_CMD_CHARS` bounds a record to about 8 KB against
    a limit of 8 MiB -- but splitting mid-record to honour a size bound would
    trade a bound nobody is near for corruption everybody would see.
    """
    start = 0
    while len(data) - start > limit:
        cut = data.rfind(b"\n", start, start + limit)
        if cut < 0:
            # No line end within the budget: take the whole over-long line.
            cut = data.find(b"\n", start + limit)
            if cut < 0:
                break
        yield data[start : cut + 1]
        start = cut + 1
    if start < len(data):
        yield data[start:]


def unpack(blob: bytes, limit: int = MAX_CHUNK_BYTES) -> bytes:
    """Inverse of :func:`pack`, refusing anything implausibly large.

    Not defensive about *shape* on purpose: zlib rejects anything that is not a
    deflate stream, so a payload written by some future format fails here rather
    than being appended to a log as garbage.

    It is defensive about *size*, which is a different question and the one the
    original version did not ask. `decompressobj` is what allows that: it stops
    at ``limit`` instead of allocating whatever the stream asks for, so the
    refusal costs one bounded buffer rather than the allocation being refused.
    """
    engine = zlib.decompressobj()
    out = engine.decompress(blob, limit)
    if not engine.eof:
        # `eof` alone, not `eof or unconsumed_tail`: a stream stopped by the
        # limit leaves both, and a truncated one leaves eof clear with no tail,
        # so the tail says nothing the first test has not. The two are told
        # apart for the message only -- to a caller they are one refusal.
        stopped_early = "is longer than" if engine.unconsumed_tail else "was truncated before"
        raise zlib.error(f"chunk {stopped_early} the {limit} byte limit")
    return out


def export(known: Machine, state: State, report: Report, now: int) -> None:
    """Seal each log file's new lines into a fresh chunk, and sign the day's list.

    The manifest is built by **extending the one this machine last signed**,
    never by listing the day's directory, and that is a security property rather
    than an optimisation.

    `merge` skips this host's own id, so a chunk somebody else planted under it
    is the one thing nothing here ever inspects. Listing the directory would
    sweep such a chunk into a manifest *this machine signs*, laundering it into
    something every peer believes -- without even the `compact` run that
    `open_chunk` was written to guard. Extending a list we already signed makes
    a chunk we did not write unrepresentable in a manifest we do sign.

    The signature covers the whole day's list every time, so a manifest is a
    complete statement of what this host published that day, not a delta.
    """
    verify_key = signing_public()

    # The tails are read first, so the loop below runs only for days that
    # actually gained lines. It used to run for every day of local history, and
    # each iteration cost an `ssh-keygen -Y verify` plus a full walk of every
    # chunk directory -- on a timer firing once a minute, over a year of logs,
    # that turned a no-op sync from 0.04 s into nearly 3 s and grew with the
    # archive forever.
    fresh: dict[str, list[tuple[str, bytes, int]]] = {}
    for log in store.iter_log_files():
        if log.host_id != known.id:
            continue  # other hosts' logs arrived decrypted; never re-export them
        data, new_offset = store.read_tail(log.path, state.exported.get(log.relpath, 0))
        if data:
            fresh.setdefault(store.day_of_log(log.relpath), []).append(
                (log.relpath, data, new_offset)
            )

    if not fresh:
        return

    # One pass for the whole host, not one per day.
    on_disk: dict[str, set[str]] = {}
    for chunk in store.iter_chunks(known.id):
        on_disk.setdefault(chunk.day, set()).add(chunk.name)

    for day, tails in sorted(fresh.items()):
        # Before the manifest, not after. Refusing does not advance
        # `state.exported`, so a refused day comes back on every sync -- and
        # `read_manifest` forks `ssh-keygen -Y verify`. Checked afterwards, one
        # orphaned day cost a fork a minute for as long as it stayed that way,
        # on a path issue #50 is already about. `day in on_disk` first because
        # it is a dict lookup and the other two are stats.
        if day in on_disk and orphaned_day_key(known.id, day):
            # See `orphaned_day_key` for what this state is. A fresh key would
            # let this sync succeed and say nothing while the day's existing
            # chunks stayed unreadable, so nothing is written. The lines stay
            # pending -- `state.exported` is not advanced -- so they are still
            # in `logs/` and publish once the sealed key is restored.
            report.orphaned.add(day)
            continue

        # A manifest this machine wrote before and that is no longer there.
        # `read_manifest` returns nothing for "absent" and for "will not verify"
        # alike, and the branch below only covers the second -- so a deletion
        # looked exactly like a day never published, and the fresh manifest
        # signed at the end of this loop listed only what this run wrote.
        # Everything published earlier that day was dropped out of it, and a
        # peer refuses a chunk no signed manifest names. The machine that did it
        # keeps its own copy in `logs/`, and the only thing said out loud was
        # `foreign`, whose message blames a planted chunk.
        #
        # The signal is this machine's own export watermark, not the presence of
        # chunks: a chunk under our id proves only that *someone* wrote one, so
        # keying on that would let anyone who can push plant one on a day we
        # have not published yet and block that day for good.
        published_before = any(relpath in state.exported for relpath, _, _ in tails)
        has_manifest = not manifest_gone(known.id, day)
        if published_before and not has_manifest:
            report.manifest_missing.add(day)
            continue

        # What this machine has already put its name to for this day. Extended,
        # never rebuilt from the directory -- see the docstring.
        listed = read_manifest(known.id, day, verify_key)
        if not listed and has_manifest:
            # There is a manifest for this day and this machine cannot verify
            # it, so it cannot tell what it has already published. Signing a
            # fresh one would silently drop every earlier chunk of that day from
            # the signed list and every peer would then refuse them -- losing
            # published history to a file somebody else was able to corrupt.
            # Nothing is written for this day, and it is reported.
            report.unsignable.add(day)
            continue

        for relpath, data, new_offset in tails:
            # Split rather than capped: a reader refuses a chunk over
            # `MAX_CHUNK_BYTES`, and the writer used to be able to exceed that
            # with no idea it had. See `MAX_EXPORT_BYTES`. A day already holds
            # many chunks, so this needs no format change and is invisible to
            # anything whose tail fits -- which is everything anyone has.
            public = day_public_key(known, day)
            for piece in split_for_export(data, MAX_EXPORT_BYTES):
                sealed = crypto.encrypt_to(pack(piece), public)
                written = store.new_chunk(known.id, day, now)
                store.write_atomic(written, sealed)
                listed[written.name] = Entry(digest_of(sealed))

                report.chunks_written += 1
                report.lines_exported += piece.count(b"\n")

            # The pieces are exactly this tail, so the watermark lands where one
            # chunk would have left it. Advancing per piece would buy nothing:
            # `state.save()` runs only at the end of a successful `run`, so a run
            # that dies part way through persists no watermark at all. What it
            # does leave is chunks in no manifest, which is issue #66.
            state.exported[relpath] = new_offset

        write_manifest(known, day, listed)

        # A chunk under this host's own id that this host never wrote. `merge`
        # skips our own id, so nothing else would ever look at it, and peers
        # would take it for ours. It is left on disk and unsigned rather than
        # deleted -- deleting evidence is not this command's job -- but it is
        # said out loud, because it means someone else can write here.
        #
        # Only for days this sync touched: checking every day would mean reading
        # every manifest, which is the cost this loop exists to avoid. A planted
        # chunk on a quiet day is refused by peers either way, because it is in
        # no manifest -- this is a report, not the defence.
        report.foreign |= {f"{day}/{name}" for name in on_disk.get(day, set()) - set(listed)}


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


def open_chunk(path: Path, expected: str) -> bytes:
    """A chunk's sealed bytes, or :class:`ValueError` if they are not the ones
    the host's signed manifest names.

    The *only* way to read a chunk. Verification lives here so that "read a
    chunk without checking it" is not expressible: `compact` once took the other
    branch and became a laundering path -- it re-sealed and re-tagged whatever
    it found under this host's own id, which `merge` never inspects, turning a
    planted chunk into one every peer would believe, and then deleted the
    evidence.

    The digest, not a tag: what makes the bytes trustworthy is that a signature
    by one specific machine covers this digest. A shared tag could be produced
    by anyone who could check it, which is what made a revoked machine able to
    keep publishing (#38).
    """
    blob = path.read_bytes()
    if digest_of(blob) != expected:
        raise ValueError(f"{path.name} is not the chunk its manifest names")
    return blob


def _trusted_signer(host_id: str, state: State, report: Report) -> str | None:
    """The key this machine accepts ``host_id``'s history under, if any.

    Three answers, and only one of them proceeds:

    - pinned, and the repo still shows the same key: go ahead;
    - pinned, and the repo now shows a different one: refuse and say so, never
      re-pin. It is either a re-enrolled machine or someone rewriting the repo,
      and nothing available here can tell those apart;
    - not pinned: refuse and say so. This is what a machine enrolled since this
      one last looked is *supposed* to look like, so it is reported as a state
      to resolve rather than as an attack.
    """
    pinned = state.signers.get(host_id)
    if pinned is None:
        report.untrusted.add(host_id)
        return None

    published = read_signer(host_id)
    if published and published.verify_key != pinned:
        report.changed_signer.add(host_id)
        return None
    return pinned


def _merge_host(known: Machine, host_id: str, state: State, report: Report) -> None:
    verify_key = _trusted_signer(host_id, state, report)
    if verify_key is None:
        return

    #: None marks a day whose key we already failed to open. Caching the failure
    #: matters as much as caching the success: without it, a machine that has
    #: not been granted access yet retries the same doomed `age -d` once per
    #: chunk rather than once per day -- tens of thousands of subprocess spawns
    #: instead of hundreds, on precisely the first sync a new machine runs.
    day_keys: dict[str, str | None] = {}

    # Grouped by day rather than streamed chunk by chunk, because whether a day
    # is being *rewritten* has to be known before deciding which of its chunks
    # to read -- and that answer is in the manifest. `iter_chunks` yields a
    # day's chunks contiguously, which is what makes one group one day.
    #
    # A rewrite replaces the day's log file outright, so it has to be rebuilt
    # from every chunk the manifest lists, not from the ones this run happens to
    # find new. Skipping the already-merged ones first, as this used to, meant a
    # single chunk appended after a compaction rewrote the day down to just that
    # chunk and discarded everything before it, on every peer, silently. That is
    # reachable by `compact` and then `import`, which writes into past days.
    #
    # Laziness is kept where it pays: a day with nothing new never reads its
    # manifest, and a manifest costs a subprocess. Over a year of three machines
    # that is the difference between ~3.6s and nearly two minutes.
    for chunk_day, group in groupby(store.iter_chunks(host_id), key=lambda c: c.day):
        chunks = list(group)
        key = f"{host_id}/{chunk_day}"
        # `get`, not `setdefault`: a day this machine only ever *looks* at --
        # never granted, or a manifest it cannot verify -- must not gain an
        # empty record that is then written out on every sync forever.
        # A copy, not the live set: `state.merged` is updated as each chunk is
        # merged below, and aliasing it made "was this already merged" answer
        # yes for a chunk this very run had just added.
        already = frozenset(state.merged.get(key, ()))
        fresh = [chunk for chunk in chunks if chunk.name not in already]
        if not fresh:
            continue

        listed = read_manifest(host_id, chunk_day, verify_key)
        # A rewrite is owed when the day's *compacted* chunks are not the ones
        # the local copy was last rebuilt from -- not merely when it has some.
        # A compacted chunk keeps its `subsumes` list in the manifest forever,
        # so "has one" stays true for the life of the day, and keying on it
        # rebuilt the whole day every time it gained anything: linear per sync,
        # quadratic over a day still being written, and a day compacted while
        # live reaches ~1440 chunks.
        compacted = sorted(name for name, entry in listed.items() if entry.subsumes)
        day = _Day(host_id, chunk_day, listed, compacted != state.rebuilt.get(key, []))
        pending = chunks if day.rewrite else fresh

        # Whether every chunk the manifest lists reached the file this pass. A
        # rewrite that lost one to an unopenable key must not be recorded as
        # done, or the day is never rebuilt again and stays short for good.
        complete = True

        for chunk in pending:
            if chunk.name not in day.listed:
                # No signed statement that this host wrote this. Covers a
                # missing, unsigned, mis-signed or mis-addressed manifest as
                # well as a chunk simply absent from a good one -- see
                # `Report.unauthenticated`.
                report.unauthenticated.add(key)
                continue

            if chunk_day not in day_keys:
                try:
                    day_keys[chunk_day] = open_day_key(known, host_id, chunk_day)
                except (crypto.AgeError, SyncError):
                    # Sealed before this machine was enrolled, and no one has
                    # run `grant` yet. Skip it without recording it as merged,
                    # so a later sync picks it up -- and above all without
                    # aborting, or one unreadable day would block this machine's
                    # own export too.
                    day_keys[chunk_day] = None
            secret = day_keys[chunk_day]
            if secret is None:
                report.unreadable.add(key)
                complete = False
                continue

            # Authenticated before it is decrypted or decompressed, so age, zlib
            # and the parser only ever see bytes one of this user's own machines
            # wrote.
            try:
                blob = open_chunk(chunk.path, day.listed[chunk.name].digest)
            except (OSError, ValueError):
                report.unauthenticated.add(key)
                complete = False
                continue

            try:
                plaintext = unpack(crypto.decrypt_with_secret(blob, secret))
            except (crypto.AgeError, zlib.error, OSError):
                # Same judgement as an unopenable day key above: a chunk we
                # cannot consume -- damaged, written by a woswoar that packs it
                # some other way, or expanding past `MAX_CHUNK_BYTES` -- must not
                # abort the sync. Aborting would block this machine's own export
                # and every other host's readable chunks, on this run and every
                # run after it.
                report.unreadable.add(key)
                complete = False
                continue

            day.add(plaintext, report)
            state.merged.setdefault(key, set()).add(chunk.name)
            if chunk.name not in already:
                report.chunks_merged += 1

        day.flush(report)
        if compacted and complete:
            state.rebuilt[key] = compacted


class _Day:
    """One host-day's merged plaintext, on its way to the log file.

    Exists so the two things that decide *when* bytes are written -- the day
    boundary and `FLUSH_BYTES` -- are in one place rather than interleaved with
    decryption, and so "this day is being rewritten" is settled once, from the
    manifest, instead of discovered part way through.

    That last part is not tidiness. A day being rewritten has to be written in a
    single atomic replacement, so it must never be flushed early; working that
    out when the subsuming chunk turns up is too late, because chunks before it
    may already have gone to the file and the replacement would drop them.
    """

    def __init__(self, host_id: str, day: str, listed: dict[str, Entry], rewrite: bool) -> None:
        self.host_id = host_id
        self.day = day
        self.listed = listed
        # Decided by the caller, which is the only place that knows what this
        # machine last rebuilt the day from. A compacted chunk is a complete
        # statement of the chunks it names, so a day that has gained one is
        # replaced rather than added to -- the only answer right whether the
        # peer had merged all of them, some, or none.
        self.rewrite = rewrite
        self._blocks: list[bytes] = []
        self._bytes = 0

    def add(self, plaintext: bytes, report: Report) -> None:
        self._blocks.append(plaintext)
        self._bytes += len(plaintext)
        if self._bytes > FLUSH_BYTES and not self.rewrite:
            self._write(report, rewrite=False)

    def flush(self, report: Report) -> None:
        if self._blocks:
            self._write(report, rewrite=self.rewrite)

    def _write(self, report: Report, rewrite: bool) -> None:
        payload = b"".join(self._blocks)
        self._blocks = []
        self._bytes = 0
        path = store.log_file(self.host_id, self.day)
        if rewrite:
            # One atomic replacement, not unlink-then-append: search runs
            # outside the sync lock and reads these files, so a truncate leaves
            # a window where a day looks empty or half-written, and a crash in
            # it loses the day silently. `write_atomic` also owns the 0600.
            before = _line_count(path)
            store.write_atomic(path, payload)
            # Only the growth. A peer that already held the whole day and then
            # received its compaction gained nothing, and saying it merged five
            # thousand lines would be a lie in the reassuring direction.
            report.lines_imported += max(0, payload.count(b"\n") - before)
        else:
            # Another machine's history is no less private than this one's, and
            # it lands in the same tree, so it is created with the same mode.
            # Both write paths make the directory themselves.
            with store.private_append(path, binary=True) as handle:
                handle.write(payload)
            report.lines_imported += payload.count(b"\n")


#: How much merged plaintext to hold before writing it out. Not a correctness
#: bound -- `MAX_CHUNK_BYTES` is -- just the point at which holding more stops
#: buying fewer file opens. One open per day was the old behaviour and is still
#: what happens for any host with less than this waiting.
FLUSH_BYTES = 8 * 1024 * 1024


def _line_count(path: Path) -> int:
    try:
        return path.read_bytes().count(b"\n")
    except OSError:
        return 0


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
    store.write_atomic(local, name)


# ---------------------------------------------------------------------------
# The sync itself
# ---------------------------------------------------------------------------


def run(push: bool = True, now: int | None = None) -> Report:
    """Export, exchange with the remote, import. Safe to run concurrently."""
    crypto.require()
    crypto.require_signing()
    if not is_repo():
        raise SyncError("no history repo yet; run 'woswoar init' first")

    known = store.machine()
    if not store.recipients_file().is_file():
        raise SyncError("no recipients.txt in the history repo; run 'woswoar init'")

    repo = read_repo()
    _ensure_repo_config(repo)
    report = Report()
    remote = push and repo.has_remote

    with lock():
        state = State.load()

        # Take the remote's view *before* exporting. Creating a day key seals it
        # to whatever recipients.txt says right now, so exporting against a
        # stale list would produce a key that machines enrolled since the last
        # sync can never open -- silently, and permanently, because the repo is
        # append-only.
        in_sync = _fetch_and_rebase(repo) if remote else False

        # Before anything is trusted, and only ever subtracting -- see
        # `apply_withdrawals`.
        apply_withdrawals(state, report)

        # Publishing needs no permission from anyone: this machine signs with a
        # key of its own and seals to a day key it mints itself. Only *reading*
        # waits for `grant`. That is a change from the shared-key design, where
        # a machine nobody had granted access to could not tag a chunk either,
        # so its own history piled up locally until someone else acted.
        report.revoked = _this_machine_revoked(known)
        if not report.revoked:
            publish_signer(known)
            export(known, state, report, int(time.time()) if now is None else now)

        committed = _commit()

        if remote:
            # `push` contacts the remote even with nothing to send, which is the
            # slowest thing an idle run does. If the fetch above found us level
            # with the remote and nothing was committed since, there is nothing
            # to send -- and `pushed` still holds, since it means this machine's
            # history is on the remote, not that bytes moved.
            if committed or not in_sync:
                _push(repo)
            report.pushed = True

        merge(known, state, report)
        state.save()

    return report


#: Object ids as `rev-parse` prints them. Both lengths, because a repo may be
#: SHA-256 rather than SHA-1 -- woswoar never creates one, but it is handed the
#: repo the user cloned.
_HEX = frozenset("0123456789abcdef")


def _resolve(*refs: str) -> list[str]:
    """Resolve several refs in one fork; an unresolvable one comes back empty.

    `rev-parse --verify` takes a single ref, so asking about two costs two forks.
    Plain `rev-parse` takes any number, but *echoes* an argument it could not
    resolve instead of failing that line, hence the shape check.
    """
    printed = git("rev-parse", *refs, check=False).splitlines()
    if len(printed) != len(refs):
        return [""] * len(refs)
    return [line if len(line) in (40, 64) and _HEX.issuperset(line) else "" for line in printed]


def _fetch_and_rebase(repo: Repo) -> bool:
    """Adopt the remote's history under ours; say whether we now match it.

    Fetch-then-rebase rather than ``git pull --rebase`` because the tracking ref
    may legitimately not exist: cloning an *empty* remote configures a branch
    pointing at nothing, which is the normal state for the first machine to
    enrol. Checking the fetched ref directly handles that and the ordinary case
    with one code path.

    HEAD is resolved in the same fork, and a HEAD already equal to the fetched
    ref needs no replaying at all -- the shape of every idle run of the
    one-minute timer. Returning that fact lets the caller skip a push that would
    contact the remote to send nothing.

    The rebase cannot conflict over chunks -- every machine only ever adds files
    below its own host id. ``recipients.txt`` is the single shared file, and
    ``.gitattributes`` marks it ``merge=union`` so both sides' keys survive.

    Do not answer the question with ``merge --ff-only`` succeeding. Merging a ref
    that is already an *ancestor* of HEAD succeeds too -- it is a no-op -- so a
    machine holding commits nobody else has would read as level and never
    publish them. Only where HEAD lands says anything.
    """
    git("fetch", "--quiet", "origin")
    local, upstream = _resolve("HEAD", f"refs/remotes/origin/{repo.branch}")
    if not upstream:
        return False
    if local == upstream:
        return True
    try:
        git("rebase", "--autostash", f"origin/{repo.branch}")
    except SyncError:
        git("rebase", "--abort", check=False)
        raise
    # A rebase with nothing of its own to replay lands exactly on the fetched
    # ref: this machine only received. Worth one fork on the path that just
    # rebased -- never on the idle path -- because it saves opening a connection
    # to the remote to send it nothing, on every machine every time any peer
    # records a command.
    return _resolve("HEAD")[0] == upstream


def _push(repo: Repo) -> None:
    """Publish, retrying once if someone else pushed while we worked."""
    try:
        git("push", "--quiet", "-u", "origin", repo.branch)
    except SyncError:
        _fetch_and_rebase(repo)
        git("push", "--quiet", "-u", "origin", repo.branch)


#: Commit metadata is one of the few things in the repo that is *not* encrypted,
#: so it is pinned to a fixed identity rather than inheriting the user's real
#: name and email from their global gitconfig. Set on the repo so that rebase
#: and commit alike pick it up, including on a machine with no gitconfig at all.
COMMIT_NAME = "woswoar"
COMMIT_EMAIL = "woswoar@localhost"


class Repo(NamedTuple):
    """What a command needs to know about the git repo, read once up front.

    Every field was its own `git` fork before, repeated by each helper that
    wanted it: `user.name` and `user.email` read every run to be written
    approximately never, `git remote`, and `branch --show-current` twice per
    sync. None of it can change under a command -- woswoar never checks a branch
    out, and the identity is written at `init` -- so reading it once and passing
    it down is the whole saving, on a timer that fires every minute.

    `git remote` needs no fork of its own: it prints the names in the
    `remote.<name>.url` keys of the same local config read here.
    """

    has_remote: bool
    name: str
    email: str
    branch: str


def read_repo() -> Repo:
    values: dict[str, str] = {}
    for line in git("config", "--list", "--local", check=False).splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    has_remote = any(key.startswith("remote.") and key.endswith(".url") for key in values)
    return Repo(
        has_remote=has_remote,
        name=values.get("user.name", ""),
        email=values.get("user.email", ""),
        # Only asked for when there is a remote to name it to: every reader of
        # this field is inside an `if has_remote`, and a local-only install
        # would otherwise fork once a minute for a string nothing reads.
        #
        # `branch --show-current` rather than `rev-parse --abbrev-ref HEAD`,
        # which cannot answer on an unborn HEAD -- exactly the state `init` is
        # in when it enrols the first machine.
        branch=git("branch", "--show-current") if has_remote else "",
    )


def _ensure_repo_config(repo: Repo) -> None:
    """Pin the commit identity, writing only what is actually wrong."""
    if repo.name != COMMIT_NAME:
        git("config", "user.name", COMMIT_NAME)
    if repo.email != COMMIT_EMAIL:
        git("config", "user.email", COMMIT_EMAIL)


def _commit() -> bool:
    # `--verbose` prints one "add '<path>'"/"remove '<path>'" line per staged
    # path and nothing at all when the index already matches, so the same
    # command that stages also answers "is there anything to commit". The
    # obvious `status --porcelain` afterwards costs a second full stat of a
    # working tree that is tens of thousands of chunks after a couple of years,
    # on a timer that now fires every minute.
    if not git("add", "-A", "--verbose"):
        return False
    git("commit", "-q", "-m", COMMIT_MESSAGE)
    return True


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
    store.write_atomic(dedicated, identity.secret.encode("utf-8"))
    dedicated.chmod(0o600)
    return dedicated


def initialise(
    remote: str | None = None,
    new_identity: bool = False,
    identity: Path | None = None,
) -> tuple[Machine, Path, list[str]]:
    """Create or clone the history repo and enrol this machine in it.

    Returns the pinned verify keys alongside the machine and its identity,
    because trust on first use is the sharpest edge in the design and the only
    guard against a repository that lied at exactly this moment is a human
    reading what was accepted. A caller that cannot show it cannot warn.
    """
    crypto.require()
    crypto.require_signing()

    chosen = choose_identity(new_identity=new_identity, explicit=identity)
    known = store.machine()._replace(identity=str(chosen))
    store.save_machine(known)

    history = store.history_dir()
    if not is_repo():
        store.private_dir(history.parent)
        if remote and not any(history.glob("*")):
            git("clone", "--quiet", remote, str(history), cwd=history.parent)
        else:
            history.mkdir(parents=True, exist_ok=True)
            git("init", "--quiet", "-b", "main")
    if remote and not has_remote():
        git("remote", "add", "origin", remote)

    # After the remote is added, not before: that is what decides `has_remote`.
    repo = read_repo()
    _ensure_repo_config(repo)

    # Adopt the remote's state before enrolling, so we append to the real
    # recipients.txt rather than creating a competing one.
    publishing = repo.has_remote
    if publishing:
        _fetch_and_rebase(repo)

    if _write_repo_metadata(known, chosen):
        _commit()

    # Trust on first use, and only here. Every host already in the repo when
    # this machine cloned is pinned: the user named the remote, and at this
    # moment there is nothing to tell one host in it from another. From now on a
    # host that appears is refused until a human here runs `woswoar trust`.
    #
    # The pinned set is printed by `init`, because this is the sharpest edge in
    # the design and silence would make it invisible.
    state = State.load()
    pinned: list[str] = []
    for host_id in store.repo_hosts():
        signer = read_signer(host_id)
        if signer is not None and host_id not in state.signers:
            state.signers[host_id] = signer.verify_key
            pinned.append(signer.verify_key)
    state.save()

    # Publish immediately. Until this machine's public key is on the remote,
    # `grant` run elsewhere cannot include it, so onboarding would appear to
    # succeed and then silently fail to grant access to any older history.
    if publishing:
        _push(repo)

    return known, chosen, pinned


def _write_repo_metadata(known: Machine, identity: Path) -> bool:
    """Ensure this machine is enrolled. Returns whether anything changed."""
    changed = False

    attrs = store.history_dir() / store.GITATTRIBUTES
    current = attrs.read_text(encoding="utf-8") if attrs.is_file() else ""
    if current != store.GITATTRIBUTES_CONTENT:
        store.write_atomic(attrs, store.GITATTRIBUTES_CONTENT.encode("utf-8"))
        changed = True

    recipient = crypto.recipient_for(identity).strip()
    if add_recipient(recipient):
        changed = True

    # Published before the first chunk is, so a peer that fetches between
    # enrolment and the first sync sees a host it can already be asked about.
    if publish_signer(known):
        changed = True

    seal = store.name_seal(known.id)
    if not seal.is_file():
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


class Reader(NamedTuple):
    """One enrolled machine, as a human has to see it before granting.

    Everything a confirmation needs to say about a machine is decided here
    rather than at the print, including the two facts that depend on the rest of
    the list. They were computed in the CLI first, which put security-relevant
    conclusions somewhere only a test that greps stdout could reach them.
    """

    key: str
    #: Free text written by whoever added the key. A name, not an identity.
    label: str
    #: Derived from the key and so not chooseable. This is the identity.
    fingerprint: str
    #: Not among the keys this machine last granted. These are what a
    #: confirmation is actually about: a machine that was already granted can
    #: read everything already, so re-listing it only makes the one extra line
    #: that matters easier to miss.
    is_new: bool
    #: The machine the human is sitting at.
    is_mine: bool
    #: Another key in the list carries the same name. Legitimate -- two machines
    #: really can both be `martin@laptop` -- and also exactly what a key added by
    #: someone else looks like, which is why it is said out loud.
    shares_name: bool

    def display_name(self) -> str:
        """The label, in a form that cannot rearrange the line it is printed on.

        `make_inert` has already removed C0, so this is about the rest: `repr`
        escapes everything Python calls unprintable -- a bidi override among
        them -- and quotes the result, so leading and trailing whitespace is
        visible rather than shifting the columns around it.

        A method rather than a rule the caller has to remember: printing
        `label` directly is the bug this whole change exists to fix, and it
        should not be reachable by forgetting a `!r`.
        """
        return repr(self.label)


def readers() -> list[Reader]:
    """Who would be able to read the whole history if `grant` ran now.

    Fetches first, because the point of granting is to include a machine that
    enrolled since this one last looked -- reporting the pre-fetch list would
    show fewer machines than the operation is about to authorise, which is the
    one direction a security confirmation must never be wrong in.

    One read of recipients.txt, not two. The list shown to the human and the
    list handed back to `grant` as their answer used to come from separate
    parses either side of the prompt, so they could describe different sets.
    """
    with lock():
        repo = read_repo()
        if repo.has_remote:
            _fetch_and_rebase(repo)
        granted = set(State.load().granted)
        enrolled = recipients()
        owners = _host_owners()

        # Not fatal if this machine's own key cannot be worked out: the list is
        # still true, it just loses the "(this machine)" note. `grant` is the
        # command someone runs *because* something is wrong with enrolment.
        try:
            mine = crypto.recipient_for(identity_path(store.machine())).strip()
        except (WoswoarError, OSError):
            mine = ""

        labelled = [(key, name_for(owners.get(key))) for key in enrolled]
        names = Counter(name.text for _, name in labelled if name.known)
        return [
            Reader(
                key=key,
                label=name.text,
                fingerprint=crypto.fingerprint(key),
                is_new=key not in granted,
                is_mine=key == mine,
                shares_name=name.known and names[name.text] > 1,
            )
            for key, name in labelled
        ]


class _AccessChange(NamedTuple):
    identity: Path
    known: Machine
    remote: bool


@contextmanager
def _access_change() -> Iterator[_AccessChange]:
    """The protocol every command that changes who can read history follows.

    Fetch, act under the lock, commit, push -- in that order, and the order is
    the point. Reading `recipients.txt` before the fetch re-seals to a stale
    list and reports full success while granting or revoking nothing, which is
    a silent wrong answer rather than a failure.

    `_reseal` factored out the *loop* the access-changing commands share; this
    is the transaction around it, which is the half that encodes the ordering.
    Leaving it copied per command made the hazard `_reseal`'s docstring names
    -- picked up by widening access and forgotten by narrowing it -- true one
    level up, where nothing would fail loudly.

    Raising inside the block skips the commit and the push, so a refusal leaves
    the working tree as it was rather than half-applied.
    """
    crypto.require()
    if not is_repo():
        raise SyncError("no history repo yet; run 'woswoar init' first")

    known = store.machine()
    identity = identity_path(known)
    repo = read_repo()
    _ensure_repo_config(repo)
    remote = repo.has_remote

    # The same lock sync takes: this rewrites files in the working tree, and a
    # timer-driven sync must not be reading them halfway through.
    with lock():
        if remote:
            _fetch_and_rebase(repo)
        yield _AccessChange(identity, known, remote)
        _commit()
        if remote:
            _push(repo)


def _reseal(identity: Path, keys: list[str]) -> tuple[int, int]:
    """Re-seal every sealed-to-recipients file to ``keys``. ``(resealed, skipped)``.

    The mechanism both `grant` and `revoke` are made of, and the reason they are
    two commands rather than one flag: the files rewritten and the way they are
    rewritten are identical, and only the intent behind the recipient list
    differs. One loop means a file that starts being sealed to the recipients
    cannot be picked up by widening access and forgotten by narrowing it.

    Only a machine that is *already* a recipient can do this: re-sealing means
    opening the existing file first. That is the point rather than a limitation
    -- if a machine nobody had granted access to could re-seal old keys, the
    encryption would not be worth anything.
    """
    # Only what is sealed *to the recipients* is here. Signing keys are not:
    # nobody but the machine that made one ever needs it, which is exactly why a
    # revoked machine keeping its copy buys it nothing.
    sealed: list[Path] = []
    for host_id in store.repo_hosts():
        sealed.extend(store.iter_day_keys(host_id))
        sealed.append(store.name_seal(host_id))

    resealed = skipped = 0
    for path in sealed:
        if not path.is_file():
            continue
        try:
            plain = crypto.decrypt_with_file(path.read_bytes(), identity)
        except (crypto.AgeError, OSError):
            # Not ours to re-seal: keys sealed before this machine joined, or
            # left behind by one since removed.
            skipped += 1
            continue
        store.write_atomic(path, crypto.encrypt_to_recipients(plain, keys))
        resealed += 1
    return resealed, skipped


def grant(approved: list[str] | None = None) -> ReencryptReport:
    """Re-seal every key file to the current recipient list, and publish it.

    Named for what it does to the user's history rather than to the files:
    afterwards, every machine in `recipients.txt` can read *everything*,
    including days recorded before it existed. `reencrypt` described the
    mechanism and hid that.

    ``approved`` is the list a human at this machine agreed to, and passing it
    asserts exactly that. It does two jobs, both following from that one
    meaning: if the fetch below turns up a different list -- someone enrolled a
    machine in the meantime -- this refuses rather than granting access to a
    machine nobody approved; and the list is remembered, so the next
    confirmation can show what has appeared since. Omit it and neither happens,
    which is what an unattended caller wants.

    This is what makes a newly onboarded machine able to read *old* history.
    Chunks are encrypted to per-day keys, so only those small key files -- and
    the name seals -- have to be rewritten; the tens of thousands of chunks are
    untouched. Any machine already enrolled can do it for every host, because
    all hosts seal their keys to the same recipient list.

    `_reseal` explains why only an already-enrolled machine can do this, so the
    new machine cannot onboard itself and one that cannot open a key skips it.

    Fetching first is not optional, for the same reason `run` exports after
    fetching: the recipient list this re-seals to is a *file in the working
    tree*, and the whole purpose of the operation is that a machine enrolled
    since the last sync appears in it. Re-sealing against a stale checkout
    reports full success and grants exactly nothing.

    This is one of only two operations that rewrite an existing file. It is
    deliberately explicit rather than part of sync.
    """
    with _access_change() as change:
        # Read *after* the fetch `_access_change` did, never before. The whole
        # point of this command is to seal to a machine that enrolled since the
        # last sync, so taking the list from a pre-fetch checkout would re-seal
        # everything to the old recipients and report full success. Reading it
        # once here also saves re-parsing the file for every one of a few
        # thousand key files.
        keys = recipients()
        if approved is not None and sorted(keys) != sorted(approved):
            raise SyncError(
                "the set of machines changed while you were deciding; "
                "run 'woswoar grant' again to see the current list"
            )

        resealed, skipped = _reseal(change.identity, keys)

        if approved is not None:
            # Recorded rather than counted: what the next confirmation subtracts
            # is the set a human agreed to, not the set that happened to be
            # re-sealable from here. A machine that could open nothing still
            # approved these, and a grant with no approved list behind it -- an
            # unattended one -- must not silence the next prompt.
            state = State.load()
            state.granted = keys
            state.save()

    return ReencryptReport(resealed, skipped, pushed=change.remote)


class RevokeReport(NamedTuple):
    """What `revoke` did, and -- just as much -- what it could not do."""

    resealed: int
    skipped: int
    pushed: bool
    #: Days *this host* has a key for already and is still recording into, so
    #: commands added to them stay readable by the revoked key. In practice
    #: today. Reported rather than left to be worked out, because it is the one
    #: gap someone might act on.
    #:
    #: Per-host, and the wording that carries it says so: every other enrolled
    #: machine has the same gap for the days it is recording into, and this one
    #: cannot see which those are.
    still_readable: list[str]


def find_reader(fingerprint: str) -> Reader:
    """The one enrolled machine ``fingerprint`` names, or a useful refusal.

    Matched on the fingerprint rather than the name, because the name is the
    part anyone with push access can choose -- revoking by a string an attacker
    wrote is how you revoke the wrong machine. A unique prefix is accepted, the
    way git takes a short commit id; an ambiguous one is refused rather than
    resolved, since guessing here removes somebody's access.
    """
    wanted = fingerprint.strip()
    if not wanted:
        raise SyncError("no fingerprint given; run 'woswoar grant' to see them")

    candidates = [reader for reader in readers() if reader.fingerprint.startswith(wanted)]
    if not candidates:
        raise SyncError(f"no enrolled machine has a fingerprint starting {wanted!r}")
    if len(candidates) > 1:
        shown = "\n".join(f"  {reader.fingerprint}" for reader in candidates)
        raise SyncError(f"{wanted!r} matches {len(candidates)} machines:\n{shown}")
    return candidates[0]


def _still_readable(host_id: str) -> list[str]:
    """Days this host has both a key for and a log it may still append to."""
    keyed = {store.day_of_key(path) for path in store.iter_day_keys(host_id)}
    logged = {
        store.day_of_log(log.relpath) for log in store.iter_log_files() if log.host_id == host_id
    }
    return sorted(keyed & logged)


def revoke(reader: Reader) -> RevokeReport:
    """Withdraw one machine's access, and re-seal what is left without it.

    Three things happen, and it is worth being exact about which of them is a
    guarantee:

    1. A tombstone is appended, so every machine subtracts this key from the
       recipient list on its next fetch. Permanent, and survives ``merge=union``
       -- see `_REVOKED`.
    2. Every sealed key file is re-sealed to the remaining recipients, so a copy
       of the repo taken *after* this cannot be opened with that key.
    3. Day keys minted from now on exclude it, which is what actually stops the
       revoked machine reading tomorrow's commands.

    What this cannot undo: anything the revoked machine already read or already
    cloned, and any day whose key it already holds -- reported as
    ``still_readable`` rather than left for the caller to work out. Rotating
    those mid-day is not an option: a day key is minted once and every chunk of
    that day is sealed to it, so replacing it would strand the chunks already
    written on every machine that has not merged them yet.

    It also does not touch git access -- a revoked machine can still fetch --
    and it still holds the authentication key, so it can still *write* history
    other machines will accept. Those want the git credential rotated and, for
    the authentication key, a rebuilt repo.
    """
    if reader.is_mine:
        raise SyncError(
            "that is this machine's own key; revoking it here would lock this "
            "machine out of its own history. Run 'woswoar revoke' from another one."
        )

    with _access_change() as change:
        # Checked before the tombstone is written, so a refusal leaves the file
        # as it was rather than tombstoned-but-not-re-sealed.
        if set(recipients()) <= {reader.key}:
            raise SyncError(
                "that is the only machine left; revoking it would seal the "
                "history to nobody and lose it. Enrol another machine first."
            )

        # No date in the tombstone: the commit carries one already, and a date
        # written into a shared file is a date the machine that wrote it chose.
        _append_recipient_line(f"{_REVOKED}{reader.key}{_LABEL_SEP}revoked")

        # Read back rather than filtered by hand. What a tombstone subtracts is
        # `recipients`' rule, and a second copy of it here is a second
        # thing to keep in step -- the reason the file is written first.
        resealed, skipped = _reseal(change.identity, recipients())
        still_readable = _still_readable(change.known.id)

    return RevokeReport(resealed, skipped, pushed=change.remote, still_readable=still_readable)


def compact(before: str) -> tuple[int, int, int]:
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

    Returns (days compacted, chunks replaced, days left alone because merging
    them would exceed `MAX_EXPORT_BYTES`).
    """
    crypto.require()
    crypto.require_signing()
    known = store.machine()

    with lock():
        verify_key = signing_public()
        by_day: dict[str, list[store.Chunk]] = {}
        for chunk in store.iter_chunks(known.id):
            if chunk.day < before:
                by_day.setdefault(chunk.day, []).append(chunk)

        days = 0
        replaced = 0
        skipped = 0
        for day, chunks in sorted(by_day.items()):
            if len(chunks) < 2:
                continue
            if orphaned_day_key(known.id, day):
                # Nothing here can be opened, and one such day used to abort the
                # whole run -- so a single unreadable day left every later day
                # uncompacted, reported as a bare path. Skipped instead: `sync`
                # and `doctor` are where this state is explained, and compaction
                # has no business being the messenger.
                continue
            secret = open_day_key(known, known.id, day)

            # Checked against what this machine *signed*, and fatally so.
            # Compaction re-signs what it merges and then deletes the original,
            # so anything it accepts here it launders into something every peer
            # believes. `merge` skips this host's own id, so this and `export`
            # are the only places these chunks are ever looked at.
            #
            # Stronger than the tag this replaced: a tag proved only that
            # *someone* enrolled had written it, which a revoked machine could
            # still forge. A signature proves this machine wrote it.
            listed = read_manifest(known.id, day, verify_key)
            if {chunk.name for chunk in chunks} - set(listed):
                raise SyncError(
                    f"refusing to compact {day}: it holds chunks this machine never "
                    "published, or its manifest is not one this machine signed.\n"
                    "Compaction re-signs what it merges, so it must not be used to "
                    "launder a chunk this machine did not write."
                )
            try:
                plaintexts = [
                    unpack(
                        crypto.decrypt_with_secret(
                            open_chunk(c.path, listed[c.name].digest), secret
                        )
                    )
                    for c in chunks
                ]
            except (ValueError, zlib.error) as exc:
                # `zlib.error` as well as `ValueError`, which it is not a
                # subclass of: `open_chunk` raises the second and `unpack` the
                # first, so catching only one let a chunk that will not
                # decompress -- damaged, or past `MAX_CHUNK_BYTES` -- escape as
                # a bare zlib traceback instead of the guided refusal.
                raise SyncError(f"refusing to compact {day}: {exc}") from exc

            # Compaction is the other producer of chunks, so it is bound by the
            # same budget: joining a whole day unbounded would rebuild exactly
            # the over-cap chunk `MAX_EXPORT_BYTES` exists to prevent, and then
            # unlink the smaller ones that were fine -- leaving a day no peer
            # can read and no way to re-export it.
            #
            # Skipped rather than merged in batches. Batching was backed out
            # when a peer rebuilding a compacted day still dropped every chunk
            # it had already merged; that was #67 and is fixed, so batching is
            # safe again and would compact these days properly -- see #70. It
            # is not done here because the batch budget then bounds peak merge
            # memory, which wants measuring rather than assuming. A day this
            # large stays as the small chunks it already is, which is correct
            # if untidy.
            plain = b"".join(plaintexts)
            if len(plain) > MAX_EXPORT_BYTES:
                skipped += 1
                continue

            merged = store.new_chunk(known.id, day, int(chunks[-1].name.split("-")[0]))
            sealed = crypto.encrypt_to(pack(plain), crypto.public_of(secret))
            store.write_atomic(merged, sealed)
            for chunk in chunks:
                chunk.path.unlink()
                del listed[chunk.name]
            # What it replaced, recorded and signed. A peer that already merged
            # any of those must not merge this as if it were new history, and it
            # cannot work that out from the bytes -- the lines are the same ones.
            listed[merged.name] = Entry(
                digest_of(sealed), tuple(sorted(chunk.name for chunk in chunks))
            )
            write_manifest(known, day, listed)
            days += 1
            replaced += len(chunks)

        if days:
            _commit()

    return days, replaced, skipped


def add_recipient(recipient: str) -> bool:
    """Append a public key to recipients.txt if it is not already listed.

    Nothing but the key. woswoar used to append ``# $USER@$(uname -n)`` here so
    that `grant` had a name to show, which published a username and a hostname
    in the one file of the repo that is deliberately plaintext -- while the host
    directories beside it are opaque hex for exactly the opposite reason. The
    name comes from the sealed `name.age` now; see `name_of`.

    One line per key, guaranteed rather than assumed: the key comes from this
    machine's own files, and a newline in it would append a second entry that
    nobody added. It is refused rather than rewritten, because a key with a
    newline in it is malformed rather than merely ugly, and age would reject it
    later and less clearly.
    """
    key = recipient.strip()
    if "\n" in key:
        raise SyncError(f"the public key for this machine is not one line: {key!r}")
    if key.startswith(_REVOKED):
        # No real key starts with "-", so this is not a shape anyone reaches by
        # accident -- but one that did would be indistinguishable from a
        # tombstone, and would silently withdraw whatever key followed it.
        raise SyncError(f"a public key may not start with {_REVOKED!r}: {key!r}")
    if key in revoked_keys():
        # Loudly, and not as "already enrolled". Appending the line would be
        # harmless -- `recipients` subtracts it either way -- but this
        # machine would then record and sync for days while reading nothing, and
        # the reason would sit in a file it never prints.
        raise SyncError(
            "this key was revoked, and a revocation is permanent. Re-enrol this "
            "machine with a new identity:  woswoar init <url> --new-identity"
        )
    if key in recipients():
        return False
    _append_recipient_line(key)
    return True
