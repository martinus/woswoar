"""Incremental parse cache.

Parsing every log file on every Ctrl-R would be wasteful, so parsed entries are
pickled and only the *appended* tail of each file is re-read on the next run.

Entries are grouped per file rather than kept in one flat list. That costs a
``chain.from_iterable`` at read time and buys O(1) invalidation of a single
file, which is exactly what sync needs when it appends decrypted
lines to one host's log.

The cache is disposable by design: any corruption, version mismatch, or
unreadable state falls back to a full rebuild rather than raising.
"""

from __future__ import annotations

import hashlib
import pickle
from itertools import chain
from pathlib import Path
from typing import NamedTuple

from . import store
from .entry import Entry, parse_line

CACHE_VERSION = 1

#: Enough of the file head to notice a truncate-and-rewrite that happens to land
#: on the same size. Only read when size or mtime already indicate a change, so
#: the steady-state cost is zero.
_HEAD_BYTES = 256

#: How many freshly parsed entries must accumulate before the cache is rewritten.
#:
#: Writing the cache costs ~48 ms on a 52k-entry history, because pickling is
#: proportional to the whole history rather than to what changed. Saving on
#: every run would put that on the Ctrl-R path permanently: you always type a
#: command before you search, so "one new line since last time" is the normal
#: case, not an edge case -- it measured 42 ms without a save and 98 ms with one.
#:
#: Skipping the save just means the next run re-parses that tail, at roughly
#: 2 us per entry. At this threshold the re-parse costs ~4 ms, which buys back
#: far more than it spends, and the write happens every couple of thousand
#: commands instead of every single one.
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
        self.version = CACHE_VERSION
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


def load() -> Cache:
    """Read the cache from disk, or return an empty one."""
    path = store.cache_file()
    try:
        with path.open("rb") as handle:
            data = pickle.load(handle)
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ValueError):
        return Cache()

    if not isinstance(data, Cache) or data.version != CACHE_VERSION:
        return Cache()

    # Right class and right version is not the same as right *shape*. Pickle
    # restores __dict__ directly and never runs __init__, so a file written by
    # a build whose Cache had one attribute fewer unpickles cleanly here and
    # then raises AttributeError deep inside refresh() -- on the Ctrl-R path,
    # for a file this module promises is disposable. Found in the wild: a
    # cache predating `unsaved` crashed `woswoar doctor` outright.
    fresh = Cache()
    if data.__dict__.keys() != fresh.__dict__.keys():
        return fresh
    return data


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
        store.write_atomic(
            store.cache_file(), pickle.dumps(cache, protocol=pickle.HIGHEST_PROTOCOL)
        )
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
