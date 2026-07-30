"""Encryption, delegated entirely to the ``age`` binary.

Python's standard library has no cipher -- only hashing and randomness -- so
sealing the synced history needs an external tool. ``age`` was chosen because it
is one small static binary and it accepts **SSH public keys as recipients**,
which means each machine can use the keypair it already pushes to git with and
no secret ever has to be copied between machines.

Nothing here knows about history, chunks, or git; it is a thin, testable seam so
that swapping the backend later touches one file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from .errors import WoswoarError

AGE = "age"
AGE_KEYGEN = "age-keygen"

_TIMEOUT = 120


class AgeError(WoswoarError):
    """age is missing, or ran and refused.

    One class rather than two: every call site caught both and handled them
    identically, so the split was a distinction nothing branched on.
    """


class Identity(NamedTuple):
    secret: str
    public: str


_MISSING = (
    "woswoar: 'age' not found on PATH.\n"
    "Sync encrypts every line before it reaches git, so age is required.\n"
    "  Fedora:  sudo dnf install age\n"
    "  Debian:  sudo apt install age\n"
    "  macOS:   brew install age"
)

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
        raise AgeError(_MISSING)


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


def encrypt_to_recipients(data: bytes, recipients_file: Path) -> bytes:
    """Seal ``data`` so that every recipient listed in the file can open it.

    age wraps one random file key separately per recipient, so the payload is
    stored once no matter how many machines are listed.
    """
    return _run([AGE, "-R", str(recipients_file)], data)


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
    # On stdin, not as a path argument -- see decrypt_with_file.
    return _run([AGE_KEYGEN, "-y"], identity.read_bytes()).decode("utf-8").strip()


def why_unusable(identity: Path) -> str:
    """``""`` if this identity can decrypt unattended, else why not.

    Checked by actually performing a round trip rather than by inspecting the
    key file, because the thing that matters is whether an unattended sync will
    work, not what format the key claims to be.

    The reason is returned rather than a bare bool because the two failures
    need different advice and used to be reported as the same one: a key that
    needs a passphrase wants ``--new-identity``, whereas a key this process
    cannot even read wants the *file* looked at, and telling someone their
    unencrypted key needs a passphrase sends them the wrong way entirely.
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
    except (AgeError, OSError):
        return "needs a passphrase, so an unattended sync could never use it"
    return ""


def usable(identity: Path) -> bool:
    return not why_unusable(identity)
