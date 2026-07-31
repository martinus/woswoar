"""Incremental parse cache.

Parsing every log file on every Ctrl-R would be wasteful, so parsed entries are
serialised and only the *appended* tail of each file is re-read on the next run.

Entries are grouped per file rather than kept in one flat list. That costs a
``chain.from_iterable`` at read time and buys O(1) invalidation of a single
file, which is exactly what sync needs when it appends decrypted
lines to one host's log.

The cache is disposable by design: any corruption, version mismatch, or
unreadable state falls back to a full rebuild rather than raising.

**Not pickle.** This file is read on every Ctrl-R, and unpickling executes
whatever the file says before any validation can run -- an `isinstance` check
afterwards is too late. The file is 0600, so this was never a cross-user hole;
what it did was turn "can write one file under ~/.cache" -- a restored backup, a
synced dotfiles directory, a sandboxed app with home access -- into running code
as you, on the hottest path, for a project whose whole posture is a small
trusted surface. So the format is plain text that a parser reads and a `split`
cannot execute.

The separators are NUL and the two bytes after it, which no field should
contain: a command comes from `execve`, whose arguments are NUL-terminated, and
the rest are timestamps, ids and paths. Nothing is escaped on either side, which
is most of why a pure-Python parser can keep up with a C one. A file whose
values do contain a separator -- reachable only from a peer's chunk -- is left
out of the cache by :func:`dumps` and re-parsed every run instead.

It costs nothing to have done this. Measured on the 52,000-entry history in
`tests/test_perf.py`, medians of three:

                        pickle      this
    warm cache load     39.6ms    36.8ms   (budget 50ms)
    Ctrl-R end to end   86.3ms    86.9ms   (budget 200ms)
    cold build         109.2ms    74.6ms

Faster to write, and a keypress does not notice. That is not the obvious
outcome and it is not free: the parse loop in :func:`loads` is written the way
it is for exactly this reason, and a straightforward per-row list comprehension
gives about 8ms of it back.
"""

from __future__ import annotations

import hashlib
from itertools import chain
from pathlib import Path
from typing import NamedTuple

from . import store
from .entry import Entry, parse_line

#: Bump when the on-disk shape changes. Nothing reads the number back out and
#: branches on it -- there is no migration code and no need for any -- so the
#: whole first field is simply compared, and a pickle, an empty file or last
#: year's shape all fail it alike.
CACHE_VERSION = 2
_MAGIC = f"woswoar-cache-{CACHE_VERSION}"

#: Field, record and file separators. See the module docstring for why these
#: three bytes are safe to use unescaped.
_FIELD = "\x00"
_RECORD = "\x01"
_FILE = "\x02"

#: Fields in a record, and in a file's header. Used to check on the way out
#: that no value smuggled a separator in; see :func:`dumps`.
_FIELDS_PER_ENTRY = 6

#: Enough of the file head to notice a truncate-and-rewrite that happens to land
#: on the same size. Only read when size or mtime already indicate a change, so
#: the steady-state cost is zero.
_HEAD_BYTES = 256

#: How many freshly parsed entries must accumulate before the cache is rewritten.
#:
#: Writing the cache costs ~16 ms on a 52k-entry history: serialising is
#: proportional to the whole history rather than to what changed, so it is paid
#: in full for one new line. And one new line is the normal case, not an edge
#: case -- you always type a command before you search.
#:
#: Skipping the save means the next run re-parses that tail instead, at roughly
#: 2 us per entry. At this threshold the re-parse costs ~4 ms against the 16 ms
#: it defers, and the write happens every couple of thousand commands rather
#: than on every single Ctrl-R.
#:
#: Both figures were ~48 ms and ~98 ms while this file was a pickle. Changing
#: the format did not change the mechanism, but it did change the numbers, and
#: a threshold tuned against a cost that no longer exists is worth re-checking.
_SAVE_THRESHOLD = 2000


