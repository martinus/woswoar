"""The record format contract."""

from __future__ import annotations

import unittest

from woswoar.entry import (
    MAX_CMD_CHARS,
    TRUNCATION_MARKER,
    Entry,
    escape,
    format_line,
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


if __name__ == "__main__":
    unittest.main()
