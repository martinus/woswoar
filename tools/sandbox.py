"""What a tool comparing two revisions has to get right before it measures.

`compare` and `bench` both need a checkout of some revision, a throwaway ``$HOME``
that woswoar will treat as a fresh machine, and a count of the CPUs this process
may actually use. Both grew their own copy of each in one commit, and the copies
had already diverged in the way that matters:

- `bench` built its environment as ``{**os.environ, ...}`` and overrode four
  keys. `store.ENV_KEYS` has six. ``WOSWOAR_DIR`` beats ``XDG_DATA_HOME`` in
  `store.data_dir`, and ``WOSWOAR_SESSION`` is exported by the installed hook in
  every interactive shell -- so a benchmark run from a normal terminal measured
  the developer's real history in both trees instead of the empty sandbox. That
  is exactly what `store.ENV_KEYS`' own comment was written for: "a variable
  added here but missed in a hand-kept copy would silently point a 'sandbox' at
  the real installation, with nothing failing."
- `bench` placed its worktree beside the repository and `compare` left it in
  ``/tmp``. Only one of those is deliberate, and the difference is invisible.

So the environment here is built from **nothing**, not from `os.environ`. That is
what makes it correct as `store` grows: a key woswoar reads and this does not set
is simply absent, so `store` falls back to its ``$HOME``-relative default, which
is inside the sandbox. Inheriting and overriding is the shape that fails
silently, and it fails towards the real installation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from woswoar import store

#: The identity every sandbox is given, on every side of every comparison.
#: Seeded rather than generated: `install` prints the id it made, so a fresh one
#: per run would differ between two trees for a reason that says nothing about
#: either. Sixteen hex digits because that is the real shape -- a short one would
#: be caught by nothing here and would then not resemble what it stands in for.
MACHINE_ID = "0123456789abcdef"


def seed_machine(home: Path, name: str) -> None:
    """Give ``home`` a machine identity, so nothing has to generate one."""
    config = home / "config" / "woswoar"
    config.mkdir(parents=True, exist_ok=True)
    (config / "machine").write_text(f"id={MACHINE_ID}\nname={name}\n", encoding="utf-8")


def sandbox_env(tree: Path, home: Path) -> dict[str, str]:
    """The whole environment for a woswoar run against ``tree``, inside ``home``.

    Everything the child gets, and nothing else. See the module docstring for why
    this does not start from `os.environ` -- the short version is that the failure
    mode of inheriting is a "sandbox" pointed at the real installation.

    ``PATH`` is the one thing carried across, because the suite and the CLI both
    need to find real `age`, `git` and `bash`, and there is no sandbox-relative
    answer to where those are.
    """
    return {
        # `store.SANDBOX_CARRIES` rather than `PATH` alone, and from there
        # rather than from a list of its own: that constant is where the suite's
        # sandbox keeps the same knowledge, and two lists of carried names is
        # the shape this module's docstring is written against. Concretely, it
        # is what makes CI's `PYTHONWARNINGS=error::DeprecationWarning` reach
        # the `python` children `bench` and `compare` spawn -- carrying only
        # `PATH` left that guard downgraded to a warning in here, silently.
        **{name: os.environ[name] for name in store.SANDBOX_CARRIES if name in os.environ},
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "PYTHONPATH": str(tree),
        # Bytecode goes in the sandbox, so no `__pycache__` in the tree is either
        # read or written. Both halves matter and the first one is subtle: a `.pyc`
        # is validated against `(mtime_seconds, size)`, so an edit that changes a
        # file by no bytes inside one second leaves a *valid-looking* cache of the
        # previous source. That is not hypothetical -- `compare` reported a
        # difference between two identical trees this way, because `logs    :` had
        # been edited to `LOGS    :` and back, and the checkout it was compared
        # against was fresh while the working tree still had the stale cache.
        # `PYTHONDONTWRITEBYTECODE` would not have helped: it stops writing, not
        # reading.
        "PYTHONPYCACHEPREFIX": str(home / "pycache"),
        # A terminal would turn on colour and the progress meter, neither of
        # which is ever the thing being compared, and both of which vary with how
        # the comparison itself was invoked.
        "NO_COLOR": "1",
        "SHELL": "/bin/bash",
    }


@contextmanager
def worktree(revision: str, beside: Path | None = None) -> Iterator[tuple[Path, str]]:
    """A detached checkout of ``revision`` and its short sha, removed afterwards.

    ``beside`` puts it on a named directory's filesystem. `bench` needs that and
    `compare` does not, which is the only difference between the two copies this
    replaces -- so it is a parameter with a default rather than two functions.

    The teardown is both halves on purpose: `git worktree remove` alone can fail
    and leave the directory, and `rmtree` alone leaves a stale entry under
    ``.git/worktrees`` that makes the next ``worktree add`` on the same path fail.
    """
    sha = subprocess.run(
        ["git", "rev-parse", "--short", revision], capture_output=True, text=True, check=True
    ).stdout.strip()
    path = Path(tempfile.mkdtemp(dir=beside, prefix=".woswoar-worktree-"))
    # `mkdtemp` made it and `git worktree add` insists on making it itself.
    path.rmdir()
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(path), revision],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        yield path, sha
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(path, ignore_errors=True)


def usable_cpus() -> int:
    """How many CPUs this process may actually use.

    Not ``os.cpu_count()``, which reports the host's and ignores an affinity mask
    or a container's quota -- so on a two-core-limited runner on a sixteen-core
    host it answers sixteen. `tools/run_tests.py` has said so since it was
    written; this is the second caller, which is why it is here rather than
    spelled again.
    """
    counter = getattr(os, "process_cpu_count", os.cpu_count)  # process_cpu_count is 3.13+
    return counter() or 4


def same_filesystem(one: Path, other: Path) -> bool:
    """Whether two paths sit on the same device.

    A checked precondition rather than a convention. `bench` used to say "put the
    worktree beside the repository" and leave it there, which is a *remedy* for
    comparing tmpfs against btrfs, not the invariant -- and it fails silently
    wherever the parent directory is a different mount, which is any repo at a
    mountpoint and any container bind. Asking the question costs two `stat` calls.
    """
    return one.stat().st_dev == other.stat().st_dev
