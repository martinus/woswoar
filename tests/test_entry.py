"""The record format contract."""

from __future__ import annotations

import unittest

from woswoar.entry import (
    MAX_CMD_CHARS,
    TRUNCATION_MARKER,
    Entry,
    escape,
    format_line,
    home_relative,
    parse_line,
    truncate,
    unescape,
)

TRICKY = [
    "git status",
    "a\tb",
    "line1\nline2",
    "carriage\rreturn",
    "back\\slash",
    # The case that breaks a chained-str.replace implementation: a literal
    # backslash followed by the letter t must not decode to a tab.
    "literal\\tbackslash-t",
    "\\",
    "\\\\",
    "\\n",
    "trailing\\",
    "über 😀 naïve",
    "",
    "  leading and trailing  ",
    "mixed\t\n\\\r all at once",
]


class TestEscaping(unittest.TestCase):
    def test_round_trip(self) -> None:
        for value in TRICKY:
            with self.subTest(value=value):
                self.assertEqual(unescape(escape(value)), value)

    def test_escaped_form_is_single_line(self) -> None:
        for value in TRICKY:
            with self.subTest(value=value):
                encoded = escape(value)
                self.assertNotIn("\n", encoded)
                self.assertNotIn("\t", encoded)
                self.assertNotIn("\r", encoded)

    def test_unescape_leaves_unknown_sequences_alone(self) -> None:
        # \q has no meaning; mangling it would corrupt a real command.
        self.assertEqual(unescape("\\q"), "\\q")

    def test_unescape_of_plain_text_is_identity(self) -> None:
        self.assertIs(unescape("no backslashes here"), "no backslashes here")


class TestTruncate(unittest.TestCase):
    def test_short_command_untouched(self) -> None:
        self.assertEqual(truncate("echo hi"), "echo hi")

    def test_long_command_marked(self) -> None:
        result = truncate("x" * (MAX_CMD_CHARS + 500))
        self.assertTrue(result.endswith(TRUNCATION_MARKER))
        self.assertEqual(len(result), MAX_CMD_CHARS + len(TRUNCATION_MARKER))


class TestLines(unittest.TestCase):
    def test_round_trip(self) -> None:
        for cmd in TRICKY:
            if not cmd:
                continue
            with self.subTest(cmd=cmd):
                original = Entry(
                    ts=1753781234,
                    host="host1",
                    session="sess",
                    cwd="/src/a\tb",
                    exit_code=1,
                    duration_ms=42,
                    cmd=cmd,
                )
                self.assertEqual(parse_line(format_line(original), "host1"), original)

    def test_line_has_exactly_six_fields(self) -> None:
        entry = Entry(1, "h", "s", "/a\tb", 0, 0, "cmd\twith\ttabs")
        self.assertEqual(len(format_line(entry).split("\t")), 6)

    def test_host_comes_from_the_caller_not_the_line(self) -> None:
        entry = Entry(1, "ignored", "s", "/tmp", 0, 0, "ls")
        parsed = parse_line(format_line(entry), "actual-host")
        assert parsed is not None
        self.assertEqual(parsed.host, "actual-host")

    def test_rejects_unusable_lines(self) -> None:
        for bad in [
            "",
            "\n",
            "not a record",
            "1\t2\t3",  # too few fields
            "notanumber\ts\t/tmp\t0\t0\tls",  # bad timestamp
            "1\ts\t/tmp\tX\t0\tls",  # bad exit code
            "1\ts\t/tmp\t0\tY\tls",  # bad duration
        ]:
            with self.subTest(line=bad):
                self.assertIsNone(parse_line(bad, "h"))

    def test_partial_final_line_costs_only_that_line(self) -> None:
        # A shell killed mid-append leaves a truncated record; it must not take
        # the rest of the file with it.
        self.assertIsNone(parse_line("1753781234\tsess\t/tmp", "h"))

    def test_command_containing_tabs_survives(self) -> None:
        entry = Entry(1, "h", "s", "/tmp", 0, 0, "awk -F'\t' '{print $1}'")
        parsed = parse_line(format_line(entry), "h")
        assert parsed is not None
        self.assertEqual(parsed.cmd, "awk -F'\t' '{print $1}'")


class TestParsedCommandsAreBounded(unittest.TestCase):
    def test_an_over_long_line_is_clamped_on_the_way_in(self) -> None:
        """`format_line` clamps what this machine writes; `parse_line` clamps
        what arrives.

        A line can reach the logs from another machine's chunk, where the only
        thing bounding its length is whatever wrote it -- and every consumer
        from the cache to the picker assumes MAX_CMD_CHARS holds.
        """
        raw = "1700000000\ts1\t~\t0\t5\t" + "x" * (MAX_CMD_CHARS * 3)
        parsed = parse_line(raw, "host")
        assert parsed is not None
        self.assertLessEqual(len(parsed.cmd), MAX_CMD_CHARS + len(TRUNCATION_MARKER))
        self.assertTrue(parsed.cmd.endswith(TRUNCATION_MARKER))

    def test_an_ordinary_command_is_returned_whole(self) -> None:
        parsed = parse_line("1700000000\ts1\t~\t0\t5\tgit status", "host")
        assert parsed is not None
        self.assertEqual(parsed.cmd, "git status")


if __name__ == "__main__":
    unittest.main()


class TestHomeRelative(unittest.TestCase):
    """The `~` rule, which three implementations now have to agree on.

    The hook writes it in shell, the importer applies it to this machine's own
    rows, and `search`'s `dir` scope rebuilds it to compare against. It moved
    here from `importer` when the third caller arrived: `search` must not import
    `importer`, whose weight on the scope-switch path `credentials` documents as
    a measured cost.
    """

    HOME = "/home/martinus"

    def test_the_home_directory_itself(self) -> None:
        self.assertEqual(home_relative(self.HOME, self.HOME), "~")

    def test_a_path_below_home(self) -> None:
        self.assertEqual(home_relative(f"{self.HOME}/src/woswoar", self.HOME), "~/src/woswoar")

    def test_a_sibling_that_starts_the_same_is_left_alone(self) -> None:
        """The trap in `${PWD/#$HOME/~}`, which rewrites this to `~copy`. The
        hook spells the rule out in shell for the same reason, and
        `test_shell_hook.test_sibling_of_home_is_not_mangled` pins that half."""
        self.assertEqual(home_relative("/home/martinuscopy", self.HOME), "/home/martinuscopy")

    def test_a_path_outside_home(self) -> None:
        self.assertEqual(home_relative("/etc", self.HOME), "/etc")

    def test_no_home_leaves_every_path_absolute(self) -> None:
        """A machine with `$HOME` unset, where every path is honestly absolute
        -- not a guess to be made on its behalf."""
        self.assertEqual(home_relative("/home/martinus/src", ""), "/home/martinus/src")

    def test_an_empty_path_stays_empty(self) -> None:
        self.assertEqual(home_relative("", self.HOME), "")
