"""Shared test scaffolding."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from woswoar import store
from woswoar.entry import Entry

MACHINE_ID = "0123456789abcdef"


def make_entry(ts: int, cmd: str, host: str = MACHINE_ID, session: str = "s1") -> Entry:
    """An Entry with plausible defaults, so fixture fields live in one place."""
    return Entry(ts=ts, host=host, session=session, cwd="/tmp", exit_code=0, duration_ms=1, cmd=cmd)


_ENV_KEYS = (
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

        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        self.addCleanup(self._restore_env)

        for key in _ENV_KEYS:
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
