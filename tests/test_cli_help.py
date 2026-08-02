"""Every subcommand has to explain itself.

`add_parser(help=...)` shows a line in `woswoar --help` and nothing at all in
`woswoar <command> -h`, which needs `description=`. Every subcommand was missing
it, so `woswoar doctor -h` printed a usage line, `-h, --help`, and no statement
of what doctor does. These are the tests that stop that coming back one
subcommand at a time.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from woswoar.__main__ import build_parser


def _subparsers_action() -> object:
    """The single `_SubParsersAction` this parser has.

    Reached through the public `_actions` list because argparse offers nothing
    better, and identified by what it holds rather than by an isinstance on a
    private class -- so this breaks loudly if the parser is restructured, rather
    than quietly checking nothing.
    """
    parser = build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "sync" in choices:
            return action
    raise AssertionError("no subcommands found; build_parser changed shape")


def subcommands() -> dict[str, object]:
    """Every registered subparser, aliases included, keyed by name."""
    return dict(_subparsers_action().choices)  # type: ignore[attr-defined]


def summaries() -> dict[str, str]:
    """The one-liners `woswoar --help` lists, keyed by command name."""
    listed = _subparsers_action()._choices_actions  # type: ignore[attr-defined]
    return {action.dest: action.help or "" for action in listed}


class TestEveryCommandExplainsItself(unittest.TestCase):
    def test_there_are_subcommands_to_check(self) -> None:
        """Without this the rest is vacuous if `subcommands` ever returns {}."""
        self.assertGreaterEqual(len(subcommands()), 13)

    def test_each_one_has_a_description(self) -> None:
        missing = [name for name, p in subcommands().items() if not p.description]  # type: ignore[attr-defined]
        self.assertEqual(missing, [], "these print nothing useful for `-h`")

    def test_each_description_says_more_than_the_one_line_summary(self) -> None:
        """A `description` that repeats `help` verbatim is the same nothing in a
        longer form: it tells someone who ran `-h` what they already read."""
        thin = []
        for name, sub in subcommands().items():
            body = str(sub.description).strip()  # type: ignore[attr-defined]
            # First line is the summary; there has to be something after it.
            rest = body.split("\n", 1)[1].strip() if "\n" in body else ""
            if len(rest) < 80:
                thin.append(name)
        self.assertEqual(thin, [], "these say nothing beyond their one-line summary")

    def test_each_one_still_has_the_short_summary(self) -> None:
        """`woswoar --help` lists these; a subcommand with no `help=` is
        invisible there even though it exists and runs."""
        listed = summaries()
        self.assertIn("sync", listed, "precondition: summaries were found at all")
        self.assertEqual([name for name, text in listed.items() if not text], [])

    def test_help_is_printed_and_exits_cleanly(self) -> None:
        """The description is only worth having if `-h` actually reaches it."""
        for name in subcommands():
            # Captured: argparse prints to the real stdout, and thirteen help
            # texts in the suite's output is how people stop reading it.
            with (
                self.subTest(command=name),
                redirect_stdout(io.StringIO()) as shown,
                self.assertRaises(SystemExit) as exited,
            ):
                build_parser().parse_args([name, "-h"])
            self.assertEqual(exited.exception.code, 0)
            self.assertIn("usage:", shown.getvalue())


if __name__ == "__main__":
    unittest.main()
