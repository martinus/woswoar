"""What `woswoar install` writes into .bashrc.

One .bashrc is routinely shared across machines through a dotfiles repo, so the
line has to be identical on all of them. It was not: it named the absolute path,
which differs the moment two machines disagree about the username.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from woswoar import cache, install, store
from woswoar.__main__ import main
from woswoar.install import portable_hook_path

from . import support
from .support import (
    MACHINE_ID,
    WoswoarTestCase,
    make_entry,
    requires_bash,
    requires_bash5,
    requires_zsh5,
)


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


class TestTheSuiteCannotReachTheMachineItRunsOn(WoswoarTestCase):
    """The guard for `WoswoarTestCase`'s sandbox -- see `store.sandbox_environ`
    for the incidents, one of which this file's tests caused.

    It belongs here because `install` is the command that made it reachable:
    `rcfile_for` is `Path.home() / ".bashrc"`, the one path in woswoar with no
    XDG variable in front of it. What is asserted is no longer two names,
    though. The harness used to remove a list and leave the rest of the
    developer's environment in place, so the guard could only ever say that the
    names already on the list were on it -- green for exactly the variables
    nobody had been bitten by yet.
    """

    #: Exported by a plausible developer, and each with a route into a test:
    #: where zsh looks for its rc file, against a suite that drives a real zsh;
    #: what non-interactive bash sources, against one that spawns many; a git
    #: config whose hooks and filters would run inside the sandbox; and two that
    #: decide the shape of output an assertion then reads.
    LEAKS: ClassVar[dict[str, str]] = {
        "ZDOTDIR": "/somewhere/else",
        "BASH_ENV": "/somewhere/else/env.sh",
        "GIT_CONFIG_GLOBAL": "/somewhere/else/gitconfig",
        "EDITOR": "vi-from-the-developer",
        "LC_ALL": "tr_TR.UTF-8",
    }

    #: Declared so that `mypy` can see a class attribute assigned in
    #: `setUpClass` rather than in `__init__`.
    _outer_path: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        # Started here rather than in `setUp`, so that these are part of the
        # environment the harness is then asked to replace -- `setUpClass` runs
        # first. Setting them inside a test would prove nothing: by then the
        # sandbox is already built.
        super().setUpClass()
        # Read before the sandbox exists, so the carry assertion below has
        # something outside it to compare against.
        cls._outer_path = os.environ.get("PATH", "")
        exported = mock.patch.dict(os.environ, cls.LEAKS)
        exported.start()
        cls.addClassCleanup(exported.stop)

    def test_nothing_the_developer_exported_reaches_a_test(self) -> None:
        """Subsumed by the assertion below -- no leaked name is in
        `SANDBOX_CARRIES`, so a set difference catches it either way. Kept for
        what it says when it fails: the name, rather than a diff of two sets."""
        for name in self.LEAKS:
            with self.subTest(name=name):
                self.assertNotIn(name, os.environ)

    def test_what_survives_is_what_the_sandbox_built(self) -> None:
        """The other half, and the reason this is a property rather than a list
        of names: whatever a test *can* see is either carried on purpose or set
        on purpose, and adding to either is an edit to `store.sandbox_environ`
        that lands in this assertion."""
        carried = {name for name in store.SANDBOX_CARRIES if name in os.environ}
        self.assertEqual(
            set(os.environ) - carried,
            {
                "HOME",
                "SHELL",
                "USER",
                "LOGNAME",
                "WOSWOAR_DIR",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "XDG_DATA_HOME",
            },
        )

    def test_what_is_carried_actually_arrives(self) -> None:
        """The other direction, and not symmetry for its own sake: a sandbox
        that carries *nothing* satisfies every assertion above, because the
        developer's variables are certainly gone. That shape cost a red macOS
        CI run -- see `store.SANDBOX_CARRIES`."""
        self.assertEqual(os.environ.get("PATH"), self._outer_path)
        self.assertIsNotNone(shutil.which("git"), "the suite runs a real git")

    def test_the_planted_names_are_the_names_a_sandbox_carries(self) -> None:
        """`support.PLANTED` is a hand-written copy of `store.SANDBOX_CARRIES`,
        and this is what keeps the copy honest.

        Deriving it was the obvious version and it guarded nothing: an
        expectation built from the list under test shrinks whenever the list
        does, so removing a carried name passed 1213 tests. Written out, the
        removal fails three carry assertions -- and this one, which is the only
        place that says *why* they now disagree.
        """
        self.assertEqual(set(support.PLANTED), set(store.SANDBOX_CARRIES))

    def test_home_is_the_sandbox(self) -> None:
        self.assertEqual(Path.home(), self.home)
        self.assertEqual(install.rcfile_for("bash").parent, self.home)

    def test_the_login_shell_of_the_machine_running_the_suite_does_not_leak(self) -> None:
        """`auto` falls back to `$SHELL` when there is no rc file to go on, so
        an un-scrubbed one makes `install` answer differently on a developer's
        zsh box than in CI. That has cost a release before, in the fzf check.

        The behaviour first, because that is the property worth having. The
        constant after it, because the behaviour alone cannot fail on a machine
        whose login shell is already bash -- which is most of them, and would
        leave this silent exactly where a guard is cheapest to lose.
        """
        self.assertEqual(install.detect_shells(), ["bash"])
        self.assertEqual(os.environ["SHELL"], "/bin/bash")


class TestTheHarnessRefusesARealStore(unittest.TestCase):
    """#245: what a sandbox that did not take must do.

    A `machine` file holding this suite's two fixture lines turned up in the
    maintainer's real installation, which then reported "no identity configured"
    and recorded three hours of commands under a fixture id. Whatever put it
    there, a harness that writes a machine identity has to fail its own test
    rather than land in `~/.config` -- so the check is before the first write,
    and this drives it by breaking the sandbox the way an accident would.
    """

    def test_a_sandbox_that_did_not_take_errors_instead_of_writing(self) -> None:
        class Probe(WoswoarTestCase):
            def runTest(self) -> None:  # pragma: no cover - setUp is the subject
                pass

        # The builder hands back the *ambient* environment, so every store path
        # resolves outside the sandbox -- which is precisely the shape that
        # reached a real machine.
        with mock.patch.object(store, "sandbox_environ", lambda root, home: dict(os.environ)):
            outcome = Probe().run()

        assert outcome is not None
        self.assertEqual(len(outcome.errors), 1, "the sandbox not taking must be an error")
        self.assertIn("sandbox did not take", outcome.errors[0][1])


class TestWhatASpawnedChildInherits(WoswoarTestCase):
    """The other half of the class above: what a test's *child* can see.

    Six fixtures built one as a literal list of six names, so none carried
    `PYTHONWARNINGS` -- which CI exports as `error::DeprecationWarning` for the
    whole workflow. In the pty and hook suites, where the shipped CLI actually
    runs as `python -m woswoar`, a deprecation had therefore never been an
    error (#239). It failed quiet, which is the only reason it lasted.

    Asserted through the thing itself: a child that emits one must die when the
    policy says so. A test comparing two dicts would pass just as happily on a
    helper nobody calls.
    """

    def test_the_warning_policy_reaches_it(self) -> None:
        warns = "import warnings; warnings.warn('x', DeprecationWarning)"
        with mock.patch.dict(os.environ, {"PYTHONWARNINGS": "error::DeprecationWarning"}):
            died = subprocess.run(
                [sys.executable, "-c", warns],
                capture_output=True,
                env=support.child_env(),
                check=False,
            )
        self.assertNotEqual(died.returncode, 0, died.stderr)

    def test_and_is_not_invented_when_it_is_absent(self) -> None:
        """Carried, not imposed. Without this, a helper that hardcoded the flag
        would pass the test above while telling every child the same lie."""
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONWARNINGS", None)
            self.assertNotIn("PYTHONWARNINGS", support.child_env())


class TestInstall(WoswoarTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.rcfile = self.home / ".bashrc"

    def install(self) -> str:
        self.assertEqual(main(["install", "--rcfile", str(self.rcfile)]), 0)
        return self.rcfile.read_text(encoding="utf-8")

    def test_the_sourced_line_names_no_username(self) -> None:
        os.environ["WOSWOAR_DIR"] = str(self.home / ".local/share/woswoar")
        text = self.install()
        self.assertIn('source "$HOME/.local/share/woswoar/woswoar.bash"', text)
        self.assertNotIn(str(self.home), text)

    def test_a_backslash_in_the_path_survives_a_reinstall(self) -> None:
        """#292. The second argument to `re.sub` is a replacement *template*,
        and the block being substituted holds a path the user controls.

        Two installs, because one never reaches `sub` -- the first appends the
        block through the other branch, so this worked once and then broke. A
        fixture with a single install cannot tell the two implementations apart,
        which is why the existing reinstall test above did not catch it.

        `\\i` is `bad escape` and raises; the quieter half is `\\1`, which
        substitutes a group from the pattern and writes a `source` line pointing
        somewhere else while reporting success.
        """
        weird = self.home / "back\\slash"
        os.environ["WOSWOAR_DIR"] = str(weird)
        first = self.install()
        self.assertIn("woswoar.bash", first)

        text = self.install()
        self.assertIn("woswoar.bash", text)
        self.assertEqual(text.count("# >>> woswoar >>>"), 1, "the block was duplicated")

    def test_a_group_reference_in_the_path_is_not_expanded(self) -> None:
        """The silent half on its own. `\\1` does not raise -- it substitutes,
        so the rc file still looks plausible and the hook simply never loads."""
        os.environ["WOSWOAR_DIR"] = str(self.home / "g\\1roup")
        self.install()
        text = self.install()
        self.assertIn("g\\1roup", text, "the group reference was expanded away")

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
            env=support.child_env(HOME=str(self.home)),
        ).stdout
        self.assertTrue(Path(resolved).is_file(), f"{resolved} was not written by install")


class TestPrivateByDefault(WoswoarTestCase):
    """Recorded history is at least as private as ~/.bash_history.

    bash creates that 0600. woswoar's logs hold strictly more -- the command,
    the working directory, the exit status, and every other machine's history
    once sync has run -- so anything looser would make installing woswoar a
    downgrade on any box with a group- or world-readable home.
    """

    def as_if_never_installed(self) -> None:
        """Drop the config directory the shared harness pre-creates.

        It is made with a plain mkdir at the ambient umask, so leaving it would
        make every assertion here about a directory woswoar inherited rather
        than one it created.
        """
        shutil.rmtree(store.config_dir())

    def first_run(self) -> None:
        """What a real first run writes, through the real entry points."""
        self.as_if_never_installed()
        store.save_machine(store.machine())
        store.append_entries(MACHINE_ID, [make_entry(1_700_000_000, "git status")])

    def test_a_stock_umask_cannot_loosen_what_is_written(self) -> None:
        # 022 is the default nearly everywhere, and is exactly what made the
        # logs 0644: `mkdir` and `open(..., "a")` both honour it. Pinned rather
        # than inherited, so the test cannot pass vacuously on a strict runner.
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        self.first_run()
        self.assertEqual(support.loose_paths(), [])

    def test_an_older_install_is_retightened(self) -> None:
        self.first_run()
        for path in (store.data_dir(), store.logs_dir(), *store.logs_dir().rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        self.assertTrue(support.loose_paths(), "the fixture is not actually loose")

        store.harden()
        self.assertEqual(support.loose_paths(), [])

    def test_creating_a_file_does_not_repermission_a_directory_it_found(self) -> None:
        """`write_atomic` is a durability primitive, not a permissions one.

        It creates missing parents privately, but must not clamp a directory
        that was already there -- `history/` arrives from `git clone`, and a
        user-chosen WOSWOAR_DIR is not woswoar's to re-permission. Migration is
        `harden`'s job precisely so this one can stay out of it.
        """
        borrowed = store.data_dir() / "borrowed"
        borrowed.mkdir(parents=True)
        borrowed.chmod(0o755)
        store.write_atomic(borrowed / "note", b"hello")
        self.assertEqual(borrowed.stat().st_mode & 0o777, 0o755)
        self.assertEqual((borrowed / "note").stat().st_mode & 0o777, 0o600)

    def test_a_failed_write_leaves_no_scratch_file(self) -> None:
        """The branch whose comment says "Never leave the scratch file next to
        the real one" -- and which nothing executed.

        It matters because the scratch name is `.<name>-<random>.tmp` in the
        *same* directory as the real file, so a leaked one sits inside `logs/`
        or `history/` looking like data. `iter_log_files` and `chunk_names`
        filter by suffix and would ignore it, but `doctor`'s private-mode walk
        and any human reading the directory would not.

        The write is made to fail rather than mocked away: a real `OSError` out
        of `handle.write` is what a full disk gives.
        """
        target = store.data_dir() / "atomic" / "note"
        boom = OSError("no space left on device")
        with mock.patch("os.fdopen", side_effect=boom), self.assertRaises(OSError):
            store.write_atomic(target, b"hello")
        self.assertFalse(target.exists(), "the real file must not appear")
        leftovers = [p.name for p in target.parent.iterdir()]
        self.assertEqual(leftovers, [], f"scratch file left behind: {leftovers}")

    def test_doctor_reports_an_exposed_history(self) -> None:
        self.first_run()
        store.logs_dir().chmod(0o755)
        self.assertRegex(support.run_cli("doctor").out, r"\[FAIL\] private")


class TestShellDetection(WoswoarTestCase):
    """Which shells `install` writes to when nobody said (#159).

    The rule is "every shell whose rc file already exists". Both halves of that
    are load-bearing and each has its own test below, because the two obvious
    simplifications are both wrong: installing only the first match leaves a
    user with two shells recording from one of them, and installing
    unconditionally puts a `~/.zshrc` on the machine of somebody who has never
    run zsh.
    """

    def rcfiles(self, *shells: str) -> None:
        for shell in shells:
            (self.home / install.RCFILES[shell]).write_text("# mine\n", encoding="utf-8")

    def sourced(self, shell: str) -> bool:
        """Whether that shell's rc file now sources that shell's hook."""
        rcfile = self.home / install.RCFILES[shell]
        if not rcfile.is_file():
            return False
        return install.HOOKS[shell] in rcfile.read_text(encoding="utf-8")

    def test_auto_installs_into_every_existing_rcfile(self) -> None:
        """A machine with both shells must record from both.

        The fixture needs *both* files: with one, "install into every match" and
        "install into the first match" give the same answer and the test asserts
        nothing about which was written.
        """
        self.rcfiles("bash", "zsh")
        self.assertEqual(main(["install"]), 0)
        self.assertTrue(self.sourced("bash"), "the bash hook was not wired up")
        self.assertTrue(self.sourced("zsh"), "the zsh hook was not wired up")
        for shell in ("bash", "zsh"):
            self.assertTrue((store.data_dir() / install.HOOKS[shell]).is_file())

    def test_auto_does_not_create_an_rcfile_that_is_absent(self) -> None:
        """The half that keeps `install` from making a decision for someone.

        Writing a `~/.zshrc` onto a machine that has never run zsh is not a
        harmless extra file: it is a startup file that shell will now read, put
        there by a command the user ran about *bash*.
        """
        self.rcfiles("bash")
        self.assertEqual(main(["install"]), 0)
        self.assertTrue(self.sourced("bash"))
        self.assertFalse((self.home / ".zshrc").exists(), "install created a ~/.zshrc")
        self.assertFalse(
            (store.data_dir() / install.HOOKS["zsh"]).exists(),
            "a hook was copied for a shell with no rc file",
        )

    def test_auto_falls_back_to_the_login_shell(self) -> None:
        """A container or a fresh account has neither file, and still has a shell.

        This is also the one path on which `install` *creates* an rc file, and
        the sentence in `docs/shell-integration.md` that says so points here.
        `sourced` is not enough on its own to say that: a machine that already
        had a `~/.zshrc` would satisfy it too, so the fixture -- neither file
        present -- is what makes the file's existence afterwards mean creation.
        """
        os.environ["SHELL"] = "/usr/bin/zsh"
        self.assertFalse((self.home / ".zshrc").exists(), "the fixture already had one")
        self.assertEqual(main(["install"]), 0)
        self.assertTrue(self.sourced("zsh"), "the login shell was not consulted")
        self.assertFalse((self.home / ".bashrc").exists(), "it installed for bash as well")

    def test_an_unknown_login_shell_falls_back_to_bash(self) -> None:
        """`SHELL=/usr/bin/fish` must not leave the machine with no hook at all.

        bash rather than nothing: its hook is the one that has shipped longest,
        and a fish user who wanted zsh can say so.
        """
        os.environ["SHELL"] = "/usr/bin/fish"
        self.assertEqual(main(["install"]), 0)
        self.assertTrue(self.sourced("bash"))

    def test_shell_overrides_what_is_on_the_machine(self) -> None:
        self.rcfiles("bash")
        self.assertEqual(main(["install", "--shell", "zsh"]), 0)
        self.assertTrue(self.sourced("zsh"), "--shell was ignored")
        self.assertFalse(self.sourced("bash"), "--shell zsh installed bash too")

    def test_shell_both_installs_for_a_shell_with_no_rcfile_yet(self) -> None:
        """The escape hatch from the rule above, for someone about to start
        using zsh. Asking for it explicitly is what makes creating the file
        acceptable here and not in `auto`."""
        self.assertEqual(main(["install", "--shell", "both"]), 0)
        self.assertTrue(self.sourced("bash"))
        self.assertTrue(self.sourced("zsh"))

    def test_rcfile_alone_picks_the_shell_from_its_name(self) -> None:
        """`--rcfile ~/.zshrc` cannot sensibly mean the bash hook.

        Deciding by detection instead -- which is what #159 suggested -- would
        hand someone with a `.bashrc` beside it a `.zshrc` that sources
        `woswoar.bash`: a file zsh reads, sourcing shell code written for the
        other shell, which loads nothing and says nothing.
        """
        self.rcfiles("bash")
        target = self.home / ".zshrc"
        self.assertEqual(main(["install", "--rcfile", str(target)]), 0)
        self.assertIn(install.HOOKS["zsh"], target.read_text(encoding="utf-8"))
        self.assertFalse(self.sourced("bash"), "--rcfile installed a second shell as well")

    def test_rcfile_with_shell_both_is_refused_rather_than_resolved(self) -> None:
        """Both resolutions are wrong and one of them is silent.

        Two blocks into one file leaves only the second, because each replaces
        the marked block the last one wrote -- so this would end with a
        `.bashrc` that sources the zsh hook, and say "added" twice on the way.
        """
        self.rcfiles("bash")
        ran = support.run_cli("install", "--rcfile", str(self.home / ".bashrc"), "--shell", "both")
        self.assertEqual(ran.code, 1)
        self.assertIn("--rcfile names one file", ran.err)
        self.assertNotIn(
            install.HOOKS["zsh"],
            (self.home / ".bashrc").read_text(encoding="utf-8"),
            "it wrote the zsh hook into the bash rc file anyway",
        )

    def test_an_unrecognisable_rcfile_name_installs_one_shell(self) -> None:
        """`--rcfile /tmp/rc` says nothing about which shell, so detection
        answers -- but it still means *one* file, never two."""
        self.rcfiles("bash", "zsh")
        target = self.home / "rc"
        self.assertEqual(main(["install", "--rcfile", str(target)]), 0)
        text = target.read_text(encoding="utf-8")
        self.assertEqual(text.count(install.BEGIN), 1)
        self.assertIn(install.HOOKS["bash"], text)


class TestAFinderVisitDoesNotDisturbTheLogs(WoswoarTestCase):
    """`.DS_Store` under `logs/` (#160).

    Finder writes one into any directory it displays, and `~/.local/share` is
    somewhere people browse. Two things must be true of it and neither is true
    by accident: it is not a log file, and it is not an exposure `doctor` can
    ask the user to fix -- Finder recreates it at the ambient umask the next
    time that directory is opened, so a red line about it stays red forever.

    Written by hand rather than by a Mac. That is the point: this has to be
    pinned by the Linux suite or it regresses the moment nobody is testing on a
    Mac, which today is everybody.
    """

    def setUp(self) -> None:
        super().setUp()
        # Beside real logs, in three directories a person would actually open.
        # A tree of nothing but `.DS_Store` cannot tell a suffix filter from a
        # walk that found nothing -- `CLAUDE.md` rule 3, second bullet.
        self.write_log(MACHINE_ID, "2026-08-01", ["1785000000\ts1\t~\t0\t5\treal-command-one"])
        self.write_log(MACHINE_ID, "2026-08-02", ["1785086400\ts1\t~\t0\t5\treal-command-two"])
        store.save_machine(store.machine())
        store.harden()
        self.litter = [
            store.logs_dir() / ".DS_Store",
            store.logs_dir() / "hosts" / ".DS_Store",
            store.host_dir(MACHINE_ID) / ".DS_Store",
        ]
        for path in self.litter:
            path.write_bytes(b"\x00\x00\x00\x01Bud1")

    def test_it_is_not_read_as_a_log(self) -> None:
        found = sorted(log.path.name for log in store.iter_log_files())
        self.assertEqual(found, ["2026-08-01.tsv", "2026-08-02.tsv"])
        recorded = {entry.cmd for entry in cache.load_entries()}
        self.assertEqual(recorded, {"real-command-one", "real-command-two"})

    def test_doctor_does_not_report_it_as_an_exposure(self) -> None:
        """The mode is Finder's to choose and nobody's to keep. A `doctor` that
        goes red for a file the user cannot durably fix teaches them to stop
        reading `doctor`, which costs more than this file's mode.

        `loose_paths` is asserted to *see* it, not to miss it. That helper walks
        the tree independently of woswoar's own idea of what it owns -- that is
        why it exists -- so the file really is 0644 and saying otherwise would be
        the wrong claim. What is under test is that woswoar decides not to report
        it, which is a decision, not an oversight.
        """
        for path in self.litter:
            path.chmod(0o644)
        self.assertTrue(
            [p for p in support.loose_paths() if ".DS_Store" in p],
            "the fixture is not actually loose, so the exclusion below proves nothing",
        )
        self.assertEqual(store.readable_by_others(), [])
        self.assertNotIn("[FAIL] private", support.run_cli("doctor").out)

    def test_a_real_log_left_loose_is_still_reported(self) -> None:
        """Otherwise the test above passes against a `doctor` that stopped
        looking at `logs/` at all."""
        store.host_dir(MACHINE_ID).joinpath("2026-08-01.tsv").chmod(0o644)
        self.assertTrue(store.readable_by_others(), "the fixture is not actually loose")
        self.assertIn("[FAIL] private", support.run_cli("doctor").out)


@requires_bash5
@requires_zsh5
class TestBothShellsRecordIntoOneHistory(WoswoarTestCase):
    """`install`, then a real bash and a real zsh, then one host directory.

    Both gates name a *version*, not merely a binary: each hook refuses below 5
    and switches itself off, so a test asking only for `bash` or `zsh` fails
    there rather than skipping -- and says "the two shells disagree" about one
    that was never recording. macOS is where that stops being hypothetical: it
    ships bash 3.2, and bash on macOS is out of scope by design, not broken.

    Everything upstream of this is asserted in pieces -- which rc file was
    written, which hook was copied. This is the piece nobody can assert from a
    string: that following `install` with both shells present leaves one machine
    id and one day file, rather than two histories that never meet.
    """

    def setUp(self) -> None:
        super().setUp()
        for name in (".bashrc", ".zshrc"):
            (self.home / name).write_text("", encoding="utf-8")
        self.assertEqual(main(["install"]), 0)

    def run_shell(self, argv: list[str], rcfile: str, marker: str) -> None:
        """One command, in a shell that loads its own rc file the way a login does.

        Sourcing the rc file rather than the hook is the whole point: what is
        under test is the line `install` wrote into it.
        """
        subprocess.run(
            argv,
            input=f"source {self.home / rcfile}\necho {marker}\n",
            text=True,
            env=support.child_env(HOME=str(self.home), TERM="dumb", WOSWOAR_SYNC_INTERVAL="0"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )

    def test_both_shells_write_to_one_host_directory(self) -> None:
        self.run_shell(["bash", "--norc", "-i"], ".bashrc", "from_bash_marker")
        self.run_shell(["zsh", "-f", "-i"], ".zshrc", "from_zsh_marker")

        hosts = sorted(p.name for p in (store.logs_dir() / "hosts").iterdir())
        self.assertEqual(hosts, [MACHINE_ID], f"two shells, {len(hosts)} machine ids")

        recorded = {entry.cmd for entry in cache.load_entries()}
        self.assertIn("echo from_bash_marker", recorded, "the bash rc line did not record")
        self.assertIn("echo from_zsh_marker", recorded, "the zsh rc line did not record")


class TestInstallHardensAfterItCopies(WoswoarTestCase):
    """The order `install` does its two jobs in, which a comment claimed and
    nothing held.

    `write_bytes` creates at the ambient umask, so hardening *first* leaves the
    files install itself just wrote as the ones its own migration missed -- and
    on a machine with a permissive umask that is a world-readable copy of the
    shell hook in a directory woswoar promises is owner-only. Found by mutation
    while moving this code in #202: the invariant predates the move, the test
    does not.
    """

    def test_a_permissive_umask_does_not_leave_the_hook_readable(self) -> None:
        # 0 rather than a merely loose value: this is what makes `write_bytes`
        # produce 0666, which is the state the ordering exists to clean up.
        previous_mask = os.umask(0)
        self.addCleanup(os.umask, previous_mask)

        # `--rcfile` because what is under test is the *hook's* mode, and naming
        # the file keeps the rc file out of it entirely: `--shell bash` means
        # detection never runs, so nothing here reads one.
        rcfile = self.root / "bashrc"
        self.assertEqual(main(["install", "--shell", "bash", "--rcfile", str(rcfile)]), 0)
        # `support.loose_paths`, not `store.readable_by_others`: the latter and
        # `store.harden` are both built on `store._private_paths`, so asking it
        # would be the migration checking itself -- a path that walk stopped
        # yielding would be skipped by `harden` and reported clean in the same
        # breath. The helper says so in its own docstring, and the sibling test
        # `test_a_stock_umask_cannot_loosen_what_is_written` already used it.
        self.assertEqual(
            support.loose_paths(),
            [],
            "install left something another account on this machine can read",
        )


class TestTheHookIsReportedBeforeTheRcFile(WoswoarTestCase):
    """Why `install_hooks` and `wire_rcfiles` are two calls rather than one.

    The rc file is the step that can fail on someone else's terms -- a read-only
    home, a `.zshrc` owned by root, a path that is not a file at all. When it
    does, the hook is already copied and can be sourced by hand, and that is the
    one thing worth knowing. This is the test that holds it; `install.py` says why
    the seam is there.
    """

    def test_an_unwritable_rcfile_still_names_the_hook_it_copied(self) -> None:
        # A directory, because it fails the same way a read-only file does and
        # needs no root to arrange: `write_text` on it raises OSError.
        blocked = self.root / "not-a-file"
        blocked.mkdir()

        # `support.run_cli` cannot be used here: it returns the captured streams,
        # and the exception means it never returns. The point of the test is what
        # reached stdout *before* the failure, so the capture has to outlive it.
        shown = io.StringIO()
        with self.assertRaises(OSError), contextlib.redirect_stdout(shown):
            main(["install", "--shell", "bash", "--rcfile", str(blocked)])

        hook = store.data_dir() / install.HOOKS["bash"]
        self.assertEqual(
            hook.read_bytes(), install.hook_bytes("bash"), "the hook was not copied first"
        )
        self.assertIn(
            f"hook    : {hook}",
            shown.getvalue(),
            "the install failed without saying where the hook it wrote is",
        )


class TestTheRefreshNeverCreates(WoswoarTestCase):
    """The hazard #159 names, and the reason this is not a tidy-up.

    `install.refresh_hook` runs unattended, from the background sync a prompt
    starts.
    If it ever *created* rather than refreshed, the first sync after an upgrade
    would put a `woswoar.zsh` on a bash-only machine: a file nothing sources,
    that nobody asked for, from a command nobody ran.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.home / ".bashrc").write_text("# mine\n", encoding="utf-8")
        self.assertEqual(main(["install", "--shell", "bash"]), 0)

    def hook(self, shell: str) -> Path:
        return store.data_dir() / install.HOOKS[shell]

    def test_a_refresh_never_creates_a_second_hook(self) -> None:
        # A `.zshrc` as well, so that *detection* would say zsh -- the refresh
        # must go on what is installed, not on what the machine looks like.
        # Without this the test passes against a refresh that consults
        # `detect_shells`, which is the wrong rule reached by a plausible route.
        (self.home / ".zshrc").write_text("# mine\n", encoding="utf-8")
        self.hook("bash").write_bytes(b"# an older woswoar\n")

        self.assertTrue(install.refresh_hook(), "the stale bash hook was not refreshed")
        self.assertEqual(self.hook("bash").read_bytes(), install.hook_bytes("bash"))
        self.assertFalse(self.hook("zsh").exists(), "the refresh planted a hook nobody installed")

    def test_a_stale_zsh_hook_is_refreshed(self) -> None:
        """The other direction: once zsh *is* installed, it heals like bash.

        Restricting staleness to bash would leave a zsh user running whatever
        shell code they installed on the day they set up, forever, with
        `doctor` reporting green.
        """
        self.assertEqual(main(["install", "--shell", "zsh"]), 0)
        self.hook("zsh").write_bytes(b"# an older woswoar\n")

        self.assertTrue(install.refresh_hook())
        self.assertEqual(self.hook("zsh").read_bytes(), install.hook_bytes("zsh"))

    def test_the_names_of_the_refreshed_hooks_come_back(self) -> None:
        """What #202 made load-bearing. `refresh_hook` used to print the message
        itself; now it returns the hooks it rewrote and `cmd_sync` prints them, so
        an empty return is the difference between a person being told their shell
        code was replaced under them and it happening in silence. Nothing else
        asserts that message, and the file is rewritten either way -- so a
        `return []` would leave every other test in this class green."""
        self.assertEqual(main(["install", "--shell", "zsh"]), 0)
        for shell in ("bash", "zsh"):
            self.hook(shell).write_bytes(b"# an older woswoar\n")
        self.assertEqual(
            sorted(install.refresh_hook()), sorted(self.hook(s) for s in install.HOOKS)
        )

    def test_a_sync_says_which_hook_it_replaced(self) -> None:
        """The other end of the same thread: the message reaching a terminal.

        Through the CLI, because what broke in the move was *which* function
        prints -- a test that only called `refresh_hook` would pass with nobody
        printing at all.
        """
        self.hook("bash").write_bytes(b"# an older woswoar\n")
        ran = support.run_cli("sync")
        self.assertIn("updated the shell hook", ran.out)
        self.assertIn(str(self.hook("bash")), ran.out)

    def test_a_current_hook_is_not_announced(self) -> None:
        """Otherwise every sync would report an upgrade that did not happen."""
        ran = support.run_cli("sync")
        self.assertNotIn("updated the shell hook", ran.out)

    def test_a_stale_zsh_hook_makes_doctor_say_so(self) -> None:
        self.assertEqual(main(["install", "--shell", "zsh"]), 0)
        self.hook("zsh").write_bytes(b"# an older woswoar\n")
        ran = support.run_cli("doctor")
        self.assertIn("older than this woswoar", ran.out)
        self.assertIn(install.HOOKS["zsh"], ran.out)


if __name__ == "__main__":
    unittest.main()
