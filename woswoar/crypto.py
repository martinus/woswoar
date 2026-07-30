"""Encryption and signing, delegated entirely to ``age`` and ``ssh-keygen``.

Python's standard library has no cipher -- only hashing and randomness -- so
sealing the synced history needs an external tool. ``age`` was chosen because it
is one small static binary and it accepts **SSH public keys as recipients**,
which means each machine can use the keypair it already pushes to git with and
no secret ever has to be copied between machines.

``age`` gives confidentiality and nothing else: it has no notion of a sender, so
a recipient list published in the repo lets *anyone who can push* seal a chunk
that every machine will happily open. Authenticity therefore needs a second
primitive, and ``ssh-keygen -Y sign``/``-Y verify`` is it -- OpenSSH's signature
mode, which is as much a "one job, already audited, already installed" tool as
age is. Same rule as encryption: no primitive is composed here, only invoked.

Nothing here knows about history, chunks, or git; it is a thin, testable seam so
that swapping either backend later touches one file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from .errors import WoswoarError

AGE = "age"
AGE_KEYGEN = "age-keygen"
SSH_KEYGEN = "ssh-keygen"

#: Domain separator for chunk signatures. ssh-keygen refuses a signature made
#: for a different namespace, so a signature harvested from some other use of
#: the same key -- git commit signing, `ssh-keygen -Y sign` in a script -- can
#: never be replayed as a woswoar chunk.
CHUNK_NAMESPACE = "woswoar-chunk"

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
    return _spawn(argv, data, pass_fds)


def _spawn(argv: list[str], data: bytes | None = None, pass_fds: tuple[int, ...] = ()) -> bytes:
    """Run a tool and return its stdout, raising :class:`AgeError` on failure.

    Split out from :func:`_run` so the ssh-keygen calls below do not assert that
    *age* is installed: signing and encryption are separate tools, and reporting
    a missing age when ssh-keygen is what is absent sends the reader to the
    wrong package.
    """
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
# Signing. Answers "who wrote this?", which age cannot.
# ---------------------------------------------------------------------------


def signing_available() -> bool:
    return shutil.which(SSH_KEYGEN) is not None


def require_signing() -> None:
    if not signing_available():
        from . import deps

        raise AgeError(
            "Sync verifies who wrote every chunk before opening it.\n"
            + deps.report([deps.SSH_KEYGEN])
        )


def generate_signing_key() -> Identity:
    """Create a fresh unencrypted ed25519 signing keypair.

    Every machine gets a dedicated signing key rather than reusing whichever
    identity `sync.choose_identity` picked. That identity may be an *age* key,
    which is X25519 and cannot sign at all, so reuse would work on some machines
    and fail on others -- one code path that always works beats a branch that
    only mostly does.

    ssh-keygen can only write a keypair to a path, so this generates into a
    private temporary directory and hands back the bytes. Storing it is the
    caller's business, which keeps the "no key material is written by this
    module" property the encryption half already has.
    """
    require_signing()
    with tempfile.TemporaryDirectory(prefix="woswoar-key-") as scratch:
        path = Path(scratch) / "signing_key"
        _spawn(
            [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "woswoar", "-f", str(path)],
        )
        return Identity(
            secret=path.read_text(encoding="utf-8"),
            public=path.with_suffix(".pub").read_text(encoding="utf-8").strip(),
        )


def public_of_signing_key(key: Path) -> str:
    """Recover a signing key's public half from the private key file."""
    require_signing()
    return _spawn([SSH_KEYGEN, "-y", "-f", str(key)]).decode("utf-8").strip()


def sign(data: bytes, key: Path) -> str:
    """Sign ``data`` with the signing key at ``key``, returning armoured text.

    Takes a path, unlike everything on the encryption side, and the exception is
    forced rather than chosen: ``ssh-keygen -Y sign`` resolves the ``.pub``
    sibling of whatever ``-f`` names, so handing it ``/dev/fd/N`` fails with
    "Couldn't load public key" -- the trick that works for age does not work
    here. The cost is small, because this key is woswoar's own file in its own
    config directory rather than something in ``~/.ssh`` a sandboxed binary may
    be refused: `doctor` proves the round trip works on this machine rather than
    assuming it.
    """
    require_signing()
    argv = [SSH_KEYGEN, "-Y", "sign", "-f", str(key), "-n", CHUNK_NAMESPACE, "-"]
    return _spawn(argv, data).decode("utf-8")


