"""Encryption, delegated entirely to the ``age`` binary, plus one MAC.

Python's standard library has no cipher -- only hashing and randomness -- so
sealing the synced history needs an external tool. ``age`` was chosen because it
is one small static binary and it accepts **SSH public keys as recipients**,
which means each machine can use the keypair it already pushes to git with and
no secret ever has to be copied between machines.

``age`` answers "who may read this?" and nothing else. It has no notion of a
sender, and the recipient list is published in the repo, so on its own anyone
who can push could seal a chunk every machine would open and offer in Ctrl-R.
Answering "did one of *my* machines write this?" needs a second primitive, and
that one *is* in the standard library: :func:`tag` is ``hmac`` over the sealed
bytes with a key that lives encrypted in the repo, readable only by machines
that are already recipients.

That is the only cryptographic primitive woswoar composes itself, and it is the
one with nothing to get wrong: no nonce, no mode, no padding, no IV, and a
constant-time comparison the standard library provides. Everything else is
still someone else's audited binary.

Nothing here knows about history, chunks, or git; it is a thin, testable seam so
that swapping the backend later touches one file.
"""

from __future__ import annotations

import hmac
import os
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from .errors import WoswoarError

AGE = "age"
AGE_KEYGEN = "age-keygen"

#: HMAC-SHA256 output, and so the fixed-width prefix every chunk carries.
TAG_BYTES = 32

#: Key length. The same 32 as :data:`TAG_BYTES` by coincidence, not by
#: derivation -- widening the tag is a format change, and re-sizing the key
#: is not, so reading one from the other would couple two unrelated decisions.
_KEY_BYTES = 32

_TIMEOUT = 120


class AgeError(WoswoarError):
    """age is missing, or ran and refused.

    One class rather than two: every call site caught both and handled them
    identically, so the split was a distinction nothing branched on.
    """


class Identity(NamedTuple):
    secret: str
    public: str


def _missing_message() -> str:
    """Built when needed, so the install command matches *this* machine.

    The previous version listed Fedora, Debian and macOS unconditionally and
    left the reader to pick; `deps` detects the distro instead.
    """
    from . import deps

    return "Sync encrypts every line before it reaches git.\n" + deps.report([deps.AGE])


#: age emits this when it wants a passphrase it cannot ask for. Worth
#: recognising: an unattended sync from a systemd timer has no terminal, so a
#: passphrase-protected SSH key turns into a recurring silent failure.
_PASSPHRASE_MARKER = "failed to obtain passphrase"

_PASSPHRASE_HELP = (
    "the identity is passphrase-protected and age cannot prompt here.\n"
    "age does not use ssh-agent. Either point woswoar at an unencrypted key, or\n"
    "run 'woswoar init --new-identity' to give this machine a dedicated one."
)


def available() -> bool:
    return shutil.which(AGE) is not None and shutil.which(AGE_KEYGEN) is not None


def require() -> None:
    if not available():
        raise AgeError(_missing_message())


