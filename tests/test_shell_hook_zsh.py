"""Contract tests for the zsh hook.

A sibling of ``tests/test_shell_hook.py``, not a rewrite of it. What the two
hooks must agree on -- the escape function, the two constants mirrored from
Python, the credential filter, the fork count -- is asserted by mixins that live
in that file and are subclassed here, so there is one corpus and one set of
assertions for both shells.

What is left is the part where zsh is genuinely a different shell, and it is
almost all about *not recording*. bash hands the hook a signal for "I declined
to store that line" -- the history number did not move -- and woswoar.bash
defers to it, which buys HISTCONTROL, `ignoredups` and HISTIGNORE for nothing.
zsh offers no such signal: $HISTCMD moves backwards over an ignored line and
$history still holds a space-hidden command while `preexec` runs. So the hook
reimplements those rules, and every one of them is a test here -- HIST_IGNORE_SPACE
most of all, because that is how a person hides a secret and a miss publishes it
to a git repository.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import ClassVar

from woswoar import search

from .support import MACHINE_ID, requires_bash5
from .test_shell_hook import (
    BASH_HOOK,
    ZSH_HOOK,
    CaptureMixin,
    CloneScalingMixin,
    ConstantParityMixin,
    CtrlROnThePromptMixin,
    DefaultIgnorePatternMixin,
    EscapeParityMixin,
    PromptSyncMixin,
    RecordingIsPrivateMixin,
    ShellHookTestCase,
)


def zsh_major() -> int:
    zsh = shutil.which("zsh")
    if not zsh:
        return 0
    out = subprocess.run(
        [zsh, "-f", "-c", "echo ${ZSH_VERSION%%.*}"], capture_output=True, text=True, check=False
    )
    return int(out.stdout.strip() or 0)


requires_zsh5 = unittest.skipUnless(zsh_major() >= 5, "zsh 5.0+ required")


class ZshHookTestCase(ShellHookTestCase):
    """The bash harness, pointed at zsh.

    ``-f`` rather than bash's ``--norc``: same idea, no startup files, so the
    only thing in this shell is what the test put there.
    """

    INTERPRETER: ClassVar[list[str]] = ["zsh", "-f", "-i"]
    HOOK: ClassVar[Path] = ZSH_HOOK

    #: bash draws no prompt when its stdin is a pipe. zsh does, and decorates
    #: it: `PROMPT_SP` prints an inverse `%` for a partial line and `PROMPT_CR` a
    #: carriage return, so the first thing a test's own `printf` shares a line
    #: with is a `\r` and a `%`. That is not cosmetic -- assertions here match
    #: whole lines, and `for> for> KEEP 0` is not the line the test is looking
    #: for. PS2 is cleared for the same reason: it is what puts those `for>`s in
    #: front of the first line a multi-line command prints.
    #:
    #: The trailing `print` flushes the prompts drawn *before* this line took
    #: effect onto a line of their own, so every line after it is the shell's
    #: alone. It runs before the hook is sourced, so it is never recorded.
    PREAMBLE: ClassVar[str] = "PS1=; PS2=; unsetopt promptcr promptsp; print\n"

    def assertRecorded(self, expected: list[str]) -> None:
        """Which commands were recorded, and how many times each -- not in which order.

        `recorded()` sorts by `(ts, duration_ms)`, and three `echo`s inside one
        second share a timestamp and differ only in a duration of 0 or 1
        milliseconds. So the order it returns is decided by which of them
        happened to take the extra millisecond, and asserting on it is a flake
        that reproduces about once in ten runs -- observed, not predicted.

        Sorting both sides costs nothing here: every claim in this file is about
        the set of commands that survived a history rule, never about their
        order.
        """
        self.assertEqual(sorted(self.commands()), sorted(expected))


@requires_zsh5
class TestCapture(CaptureMixin, ZshHookTestCase):
    """`CaptureMixin` carries every claim the two shells share. What is left here
    is where zsh differs: it is handed the command line rather than reading it
    back, and its `$?` has to be caught before `emulate` clobbers it."""

    def test_a_multiline_loop_is_one_record(self) -> None:
        """The one place the two shells legitimately record different text.

        bash reads the line back out of `history`, which joins it with
        semicolons; zsh is handed the buffer the user typed, newlines and all.
        Both are faithful to their shell, so this is a per-shell expectation
        rather than a shared one.

        It is also the only test in either suite where a real newline reaches
        `__woswoar_escape` through a shell. Drop the `\\n` substitution and this
        record becomes three lines, none of which parses.
        """
        self.run_shell("for i in 1 2; do\ntrue $i\ndone\n")
        raw = self.raw_logs()
        self.assertEqual(raw.count("\n"), 1, f"the record did not stay on one line: {raw!r}")
        self.assertTrue(raw.rstrip("\n").endswith(r"for i in 1 2; do\ntrue $i\ndone"), raw)
        # And what comes back out carries a *visible* `\n`, which is
        # `entry.make_inert` and not a failure to unescape: a recalled multi-line
        # command must not arrive in the edit buffer as several commands.
        self.assertEqual(self.commands(), [r"for i in 1 2; do\ntrue $i\ndone"])

    def test_a_function_definition_is_recorded_whole(self) -> None:
        """`preexec` is handed the command line twice, and $2 is lossy.

        $2 is the "expanded" form, and it renders a function body as
        `f () { ... }` -- the three dots literally. Reading it instead of $1
        would record a definition nobody can get back.
        """
        self.run_shell("_marker() { echo inside-the-body }\n")
        self.assertEqual(self.commands(), ["_marker() { echo inside-the-body }"])

    def test_exit_codes_are_recorded(self) -> None:
        """`emulate -L zsh` clobbers $?, so the capture has to come first.

        Get the order wrong and every command in the log is a success -- the
        same class of bug woswoar.bash carries a prepended PROMPT_COMMAND entry
        to avoid, reached by a different route.
        """
        self.run_shell("false\ntrue\n(exit 42)\n")
        by_cmd = self.by_cmd()
        self.assertEqual(by_cmd["false"].exit_code, 1)
        self.assertEqual(by_cmd["true"].exit_code, 0)
        self.assertEqual(by_cmd["(exit 42)"].exit_code, 42)

    def test_the_hook_returns_the_status_it_was_given(self) -> None:
        """Called directly, because that is the only way to see the return value.

        zsh hands each `precmd_functions` entry the user's own $? regardless of
        what the entry before it returned -- observed on 5.9, and the reason the
        sibling test below passes either way. Whether that is a promise or an
        implementation detail is not written down anywhere, so the hook returns
        the status explicitly; this is what says the line is doing something.
        """
        out = self.run_shell("false; __woswoar_precmd; printf 'RC[%s]\\n' \"$?\"\n")
        self.assertIn("RC[1]", out)

    def test_a_later_precmd_hook_still_sees_the_real_status(self) -> None:
        """The property a user's exit-code-colouring prompt depends on."""
        before = """
            autoload -Uz add-zsh-hook
            _later() { printf 'LATER[%s]\\n' "$?" }
            add-zsh-hook precmd _later
        """
        out = self.run_shell("false\n", before=before)
        self.assertIn("LATER[1]", out)
        self.assertEqual(self.by_cmd()["false"].exit_code, 1)


