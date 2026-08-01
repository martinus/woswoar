"""Encryption and signatures, delegated entirely to ``age`` and ``ssh-keygen``.

Python's standard library has no cipher and no signature scheme -- only hashing
and randomness -- so both halves of this need an external tool.

``age`` answers **"who may read this?"**. It was chosen because it is one small
static binary and it accepts SSH public keys as recipients, so each machine can
use the keypair it already pushes to git with and no secret is ever copied
between machines.

``ssh-keygen -Y`` answers **"which machine wrote this?"**, which age cannot: it
has no notion of a sender, and the recipient list is published in the repo, so
on its own anyone who can push could seal a chunk every machine would open and
offer in Ctrl-R.

That second question was once answered with an HMAC over a key shared by the
whole fleet. It cannot be: whoever holds the key to *check* a tag holds the key
to *forge* one, so a machine that was enrolled and then revoked could keep
publishing history every other machine believed (issue #38). Nothing symmetric
fixes that, which is why signing is asymmetric and per-machine. The private half
never leaves the machine that made it, because no one else ever needed it.

Nothing here knows about history, chunks, or git; it is a thin, testable seam so
that swapping either backend later touches one file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from .errors import WoswoarError

AGE = "age"
AGE_KEYGEN = "age-keygen"
SSH_KEYGEN = "ssh-keygen"

#: What ssh-keygen calls the signer, in an allowed-signers line. A label only:
#: `verify` builds that line from the key it was handed, so this always matches
#: itself and can never fail. What binds a signature to a machine is whatever
#: the *caller* put in the bytes it signed.
_PRINCIPAL = "woswoar"

_TIMEOUT = 120


class AgeError(WoswoarError):
    """age is missing, or ran and refused.

    One class rather than two: every call site caught both and handled them
    identically, so the split was a distinction nothing branched on.
    """


class SigningError(WoswoarError):
    """ssh-keygen is missing, or could not sign.

    Separate from :class:`AgeError` because the remedy is a different package,
    and `deps` reports them separately. A caller that does not care which tool
    failed catches :class:`~woswoar.errors.WoswoarError`, their common base.

    Note that a signature *failing to verify* is not this: that is an ordinary
    answer about a chunk, not a failure of the tool, so `verify` returns False.
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
        result = _spawn(argv, data, pass_fds)
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
    with _as_fd(secret.encode("utf-8")) as identity_fd:
        return _run([AGE, "-d", "-i", f"/dev/fd/{identity_fd}"], data, pass_fds=(identity_fd,))


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

    Normalised through `without_comment`, and here rather than at the call
    sites, because this string is not only handed to age: it is written to
    `recipients.txt`, compared for deduplication, and named by a tombstone. Two
    machines disagreeing about whether the comment is part of it would enrol the
    same key twice and revoke neither.
    """
    pub = public_half(identity)
    if pub.is_file():
        return without_comment(pub.read_text(encoding="utf-8").strip())
    # Reading the file ourselves, then reusing public_of, which already knows
    # both how to skip the subprocess when the identity carries its own
    # `# public key:` comment and how to feed age on stdin when it does not.
    return public_of(identity.read_text(encoding="utf-8"))


def without_comment(recipient: str) -> str:
    """A recipient with any trailing SSH comment removed.

    An SSH public key ends in a free-text comment, conventionally
    ``user@host`` -- so publishing one publishes a username and a hostname, in
    the one file in the repo that is deliberately plaintext. The repo goes to
    lengths to avoid that: host directories are opaque hex precisely so a
    leaked archive does not name the machines.

    age never needed it. It reads the type and the key material and ignores the
    rest, so dropping it costs nothing and is not a downgrade of any kind.
    """
    fields = recipient.split()
    if len(fields) > 2 and fields[0].startswith("ssh-"):
        return " ".join(fields[:2])
    return recipient


def fingerprint(recipient: str) -> str:
    """A short name for a recipient that is derived from the key, not chosen.

    The name shown beside a key is free text a machine publishes about itself,
    in a `name.age` that sealing needs no secret to write. So a human asked to
    approve *a name* is approving a string an attacker with push access chose --
    ``martin@laptop`` twice, and one of them is not.

    For an SSH key this is byte-for-byte what ``ssh-keygen -lf id_ed25519.pub``
    prints, which is the point: it can be checked against the machine itself
    rather than against the repo making the claim. An age recipient is already
    short, canonical, and derived from its own secret -- ``age-keygen -y``
    reproduces it -- so it is its own fingerprint and is returned unchanged.

    Anything else -- a hand-edited line, a key type this does not parse -- is
    hashed whole. It matches no external tool, but it is still derived from the
    line rather than chosen, which is the property the caller is promised: two
    malformed entries must not come out looking like the same machine.
    """
    # `hashlib` is imported here rather than at module scope because it costs a
    # measured 1.3 ms (mostly `_hashlib`) and nothing else pulls it in -- `hmac`
    # does not, since `tag` passes the digest name as a string. This module is
    # loaded by every sync, and this function runs once per machine on `grant`.
    # `base64` is already loaded by then; it is deferred only to sit with it.
    import base64
    import hashlib

    def digest(data: bytes) -> str:
        # ssh-keygen prints the digest unpadded.
        return "SHA256:" + base64.b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")

    fields = recipient.split()
    if not fields:
        return ""
    if len(fields) == 1:
        return fields[0]
    try:
        return digest(base64.b64decode(fields[1], validate=True))
    except ValueError:  # binascii.Error is a subclass
        return digest(recipient.encode())


#: How to reproduce a fingerprint, keyed by whether it is one we computed.
#: Beside `fingerprint` on purpose: the format and the command that reproduces
#: it are one decision, and splitting them is how a prompt ends up telling
#: someone to run ssh-keygen against an age identity.
_CHECK_SSH = "ssh-keygen -lf ~/.ssh/id_ed25519.pub"
_CHECK_AGE = "age-keygen -y ~/.config/woswoar/identity"


def how_to_check(fingerprint: str) -> str:
    """The command that reproduces ``fingerprint`` on the machine it names."""
    return _CHECK_SSH if fingerprint.startswith("SHA256:") else _CHECK_AGE


def how_to_check_signer() -> str:
    """The command that reproduces a *signing* fingerprint on its own machine.

    Beside `how_to_check` and separate from it: a signing key is woswoar's own,
    not the identity, so sending someone to ``~/.ssh/id_ed25519.pub`` for it
    would be advice that quietly prints a different key.
    """
    return "ssh-keygen -lf ~/.config/woswoar/signing_key.pub"


# ---------------------------------------------------------------------------
# Authorship. Answers "which of my machines wrote this?", which age cannot, and
# which a shared secret cannot either: whoever can check a MAC can forge one.
# ---------------------------------------------------------------------------


def signing_available() -> bool:
    return shutil.which(SSH_KEYGEN) is not None


def require_signing() -> None:
    if not signing_available():
        from . import deps

        raise SigningError(
            "Sync signs what it publishes, so other machines can tell which one "
            "wrote it.\n" + deps.report([deps.SSH_KEYGEN])
        )


def generate_signing_key(path: Path) -> str:
    """Create this machine's signing key at ``path``; return its public half.

    A key of woswoar's own rather than the sync identity, for a reason that is
    not stylistic: `choose_identity` may hand back an *age* identity, which is
    X25519 and cannot sign at all. Reusing it would work on machines that happen
    to sync with an SSH key and fail on the rest.

    The private half never leaves this machine and is never published, which is
    the whole point -- it is the one thing a revoked machine cannot keep a
    useful copy of, because nobody else ever needed it to verify.
    """
    require_signing()
    # 0700 on the leaf. This module deliberately does not import `store`, so
    # `private_dir` is out of reach -- but the directory it makes here is
    # woswoar's own, and inheriting a 022 umask would leave it 0755.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ssh-keygen refuses to overwrite, and would prompt about it on a terminal.
    path.unlink(missing_ok=True)
    public_half(path).unlink(missing_ok=True)
    _run_signer([SSH_KEYGEN, "-t", "ed25519", "-N", "", "-C", "woswoar", "-q", "-f", str(path)])
    # Both halves, not just the secret one. ssh-keygen writes the private key
    # 0600 and the public key at the umask -- 0644 on a stock install. Nothing
    # is exposed by a public key being readable; what breaks is the guarantee
    # that *every* path woswoar creates is owner-only, which `doctor` enforces
    # by walking them. It reported `[FAIL] private` on the first run of a fresh
    # machine, before the user had done anything wrong.
    # The private half is belt and braces: ssh-keygen writes a private key 0600
    # whatever the umask, so removing this line changes nothing observable and
    # no test can catch it. Kept anyway, because the mode of a file woswoar
    # creates should not depend on another tool's habits.
    path.chmod(0o600)
    public_half(path).chmod(0o600)
    return signing_public(path)


def public_half(path: Path) -> Path:
    """The ``.pub`` sibling of a key file.

    One spelling, because ``with_suffix(".pub")`` and ``suffix + ".pub"`` differ
    for any name containing a dot and agreeing by accident is how they drift.
    """
    return path.with_suffix(path.suffix + ".pub")


def signing_public(path: Path) -> str:
    """The verify half of a signing key, as it is published and pinned."""
    return public_half(path).read_text(encoding="utf-8").strip()


def _spawn(
    argv: list[str], data: bytes | None = None, pass_fds: tuple[int, ...] = ()
) -> subprocess.CompletedProcess[bytes]:
    """Run a tool and hand back the result, judging nothing.

    The three callers want different things from a nonzero exit -- age's is an
    `AgeError`, ssh-keygen's a `SigningError`, and `verify`'s is simply "no" --
    so the judgement is theirs. What they agree on is the invocation, and that
    is here so the timeout, the capture and `check=False` cannot drift apart.
    """
    return subprocess.run(
        argv, input=data, capture_output=True, check=False, timeout=_TIMEOUT, pass_fds=pass_fds
    )


def _run_signer(argv: list[str], data: bytes | None = None) -> bytes:
    try:
        result = _spawn(argv, data)
    except FileNotFoundError as exc:
        raise SigningError(f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - needs a hung ssh-keygen
        raise SigningError(f"{argv[0]} timed out after {_TIMEOUT}s") from exc
    if result.returncode != 0:
        raise SigningError(
            result.stderr.decode("utf-8", errors="replace").strip() or "signing failed"
        )
    return result.stdout


def sign(data: bytes, key: Path, namespace: str) -> bytes:
    """Sign ``data`` with this machine's key, returning the armoured signature.

    The data goes in on stdin and the signature comes back on stdout, so nothing
    being signed is written to a temporary file. ``-f`` is the one real path any
    of this needs: ssh-keygen will not take a private key on an inherited
    descriptor -- ``-f /dev/fd/N`` fails with "Couldn't load public key", which
    is what stalled the first attempt at this (PR #28). The path it is given is
    woswoar's own 0600 file in woswoar's own config directory, so the rule that
    matters -- never name a file of the *user's* that a sandboxed tool cannot
    open -- is intact.
    """
    require_signing()
    return _run_signer([SSH_KEYGEN, "-Y", "sign", "-q", "-f", str(key), "-n", namespace], data=data)


def verify(data: bytes, signature: bytes, verify_key: str, namespace: str) -> bool:
    """Whether ``signature`` is ``principal``'s signature over ``data``.

    Returns a bool rather than raising, because a signature that does not verify
    is an ordinary fact about a chunk -- someone published one your machines did
    not write -- and not a failure of the tool.

    Both inputs reach ssh-keygen on inherited pipes as ``/dev/fd/N``, the same
    way `decrypt_with_secret` hands age an identity: the allowed-signers list is
    built here from the single key the caller already decided to trust, so there
    is no file of trusted keys on disk to be edited behind woswoar's back, and
    no temporary file to clean up. The question asked is exactly "did *this* key
    sign this?", never "did anyone in some list sign this?".

    ssh-keygen's own principal matching does no work here and is not asked to:
    the allowed-signers line is built from the key, so the principal always
    matches itself. There was an argument for the caller to pass one anyway, to
    make ssh-keygen's errors readable -- but this captures its output and reads
    only the exit status, so nobody ever saw them. Binding a signature to a
    machine is the caller's job, done in the bytes it signs.

    ``namespace`` is not decoration: ssh-keygen refuses a signature made under a
    different one, which is what stops a signature meant for one purpose being
    replayed as another. The caller owns it, because the caller owns the format
    it versions.
    """
    require_signing()
    allowed = f"{_PRINCIPAL} {verify_key}\n".encode()
    with _as_fd(allowed) as allowed_fd, _as_fd(signature) as signature_fd:
        result = _spawn(
            [
                SSH_KEYGEN,
                "-Y",
                "verify",
                "-q",
                "-f",
                f"/dev/fd/{allowed_fd}",
                "-I",
                _PRINCIPAL,
                "-n",
                namespace,
                "-s",
                f"/dev/fd/{signature_fd}",
            ],
            data,
            (allowed_fd, signature_fd),
        )
    return result.returncode == 0


@contextmanager
def _as_fd(data: bytes) -> Iterator[int]:
    """``data`` as a readable ``/dev/fd/N``, for a subprocess to open.

    How both tools are handed key material without a path: age gets an identity
    this way, ssh-keygen an allowed-signers line and a signature. The rule it
    serves is the same one -- never name a file a sandboxed tool might not be
    able to open, and never put a secret in a temporary file.

    Every payload is small: an identity, an allowed-signers line, an armoured
    signature, a few hundred bytes each. So writing and closing before the child
    starts reading cannot fill the pipe buffer and deadlock.
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, data)
        os.close(write_fd)
        write_fd = -1
        yield read_fd
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)


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