class FileMeta(NamedTuple):
    size: int
    mtime_ns: int
    #: Bytes consumed so far. Differs from ``size`` when the file ends in a
    #: partially written line -- a shell killed mid-append -- because only whole
    #: lines are ever consumed.
    offset: int
    head: bytes


class Cache:
    """Parsed entries plus the per-file state needed to update them in place.

    A plain class rather than a dataclass: it is constructed in two places and
    needs none of the generated machinery, and importing ``dataclasses`` pulls
    in ``inspect``, which costs ~4 ms of interpreter startup on a path where the
    whole budget is ~100 ms.
    """

    def __init__(self) -> None:
        #: No `version` attribute: the version lives in the file's first field,
        #: where it is checked before anything is built. Carrying it on the
        #: object as well meant a stale cache could be constructed and only
        #: then rejected, and left two things to keep in step.
        self.files: dict[str, list[Entry]] = {}
        self.meta: dict[str, FileMeta] = {}
        #: Entries parsed since the last write. Not persisted as a live count --
        #: :func:`save` zeroes it -- so it measures exactly the work that would
        #: have to be redone if we never got around to writing.
        self.unsaved = 0

    def entries(self) -> list[Entry]:
        return list(chain.from_iterable(self.files.values()))


def _fingerprint(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return hashlib.blake2b(handle.read(_HEAD_BYTES), digest_size=8).digest()
    except OSError:
        return b""


def _read_from(path: Path, host: str, offset: int) -> tuple[list[Entry], int]:
    """Parse whole lines starting at ``offset``.

    The byte-level "read the tail, stop at the last complete line" step lives in
    store, shared with sync's export -- both read these same files and must
    agree on where one ends.
    """
    data, new_offset = store.read_tail(path, offset)
    if not data:
        return [], offset

    text = data.decode("utf-8", errors="replace")
    entries = [e for e in (parse_line(line, host) for line in text.splitlines()) if e is not None]
    return entries, new_offset


def dumps(cache: Cache) -> bytes:
    """Serialise `cache`. See the module docstring for the format."""
    parts = [_MAGIC]
    for relpath, entries in cache.files.items():
        meta = cache.meta[relpath]
        host = entries[0].host if entries else ""
        rows = [
            _FIELD.join((str(e.ts), e.session, e.cwd, str(e.exit_code), str(e.duration_ms), e.cmd))
            for e in entries
        ]
        header = _FIELD.join(
            (relpath, host, str(meta.size), str(meta.mtime_ns), str(meta.offset), meta.head.hex())
        )
        chunk = _RECORD.join([header, *rows])

        # A value that contains a separator would shift every field after it.
        # Only reachable from a peer's chunk, and counted rather than scanned
        # per field: two `str.count` calls over the whole chunk cost less than
        # six membership tests per entry.
        #
        # The file is left out of the cache rather than repaired. Stripping the
        # byte was tried and rejected: it made the same command render one way
        # from a warm cache and another after a rebuild, and a derived artefact
        # must never change what you see. Omitted, the file is simply re-parsed
        # every run -- correct, self-healing, and costing one file's parse on a
        # history that should never contain one.
        expected_fields = (len(entries) + 1) * (_FIELDS_PER_ENTRY - 1)
        if (
            chunk.count(_FIELD) != expected_fields
            or chunk.count(_RECORD) != len(entries)
            or _FILE in chunk
        ):
            continue
        parts.append(chunk)
    return _FILE.join(parts).encode("utf-8")


def loads(blob: bytes) -> Cache:
    """Parse what :func:`dumps` wrote, or raise.

    Every failure mode -- wrong magic, a short record, a field that is not a
    number, invalid UTF-8 -- comes out as an exception. Nothing here can execute
    what it reads.

    The shape of the loop is load-bearing, not style. Splitting each file's
    body once and striding the flat list, then building entries with
    ``tuple.__new__`` through ``zip``, keeps the whole thing in C: it measured
    29ms against 37ms for the obvious per-row list comprehension, which is the
    difference between matching pickle and losing to it.
    """
    chunks = blob.decode("utf-8").split(_FILE)
    if chunks[0] != _MAGIC:
        raise ValueError("not a woswoar cache of this version")

    cache = Cache()
    # `session` and `cwd` repeat enormously -- a few dozen sessions and a
    # handful of directories across a whole history -- so equal values are
    # shared rather than allocated per entry. Measured on 52,000 entries: no
    # difference in time, and 30% less memory retained. One table for the whole
    # file, because the same values recur across days.
    shared: dict[str, str] = {}
    share = shared.setdefault
    for chunk in chunks[1:]:
        header, _, body = chunk.partition(_RECORD)
        relpath, host, size, mtime_ns, offset, head = header.split(_FIELD)
        cache.meta[relpath] = FileMeta(
            size=int(size), mtime_ns=int(mtime_ns), offset=int(offset), head=bytes.fromhex(head)
        )
        if not body:
            cache.files[relpath] = []
            continue

        flat = body.replace(_RECORD, _FIELD).split(_FIELD)
        count, remainder = divmod(len(flat), _FIELDS_PER_ENTRY)
        if remainder:
            raise ValueError(f"{relpath}: {len(flat)} fields is not a whole number of entries")
        cache.files[relpath] = list(
            map(
                tuple.__new__,
                (Entry,) * count,
                zip(
                    map(int, flat[0::6]),
                    (host,) * count,
                    map(share, flat[1::6], flat[1::6]),
                    map(share, flat[2::6], flat[2::6]),
                    map(int, flat[3::6]),
                    map(int, flat[4::6]),
                    flat[5::6],
                    strict=True,
                ),
            )
        )
    return cache


def load() -> Cache:
    """Read the cache from disk, or return an empty one."""
    try:
        return loads(store.cache_file().read_bytes())
    # UnicodeDecodeError is a ValueError, so it needs no entry of its own.
    except (OSError, ValueError, IndexError):
        return Cache()


def refresh(cache: Cache) -> bool:
    """Bring ``cache`` up to date with the logs. Returns whether it changed."""
    changed = False
    seen: set[str] = set()

    for log in store.iter_log_files():
        seen.add(log.relpath)
        try:
            stat = log.path.stat()
        except OSError:
            continue

        known = cache.meta.get(log.relpath)
        if known and known.size == stat.st_size and known.mtime_ns == stat.st_mtime_ns:
            continue

        head = _fingerprint(log.path)
        rewritten = known is not None and (stat.st_size < known.offset or head != known.head)
        offset = 0 if (known is None or rewritten) else known.offset

        entries, new_offset = _read_from(log.path, log.host_id, offset)
        if offset == 0:
            # Parsed from the start, so this replaces whatever was held before --
            # which is also how a rewritten file gets its stale entries dropped.
            cache.files[log.relpath] = entries
        else:
            cache.files.setdefault(log.relpath, []).extend(entries)

        cache.meta[log.relpath] = FileMeta(
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            offset=new_offset,
            head=head,
        )
        cache.unsaved += len(entries)
        changed = True

    for gone in set(cache.meta) - seen:
        cache.files.pop(gone, None)
        cache.meta.pop(gone, None)
        changed = True

    return changed


def save(cache: Cache) -> None:
    """Persist the cache. Failures are non-fatal -- it is a disposable artefact."""
    cache.unsaved = 0
    try:
        store.write_atomic(store.cache_file(), dumps(cache))
    except OSError:
        return


def load_entries() -> list[Entry]:
    """The one call search needs: load, incrementally update, persist, flatten."""
    cache = load()
    refresh(cache)
    # Always write the first build -- that is what makes every later run cheap.
    # After that, defer until enough has accumulated to be worth the ~48 ms.
    if cache.unsaved and (cache.unsaved >= _SAVE_THRESHOLD or not store.cache_file().exists()):
        save(cache)
    return cache.entries()