@requires_zsh5
class TestTheHistoryRulesZshDoesNotHandOver(ZshHookTestCase):
    """The rules bash gives woswoar for free and zsh does not.

    Every one of these was verified to *need* reimplementing before it was
    written: `preexec` fires for space-prefixed, duplicate and
    HISTORY_IGNORE-matching lines alike, and none of the three signals zsh does
    offer can tell them apart. $HISTCMD read at `precmd` over
    `echo a` / ` echo spaced` / `echo a` / `echo a` / `echo b` goes 5, 6, 5, 5, 6
    -- so "did it advance" drops the next real command after every ignored one.
    """

    def test_hist_ignore_space_is_respected(self) -> None:
        """A leading space is how a person keeps a command out of history.

        In woswoar a missed one is not merely recorded, it is encrypted, pushed
        to a git repository and pulled onto every other machine. So this asserts
        against the bytes on disk rather than against `commands()`, which would
        also pass if the parser happened to drop the line for some other reason.
        """
        out = self.run_shell(
            " echo hidden-marker\necho visible-marker\n", before="setopt hist_ignore_space"
        )
        self.assertIn("hidden-marker", out, "precondition: the command really ran")
        self.assertNotIn("hidden-marker", self.raw_logs())
        self.assertIn("echo visible-marker", self.commands())

    def test_hist_ignore_space_off_records_the_space_prefixed_line(self) -> None:
        """Otherwise the test above passes against a hook that drops every line
        beginning with whitespace, which is a different and much worse rule.

        The `echo first` is not decoration: `run_shell` puts the script through
        `textwrap.dedent`, so a lone indented line has its indentation removed
        before the shell ever sees it -- and this test would then assert nothing
        while looking exactly as it does now.
        """
        self.run_shell("echo first\n echo spaced-marker\n")
        self.assertIn(" echo spaced-marker", self.commands())

    def test_hist_ignore_dups_is_respected(self) -> None:
        self.run_shell("echo twice\necho twice\necho after\n", before="setopt hist_ignore_dups")
        self.assertRecorded(["echo twice", "echo after"])

    def test_hist_ignore_all_dups_is_respected(self) -> None:
        self.run_shell("echo twice\necho twice\necho after\n", before="setopt hist_ignore_all_dups")
        self.assertRecorded(["echo twice", "echo after"])

    def test_a_repeat_is_recorded_when_neither_option_is_set(self) -> None:
        """The default. Without this the two above pass against a hook that
        drops every repeated command, which is not what either option means."""
        self.run_shell("echo twice\necho twice\n")
        self.assertRecorded(["echo twice", "echo twice"])

    def test_an_ignored_line_does_not_hide_the_command_after_it(self) -> None:
        """The failure mode of every "did the history move" scheme on zsh.

        $HISTCMD goes *backwards* over an ignored line, so a hook that watched it
        would drop the next real command as well -- silently losing history that
        the user did nothing to hide.

        `echo two` twice, then `echo three`: the duplicate is what the option
        asks to be dropped, and `echo three` is what says the run recovered
        rather than stopping at the hidden line.
        """
        self.run_shell(
            "echo one\n echo hidden\necho two\necho two\necho three\n",
            before="setopt hist_ignore_space hist_ignore_dups",
        )
        self.assertRecorded(["echo one", "echo two", "echo three"])

    def test_history_ignore_is_respected(self) -> None:
        self.run_shell("cd /tmp\necho kept\n", before='HISTORY_IGNORE="(ls|cd *)"')
        self.assertEqual(self.commands(), ["echo kept"])

    def test_a_history_ignore_in_extended_glob_syntax_is_honoured(self) -> None:
        """`emulate -L zsh` turns EXTENDED_GLOB *off*, and $HISTORY_IGNORE is the
        user's pattern written in whichever dialect their options select.

        `^echo*` means "anything but an echo" with the option on and something
        else entirely with it off, so matching it in the wrong dialect records
        exactly the commands they told zsh to forget. The option is therefore
        read before emulating and put back afterwards.
        """
        self.run_shell(
            "true dropped-marker\necho kept\n",
            before="setopt extended_glob\nHISTORY_IGNORE='^echo*'",
        )
        self.assertEqual(self.commands(), ["echo kept"])

    def test_a_history_ignore_holding_a_command_substitution_runs_nothing(self) -> None:
        """`${~VAR}` is pattern interpretation, not evaluation.

        It reads like `eval` and is not, and the difference is whether a value
        that arrives from the environment can run a command on every prompt.
        `$HISTORY_IGNORE` is an ordinary exported variable, so whatever started
        your shell chooses it.
        """
        canary = self.root / "history-ignore-ran"
        out = self.run_shell("echo marker\n", env_extra={"HISTORY_IGNORE": f"$(touch {canary})"})
        self.assertIn("marker", out)
        self.assertFalse(canary.exists(), "the pattern was evaluated as a command")
        self.assertIn("echo marker", self.commands())

    def test_an_aws_secret_never_reaches_the_log(self) -> None:
        """$WOSWOAR_IGNORE is the one filter zsh and bash share outright."""
        self.run_shell("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI\necho safe\n")
        self.assertEqual(self.commands(), ["echo safe"])
        self.assertNotIn("wJalrXUtnFEMI", self.raw_logs())

    def test_the_match_parameters_do_not_leak(self) -> None:
        """A `[[ =~ ]]` sets six parameters, in the user's shell -- unless the
        function declares all six local first. Four of them are `MATCH`,
        `MBEGIN`, `MEND` and `match`, which are the names zsh's own globbing
        uses, so leaking them is not merely untidy.

        The command has to be one the pattern actually *matches*. zsh sets these
        only on a successful match, so an ordinary `echo` leaves them alone
        whether or not they were declared -- which is how this test first
        managed to pass with the six `local`s deleted.
        """
        out = self.run_shell(
            "export GITHUB_TOKEN=ghp_secret\n"
            'printf "LEAK[%s][%s][%s][%s]\\n" '
            '"${MATCH-unset}" "${MBEGIN-unset}" "${MEND-unset}" "${match-unset}"\n'
        )
        self.assertNotIn("ghp_secret", self.raw_logs(), "precondition: the pattern matched")
        self.assertIn("LEAK[unset][unset][unset][unset]", out)


