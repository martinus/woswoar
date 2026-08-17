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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import archive, crypto, deps, store, sync
from .entry import Entry
from .errors import WoswoarError
from .report import Check

#: `store.ENV_KEYS` is everything woswoar itself resolves paths from; HOME is
#: for git, which reads ``~/.gitconfig``, whose hooks and filters must neither
#: run against the sandbox nor colour the proof.
_ENV_KEYS = (*store.ENV_KEYS, "HOME")

#: git's background `gc --auto` detaches and keeps repacking after the command
#: that triggered it returns -- so it would still be rewriting a short-lived
#: repository while a scan reads it and the cleanup deletes it. The sync test
#: suite met that as five separate red mains in three disguises before pinning
#: it down (see tests/test_sync.py, which imports this); here it would surface
#: as a randomly failed proof.
QUIET_MAINTENANCE = "[gc]\n\tauto = 0\n\tautoDetach = false\n[receive]\n\tautogc = false\n"

#: Strings woswoar itself writes into every repository -- commit boilerplate,
#: the committed files, and every path component a tree object carries. A
#: username or hostname that happens to be one of them cannot be told from
#: this boilerplate by searching bytes, so it is reported as not searchable
#: rather than searched and wrongly failed. Two real shapes, met on the first
#: two machines this ran on: a machine named "localhost" collides with the
#: commit email, and a user sharing a name with the maintainer collides with
#: the repository URL in the committed README.
_BOILERPLATE = (
    sync.COMMIT_NAME,
    sync.COMMIT_EMAIL,
    sync.COMMIT_MESSAGE,
    archive.README_CONTENT,
    archive.GITATTRIBUTES_CONTENT,
    archive.GITIGNORE_CONTENT,
    archive.README,
    archive.GITATTRIBUTES,
    archive.GITIGNORE,
    archive.RECIPIENTS,
    # The repo-layout names, from the module that owns them: tree objects
    # spell out every directory and file name under `hosts/`.
    archive.HOSTS,
    archive.KEYS,
    archive.MANIFESTS,
    archive.signer_public("x").name,
    archive.name_seal("x").name,
    # git's own vocabulary in refs and config.
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
    the real one. Only ``home`` is created up front -- the write below does
    not make parents -- everything else woswoar creates itself, owner-only,
    exactly as it would on a real machine.
    """
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    tmp = tempfile.mkdtemp(prefix="woswoar-prove-")
    try:
        # Resolved, which is nothing on Linux and load-bearing on macOS: there
        # `$TMPDIR` is `/var/folders/...` and `/var` is a symlink to
        # `/private/var`. `_published_bytes` prunes git's object store by asking
        # git for its own directory and comparing that against paths from
        # `rglob` -- and git answers with the *real* path, so under two spellings
        # of one directory the prune silently matches nothing: every loose object
        # is then read raw and inflated, and the byte count double-counts. The
        # proof still passes, which is what makes it worth resolving here rather
        # than noticing later.
        root = Path(tmp).resolve()
        (root / "home").mkdir()
        (root / "home" / ".gitconfig").write_text(QUIET_MAINTENANCE, encoding="utf-8")
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


def _git_bytes(*args: str, cwd: Path | None = None) -> bytes:
    """git output as raw bytes; `sync.git` decodes, and objects are not text."""
    done = subprocess.run(["git", *args], capture_output=True, check=False, timeout=60, cwd=cwd)
    if done.returncode != 0:
        raise WoswoarError(
            f"git {args[0]} failed in the sandbox:\n{done.stderr.decode('utf-8', 'replace')}"
        )
    return done.stdout


def _published_bytes(repo: Path) -> list[tuple[str, bytes]]:
    """Every byte ``repo`` holds, by name, decompressed where git compresses.

    Two passes, because each is blind where the other sees. The raw files
    cover refs, config, packed-refs and any working tree -- but a committed
    byte lives zlib-compressed under ``objects/``, where no byte scan can
    recognise it, so that directory is skipped raw and read through
    ``cat-file --batch-all-objects`` instead, which prints every object
    inflated: blobs, and also the trees and commits that carry paths,
    directory names and author lines.
    """
    git_dir = Path(_git_bytes("rev-parse", "--absolute-git-dir", cwd=repo).decode().strip())
    objects = git_dir / "objects"
    streams = [
        (str(path.relative_to(repo)), path.read_bytes())
        for path in sorted(repo.rglob("*"))
        if path.is_file() and objects not in path.parents
    ]
    inflated = _git_bytes("cat-file", "--batch-all-objects", "--batch", cwd=repo)
    streams.append(("every git object, decompressed", inflated))
    return streams


def _find(streams: list[tuple[str, bytes]], text: str) -> list[str]:
    """The stream names in which ``text``'s bytes appear."""
    needle = text.encode("utf-8")
    return [where for where, data in streams if needle in data]


def _pushed_plaintext(origin: Path, known: store.Machine, day: str) -> bytes:
    """What ``day``'s chunks on the remote decrypt to, using the sandbox's key.

    Read from the origin's objects, not from the local checkout: the claim
    being proven is about what *left* the machine, so that is what is opened.
    """
    tip = _git_bytes("rev-list", "--all", "-n", "1", cwd=origin).decode().strip()
    if not tip:
        raise WoswoarError("the sandbox remote holds no commit")
    history = store.history_dir()
    sealed_rel = archive.day_key(known.id, day).relative_to(history)
    sealed = _git_bytes("cat-file", "blob", f"{tip}:{sealed_rel}", cwd=origin)
    day_secret = sync.open_day_key_bytes(known, sealed)

    chunk_rel = archive.chunk_dir(known.id, day).relative_to(history)
    listing = _git_bytes("ls-tree", "-r", "--name-only", tip, str(chunk_rel), cwd=origin).decode(
        "utf-8"
    )
    pieces = []
    for name in listing.splitlines():
        blob = _git_bytes("cat-file", "blob", f"{tip}:{name}", cwd=origin)
        pieces.append(sync.unpack(crypto.decrypt_with_secret(blob, day_secret)))
    return b"".join(pieces)


def _name_tokens(name: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split ``user@host`` into what can be searched for and what cannot.

    The full name is always searchable -- ``@`` keeps it out of any encoding a
    repository legitimately contains. The halves are searched one by one so a
    leak of either alone is still caught, except where a byte search cannot
    mean anything; those come back as ``(token, why not)`` pairs, said to the
    user rather than silently narrowing the claim.
    """
    user, _, host = name.partition("@")
    searchable, unsearchable = [name], []
    for token in (user, host):
        if len(token) < _MIN_TOKEN:
            unsearchable.append((token, "short enough to appear in ciphertext by chance"))
        elif any(token in text for text in _BOILERPLATE):
            unsearchable.append((token, "woswoar's own boilerplate happens to contain it"))
        else:
            searchable.append(token)
    return searchable, unsearchable


def run() -> Iterator[Check]:
    """Record a canary in a sandbox, publish it, and prove what the remote saw.

    The checks come back in the order the bytes travel: plaintext log, sealed
    chunk on the remote, back to plaintext under the key, and nowhere else.

    Returned rather than printed. `doctor` renders them like every other check,
    and a test can assert that the canary was unreadable without reading a
    screen -- which for the one command whose whole job is to demonstrate a
    security property is the difference between a test and a screenshot.

    A generator, not a list. Everything between the sandbox line and the last
    check is subprocess work -- `git init`, an identity, a full sync, a decrypt,
    a byte scan of the remote -- and `docs/verify.md` shows the transcript
    starting with the sandbox path immediately. Accumulating first would leave
    somebody watching an empty terminal for the whole of it.
    """
    crypto.require()
    crypto.require_signing()
    if not deps.GIT.present:
        raise WoswoarError(deps.report([deps.GIT]))

    # Long enough that finding it anywhere is a fact, not a coincidence.
    marker = f"woswoar-canary-{secrets.token_hex(12)}"

    with _sandbox() as root:
        origin = root / "origin.git"
        # `--template=` keeps git from copying its ~27 KB of sample hooks into
        # the "remote": they would dominate the byte count reported below, and
        # none of them was published by anything.
        _git_bytes("init", "--quiet", "--bare", "--template=", str(origin))
        # Belt and braces beside the sandbox HOME: a push's `receive-pack`
        # runs on the origin side, and the origin's own config governs it
        # wherever it inherits its environment from.
        with (origin / "config").open("a", encoding="utf-8") as handle:
            handle.write(QUIET_MAINTENANCE)
        yield Check(
            "sandbox",
            f"{root} - a throwaway install; your real history is not touched",
            ok=None,
        )

        sync.initialise(remote=str(origin), new_identity=True)
        known = store.machine()

        now = int(time.time())
        day = store.day_for(now)
        entry = Entry(
            ts=now,
            host=known.id,
            session="prove",
            cwd="~",
            exit_code=0,
            duration_ms=1,
            cmd=f"echo {marker}",
        )
        store.append_entries(known.id, [entry])

        logged = store.log_file(known.id, day).read_bytes()
        yield Check(
            "recorded",
            f"the canary '{marker}' sits in the sandbox's plaintext log,"
            " exactly as logs/ holds your own",
            ok=marker.encode("utf-8") in logged,
        )

        report = sync.run()
        yield Check(
            "published",
            f"{report.lines_exported} line(s) sealed into {report.chunks_written} chunk(s)"
            " and pushed to the sandbox's remote",
            ok=report.lines_exported >= 1 and report.pushed,
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
        yield Check("sealed", detail, ok=opened)

        streams = _published_bytes(origin)
        total = sum(len(data) for _, data in streams)
        hits = _find(streams, marker)
        yield Check(
            "unreadable",
            f"the canary appears in none of the {total:,} bytes on the remote"
            if not hits
            else f"the canary is readable in: {', '.join(hits)}",
            ok=not hits,
        )

        searchable, unsearchable = _name_tokens(known.name)
        named = [token for token in searchable if _find(streams, token)]
        yield Check(
            "anonymous",
            f"neither is any of: {', '.join(repr(t) for t in searchable)}"
            if not named
            else f"readable on the remote: {', '.join(repr(t) for t in named)}",
            ok=not named,
        )
        for token, why in unsearchable:
            yield Check(
                "",
                f"{token!r} was not searched for ({why}); the full name above still was",
                ok=None,
            )
