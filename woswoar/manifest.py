"""Which chunks a host says are its own, signed so nobody else can say it.

The whole of chunk authenticity rests on this, which is why it is a module of its
own rather than a section of `sync`: it is self-contained -- crypto, a path from
`archive`, and an atomic write -- and until it moved, reaching any of it from a
test meant driving `sync.run()`.

Each host signs a manifest of its own chunks, per day, with a key only it holds.
That is what makes a chunk attributable to one machine rather than to the fleet,
and so what lets `revoke` stop a machine publishing as well as reading.

`sync` reaches these by attribute -- ``manifest.open_chunk(...)`` -- and never by
binding the bare name, so that this module's attributes stay the single seam the
tests count chunk reads and signature checks at. `read`,
`write` and `open_chunk` are the three they patch, and one of those counts is
asserted to be *zero*, which is the shape that passes vacuously against a spy
nothing calls. See `gitrepo` for the same argument at more length, and
`tests/test_architecture.py::TestTheSeamsAreReachedByAttribute` for the check.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import NamedTuple

from . import archive, crypto, store
from .store import Machine

#: First token of a manifest body, and the ssh-keygen signature namespace, which
#: are deliberately the same string. It is *inside the signed bytes* and also the
#: domain the signature is made in, so a future manifest shape gets one new value
#: and old signatures stop verifying against it twice over -- rather than a
#: version check somebody has to remember to write.
_MAGIC = "woswoar-manifest-v1"

#: Separates the armoured signature from the bytes it covers. The signature is
#: in the same file as the body, not beside it, so `write_atomic` makes the two
#: impossible to disagree -- a detached pair has a window where one has landed
#: and the other has not, and during that window a real chunk looks forged.
_SEPARATOR = "\n\n"


def digest_of(data: bytes) -> str:
    """The digest a manifest records for a chunk."""
    return hashlib.sha256(data).hexdigest()


def signs_and_verifies(key: Path, verify_key: str) -> bool:
    """Whether ``key`` can actually produce a signature this host accepts back.

    Here rather than in `sync.signing_status`, which reports it, because the
    namespace is the thing being tested and the namespace is this module's.
    `_MAGIC` is *inside* the signed bytes, so a self-test made in some other
    namespace would pass while every real manifest failed to verify -- which is
    the failure `doctor` runs this check to rule out.

    Signed and verified for real rather than inspecting the key file, the same
    argument `crypto.why_unusable` makes about age: what matters is whether an
    unattended sync will work.
    """
    probe = b"woswoar signing selftest"
    return crypto.verify(probe, crypto.sign(probe, key, _MAGIC), verify_key, _MAGIC)


def _header(host_id: str, day: str) -> str:
    """The line that binds a manifest to one host and one day.

    Built in one place and compared in another, so the round trip cannot be
    broken from one side only.
    """
    return f"{_MAGIC} {host_id} {day}"


class ManifestEntry(NamedTuple):
    """One line of a manifest: a chunk, and what it replaced.

    ``subsumes`` is empty for an ordinary chunk and holds the names `compact`
    merged into this one otherwise. It is in the *signed* bytes because it
    decides what a peer does with the chunk, and anything that decides that has
    to be something only the publishing machine can say.

    Not `Entry`, which `entry.Entry` already is -- a recorded command. The two
    lived in one package under one name until this module was split out, and a
    package-wide grep could not tell them apart.
    """

    digest: str
    subsumes: tuple[str, ...] = ()


def _body(host_id: str, day: str, entries: dict[str, ManifestEntry]) -> str:
    """The signed part of a manifest: what this host claims it wrote that day.

    The header names the host and the day, so a genuine manifest cannot be
    lifted to another host's directory or another date and still verify --
    ssh-keygen's own principal matching cannot do that job (see
    `crypto.verify`), so it is done here, in the bytes the signature covers.

    Sorted, so the same set of chunks always produces byte-identical output and
    a sync that adds nothing rewrites nothing.
    """
    lines = [_header(host_id, day)]
    for name in sorted(entries):
        entry = entries[name]
        lines.append(" ".join([name, entry.digest, *entry.subsumes]))
    return "\n".join(lines) + "\n"


def write(known: Machine, day: str, entries: dict[str, ManifestEntry]) -> None:
    """Sign this host's chunk list for ``day`` and write it."""
    body = _body(known.id, day, entries)
    signature = crypto.sign(body.encode("utf-8"), store.signing_key_file(), _MAGIC)
    blob = signature.decode("utf-8").strip() + _SEPARATOR + body
    store.write_atomic(archive.day_manifest(known.id, day), blob.encode("utf-8"))


def read(host_id: str, day: str, verify_key: str) -> dict[str, ManifestEntry]:
    """The digests ``host_id`` signed for ``day``, or ``{}`` if it did not.

    Empty rather than an exception for an unsigned, malformed, mis-signed or
    mis-addressed manifest, because callers do the same thing with all of them:
    every chunk of that day goes unverified and is refused. Distinguishing them
    would be distinguishing a truthful failure from an attacker's, which is
    exactly the split #29 tried for chunk tags and reverted -- an attacker can
    produce any of these shapes at will.
    """
    try:
        blob = archive.day_manifest(host_id, day).read_text(encoding="utf-8")
    except OSError:
        return {}

    signature, separator, body = blob.partition(_SEPARATOR)
    if not separator:
        return {}
    if not crypto.verify(body.encode("utf-8"), signature.encode("utf-8"), verify_key, _MAGIC):
        return {}

    lines = body.splitlines()
    # Checked *after* the signature, so this only ever parses bytes the host's
    # own key vouched for. The header binding is the point: without it a
    # manifest signed for one day would verify for every other day.
    if not lines or lines[0] != _header(host_id, day):
        return {}

    entries: dict[str, ManifestEntry] = {}
    for line in lines[1:]:
        name, _, rest = line.partition(" ")
        digest, _, subsumed = rest.partition(" ")
        if name and digest:
            entries[name] = ManifestEntry(digest, tuple(subsumed.split()))
    return entries


def claimed_names(host_id: str, day: str) -> set[str]:
    """The names a day's manifest claims, *without* checking who signed it.

    Only ever used to decide whether a signature check is worth doing. A forged
    manifest can make a chunk look accounted for here, and that is fine: the
    same forgery makes every peer refuse the whole day, which `sync` reports as
    unauthenticated rather than as a stray file.
    """
    try:
        blob = archive.day_manifest(host_id, day).read_text(encoding="utf-8")
    except OSError:
        return set()
    _, _, body = blob.partition(_SEPARATOR)
    return {line.partition(" ")[0] for line in body.splitlines()[1:]}


def exists(host_id: str, day: str) -> bool:
    """Whether a day has a signed list at all, said once so both callers agree.

    Meaningless on its own -- a day never published has no manifest either --
    so every caller pairs it with "did this machine publish this day", which is
    its own export watermark.
    """
    return archive.day_manifest(host_id, day).exists()


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
