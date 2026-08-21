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
  holds -- see `manifest`, which owns that layer and the argument for it, and
  `_trusted_signer`, which decides whose signature counts.

What is left here is the orchestration: `run`, `export`, `merge` and `_Day`. They
stayed in one file because the *ordering* between them is the correctness
argument rather than an implementation detail, and each of the three orderings
that matter is one line from being broken. The list is in `docs/architecture.md`
under "The repository is append-only", and each one is also commented where it is
enforced. Three layers did come out cleanly: `manifest`, the authenticity layer,
and `gitrepo`, every git fork and the only thing here that talks to the network
-- so nothing in this module spawns a subprocess any more.
"""

from __future__ import annotations

import fcntl
import hashlib
import time
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import archive, codec, crypto, fleet, forget, gitrepo, manifest, progress, store
from .entry import make_inert
from .errors import SyncError, WoswoarError
from .report import Check, Notice
from .store import Machine

#: Both "no repo key yet" and "some days unreadable" have the same fix, and used
#: to say so in two independently worded paragraphs.
_GRANT_REMEDY = (
    "On a machine that is already enrolled run:\n"
    "    woswoar accept\n"
    "then sync here again. Nothing is lost in the meantime."
)

#: What a machine whose key was withdrawn has to do, said once. `add_recipient`
#: refuses a revoked key with the same advice, and the two were independent
#: paragraphs 3000 lines apart -- which is the condition `_GRANT_REMEDY` above
#: exists to end, reproduced inside one module.
_REENROL_REMEDY = "woswoar init <url> --new-identity"


def _restore_remedy(path: str) -> str:
    """How to recover a file a peer deleted from the history repo.

    Deleting one is a commit like any other and woswoar never rewrites history,
    so the blob is still reachable in every clone. Shared by the two files this
    can happen to, so the recipe cannot drift between their messages.
    """
    return (
        "It is still in git history -- woswoar never rewrites it, so every clone\n"
        "has the blob. To put it back:\n"
        f"    git -C <history> log --diff-filter=D -- {path}\n"
        f"    git -C <history> show <commit>^:{path} > that path\n"
    )


class Outcome:
    """One way a sync run goes other than plainly working.

    Everything about a kind in one literal: what it is called, how loudly to say
    it, and the paragraph that explains it. Those three used to live apart -- a
    field on `Report`, a `warning=True` argument, and one branch of a 143-line
    `notices` -- and the drift that costs was already visible before this: a
    field comment here named `trust` where the message on screen names `accept`.

    Declaring them together is also what makes them movable, which is the reason
    this landed before #201 rather than after. A kind now travels with the code
    that detects it: the constant, its prose and the `record` call are the whole
    of it, and neither `Report` nor `notices` names a single one of them.

    ``body`` is handed the subjects already sorted. Every message that lists them
    listed them sorted, and the ones that only count them do not care -- so the
    sort belongs here rather than being repeated, or forgotten, in each.

    A plain class rather than the `NamedTuple` this file uses elsewhere, because
    these are singletons used as dictionary keys and identity is the equality
    wanted. A tuple compares by value, so two kinds that happened to be worded
    alike would silently be one key.
    """

    def __init__(
        self, name: str, body: Callable[[list[str]], str], *, warning: bool = False
    ) -> None:
        #: For `repr` alone: a failing test prints the whole `Report`, and
        #: `<Outcome unreadable>` is the difference between reading that and not.
        self.name = name
        self.body = body
        #: Worth shouting about: the repository disagrees with what this machine
        #: expects, or history could not be published. The quieter ones are
        #: ordinary states -- a machine not granted access yet, a peer nobody has
        #: accepted -- which are not going wrong so much as not finished.
        self.warning = warning
        OUTCOMES.append(self)

    def __repr__(self) -> str:
        return f"<Outcome {self.name}>"


#: Every kind of outcome, in the order a run reports them. Appended to by
#: `Outcome` itself as each is declared, rather than written out a second time
#: underneath them: a hand-kept list is a second thing to keep in step, and a
#: kind missing from it would never be reported at all -- silently, because
#: nothing else reads this.
OUTCOMES: list[Outcome] = []


#: "<host>/<day>" entries sealed before this machine was enrolled. Not an error:
#: it is what a freshly joined machine sees until someone runs `grant` on a
#: machine that was already a recipient.
UNREADABLE = Outcome(
    "unreadable",
    lambda days: (
        f"{len(days)} day(s) of history are sealed to recipients that do not include\n"
        "this machine - it joined after they were written.\n"
        f"{_GRANT_REMEDY}"
    ),
)

#: "<host>/<day>" left exactly as it was, because rebuilding it would have needed
#: a chunk this machine could not read. Stale rather than short: the day keeps
#: every line it already had, and the next sync tries again.
STALE = Outcome(
    "stale",
    lambda days: (
        f"{len(days)} day(s) could not be rebuilt, and were left exactly as\n"
        "they were rather than rewritten from the part that could be read:\n"
        f"{', '.join(days)}\n"
        "Nothing was lost. If it persists, a chunk of that day is damaged in this\n"
        "checkout, or missing from it; woswoar never rewrites or deletes a chunk,\n"
        "so 'git -C ~/.local/share/woswoar/history log --diff-filter=MD' finds who\n"
        "did."
    ),
)

#: Hosts publishing history under a signing key nobody here has accepted. Not an
#: error and usually not an attack.
UNTRUSTED = Outcome(
    "untrusted",
    lambda hosts: (
        f"{len(hosts)} machine(s) publish history this one has not been\n"
        "told to accept, so none of it was merged. That is what a machine enrolled\n"
        "since this one last looked is supposed to look like.\n"
        "    woswoar accept"
    ),
)

#: Hosts whose pin was dropped because a tombstone withdrew them.
UNPINNED = Outcome(
    "unpinned",
    lambda hosts: (
        f"{len(hosts)} machine(s) were withdrawn by a revocation. Nothing\n"
        "they publish is accepted here any more, including history they published\n"
        "before it that this machine had not yet merged."
    ),
)

#: Hosts now publishing under a different key than the one pinned here. Never
#: resolved silently.
CHANGED_SIGNER = Outcome(
    "changed_signer",
    lambda hosts: (
        f"{len(hosts)} machine(s) now sign with a different\n"
        "key than the one accepted here, and nothing from them was merged. That is\n"
        "either a machine that was re-enrolled or someone rewriting the repository,\n"
        "and woswoar cannot tell which. If you re-enrolled it, accept the new key:\n"
        "    woswoar trust --replace"
    ),
    warning=True,
)

#: Days of this machine's own history it could not extend, because the manifest
#: already there is one it cannot verify.
UNSIGNABLE = Outcome(
    "unsignable",
    lambda days: (
        f"{len(days)} day(s) of this machine's own history\n"
        "could not be published, because the signed list already in the repository\n"
        "for them is not one this machine can verify. Publishing a replacement\n"
        "would disown everything it published earlier on those days.\n"
        "If this machine's signing key was replaced, that is why: days it signed\n"
        "with the old one stay as they are, and new days publish normally."
    ),
    warning=True,
)

#: Days of this machine's own history whose sealed day key has gone while chunks
#: sealed to its public half remain. Nothing further is written for them, because
#: a new key cannot rescue what the old one sealed.
ORPHANED = Outcome(
    "orphaned",
    lambda days: (
        f"{len(days)} day(s) of this machine's own history\n"
        f"cannot be published: {', '.join(days)}\n"
        "Their sealed key is missing from the repository while chunks encrypted to\n"
        "it are still there. Those chunks are unreadable by every machine, and a\n"
        "new key would not change that -- so nothing more is written for those\n"
        "days rather than adding chunks nobody will ever read.\n"
        "The commands themselves are still in this machine's own logs.\n"
        f"{_restore_remedy('hosts/<id>/keys/<day>.age')}"
        "'woswoar doctor' prints the host id and day. If it really is gone, delete\n"
        "keys/<day>.pub to write the day off: the old chunks stay unreadable, but\n"
        "this machine starts a new key and publishes again."
    ),
    warning=True,
)

#: Days this machine has published before whose signed manifest has gone. Nothing
#: is written for them.
MANIFEST_MISSING = Outcome(
    "manifest_missing",
    lambda days: (
        f"{len(days)} day(s) of this machine's own\n"
        f"history cannot be published: {', '.join(days)}\n"
        "The signed list this machine published for them is gone from the\n"
        "repository. Signing a replacement would name only what is written now,\n"
        "so every chunk published earlier that day would stop being one any peer\n"
        "accepts. Nothing is written for those days instead.\n"
        f"{_restore_remedy('hosts/<id>/manifests/<day>')}"
        "'woswoar doctor' prints the days."
    ),
    warning=True,
)

#: "<day>/<name>" chunks sitting under *this* machine's own id that it never
#: published. Nothing else would ever look at them -- `merge` skips our own id --
#: and they are deliberately not swept into a manifest we sign.
FOREIGN = Outcome(
    "foreign",
    lambda chunks: (
        f"{len(chunks)} chunk(s) sit under this machine's own id that it\n"
        "never signed. They are inert: no machine will read them, including this\n"
        "one, and nothing was lost -- whatever was in them has been published again\n"
        "under a chunk that is signed.\n"
        "Two things look like this and cannot be told apart from here: a run of\n"
        "this machine that was killed between writing a chunk and signing for it,\n"
        "and someone else writing into the repository. A power cut is the ordinary\n"
        "explanation. 'woswoar doctor' lists them under 'chunks'."
    ),
)

#: "<host>/<day>" entries that could not be authenticated, and so were refused
#: rather than merged.
#:
#: One category on purpose: a manifest that is missing, malformed, signed by the
#: wrong key, or signed for another day are the same answer, as is a chunk whose
#: digest it does not list. Splitting them on the shape of the bytes was tried
#: and reverted, because an attacker can produce any of those shapes at will --
#: so the split told the reassuring story for exactly the case that most needed
#: reporting.
UNAUTHENTICATED = Outcome(
    "unauthenticated",
    lambda days: (
        f"{len(days)} day(s) of history could not be\n"
        "authenticated and were refused. Every machine signs a list of the chunks\n"
        "it published, so this means the repo contains history none of your\n"
        "machines put its name to - someone else can write to it."
    ),
    warning=True,
)


@dataclass
class Report:
    """What one sync run did.

    Seventeen fields once: four counters, two flags, a set of hosts, and ten
    more sets that were each a *kind* of outcome. Those ten are now one
    dictionary keyed by `Outcome`, which is what makes adding a failure mode a
    single `record` call beside the code that detects it, rather than an edit to
    this class, to `notices` and to the tests in lockstep.

    What is left as a field is what is genuinely a field: how much was moved, and
    two states of the machine itself.
    """

    chunks_written: int = 0
    lines_exported: int = 0
    chunks_merged: int = 0
    lines_imported: int = 0
    #: This machine's history is on the remote. A state, not an event: a run
    #: that was already level with the remote and committed nothing sets it
    #: without sending anything, because there was nothing to send.
    pushed: bool = False
    #: Every other machine whose history this run looked at, whether or not it
    #: had anything new. Not an outcome: it is the denominator the summary line
    #: is stated against, and there is nothing to warn about.
    hosts_seen: set[str] = field(default_factory=set)
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
    #:
    #: A flag rather than an `Outcome` because it has no subjects to count and it
    #: silences every other notice; see `silent`.
    revoked: bool = False
    #: Everything that went other than plainly, by kind. Absent means none, which
    #: is why `record` and `subjects` exist rather than callers reaching in.
    outcomes: dict[Outcome, set[str]] = field(default_factory=dict)

    def record(self, kind: Outcome, subject: str) -> None:
        """Note that `subject` -- a day, a host, a chunk -- came out this way."""
        self.outcomes.setdefault(kind, set()).add(subject)

    def subjects(self, kind: Outcome) -> set[str]:
        """Everything recorded under `kind`, empty if nothing was.

        For reading only. The empty case is a fresh set rather than a stored
        one, so writing through it does nothing and `record` stays the single way
        in -- which is what lets a kind nothing happened under simply be absent
        from the dictionary rather than present and empty.
        """
        return self.outcomes.get(kind, set())

    @property
    def silent(self) -> bool:
        """Nothing this run did is worth summarising.

        Named because two places have to agree about it: `notices` returns the
        revocation and nothing else, and `cmd_sync` suppresses the summary above
        it. Left as two independent `if self.revoked` tests, dropping either one
        prints "exported 0 line(s)... in sync with the remote" directly above a
        paragraph saying this machine publishes nothing -- and only an
        end-to-end stdout test would catch it.
        """
        return self.revoked

    def notices(self) -> list[Notice]:
        """Everything this run has to tell a person, in the order it says it.

        Here rather than in `cmd_sync`, which is where all of it used to be: the
        condition, the count and the prose were eleven `if report.X:` blocks in
        the CLI, so "does this run warn about a changed signer" was a question
        only a test that grepped stderr could ask. Then they were eleven blocks
        here instead, which was the same method with a better address. Now the
        prose sits on the kind and this is the loop over them -- so a new kind
        does not touch this at all.

        These are the notices this class alone decides. The paragraphs
        `cmd_grant`, `cmd_revoke`, `cmd_trust` and `cmd_accept` print are
        deliberately not here and should not be moved: they are a *dialog*,
        ordered against `_confirm` so that a person reads the consequences
        before they are asked, and turning them into a list to render afterwards
        would print the reasons someone might answer no after the question.
        """
        if self.silent:
            # Not an `Outcome`: it has nothing to count, and it replaces the
            # report rather than joining it.
            return [
                Notice(
                    "This machine's access to the shared history was revoked, so nothing\n"
                    "is published from here and every other machine refuses what it already\n"
                    "published. This is not something 'woswoar grant' can undo -- a\n"
                    "revocation is permanent, deliberately.\n\n"
                    "Commands are still being recorded locally, and 'woswoar list' still\n"
                    "shows everything this machine had before. To take part again, enrol\n"
                    f"with a fresh identity:  {_REENROL_REMEDY}"
                )
            ]

        out: list[Notice] = []
        for kind in OUTCOMES:
            subjects = self.subjects(kind)
            if subjects:
                out.append(Notice(kind.body(sorted(subjects)), kind.warning))
        return out


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
    path = archive.recipients_file()
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
    path = archive.recipients_file()
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


class Owners(NamedTuple):
    """Which host claims which recipient, and where two of them disagree."""

    #: ``owning recipient -> host id``, for recipients exactly one host claims.
    by_recipient: dict[str, str]
    #: Recipients that more than one host directory claims to own. Deliberately
    #: *not* resolved: see `_host_owners`.
    contested: set[str]


def _host_owners() -> Owners:
    """Who each host says it is, and which of those claims collide.

    Built once per prompt rather than rescanned per recipient: `read_signer`
    opens a file per host, so asking "which host owns this key?" separately for
    each of them is quadratic -- 0.35 ms at five machines, 89 ms at a hundred.

    Everything `signer.pub` says is untrusted, and this used to keep the first
    claim in sorted order and drop the rest -- `setdefault` over
    `archive.repo_hosts()`. Host ids are chosen locally, so anyone who can push
    could add a directory whose id sorts first, name *your* recipient as its
    owner, and win the mapping for it (#110). `accept` would then print that
    host's verify key directly beneath your own real, checkable age
    fingerprint, which reads as one machine confirming the other.

    A recipient two hosts claim is not an ambiguity to resolve -- there is no
    evidence here that could resolve it -- so it belongs to neither. It is
    reported instead, and its keys are shown alone rather than beside each
    other. Silently picking one is what made a contradiction look like a fact.
    """
    claims: dict[str, list[str]] = {}
    for host_id in archive.repo_hosts():
        signer = read_signer(host_id)
        if signer is not None:
            claims.setdefault(signer.owner, []).append(host_id)
    return Owners(
        by_recipient={key: hosts[0] for key, hosts in claims.items() if len(hosts) == 1},
        contested={key for key, hosts in claims.items() if len(hosts) > 1},
    )


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
    return name_for(_host_owners().by_recipient.get(recipient)).text


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


def learn_names(known: Machine, host_ids: Iterable[str]) -> None:
    """Open any `name.age` this machine has not read yet.

    `name_for` reads the mirror `_merge_name` fills during a *merge*, which is
    the right trade everywhere except the two places it matters most. `grant`
    and `trust` are both asked about a machine that just enrolled, and a machine
    that just enrolled is precisely one whose history has never been merged --
    so the confirmation that widens access showed `(unnamed)`, and the name
    turned up a sync later, once nobody was looking at it.

    The sealed name is already in the tree by then: both callers fetch first,
    and it is sealed to the recipients, this machine among them. So this is a
    decrypt of something already present, once per machine ever, not a fetch.

    It cannot fail loudly: `_merge_name` swallows a name it cannot open, which
    is the correct answer for a machine that enrolled before this one and has
    not granted since. `(unnamed)` beside a fingerprint is still true, and the
    fingerprint was always the part being consented to.
    """
    for host_id in host_ids:
        _merge_name(known, host_id)


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
    #: "<host>/<day>" -> the chunk filenames of that day already merged into
    #: logs/. Pruned to the day's *signed manifest* once every name in it has
    #: been merged, so this shrinks when a peer compacts -- it is a record of
    #: what the host says the day holds, not of every file ever seen.
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
    #: Whether the grant that recorded `granted` sealed *every* key file. A
    #: partial one -- this machine could not open some of them -- leaves key
    #: files that are not sealed to everyone, so "the recipient list has not
    #: changed" is not enough on its own to skip the work.
    granted_complete: bool = False
    #: host id -> the signing key this machine accepts history from that host
    #: under. *The* trust anchor, and local for the reason the rest of this class
    #: is: everything in the repo can be rewritten by anyone who can push,
    #: `hosts/<id>/signer.pub` included, so the key a host is held to has to be
    #: remembered somewhere the remote cannot reach.
    signers: dict[str, str] = field(default_factory=dict)
    #: The host ids this machine last published in its `accepts.age`, sorted.
    #: Progress rather than history: losing it costs one extra publish. See
    #: `publish_accepts` for why the comparison lives here rather than in the
    #: file.
    #:
    #: No longer what decides whether to republish -- `published_digest` is --
    #: but still written, so a machine that downgrades reads a field in the
    #: shape it expects rather than a digest it would take for a host id.
    published_accepts: list[str] = field(default_factory=list)
    #: A digest of the id -> fingerprint mapping this machine last published.
    #:
    #: The ids alone were the comparison until #290, and they cannot see a key
    #: *rotation*: `woswoar trust --replace` leaves the host set untouched and
    #: changes what every entry points at. The seal carries the mapping, so the
    #: check has to as well, or `accepts.age` keeps a fingerprint that no longer
    #: matches and every peer's `fleet` marks this machine `!` for ever.
    #:
    #: Empty on an installation that predates the field, which republishes once
    #: on the next sync and costs one seal. That is the upgrade path, and it is
    #: the safe direction: reading a missing digest as "already published" would
    #: preserve exactly the bug.
    published_digest: str = ""
    #: "<host>/<day>" -> the day directory's mtime when every chunk its signed
    #: manifest listed had been merged. A day whose stamp still matches has gained nothing since, so
    #: it needs no listing at all -- and listing is what a peer's whole archive
    #: costs on a timer that fires every minute. See `archive.day_stamp`.
    #:
    #: Named for what it means: a day merged *completely*, not one looked at.
    #: One left short by an unopenable key or a chunk that would not
    #: authenticate has to be read again, and a stamp would say it need not be.
    merged_at: dict[str, int] = field(default_factory=dict)
    #: What was on disk when this was loaded, so `save` can tell whether there
    #: is anything to write. Not part of the state itself.
    _loaded: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def load(cls) -> State:
        raw = store.load_json(store.state_file())
        exported = raw.get("exported", {})
        merged = raw.get("merged", {})
        granted = raw.get("granted", [])
        granted_complete = raw.get("granted_complete", False)
        published = raw.get("published_accepts", [])
        digest = raw.get("published_digest", "")
        signers = raw.get("signers", {})
        merged_at = raw.get("merged_at", {})
        if not isinstance(exported, dict) or not isinstance(merged, dict):
            return cls()
        try:
            watermarks = {str(k): int(v) for k, v in exported.items()}
        except (TypeError, ValueError):
            # The same answer as a malformed *shape* on the line above, for the
            # same class of corruption. `int(v)` on a `null` raises `TypeError`,
            # and `load` runs on the way into every command -- so before #291 a
            # single bad value in a file that was otherwise valid JSON made
            # every command traceback until somebody deleted it by hand.
            #
            # A whole reset rather than dropping the one bad entry, which is the
            # tempting alternative and says less: a watermark that quietly
            # becomes 0 re-exports that log anyway, and nothing tells the reader
            # a value was discarded. Rule 8 prices this deliberately -- losing
            # `state.json` "costs a re-merge and a re-export, never a command" --
            # so the expensive-but-whole answer is the one the file is allowed
            # to cost.
            return cls()
        state = cls(
            exported=watermarks,
            # Guarded per value, not just on `merged` being a dict. A value
            # that is a bare *string* rather than a list is iterable, so without
            # this every day's record turns into its own characters, every chunk
            # looks unmerged, and the next sync duplicates every peer's history
            # wholesale. Forgetting a malformed day and reading it again is the
            # direction that repeats work rather than dropping history.
            merged={
                str(k): {str(name) for name in v} for k, v in merged.items() if isinstance(v, list)
            },
            # A malformed list costs the prompt its memory, which shows every
            # machine as new -- the safe direction to be wrong in.
            granted=[str(k) for k in granted] if isinstance(granted, list) else [],
            # Anything but a true boolean means "assume it was partial", which
            # costs one full re-seal rather than leaving a key file behind.
            granted_complete=granted_complete is True,
            # And a malformed map costs every pin, which refuses every host until
            # a human re-runs `trust` -- also the safe direction, and loud,
            # because `sync` reports the untrusted hosts rather than skipping on.
            published_accepts=sorted(str(host) for host in published)
            if isinstance(published, list)
            else [],
            # Anything that is not a string is not a digest this wrote, and the
            # empty default republishes once rather than trusting it.
            published_digest=digest if isinstance(digest, str) else "",
            signers={str(k): str(v) for k, v in signers.items()}
            if isinstance(signers, dict)
            else {},
            # A malformed stamp costs one listing of that day, which is what
            # every run did before it existed.
            merged_at={str(k): int(v) for k, v in merged_at.items() if isinstance(v, int)}
            if isinstance(merged_at, dict)
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
            "granted_complete": self.granted_complete,
            "published_accepts": list(self.published_accepts),
            "published_digest": self.published_digest,
            "signers": dict(self.signers),
            "merged_at": dict(self.merged_at),
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


def forget_rows(needle: str, only_credentials: bool, shown: set[str]) -> list[forget.Match]:
    """Remove ``shown`` from this machine's logs, and reseat what that moves.

    Here rather than in `forget` because it is one operation and half of it is
    `sync`'s: `state.exported` is a byte count into a plaintext log that only
    `export` knows how to read, and `lock` is what every other command that
    touches `State` already takes. Split across the two modules it was five
    ordered steps a second caller would have to rediscover from the CLI, and
    `forget` would have needed an edge back to this module to hold them.

    ``shown`` is the digests of the rows a person was actually shown. The search
    is run again in here because the listing was taken outside the lock -- a
    sync since then may have rewritten a peer's day file and moved every offset
    -- but only rows that were on screen are removed. Without that intersection,
    a command recorded between the listing and the ``--yes`` could be deleted
    having never been displayed, which for this command is the one thing that
    must not happen.

    The files are rewritten before the watermarks are saved, and that order is
    load-bearing rather than incidental. A crash in between leaves a watermark
    pointing past a file that is now shorter, so `export` publishes nothing for
    that day until it grows back -- one line lost, and #263 is about making that
    unreachable by checking the mark lands on a line boundary. The other order
    is worse *for this command specifically*: a watermark saved low with the
    rows still in `logs/` re-seals and re-publishes them, and the row in
    question is the one somebody just asked to be rid of.
    """
    with lock():
        state = State.load()
        matches = [
            match
            for match in forget.find(
                needle, only_credentials=only_credentials, exported=state.exported
            )
            if forget.digest(match.line) in shown
        ]
        state.exported.update(forget.apply(matches, state.exported))
        state.save()
    return matches


class Failure(NamedTuple):
    """A recorded sync failure, and when it happened."""

    when: float
    message: str


def record_failure(message: str) -> None:
    """Remember that the last unattended sync failed, for the status line.

    Deliberately not in `state.json`. That file is written only when something
    in it changed (`State.save`), which is what keeps a one-minute sync from
    rewriting a megabyte to say nothing happened -- and a timestamp changes
    every time, so putting one there would defeat exactly that.
    """
    store.save_json(store.sync_failure_file(), {"when": time.time(), "error": message})


def clear_failure() -> None:
    """Forget any recorded failure. Called when a sync works."""
    store.sync_failure_file().unlink(missing_ok=True)


def last_failure() -> Failure | None:
    """The recorded failure, if the last unattended sync left one.

    Degrades to `None` rather than raising, the same way `State.load` treats a
    value it cannot read: this is reported by the bare `woswoar`, and a status
    line that cannot be printed because the thing it reports on is broken is
    the worst of both.
    """
    payload = store.load_json(store.sync_failure_file())
    message = payload.get("error")
    if not isinstance(message, str) or not message:
        return None
    when = payload.get("when")
    return Failure(when if isinstance(when, int | float) else 0.0, message)


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


def identity_status(known: Machine) -> Check:
    """Whether this machine could actually decrypt during an unattended sync.

    Lives here rather than in `doctor` so the judgement is testable and stated
    once. The passphrase case is the reason it exists: age does not use
    ssh-agent, so such a key works perfectly by hand and fails forever from a
    timer -- a failure mode nobody notices without being told.
    """
    if not known.identity:
        return Check("identity", "no identity recorded - run 'woswoar init'", ok=False)

    path = Path(known.identity).expanduser()
    if not path.is_file():
        return Check("identity", f"identity {path} is missing - re-run 'woswoar init'", ok=False)
    if not crypto.available():
        return Check("identity", f"identity {path} (age missing, cannot verify)", ok=True)
    reason = crypto.why_unusable(path)
    if reason:
        # No hint appended here: crypto already puts the right advice in the
        # reason, and picking it by grepping this string for "passphrase" made
        # sync depend on crypto's exact wording.
        return Check("identity", f"identity {path} {reason}", ok=False)
    return Check("identity", f"identity {path}", ok=True)


def signing_status(known: Machine) -> Check:
    """Whether this machine can sign what it publishes, for `doctor` to report.

    Lives here rather than in `doctor` for the same reason `identity_status`
    does: the judgement is testable and stated once. Worth surfacing because a
    machine that cannot sign publishes history no peer will ever accept -- it
    keeps recording, looks healthy, and says so only on a timer's stderr.

    Checked by actually signing and verifying rather than by looking at the
    file -- see `manifest.signs_and_verifies`, which owns the round trip because
    it owns the namespace the signature has to be made in.
    """
    if not crypto.signing_available():
        return Check("signing", "ssh-keygen is not installed - history cannot be signed", ok=False)
    path = store.signing_key_file()
    if not path.is_file():
        return Check("signing", f"{path} is missing - it is created by 'woswoar init'", ok=False)
    try:
        verify_key = crypto.signing_public(path)
        if not manifest.signs_and_verifies(path, verify_key):
            return Check("signing", f"{path} signs, but the signature does not verify", ok=False)
    except (WoswoarError, OSError) as exc:
        return Check("signing", f"{path} cannot sign: {exc}", ok=False)
    return Check("signing", f"{path} ({crypto.fingerprint(verify_key)})", ok=True)


def trust_status(known: Machine) -> Check:
    """How many machines publishing here this one accepts, for `doctor`.

    Beside `signing_status` and `identity_status` for the reason their
    docstrings give: the judgement is testable and stated once, rather than
    derived inline in the CLI where only a test that greps stdout could reach
    it. Withdrawn hosts are subtracted, so a revoked machine never reads as
    accepted just because no sync has pruned the pin yet.
    """
    if not gitrepo.is_repo():
        return Check("trust", "no history repo yet", ok=True)
    accepted = accepted_hosts(State.load())
    others = [host for host in archive.repo_hosts() if host != known.id]
    waiting = [host for host in others if host not in accepted]
    detail = f"{len(others) - len(waiting)} of {len(others)} other machine(s) accepted"
    if waiting:
        detail += " - run 'woswoar trust' to accept the rest"
    return Check("trust", detail, ok=not waiting)


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
        archive.day_key_public(host_id, day).is_file()
        and not archive.day_key(host_id, day).is_file()
    )


def has_chunks(host_id: str, day: str) -> bool:
    """Whether anything is already sealed to this host's key for ``day``.

    What turns a half-written key pair from a nuisance into a loss: with no
    chunks the next `export` simply mints a new pair over it and nothing is
    ever noticed, so reporting that state would be a false alarm.

    Through the same listing that decides what a chunk *is*, so a day directory
    holding only a partial write or an editor's leftover reads as empty here
    too. "Anything at all is in the directory" said the opposite, which would
    have turned a stray file into a reported loss.

    One day, asked directly: `orphaned_days` calls this once per orphaned day,
    so answering by walking the whole host would be quadratic in the archive --
    186 ms rather than 3.8 ms for 40 orphaned days at 20k chunks, in exactly the
    state `doctor` exists to find.
    """
    return bool(archive.chunk_names(host_id, day))


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
    for host_id in archive.repo_hosts():
        sealed, public = archive.day_key_days(host_id)
        for day in sorted(public - sealed):
            if has_chunks(host_id, day):
                found.append((host_id, day))
    return found


def unlisted_chunks() -> list[tuple[str, str, int]]:
    """``(host, day, how many)`` for chunks no signed manifest accounts for.

    A chunk in no manifest is one somebody wrote into the repository that the
    host it sits under never vouched for. Every peer refuses it -- that is what
    the signature is for -- so nothing is at risk, and `sync` says so the first
    time it sees one. But since #87 a day settles once its *listed* chunks are
    merged, so `sync` says it once and then not again, most likely into a
    journal from the timer rather than to a person. This is where the question
    can be asked afterwards.

    Cheap in the case that matters. The manifest body is read without verifying
    it first, which needs no subprocess; only a day that appears to hold an
    unaccounted-for name pays the `ssh-keygen -Y verify` to be sure. On a
    repository where nothing is planted -- every repository, almost always --
    that is a listing and a file read per day and no forks at all.

    Skips a host this machine has not pinned. There is no key to check its
    manifests against, so every chunk would read as unlisted; `sync` reports
    that host as untrusted, which is the more useful sentence.
    """
    state = State.load()
    found: list[tuple[str, str, int]] = []
    for host_id in archive.repo_hosts():
        verify_key = state.signers.get(host_id)
        if verify_key is None:
            continue
        for day, names in archive.iter_chunk_days(host_id):
            claimed = manifest.claimed_names(host_id, day)
            if not set(names) - claimed:
                continue
            # Only now is it worth a signature check: an unverifiable manifest
            # accounts for nothing, and would make every chunk of the day look
            # planted when the manifest is what is wrong.
            listed = manifest.read(host_id, day, verify_key)
            strays = set(names) - set(listed)
            if strays and listed:
                found.append((host_id, day, len(strays)))
    return found


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
        if not manifest.exists(known.id, day)
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
    pub_path = archive.day_key_public(known.id, day)
    if pub_path.is_file() and archive.day_key(known.id, day).is_file():
        return pub_path.read_text(encoding="utf-8").strip()

    identity = crypto.generate_identity()
    sealed = crypto.encrypt_to_recipients(identity.secret.encode("utf-8"), recipients())
    # Sealed half first: see `orphaned_day_key`.
    store.write_atomic(archive.day_key(known.id, day), sealed)
    store.write_atomic(pub_path, (identity.public + "\n").encode("utf-8"))
    return identity.public


def open_day_key(known: Machine, host_id: str, day: str) -> str:
    """Recover a day's private identity so its chunks can be read."""
    sealed_path = archive.day_key(host_id, day)
    try:
        sealed = sealed_path.read_bytes()
    except OSError as exc:
        raise SyncError(f"missing day key {sealed_path}") from exc
    return open_day_key_bytes(known, sealed)


def open_day_key_bytes(known: Machine, sealed: bytes) -> str:
    """The identity inside a sealed day key, wherever the bytes came from.

    Split from `open_day_key` so that `prove`, which reads the sealed bytes
    out of a git object rather than the working tree, opens them through the
    same code -- a change to how day keys are sealed then has one reader to
    update, not two.
    """
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
        lines = archive.signer_public(host_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        return None
    return Signer(lines[0].strip(), lines[1].strip())


def accepts_digest(signers: dict[str, str]) -> str:
    """One comparable string for the id -> fingerprint mapping that gets sealed.

    A digest rather than the mapping itself, because this is written into
    `state.json` on every sync and a fleet of any size would put the whole table
    there twice -- once as the pin, once as a record of having published it.

    Tab-separated, and the separator matters. `hashlib` sees a byte sequence, so
    two different mappings must not flatten into one string: without a delimiter
    `{"ab": "c"}` and `{"a": "bc"}` are both `abc`. Host ids and age fingerprints
    contain neither tabs nor newlines, so this pair cannot be forged from the
    other side either -- which is the same argument `forget.digest` makes for
    hashing a command apart from its timestamp.
    """
    rendered = "\n".join(f"{host}\t{signers[host]}" for host in sorted(signers))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def publish_accepts(known: Machine, state: State, now: int) -> bool:
    """Publish which hosts this machine has accepted. Says whether it wrote.

    Guarded on what this machine *last published*, kept in `state`, because
    neither of the two obvious comparisons works: reading the file back means
    decrypting it, and a machine that cannot -- no identity yet, a key not
    granted to itself -- would republish on every timer tick; and the ciphertext
    cannot be compared at all, since age is randomised and two seals of one body
    never match.

    `State.signers` is the authority for the content, and that is the point:
    what a machine accepts is a local pin, so it cannot be read back out of the
    repository it syncs with. See `woswoar/fleet.py`.
    """
    if not state.signers:
        return False
    if state.published_digest == accepts_digest(state.signers):
        return False
    # Through `signing_public`, which mints the key if it is missing, exactly as
    # `publish_signer` does. Signing directly would turn "this machine lost its
    # signing key" from a thing sync recovers from into a crash.
    signing_public()
    store.write_atomic(
        archive.accepts_seal(known.id), fleet.seal(known.id, state.signers, recipients(), now)
    )
    # Recorded on the caller's `state`, which is the one `run` saves at the end.
    # Loading a second copy here and saving it was the first shape, and `run`
    # then wrote its older copy over the top -- so every sync republished,
    # committed and pushed, which three of this file's own invariants noticed.
    state.published_accepts = sorted(state.signers)
    state.published_digest = accepts_digest(state.signers)
    return True


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
        archive.signer_public(known.id),
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


def others_enrolled() -> bool:
    """Is any machine but this one enrolled here?

    Its own function because the answer decides whether a human is told about a
    step they must take on another machine, and because working out "this one"
    can fail: `_readers` wraps the same call in a `try` and says why. Failing
    soft here means erring towards *mentioning* the step, which costs a sentence
    someone does not need -- the other direction costs them the setup.
    """
    try:
        mine = _own_recipient(store.machine())
    except (WoswoarError, OSError):
        mine = ""
    return any(key != mine for key in recipients())


def _refresh() -> None:
    """Take the remote's view, under a lock the caller already holds.

    Every command that asks a human about another machine does this first, and
    for the same reason: the machine being asked about is the one that enrolled
    since this one last looked, so a pre-fetch answer lists fewer machines than
    the operation is about to act on. That is the one direction a security
    confirmation must never be wrong in.
    """
    repo = gitrepo.read_repo()
    if repo.has_remote:
        gitrepo.fetch_and_rebase(repo)


def trust_candidates() -> list[Candidate]:
    """Every other machine publishing here, with what this one accepts from it.

    Fetches first, for the same reason `readers` does: the whole point is a
    machine that appeared since this one last looked.
    """
    with lock():
        _refresh()
        return _trust_candidates()


def _trust_candidates() -> list[Candidate]:
    """The list itself, assuming the caller holds the lock and has fetched.

    Split out for `newcomers`, which needs this *and* `_readers` and must not
    pay for two fetches of the same repository to get them -- `accept` is a
    command someone runs while waiting.
    """
    known = store.machine()
    state = State.load()
    live = set(recipients())

    # Every host, before the loop: `trust` is *always* about a machine this
    # one has never merged, so without this it never had a name to show.
    learn_names(known, archive.repo_hosts())

    out: list[Candidate] = []
    for host_id in archive.repo_hosts():
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
        report.record(UNPINNED, host_id)


# ---------------------------------------------------------------------------
# Export: plaintext tail -> sealed chunk
# ---------------------------------------------------------------------------


def export(known: Machine, state: State, report: Report, now: int) -> bool:
    """Seal each log file's new lines into a fresh chunk, and sign the day's list.

    The manifest is built by **extending the one this machine last signed**,
    never by listing the day's directory, and that is a security property rather
    than an optimisation.

    `merge` skips this host's own id, so a chunk somebody else planted under it
    is the one thing nothing here ever inspects. Listing the directory would
    sweep such a chunk into a manifest *this machine signs*, laundering it into
    something every peer believes -- without even the `compact` run that
    `manifest.open_chunk` was written to guard. Extending a list we already
    signed makes a chunk we did not write unrepresentable in a manifest we sign.

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
    # Only this machine's own logs: other hosts' arrived decrypted and are never
    # re-exported. Asked for by host rather than listed and then filtered, which
    # at three machines walked two thirds of a tree to throw it away.
    for log in store.iter_log_files(known.id):
        exported = state.exported.get(log.relpath, 0)

        # One stat instead of an open, a seek, a read and a close, on a file
        # that is almost always exactly as long as it was a minute ago. Reading
        # from at or past the end returns nothing anyway, so this is the same
        # answer for a fraction of the syscalls -- which is why the comparison
        # is `<=` and not `==`: a *truncated* log also has no tail to take.
        #
        # Together with the line above, `export`'s early exit -- the shape of
        # every idle sync -- goes from 5.50 ms to 1.85 ms at three years of
        # three machines, and stops growing with how many machines there are.
        try:
            size = log.path.stat().st_size
        except OSError:
            # As `read_tail` treats one: a file that went away between being
            # listed and being looked at has no tail to export.
            continue
        if size <= exported:
            continue

        # The watermark has to name the start of a line, and until `forget`
        # existed nothing could shorten a log, so nothing had to check. See
        # `store.line_start` for why the guard is on the reading side (#263).
        exported = store.line_start(log.path, exported)
        data, new_offset = store.read_tail(log.path, exported)
        if data:
            fresh.setdefault(store.day_of_log(log.relpath), []).append(
                (log.relpath, data, new_offset)
            )

    # Whether anything reached the repo. What lets `run` skip the `git add` that
    # would otherwise stat the whole working tree to find that out.
    wrote = False

    if not fresh:
        return wrote

    # One pass for the whole host, not one per day.

    # The first sync after an import is the long one: a day-key minted, sealed
    # and signed per day, which at two years of history is where six of its six
    # and a half seconds go.
    progress.phase("sealing this machine's history")
    for done, (day, tails) in enumerate(sorted(fresh.items())):
        progress.tick(done, len(fresh), "days")
        # Before the manifest, not after. Refusing does not advance
        # `state.exported`, so a refused day comes back on every sync -- and
        # `manifest.read` forks `ssh-keygen -Y verify`. Checked afterwards, one
        # orphaned day cost a fork a minute for as long as it stayed that way,
        # on a path issue #50 is already about. `day in on_disk` first because
        # it is a dict lookup and the other two are stats.
        on_disk = set(archive.chunk_names(known.id, day))
        if on_disk and orphaned_day_key(known.id, day):
            # See `orphaned_day_key` for what this state is. A fresh key would
            # let this sync succeed and say nothing while the day's existing
            # chunks stayed unreadable, so nothing is written. The lines stay
            # pending -- `state.exported` is not advanced -- so they are still
            # in `logs/` and publish once the sealed key is restored.
            report.record(ORPHANED, day)
            continue

        # A manifest this machine wrote before and that is no longer there.
        # `manifest.read` returns nothing for "absent" and for "will not verify"
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
        has_manifest = manifest.exists(known.id, day)
        if published_before and not has_manifest:
            report.record(MANIFEST_MISSING, day)
            continue

        # What this machine has already put its name to for this day. Extended,
        # never rebuilt from the directory -- see the docstring.
        listed = manifest.read(known.id, day, verify_key)
        if not listed and has_manifest:
            # There is a manifest for this day and this machine cannot verify
            # it, so it cannot tell what it has already published. Signing a
            # fresh one would silently drop every earlier chunk of that day from
            # the signed list and every peer would then refuse them -- losing
            # published history to a file somebody else was able to corrupt.
            # Nothing is written for this day, and it is reported.
            report.record(UNSIGNABLE, day)
            continue

        for relpath, data, new_offset in tails:
            # Split rather than capped: a reader refuses a chunk over
            # `codec.MAX_CHUNK_BYTES`, and the writer used to be able to exceed that
            # with no idea it had. See `codec.MAX_EXPORT_BYTES`. A day already holds
            # many chunks, so this needs no format change and is invisible to
            # anything whose tail fits -- which is everything anyone has.
            public = day_public_key(known, day)
            for piece in codec.split_for_export(data, codec.MAX_EXPORT_BYTES):
                sealed = crypto.encrypt_to(codec.pack(piece), public)
                written = archive.new_chunk(known.id, day, now)
                store.write_atomic(written, sealed)
                listed[written.name] = manifest.ManifestEntry(manifest.digest_of(sealed))

                report.chunks_written += 1
                report.lines_exported += piece.count(b"\n")

            # The pieces are exactly this tail, so the watermark lands where one
            # chunk would have left it. Advancing per piece would buy nothing:
            # `state.save()` runs only at the end of a successful `run`, so a run
            # that dies part way through persists no watermark at all. What it
            # does leave is chunks in no manifest, which is issue #66.
            state.exported[relpath] = new_offset

        manifest.write(known, day, listed)
        wrote = True

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
        for name in on_disk - set(listed):
            report.record(FOREIGN, f"{day}/{name}")

    # Set at the manifest rather than approximated by "we got past the guards".
    # A day can be refused for an orphaned key, a missing manifest or one that
    # will not verify, and a refused day never advances `state.exported` -- so
    # its tail is fresh again next run, and every run after that. Answering
    # "probably wrote" there would mean a full stat of the working tree once a
    # minute for the life of that machine, in precisely the stuck state this
    # exists to relieve.
    #
    # The manifest is the right place to set it: every repo write in the loop
    # above -- minting a day key, sealing a chunk -- happens in the same
    # iteration, ahead of it.
    return wrote


# ---------------------------------------------------------------------------
# Import: sealed chunk -> plaintext log
# ---------------------------------------------------------------------------


def merge(known: Machine, state: State, report: Report) -> None:
    """Decrypt every chunk from other hosts that we have not merged yet."""
    # Our own plaintext is already the source of truth, so it is dropped here
    # rather than skipped in the loop: the phase names the machine being read,
    # and `_merge_host` counts that machine's days underneath it.
    others = [host for host in archive.repo_hosts() if host != known.id]
    # Read once for the whole merge rather than per host or per chunk. A chunk
    # keeps a forgotten row for ever -- history is append-only, which is the
    # point -- so the only place to stop it is here, on every sync, for as long
    # as the chunk exists. Empty on every machine that has never run `forget`,
    # and `forget.surviving` returns its argument unchanged for that case.
    suppressed = frozenset(forget.load_digests())
    for host_id in others:
        progress.phase(f"reading history from {name_for(host_id).text}")
        report.hosts_seen.add(host_id)
        _merge_host(known, host_id, state, report, suppressed)
        _merge_name(known, host_id)


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
        report.record(UNTRUSTED, host_id)
        return None

    published = read_signer(host_id)
    if published and published.verify_key != pinned:
        report.record(CHANGED_SIGNER, host_id)
        return None
    return pinned


#: How long a day directory must have been still before its mtime is trusted as
#: a statement about its contents.
#:
#: mtime is not a serial number. Where the filesystem stores it to the second --
#: ext3, ext4 formatted with 128-byte inodes, HFS+, NFSv3, and two seconds on
#: exFAT -- a chunk written into a day *after* this listing but within the same
#: tick would land under a stamp already recorded, and would never be read.
#: Inside woswoar that cannot happen: every writer of `history/` takes the same
#: lock and `merge` runs after them. But a hand-run `git pull` in the repo, or a
#: restore with `rsync -a`, is a writer woswoar does not know about.
#:
#: So a day is stamped only once it has been still for longer than any of those
#: granularities. Git's index does the same for the same reason and calls such
#: an entry racily clean. The cost is that a day being written *right now* is
#: listed again next run -- and that is a day gaining chunks anyway.
RACY_WINDOW_NS = 2_000_000_000


def _stamp(state: State, key: str, stamp: int | None, settled: bool) -> None:
    """Record that ``key`` is merged as of ``stamp``, where that can be trusted."""
    if stamp is not None and settled and time.time_ns() - stamp > RACY_WINDOW_NS:
        state.merged_at[key] = stamp


def _merge_host(
    known: Machine,
    host_id: str,
    state: State,
    report: Report,
    suppressed: frozenset[str],
) -> None:
    verify_key = _trusted_signer(host_id, state, report)
    if verify_key is None:
        return

    #: None marks a day whose key we already failed to open. Caching the failure
    #: matters as much as caching the success: without it, a machine that has
    #: not been granted access yet retries the same doomed `age -d` once per
    #: chunk rather than once per day -- tens of thousands of subprocess spawns
    #: instead of hundreds, on precisely the first sync a new machine runs.
    day_keys: dict[str, str | None] = {}

    # A day at a time rather than a chunk at a time, because whether a day is
    # being *rewritten* has to be known before deciding which of its chunks to
    # read -- and that answer is in the manifest.
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
    # Names rather than `Chunk` objects, and paths built only for the chunks
    # actually read -- see `archive.chunk_names` for what that is worth here.
    #
    # Materialised for its length alone, so the counter can say how far along a
    # first sync is. An idle sync skips every one of these on the stamp check
    # below, and never gets far enough into the wait for anything to be shown.
    days = list(archive.chunk_days(host_id))
    for done, chunk_day in enumerate(days):
        progress.tick(done, len(days), "days")
        key = f"{host_id}/{chunk_day}"

        # Read before the listing, not after: a chunk arriving between the two
        # would otherwise be covered by a stamp taken after it landed, and not
        # looked at until something else changed the directory.
        stamp = archive.day_stamp(host_id, chunk_day)
        if stamp is not None and state.merged_at.get(key) == stamp:
            continue

        names = archive.chunk_names(host_id, chunk_day)
        # `get`, not `setdefault`: a day this machine only ever *looks* at --
        # never granted, or a manifest it cannot verify -- must not gain an
        # empty record that is then written out on every sync forever.
        # A copy, not the live set: `state.merged` is updated as each chunk is
        # merged below, and aliasing it made "was this already merged" answer
        # yes for a chunk this very run had just added.
        already = frozenset(state.merged.get(key, ()))
        fresh = [name for name in names if name not in already]
        if not fresh:
            # Nothing new, but the stamp moved -- a chunk added and removed
            # again, a stray file dropped in, or a first sight of a day already
            # merged from elsewhere. Recording it here is what stops the next
            # run listing that day too, and for ever.
            #
            # Judged on the directory here, and on the manifest below the
            # merge. Deliberately, and the two do not have to agree: `not fresh`
            # means every name on disk is already merged, so the only thing a
            # manifest could add is a name with no file behind it -- which
            # nothing can merge now, and whose arrival would move the day's
            # mtime and bring it back here anyway.
            #
            # This branch has no manifest to judge against, because reading one
            # is the `ssh-keygen` fork that #87 exists to stop paying. That also
            # means it cannot prune: a day settling here keeps whatever
            # `state.merged` already held, and is pruned on the next pass that
            # has a reason to read the manifest.
            #
            # `names` because an empty directory is not a day (see
            # `archive.iter_chunk_days`), and stamping one would put a permanent
            # entry in `state.json` for something that is not there.
            _stamp(state, key, stamp, settled=bool(names))
            continue

        listed = manifest.read(host_id, chunk_day, verify_key)
        day = _Day(host_id, chunk_day, listed, already)
        pending = names if day.rewrite else fresh
        directory = archive.chunk_dir(host_id, chunk_day)
        #: Chunk names whose plaintext reached `day`.
        taken: list[str] = []

        for name in pending:
            if name not in day.listed:
                # No signed statement that this host wrote this. Covers a
                # missing, unsigned, mis-signed or mis-addressed manifest as
                # well as a chunk simply absent from a good one -- see
                # `Report.unauthenticated`.
                report.record(UNAUTHENTICATED, key)
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
                report.record(UNREADABLE, key)
                continue

            # Authenticated before it is decrypted or decompressed, so age, zlib
            # and the parser only ever see bytes one of this user's own machines
            # wrote.
            try:
                blob = manifest.open_chunk(directory / name, day.listed[name].digest)
            except (OSError, ValueError):
                report.record(UNAUTHENTICATED, key)
                continue

            try:
                plaintext = codec.unpack(crypto.decrypt_with_secret(blob, secret))
            except (crypto.AgeError, zlib.error, OSError):
                # Same judgement as an unopenable day key above: a chunk we
                # cannot consume -- damaged, written by a woswoar that packs it
                # some other way, or expanding past `codec.MAX_CHUNK_BYTES` -- must not
                # abort the sync. Aborting would block this machine's own export
                # and every other host's readable chunks, on this run and every
                # run after it.
                report.record(UNREADABLE, key)
                continue

            # Filtered before the day is assembled, not after, because a
            # rewrite replaces the file from the chunks alone: anything let
            # through is what the day *becomes*. `state.merged` stops a chunk
            # being read twice, so on an ordinary machine this only ever sees
            # new lines -- but a re-merge, a fresh clone or a compaction rebuild
            # sends the whole day back through here, which is exactly when a
            # forgotten row would otherwise return.
            plaintext = forget.surviving(plaintext, suppressed)
            if plaintext:
                day.add(plaintext, report)
            # Outside the guard on purpose: a chunk whose every line was
            # forgotten has still been read, and leaving it out of `taken` would
            # re-read it on every sync for as long as the chunk exists.
            taken.append(name)

        # A rewrite *replaces* the day's file, so it must not run on a partial
        # read: it would drop lines this machine already had in exchange for
        # the ones it could get. Measured before this guard, on a day of five
        # commands whose compacted chunk would not open: the day was rewritten
        # to one line, and stayed at one for as long as the chunk stayed
        # damaged. The lines it lost were in that chunk and nowhere else.
        #
        # Appending is safe partially, by contrast, and `add` may already have
        # flushed some of it -- which is why only the rewrite is refused.
        # Against the manifest, and against the whole of it -- not against the
        # names that happened to be on disk. A chunk the manifest lists but that
        # is *absent* never reaches the loop at all, so measuring the directory
        # counted it as nothing to miss and rewrote the day without it: five
        # commands became one, with nothing reported. A machine with push access
        # can delete a chunk; woswoar itself never does.
        #
        # A stray file is excluded for free, because it is not in the manifest:
        # it is not part of the day, and counting it would leave that day stale
        # for ever, which is #87 by another route.
        #
        # Not when the day key will not open, though. Then nothing decrypted,
        # there is nothing partial to write, and `unreadable` already names that
        # day with the remedy -- it is the ordinary state of a machine that has
        # not been granted access yet, and calling it damage would be a lie.
        refused = (
            day.rewrite
            and day_keys.get(chunk_day) is not None
            and bool(set(day.listed) - set(taken))
        )
        if refused:
            report.record(STALE, key)
        else:
            day.flush(report)
            # Recorded only once the bytes are on disk. A refused rewrite that
            # had marked its chunks merged would never read them again, and
            # would have lost exactly what it was trying not to lose.
            for name in taken:
                if name not in already:
                    report.chunks_merged += 1
                state.merged.setdefault(key, set()).add(name)

        # Judged against what the host *signed*, not against what is in the
        # directory. A name the manifest does not list is not part of the day --
        # it is debris from an interrupted write, or something a stranger
        # dropped in -- and nothing will ever merge it, so waiting for it means
        # the day is re-listed and its manifest re-verified, one `ssh-keygen`
        # fork, on every sync for ever (#87).
        #
        # An empty `listed` is not "a day holding nothing": `manifest.read`
        # answers that way for a manifest that is missing, unsigned, or will not
        # verify. Settling on it would stamp a day whose every chunk is still
        # being refused, and pruning on it would forget the whole day and merge
        # it again from scratch, duplicating its lines.
        # Never on a pass that refused to rebuild. A rewrite owed because the
        # manifest went *backwards* can leave every listed name already merged,
        # so this would otherwise read as complete -- and stamp and prune a day
        # whose file was deliberately not rebuilt, which is a day left wrong and
        # then never looked at again.
        signed = set(day.listed)
        settled = not refused and bool(signed) and signed <= state.merged.get(key, set())
        if settled:
            # Pruned to the same statement. Compaction *removes* the names it
            # subsumed from the manifest, so this is what stops `state.merged`
            # growing with every chunk that ever existed -- including while the
            # archive it describes is getting smaller (#86).
            #
            # To the manifest and never to the directory: a chunk missing from
            # this checkout for a moment would otherwise be forgotten and then
            # merged again when it came back, and `_Day` appends.
            state.merged[key] = signed
        _stamp(state, key, stamp, settled=settled)


class _Day:
    """One host-day's merged plaintext, on its way to the log file.

    Exists so the two things that decide *when* bytes are written -- the day
    boundary and `FLUSH_BYTES` -- are in one place rather than interleaved with
    decryption, and so "this day is being rewritten" is settled once, instead of
    discovered part way through.

    That last part is not tidiness. A day being rewritten has to be written in a
    single atomic replacement, so it must never be flushed early; working that
    out when the subsuming chunk turns up is too late, because chunks before it
    may already have gone to the file and the replacement would drop them.
    """

    def __init__(
        self,
        host_id: str,
        day: str,
        listed: dict[str, manifest.ManifestEntry],
        already: frozenset[str],
    ) -> None:
        self.host_id = host_id
        self.day = day
        self.listed = listed
        self.compacted = {name for name, entry in listed.items() if entry.subsumes}
        # A compacted chunk is a complete statement of the chunks it names, so a
        # day that gains one is replaced rather than added to -- the only answer
        # right whether the peer had merged all of them, some, or none.
        #
        # "One this machine has not taken in yet", not "the day has one at all".
        # A compacted chunk keeps its `subsumes` list in the signed manifest for
        # good, so the second reading stays true for the life of the day, and it
        # rebuilt the whole day every time that day gained anything: linear per
        # sync, quadratic over a day, and a day compacted while still being
        # written reaches ~1440 chunks.
        #
        # `already` can answer this because a compacted chunk only ever enters
        # `state.merged` on a pass that rewrote the file. So a rewrite cut short
        # by an unopenable day key is still owed on the next pass, with no
        # second record of what was rebuilt to keep in step with this one.
        # Or when the manifest has lost a name this machine merged. A name only
        # enters `merged` while the manifest lists it, so in ordinary growth
        # this is empty; it is not empty when the manifest went *backwards* --
        # a working tree rolled back to before a compaction, a half-applied
        # restore. Appending then would write the day's lines a second time,
        # because `merged` was pruned to the compaction and no longer remembers
        # the chunks it replaced. Rebuilding from what the manifest now lists is
        # right either way.
        #
        # An unverifiable manifest lands here too, with nothing listed -- and
        # nothing then merges, so `flush` has no blocks and leaves the day
        # alone rather than replacing it with nothing.
        self.rewrite = bool(self.compacted - already) or bool(already - set(listed))
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
#: bound -- `codec.MAX_CHUNK_BYTES` is -- just the point at which holding more stops
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
    sealed = archive.name_seal(host_id)
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
    if not gitrepo.is_repo():
        raise SyncError("no history repo yet; run 'woswoar init' first")

    known = store.machine()
    if not archive.recipients_file().is_file():
        raise SyncError("no recipients.txt in the history repo; run 'woswoar init'")

    repo = gitrepo.read_repo()
    gitrepo.ensure_config(repo)
    report = Report()
    remote = push and repo.has_remote

    with lock():
        state = State.load()

        # Take the remote's view *before* exporting. Creating a day key seals it
        # to whatever recipients.txt says right now, so exporting against a
        # stale list would produce a key that machines enrolled since the last
        # sync can never open -- silently, and permanently, because the repo is
        # append-only.
        in_sync = gitrepo.fetch_and_rebase(repo) if remote else False

        # Straight after the fetch, so what is checked is what the remote says
        # rather than what this machine last wrote, and before anything is
        # exported, committed or merged.
        require_known_repo_format()
        # An existing repository acquires its marker here rather than through
        # `init`, which nobody runs twice. This is the migration, and it costs
        # one `is_file` on every idle sync.
        marked = write_repo_format()
        # The same argument, for the same reason, one release later: a machine
        # that ran `init` before `.gitignore` existed would otherwise go on
        # staging a `.DS_Store` forever, which is the whole failure the file was
        # added to prevent. Re-running `init` is not the answer -- it also
        # TOFU-pins every host currently in the repo, which is a decision, not a
        # migration.
        marked = write_gitignore() or marked

        # Before anything is trusted, and only ever subtracting -- see
        # `apply_withdrawals`.
        apply_withdrawals(state, report)

        # Publishing needs no permission from anyone: this machine signs with a
        # key of its own and seals to a day key it mints itself. Only *reading*
        # waits for `grant`. That is a change from the shared-key design, where
        # a machine nobody had granted access to could not tag a chunk either,
        # so its own history piled up locally until someone else acted.
        report.revoked = _this_machine_revoked(known)
        published = False
        exported = False
        # One spelling of "now", shared below: two lines apart, `now if now is
        # not None else int(time.time())` and `int(time.time()) if now is None
        # else now` made a reader check twice that they meant the same thing.
        stamp = int(time.time()) if now is None else now
        if not report.revoked:
            # With `publish_signer` and `export` rather than above them: a
            # machine on its way out must not sign, seal and push a row about
            # whom it trusts, and those two are skipped here for the same
            # reason. It cannot live in `_write_repo_metadata` either -- what a
            # machine accepts changes with `accept`, long after enrolment -- and
            # the guard inside makes an idle sync write nothing.
            if publish_accepts(known, state, stamp):
                marked = True
            published = publish_signer(known)
            exported = export(known, state, report, stamp)

        # `git add -A` is a full stat of the working tree -- every chunk this
        # machine has ever published -- and this runs once a minute. On an idle
        # run there is provably nothing to stage, because the only two things
        # that write into the repo here have just said they wrote nothing.
        # Measured at 20k chunks: an idle sync drops from 20.6 ms to 10.3 ms,
        # and stops growing with the size of the history.
        #
        # A run that died between writing a chunk and committing it leaves that
        # chunk unstaged, and this does not strand it: the watermark was not
        # saved either, so the next run with anything to export writes the same
        # lines again, and `add -A` on *that* run stages the leftovers too.
        #
        # That argument is about `export`, whose write and whose watermark are
        # one all-or-nothing unit. It does not carry to `publish_signer`, whose
        # write *is* its own watermark: the file survives the crash, so the next
        # run reads it back, agrees with it, and reports writing nothing. The
        # file then waits for an unrelated export. Benign -- a machine in that
        # state publishes nothing, so peers keep seeing the old key beside the
        # old chunks it signed -- and `grant`, `revoke` and `init` still sweep
        # the tree unconditionally, which is also what catches a
        # `recipients.txt` edited by hand.
        # `marked` joins the other two for the same reason they are here: the
        # marker is a write into the working tree, so the run that makes it is
        # the run that has to stage it. Left out, it would sit unstaged until
        # some unrelated export swept it up -- which on a machine that records
        # nothing is never.
        #
        # A run that dies between writing it and committing leaves it unstaged
        # and the next run says `marked` is False, because the file is there.
        # Benign in the same way `publish_signer`'s equivalent is: the marker
        # waits for the next export, `grant`, `revoke` or `init`, all of which
        # sweep the tree unconditionally -- and every peer writes it too.
        committed = gitrepo.commit() if published or exported or marked else False

        if remote:
            # `push` contacts the remote even with nothing to send, which is the
            # slowest thing an idle run does. If the fetch above found us level
            # with the remote and nothing was committed since, there is nothing
            # to send -- and `pushed` still holds, since it means this machine's
            # history is on the remote, not that bytes moved.
            if committed or not in_sync:
                gitrepo.push(repo)
            report.pushed = True

        merge(known, state, report)
        state.save()

    return report


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
    # Belt and braces, and unobservable: `write_atomic` creates through
    # `tempfile.mkstemp`, which is 0600 before the `os.replace`, so removing this
    # changes no mode any test could read. Kept because the mode of a file woswoar
    # creates should not depend on another module's implementation of "atomic".
    dedicated.chmod(0o600)  # pragma: no mutate
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

    if remote is not None and remote.startswith("-"):
        # Refused here rather than only guarded with `--` at the git calls, so
        # the user gets a sentence instead of git's own error, and so the rule
        # is stated once where it can be tested. `--` is still passed below:
        # this is the message, that is the defence.
        raise SyncError(
            f"a remote may not start with '-': {remote!r}\n"
            "That is an option to git, not an address -- 'git clone' takes\n"
            "'--upload-pack=<command>' and runs it."
        )

    chosen = choose_identity(new_identity=new_identity, explicit=identity)
    known = store.machine()._replace(identity=str(chosen))
    store.save_machine(known)

    history = store.history_dir()
    if not gitrepo.is_repo():
        store.private_dir(history.parent)
        if remote and not any(history.glob("*")):
            # `--` before anything the user supplied. `git clone` takes
            # `--upload-pack=<cmd>` and runs it, so a "remote" beginning with a
            # dash is a command, not an address -- and argparse hands one
            # through happily, because `--` ends *woswoar's* option parsing and
            # makes the rest a positional.
            # `--no-local` because a local-path remote is an ordinary woswoar
            # setup -- a bare repo on a NAS or a shared filesystem -- and for
            # one of those `git clone` skips the transport entirely: it walks
            # the origin's `objects/` and hardlinks, or copies, what the
            # directory listing showed. That listing is not a snapshot, and the
            # object store underneath it has writers:
            #
            # - another machine pushing, whose `receive-pack` migrates its
            #   incoming objects out of a quarantine directory;
            # - git's own `gc --auto`, which the push before this one detached
            #   into the background and which is still pruning loose objects
            #   that are now packed.
            #
            # Either way an object named by the listing is gone by the time
            # clone opens it, and the clone dies -- `fatal: failed to copy file
            # to ...`, or in the hardlink spelling of the same defect (#79)
            # `fatal: hardlink different from source`. Those are one bug, not
            # two: when `link()` fails git silently falls back to copying, so
            # both spellings end in the same unguarded read.
            #
            # `--no-local` asks `upload-pack` for a pack instead -- the path
            # every clone over ssh or https already took, against servers that
            # repack constantly. It also settles #79's other half for free: a
            # received pack is this machine's own file, so a `git gc` here
            # cannot reach the origin's objects.
            #
            # git ignores the option for a remote that is not a local path, so
            # it costs an ssh clone nothing.
            gitrepo.git(
                "clone",
                "--quiet",
                "--no-local",
                "--",
                remote,
                str(history),
                cwd=history.parent,
            )
        else:
            history.mkdir(parents=True, exist_ok=True)
            gitrepo.git("init", "--quiet", "-b", "main")
    if remote and not gitrepo.has_remote():
        gitrepo.git("remote", "add", "origin", "--", remote)

    # After the remote is added, not before: that is what decides `has_remote`.
    repo = gitrepo.read_repo()
    gitrepo.ensure_config(repo)

    # Adopt the remote's state before enrolling, so we append to the real
    # recipients.txt rather than creating a competing one.
    publishing = repo.has_remote
    if publishing:
        gitrepo.fetch_and_rebase(repo)

    # Before enrolling, not after: joining a repository whose layout this
    # version cannot read would publish a key and a name into it, and nothing
    # in the repo is ever rewritten. The message says to upgrade, which is the
    # whole of the fix.
    require_known_repo_format()

    if _write_repo_metadata(known, chosen):
        gitrepo.commit()

    # Trust on first use, and only here. Every host already in the repo when
    # this machine cloned is pinned: the user named the remote, and at this
    # moment there is nothing to tell one host in it from another. From now on a
    # host that appears is refused until a human here runs `woswoar trust`.
    #
    # The pinned set is printed by `init`, because this is the sharpest edge in
    # the design and silence would make it invisible.
    state = State.load()
    pinned: list[str] = []
    for host_id in archive.repo_hosts():
        signer = read_signer(host_id)
        if signer is not None and host_id not in state.signers:
            state.signers[host_id] = signer.verify_key
            pinned.append(signer.verify_key)

    # Here, with the pins that were just made, rather than on the first sync.
    # Publishing is a write, a commit and a push, and doing it on the first sync
    # would make a machine's opening sync push even when it only received --
    # which `TestSyncDoesNotForkGitMoreThanItNeedsTo` guards, correctly: that
    # round trip fires on every machine every time any peer records a command.
    # Enrolment is already writing and pushing, so it costs nothing here.
    if publish_accepts(known, state, int(time.time())):
        gitrepo.commit()
    state.save()

    # Publish immediately. Until this machine's public key is on the remote,
    # `grant` run elsewhere cannot include it, so onboarding would appear to
    # succeed and then silently fail to grant access to any older history.
    if publishing:
        gitrepo.push(repo)

    return known, chosen, pinned


def write_repo_format() -> bool:
    """Write the layout marker if the repo has none. Says whether it wrote.

    Only when absent, and never rewritten to match: a repo that says something
    *older* is still this shape, and a repo that says something newer has
    already been refused by `require_known_repo_format` before anything reaches
    here. Rewriting it would also give two machines on different versions a file
    to take turns overwriting, which is the failure `archive.README_CONTENT`
    describes for prose and which matters more for a version.
    """
    if archive.format_file().is_file():
        return False
    store.write_atomic(archive.format_file(), archive.FORMAT_CONTENT.encode("utf-8"))
    return True


def write_gitignore() -> bool:
    """Write the repo's ignore rules if they are absent. Says whether it wrote.

    Only when absent, and deliberately *not* compared and rewritten the way
    `_write_repo_metadata` treats `.gitattributes`. This runs on every sync
    rather than only at enrolment, so two machines on different versions would
    have a file to take turns overwriting and push to each other -- the failure
    `archive.README_CONTENT` describes, and the reason `write_repo_format` is
    written this way too. Enrolment still rewrites it to match; a sync only ever
    supplies a missing one.
    """
    path = store.history_dir() / archive.GITIGNORE
    if path.is_file():
        return False
    store.write_atomic(path, archive.GITIGNORE_CONTENT.encode("utf-8"))
    return True


def repo_format_status() -> Check:
    """Whether this machine may write to the repository as it stands.

    Beside `signing_status` and `trust_status` for the reason their docstrings
    give: the judgement is stated once and is testable, rather than derived
    inline in the CLI. `doctor` renders it and `require_known_repo_format`
    refuses on it, so the rule and its wording cannot drift apart -- which they
    would the first time this version understands two formats rather than one.

    The detail names both numbers, because the fix is "upgrade this machine" and
    neither is visible anywhere else.
    """
    found = archive.repo_format()
    if found is None:
        return Check(
            "repo format", f"{archive.REPO_FORMAT} - unmarked, the next sync records it", ok=True
        )
    if found > archive.REPO_FORMAT:
        return Check(
            "repo format",
            f"repo is format {found}, this woswoar understands {archive.REPO_FORMAT}"
            " - upgrade woswoar here ('pipx upgrade woswoar'); until then nothing"
            " is published or merged",
            ok=False,
        )
    return Check("repo format", str(found), ok=True)


def require_known_repo_format() -> None:
    """Refuse a repository written by a woswoar newer than this one.

    Read after the fetch and before anything is exported or merged, so the
    answer is the *remote's* view and a refusal leaves the local history exactly
    as it was. Refusing the whole operation rather than only the merge is
    deliberate: publishing into a layout this version does not understand is how
    one machine writes what the new shape cannot account for, and the
    append-only repo makes that permanent.
    """
    status = repo_format_status()
    if not status.ok:
        raise SyncError(f"refusing to write to this history repo: {status.detail}")


def _write_repo_metadata(known: Machine, identity: Path) -> bool:
    """Ensure this machine is enrolled. Returns whether anything changed."""
    changed = False

    # Both compared and rewritten rather than written once, because both hold
    # content woswoar depends on rather than prose -- see `archive.README_CONTENT`
    # for the file where that argument comes out the other way.
    for name, content in (
        (archive.GITATTRIBUTES, archive.GITATTRIBUTES_CONTENT),
        (archive.GITIGNORE, archive.GITIGNORE_CONTENT),
    ):
        path = store.history_dir() / name
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current != content:
            store.write_atomic(path, content.encode("utf-8"))
            changed = True

    # Only when absent -- see `archive.README_CONTENT` for why it is not compared
    # and rewritten the way `.gitattributes` is.
    readme = store.history_dir() / archive.README
    if not readme.is_file():
        store.write_atomic(readme, archive.README_CONTENT.encode("utf-8"))
        changed = True

    if write_repo_format():
        changed = True

    recipient = crypto.recipient_for(identity).strip()
    if add_recipient(recipient):
        changed = True

    # Published before the first chunk is, so a peer that fetches between
    # enrolment and the first sync sees a host it can already be asked about.
    if publish_signer(known):
        changed = True

    seal = archive.name_seal(known.id)
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
        _refresh()
        return _readers()


def _readers(owners: Owners | None = None) -> list[Reader]:
    """The list itself, assuming the caller holds the lock and has fetched.

    Split from `readers` for `newcomers`, which needs this and
    `_trust_candidates` from one fetch rather than two. `owners` is passed by a
    caller that has already scanned every `signer.pub`, for the same reason.
    """
    granted = set(State.load().granted)
    enrolled = recipients()
    owners = _host_owners() if owners is None else owners

    # Not fatal if this machine's own key cannot be worked out: the list is
    # still true, it just loses the "(this machine)" note. `grant` is the
    # command someone runs *because* something is wrong with enrolment.
    try:
        mine = _own_recipient(store.machine())
    except (WoswoarError, OSError):
        mine = ""

    learn_names(store.machine(), {host for host in owners.by_recipient.values() if host})
    # A contested recipient resolves to no host, so it also gets no name: a
    # machine that could win the pairing by claiming someone else's key could
    # otherwise put a *name* on it too.
    labelled = [(key, name_for(owners.by_recipient.get(key))) for key in enrolled]
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


class Newcomer(NamedTuple):
    """One machine, and both of the things `accept` would do about it.

    `grant` and `trust` answer different questions -- who may *read* what is
    here, and whose word this machine *believes* -- and they are deliberately
    separate commands with separate keys. That distinction is real and stays in
    the interface. What it is not is something a person setting up their second
    laptop should have to hold in their head to get there: the answer to both,
    for a machine they own, is yes.

    So this pairs them per machine, and `accept` says both out loud in one
    prompt rather than collapsing them into one word. Two fingerprints are shown
    because there really are two keys; one of them being unfamiliar is exactly
    what a machine that is not yours looks like.
    """

    #: The age recipient it reads with. None for a host publishing a signer this
    #: machine cannot tie to any recipient it has -- two host directories naming
    #: one owner -- which `_host_owners` refuses to resolve, so neither gets it.
    reader: Reader | None
    #: The signing key it publishes with. None for a machine that enrolled but
    #: has never pushed a `signer.pub`.
    candidate: Candidate | None

    @property
    def label(self) -> str:
        """Both halves resolve the same name for the same host, so either does."""
        return self.candidate.label if self.candidate is not None else self._reader.label

    @property
    def _reader(self) -> Reader:
        assert self.reader is not None, "a Newcomer has a reader or a candidate"
        return self.reader

    @property
    def needs_grant(self) -> bool:
        return self.reader is not None and self.reader.is_new

    @property
    def needs_trust(self) -> bool:
        return self.candidate is not None and self.candidate.pinned is None

    @property
    def changed_key(self) -> bool:
        """Re-enrolled, or someone rewriting the repository. Never guessed at.

        Kept apart from `needs_grant` and `needs_trust` by every caller, because
        a machine can be *both* new to read and changed to sign -- which is the
        ordinary state on a machine that has just joined, where TOFU has pinned
        every peer and `granted` is still empty. Listing such a machine among
        the newcomers printed its unpinned new key under the words "already
        accepted here", while the same screen said its key had changed.
        """
        return self.candidate is not None and self.candidate.changed

    @property
    def shares_name(self) -> bool:
        """Another enrolled key carries this name too.

        Legitimate -- two machines really can both be `martin@laptop` -- and also
        exactly what a key somebody else added looks like. `grant` has said this
        out loud since it existed; `accept` widens read access on the same
        evidence, so it cannot be the quieter of the two.
        """
        return self.reader is not None and self.reader.shares_name

    @property
    def host_id(self) -> str | None:
        """The host directory it publishes under, when it has published one."""
        return self.candidate.host_id if self.candidate is not None else None

    @property
    def named(self) -> bool:
        """Whether this machine's name could actually be read here.

        A name lives in `hosts/<id>/name.age`, sealed to the recipients, so a
        machine that has not been granted yet cannot open a single one of them
        and reads every peer as `(unnamed)`. Asked through `name_for` rather
        than by comparing `label` against `UNNAMED`, for the reason `Name`
        gives: sealing a `name.age` needs no secret, so a peer can spell the
        placeholder itself.
        """
        return name_for(self.host_id).known

    @property
    def fingerprint(self) -> str:
        """The part of this machine's identity the repository cannot choose.

        Which is the whole reason it is worth printing beside an unreadable
        name: three lines reading `(unnamed)` distinguish nothing, and the
        fingerprint was always the thing being consented to.
        """
        if self.candidate is not None:
            return self.candidate.fingerprint
        return self._reader.fingerprint

    def display_name(self) -> str:
        """See `Reader.display_name`: never print `label` directly."""
        return repr(self.label)


class Pending(NamedTuple):
    """What `accept` has left to do, and the list it was decided against.

    `enrolled` is here because `grant(approved=...)` means "the list a human at
    this machine agreed to", and it refuses if its own fetch disagrees. Getting
    that list by asking again *after* the prompt makes the refusal unable to
    fire for the window it exists to cover: a machine that enrols while the
    prompt is on screen is picked up by both reads, they agree, and it is sealed
    into the whole history without ever having been shown. So the list travels
    with the machines it was shown beside, from the one fetch that produced it.
    """

    machines: list[Newcomer]
    #: Every enrolled recipient at the moment `machines` was computed -- not
    #: only the ones with something to decide. `grant` re-seals to all of them.
    enrolled: list[str]
    #: Recipients that more than one host directory claims to own. Nothing here
    #: can say which is telling the truth, so neither is paired with them and
    #: the fact is put on screen: two hosts claiming one key is not something
    #: an honest repository does.
    contested: set[str]


def newcomers() -> Pending:
    """Every machine `accept` has something to do about, from one fetch.

    Own machine excluded -- `_trust_candidates` drops it already, and a reader
    with no candidate is dropped below for the same reason. A machine that is
    fully granted and fully trusted is not returned at all: `accept` is about
    what is left to decide.
    """
    with lock():
        _refresh()
        return _newcomers()


def local_newcomers() -> Pending:
    """`newcomers` without the fetch: what the last sync already brought in.

    For the status line, which is typed for its own sake and must not reach the
    network to answer. It is also the honest answer there: a machine that has
    not arrived here yet is not something this machine has to decide about, and
    the timer fetches once a minute anyway.

    `accept` keeps the fetching version, because *that* is the moment the list
    has to be current -- see `readers`.
    """
    with lock():
        return _newcomers()


def _newcomers() -> Pending:
    """The pairing itself, assuming the caller holds the lock."""
    # One scan of every `signer.pub`, shared by both halves. Built here and
    # passed down because `_readers` would otherwise build its own: the
    # measurement in `_host_owners` is 0.35 ms at five machines and 89 ms at
    # a hundred, and `accept` was paying it three times over.
    owners = _host_owners()
    by_host = {c.host_id: c for c in _trust_candidates()}

    out: list[Newcomer] = []
    enrolled = _readers(owners)
    for reader in enrolled:
        # `pop`, so what is left in `by_host` is exactly the hosts no reader
        # claimed. `""` is never a host id, so an unpaired recipient misses.
        candidate = by_host.pop(owners.by_recipient.get(reader.key, ""), None)
        # Our own machine is not a newcomer to itself. It has no candidate
        # either -- `_trust_candidates` drops it -- so this is the same test.
        if candidate is None and reader.is_mine:
            continue
        out.append(Newcomer(reader=reader, candidate=candidate))

    # Publishing a signer, but tied to no recipient this machine can see.
    # Still offered: trusting it is a real decision, and refusing to show it
    # would hide the machine rather than the problem.
    out += [Newcomer(reader=None, candidate=c) for c in by_host.values()]

    return Pending(
        machines=[n for n in out if n.needs_grant or n.needs_trust or n.changed_key],
        enrolled=[reader.key for reader in enrolled],
        contested=owners.contested,
    )


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
    if not gitrepo.is_repo():
        raise SyncError("no history repo yet; run 'woswoar init' first")

    known = store.machine()
    identity = identity_path(known)
    repo = gitrepo.read_repo()
    gitrepo.ensure_config(repo)
    remote = repo.has_remote

    # The same lock sync takes: this rewrites files in the working tree, and a
    # timer-driven sync must not be reading them halfway through.
    with lock():
        if remote:
            gitrepo.fetch_and_rebase(repo)
        # `grant` and `revoke` rewrite every sealed key file in the repo, which
        # is a bigger write than `sync` makes -- so the version gate belongs
        # here too, and after the fetch for the same reason.
        require_known_repo_format()
        yield _AccessChange(identity, known, remote)
        gitrepo.commit()
        if remote:
            gitrepo.push(repo)


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
    for host_id in archive.repo_hosts():
        sealed.extend(archive.iter_day_keys(host_id))
        sealed.append(archive.name_seal(host_id))
        sealed.append(archive.accepts_seal(host_id))

    resealed = skipped = 0
    # Two `age` invocations per file that is ours -- open, then seal to the new
    # list -- and one file per day per machine. At two years that is where
    # `grant`'s three seconds go, all of it after the last thing it printed.
    progress.phase("re-sealing the day keys")
    for done, path in enumerate(sealed):
        progress.tick(done, len(sealed), "keys")
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

        # Nothing new, and nothing left behind last time: every key file is
        # already sealed to exactly these recipients, and re-sealing would fork
        # `age` twice per file to produce different ciphertext of identical
        # plaintext. On three hosts with a year of history that is ~1000 files
        # and ~2000 subprocesses.
        #
        # Both halves are needed. "The list has not changed" alone is not
        # enough, because a *partial* grant leaves key files this machine could
        # not open, and those are still sealed to the older, smaller list.
        #
        # Key files created since are already right: `export` seals a new day to
        # whatever `recipients.txt` said, and by this branch it has not changed.
        state = State.load()
        settled = state.granted_complete and sorted(state.granted) == sorted(keys)
        resealed, skipped = (0, 0) if settled else _reseal(change.identity, keys)

        if approved is not None:
            # Recorded rather than counted: what the next confirmation subtracts
            # is the set a human agreed to, not the set that happened to be
            # re-sealable from here. A machine that could open nothing still
            # approved these, and a grant with no approved list behind it -- an
            # unattended one -- must not silence the next prompt.
            state.granted = keys
            state.granted_complete = skipped == 0 or settled
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
    keyed = {archive.day_of_key(path) for path in archive.iter_day_keys(host_id)}
    logged = {store.day_of_log(log.relpath) for log in store.iter_log_files(host_id)}
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


def compact(before: str | None = None) -> tuple[int, int, int]:
    """Merge a host's own chunks for completed days into one chunk per day.

    Write-once chunks trade bytes for inodes: a 5-minute timer produces roughly
    40 files a day. This is the escape hatch, and it is opt-in precisely because
    it is the only routine operation that *deletes* files -- which is the
    property the rest of the design leans on. Only this host's own chunks are
    touched, so it still cannot conflict.

    Holds the same lock `run` does, and must: this is the one operation that
    unlinks chunks, and the timer fires every minute. It is also what makes
    `archive.new_chunk`'s uniqueness check sound, since that check assumes no
    other process is creating chunks for this host concurrently.

    ``before`` defaults to today, and may not be later than it: compaction is
    for days that are *finished*. A future one takes in today and every day
    after it, and those days keep gaining chunks -- so each becomes a day whose
    local copy every peer rebuilds each time it grows (#69). There is no case
    where asking for it is what was meant.

    Returns (days compacted, chunks replaced, days left alone because merging
    them would exceed `codec.MAX_EXPORT_BYTES`).
    """
    crypto.require()
    crypto.require_signing()
    # `store.day_for`, so "today" here is the bucket the shell hook records
    # into rather than a second definition of it.
    today = store.day_for(int(time.time()))
    if before is None:
        before = today
    elif before > today:
        raise SyncError(
            f"--before {before} is in the future; compaction is for days that are finished"
        )
    known = store.machine()

    with lock():
        # The only routine operation that *deletes* files, so it is the last
        # place to run against a layout this version does not understand. No
        # fetch to be after: `compact` touches this host's own chunks and never
        # goes to the remote, so the local checkout is the only view there is.
        require_known_repo_format()

        verify_key = signing_public()
        by_day: dict[str, list[archive.Chunk]] = {}
        for chunk in archive.iter_chunks(known.id):
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
            listed = manifest.read(known.id, day, verify_key)
            if {chunk.name for chunk in chunks} - set(listed):
                raise SyncError(
                    f"refusing to compact {day}: it holds chunks this machine never "
                    "published, or its manifest is not one this machine signed.\n"
                    "Compaction re-signs what it merges, so it must not be used to "
                    "launder a chunk this machine did not write."
                )
            try:
                plaintexts = [
                    codec.unpack(
                        crypto.decrypt_with_secret(
                            manifest.open_chunk(c.path, listed[c.name].digest), secret
                        )
                    )
                    for c in chunks
                ]
            except (ValueError, zlib.error) as exc:
                # `zlib.error` as well as `ValueError`, which it is not a
                # subclass of: `manifest.open_chunk` raises the second and `codec.unpack` the
                # first, so catching only one let a chunk that will not
                # decompress -- damaged, or past `codec.MAX_CHUNK_BYTES` -- escape as
                # a bare zlib traceback instead of the guided refusal.
                raise SyncError(f"refusing to compact {day}: {exc}") from exc

            # Compaction is the other producer of chunks, so it is bound by the
            # same budget: joining a whole day unbounded would rebuild exactly
            # the over-cap chunk `codec.MAX_EXPORT_BYTES` exists to prevent, and then
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
            if len(plain) > codec.MAX_EXPORT_BYTES:
                skipped += 1
                continue

            merged = archive.new_chunk(known.id, day, int(chunks[-1].name.split("-")[0]))
            sealed = crypto.encrypt_to(codec.pack(plain), crypto.public_of(secret))
            store.write_atomic(merged, sealed)
            for chunk in chunks:
                chunk.path.unlink()
                del listed[chunk.name]
            # What it replaced, recorded and signed. A peer that already merged
            # any of those must not merge this as if it were new history, and it
            # cannot work that out from the bytes -- the lines are the same ones.
            listed[merged.name] = manifest.ManifestEntry(
                # `sorted` is defensive and no test can see it: `chunks` arrives
                # from `archive.iter_chunks`, which lists via `chunk_names` --
                # already sorted. It stays because these bytes are *signed*, and
                # manifest determinism must not depend on the iteration order of
                # another module.
                manifest.digest_of(sealed),
                tuple(sorted(chunk.name for chunk in chunks)),  # pragma: no mutate
            )
            manifest.write(known, day, listed)
            days += 1
            replaced += len(chunks)

        if days:
            gitrepo.commit()

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
            f"machine with a new identity:  {_REENROL_REMEDY}"
        )
    if key in recipients():
        return False
    _append_recipient_line(key)
    return True
