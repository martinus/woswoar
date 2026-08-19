"""Which machine has accepted which -- the question an enrolment leaves open.

Accepting is per-machine and one-directional: enrolling a laptop means running
`woswoar accept` on the laptop *and* on every machine already in the fleet. With
N machines that is N walks, and nothing anywhere reported how far through you
were. A machine you missed is silent -- it keeps recording, publishes fine, and
simply never merges the history of the one you forgot.

So each host publishes what it has pinned, and this module is both halves of
that file: what a host writes about itself, and what a reader may conclude from
someone else's.

**A cell is what that machine says, never what is true.** What a machine accepts
is deliberately local -- `State.signers` says why, and `trust` repeats it:
anyone who can push can rewrite `hosts/<id>/signer.pub`, so the key a host is
held to has to be remembered somewhere the remote cannot reach. A published
`accepts` file is therefore *advisory*, and the report says so per row: a
verdict -- `doctor`'s own tick or cross -- for a host whose signing key this
machine has pinned, and `?` for one it has not, because there the file's author
is "whoever can push". The matrix must never become the thing someone checks
instead of running `accept`, or the repository has made the trust decision the
design refuses to let it make.

**Signature inside the sealed file, not beside it.** `manifest` makes this
argument first and it applies unchanged: a detached pair has a window where one
half has landed and the other has not, and during that window an honest file
looks forged. One `write_atomic` of one blob has no such window.

**Sealed rather than plaintext**, because the host directories are opaque hex on
purpose. The *nodes* of this graph are already public in `signer.pub`; the edges
would be new.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from . import archive, crypto, store

#: First token of the body and the ssh-keygen signature namespace, deliberately
#: the same string, for the reason `manifest._MAGIC` gives: the shape is inside
#: the signed bytes *and* is the domain the signature was made in, so a future
#: shape invalidates old signatures twice over rather than through a version
#: check somebody has to remember to write.
MAGIC = "woswoar-accepts-v1"

#: Separates the armoured signature from the bytes it covers, as in `manifest`.
_SEPARATOR = "\n\n"


class Accepts(NamedTuple):
    """What one host published about who it accepts, and how much to believe."""

    #: Host id -> the fingerprint of the key it accepted that host under. The
    #: fingerprint is half the point: "B accepted A" is only meaningful about a
    #: *key*, since an attacker who can push can move `hosts/A/signer.pub` and a
    #: row naming A alone would then read as B vouching for whatever sits there
    #: now. `__main__._cell` is what compares it with the local pin.
    hosts: dict[str, str]
    #: When it said so, from the body -- a row is only as fresh as that host's
    #: last push, and a reader who cannot see the date cannot see that.
    when: int
    #: Whether the signature was checked against a key *this* machine pinned.
    #: False means the file parsed and nothing more: its author is whoever can
    #: push. Never let this decide anything except how the row is printed.
    verified: bool


def mine(signers: dict[str, str], now: int) -> Accepts:
    """This machine's own row, from its pins rather than from a published file.

    Through here rather than assembled at the call site, and that is worth a
    function: `hosts` is fingerprints and `State.signers` is whole keys, so
    `Accepts(dict(state.signers), ...)` type-checks, reads correctly, and is
    wrong in the one row nothing can catch it in. Every other row arrives
    through `parse`, which puts fingerprints there because `body` wrote them;
    the row a machine keeps about itself is the only one with no file behind it,
    and it was the only one built by hand -- so `__main__._cell` compared a key
    with a fingerprint and reported every machine this one had accepted as
    accepted under a key it had not pinned.

    ``verified`` is `True` and is not a claim about a signature: there is no
    signature, because there is no file. It is the local pin, which is the one
    thing in this module that the repository cannot reach.
    """
    return Accepts(
        {host: crypto.fingerprint(key) for host, key in signers.items()}, now, verified=True
    )


def body(host_id: str, accepted: dict[str, str], now: int) -> str:
    """What `host_id` signs: one line per host it has accepted.

    Sorted, so a host that accepted nothing new republishes byte-identical
    bytes and the guard in `sync` writes nothing.

    The fingerprint is carried beside each id rather than the id alone, because
    "B accepted A" is only meaningful about a *key*: an attacker who can push
    can move `hosts/<A>/signer.pub`, and a row that named A alone would then
    read as B vouching for whatever key sits there now.
    """
    lines = [f"{MAGIC} {host_id} {now}"]
    lines += [f"{other} {crypto.fingerprint(key)}" for other, key in sorted(accepted.items())]
    return "\n".join(lines) + "\n"


def parse(blob: str, host_id: str, verify_key: str | None) -> Accepts | None:
    """What `host_id`'s published file says, or `None` if it says nothing usable.

    `None` rather than an exception for a malformed, mis-addressed or
    mis-signed file, and for the reason `manifest.read` gives: a caller does the
    same thing with all of them, and telling a truthful failure from an
    attacker's would be distinguishing shapes an attacker can produce at will.

    `verify_key` is what *this* machine pinned for that host, or `None` if it
    has not pinned one. A file that fails a signature check against a key we
    pinned is refused outright -- that is a claim about a machine we know, made
    by something that is not it. A file we cannot check at all is returned with
    `verified=False`, because "this host has not been accepted here yet" is
    exactly the state the report exists to show.
    """
    signature, separator, text = blob.partition(_SEPARATOR)
    if not separator:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    head = lines[0].split()
    if len(head) != 3 or head[0] != MAGIC or head[1] != host_id:
        return None
    try:
        when = int(head[2])
    except ValueError:
        return None
    verified = False
    if verify_key is not None:
        if not crypto.verify(text.encode("utf-8"), signature.encode("utf-8"), verify_key, MAGIC):
            return None
        verified = True
    hosts = {
        parts[0]: parts[1] for parts in (line.split() for line in lines[1:]) if len(parts) == 2
    }
    return Accepts(hosts, when, verified)


def published(host_id: str, verify_key: str | None, identity: Path) -> Accepts | None:
    """Read and open `host_id`'s file. `None` when there is nothing to read.

    `identity` is passed through to `crypto.decrypt_with_file`; a host that
    published before this existed, or has not synced since, simply has no file
    -- which is the `?` the report needs anyway.
    """
    sealed = archive.accepts_seal(host_id)
    try:
        blob = crypto.decrypt_with_file(sealed.read_bytes(), identity)
    except (crypto.AgeError, OSError):
        return None
    return parse(blob.decode("utf-8", errors="replace"), host_id, verify_key)


def seal(known_id: str, accepted: dict[str, str], recipients: list[str], now: int) -> bytes:
    """This machine's own file: signed, then sealed to the fleet."""
    text = body(known_id, accepted, now)
    signature = crypto.sign(text.encode("utf-8"), store.signing_key_file(), MAGIC)
    blob = signature.decode("utf-8").strip() + _SEPARATOR + text
    return crypto.encrypt_to_recipients(blob.encode("utf-8"), recipients)
