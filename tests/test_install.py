"""What `woswoar install` writes into .bashrc.

One .bashrc is routinely shared across machines through a dotfiles repo, so the
line has to be identical on all of them. It was not: it named the absolute path,
which differs the moment two machines disagree about the username.
"""

from __future__ import annotations

import io
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from woswoar import store
from woswoar.__main__ import main, portable_hook_path

from .support import MACHINE_ID, WoswoarTestCase, make_entry, requires_bash


class TestPortableHookPath(unittest.TestCase):
    def test_a_path_under_home_becomes_a_variable(self) -> None:
        home = Path.home()
        self.assertEqual(
            portable_hook_path(home / ".local/share/woswoar/woswoar.bash"),
            "$HOME/.local/share/woswoar/woswoar.bash",
        )

    def test_a_path_outside_home_stays_absolute(self) -> None:
        # Nothing portable to say about it, and guessing beats being explicit
        # in exactly no situation.
        self.assertEqual(
            portable_hook_path(Path("/opt/woswoar/woswoar.bash")), "/opt/woswoar/woswoar.bash"
        )


class TestInstall(WoswoarTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self._home_before = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        self.rcfile = self.home / ".bashrc"

    def _restore_home(self) -> None:
        if self._home_before is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home_before

    def install(self) -> str:
        self.assertEqual(main(["install", "--rcfile", str(self.rcfile)]), 0)
        return self.rcfile.read_text(encoding="utf-8")

    def test_the_sourced_line_names_no_username(self) -> None:
        os.environ["WOSWOAR_DIR"] = str(self.home / ".local/share/woswoar")
        text = self.install()
        self.assertIn('source "$HOME/.local/share/woswoar/woswoar.bash"', text)
        self.assertNotIn(str(self.home), text)

    def test_reinstalling_rewrites_an_older_absolute_block(self) -> None:
        """The upgrade path. Both of the reporting machines have one of these."""
        os.environ["WOSWOAR_DIR"] = str(self.home / ".local/share/woswoar")
        stale = (
            "# >>> woswoar >>>\n"
            f'source "{self.home}/.local/share/woswoar/woswoar.bash"\n'
            "# <<< woswoar <<<\n"
        )
        self.rcfile.write_text(f"# my settings\n\n{stale}", encoding="utf-8")

        text = self.install()
        self.assertIn('source "$HOME/.local/share/woswoar/woswoar.bash"', text)
        self.assertNotIn(str(self.home), text)
        self.assertEqual(text.count("# >>> woswoar >>>"), 1, "block was duplicated")
        self.assertIn("# my settings", text, "the rest of the file must survive")

    def test_a_directory_outside_home_is_still_written_absolute(self) -> None:
        outside = self.root / "elsewhere"
        os.environ["WOSWOAR_DIR"] = str(outside)
        text = self.install()
        self.assertIn(f'source "{outside}/woswoar.bash"', text)

    @requires_bash
    def test_bash_resolves_the_line_to_the_installed_hook(self) -> None:
        """The point of the whole thing: the line has to still work.

        Asserting on the string only proves it looks portable. This proves a
        real bash expands it to the file install just wrote.
        """
        os.environ["WOSWOAR_DIR"] = str(self.home / ".local/share/woswoar")
        self.install()
        line = next(
            ln
            for ln in self.rcfile.read_text(encoding="utf-8").splitlines()
            if ln.startswith("source ")
        )
        resolved = subprocess.run(
            ["bash", "-c", f'printf "%s" {line.removeprefix("source ")}'],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env={"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        ).stdout
        self.assertTrue(Path(resolved).is_file(), f"{resolved} was not written by install")


class TestPrivateByDefault(WoswoarTestCase):
    """Recorded history is at least as private as ~/.bash_history.

    bash creates that 0600. woswoar's logs hold strictly more -- the command,
    the working directory, the exit status, and every other machine's history
    once sync has run -- so anything looser would make installing woswoar a
    downgrade on any box with a group- or world-readable home.
    """

    def modes(self) -> dict[str, int]:
        paths = [
            store.data_dir(),
            store.config_dir(),
            store.logs_dir(),
            *store.logs_dir().rglob("*"),
        ]
        return {str(p): p.stat().st_mode & 0o777 for p in paths if p.exists()}

    def first_run(self) -> None:
        """What a real first run writes, through the real entry points.

        The shared harness pre-creates the config directory with a plain
        mkdir, so a test that only appended entries would be asserting about a
        directory woswoar never wrote.
        """
        store.save_machine(store.machine())
        store.append_entries(MACHINE_ID, [make_entry(1_700_000_000, "git status")])

    def test_everything_written_is_owner_only(self) -> None:
        self.first_run()
        for path, mode in self.modes().items():
            self.assertEqual(mode & 0o077, 0, f"{path} is {oct(mode)}")

    def test_a_stock_umask_cannot_loosen_them(self) -> None:
        # 022 is the default nearly everywhere, and is exactly what made the
        # logs 0644 before: `mkdir` and `open(..., "a")` both honour it.
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        self.first_run()
        for path, mode in self.modes().items():
            self.assertEqual(mode & 0o077, 0, f"{path} is {oct(mode)}")

    def test_an_older_install_is_retightened(self) -> None:
        self.first_run()
        for path in (store.data_dir(), store.logs_dir(), *store.logs_dir().rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        self.assertTrue(store.world_readable(), "the fixture is not actually loose")

        store.harden()
        self.assertEqual(store.world_readable(), [])

    def test_doctor_reports_an_exposed_history(self) -> None:
        self.first_run()
        store.logs_dir().chmod(0o755)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            main(["doctor"])
        self.assertRegex(out.getvalue(), r"\[FAIL\] private")


if __name__ == "__main__":
    unittest.main()
