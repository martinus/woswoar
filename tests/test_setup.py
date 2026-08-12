"""`woswoar setup`: the guided first run.

It owns no logic of its own -- every step calls the command someone would have
run by hand -- so what these test is the *asking*: which questions appear, what
the answers are turned into, and that it refuses rather than guessing when there
is nobody to ask.
"""

from __future__ import annotations

import argparse
import os
import unittest
from pathlib import Path
from unittest import mock

from woswoar import __main__ as main_module

from . import support
from .support import WoswoarTestCase


class Answers:
    """Scripted replies, in order, for `input`.

    Runs out deliberately: a prompt this did not expect raises rather than
    blocking the suite forever or silently taking a default.
    """

    def __init__(self, *replies: str) -> None:
        self.remaining = list(replies)
        self.asked: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.asked.append(prompt)
        if not self.remaining:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return self.remaining.pop(0)


class SetupTestCase(WoswoarTestCase):
    def setUp(self) -> None:
        super().setUp()
        # `setup` refuses without one, and every test here is about what it does
        # when it has one.
        self._tty = mock.patch("sys.stdin.isatty", return_value=True)
        self._tty.start()
        self.addCleanup(self._tty.stop)

        # `importer` resolves every default path from `Path.home()`, and
        # `WoswoarTestCase` redirects the XDG variables but not HOME -- so
        # without this these tests read the developer's own shell history and
        # `setup` offers to import it.
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(self.root)
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("HOME", previous)
                if previous is not None
                else os.environ.pop("HOME", None)
            )
        )
        # Step 1 asks an extra question when a tool is missing, so leaving this
        # to the machine makes every scripted answer below land on a different
        # prompt depending on whether fzf happens to be installed. CI installs
        # it and the release workflow did not, which is exactly how this got
        # through review and then failed while cutting a release.
        self._tools = mock.patch("woswoar.deps.missing", return_value=[])
        self._tools.start()
        self.addCleanup(self._tools.stop)

        self.rcfile = Path(self.root) / "bashrc"
        self.rcfile.write_text("# existing\n", encoding="utf-8")

    def run_setup(self, *replies: str) -> Answers:
        answers = Answers(*replies)
        with mock.patch("builtins.input", answers):
            main_module.cmd_setup(argparse.Namespace(rcfile=str(self.rcfile)))
        return answers


def _never_asks(prompt: str = "") -> str:
    """An `input` that fails instead of blocking.

    Without this a `setup` that wrongly proceeds past the tty check reads the
    test runner's own stdin and hangs the suite -- which is exactly what
    happened the first time a mutation removed that check. A test that can hang
    forever reports nothing; one that raises names the bug.
    """
    raise AssertionError(f"asked a question with nobody there: {prompt!r}")


class TestItRefusesWithNobodyToAsk(WoswoarTestCase):
    """No HOME redirection needed: it exits before reading anything."""

    def test_a_pipe_gets_the_commands_to_run_instead(self) -> None:
        with (
            mock.patch("sys.stdin.isatty", return_value=False),
            mock.patch("builtins.input", _never_asks),
        ):
            ran = support.run_cli("setup")
        self.assertEqual(ran.code, 1)
        # The alternative, not just a refusal: someone in a script needs the
        # three commands, not the news that this one is interactive.
        for command in ("woswoar install", "woswoar import", "woswoar init"):
            self.assertIn(command, ran.err)

    def test_it_changes_nothing_on_the_way_out(self) -> None:
        rcfile = Path(self.root) / "bashrc"
        rcfile.write_text("# existing\n", encoding="utf-8")
        with (
            mock.patch("sys.stdin.isatty", return_value=False),
            mock.patch("builtins.input", _never_asks),
        ):
            support.run_cli("setup", "--rcfile", str(rcfile))
        self.assertEqual(rcfile.read_text(encoding="utf-8"), "# existing\n")


class TestTheToolsStep(SetupTestCase):
    """The branch the other tests patch away, tested on its own."""

    def test_a_missing_tool_is_reported_and_can_be_carried_past(self) -> None:
        from woswoar import deps

        with mock.patch("woswoar.deps.missing", return_value=[deps.FZF]):
            answers = self.run_setup("y", "")  # carry on, then blank URL
        self.assertTrue(any("Carry on without them" in prompt for prompt in answers.asked))

    def test_declining_stops_before_anything_is_written(self) -> None:
        from woswoar import deps

        with mock.patch("woswoar.deps.missing", return_value=[deps.FZF]):
            self.run_setup("n")
        self.assertEqual(self.rcfile.read_text(encoding="utf-8"), "# existing\n")