@requires_zsh5
class TestOptionsThatWouldSilenceRecording(ZshHookTestCase):
    """Options a user sets for their own reasons that the hook must survive.

    All of these fail the same way -- the hook loads, `doctor` is happy, and
    nothing is ever written -- which is the failure worth the most tests.
    """

    def test_noclobber_does_not_silence_recording(self) -> None:
        """`setopt noclobber` makes `>>` to a file that does not exist yet *fail*.

        Not a corner: it is a common setting, and the day file does not exist at
        the first command of every day. A zsh user with it set would record
        nothing, ever, and nothing would say so. Guarded twice -- `emulate -L zsh`
        clears NO_CLOBBER for the function, and the append is written `>>|`.
        """
        self.run_shell("echo hello\n", before="setopt noclobber")
        self.assertEqual(self.commands(), ["echo hello"])

    def test_recording_survives_a_shell_full_of_hostile_options(self) -> None:
        """`emulate -L zsh` earns its place here.

        Each of these changes the meaning of something on the record path:
        KSH_ARRAYS the string subscripting, SH_WORD_SPLIT the expansions,
        EXTENDED_GLOB the patterns, NO_UNSET every `${x-}` that is not written
        defensively, and REMATCH_PCRE the credential filter.

        WARN_CREATE_GLOBAL is the odd one out: it breaks nothing, it just talks.
        `__woswoar_escape` assigns a global from inside a function, so without
        `emulate` the user gets a line about it at their prompt after every
        command -- which is the same kind of failure as the redirection message
        above, and is why the output is asserted on and not only the log.
        """
        options = (
            "ksh_arrays sh_word_split extended_glob no_unset"
            " re_match_pcre noclobber warn_create_global"
        )
        out = self.run_shell(
            "echo hello\nexport GITHUB_TOKEN=ghp_secret\n", before=f"setopt {options}"
        )
        self.assertEqual(self.commands(), ["echo hello"])
        self.assertNotIn("ghp_secret", self.raw_logs())
        self.assertNotIn("created globally", out)

    def test_recording_works_without_add_zsh_hook(self) -> None:
        """`add-zsh-hook` is autoloaded from $fpath, and $fpath is a variable.

        A shell that cannot find it would otherwise register neither hook and
        record nothing, so the wiring falls back to appending to the two arrays
        itself -- which is all add-zsh-hook does with the names it is given.
        """
        out = self.run_shell("echo hello\n", before="fpath=()")
        self.assertNotIn("command not found", out)
        self.assertEqual(self.commands(), ["echo hello"])

    def test_the_hook_leaves_the_shell_umask_alone(self) -> None:
        """It drops to 077 to create the day file; it must put it back."""
        out = self.run_shell("echo start\numask\n")
        self.assertIn("022", out)