def _run(argv: list[str], data: bytes | None = None, pass_fds: tuple[int, ...] = ()) -> bytes:
    require()
    try:
        result = subprocess.run(
            argv,
            input=data,
            capture_output=True,
            check=False,
            timeout=_TIMEOUT,
            pass_fds=pass_fds,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - needs a hung age
        raise AgeError(f"{argv[0]} timed out after {_TIMEOUT}s") from exc

    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        if _PASSPHRASE_MARKER in message:
            raise AgeError(_PASSPHRASE_HELP)
        raise AgeError(message or f"{argv[0]} failed with status {result.returncode}")
    return result.stdout


def encrypt_to_recipients(data: bytes, recipients: Iterable[str]) -> bytes:
    """Seal ``data`` so that every one of ``recipients`` can open it.

    age wraps one random file key separately per recipient, so the payload is
    stored once no matter how many machines are listed.

    Takes the keys, not the path to the file holding them, for the same reason
    the identity functions do: no age invocation may name a file in ``$HOME``.
    Passing ``-R recipients.txt`` reintroduced exactly the failure this module
    exists to avoid, one step later in `init`.
    """
    keys = list(recipients)
    if not keys:
        raise AgeError("no recipients to seal to; run 'woswoar init' first")
    argv = [AGE]
    for key in keys:
        argv += ["-r", key]
    return _run(argv, data)


def encrypt_to(data: bytes, recipient: str) -> bytes:
    """Seal ``data`` to a single recipient (used for the per-day key)."""
    return _run([AGE, "-r", recipient], data)


def decrypt_with_file(data: bytes, identity: Path) -> bytes:
    """Open ``data`` using an identity stored on disk (an SSH or age key).

    Python reads the file and hands age the *bytes*, never the path. Passing
    a path makes the operation depend on age being able to open it, which is
    not the same question as whether the user can: a sandboxed age -- snap,
    flatpak, or anything else confined -- is refused access to ``~/.config``
    and ``~/.ssh`` and fails with a bare "permission denied" on a file the
    owner can plainly read.
    """
    return decrypt_with_secret(data, identity.read_text(encoding="utf-8"))


def decrypt_with_secret(data: bytes, secret: str) -> bytes:
    """Open ``data`` using an in-memory identity.

    The secret is handed to age through a pipe rather than a temporary file, so
    a day key recovered during sync is never written to disk. Identities are a
    couple of hundred bytes, comfortably inside the pipe buffer, so writing and
    closing before age starts reading cannot deadlock.
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, secret.encode("utf-8"))
        os.close(write_fd)
        write_fd = -1
        return _run([AGE, "-d", "-i", f"/dev/fd/{read_fd}"], data, pass_fds=(read_fd,))
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)


def generate_identity() -> Identity:
    """Create a fresh X25519 identity, returned rather than written to disk."""
    secret = _run([AGE_KEYGEN]).decode("utf-8")
    return Identity(secret=secret, public=public_of(secret))


def public_of(secret: str) -> str:
    """Recover the public key from an identity's text."""
    for line in secret.splitlines():
        line = line.strip()
        if line.startswith("# public key: "):
            return line.removeprefix("# public key: ").strip()
    # age-keygen always emits the comment, but a hand-written identity file may
    # not, so fall back to asking age itself.
    return _run([AGE_KEYGEN, "-y"], secret.encode("utf-8")).decode("utf-8").strip()


def recipient_for(identity: Path) -> str:
    """The public recipient string matching an identity file.

    For an SSH key this is the contents of its ``.pub`` sibling, which is what
    other machines will encrypt to; for an age identity, age derives it.
    """
    pub = identity.with_suffix(identity.suffix + ".pub")
    if pub.is_file():
        return pub.read_text(encoding="utf-8").strip()
    # Reading the file ourselves, then reusing public_of, which already knows
    # both how to skip the subprocess when the identity carries its own
    # `# public key:` comment and how to feed age on stdin when it does not.
    return public_of(identity.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Authenticity. Answers "did one of my machines write this?", which age cannot.
# ---------------------------------------------------------------------------


def new_mac_key() -> bytes:
    """A fresh key for :func:`tag`, from the OS random source.

    ``os.urandom`` rather than ``secrets.token_bytes``, which is a thin wrapper
    over it: importing ``secrets`` drags in ``random`` and ``bisect`` for ~1.4 ms
    of interpreter start, and this module is loaded by every `sync`.
    """
    return os.urandom(_KEY_BYTES)


def tag(key: bytes, data: bytes) -> bytes:
    """The authentication tag for ``data``.

    Computed over the *sealed* bytes rather than the plaintext, so a reader can
    establish that a chunk came from one of its own machines before decrypting
    or decompressing it -- encrypt-then-MAC, the ordering that does not need
    the recipient to parse hostile input first.
    """
    return hmac.new(key, data, "sha256").digest()


def tag_matches(key: bytes, data: bytes, expected: bytes) -> bool:
    """Whether ``expected`` is the right tag for ``data``.

    ``compare_digest`` rather than ``==``: the standard library's constant-time
    comparison, so the check cannot be turned into an oracle by timing it.
    """
    return hmac.compare_digest(tag(key, data), expected)


def selftest() -> str:
    """``""`` if age can actually do the work here, else what went wrong.

    Generates an identity, seals to it, and reopens it with the key delivered on
    an inherited pipe as ``/dev/fd/N`` -- the path every real decrypt takes, and
    the one assumption the "never hand age a path" rule still rests on.

    Needs no repo, no configuration and no disk, which is the point: the failure
    this exists to catch happens during `init`, before there is anything else to
    check. ``age --version`` proves only that a binary is on PATH, and a
    sandboxed age passes that and then cannot open a thing.
    """
    probe = b"woswoar selftest"
    try:
        identity = generate_identity()
        sealed = encrypt_to(probe, identity.public)
        if decrypt_with_secret(sealed, identity.secret) != probe:
            return "age ran but returned different bytes than it was given"
    except (AgeError, OSError) as exc:
        return str(exc)
    return ""


def why_unusable(identity: Path) -> str:
    """``""`` if this identity can decrypt unattended, else why not.

    Checked by actually performing a round trip rather than by inspecting the
    key file, because the thing that matters is whether an unattended sync will
    work, not what format the key claims to be.

    The reason is returned rather than a bare bool because the failures need
    different advice and used to be reported as the same one: a key that needs
    a passphrase wants ``--new-identity``, whereas a key this process cannot
    even read wants the *file* looked at, and telling someone their unencrypted
    key needs a passphrase sends them the wrong way entirely. The wording comes
    from `_run`, which already classifies the passphrase case from age's own
    stderr -- inferring it from which step failed is how the misdiagnosis
    happened in the first place.
    """
    try:
        identity.read_bytes()
    except OSError as exc:
        return f"cannot be read: {exc.strerror}"

    try:
        recipient = recipient_for(identity)
        sealed = encrypt_to(b"woswoar", recipient)
    except (AgeError, OSError) as exc:
        return f"no usable public key: {exc}"

    try:
        if decrypt_with_file(sealed, identity) != b"woswoar":
            return "age round trip did not return the original"
    except (AgeError, OSError) as exc:
        return f"cannot decrypt: {exc}"
    return ""


def usable(identity: Path) -> bool:
    return not why_unusable(identity)
