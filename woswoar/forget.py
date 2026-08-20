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
`--yes`, and a row still being appended is not a row at all: `_rows` stops where
`store.read_tail` stops, so every pipeline that reads these files agrees on
where one ends.

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
the repository, and every writer into `logs/` consults it: `sync._Day` on the
merge path and `importer` through `keep` on the import one. Storing a digest
rather than the text is the point: this has to *recognise* the row, not keep a
plaintext copy of the thing somebody asked to be rid of.

The transaction itself is deliberately **not** here. Rewriting a log and moving
`state.exported` is one operation, and what `state.exported` means belongs to
`sync` -- so `sync.forget_rows` owns the lock, the load, the apply and the save,
and this module keeps the one-way edge it was given.

What is deliberately not here at all is propagating a forget to peers. woswoar
has the shape for it -- `recipients.txt` is `merge=union` with tombstones -- but
a published tombstone is a new signed record type that publishes the fact and
the time that something was forgotten, which is metadata that did not exist
before. That is its own issue if the local command turns out not to be enough.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterator
from typing import NamedTuple

from . import entry, store
from .entry import Entry
from .errors import WoswoarError

#: One digest per line, lowercase hex, in the order they were forgotten.
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

#: How a stored line becomes bytes, in the one place both spellings agree on.
#: ``surrogateescape`` rather than the ``replace`` used for display: two distinct
#: byte sequences must not collapse into one string here, or a digest could
#: suppress a row nobody named.
_CODEC = ("utf-8", "surrogateescape")


def _of(row: bytes) -> str:
    """`digest`, for a caller that already has the bytes.

    The hot path: `surviving` runs this over every line of every chunk a peer
    publishes. Hashing the bytes directly rather than decoding and re-encoding
    them saves two allocations a line, measured at 50.7 ms against 58.0 ms over
    52,000 lines. The two spellings are the same function -- the round trip is a
    bijection under ``surrogateescape`` -- and `TestTheDigest` pins that.
    """
    return hashlib.sha256(row).hexdigest()


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
    return _of(line.encode(*_CODEC))


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

    #: Path relative to ``logs/``, which is also `state.exported`'s key. The day
    #: is `store.day_of_log` of this, derived rather than carried so that a
    #: `Match` cannot hold a day and a path that disagree.
    relpath: str
    host_id: str
    #: Byte offset of the line in its file, and its length *including* the
    #: newline. Both are needed to move `state.exported` by the right amount.
    #:
    #: These stay valid between `find` and `apply` only because the logs are
    #: append-only: a command recorded in between lands past every offset here
    #: and shifts nothing. That is the load-bearing assumption of this module,
    #: and the reason `sync.forget_rows` re-finds under the lock anyway -- a
    #: merge may rewrite a *peer's* day file wholesale, which does shift them.
    offset: int
    length: int
    #: The line as stored, without its newline. What `digest` is taken over, and
    #: not reconstructible from `record`, which has been unescaped and truncated.
    line: str
    record: Entry
    #: Whether this row has left the machine. For this machine's own logs that is
    #: `state.exported`'s watermark; for another host's it is simply true -- the
    #: row is here because that host published it.
    published: bool


def _rows(raw: bytes) -> Iterator[tuple[int, bytes]]:
    """``(offset, line-with-newline)`` for each **complete** line of a log file.

    Complete, and that is the whole subtlety. `store.read_tail` deliberately
    leaves a partly written final line unconsumed -- a shell killed mid-append,
    or, far more often, the hook appending right now -- and this has to give the
    same answer. Otherwise `forget` offers to delete a record that is still
    being written, on the primary copy, and leaves its writer's remaining bytes
    to land on some other line. `read_tail`'s own docstring says the two
    pipelines reading these files must agree on where one ends; this is the
    third.

    A generator rather than a list: as a list, 52,000 lines retained 9.3 MiB,
    3.4x the file they came from, and every caller walks them once in order.

    Split on ``\\n`` alone, because the format escapes ``\\r`` -- so
    `bytes.splitlines` would invent a line break inside a recorded command.
    """
    offset = 0
    while (end := raw.find(b"\n", offset)) >= 0:
        yield offset, raw[offset : end + 1]
        offset = end + 1