@requires_zsh5
class TestRecordingIsPrivate(RecordingIsPrivateMixin, ZshHookTestCase):
    def test_every_line_written_is_parseable(self) -> None:
        self.run_shell("echo 'a\tb'\necho 'c\\d'\nfor i in 1; do\ntrue\ndone\necho done\n")
        raw = self.raw_logs()
        self.assertTrue(raw.endswith("\n"))
        for line in raw.splitlines():
            with self.subTest(line=line):
                self.assertEqual(len(line.split("\t")), 6)


@requires_zsh5
class TestALostLogDirectory(ZshHookTestCase):
    """Recording may stop. It may not talk about it.

    zsh reports a failed redirection on the *shell's* stderr rather than the
    command's, so woswoar.bash's fix for this -- ordering `2>/dev/null` ahead of
    the append -- does nothing here, in either order. Only a surrounding block
    catches it, and without one the user gets a path printed at the prompt after
    every command, which is exactly the bug 0.4.1 shipped.
    """

    def test_losing_it_mid_session_is_silent(self) -> None:
        out = self.run_shell(
            f"""
            echo first
            rm -rf {self.logdir()}
            echo second
            echo third
            """
        )
        self.assertNotIn("no such file or directory", out.lower())
        self.assertIn("third", out, "precondition: the shell kept running")

    def test_it_comes_back_when_the_day_turns(self) -> None:
        target = self.logdir()
        self.run_shell(
            f"""
            echo before
            rm -rf {target}
            __woswoar_today=not-today
            echo after
            """
        )
        self.assertTrue(target.is_dir(), "the log directory was not remade")
        self.assertEqual(target.stat().st_mode & 0o077, 0, "and not owner-only")
        self.assertIn("echo after", self.commands(), f"recording did not resume: {self.commands()}")


