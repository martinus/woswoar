"""Shared test scaffolding."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import NamedTuple

from woswoar import crypto, store
from woswoar.__main__ import main
from woswoar.entry import Entry

MACHINE_ID = "0123456789abcdef"

#: External tools some suites need. Defined once so that a check which has to
#: grow later -- an age version floor, say -- cannot be updated in one file and
#: forgotten in the other.
requires_age = unittest.skipUnless(crypto.available(), "age required")
requires_git = unittest.skipUnless(shutil.which("git"), "git required")
requires_ssh_keygen = unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen required")
requires_bash = unittest.skipUnless(shutil.which("bash"), "bash required")
requires_fzf = unittest.skipUnless(shutil.which("fzf"), "fzf required")


class Ran(NamedTuple):
    """What one CLI invocation did."""

    code: int
    out: str
    err: str


def run_cli(*argv: str) -> Ran:
    """Drive the real CLI in process, capturing both streams.

    Both, because several commands put the thing a test cares about on stderr --
    `sync`'s warning that a day could not be authenticated, `grant`'s refusal to
    act without a terminal. Four modules used to spell this out separately and
    every one of them captured stdout alone, so a test meaning "it warned" could
    only check the exit code, and would have passed had the warning been
    deleted. That is the failure mode `CLAUDE.md` rule 3 is about, and it was
    invisible at each individual call site.
    """
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return Ran(code, out.getvalue(), err.getvalue())


def loose_paths() -> list[str]:
    """Paths another account could read, walked independently of the code.

    Deliberately not `store.readable_by_others()`: asking the helper about
    itself could not catch it forgetting a path, which is the failure this
    guards against. `history/` is excluded -- it is ciphertext arriving from
    `git clone`, and the directory above it is owner-only.
    """
    roots = [store.data_dir(), store.config_dir(), store.cache_dir()]
    seen = [r for r in roots if r.is_dir()]
    seen += [p for r in list(seen) for p in r.rglob("*")]
    return sorted(
        f"{p} is {oct(p.stat().st_mode & 0o777)}"
        for p in seen
        if p.stat().st_mode & 0o077 and store.history_dir() not in [p, *p.parents]
    )


def make_entry(ts: int, cmd: str, host: str = MACHINE_ID, session: str = "s1") -> Entry:
    """An Entry with plausible defaults, so fixture fields live in one place."""
    return Entry(ts=ts, host=host, session=session, cwd="/tmp", exit_code=0, duration_ms=1, cmd=cmd)


#: Every environment variable store.py consults. Shared with tests/test_sync.py
#: so a new one cannot be isolated in one place and leak in the other.
ENV_KEYS = (
    "WOSWOAR_DIR",
    "WOSWOAR_SESSION",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
)


class WoswoarTestCase(unittest.TestCase):
    """Runs each test against a throwaway WOSWOAR_DIR.

    Every path in :mod:`woswoar.store` is resolved from the environment on each
    call rather than at import time, which is what makes this isolation work.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="woswoar-test-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        self.addCleanup(self._restore_env)

        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["WOSWOAR_DIR"] = str(self.root / "data")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        os.environ["XDG_CACHE_HOME"] = str(self.root / "cache")

        (store.config_dir()).mkdir(parents=True, exist_ok=True)
        (store.config_dir() / "machine").write_text(
            f"id={MACHINE_ID}\nname=test@machine\n", encoding="utf-8"
        )

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_log(self, host: str, day: str, lines: list[str]) -> Path:
        """Write raw log lines for ``host`` on ``day`` and return the path."""
        path = store.logs_dir() / "hosts" / host / f"{day}.tsv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return path

    entry = staticmethod(make_entry)
