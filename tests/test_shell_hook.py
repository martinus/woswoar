"""Contract tests for the bash hook.

The hook reimplements the escaping from :mod:`woswoar.entry` in pure shell so
that recording never forks. That duplication is deliberate but fragile, so these
tests drive the *real* hook in a *real* bash and parse the result with the
*real* Python parser. If the two implementations ever disagree, this is where it
shows up.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from woswoar import cache
from woswoar.entry import MAX_CMD_CHARS, TRUNCATION_MARKER, Entry, escape, unescape

from .support import MACHINE_ID, WoswoarTestCase

HOOK = Path(__file__).resolve().parent.parent / "woswoar" / "shell" / "woswoar.bash"

#: Inputs the two escape implementations must agree on. Command arguments cannot
#: carry a NUL byte, so that one case is out of reach here and is covered by the
#: pure-Python tests instead.
ESCAPE_CORPUS = [
    "git status",
    "a\tb",
    "line1\nline2",
    "carriage\rreturn",
    "back\\slash",
    "literal\\tbackslash-t",
    "\\",
    "\\\\",
    "\\n",
    "trailing\\",
    "tab\tand\nnewline\\and\\\\backslashes",
    "über 😀 naïve",
    "  leading and trailing  ",
    "$HOME `whoami` $(id) ${x}",
    "quote'single\"double",
    "*?[]{}",
    "x" * 5000,
]


def bash_major() -> int:
    bash = shutil.which("bash")
    if not bash:
        return 0
    out = subprocess.run(
        [bash, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False
    )
    return int(out.stdout.strip() or 0)


requires_bash5 = unittest.skipUnless(bash_major() >= 5, "bash 5.0+ required")


class ShellHookTestCase(WoswoarTestCase):
    def shell_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """A minimal environment pointing the hook at this test's sandbox."""
        runtime = self.root / "run"
        runtime.mkdir(exist_ok=True)
        env = {
            "HOME": str(self.root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": "dumb",
            "WOSWOAR_DIR": os.environ["WOSWOAR_DIR"],
            "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
            "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
            "XDG_RUNTIME_DIR": str(runtime),
        }
        env.update(extra or {})
        return env

    def run_shell(self, script: str, env_extra: dict[str, str] | None = None) -> None:
        """Run ``script`` line by line in an interactive bash with the hook loaded."""
        subprocess.run(
            ["bash", "--norc", "-i"],
            input=f"source {HOOK}\n{textwrap.dedent(script)}",
            text=True,
            env=self.shell_env(env_extra),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )

    def recorded(self) -> list[Entry]:
        """Every entry the hook wrote, oldest first, minus the `source` line."""
        entries = sorted(cache.load_entries(), key=lambda e: (e.ts, e.duration_ms))
        return [e for e in entries if not e.cmd.startswith("source ")]

    def commands(self) -> list[str]:
        return [e.cmd for e in self.recorded()]


@requires_bash5
class TestCapture(ShellHookTestCase):
    def test_records_a_plain_command(self) -> None:
        self.run_shell("echo hello\n")
        self.assertEqual(self.commands(), ["echo hello"])

    def test_records_the_whole_compound_line(self) -> None:
        # $BASH_COMMAND would report only "true a" here. Capturing the full line
        # is the entire reason the hook reads from `history` instead.
        self.run_shell("true a && true b\n")
        self.assertEqual(self.commands(), ["true a && true b"])

    def test_records_a_pipeline_in_full(self) -> None:
        self.run_shell("echo x | cat\n")
        self.assertEqual(self.commands(), ["echo x | cat"])

    def test_records_a_multiline_loop_as_one_entry(self) -> None:
        self.run_shell("for i in 1 2; do\ntrue $i\ndone\n")
        self.assertEqual(self.commands(), ["for i in 1 2; do true $i; done"])

    def test_blank_lines_record_nothing(self) -> None:
        self.run_shell("\n\n\necho only-this\n\n")
        self.assertEqual(self.commands(), ["echo only-this"])

    def test_metadata_is_captured(self) -> None:
        self.run_shell("cd /tmp\nfalse\n")
        by_cmd = {e.cmd: e for e in self.recorded()}
        self.assertEqual(by_cmd["false"].exit_code, 1)
        self.assertEqual(by_cmd["false"].cwd, "/tmp")
        self.assertEqual(by_cmd["false"].host, MACHINE_ID)
        self.assertTrue(by_cmd["false"].session)

    def test_duration_is_measured(self) -> None:
        self.run_shell("sleep 0.3\n")
        entry = self.recorded()[0]
        self.assertEqual(entry.cmd, "sleep 0.3")
        # Generous bounds: this must not flake on a loaded CI runner.
        self.assertGreaterEqual(entry.duration_ms, 250)
        self.assertLess(entry.duration_ms, 30_000)

    def test_duration_survives_a_comma_decimal_locale(self) -> None:
        # $EPOCHREALTIME honours LC_NUMERIC, so it reads "1785321992,048777"
        # under a de_AT locale. Splitting on "." alone silently yields garbage.
        self.run_shell("sleep 0.3\n", env_extra={"LC_NUMERIC": "de_AT.UTF-8"})
        entry = self.recorded()[0]
        self.assertGreaterEqual(entry.duration_ms, 250)
        self.assertLess(entry.duration_ms, 30_000)


@requires_bash5
class TestEscapeParity(unittest.TestCase):
    """The drift guard.

    Calls the hook's ``__woswoar_escape`` directly rather than going through an
    interactive session, because the piped-stdin harness cannot deliver every
    byte we need to cover: interactive bash reading from a pipe drops a literal
    tab before the command even runs (a real terminal, where readline handles
    bracketed paste, does not). Testing the function directly exercises the
    thing that can actually drift.
    """

    _extracted: Path
    _tmpdir: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        # Lift the function out of the real hook rather than copying it here --
        # a copy would be one more thing that can drift. The hook itself cannot
        # be sourced non-interactively; it returns early by design.
        source = HOOK.read_text(encoding="utf-8")
        match = re.search(r"^__woswoar_escape\(\) \{\n.*?^\}$", source, re.MULTILINE | re.DOTALL)
        assert match is not None, "__woswoar_escape not found in the hook"
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="woswoar-escape-")
        cls._extracted = Path(cls._tmpdir.name) / "escape.bash"
        cls._extracted.write_text(match.group(0) + "\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def escape_in_bash(self, value: str) -> str:
        script = (
            f'source "{self._extracted}"; __woswoar_escape "$1"; printf %s "$__woswoar_escaped"'
        )
        out = subprocess.run(
            ["bash", "--norc", "-c", script, "bash", value],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout

    def test_matches_python_escape(self) -> None:
        for value in ESCAPE_CORPUS:
            with self.subTest(value=value):
                self.assertEqual(self.escape_in_bash(value), escape(value))

    def test_python_can_unescape_what_bash_produced(self) -> None:
        for value in ESCAPE_CORPUS:
            with self.subTest(value=value):
                self.assertEqual(unescape(self.escape_in_bash(value)), value)

    def test_bash_output_is_always_single_line(self) -> None:
        for value in ESCAPE_CORPUS:
            with self.subTest(value=value):
                encoded = self.escape_in_bash(value)
                self.assertNotIn("\n", encoded)
                self.assertNotIn("\t", encoded)
                self.assertNotIn("\r", encoded)


@requires_bash5
class TestEscapingThroughAShell(ShellHookTestCase):
    def test_backslashes(self) -> None:
        self.run_shell("echo 'back\\slash'\n")
        self.assertEqual(self.commands(), ["echo 'back\\slash'"])

    def test_backslash_t_is_not_decoded_as_a_tab(self) -> None:
        # The exact case that a chained-str.replace unescape would corrupt.
        self.run_shell("printf 'a\\tb\\n'\n")
        self.assertEqual(self.commands(), ["printf 'a\\tb\\n'"])

    def test_unicode(self) -> None:
        self.run_shell("echo 'über 😀 naïve'\n")
        self.assertEqual(self.commands(), ["echo 'über 😀 naïve'"])

    def test_every_line_written_is_parseable(self) -> None:
        self.run_shell("echo 'a\tb'\necho 'c\\d'\nfor i in 1; do\ntrue\ndone\necho done\n")
        log = next((self.root / "data" / "logs").rglob("*.tsv"))
        raw = log.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        for line in raw.splitlines():
            with self.subTest(line=line):
                self.assertEqual(len(line.split("\t")), 6)


@requires_bash5
class TestFiltering(ShellHookTestCase):
    def test_ignore_pattern_suppresses_secrets(self) -> None:
        self.run_shell("export MY_SECRET_TOKEN=abc123\necho safe\n")
        self.assertEqual(self.commands(), ["echo safe"])

    def test_custom_ignore_pattern(self) -> None:
        self.run_shell("echo keep-me\necho drop-me\n", env_extra={"WOSWOAR_IGNORE": "drop-me"})
        self.assertEqual(self.commands(), ["echo keep-me"])

    def test_histcontrol_ignorespace_is_respected(self) -> None:
        # The hook defers to bash's own history rules rather than reimplementing
        # them, so a leading space is skipped for free.
        self.run_shell(
            "HISTCONTROL=ignorespace\n echo hidden\necho visible\n",
        )
        self.assertNotIn(" echo hidden", self.commands())
        self.assertIn("echo visible", self.commands())


@requires_bash5
class TestConstantParity(unittest.TestCase):
    """The hook hardcodes values that live in Python; pin them together.

    ``escape`` has its own parity test. These two constants were mirrored into
    the hook with only a comment to keep them honest, so changing
    ``MAX_CMD_CHARS`` in Python would silently leave bash truncating at the old
    length with the old marker, and CI would stay green.
    """

    source: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HOOK.read_text(encoding="utf-8")

    def test_truncation_length_matches(self) -> None:
        match = re.search(r"^__woswoar_max=(\d+)$", self.source, re.MULTILINE)
        assert match is not None, "__woswoar_max not found in the hook"
        self.assertEqual(int(match.group(1)), MAX_CMD_CHARS)

    def test_truncation_marker_matches(self) -> None:
        self.assertIn(f"'{TRUNCATION_MARKER}'", self.source)

    def test_hook_requires_the_documented_bash_version(self) -> None:
        # doctor reports "5.0+ required" independently; make sure the gate the
        # hook actually enforces is the same one.
        self.assertIn("BASH_VERSINFO[0] < 5", self.source)


@requires_bash5
@unittest.skipUnless(shutil.which("strace"), "strace required")
class TestForkFree(ShellHookTestCase):
    """Recording runs on every prompt, so its cost must not scale with usage."""

    def clone_count(self, command_count: int) -> int:
        script = "".join(f"echo cmd{i}\n" for i in range(command_count))
        proc = subprocess.run(
            ["strace", "-f", "-c", "-e", "trace=clone,clone3,vfork,fork", "bash", "--norc", "-i"],
            input=f"source {HOOK}\n{script}",
            text=True,
            env=self.shell_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
        total = 0
        for line in proc.stderr.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[-1] in {"clone", "clone3", "vfork", "fork"}:
                total += int(fields[3])
        return total

    def test_clone_count_does_not_scale_with_commands(self) -> None:
        # Not "zero forks": startup legitimately runs mkdir and one `trap -p`
        # subshell. What matters is that the per-command path adds nothing, so
        # the count must be identical for 3 and for 30 commands.
        few = self.clone_count(3)
        many = self.clone_count(30)
        self.assertEqual(
            few,
            many,
            f"record path forks: {few} clones for 3 commands, {many} for 30",
        )


if __name__ == "__main__":
    unittest.main()