@requires_zsh5
@requires_bash5
class TestAZshAndABashShellShareOneHistory(ZshHookTestCase):
    """One machine id, one host directory, one day file -- whichever shell wrote it.

    Needs bash **5**, not merely bash. macOS has a bash and it is 3.2, which the
    hook refuses -- so without this gate the class fails there instead of
    skipping, and says "the two shells disagree" about a shell that was never
    recording in the first place.

    This is the case a user reaches by adding zsh to a machine that already runs
    the bash hook, and it needs nothing to be exchanged: both append to the same
    file, and every Ctrl-R re-reads it.
    """

    def run_a_bash_shell(self) -> None:
        """One command through the *other* hook, into the same sandbox.

        Not `run_shell`: that one is this suite's zsh. The point of the class is
        that the two programs land in one file, so one of them has to be spelled
        out.
        """
        subprocess.run(
            ["bash", "--norc", "-i"],
            input=f"source {BASH_HOOK}\necho from_the_bash_shell\n",
            text=True,
            env=self.shell_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )

    def test_each_shell_sees_the_others_commands(self) -> None:
        self.run_shell("echo from_the_zsh_shell\n")
        self.run_a_bash_shell()
        shown = [search.command_from_line(line) for line in search.lines_for("global")]
        self.assertIn("echo from_the_zsh_shell", shown)
        self.assertIn("echo from_the_bash_shell", shown)

        hosts = {entry.host for entry in self.recorded()}
        self.assertEqual(hosts, {MACHINE_ID}, "the two shells disagreed about the machine")

    def test_one_day_file_holds_both(self) -> None:
        """O_APPEND is what makes that safe, and it is a property of the file
        rather than of the shell doing the writing -- but it is only now
        reachable from two different programs at once, so it is worth saying
        that they really do share the file."""
        self.run_shell("echo from_the_zsh_shell\n")
        self.run_a_bash_shell()
        files = sorted(self.logdir().parent.rglob("*.tsv"))
        self.assertEqual(len(files), 1, f"two shells wrote {len(files)} files: {files}")


