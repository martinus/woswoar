"""Every git fork woswoar makes, and the repo facts worth reading only once.

Split out of `sync` because it is the one layer in there that talks to the
network and the only one that spawns a subprocess, and because it was already
isolated behind the `Repo` record -- the rest of sync passes that record down
rather than asking git again.

`git` is deliberately the single seam. Nothing else in the package spawns git
(`prove._git_bytes` is the one exception and says why beside itself), so the
fork-count tests in `tests/test_sync.py` can patch one module attribute and see
every call `sync.run` makes.

That is also why `sync` reaches these as ``gitrepo.read_repo()`` rather than
binding the bare names with a ``from`` import: such an import copies the function
object at import time, so a spy installed on this module would then miss every
call site in `sync` -- silently, as a count that got smaller. It is the *wrappers* that
matter here rather than `git` itself, which `sync` only calls in `initialise`;
`commit`, `read_repo`, `fetch_and_rebase` and `push` are the ones the tests
patch. `tests/test_architecture.py::TestTheSeamsAreReachedByAttribute` holds it,
because two paragraphs of prose held it before and prose does not fail CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple

from . import progress, store
from .errors import SyncError

GIT_TIMEOUT = 300

#: Commits carry no useful information -- the content is opaque and the author
#: is always this machine -- so they are uniform. A varying message would only
#: leak activity patterns into a place that is not encrypted.
COMMIT_MESSAGE = "woswoar sync"

#: Commit metadata is one of the few things in the repo that is *not* encrypted,
#: so it is pinned to a fixed identity rather than inheriting the user's real
#: name and email from their global gitconfig. Set on the repo so that rebase
#: and commit alike pick it up, including on a machine with no gitconfig at all.
COMMIT_NAME = "woswoar"
COMMIT_EMAIL = "woswoar@localhost"


def is_repo() -> bool:
    """Whether there is a history repository here at all.

    The one git *fact*, as against the forks below, and it is here because it is
    the question modules that want nothing else from git ask: `setup` reaches for
    it and for nothing else, so its home decides whether the wizard imports the
    whole sync protocol to answer it.
    """
    return (store.history_dir() / ".git").exists()


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """One git fork, with a timeout that is an error rather than a traceback.

    `crypto._run` and `_run_signer` both convert `TimeoutExpired`; this seam did
    not, and it is the one that talks to the **network** -- so a hang here is
    ordinary rather than exotic. A remote that accepts the connection and then
    stalls (a sleeping NAS, a dead ssh host, an https remote behind a captive
    portal) gives `git fetch` no reason to exit, and after `GIT_TIMEOUT` the
    unattended sync ended in a stack trace nobody reads. `WoswoarError`'s
    docstring is explicit that this is the contract: sync runs from a timer,
    "where nobody ever reads one". See #286.

    **Raised regardless of `check`, which is why it is on the `try` and not
    inside the `if`.** `check=False` means "a non-zero exit is an answer" --
    `read_repo`, `remote_summary` and `resolve` all rely on that -- and a
    timeout is an answer in none of them: `resolve` would hand back empty
    strings and `fetch_and_rebase` would read that as "no upstream" and skip
    the rebase without a word.

    `OSError` is deliberately left alone: git missing from `PATH` is a different
    failure with a different remedy, and `deps` already owns that message.
    """
    repo = cwd or store.history_dir()
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT}s") from exc
    if check and result.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


class Repo(NamedTuple):
    """What a command needs to know about the git repo, read once up front.

    Every field was its own `git` fork before, repeated by each helper that
    wanted it: `user.name` and `user.email` read every run to be written
    approximately never, `git remote`, and `branch --show-current` twice per
    sync. None of it can change under a command -- woswoar never checks a branch
    out, and the identity is written at `init` -- so reading it once and passing
    it down is the whole saving, on a timer that fires every minute.

    `git remote` needs no fork of its own: it prints the names in the
    `remote.<name>.url` keys of the same local config read here.
    """

    has_remote: bool
    name: str
    email: str
    branch: str


def read_repo() -> Repo:
    values: dict[str, str] = {}
    for line in git("config", "--list", "--local", check=False).splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    has_remote = any(key.startswith("remote.") and key.endswith(".url") for key in values)
    return Repo(
        has_remote=has_remote,
        name=values.get("user.name", ""),
        email=values.get("user.email", ""),
        # Only asked for when there is a remote to name it to: every reader of
        # this field is inside an `if has_remote`, and a local-only install
        # would otherwise fork once a minute for a string nothing reads.
        #
        # `branch --show-current` rather than `rev-parse --abbrev-ref HEAD`,
        # which cannot answer on an unborn HEAD -- exactly the state `init` is
        # in when it enrols the first machine.
        branch=git("branch", "--show-current") if has_remote else "",
    )


def has_remote() -> bool:
    """For the callers that want only this. The definition lives in `Repo`."""
    return read_repo().has_remote


def remote_summary() -> str:
    """One line describing where history is published, for humans."""
    remotes = git("remote", "-v", check=False).splitlines()
    return remotes[0] if remotes else "none (history is local only)"


def ensure_config(repo: Repo) -> None:
    """Pin the commit identity, writing only what is actually wrong."""
    if repo.name != COMMIT_NAME:
        git("config", "user.name", COMMIT_NAME)
    if repo.email != COMMIT_EMAIL:
        git("config", "user.email", COMMIT_EMAIL)


#: Object ids as `rev-parse` prints them. Both lengths, because a repo may be
#: SHA-256 rather than SHA-1 -- woswoar never creates one, but it is handed the
#: repo the user cloned.
_HEX = frozenset("0123456789abcdef")


def resolve(*refs: str) -> list[str]:
    """Resolve several refs in one fork; an unresolvable one comes back empty.

    `rev-parse --verify` takes a single ref, so asking about two costs two forks.
    Plain `rev-parse` takes any number, but *echoes* an argument it could not
    resolve instead of failing that line, hence the shape check.
    """
    printed = git("rev-parse", *refs, check=False).splitlines()
    if len(printed) != len(refs):
        return [""] * len(refs)
    return [line if len(line) in (40, 64) and _HEX.issuperset(line) else "" for line in printed]


def fetch_and_rebase(repo: Repo) -> bool:
    """Adopt the remote's history under ours; say whether we now match it.

    Announced rather than counted: this is one `git fetch` over the network, so
    there is nothing to be a fraction of, and a slow one here is a slow remote
    rather than anything woswoar is doing.

    Fetch-then-rebase rather than ``git pull --rebase`` because the tracking ref
    may legitimately not exist: cloning an *empty* remote configures a branch
    pointing at nothing, which is the normal state for the first machine to
    enrol. Checking the fetched ref directly handles that and the ordinary case
    with one code path.

    HEAD is resolved in the same fork, and a HEAD already equal to the fetched
    ref needs no replaying at all -- the shape of every idle run of the
    one-minute timer. Returning that fact lets the caller skip a push that would
    contact the remote to send nothing.

    The rebase cannot conflict over chunks -- every machine only ever adds files
    below its own host id. ``recipients.txt`` is the single shared file, and
    ``.gitattributes`` marks it ``merge=union`` so both sides' keys survive.

    Do not answer the question with ``merge --ff-only`` succeeding. Merging a ref
    that is already an *ancestor* of HEAD succeeds too -- it is a no-op -- so a
    machine holding commits nobody else has would read as level and never
    publish them. Only where HEAD lands says anything.
    """
    progress.phase("fetching from the remote")
    git("fetch", "--quiet", "origin")
    local, upstream = resolve("HEAD", f"refs/remotes/origin/{repo.branch}")
    if not upstream:
        return False
    if local == upstream:
        return True
    try:
        git("rebase", "--autostash", f"origin/{repo.branch}")
    except SyncError:
        git("rebase", "--abort", check=False)
        raise
    # A rebase with nothing of its own to replay lands exactly on the fetched
    # ref: this machine only received. Worth one fork on the path that just
    # rebased -- never on the idle path -- because it saves opening a connection
    # to the remote to send it nothing, on every machine every time any peer
    # records a command.
    return resolve("HEAD")[0] == upstream


def push(repo: Repo) -> None:
    """Publish, retrying once if someone else pushed while we worked."""
    progress.phase("publishing to the remote")
    try:
        git("push", "--quiet", "-u", "origin", repo.branch)
    except SyncError:
        fetch_and_rebase(repo)
        git("push", "--quiet", "-u", "origin", repo.branch)


def commit() -> bool:
    # `--verbose` prints one "add '<path>'"/"remove '<path>'" line per staged
    # path and nothing at all when the index already matches, so the same
    # command that stages also answers "is there anything to commit". The
    # obvious `status --porcelain` afterwards costs a second full stat of a
    # working tree that is tens of thousands of chunks after a couple of years.
    #
    # `run` no longer reaches here on an idle sync -- it asks its writers first
    # -- so this is now paid only by runs that did write, and by `grant`,
    # `revoke` and `init`. That makes it cheaper, not unnecessary: those runs
    # still have a whole tree to stat, and doing it once beats doing it twice.
    if not git("add", "-A", "--verbose"):
        return False
    git("commit", "-q", "-m", COMMIT_MESSAGE)
    return True