def verify(data: bytes, signature: str, public_key: str, principal: str) -> bool:
    """Whether ``signature`` is ``public_key``'s signature over ``data``.

    Returns a bool rather than raising: an unverifiable chunk is an ordinary
    thing to meet in a repo anyone can push to, and sync's job is to skip it and
    carry on, not to abort. A *missing ssh-keygen* is different and does raise,
    because silently treating "cannot check" as "not valid" would turn a missing
    package into apparently-lost history.

    ssh-keygen needs both the allowed-signers list and the signature as real
    files. Both are public data -- a public key and a signature -- so unlike key
    material they can go to a temporary directory without weakening anything.
    """
    require_signing()
    with tempfile.TemporaryDirectory(prefix="woswoar-verify-") as scratch:
        allowed = Path(scratch) / "allowed_signers"
        allowed.write_text(
            f'{principal} namespaces="{CHUNK_NAMESPACE}" {public_key.strip()}\n',
            encoding="utf-8",
        )
        sig = Path(scratch) / "chunk.sig"
        sig.write_text(signature, encoding="utf-8")
        argv = [SSH_KEYGEN, "-Y", "verify"]
        argv += ["-f", str(allowed), "-I", principal]
        argv += ["-n", CHUNK_NAMESPACE, "-s", str(sig)]
        try:
            _spawn(argv, data)
        except AgeError:
            # Through _spawn rather than subprocess.run so a hung ssh-keygen
            # becomes the same timeout error every other tool call raises. A
            # bad signature and a stuck binary both mean "do not merge this",
            # and only one of them should be able to escape as a raw
            # TimeoutExpired and abort the whole sync.
            return False
        return True


def fingerprint(public_key: str) -> str:
    """A short human-comparable digest of a signing key.

    Shown when a machine is trusted for the first time, because that is a
    decision nobody can make against 68 characters of base64.
    """
    with tempfile.TemporaryDirectory(prefix="woswoar-fp-") as scratch:
        path = Path(scratch) / "key.pub"
        path.write_text(public_key.strip() + "\n", encoding="utf-8")
        try:
            out = _spawn([SSH_KEYGEN, "-l", "-f", str(path)]).decode("utf-8")
        except AgeError:
            return public_key.strip()[:24] + "..."
    fields = out.split()
    return fields[1] if len(fields) > 1 else out.strip()


def signing_selftest() -> str:
    """``""`` if ssh-keygen can sign and verify here, else what went wrong.

    The same reasoning as :func:`selftest`: ``ssh-keygen --help`` proves a
    binary exists, not that it can complete a round trip. Signing is the one
    place woswoar still hands a tool a *path* to key material, so a confined
    ssh-keygen fails here exactly the way a confined age failed before it -- and
    an unsigned chunk is one no other machine will ever accept.
    """
    from . import store

    probe = b"woswoar selftest"
    real = store.signing_key_file()
    try:
        with tempfile.TemporaryDirectory(prefix="woswoar-selftest-") as scratch:
            # The machine's *real* key when it has one, because that is the file
            # a confined ssh-keygen is refused -- it lives beside the identity
            # in ~/.config/woswoar that produced the original "permission
            # denied". Signing a throwaway under /tmp would pass on exactly the
            # machine this exists to catch.
            key = real if real.is_file() else Path(scratch) / "signing_key"
            if key != real:
                _spawn(
                    [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "woswoar", "-f", str(key)]
                )
            public = public_of_signing_key(key)
            if not verify(probe, sign(probe, key), public, "selftest"):
                return "ssh-keygen signed but would not verify its own signature"
    except (AgeError, OSError) as exc:
        return str(exc)
    return ""


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