def find(
    needle: str = "",
    *,
    only_credentials: bool = False,
    exported: dict[str, int],
) -> list[Match]:
    """Every recorded row the selector picks, oldest file first.

    ``needle`` is a substring of the command as it was typed -- see the module
    docstring for why it is not a regex. ``only_credentials`` runs
    `credentials.looks_like_credential` instead, which is the read-only `scan`
    #54 asked for without a second verb to learn.

    ``exported`` is `state.exported`, and is required rather than defaulted
    because there is no safe value to guess: without it every one of this
    machine's own rows reports ``local only``, which is the line telling somebody
    they do *not* need to rotate a credential.

    Matched against the command alone. The directory and the session are
    recorded rather than typed, and a person asking to forget something is
    naming what they typed; a substring that happened to appear in a path would
    take rows they never looked at.
    """
    if not needle and not only_credentials:
        # An empty substring is in every command, so this would select the whole
        # history. Refused here rather than at the CLI because `find` is what any
        # future caller reaches, and the cost of getting it wrong is the primary
        # copy. A `WoswoarError` so `main`'s own handler prints it, rather than a
        # second copy of that handler at the call site.
        raise WoswoarError(
            "forget needs something to match; an empty pattern would select everything"
        )

    # Imported here rather than at the top, and that is what keeps this module
    # cheap enough for `sync` to hold. `merge` consults `load_digests` and
    # `surviving` on a one-minute timer and never selects anything; the
    # credential rules are the CLI's alone, and 277 us of import for them on
    # every background sync is 277 us for nothing.
    from . import credentials

    extra = credentials.user_pattern() if only_credentials else None
    mine = store.machine().id
    found: list[Match] = []
    for log in store.iter_log_files():
        try:
            raw = log.path.read_bytes()
        except OSError:
            # Gone between the listing and the read. Nothing to forget in a file
            # that is not there, and refusing the whole run over it would make an
            # ordinary race fatal.
            continue
        watermark = exported.get(log.relpath, 0)
        # Another host's rows reached this machine inside a chunk that host
        # signed and published, so they are published by construction. Only this
        # machine's own logs have a not-yet-sealed tail, and `state.exported` is
        # where its length is kept.
        theirs = log.host_id != mine
        for offset, blob in _rows(raw):
            text = blob.decode(*_CODEC).rstrip("\n")
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
                    offset=offset,
                    length=len(blob),
                    line=text,
                    record=record,
                    published=theirs or offset + len(blob) <= watermark,
                )
            )
    return found


