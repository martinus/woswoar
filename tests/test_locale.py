"""Does woswoar still read its own history when the machine is not UTF-8?

Every file this package opens passes `encoding="utf-8"`, and until #229 nothing
proved it stays that way: the suite runs UTF-8 on every developer machine and on
all three CI Pythons, and there `read_text()` and `read_text(encoding="utf-8")`
are the same call. A whole-package `drop-kwarg` sweep made the gap countable --
every `encoding=` row it generated survived (see `tests.test_mutants`'s note on
why the operator no longer asks).

The consequence is a user's own history: woswoar stores shell commands verbatim,
and those routinely carry an em dash from a commit message, an accent in a path,
a `✔` in a script. On a machine under `LC_ALL=C` -- a container, a cron job, a
minimal image, a systemd unit with no locale -- a read missing its `encoding=`
raises `UnicodeDecodeError` on that history, and a write missing it raises
`UnicodeEncodeError` rather than recording.

A module of its own because the guarantee is package-wide rather than any one
module's, and because everything here costs a subprocess: the locale is chosen
at interpreter start, so nothing set inside this process can move it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from woswoar import store

from .support import MACHINE_ID, WoswoarTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A command with characters outside ASCII in it, and a machine named by someone
#: who types their own language. Two, because they take different routes: the
#: command reaches disk through the importer's append and comes back through the
#: cache, while the name is `read_text` in `store.host_name`.
NON_ASCII = "git commit -m 'fix ✔ — done'"
NON_ASCII_HOST = "café@büro"

#: What it takes to make Python prefer ASCII for *files*. `LC_ALL` and `LANG`
#: select the C locale; `PYTHONUTF8=0` turns off UTF-8 mode, which PEP 686 makes
#: the default from 3.15 and which would otherwise paper over the thing being
#: tested. `PYTHONIOENCODING` covers stdout only, so a child that reads its files
#: correctly can still print what it read -- without it these tests would fail on
#: a `UnicodeEncodeError` at the terminal and prove nothing about the files.
ASCII_LOCALE = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}


def _prefers_ascii() -> bool:
    """Whether a child started this way really does prefer ASCII for files.

    Asked by trying it rather than by reading a version number, for the reason
    `tests.test_mutate.enforced` gives about `RLIMIT_AS`: a guard that decides
    from `sys.version_info` goes green on the platform where it silently
    protects nothing. Once per process, as `support.bash_major` is.
    """
    probe = "import locale; print(locale.getpreferredencoding(False))"
    said = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, **ASCII_LOCALE},
        check=False,
    )
    return "utf-8" not in said.stdout.strip().lower().replace("_", "-")


requires_ascii_locale = unittest.skipUnless(
    _prefers_ascii(), "this Python prefers UTF-8 whatever the locale says"
)


@requires_ascii_locale
class TestARoundTripUnderAnAsciiLocale(WoswoarTestCase):
    """Written and read back by children that prefer ASCII.

    Both halves in the child on purpose. A fixture that wrote the log from this
    process would test only the reading, and the write is the half that raises
    rather than returning something subtly wrong.

    What this witnesses is three paths, not the whole package: the day file
    (the importer's append, and the cache's read of it), `.name`
    (`store.host_name`), and `config/machine` (`store.machine`, which every run
    below reads before it does anything else). The rest of the `encoding=` sites
    carry ids, keys and offsets -- ASCII by construction, where dropping the
    argument cannot change an answer.
    """

    def setUp(self) -> None:
        super().setUp()
        # The harness writes an ASCII name here; a non-ASCII one costs nothing
        # and puts `store.machine`'s own `read_text` on the path of every child
        # this class starts.
        (store.config_dir() / "machine").write_text(
            f"id={MACHINE_ID}\nname={NON_ASCII_HOST}\n", encoding="utf-8"
        )
        self.env = {**os.environ, **ASCII_LOCALE, "PYTHONPATH": str(REPO_ROOT)}

    def woswoar(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "woswoar", *argv],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=120,
        )

    def test_a_command_written_under_it_is_read_back_whole(self) -> None:
        history = self.root / "history"
        history.write_text(f"{NON_ASCII}\n", encoding="utf-8")
        imported = self.woswoar("import", "bash", "--file", str(history))
        self.assertEqual(imported.returncode, 0, imported.stderr)

        store.write_atomic(store.name_file(MACHINE_ID), f"{NON_ASCII_HOST}\n".encode())
        # `stats` rather than `list`: it prints the host *names*, so it is the
        # reader that opens `.name`, and it prints the commands too -- so one
        # run answers both halves.
        said = self.woswoar("stats")
        self.assertEqual(said.returncode, 0, said.stderr)
        self.assertIn(NON_ASCII_HOST, said.stdout)
        self.assertIn(NON_ASCII, said.stdout)
