"""What `woswoar install` writes into .bashrc.

One .bashrc is routinely shared across machines through a dotfiles repo, so the
line has to be identical on all of them. It was not: it named the absolute path,
which differs the moment two machines disagree about the username.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from woswoar.__main__ import main, portable_hook_path

from .support import WoswoarTestCase, requires_bash


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


if __name__ == "__main__":
    unittest.main()