@requires_zsh5
class TestPromptTriggeredSync(PromptSyncMixin, ZshHookTestCase):
    """`PromptSyncMixin` owns the stand-in, the polling and the claims about
    *when* a sync fires. What is left is what zsh does with a value that is not a
    number, which is not what bash does with one."""

    def test_a_hostile_interval_from_the_environment_never_reaches_arithmetic(self) -> None:
        """`WOSWOAR_SYNC_INTERVAL` is inherited: whatever started your shell sets it.

        This is **not** the bash hazard, and saying so is the point of the test.
        bash runs a command substitution it finds in an arithmetic array
        subscript, so `x[$(...)]` there is code execution; zsh does not, verified
        both ways with the same two lines. What zsh does is print
        `bad math expression` on the shell's *own* stderr -- so at the user's
        prompt, after every command -- and abandon the assignment, which leaves
        the shell not syncing at all. Both of those are what is asserted, because
        both of them can fail.
        """
        out = self.run_shell(
            "echo marker\n", {"WOSWOAR_SYNC_INTERVAL": f"x[$(touch {self.root / 'interval-ran'})]"}
        )
        self.assertIn("marker", out)
        self.assertNotIn("bad math expression", out)
        self.assertTrue(self.stamp.exists(), "it should fall back to the default, not switch off")

    def test_a_leading_zero_in_the_interval_is_read_as_decimal(self) -> None:
        """`08` is a perfectly reasonable way to write eight seconds.

        The interval is normalised at file scope, before any `emulate` has had a
        chance to turn OCTAL_ZEROES off -- so under a user who sets it, `08` is
        `bad math expression` printed at every prompt and `010` quietly means
        eight. Driven with the option on, because with it off this test passes
        against a hook that does no normalising at all.
        """
        out = self.run_shell(
            "echo marker\n", {"WOSWOAR_SYNC_INTERVAL": "08"}, before="setopt octal_zeroes"
        )
        self.assertIn("marker", out)
        self.assertNotIn("bad math expression", out)
        self.assertTrue(self.stamp.exists(), "a valid interval stopped it syncing at all")

    def test_a_stamp_that_is_not_a_plain_number_is_rejected_not_salvaged(self) -> None:
        """`>` is not atomic and every shell on this machine writes this file, so
        a value that is not a number needs no attacker -- a torn write is enough.

        Which junk is used matters, and it is not the junk the bash suite uses.
        `x[$(...)]` is code execution under bash, which reads it as an arithmetic
        array subscript and runs the substitution; zsh reads it as an *unset*
        array and quietly calls it zero, so a test built on that payload alone
        passes with the sanitising line deleted -- which is how this one was
        first written. What zsh handles badly is the ordinary torn write, an
        epoch with a leftover tail on it: that is `bad math expression` on the
        shell's own stderr, at the user's prompt, after every command.

        So the corpus is both shapes, and the assertion the tail can fail is the
        one that carries the test.
        """
        for junk in ("9999999999oops", "1785321992abc", "not-a-number", "", "x[$(touch it)]"):
            with self.subTest(stamp=junk):
                self.stamp.write_text(f"{junk}\n", encoding="utf-8")
                out = self.run_shell("echo marker\n")
                self.assertIn("marker", out)
                self.assertNotIn("bad math expression", out)
                self.assertNotEqual(
                    self.stamp.read_text(encoding="utf-8").strip(),
                    junk,
                    "an unreadable stamp was believed rather than replaced",
                )

    def test_a_missing_stamp_is_not_announced_at_the_prompt(self) -> None:
        """The `{ read } 2>/dev/null` block, for the same reason as the append:
        zsh puts a failed redirection on the shell's stderr, so the obvious
        spelling prints the stamp's path after every command."""
        self.stamp.unlink(missing_ok=True)
        out = self.run_shell("echo marker\n")
        self.assertIn("marker", out)
        self.assertNotIn("sync-stamp", out)


@requires_zsh5
class TestZshEscapeParity(EscapeParityMixin, unittest.TestCase):
    NONINTERACTIVE: ClassVar[list[str]] = ["zsh", "-f", "-c"]
    HOOK: ClassVar[Path] = ZSH_HOOK


@requires_zsh5
class TestZshConstantParity(ConstantParityMixin, unittest.TestCase):
    HOOK: ClassVar[Path] = ZSH_HOOK

    def test_hook_requires_the_documented_zsh_version(self) -> None:
        self.assertIn("${ZSH_VERSION%%.*} < 5", self.source)


@requires_zsh5
class TestTheZshDefaultIgnorePattern(DefaultIgnorePatternMixin, ZshHookTestCase):
    pass


@requires_zsh5
@unittest.skipUnless(shutil.which("strace"), "strace required")
class TestForkFree(CloneScalingMixin, ZshHookTestCase):
    pass


@requires_zsh5
class TestCtrlRLandsOnThePrompt(CtrlROnThePromptMixin, ZshHookTestCase):
    """The pty test itself is the mixin's -- `self.INTERPRETER` and `self.HOOK`
    are the only things in it that were ever shell-specific. What is left is the
    binding, which is `bindkey` here and `bind -x` there."""

    def test_the_binding_is_skipped_when_asked(self) -> None:
        """`WOSWOAR_NO_BIND=1` is what a user keeping atuin's Ctrl-R sets, and
        the documentation now points zsh users at it too."""
        out = self.run_shell("bindkey '^R'\n", env_extra={"WOSWOAR_NO_BIND": "1", "TERM": "xterm"})
        self.assertNotIn("__woswoar_widget", out)


if __name__ == "__main__":
    unittest.main()
