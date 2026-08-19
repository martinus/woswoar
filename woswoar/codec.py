"""Compressing a chunk on the way in, and refusing a bomb on the way out.

A leaf: `zlib`, and nothing from the package. It came out of `sync.py` in #214
as the third and cleanest of the three slices, participating in none of the
orderings that keep `run`, `export`, `merge` and `_Day` together.

Lifted because `unpack`'s bound is a claim in `docs/security.md` -- "a chunk
that unpacks past the cap is refused and reported, and the peak allocation is
measured to stay bounded rather than the payload being materialised first" --
and claims there are backed by tests rather than prose. Until this moved,
reaching it from a test meant driving `sync.run()`.

**Reached by attribute, never by binding the names.** `sync` says
`codec.pack(...)`, and eight tests patch `codec.MAX_EXPORT_BYTES` and
`codec.pack` by name; a caller that imported those names directly would bind
them at import and silently unhook every one of those patches. `manifest` and `gitrepo`
carry the same rule, and `tests/test_architecture.py`'s `SPY_SEAMS` is what
enforces it.

`MAX_CHUNK_BYTES` and `MAX_EXPORT_BYTES` are a reader/writer pair and belong
together; the argument for why there are two is on the second.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator


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
