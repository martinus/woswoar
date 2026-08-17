"""The layout of the history repository: ciphertext only, every file write-once.

Split from :mod:`woswoar.store`, which now owns ``logs/`` and the filesystem
primitives. The two were one module, so everything that wanted ``logs_dir()``
also depended on the chunk layout, the manifest paths and the repo format
marker -- `cache` and `search` among them, neither of which has any business
knowing what a chunk directory is called.

The boundary is deliberate and narrow: `store.history_dir` stays where it is,
because `store._private_paths` has to *prune* that directory by name -- see that
docstring for the measurement. So `store` knows the directory exists; this
module knows what is inside it.

What belongs here is the repository's **layout and its self-description**: the
names, the paths, the format marker, and the boilerplate that says which shape a
directory is. What does not belong here is the *history* -- a chunk, a manifest
and a seal are content, and reading one is `sync`'s. That line is worth being
exact about, because it is the only rule a later change has for deciding where a
new function goes: `repo_format` reads a file and is still layout, since what it
reads is the statement of the shape rather than anything recorded in it.

The point of gathering the paths at all is that a hand-typed one cannot drift
out of step with the thing that decides the shape -- the argument `iter_day_keys`
and `day_of_key` below already make for their own suffixes.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from . import store

# The three directory names a repo path is built from. Public because
# `prove._BOILERPLATE` asserts on them: it proves no layout name reaches the
# remote in the clear, and the names have to come from whoever owns the layout
# rather than a second copy hand-typed in the test. The suffixes below are
# private because `prove` reaches them through `signer_public("x").name`.
#
# `HOSTS` is spelled here and again in `store`, and that is deliberate rather
# than an oversight. `logs/hosts/` and `history/hosts/` agree today, but they
# are two formats: `REPO_FORMAT` versions this one alone, so a future layout
# that renamed the repo's host directory would have to be expressible without
# also renaming the plaintext tree -- which one shared constant cannot say.
HOSTS = "hosts"
KEYS = "keys"
MANIFESTS = "manifests"
_CHUNK_SUFFIX = ".age"
_PUB_SUFFIX = ".pub"
RECIPIENTS = "recipients.txt"
GITATTRIBUTES = ".gitattributes"
GITIGNORE = ".gitignore"
README = "README.md"
FORMAT = "FORMAT"
#: The layout of the *repository* -- `recipients.txt`, `hosts/<id>/keys/`,
#: `manifests/`, `<day>/<ts>-<rand>.age`. Every other format woswoar owns already
#: says which one it is: the cache carries `woswoar-cache-2`, a manifest carries
#: `woswoar-manifest-v1` inside the bytes it signs. The repo was the exception,
#: and it is the one an upgrade cannot be retrofitted onto later -- a machine
#: still running the old version would not write the marker, so by the time a
#: layout change needs one it is too late by definition.
#:
#: Deliberately not `woswoar.__version__`. The program version moved sixteen
#: times in a fortnight and this has moved zero times; coupling them means a
#: meaningless bump on every release and no signal on the one release where it
#: matters.
REPO_FORMAT = 1
_FORMAT_PREFIX = "woswoar-repo-"


def format_content(version: int = REPO_FORMAT) -> str:
    """What the marker says for ``version``. The one place the prefix is spelt.

    Takes a version because the tests need to write one this build does not
    understand, and hand-typing `woswoar-repo-2` there would put a second copy
    of the prefix outside this module -- where it would go on parsing after a
    rename, as `None`, and quietly stop testing the refusal.
    """
    return f"{_FORMAT_PREFIX}{version}\n"


FORMAT_CONTENT = format_content()
#: What a stranger finding the repository sees first, and what its owner sees in
#: three years having forgotten what it was. Deliberately says nothing about
#: whose it is: the host directories are opaque hex precisely so the archive does
#: not name machines, and a README that undid that would be worse than none.
#:
#: Written only when absent, unlike `.gitattributes`, which is compared and
#: rewritten to match. Both are written by `init` rather than by `sync`, so the
#: cost of getting this wrong is bounded -- but a machine on a newer version
#: would still rewrite a peer's wording the next time anyone ran `init`, and the
#: older one would put it back. `.gitattributes` earns that because its content
#: is load-bearing: `merge=union` on `recipients.txt` is what makes two machines
#: enrolling at once conflict-free. Prose does not.
README_CONTENT = """# woswoar history