class TestWhatItOffersToImport(SetupTestCase):
    def test_only_histories_that_exist_and_hold_something(self) -> None:
        (Path.home() / ".bash_history").write_text("echo hi\n", encoding="utf-8")
        (Path.home() / ".zsh_history").write_text("", encoding="utf-8")  # empty
        kinds = [kind for kind, _, _ in main_module._importable()]
        self.assertEqual(kinds, ["bash"], "an empty history is not worth offering")

    def test_the_biggest_is_offered_first(self) -> None:
        (Path.home() / ".bash_history").write_text("x\n", encoding="utf-8")
        (Path.home() / ".zsh_history").write_text("y" * 5000, encoding="utf-8")
        self.assertEqual([kind for kind, _, _ in main_module._importable()], ["zsh", "bash"])

    def test_nothing_found_is_not_a_failure(self) -> None:
        answers = self.run_setup("")  # only the repository URL is asked
        self.assertEqual(len(answers.asked), 1)


class TestTheAtuinQuestion(SetupTestCase):
    """The one question `setup` asks that no single command asks.

    atuin keeps every machine it has synced with in one database, and woswoar
    publishes only a machine's own commands -- so importing all of them on every
    machine stores each machine's history once per machine. Which answer is
    right depends on something only the person knows: whether woswoar is going
    on those other machines too.
    """

    def setUp(self) -> None:
        super().setUp()
        db = Path.home() / ".local/share/atuin/history.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"x" * 4096)
        self.imported: list[argparse.Namespace] = []

        def record(args: argparse.Namespace) -> int:
            self.imported.append(args)
            return 0

        patched = mock.patch.object(main_module, "cmd_import", record)
        patched.start()
        self.addCleanup(patched.stop)

    def test_it_is_asked_and_yes_reaches_the_importer(self) -> None:
        answers = self.run_setup("y", "y", "")
        self.assertTrue(
            any("only this machine" in prompt for prompt in answers.asked),
            f"never asked: {answers.asked}",
        )
        self.assertTrue(self.imported[0].this_host_only)

    def test_no_imports_every_machine(self) -> None:
        self.run_setup("y", "n", "")
        self.assertFalse(self.imported[0].this_host_only)

    def test_declining_the_import_does_not_ask_it_at_all(self) -> None:
        answers = self.run_setup("n", "")
        self.assertFalse(any("only this machine" in prompt for prompt in answers.asked))
        self.assertEqual(self.imported, [])

    def test_it_is_not_asked_for_bash(self) -> None:
        """The question is about atuin's multi-machine database, and asking it
        of a file that holds one machine's history would be noise."""
        (Path.home() / ".local/share/atuin/history.db").unlink()
        (Path.home() / ".bash_history").write_text("echo hi\n", encoding="utf-8")
        answers = self.run_setup("y", "")
        self.assertFalse(any("only this machine" in prompt for prompt in answers.asked))
        self.assertFalse(self.imported[0].this_host_only)


class TestTheRepositoryStep(SetupTestCase):
    def test_a_blank_url_stays_local_and_joins_nothing(self) -> None:
        self.run_setup("")
        self.assertFalse((Path(os.environ["WOSWOAR_DIR"]) / "history" / ".git").exists())

    def test_the_hook_is_installed_either_way(self) -> None:
        """Staying local is a supported outcome, not an abort: the single
        machine case is the README's whole install section."""
        self.run_setup("")
        self.assertIn("woswoar", self.rcfile.read_text(encoding="utf-8"))


class TestItIsSafeToRunTwice(SetupTestCase):
    def test_the_rcfile_gains_one_block_not_two(self) -> None:
        self.run_setup("")
        self.run_setup("")
        text = self.rcfile.read_text(encoding="utf-8")
        self.assertEqual(text.count(main_module._BEGIN), 1)


class TestTheBareCommandOnAFreshMachine(SetupTestCase):
    def test_nothing_at_all_here_runs_setup(self) -> None:
        answers = Answers("")  # only the repository URL, which is left blank
        with mock.patch("builtins.input", answers):
            main_module.cmd_status(argparse.Namespace())
        self.assertTrue(answers.asked, "setup never ran")
        # `cmd_status` passes no --rcfile, so this is the default ~/.bashrc --
        # under the redirected HOME, not the one the other tests here point at.
        self.assertIn("woswoar", (Path.home() / ".bashrc").read_text(encoding="utf-8"))

    def test_a_hook_alone_is_enough_to_be_reported_on_instead(self) -> None:
        """Someone who ran `install` and nothing else has set something up, and
        running `setup` at them would answer a question they did not ask."""
        hook = Path(os.environ["WOSWOAR_DIR"]) / main_module.HOOK_NAME
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#", encoding="utf-8")

        def never(prompt: str = "") -> str:
            raise AssertionError(f"setup ran on a machine that was not fresh: {prompt!r}")

        with mock.patch("builtins.input", never):
            ran = support.run_cli()
        self.assertEqual(ran.code, 0)
        self.assertIn("woswoar init", ran.out)

    def test_an_imported_history_alone_is_too(self) -> None:
        support.run_cli("import", "bash", "--file", str(self._history()))

        def never(prompt: str = "") -> str:
            raise AssertionError(f"setup ran on a machine that was not fresh: {prompt!r}")

        with mock.patch("builtins.input", never):
            self.assertEqual(support.run_cli().code, 0)

    def _history(self) -> Path:
        path = Path(self.root) / "history"
        path.write_text("echo one\necho two\n", encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
