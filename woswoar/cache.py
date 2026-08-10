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
    #: Which machine's log this is. One per *file*, which is what it always was
    #: -- the header has carried it since the format existed, and `loads` used
    #: to copy it onto all 54,000 entries so that `entries()` could hand it back
    #: unchanged.
    host: str = ""


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
        #: relpath -> the entry fields of that file, flat, `_FIELDS_PER_ENTRY`
        #: to a row and in the order `dumps` writes them. Not `Entry` objects:
        #: building 54,804 seven-field namedtuples cost 25 ms of the 39 ms it
        #: took to read a real history's cache, and Ctrl-R displays two of the
        #: seven fields. `entries()` still builds them for whoever wants them;
        #: `stamps_and_commands` gets the picker what it needs from two slices.
        self.files: dict[str, list[str]] = {}
        self.meta: dict[str, FileMeta] = {}
        #: Entries parsed since the last write. Not persisted as a live count --
        #: :func:`save` zeroes it -- so it measures exactly the work that would
        #: have to be redone if we never got around to writing.
        self.unsaved = 0

    def entries(self) -> list[Entry]:
        """Every entry as an `Entry`. Pays the full construction cost.

        `session` and `cwd` repeat enormously -- a few dozen sessions and a
        handful of directories across a whole history -- so equal values are
        shared rather than allocated per entry. Measured on 52,000 entries: no
        difference in time, and 30% less memory retained. One table for the
        whole cache, because the same values recur across days.
        """
        shared: dict[str, str] = {}
        share = shared.setdefault
        out: list[Entry] = []
        for relpath, flat in self.files.items():
            if not flat:
                continue
            host = self.meta[relpath].host
            count = len(flat) // _FIELDS_PER_ENTRY
            out.extend(
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
        return out

    def stamps_and_commands(self, hosts: set[str] | None = None) -> tuple[list[str], list[str]]:
        """The two columns the picker shows, without building anything else.

        Returned as parallel lists of *strings*: the timestamp is converted once,
        by the caller that sorts on it, rather than for every entry of a history
        that is mostly not going to be looked at.

        ``hosts`` narrows to those machines, which is free -- the host is a
        property of the file, so it is one dict lookup per file rather than a
        test per entry.
        """
        stamps: list[str] = []
        commands: list[str] = []
        for relpath, flat in self.files.items():
            if hosts is not None and self.meta[relpath].host not in hosts:
                continue
            stamps += flat[0::6]
            commands += flat[5::6]
        return stamps, commands

    def display_columns(
        self, hosts: set[str] | None = None
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """Timestamps, commands, exit codes and host ids, aligned, as strings.

        One pass and one place. Each caller used to slice what it happened to
        need, so adding a column meant adding it to every scope separately --
        and the `host` scope grew a hand-rolled comprehension that repeated what
        `stamps_and_commands` already knew.

        ``hosts`` narrows to those machines, which costs one dict lookup per
        *file*: the host belongs to the file, not the row.
        """
        stamps: list[str] = []
        commands: list[str] = []
        codes: list[str] = []
        owners: list[str] = []
        for relpath, flat in self.files.items():
            host = self.meta[relpath].host
            if hosts is not None and host not in hosts:
                continue
            stamps += flat[0::6]
            commands += flat[5::6]
            codes += flat[3::6]
            owners += [host] * (len(flat) // _FIELDS_PER_ENTRY)
        return stamps, commands, codes, owners

    def exit_codes(self) -> list[str]:
        """The exit status column, in the same order as `stamps_and_commands`."""
        return [value for flat in self.files.values() for value in flat[3::6]]

    def sessions(self) -> list[str]:
        """The session column, in the same order as `stamps_and_commands`."""
        return [value for flat in self.files.values() for value in flat[1::6]]

    def cwds(self) -> list[str]:
        """The working-directory column, in the same order as `stamps_and_commands`.

        Its own accessor rather than a fifth column on `display_columns`, which
        every scope would then pay for to serve one of them -- 0.4 ms measured
        over 54,600 rows -- and `cwd` is not a display column. It is comparable
        exactly as it comes out, which is the other half of why this is cheap:
        the cache stores what `parse_line(..., inert=True)` produced, so it is
        already unescaped and already inert.
        """
        return [value for flat in self.files.values() for value in flat[2::6]]


def _fingerprint(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return hashlib.blake2b(handle.read(_HEAD_BYTES), digest_size=8).digest()
    except OSError:
        return b""


def fields_of(entry: Entry) -> tuple[str, ...]:
    """One entry as the field strings the cache stores, in `dumps`'s order.

    `host` is not among them: it is one value per *file* and lives in the
    header, which is why the cache no longer carries a copy of it on every one
    of a hundred thousand rows.
    """
    return (
        str(entry.ts),
        entry.session,
        entry.cwd,
        str(entry.exit_code),
        str(entry.duration_ms),
        entry.cmd,
    )


def _read_from(path: Path, host: str, offset: int) -> tuple[list[str], int]:
    """Parse whole lines starting at ``offset``, as flat entry fields.

    The byte-level "read the tail, stop at the last complete line" step lives in
    store, shared with sync's export -- both read these same files and must
    agree on where one ends.
    """
    data, new_offset = store.read_tail(path, offset)
    if not data:
        return [], offset

    text = data.decode("utf-8", errors="replace")
    # Made inert here, which is the one door every consumer of a peer's history
    # comes through: `search`, `stats` and `doctor` all read entries from this
    # cache, and nothing writes back out of it -- sync exports raw log bytes and
    # the importer dedups against `store.existing_keys`, both of which read the
    # log directly and keep seeing it verbatim.
    #
    # A command arrives from another machine's chunk, so `\x1b[2K\x1b[1A` in one
    # would otherwise reach a terminal and erase the line above it. Doing it per
    # display site was tried and is the thing this replaces: it is a rule someone
    # has to remember, and it had already been forgotten once (`import`'s
    # per-machine listing) before this was written. `cwd` is not printed today,
    # and gets the same treatment so that it need not be remembered either.
    #
    # Only newly-read lines pay for it -- the cache holds the inert form -- so
    # the steady-state cost on the Ctrl-R path is zero.
    flat: list[str] = []
    for line in text.splitlines():
        entry = parse_line(line, host, inert=True)
        if entry is not None:
            # Flattened straight away: the cache holds columns, and only
            # newly-read lines come through here, so this is off the Ctrl-R
            # path in the steady state.
            flat += fields_of(entry)
    return flat, new_offset


def dumps(cache: Cache) -> bytes:
    """Serialise `cache`. See the module docstring for the format."""
    parts = [_MAGIC]
    for relpath, flat in cache.files.items():
        meta = cache.meta[relpath]
        count = len(flat) // _FIELDS_PER_ENTRY
        rows = [
            _FIELD.join(flat[i : i + _FIELDS_PER_ENTRY])
            for i in range(0, len(flat), _FIELDS_PER_ENTRY)
        ]
        header = _FIELD.join(
            (
                relpath,
                meta.host,
                str(meta.size),
                str(meta.mtime_ns),
                str(meta.offset),
                meta.head.hex(),
            )
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
        expected_fields = (count + 1) * (_FIELDS_PER_ENTRY - 1)
        if (
            chunk.count(_FIELD) != expected_fields
            or chunk.count(_RECORD) != count
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

    What it does *not* do is build `Entry` objects. Each file's body becomes one
    flat list of field strings, which is a single `str.split` in C. Measured on
    a real 54,804-command history: 39 ms to build the namedtuples here, 14 ms to
    stop at the columns. `Cache.entries` still builds them for callers that want
    them, and `Cache.stamps_and_commands` skips them for the caller that does
    not -- which is Ctrl-R, the reason this file exists.

    The numeric fields are left as strings too. They are validated on the way
    *out*, by whichever accessor converts them; a field that is not a number
    therefore surfaces as a rebuild rather than a traceback, which is what every
    other kind of damage to this file already does.
    """
    chunks = blob.decode("utf-8").split(_FILE)
    if chunks[0] != _MAGIC:
        raise ValueError("not a woswoar cache of this version")

    cache = Cache()
    for chunk in chunks[1:]:
        header, _, body = chunk.partition(_RECORD)
        relpath, host, size, mtime_ns, offset, head = header.split(_FIELD)
        cache.meta[relpath] = FileMeta(
            size=int(size),
            mtime_ns=int(mtime_ns),
            offset=int(offset),
            head=bytes.fromhex(head),
            host=host,
        )
        if not body:
            cache.files[relpath] = []
            continue

        flat = body.replace(_RECORD, _FIELD).split(_FIELD)
        remainder = len(flat) % _FIELDS_PER_ENTRY
        if remainder:
            raise ValueError(f"{relpath}: {len(flat)} fields is not a whole number of entries")
        cache.files[relpath] = flat
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

        flat, new_offset = _read_from(log.path, log.host_id, offset)
        if offset == 0:
            # Parsed from the start, so this replaces whatever was held before --
            # which is also how a rewritten file gets its stale entries dropped.
            cache.files[log.relpath] = flat
        else:
            cache.files.setdefault(log.relpath, []).extend(flat)

        cache.meta[log.relpath] = FileMeta(
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            offset=new_offset,
            head=head,
            host=log.host_id,
        )
        cache.unsaved += len(flat) // _FIELDS_PER_ENTRY
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


def load_columns() -> Cache:
    """The cache, refreshed and persisted, without building any `Entry`.

    What `load_entries` does minus its last step. Ctrl-R takes this route and
    asks the `Cache` for the two columns it displays; anything that wants whole
    entries calls `load_entries` and pays for them.
    """
    cache = load()
    refresh(cache)
    # Always write the first build -- that is what makes every later run cheap.
    # After that, defer until enough has accumulated to be worth the ~48 ms.
    if cache.unsaved and (cache.unsaved >= _SAVE_THRESHOLD or not store.cache_file().exists()):
        save(cache)
    return cache


def load_entries() -> list[Entry]:
    """Load, incrementally update, persist, and build every `Entry`."""
    return load_columns().entries()