Encrypted shell history, written by [woswoar](https://github.com/martinus/woswoar).

Every `.age` file here is ciphertext. There is nothing readable in this
repository, including the directory names -- they are random hex so that an
archive of it does not publish which machines exist.

## Do not edit this by hand

Chunks are written once and never rewritten, and each machine signs a list of
what it published each day. A file that no signed list accounts for is refused
by every other machine and reported. Editing or deleting one here does not
remove history -- other machines keep their own copies -- and will make them
report the repository as tampered with.

To take a machine's access away, run `woswoar revoke` from another one. To see
what state this repository is in, run `woswoar doctor`.
"""

#: `recipients.txt` is the one file every machine appends to, so it is also the
#: only place a merge conflict could arise. A union merge resolves it: the file
#: is an unordered set of public keys, and keeping both sides is always right.
GITATTRIBUTES_CONTENT = f"{RECIPIENTS} merge=union\n*{_CHUNK_SUFFIX} -diff -merge -text\n"
#: What must never be committed, whoever is browsing the checkout. Finder writes
#: a `.DS_Store` into any directory it is asked to display, and this is a git
#: working tree that lives under the user's home -- so one arrives the first time
#: anybody looks at their history in a file manager.
#:
#: Not a tidiness rule. `sync` stages with `git add -A`, so without this the file
#: is committed and pushed to every peer; two machines whose Finders disagree
#: about it then produce a binary conflict on the rebase that `sync` does, in a
#: file no part of woswoar knows anything about. `.gitattributes` cannot express
#: this -- it marks how a tracked file is treated, not whether it is tracked --
#: so it is a second file.
#:
#: The chunk suffix is deliberately *not* here. A chunk that no signed manifest
#: accounts for is a thing woswoar reports; a chunk git silently ignores is one
#: nobody ever sees.
GITIGNORE_CONTENT = f"{store.FINDER_METADATA}\n"


def recipients_file() -> Path:
    return store.history_dir() / RECIPIENTS


def format_file() -> Path:
    """Which shape this repository is, in plaintext at its root.

    Plaintext, and it has to be: a machine reads this *before* it knows how to
    find the day keys, so a version of it sealed to a key is a fact you can only
    learn by already understanding the layout it describes.

    It says nothing an observer could not get from the directory listing anyway
    -- `docs/security.md` has always put the shape and size of the repository
    outside what encryption hides.
    """
    return store.history_dir() / FORMAT


def repo_format() -> int | None:
    """The version the repository claims, or ``None`` when nothing legible does.

    ``None`` is "the shape that predates the marker", which is this shape -- so
    an absent file reads as compatible and every existing installation goes on
    working untouched. That is the whole migration.

    Unreadable *content* answers ``None`` too, rather than refusing. The file
    sits in a repository anyone with push access can write, so treating garbage
    as a fatal disagreement would hand any of them a way to stop every machine
    syncing by writing four bytes -- while a legible number is a statement made
    deliberately by a newer woswoar, and is worth stopping for. `State.load`
    degrades the same way, for the same reason.
    """
    try:
        text = format_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith(_FORMAT_PREFIX):
        return None
    try:
        return int(text[len(_FORMAT_PREFIX) :])
    except ValueError:
        # `int` in a `try`, and not `str.isdigit` before it: those are different
        # questions, and the gap between them is this function's whole promise.
        # `"²".isdigit()` is True and `int("²")` raises, so a marker holding
        # `woswoar-repo-²` crashed every sync with an unhandled ValueError --
        # four bytes, writable by anyone with push access, exactly the wedge the
        # docstring above says cannot happen.
        return None


def repo_host_dir(machine_id: str) -> Path:
    return store.history_dir() / HOSTS / machine_id


def name_seal(machine_id: str) -> Path:
    """Sealed friendly name, so other machines can label this host's entries."""
    return repo_host_dir(machine_id) / "name.age"


def signer_public(machine_id: str) -> Path:
    """A host's published verify key, and the recipient that owns the host.

    Two lines, and the second is why this file exists at all rather than the
    verify key living in `recipients.txt`: revocation names an *age recipient*,
    while chunks live under a *host id*, and nothing else in the repo joins the
    two. It could not go in `recipients.txt` either -- an age recipient may
    itself be an SSH key, `ssh-ed25519 AAAA... comment`, so a second key in that
    field could not be told from the first one's comment.

    Anyone who can push can rewrite this, so nothing here is believed. It is
    what `trust` *shows* a human, once; what is believed afterwards is the copy
    pinned in local state.
    """
    return repo_host_dir(machine_id) / "signer.pub"


def day_manifest(machine_id: str, day: str) -> Path:
    """The signed list of a host-day's chunks.

    Deliberately not one signature per chunk, which is what made the first
    attempt at this unusable: a signature costs a subprocess, and a machine
    waiting for `grant` re-checks everything it has not merged on every timer
    tick. One manifest per host-day turns that from once per chunk into once per
    day -- about 3.6 s over a year of three machines, against nearly two minutes.

    Rewritten as the day goes on, so it lives outside the `hosts/<id>/<day>/`
    chunk directories that `is_chunk_path` marks write-once. Only the owning
    host ever writes it, so it cannot conflict, and it needs no merge driver.
    """
    return repo_host_dir(machine_id) / MANIFESTS / day


def day_key(machine_id: str, day: str) -> Path:
    """The day's identity, sealed to every recipient."""
    return repo_host_dir(machine_id) / KEYS / f"{day}{_CHUNK_SUFFIX}"


def day_key_public(machine_id: str, day: str) -> Path:
    """The day's public key, in the clear.

    Public keys are not secret, and keeping this alongside means writing a chunk
    never has to open the sealed key first.
    """
    return repo_host_dir(machine_id) / KEYS / f"{day}{_PUB_SUFFIX}"


def chunk_dir(machine_id: str, day: str) -> Path:
    """``hosts/<id>/2026-07-29`` -- one directory per day.

    Sharding by day keeps any directory to a day's worth of chunks, but the
    date is *one* path component rather than three. Every commit rewrites the
    tree object for each level it touches, so nesting ``2026/07/29`` costs two
    extra tree objects on every single sync forever, for a directory that holds
    exactly as many entries either way. Magnitudes are in
    docs/woswoar_design_summary.md, which is re-measured as a whole; repeating
    them here is how they go stale.
    """
    return repo_host_dir(machine_id) / day


def new_chunk(machine_id: str, day: str, ts: int) -> Path:
    """A chunk path that has never existed before.

    Zero-padded seconds sort chronologically as strings, so a day's chunks are
    walked oldest-first. That ordering is a convenience, not a guarantee: two
    chunks written in the same second differ only by the random suffix, which is
    why what a machine has merged is recorded as a *set* of names rather than a
    high-water mark.

    Uniqueness is a guarantee, not a probability. A collision would silently
    overwrite an already-sealed chunk, destroying committed history in a design
    whose whole premise is that chunks are written once and never modified --
    entropy alone left that to chance, which
    tests/test_sync.py::TestChunkNaming now pins. Checking the filesystem is
    sound because both writers of a host's chunks, `sync.run` and
    `sync.compact`, hold the same lock.
    """
    directory = chunk_dir(machine_id, day)
    while True:
        path = directory / f"{ts:010d}-{secrets.token_hex(3)}{_CHUNK_SUFFIX}"
        if not path.exists():
            return path


class Chunk(NamedTuple):
    day: str
    #: Filename only. Sorts chronologically, and is what `State.merged` records.
    name: str
    path: Path


def chunk_names(machine_id: str, day: str) -> list[str]:
    """The chunk filenames in one host-day, sorted. Empty if there are none.

    The unit the layout is actually asked about: what counts as a chunk is
    decided here, so a partial write or an editor's leftover in a day directory
    is not one. Callers that want a single day must not reach it by walking the
    host -- `has_chunks` did, and turned a 0.09 ms question into a 4.6 ms one on
    an archive of 20k chunks, once per orphaned day.

    Listing is what walking an archive costs now that nothing builds a `Path`
    per file: 4.96 ms for one peer's 20k chunks, against 0.18 ms to stat its 40
    day directories, which is why `day_stamp` exists.

    `os.scandir` swallows the same errors `Path.glob` did: a day directory that
    has gone away, or that cannot be read, has no chunks to offer.
    """
    try:
        with os.scandir(chunk_dir(machine_id, day)) as entries:
            return sorted(e.name for e in entries if e.is_file() and e.name.endswith(_CHUNK_SUFFIX))
    except OSError:
        return []


def chunk_days(machine_id: str) -> list[str]:
    """The day directories one host publishes into, in order.

    One `scandir` of the host, and no listing of the days themselves -- which is
    what lets a caller decide *per day* whether it needs the names at all.
    """
    try:
        with os.scandir(repo_host_dir(machine_id)) as entries:
            return sorted(e.name for e in entries if e.is_dir() and _is_day(e.name))
    except OSError:
        return []


def day_stamp(machine_id: str, day: str) -> int | None:
    """When this day directory last gained or lost a file. ``None`` if absent.

    A directory's mtime moves when an entry is added to or removed from it,
    which is exactly what a fetch bringing in a chunk does -- so a day whose
    stamp is unchanged since it was last merged has nothing new in it, without
    listing it -- see `chunk_names` for what that saves.

    Nanoseconds, and compared for equality rather than order: a directory
    restored from a backup can be older than the stamp that was recorded, and
    "older" still means "different, look again".
    """
    try:
        return os.stat(chunk_dir(machine_id, day)).st_mtime_ns
    except OSError:
        return None


def iter_chunk_days(machine_id: str) -> Iterator[tuple[str, list[str]]]:
    """``(day, chunk filenames)`` for one host: days in order, names sorted.

    Each day appears exactly once, with all of its chunks, and days with none at
    all are not days. `sync._merge_host` reaches the same two halves separately
    -- `chunk_days` then, per day it has to look at, `chunk_names` -- because it
    can decide from the day alone that it needs no names.
    """
    for day in chunk_days(machine_id):
        # A day directory with nothing in it is not a day with no new chunks --
        # it is not a day at all, and saying so keeps every caller from having
        # to ask.
        if names := chunk_names(machine_id, day):
            yield day, names


def iter_chunks(machine_id: str) -> Iterator[Chunk]:
    """Every sealed chunk belonging to one host, oldest first.

    The flat view of `iter_chunk_days`, for the callers that want the paths.
    """
    root = repo_host_dir(machine_id)
    for day, names in iter_chunk_days(machine_id):
        for name in names:
            yield Chunk(day=day, name=name, path=root / day / name)


def _is_day(name: str) -> bool:
    """``2026-07-29``. Distinguishes day directories from ``keys``."""
    parts = name.split("-")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


def iter_day_keys(machine_id: str) -> Iterator[Path]:
    """Every sealed day key belonging to one host.

    Exists so callers never have to spell out the keys directory or the chunk
    suffix themselves -- those are this module's to know, and a hand-typed copy
    would silently stop matching rather than fail loudly if the layout changed.
    """
    keys = repo_host_dir(machine_id) / KEYS
    if not keys.is_dir():
        return
    yield from sorted(keys.glob(f"*{_CHUNK_SUFFIX}"))


def day_key_days(machine_id: str) -> tuple[set[str], set[str]]:
    """``(sealed, public)`` day strings for one host, from a single listing.

    One readdir rather than a `glob` plus a stat per key: the caller asks this
    for every host in the repo, so the cost grows with the archive forever --
    measured on a two-year, five-machine tree, 1.9ms against 36ms.

    Both sets from one pass because the only question worth asking is how they
    differ, and pulling them apart is what let two callers drift over whether a
    missing sealed half mattered.
    """
    sealed: set[str] = set()
    public: set[str] = set()
    try:
        names = os.listdir(repo_host_dir(machine_id) / KEYS)
    except OSError:
        return sealed, public
    for name in names:
        if name.endswith(_CHUNK_SUFFIX):
            sealed.add(name.removesuffix(_CHUNK_SUFFIX))
        elif name.endswith(_PUB_SUFFIX):
            public.add(name.removesuffix(_PUB_SUFFIX))
    return sealed, public


def is_chunk_path(relpath: str) -> bool:
    """Whether a repo-relative path is a write-once chunk.

    Key material and name seals live outside this shape and are deliberately
    rewritable, so telling them apart is a property of the layout rather than
    something a caller should pattern-match for itself.
    """
    parts = relpath.split("/")
    return (
        len(parts) == 4
        and parts[0] == HOSTS
        and _is_day(parts[2])
        and parts[3].endswith(_CHUNK_SUFFIX)
    )


def repo_hosts() -> list[str]:
    """Machine ids present in the history repo."""
    root = store.history_dir() / HOSTS
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def day_of_key(path: Path) -> str:
    """``hosts/<id>/keys/2026-07-29.age`` -> ``2026-07-29``.

    The inverse of `day_key`, and here rather than at the caller for the same
    reason `iter_day_keys` is: the suffix is this module's to know.
    """
    return path.name.removesuffix(_CHUNK_SUFFIX)
