"""A proof the user can run: nothing readable leaves this machine.

docs/security.md pins its claims with tests, but those run in CI -- somebody
else's machine, on somebody else's history. The gap this module closes is the
question a stranger evaluating woswoar actually has: *does the copy installed
here, today, publish anything I could regret?* No document answers that. A
round trip they can watch does.

So `woswoar doctor --prove` builds a complete throwaway installation in a
temporary directory -- its own config, identity, signing key, and a bare git
"remote" that is just another directory -- records one canary command, syncs,
and then demonstrates four things about the bytes that reached the remote:

- the canary went in (a proof against an empty repo would be vacuous),
- it comes back out with the sandbox's private key,
- it appears in no published byte, decompressed git objects included,
- and neither does this machine's username or hostname.

Everything runs against the installed code and a directory-shaped remote, so
it needs no network and touches nothing of the real installation. A FAIL here
is a defect in woswoar itself, not in the user's setup.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import crypto, deps, store, sync
from .entry import Entry, format_line
from .errors import WoswoarError

#: Print one pass/fail line, in whatever shape the caller renders checks.
Check = Callable[[str, bool, str], None]
#: Print one line of context that cannot fail.
Info = Callable[[str, str], None]

#: Everything that decides where woswoar -- and git -- read and write. All
#: redirected into the sandbox: HOME because git reads ``~/.gitconfig``, whose
#: hooks and filters must neither run against the sandbox nor colour the proof.
_ENV_KEYS = (
    "HOME",
    "WOSWOAR_DIR",
    "WOSWOAR_SESSION",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
)

#: git's background `gc --auto` detaches and keeps repacking after the command
#: that triggered it returns -- so it would still be rewriting the origin while
#: the scan below reads it and the cleanup deletes it. Same setting, same
#: reason, as the sync test suite.
_QUIET_MAINTENANCE = "[gc]\n\tauto = 0\n\tautoDetach = false\n[receive]\n\tautogc = false\n"

#: Strings woswoar itself writes into every repository. A username or hostname
#: that happens to be one of them cannot be told from this boilerplate by
#: searching bytes, so it is reported as not searchable rather than searched
#: and wrongly failed. Two real shapes, met on the first two machines this ran
#: on: a machine named "localhost" collides with the commit email, and a user
#: sharing a name with the maintainer collides with the repository URL in the
#: committed README.
_BOILERPLATE = (
    sync.COMMIT_NAME,
    sync.COMMIT_EMAIL,
    sync.COMMIT_MESSAGE,
    store.README_CONTENT,
    "main",
    "master",
    "origin",
)

#: Below this, a hit in ciphertext by chance is likelier than a real leak: an
#: age recipient line is ~60 characters of lowercase bech32, so a three-letter
#: name has fair odds of appearing in one honestly.
_MIN_TOKEN = 4


@contextmanager
def _sandbox() -> Iterator[Path]:
    """A directory that is, for the duration, the whole world.

    Restoring the environment in ``finally`` is what makes this safe to run
    from inside a live installation: every store path is resolved from the
    environment on each call, so nothing read or written in here can land in
    the real one.
    """
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    tmp = tempfile.mkdtemp(prefix="woswoar-prove-")
    try:
        root = Path(tmp)
        for name in ("data", "conf", "cache", "home"):
            (root / name).mkdir()
        (root / "home" / ".gitconfig").write_text(_QUIET_MAINTENANCE, encoding="utf-8")
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(
            {
                "HOME": str(root / "home"),
                "WOSWOAR_DIR": str(root / "data"),
                "XDG_CONFIG_HOME": str(root / "conf"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_DATA_HOME": str(root / "data"),
            }
        )
        yield root
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


def _git_bytes(*args: str) -> bytes:
    """git output as raw bytes; `sync.git` decodes, and objects are not text."""
    done = subprocess.run(["git", *args], capture_output=True, check=False, timeout=60)
    if done.returncode != 0:
        raise WoswoarError(
            f"git {args[0]} failed in the sandbox:\n{done.stderr.decode('utf-8', 'replace')}"
        )
    return done.stdout


def _published_bytes(origin: Path) -> list[tuple[str, bytes]]:
    """Every byte the sandbox pushed, by name, decompressed where git compresses.

    Two passes over the same repository, because each is blind where the other
    sees. The raw files cover refs, config and packed-refs -- but a leak inside
    a *committed* file lives zlib-compressed in ``objects/``, where no byte
    scan can recognise it. ``cat-file --batch-all-objects`` prints every
    object inflated: blobs, and also the trees and commits that carry paths,
    directory names and author lines.
    """
    streams = [
        (str(path.relative_to(origin)), path.read_bytes())
        for path in sorted(origin.rglob("*"))
        if path.is_file()
    ]
    inflated = _git_bytes("-C", str(origin), "cat-file", "--batch-all-objects", "--batch")
    streams.append(("every git object, decompressed", inflated))
    return streams


def _pushed_plaintext(origin: Path, known: store.Machine, day: str) -> bytes:
    """What ``day``'s chunks on the remote decrypt to, using the sandbox's key.

    Read from the origin's objects, not from the local checkout: the claim
    being proven is about what *left* the machine, so that is what is opened.
    """
    tip = _git_bytes("-C", str(origin), "rev-list", "--all", "-n", "1").decode().strip()
    if not tip:
        raise WoswoarError("the sandbox remote holds no commit")
    history = store.history_dir()
    sealed_rel = store.day_key(known.id, day).relative_to(history)
    sealed = _git_bytes("-C", str(origin), "cat-file", "blob", f"{tip}:{sealed_rel}")
    day_secret = crypto.decrypt_with_file(sealed, sync.identity_path(known)).decode("utf-8")

    chunk_rel = store.chunk_dir(known.id, day).relative_to(history)
    listing = _git_bytes(
        "-C", str(origin), "ls-tree", "-r", "--name-only", tip, str(chunk_rel)
    ).decode("utf-8")
    plaintext = b""
    for name in listing.splitlines():
        blob = _git_bytes("-C", str(origin), "cat-file", "blob", f"{tip}:{name}")
        plaintext += sync.unpack(crypto.decrypt_with_secret(blob, day_secret))
    return plaintext


def _name_tokens(name: str) -> tuple[list[str], list[str]]:
    """Split ``user@host`` into what can be searched for and what cannot.

    The full name is always searchable -- ``@`` keeps it out of any encoding a
    repository legitimately contains. The halves are searched one by one so a
    leak of either alone is still caught, except where a byte search cannot
    mean anything: a token short enough to appear in ciphertext by chance, or
    one that woswoar's own boilerplate already contains everywhere.
    """
    user, _, host = name.partition("@")
    searchable, unsearchable = [name], []
    for token in (user, host):
        if len(token) < _MIN_TOKEN:
            unsearchable.append(f"{token!r} (short enough to appear in ciphertext by chance)")
        elif any(token in text for text in _BOILERPLATE):
            unsearchable.append(f"{token!r} (woswoar's own boilerplate happens to contain it)")
        else:
            searchable.append(token)
    return searchable, unsearchable


def run(check: Check, info: Info) -> None:
    """Record a canary in a sandbox, publish it, and prove what the remote saw.

    The checks print in the order the bytes travel: plaintext log, sealed
    chunk on the remote, back to plaintext under the key, and nowhere else.
    """
    crypto.require()
    crypto.require_signing()
    if shutil.which("git") is None:
        raise WoswoarError(f"git is required - {deps.advice([deps.GIT])}")

    # Long enough that finding it anywhere is a fact, not a coincidence.
    marker = f"woswoar-canary-{secrets.token_hex(12)}"

    with _sandbox() as root:
        origin = root / "origin.git"
        _git_bytes("init", "--quiet", "--bare", str(origin))
        info("sandbox", f"{root} - a throwaway install; your real history is not touched")

        sync.initialise(remote=str(origin), new_identity=True)
        known = store.machine()

        now = int(time.time())
        day = store.day_for(now)
        entry = Entry(
            ts=now, host=known.id, session="prove", cwd="~", exit_code=0, duration_ms=1,
            cmd=f"echo {marker}",
        )
        with store.private_append(store.log_file(known.id, day)) as handle:
            handle.write(format_line(entry) + "\n")

        logged = store.log_file(known.id, day).read_bytes()
        check(
            "recorded",
            marker.encode("utf-8") in logged,
            f"the canary '{marker}' sits in the sandbox's plaintext log,"
            " exactly as logs/ holds your own",
        )

        report = sync.run()
        check(
            "published",
            report.lines_exported >= 1 and report.pushed,
            f"{report.lines_exported} line(s) sealed into {report.chunks_written} chunk(s)"
            " and pushed to the sandbox's remote",
        )

        # Presence first: an absence proof over a repo that never held the
        # canary would pass while proving nothing.
        try:
            plaintext = _pushed_plaintext(origin, known, day)
            opened = marker.encode("utf-8") in plaintext
            detail = (
                "the pushed chunk decrypts back to the canary -- with the sandbox's"
                " private key, which never left this machine"
            )
        except (WoswoarError, OSError, subprocess.SubprocessError, ValueError) as exc:
            opened, detail = False, f"could not open what was published: {exc}"
        check("sealed", opened, detail)

        streams = _published_bytes(origin)
        total = sum(len(data) for _, data in streams)
        hits = [where for where, data in streams if marker.encode("utf-8") in data]
        check(
            "unreadable",
            not hits,
            f"the canary appears in none of the {total:,} bytes on the remote"
            if not hits
            else f"the canary is readable in: {', '.join(hits)}",
        )

        searchable, unsearchable = _name_tokens(known.name)
        named = sorted(
            {
                token
                for token in searchable
                for where, data in streams
                if token.encode("utf-8") in data
            }
        )
        check(
            "anonymous",
            not named,
            f"neither is any of: {', '.join(repr(t) for t in searchable)}"
            if not named
            else f"readable on the remote: {', '.join(repr(t) for t in named)}",
        )
        for token in unsearchable:
            info("", f"{token} was not searched for; the full name above still was")