def apply(matches: list[Match], exported: dict[str, int]) -> dict[str, int]:
    """Rewrite each affected day file without ``matches``; return moved watermarks.

    Called only by `sync.forget_rows`, which holds the lock and owns saving what
    this returns. See `Match.offset` for why the offsets are still good.

    The watermark arithmetic is the part that is easy to leave out and silent
    when it is. `state.exported[relpath]` is a byte count into the plaintext log
    and `sync.export` takes the tail from there, so a file that loses bytes
    *below* the watermark while the number stays put would stop publishing
    exactly that many bytes of real history, with nothing said. Every removed
    line ending at or before the watermark therefore moves it down by its own
    length.

    The digests are written **before** the files, so a crash between the two
    leaves a machine that will not re-merge a row it still holds -- an ordinary
    duplicate-suppression state -- rather than one that has deleted a row and
    forgotten it was told to.
    """
    if not matches:
        return {}

    remember([digest(match.line) for match in matches])

    by_file: dict[str, list[Match]] = {}
    for match in matches:
        by_file.setdefault(match.relpath, []).append(match)

    moved: dict[str, int] = {}
    for relpath, taken in by_file.items():
        path = store.logs_dir() / relpath
        try:
            # The handle is held **across** the replacement, and that is what
            # makes this safe on a file the shell hook appends to. The hook takes
            # no lock -- it must not fork on the record path, and CI asserts that
            # -- so a command can land between the read and the `os.replace`, on
            # an inode this is about to unlink. Reading on afterwards recovers
            # exactly those bytes, and they are well defined because the logs are
            # append-only. Without this, `forget` is the one operation in the
            # tree that can silently drop a line from the primary copy.
            with path.open("rb") as handle:
                raw = handle.read()
                drop = {match.offset for match in taken}
                kept = b"".join(blob for offset, blob in _rows(raw) if offset not in drop)
                # Whatever follows the last newline is a line still being
                # written. `_rows` does not yield it, `find` never matched it,
                # and it has to survive the rewrite. `rfind` of -1 means no
                # complete line at all, and then all of it is that tail.
                kept += raw[raw.rfind(b"\n") + 1 :]
                store.write_atomic(path, kept)
                late = handle.read()
        except OSError:
            continue
        if late:
            with store.private_append(path, binary=True) as out:
                out.write(late)
        watermark = exported.get(relpath)
        if watermark is not None:
            below = sum(m.length for m in taken if m.offset + m.length <= watermark)
            if below:
                moved[relpath] = watermark - below

    # Belt and braces rather than the load-bearing step, and worth saying which:
    # `cache.refresh` already re-parses a file whose size went *down*, so the
    # ordinary case heals itself. What it does not catch is a file that ended in
    # a partial line, where the size can be unchanged from its point of view.
    # Dropping the whole cache costs one rebuild on the next search and needs no
    # reasoning about which of its offsets survived -- rule 8's judgement about
    # derived things, applied to the one command that shortens a log.
    with contextlib.suppress(OSError):
        store.cache_file().unlink()

    return moved


def surviving(plaintext: bytes, suppressed: frozenset[str] | set[str]) -> bytes:
    """``plaintext`` with every forgotten line removed, for `sync.merge`.

    The whole reason the digests are not in `state.json`. A chunk keeps the row
    for ever -- history is append-only and that is a security property -- so the
    only place a forgotten row can be stopped is on its way back out, every time.

    Returns the argument itself when there is nothing to drop, which covers both
    common cases: a machine that has never run `forget`, where this is one truth
    test per chunk, and one that has, whose chunks almost all hold none of what
    it forgot. Rebuilding a 4 MB block that lost nothing measured 4.4 ms and a
    full second copy of it.

    No offsets here, unlike `_rows`: nothing downstream wants them, and
    computing them cost 28 ms of this function's 100 ms per 52,000 lines.
    """
    if not suppressed:
        return plaintext
    kept: list[bytes] = []
    dropped = False
    offset = 0
    while offset < len(plaintext):
        end = plaintext.find(b"\n", offset)
        stop = len(plaintext) if end < 0 else end
        # Hashed without the newline, because that is how the line sits in the
        # log and therefore what `digest` was taken over; kept *with* it, so a
        # block that never had a final one keeps its shape.
        if _of(plaintext[offset:stop]) in suppressed:
            dropped = True
        else:
            kept.append(plaintext[offset : stop + 1])
        offset = stop + 1
    return b"".join(kept) if dropped else plaintext


def keep(entries: list[Entry]) -> list[Entry]:
    """``entries`` without the ones this machine has been told to forget.

    For `importer`, which is the *other* door into `logs/`, and the reason the
    module docstring says every writer consults the list rather than just the
    merge path. `run_atuin` deduplicates against `store.existing_keys`, which
    reads the log files -- so once `forget` has taken a row out, the next import
    no longer sees it as already present and writes it back verbatim, same six
    fields and same digest. The credential filter is the only thing in the way,
    and it is the filter whose misses `forget` exists for.

    Honest limit: this recognises a row only when the re-import reproduces the
    same six fields. An atuin re-import does, since the session and the
    timestamps come out of atuin's own database. A shell-history import that
    synthesises different values does not, and no digest over the record could.
    """
    suppressed = load_digests()
    if not suppressed:
        return entries
    return [item for item in entries if digest(entry.format_line(item)) not in suppressed]
