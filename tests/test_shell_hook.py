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
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import ClassVar

from woswoar import cache, store
from woswoar.entry import MAX_CMD_CHARS, TRUNCATION_MARKER, Entry, escape, unescape

from .credential_shapes import DOCUMENTED_GAPS, INNOCENT_SHAPES, SECRET_SHAPES
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
    def runtime_dir(self) -> Path:
        """Where XDG_RUNTIME_DIR points for these runs."""
        return self.root / "run"

    def shell_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """A minimal environment pointing the hook at this test's sandbox."""
        runtime = self.runtime_dir()
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

    def run_shell(
        self,
        script: str,
        env_extra: dict[str, str] | None = None,
        before: str = "",
    ) -> str:
        """Run ``script`` line by line in an interactive bash with the hook loaded.

        ``before`` runs *ahead of* the hook, which is how a real ~/.bashrc looks:
        the interesting bugs are all about what else already owns PROMPT_COMMAND
        and the DEBUG trap by the time woswoar loads. Returns the shell's output
        so a test can assert the other tool still ran.
        """
        result = subprocess.run(
            ["bash", "--norc", "-i"],
            input=f"{textwrap.dedent(before)}\nsource {HOOK}\n{textwrap.dedent(script)}",
            text=True,
            env=self.shell_env(env_extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=60,
        )
        return result.stdout

    def shell_value(self, expr: str, env_extra: dict[str, str] | None = None) -> str:
        """Run the hook, then print one shell expression and read it back."""
        out = self.run_shell(f'printf "VALUE %s\\n" {expr}\n', env_extra)
        match = re.search(r"^VALUE (.*)$", out, re.MULTILINE)
        assert match is not None, f"the shell printed no value:\n{out}"
        return match.group(1)

    def recorded(self) -> list[Entry]:
        """Every entry the hook wrote, oldest first.

        Deliberately unfiltered. This used to drop anything starting with
        ``source``, because the ``source .../woswoar.bash`` line the harness
        itself types got recorded. The hook now skips the prompt at which it
        loads, so the subtraction is not only unnecessary but would hide that
        guard regressing -- and would also swallow a real
        ``source .venv/bin/activate``.
        """
        return sorted(cache.load_entries(), key=lambda e: (e.ts, e.duration_ms))

    def commands(self) -> list[str]:
        return [e.cmd for e in self.recorded()]

    def by_cmd(self) -> dict[str, Entry]:
        """Recorded entries indexed by command, for asserting on metadata."""
        return {e.cmd: e for e in self.recorded()}


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
        by_cmd = self.by_cmd()
        self.assertEqual(by_cmd["false"].exit_code, 1)
        self.assertEqual(by_cmd["false"].cwd, "/tmp")
        self.assertEqual(by_cmd["false"].host, MACHINE_ID)
        self.assertTrue(by_cmd["false"].session)

    def test_session_id_is_compact_and_hex(self) -> None:
        # Repeated on every line, so its size is not cosmetic. <second>-<pid>,
        # both hex.
        self.run_shell("echo hello\n")
        session = self.recorded()[0].session
        self.assertRegex(session, r"^[0-9a-f]+-[0-9a-f]+$")
        self.assertLessEqual(len(session), 16)

    def test_two_shells_get_different_sessions(self) -> None:
        self.run_shell("echo one\n")
        self.run_shell("echo two\n")
        sessions = {e.session for e in self.recorded()}
        self.assertEqual(len(sessions), 2, "session id must be unique per shell")

    def test_home_is_stored_as_tilde(self) -> None:
        # $HOME is self.root for these runs, so cd'ing there must record "~".
        self.run_shell("cd ~\ntrue marker\n")
        by_cmd = self.by_cmd()
        self.assertEqual(by_cmd["true marker"].cwd, "~")

    def test_path_under_home_is_stored_relative(self) -> None:
        (self.root / "src").mkdir(exist_ok=True)
        self.run_shell("cd ~/src\ntrue marker\n")
        by_cmd = self.by_cmd()
        self.assertEqual(by_cmd["true marker"].cwd, "~/src")

    def test_path_outside_home_stays_absolute(self) -> None:
        self.run_shell("cd /tmp\ntrue marker\n")
        by_cmd = self.by_cmd()
        self.assertEqual(by_cmd["true marker"].cwd, "/tmp")

    def test_sibling_of_home_is_not_mangled(self) -> None:
        # The trap in ${PWD/#$HOME/~}: with $HOME=/home/martinus it rewrites
        # /home/martinuscopy to ~copy. Anchored matching must leave it alone.
        sibling = self.root.parent / f"{self.root.name}-sibling"
        sibling.mkdir(exist_ok=True)
        self.addCleanup(sibling.rmdir)
        self.run_shell(f"cd {sibling}\ntrue marker\n")
        by_cmd = self.by_cmd()
        self.assertEqual(by_cmd["true marker"].cwd, str(sibling))

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
class TestCoexistence(ShellHookTestCase):
    """woswoar is never the only thing hooked into a real .bashrc.

    A terminal-title hook, a prompt framework, bash-preexec, atuin and ble.sh
    all want the DEBUG trap or PROMPT_COMMAND. Every case here is something that
    was actually broken.
    """

    #: The shape of a common .bashrc: a title hook owning both. `_title_prompt`
    #: prints `$?` because that is what a real prompt does with it, and it is
    #: the only way to notice woswoar handing everyone downstream a 0.
    PRIOR_TRAP = """
        _title_preexec() { printf 'PREEXEC[%s]\\n' "$BASH_COMMAND"; }
        _title_prompt() { printf 'PROMPT[%s]\\n' "$?"; }
        trap '_title_preexec' DEBUG
    """
    PRIOR = (
        PRIOR_TRAP + '        PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }_title_prompt"\n'
    )

    def test_an_existing_debug_trap_still_fires(self) -> None:
        # A bare `trap ... DEBUG` in the hook silently replaced it, so the other
        # tool simply stopped working with nothing to show for it.
        out = self.run_shell("echo hello\n", before=self.PRIOR)
        self.assertIn("PREEXEC[echo hello]", out)
        self.assertEqual(self.commands(), ["echo hello"])

    def test_a_prior_trap_survives_a_scratch_file_that_cannot_be_written(self) -> None:
        """Issue #57: chaining must not depend on a file round trip.

        The prior handler's spec used to travel from the boot string to
        `__woswoar_wire` through the scratch file. A shell where that file could
        not be written therefore read an *empty* spec, concluded there was no
        prior trap, and replaced whoever owned it -- so the other tool stopped
        working, silently, for the life of the shell.

        Recording is genuinely off in this shell, which is the honest outcome:
        the file it captures `history 1` into is gone. What must not also be lost
        is somebody else's DEBUG trap.
        """
        # An ordinary PROMPT_COMMAND entry, which woswoar orders *ahead* of its
        # own boot string -- so it breaks the file in the window where the boot
        # string used to write the trap spec into it. It announces itself
        # because its guard makes silence ambiguous: were `__woswoar_scratch`
        # ever renamed, `_sabotage` would return 0 having sabotaged nothing and
        # this test would pass while asserting what its sibling above already
        # asserts.
        sabotage = """
            _sabotage() {
                [[ -n ${__woswoar_scratch-} ]] || return 0
                rm -f -- "$__woswoar_scratch" && mkdir -p -- "$__woswoar_scratch"
                printf 'SABOTAGED\\n'
            }
            PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }_sabotage"
        """
        out = self.run_shell("echo hello\n", before=self.PRIOR + sabotage)
        self.assertIn("SABOTAGED", out)
        self.assertIn("PREEXEC[echo hello]", out)
        self.assertEqual(self.commands(), [], "the scratch file survived the sabotage")

    def test_the_boot_entry_removes_itself_from_every_shape(self) -> None:
        """It has done its job after the first prompt, and it is not free.

        `__woswoar_unboot` matches the boot text exactly, once per branch of the
        PROMPT_COMMAND assembly, so each branch can miss on its own. What a miss
        costs is measured in the comment above `__woswoar_unboot` -- ~12us of
        re-testing a flag on every command -- and until #57 that was the whole
        price. It is now also the difference between forking once at the first
        prompt and forking at every one, which is why the shapes are enumerated
        rather than represented by the bare shell.
        """
        shapes = {
            "bare": "",
            "string": "_other() { :; }\nPROMPT_COMMAND=_other",
            "array": "_other() { :; }\nPROMPT_COMMAND=(_other)",
        }
        for label, before in shapes.items():
            with self.subTest(shape=label):
                out = self.run_shell(
                    'echo one\nprintf "PC:%s\\n" "${PROMPT_COMMAND[*]}"\n', before=before
                )
                # Searched over the whole output rather than one captured line:
                # the string form joins its entries with newlines, so the value
                # is not a line. Nothing else the shell prints or echoes back
                # contains this name, and `PC:` proves the printf itself ran.
                self.assertIn("PC:", out)
                self.assertNotIn("__woswoar_wire", out)

    def test_a_trap_that_could_not_be_read_installs_nothing(self) -> None:
        """An unreadable prior trap must not be mistaken for an absent one.

        The spec reaches `__woswoar_wire` through a command substitution, and a
        substitution that cannot fork yields an empty string -- which read as
        "nobody owns DEBUG" would clobber the owner. That is the same silent
        failure the scratch file used to cause, reached by the other route, so
        the empty case has to mean "I could not look" and install nothing.

        Driven by calling `__woswoar_wire` with an empty spec directly: the real
        trigger is `fork` failing under `RLIMIT_NPROC`, which a test cannot ask
        for without deciding how the rest of the shell behaves once it is there.
        """
        out = self.run_shell(
            "__woswoar_wired=\n"
            "trap '_title_preexec' DEBUG\n"
            "__woswoar_wire ''\n"
            "printf 'TRAP[%s]\\n' \"$(trap -p DEBUG)\"\n"
            "echo hello\n",
            before=self.PRIOR_TRAP,
        )
        # The trap is read back in the same shell: a second `run_shell` would be
        # a fresh one, where nothing had gone wrong and woswoar wired normally.
        self.assertIn("TRAP[trap -- '_title_preexec' DEBUG]", out)
        self.assertIn("PREEXEC[echo hello]", out)

    def test_a_prior_trap_survives_an_array_prompt_command(self) -> None:
        """bash 5.1's array form is a separate branch, and it was untested.

        Coverage, not a regression test for #57: it passes with #57 reverted
        too. It is here because `__woswoar_boot` is an array *element* on this
        branch rather than part of a newline-joined string, and #57 put a command
        substitution inside it -- which made it worth establishing that an
        element is evaluated somewhere that can see the parent's DEBUG trap.
        Nothing covered that before, for either shape of the boot string.
        """
        array = self.PRIOR_TRAP + "        PROMPT_COMMAND=(_title_prompt)\n"
        out = self.run_shell("sleep 0.2\n", before=array)
        self.assertIn("PREEXEC[sleep 0.2]", out)
        self.assertIn("PROMPT[0]", out)
        # A duration, not merely a recorded command: `__woswoar_precmd` captures
        # from `history 1` and would record the command even if nothing had ever
        # wired the trap. Only a start time proves `__woswoar_preexec` fired,
        # which is the half this branch is responsible for installing.
        self.assertGreaterEqual(self.by_cmd()["sleep 0.2"].duration_ms, 100)

    def test_durations_survive_chaining(self) -> None:
        self.run_shell("sleep 0.2\n", before=self.PRIOR)
        by_cmd = self.by_cmd()
        self.assertGreaterEqual(by_cmd["sleep 0.2"].duration_ms, 100)

    def test_exit_codes_survive_a_prior_prompt_command(self) -> None:
        """`$?` is only the user's status for the *first* PROMPT_COMMAND entry.

        With anything already in PROMPT_COMMAND, reading `$?` inside our own
        entry yielded the previous entry's status instead -- so every command
        was recorded as having succeeded. The fix is a bare assignment
        prepended ahead of everything else.
        """
        self.run_shell("false\ntrue\n", before=self.PRIOR)
        by_cmd = self.by_cmd()
        self.assertEqual(by_cmd["false"].exit_code, 1)
        self.assertEqual(by_cmd["true"].exit_code, 0)

    def test_recording_survives_losing_the_debug_trap(self) -> None:
        """Something claiming DEBUG *after* us must cost timing, not history.

        Recording used to be gated on a flag the trap set, so one later
        `trap ... DEBUG` anywhere in .bashrc turned woswoar off silently.
        """
        self.run_shell("_o() { :; }\ntrap '_o' DEBUG\necho after-one\necho after-two\n")
        commands = self.commands()
        self.assertIn("echo after-one", commands)
        self.assertIn("echo after-two", commands)
        by_cmd = self.by_cmd()
        # Duration is the one thing that legitimately degrades.
        self.assertEqual(by_cmd["echo after-one"].duration_ms, -1)

    def test_downstream_prompt_entries_still_see_the_real_status(self) -> None:
        """Capturing `$?` must not consume it.

        woswoar prepends its capture, which puts it in the one slot only one
        participant can have. An assignment succeeds, so without restoring the
        status afterwards every *other* PROMPT_COMMAND entry -- an exit-code
        colouring prompt, `__git_ps1` -- would see 0 for every command. That is
        the bug woswoar prepends the capture to avoid, handed outward.
        """
        out = self.run_shell("false\n", before=self.PRIOR)
        self.assertIn("PROMPT[1]", out)
        self.assertEqual(self.by_cmd()["false"].exit_code, 1)

    def test_a_prior_handler_containing_quotes_is_not_mangled(self) -> None:
        """The quotes must be in the *handler*, not merely in a function it calls.

        `trap -p` requotes whatever it prints, so a handler that is just a
        function name survives even a naive unquoter. This is the case that
        actually distinguishes them.
        """
        before = """
            trap 'printf "QUOTED[%s]\\n" "it'\\''s fine"' DEBUG
        """
        out = self.run_shell("echo hello\n", before=before)
        self.assertIn("QUOTED[it's fine]", out)
        self.assertEqual(self.commands(), ["echo hello"])

    #: The two things ble.sh gives a consumer, and the only two the hook uses.
    #: Enough to pin *our* branch; the real thing was verified by hand against
    #: ble.sh 0.4 in a pty, which CI has no way to install.
    BLESH_STUB = """
        BLE_VERSION=0.4.0-stub
        blehook() { printf '%s\\n' "$1" >> "$BLEHOOK_LOG"; }
    """

    def blehook_registrations(self, log: Path) -> list[str]:
        return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []

    def test_under_blesh_we_register_with_blehook(self) -> None:
        """ble.sh does not run PROMPT_COMMAND or fire DEBUG on user commands.

        It replaces bash's prompt machinery outright, so the hook's normal
        wiring records nothing at all -- silently, because every part of it
        still looks installed. Recording was completely dead on any machine
        that loads ble.sh, which is the usual way to run atuin on bash.
        """
        log = self.root / "blehook.log"
        self.run_shell(
            "true\n",
            env_extra={"BLEHOOK_LOG": str(log)},
            before=self.BLESH_STUB,
        )
        registered = self.blehook_registrations(log)
        self.assertIn("PREEXEC+=__woswoar_preexec", registered)
        self.assertIn("PRECMD+=__woswoar_precmd", registered)

    def test_without_blesh_we_do_not_call_blehook(self) -> None:
        # The branch has to be a branch, not a belt-and-braces both.
        log = self.root / "blehook.log"
        self.run_shell(
            "true\n",
            env_extra={"BLEHOOK_LOG": str(log)},
            before='blehook() { printf "%s\\n" "$1" >> "$BLEHOOK_LOG"; }',
        )
        self.assertEqual(self.blehook_registrations(log), [])
        self.assertEqual(self.commands(), ["true"])

    def test_the_prompt_at_which_the_hook_loads_records_nothing(self) -> None:
        # `history 1` at that point is whatever the previous session left
        # behind. The harness types `source .../woswoar.bash`, which used to be
        # subtracted in recorded() rather than not recorded in the first place.
        self.run_shell("echo only-this\n")
        self.assertEqual(self.commands(), ["echo only-this"])


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
    def test_custom_ignore_pattern(self) -> None:
        self.run_shell("echo keep-me\necho drop-me\n", env_extra={"WOSWOAR_IGNORE": "drop-me"})
        self.assertEqual(self.commands(), ["echo keep-me"])

    def test_an_extra_pattern_adds_to_the_default_rather_than_replacing_it(self) -> None:
        """Otherwise one custom rule silently freezes the built-in protection.

        The default grew because it missed `AWS_SECRET_ACCESS_KEY=`; a user who
        had pasted the old one into ~/.bashrc to add a rule would never see that.
        """
        self.run_shell(
            "deploy-to-prod\nexport GITHUB_TOKEN=ghp_x\necho safe\n",
            env_extra={"WOSWOAR_IGNORE_EXTRA": "deploy-to-prod"},
        )
        self.assertEqual(self.commands(), ["echo safe"])

    def test_histcontrol_ignorespace_is_respected(self) -> None:
        # The hook defers to bash's own history rules rather than reimplementing
        # them, so a leading space is skipped for free.
        self.run_shell(
            "HISTCONTROL=ignorespace\n echo hidden\necho visible\n",
        )
        self.assertNotIn(" echo hidden", self.commands())
        self.assertIn("echo visible", self.commands())

    def test_an_aws_secret_never_reaches_the_log(self) -> None:
        """The case from #23, driven all the way through a real bash.

        `AWS_SECRET_ACCESS_KEY=` is the shape everyone assumes is covered, and
        it was not: the old pattern needed the keyword flush against the `=`.
        """
        self.run_shell("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI\necho safe\n")
        self.assertEqual(self.commands(), ["echo safe"])

        logs = (Path(os.environ["WOSWOAR_DIR"]) / "logs").rglob("*.tsv")
        for path in logs:
            self.assertNotIn("wJalrXUtnFEMI", path.read_text(encoding="utf-8"))

    def test_a_pathological_command_does_not_stall_the_prompt(self) -> None:
        """A long run of dashes must not make the filter quadratic.

        The first version of this pattern used `--[a-z-]*`, which can start a
        match attempt at every interior `-` and rescan the rest each time. 8000
        dashes measured 654ms for a single command -- paid on the prompt, and
        before the truncation at `__woswoar_max` gets a chance to shorten
        anything.

        The match itself is timed rather than the whole shell: one bash startup
        is ~100ms, which would swallow the very regression this guards. Ten
        iterations cost ~6ms healthy and ~6.5s quadratic, so the bound below has
        two orders of magnitude of headroom in both directions.
        """
        out = self.run_shell(
            'line=$(printf -- "-%.0s" {1..8000})\n'
            "t0=${EPOCHREALTIME//[.,]/}\n"
            "for _ in {1..10}; do [[ $line =~ $WOSWOAR_IGNORE ]]; done\n"
            "t1=${EPOCHREALTIME//[.,]/}\n"
            'printf "ELAPSED %s\\n" "$(( t1 - t0 ))"\n'
        )
        match = re.search(r"^ELAPSED (\d+)$", out, re.MULTILINE)
        assert match is not None, f"the shell did not report a timing:\n{out}"
        micros = int(match.group(1))
        self.assertLess(micros, 1_000_000, "the ignore pattern went superlinear again")


@requires_bash5
class TestTheDefaultIgnorePattern(ShellHookTestCase):
    """What the shipped pattern does and does not catch.

    Evaluated by the same bash that will run it, against the value the hook
    actually sets -- not against a copy in this file, which could drift from the
    hook and keep passing. One shell tests the whole corpus, because starting a
    bash per command would cost more than the rest of the suite.
    """

    def classify(self, commands: list[str]) -> dict[str, bool]:
        """Ask a real bash which of `commands` the default pattern matches."""
        array = " ".join(shlex.quote(command) for command in commands)
        out = self.run_shell(
            f"__corpus=({array})\n"
            'printf "PATTERN %s\\n" "${#WOSWOAR_IGNORE}"\n'
            'for i in "${!__corpus[@]}"; do\n'
            '  [[ ${__corpus[i]} =~ $WOSWOAR_IGNORE ]] && printf "MATCH %s\\n" "$i" '
            '|| printf "KEEP %s\\n" "$i"\n'
            "done\n"
        )

        self.assertRegex(out, r"PATTERN [1-9]", "the hook set no default pattern")
        verdicts = {}
        for line in out.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] in {"MATCH", "KEEP"}:
                verdicts[commands[int(fields[1])]] = fields[0] == "MATCH"
        self.assertEqual(len(verdicts), len(commands), "some commands were not classified")
        return verdicts

    def test_every_credential_shape_is_dropped(self) -> None:
        verdicts = self.classify(SECRET_SHAPES)
        missed = [command for command, matched in verdicts.items() if not matched]
        self.assertEqual(missed, [], f"{len(missed)} secret-bearing commands would be recorded")
        # Counted, not just "nothing missed": a harness that classified nothing
        # would satisfy the assertion above while testing the pattern not at all.
        self.assertEqual(sum(verdicts.values()), len(SECRET_SHAPES))

    def test_the_documented_gaps_really_are_gaps(self) -> None:
        """Pin the "what it does not catch" section to the pattern's behaviour."""
        verdicts = self.classify(DOCUMENTED_GAPS)
        caught = [command for command, matched in verdicts.items() if matched]
        self.assertEqual(
            caught,
            [],
            "the pattern now catches something docs/shell-integration.md says it does not; "
            "update the document in the same commit",
        )
        self.assertEqual(len(verdicts), len(DOCUMENTED_GAPS))

    def test_no_ordinary_command_is_dropped(self) -> None:
        verdicts = self.classify(INNOCENT_SHAPES)
        eaten = [command for command, matched in verdicts.items() if matched]
        self.assertEqual(eaten, [], f"{len(eaten)} ordinary commands would be silently discarded")
        self.assertEqual(len(verdicts), len(INNOCENT_SHAPES))


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

    def clone_count(
        self,
        command_count: int,
        env_extra: dict[str, str] | None = None,
        before: str = "",
    ) -> int:
        script = "".join(f"echo cmd{i}\n" for i in range(command_count))
        proc = subprocess.run(
            ["strace", "-f", "-c", "-e", "trace=clone,clone3,vfork,fork", "bash", "--norc", "-i"],
            input=f"{textwrap.dedent(before)}\nsource {HOOK}\n{script}",
            text=True,
            env=self.shell_env(env_extra),
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
        # Not "zero forks": startup legitimately runs mkdir and two `trap -p`
        # subshells -- one to see whether the EXIT trap is free, one at the first
        # prompt to read any prior DEBUG trap. What matters is that the
        # per-command path adds nothing, so the count must be identical for 3 and
        # for 30 commands. That also pins the boot string removing itself: were
        # it left in PROMPT_COMMAND, its substitution would fork every time.
        #
        # Both scratch-file branches, because they are chosen at startup and a
        # fork added to either would be paid on every command of that shell.
        for label, env in (("runtime dir", None), ("fallback", {"XDG_RUNTIME_DIR": ""})):
            with self.subTest(scratch=label):
                few = self.clone_count(3, env)
                many = self.clone_count(30, env)
                self.assertEqual(
                    few,
                    many,
                    f"record path forks: {few} clones for 3 commands, {many} for 30",
                )

    #: Each takes a different branch of the PROMPT_COMMAND assembly, and the
    #: last two are shapes where `__woswoar_unboot`'s exact-match removal can
    #: miss -- which is when a boot entry stays and forks at every prompt.
    SHAPES: ClassVar[dict[str, str]] = {
        "array": "PROMPT_COMMAND=(_other)\n_other() { :; }",
        "a prior string entry": "_other() { :; }\nPROMPT_COMMAND=_other",
        "an entry rewritten after us": (
            "_other() { PROMPT_COMMAND=${PROMPT_COMMAND//$'\\n'/$'\\n'}; }\nPROMPT_COMMAND=_other"
        ),
        "ble.sh": "BLE_VERSION=0.4.0-stub\nblehook() { :; }",
    }

    def test_no_prompt_command_shape_forks_per_prompt(self) -> None:
        """The one fork the hook has is at the first prompt; it must stay there.

        `__woswoar_boot` forks since #57, and it removes itself by matching its
        own text in `PROMPT_COMMAND` -- a variable prompt frameworks rewrite
        freely. A miss used to cost a redirect; now it would cost a fork on every
        prompt for the life of the shell. The bare-shell case above never
        exercises a miss, because there is nothing else in the variable.
        """
        for label, before in self.SHAPES.items():
            with self.subTest(shape=label):
                few = self.clone_count(3, before=before)
                many = self.clone_count(30, before=before)
                self.assertEqual(few, many, f"{label}: {few} clones for 3 commands, {many} for 30")


@requires_bash5
class TestRecordingIsPrivate(ShellHookTestCase):
    """The hook creates the log file, so the hook is where its mode is decided.

    Python never touches that file on the recording path, so a fix in `store`
    alone would not have covered the case this exists for.
    """

    def test_the_day_file_is_owner_only_under_a_stock_umask(self) -> None:
        self.run_shell("echo hello\n")
        files = list((Path(os.environ["WOSWOAR_DIR"]) / "logs" / "hosts").rglob("*.tsv"))
        self.assertTrue(files, "nothing was recorded")
        for path in files:
            self.assertEqual(path.stat().st_mode & 0o077, 0, f"{path} is world-readable")

    def test_the_hook_agrees_with_store_about_what_private_means(self) -> None:
        """The hook is copied verbatim, so it cannot import the constants.

        Nothing else would notice `store.DIR_MODE` being relaxed and the hook
        being left behind, which is the direction that silently reopens the
        hole this class exists for.
        """
        source = HOOK.read_text(encoding="utf-8")
        self.assertIn("umask 077", source)
        self.assertEqual(store.DIR_MODE, 0o700)
        self.assertEqual(store.FILE_MODE, 0o600)

    def test_the_hook_leaves_the_shell_umask_alone(self) -> None:
        """It drops to 077 to create the file; it must put it back.

        Otherwise every file the user creates afterwards would silently become
        0600 -- a surprising thing for a history tool to do to a shell.
        """
        out = self.run_shell("echo start\numask\n")
        self.assertIn("0022", out)


@requires_bash5
class TestTheScratchFileIsPrivate(ShellHookTestCase):
    """Issue #24: the scratch file's directory decides who can touch it.

    Its contents are read back on every command, so what is pinned here is the
    *directory*, not the filename -- the name is only a pid. Until #57 the file
    also carried the trap spec that `__woswoar_wire` evaluates; it does not any
    more, which is one fewer reason to care what is in it, not a reason to stop
    caring who can write it.
    """

    HOSTILE: ClassVar[dict[str, str]] = {
        "XDG_RUNTIME_DIR": "",
        "TMPDIR": "/nonexistent/hostile",
    }

    def scratch(self, env_extra: dict[str, str] | None = None) -> str:
        return self.shell_value('"$__woswoar_scratch"', env_extra)

    def test_it_lives_in_the_runtime_dir_when_there_is_one(self) -> None:
        self.assertTrue(self.scratch().startswith(str(self.runtime_dir())))

    def test_tmpdir_and_tmp_are_never_consulted(self) -> None:
        """A predictable name in a world-writable directory is the whole bug.

        The TMPDIR here is perfectly usable, which is the point: an unusable one
        would prove nothing, because the fallback below would rescue it and the
        scratch file would land in the right place for the wrong reason.
        """
        usable = self.root / "tmpdir"
        usable.mkdir(exist_ok=True)
        env = {"XDG_RUNTIME_DIR": "", "TMPDIR": str(usable)}

        path = self.scratch(env)
        self.assertFalse(path.startswith(str(usable)), f"TMPDIR was consulted: {path}")
        self.assertTrue(path.startswith(os.environ["WOSWOAR_DIR"]))
        self.assertEqual(list(usable.iterdir()), [], "the hook wrote into TMPDIR")

        self.run_shell("echo still-recording\n", env_extra=env)
        self.assertIn("echo still-recording", self.commands())

    def test_an_unwritable_runtime_dir_falls_back_instead_of_giving_up(self) -> None:
        """XDG_RUNTIME_DIR is inherited, not verified.

        `su` hands over the calling user's, which this uid cannot write. Bailing
        there switched recording off for the whole shell with no message -- the
        same silent failure as a squatted /tmp path, reached from the other end.
        """
        unwritable = self.root / "unwritable"
        unwritable.mkdir(exist_ok=True)
        unwritable.chmod(0o500)
        self.addCleanup(unwritable.chmod, 0o700)

        env = {"XDG_RUNTIME_DIR": str(unwritable)}
        self.assertTrue(self.scratch(env).startswith(os.environ["WOSWOAR_DIR"]))

        self.run_shell("echo still-recording\n", env_extra=env)
        self.assertIn("echo still-recording", self.commands())

    def test_the_file_and_its_directory_are_owner_only(self) -> None:
        """Stats from Python, not `stat -c`, which is GNU-only.

        `before` gives the EXIT trap away, so the hook declines to claim it and
        the file outlives the shell -- which is also the case that leaves one
        behind, and the reason it lives in `run/` rather than the tree root.
        """
        self.run_shell("echo hello\n", env_extra=self.HOSTILE, before="trap ':' EXIT")

        run_dir = Path(os.environ["WOSWOAR_DIR"]) / "run"
        left = list(run_dir.glob("woswoar-hist.*"))
        self.assertTrue(left, "no scratch file survived, so this asserts nothing")
        self.assertEqual(left[0].stat().st_mode & 0o077, 0, "readable by others")
        self.assertEqual(run_dir.stat().st_mode & 0o077, 0, "its directory is reachable")


if __name__ == "__main__":
    unittest.main()


@requires_bash5
class TestALostLogDirectory(ShellHookTestCase):
    """Reported from a real machine, on v0.4.1:

        $ git s
        ## mla/OA-...  [gone]
        -bash: /home/.../logs/hosts/76326ac.../2026-08-03.tsv: No such file or directory

    The hook makes its log directory when the shell starts, and a shell outlives
    a lot -- an uninstall, a home directory moved, a work machine's cleanup. Once
    it is gone, every command in every open shell writes into nothing, and said
    so at the prompt, every time.
    """

    def logdir(self) -> Path:
        return Path(os.environ["WOSWOAR_DIR"]) / "logs" / "hosts" / MACHINE_ID

    def test_losing_it_mid_session_is_silent(self) -> None:
        """Recording may stop. It may not talk about it.

        The redirection order is the whole of this: `>>file 2>/dev/null` applies
        the append first, so bash reports its failure to a stderr it has not
        redirected yet.
        """
        out = self.run_shell(
            f"""
            echo first
            rm -rf {self.logdir()}
            echo second
            echo third
            """
        )
        self.assertNotIn("No such file or directory", out)
        self.assertIn("third", out, "precondition: the shell kept running")

    def test_it_comes_back_when_the_day_turns(self) -> None:
        """The directory is remade on the once-a-day path, so a shell that was
        open when it vanished records again rather than staying broken until
        someone thinks to restart it."""
        target = self.logdir()
        out = self.run_shell(
            f"""
            echo before
            rm -rf {target}
            __woswoar_today=not-today
            echo after
            """
        )
        self.assertNotIn("No such file or directory", out)
        self.assertTrue(target.is_dir(), "the log directory was not remade")
        # The command as typed, which is what the hook records -- not its output.
        self.assertIn("echo after", self.commands(), f"recording did not resume: {self.commands()}")

    def test_the_remade_directory_is_owner_only(self) -> None:
        """Remade under the same umask as the original, or an uninstall followed
        by a day boundary quietly downgrades what is recorded afterwards."""
        target = self.logdir()
        self.run_shell(
            f"""
            echo before
            rm -rf {target}
            __woswoar_today=not-today
            echo after
            """
        )
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
