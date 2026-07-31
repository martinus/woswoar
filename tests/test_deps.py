"""Telling someone what to install, in terms their machine understands."""

from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from woswoar import deps
from woswoar.__main__ import main

from .support import WoswoarTestCase


class TestInstaller(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="woswoar-deps-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self._next = 0

    def release(self, text: str) -> Path:
        self._next += 1
        path = self.tmp / f"os-release.{self._next}"
        path.write_text(text, encoding="utf-8")
        return path

    def test_fedora(self) -> None:
        self.assertEqual(deps.installer(self.release("ID=fedora\n")), "sudo dnf install")

    def test_ubuntu(self) -> None:
        self.assertEqual(deps.installer(self.release("ID=ubuntu\n")), "sudo apt install")

    def test_a_derivative_is_covered_by_id_like(self) -> None:
        # Linux Mint reports ID=linuxmint ID_LIKE=ubuntu. Naming every
        # derivative is a losing game; ID_LIKE is what it is for.
        path = self.release('ID=linuxmint\nID_LIKE="ubuntu debian"\n')
        self.assertEqual(deps.installer(path), "sudo apt install")

    def test_id_wins_over_id_like(self) -> None:
        path = self.release("ID=fedora\nID_LIKE=debian\n")
        self.assertEqual(deps.installer(path), "sudo dnf install")

    def test_quotes_are_stripped(self) -> None:
        self.assertEqual(deps.installer(self.release('ID="fedora"\n')), "sudo dnf install")

    def test_an_unknown_distro_admits_it(self) -> None:
        # Printing a confidently wrong command is worse than saying we do not
        # know, so `advice` falls back rather than guessing.
        path = self.release("ID=plan9\n")
        self.assertEqual(deps.installer(path), "")
        self.assertIn("your package manager", deps.advice([deps.FZF], path=path))

    def test_a_missing_os_release_does_not_raise(self) -> None:
        path = Path("/nonexistent/os-release")
        self.assertEqual(deps.installer(path), "")
        self.assertIn("fzf", deps.advice([deps.FZF], path=path))

    def test_the_advice_is_one_pasteable_command(self) -> None:
        path = self.release("ID=fedora\n")
        self.assertEqual(deps.advice([deps.FZF, deps.AGE], path=path), "sudo dnf install fzf age")

    def test_report_names_each_tool_and_why(self) -> None:
        text = deps.report([deps.FZF], path=self.release("ID=fedora\n"))
        self.assertIn("fzf", text)
        self.assertIn(deps.FZF.needed_for, text)
        self.assertIn("sudo dnf install fzf", text)

    def test_report_of_nothing_missing_is_empty(self) -> None:
        self.assertEqual(deps.report([]), "")


class TestInstallWarnsAboutMissingTools(WoswoarTestCase):
    """`woswoar install` is when someone finds out, so it is when we say it."""

    def setUp(self) -> None:
        super().setUp()
        # A PATH with a working python but none of woswoar's tools.
        empty = self.root / "emptybin"
        empty.mkdir()
        self._path = os.environ["PATH"]
        os.environ["PATH"] = str(empty)
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._path))

        self.home = self.root / "home"
        self.home.mkdir()
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

    def _restore_home(self) -> None:
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home

    def test_install_still_succeeds_but_says_what_is_missing(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["install", "--rcfile", str(self.home / ".bashrc")])

        # Recording works without any of them, so this is a warning, not a
        # failure -- the hook really was installed.
        self.assertEqual(code, 0)
        self.assertIn("woswoar.bash", out.getvalue())
        for tool in ("fzf", "age", "git", "ssh-keygen"):
            self.assertIn(tool, err.getvalue())

    def test_a_complete_machine_says_nothing(self) -> None:
        # Put the tools back so `missing()` finds them.
        binaries = self.root / "fullbin"
        binaries.mkdir()
        for name in ("fzf", "age", "git", "ssh-keygen"):
            script = binaries / name
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        os.environ["PATH"] = str(binaries)

        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            main(["install", "--rcfile", str(self.home / ".bashrc")])
        self.assertNotIn("could not find", err.getvalue())


if __name__ == "__main__":
    unittest.main()
