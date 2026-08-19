"""Removing a recorded command from this machine, and keeping it removed.

`docs/security.md` already said two true things: that no pattern catches every
credential, and that "Revoking cannot take back what was already published."
Neither was the gap. The gap was that the **local** half had no answer either: a
secret typed once stayed in `logs/`, was offered by every Ctrl-R, and counted in
`stats`, with nothing to run and nothing documented except rebuilding the
repository. That half is entirely within this machine's power, and this module
is it -- the document now says so as "Forgetting is local; publishing is not."

Three things decide the shape.

**`logs/` is the primary copy.** Rule 8 calls it that: `history/` is derived and
rebuildable, `logs/` is not. So the selector is a plain substring rather than a
regex -- a mistyped `.*` deletes history no copy anywhere can return -- the
matches are printed in full before anything is written, nothing happens without
`--yes`, and the rewrite goes through `store.write_atomic` so a crash leaves the
day as it was rather than half of it.

**Chunks are never rewritten.** `docs/security.md` and
`tests/test_sync.py::TestImmutability` both rest on history being append-only,
and the signatures rest on it too. A `forget` that edited a published chunk
would break the invariant and every peer would refuse the result anyway. So what
is published stays published, and the command's job there is to *say so*, by
name and day, rather than to fall silent -- the person needs to rotate the
credential, and that is the only honest answer available.

**A local delete undoes itself without a digest.** `state.merged` is what stops
a merged chunk being read again, and `state.exported` is what stops this
machine's own lines being sealed twice -- but `state.json` is progress, not
history, and rule 8 says losing it costs a re-merge. A re-merge, or a fresh
clone, walks the chunk store and writes every forgotten row straight back into
`logs/`. So the digests live in their own file, outside `state.json` and outside
the repository, and `merge` drops any line whose digest is in it. Storing a
digest rather than the text is the point: this has to *recognise* the row, not
keep a plaintext copy of the thing somebody asked to be rid of.

What is deliberately not here is propagating a forget to peers. woswoar has the
shape for it -- `recipients.txt` is `merge=union` with tombstones -- but a
published tombstone is a new signed record type that publishes the fact and the
time that something was forgotten, which is metadata that did not exist before.
That is its own issue if the local command turns out not to be enough.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from typing import NamedTuple

from . import entry, store
from .entry import Entry

#: One digest per line, lowercase hex, in the order they were forgotten.
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def digest(line: str) -> str:
    """The name a forgotten row is remembered by.

    The **whole stored line**, not the command: six fields, exactly as they sit
    in the log and exactly as they arrive again inside a chunk. Two runs of the
    same command differ in their timestamp, so a digest over the command alone
    would quietly suppress a future row nobody asked to forget -- which is the
    same class of mistake as a regex selector, arriving later and without a
    prompt.

    sha256 rather than something shorter: this is compared against every line of
    every chunk a peer publishes, so a collision is a line that vanishes from
    somebody's history for no reason they could ever find.
    """
    return hashlib.sha256(line.encode("utf-8", "surrogateescape")).hexdigest()


def load_digests() -> set[str]:
    """Every digest this machine has been told to forget.

    A malformed line is skipped rather than fatal, and that direction is
    deliberate: the failure of skipping is that one forgotten row could come
    back on a re-merge, which is visible and can be forgotten again. The failure
    of raising is that `sync` stops working on every machine that ever ran this
    command, which is not.
    """
    try:
        text = store.forgotten_file().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if _DIGEST.match(line.strip())}


def remember(digests: list[str]) -> None:
    """Add ``digests`` to the file, keeping what is already there.

    Appended rather than rewritten, and read back first so a repeated `forget`
    does not grow the file with names it already holds. Order is the order they
    were forgotten in, which is the only order this file has any claim to.
    """
    known = load_digests()
    fresh = [name for name in dict.fromkeys(digests) if name not in known]
    if not fresh:
        return
    with store.private_append(store.forgotten_file()) as out:
        for name in fresh:
            out.write(f"{name}\n")


class Match(NamedTuple):
    """One recorded row that the selector picked out."""

    #: Path relative to ``logs/``, which is also `state.exported`'s key.
    relpath: str
    host_id: str
    day: str
    #: Byte offset of the line in its file, and its length *including* the
    #: newline. Both are needed to move `state.exported` by the right amount.
    offset: int
    length: int
    #: The line as stored, without its newline. What `digest` is taken over.
    line: str
    record: Entry
    #: Whether this row has left the machine. For this machine's own logs that
    #: is `state.exported`'s watermark; for another host's it is simply true --
    #: the row is here because that host published it.
    published: bool


def _lines(raw: bytes) -> list[tuple[int, bytes]]:
    """``(offset, line-with-newline)`` for each line of a log file.

    `bytes.splitlines` on `\\r` as well as `\\n` would be wrong for a format
    that escapes both, so the split is on `\\n` alone and the newline is put
    back -- a final line without one (a shell killed mid-append) keeps its exact
    bytes, which is what makes the rewrite below byte-preserving for everything
    it does not remove.
    """
    found: list[tuple[int, bytes]] = []
    offset = 0
    for piece in raw.split(b"\n"):
        if not piece and offset == len(raw):
            break  # the empty piece after a trailing newline
        line = piece + b"\n" if offset + len(piece) < len(raw) else piece
        found.append((offset, line))
        offset += len(line)
    return found


def find(
    needle: str = "",
    *,
    only_credentials: bool = False,
    exported: dict[str, int] | None = None,
) -> list[Match]:
    """Every recorded row the selector picks, oldest file first.

    ``needle`` is a substring of the command as it was typed -- see the module
    docstring for why it is not a regex. ``only_credentials`` runs
    `credentials.looks_like_credential` instead, which is the read-only `scan`
    #54 asked for without a second verb to learn.

    Matched against the command alone. The directory and the session are
    recorded rather than typed, and a person asking to forget something is
    naming what they typed; a substring that happened to appear in a path would
    take rows they never looked at.
    """
    if not needle and not only_credentials:
        # An empty substring is in every command, so this would select the whole
        # history. Refused here rather than at the CLI because `find` is what
        # any future caller reaches, and the cost of getting it wrong is the
        # primary copy.
        raise ValueError("forget needs something to match; an empty pattern selects everything")

    # Imported here rather than at the top, and that is what keeps this module
    # cheap enough for `sync` to hold. `merge` consults `load_digests` and
    # `surviving` on a one-minute timer and never selects anything; the
    # credential rules are the CLI's alone, and 277 us of import for them on
    # every background sync is 277 us for nothing.
    from . import credentials

    watermarks = exported or {}
    extra = credentials.user_pattern() if only_credentials else None
    mine = store.machine().id
    found: list[Match] = []
    for log in store.iter_log_files():
        try:
            raw = log.path.read_bytes()
        except OSError:
            # Gone between the listing and the read. Nothing to forget in a file
            # that is not there, and refusing the whole run over it would make
            # an ordinary race fatal.
            continue
        day = store.day_of_log(log.relpath)
        watermark = watermarks.get(log.relpath, 0)
        # Another host's rows reached this machine inside a chunk that host
        # signed and published, so they are published by construction. Only this
        # machine's own logs have a not-yet-sealed tail, and `state.exported` is
        # where its length is kept.
        theirs = log.host_id != mine
        for offset, blob in _lines(raw):
            text = blob.decode("utf-8", "surrogateescape").rstrip("\n")
            record = entry.parse_line(text, log.host_id)
            if record is None:
                continue
            if not (
                credentials.looks_like_credential(record.cmd, extra)
                if only_credentials
                else needle in record.cmd
            ):
                continue
            found.append(
                Match(
                    relpath=log.relpath,
                    host_id=log.host_id,
                    day=day,
                    offset=offset,
                    length=len(blob),
                    line=text,
                    record=record,
                    published=theirs or offset + len(blob) <= watermark,
                )
            )
    return found


class Removal(NamedTuple):
    """What `apply` did, for the caller to print."""

    rows: int
    files: int
    #: New `state.exported` values for the files whose sealed prefix shrank.
    exported: dict[str, int]


def apply(matches: list[Match], exported: dict[str, int] | None = None) -> Removal:
    """Rewrite each affected day file without ``matches``, and remember them.

    The watermark arithmetic is the part that is easy to leave out and silent
    when it is. `state.exported[relpath]` is a byte count into the plaintext
    log, and `sync.export` takes the tail from there -- so a file that loses
    bytes *below* the watermark and keeps the old number would stop publishing
    exactly as many bytes of real history as were removed, with nothing said.
    Every removed line that ended at or before the watermark therefore moves it
    down by its own length.

    The digests are written **before** the files, so a crash between the two
    leaves a machine that will not re-merge a row it still holds -- an ordinary
    duplicate-suppression state -- rather than one that has deleted a row and
    forgotten it was told to.
    """
    if not matches:
        return Removal(0, 0, {})

    watermarks = dict(exported or {})
    remember([digest(match.line) for match in matches])

    by_file: dict[str, list[Match]] = {}
    for match in matches:
        by_file.setdefault(match.relpath, []).append(match)

    moved: dict[str, int] = {}
    for relpath, taken in by_file.items():
        path = store.logs_dir() / relpath
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        drop = {match.offset for match in taken}
        kept = b"".join(blob for offset, blob in _lines(raw) if offset not in drop)
        store.write_atomic(path, kept)
        watermark = watermarks.get(relpath)
        if watermark is not None:
            below = sum(m.length for m in taken if m.offset + m.length <= watermark)
            if below:
                moved[relpath] = watermark - below

    # Derived from `logs/`, keyed by (path, offset), and both just moved. Rule 8
    # calls a cache the cheap thing to throw away, and this is cheaper still
    # than reasoning about which of its offsets survived.
    with contextlib.suppress(OSError):
        store.cache_file().unlink()

    return Removal(len(matches), len(by_file), moved)


def surviving(plaintext: bytes, suppressed: frozenset[str] | set[str]) -> bytes:
    """``plaintext`` with every forgotten line removed, for `sync.merge`.

    The whole reason the digests are not in `state.json`. A chunk keeps the row
    for ever -- history is append-only and that is a security property -- so the
    only place a forgotten row can be stopped is on its way back out, every time.

    Returns the argument itself when nothing is forgotten, which is the case on
    every machine that has never run `forget`: one set lookup guards a path that
    otherwise splits and rejoins every byte of every peer's history on every
    sync.
    """
    if not suppressed:
        return plaintext
    kept = [
        blob
        for _, blob in _lines(plaintext)
        if digest(blob.decode("utf-8", "surrogateescape").rstrip("\n")) not in suppressed
    ]
    return b"".join(kept)
