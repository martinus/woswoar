"""What `woswoar install` writes into .bashrc.

One .bashrc is routinely shared across machines through a dotfiles repo, so the
line has to be identical on all of them. It was not: it named the absolute path,
which differs the moment two machines disagree about the username.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from woswoar import __main__ as main_module
from woswoar import cache, store
from woswoar.__main__ import _BEGIN, main, portable_hook_path

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

    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self._saved = {key: os.environ.get(key) for key in ("HOME", "SHELL")}
        self.addCleanup(self._restore)
        os.environ["HOME"] = str(self.home)
        # Never inherited from the machine running the suite: `auto`'s fallback
        # reads it, so a developer whose login shell is zsh would otherwise get
        # a different answer from CI.
        os.environ["SHELL"] = "/bin/bash"

    def _restore(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def rcfiles(self, *shells: str) -> None:
        for shell in shells:
            (self.home / main_module.RCFILES[shell]).write_text("# mine\n", encoding="utf-8")

    def sourced(self, shell: str) -> bool:
        """Whether that shell's rc file now sources that shell's hook."""
        rcfile = self.home / main_module.RCFILES[shell]
        if not rcfile.is_file():
            return False
        return main_module.HOOKS[shell] in rcfile.read_text(encoding="utf-8")

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
            self.assertTrue((store.data_dir() / main_module.HOOKS[shell]).is_file())

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
            (store.data_dir() / main_module.HOOKS["zsh"]).exists(),
            "a hook was copied for a shell with no rc file",
        )

    def test_auto_falls_back_to_the_login_shell(self) -> None:
        """A container or a fresh account has neither file, and still has a shell."""
        os.environ["SHELL"] = "/usr/bin/zsh"
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
        self.assertIn(main_module.HOOKS["zsh"], target.read_text(encoding="utf-8"))
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
            main_module.HOOKS["zsh"],
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
        self.assertEqual(text.count(_BEGIN), 1)
        self.assertIn(main_module.HOOKS["bash"], text)


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
        self.home = self.root / "home"
        self.home.mkdir()
        self._saved = {key: os.environ.get(key) for key in ("HOME", "SHELL")}
        self.addCleanup(self._restore)
        os.environ["HOME"] = str(self.home)
        os.environ["SHELL"] = "/bin/bash"
        for name in (".bashrc", ".zshrc"):
            (self.home / name).write_text("", encoding="utf-8")
        self.assertEqual(main(["install"]), 0)

    def _restore(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_shell(self, argv: list[str], rcfile: str, marker: str) -> None:
        """One command, in a shell that loads its own rc file the way a login does.

        Sourcing the rc file rather than the hook is the whole point: what is
        under test is the line `install` wrote into it.
        """
        subprocess.run(
            argv,
            input=f"source {self.home / rcfile}\necho {marker}\n",
            text=True,
            env={
                "HOME": str(self.home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TERM": "dumb",
                "WOSWOAR_DIR": os.environ["WOSWOAR_DIR"],
                "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
                "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
                "WOSWOAR_SYNC_INTERVAL": "0",
            },
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


class TestTheRefreshNeverCreates(WoswoarTestCase):
    """The hazard #159 names, and the reason this is not a tidy-up.

    `_refresh_hook` runs unattended, from the background sync a prompt starts.
    If it ever *created* rather than refreshed, the first sync after an upgrade
    would put a `woswoar.zsh` on a bash-only machine: a file nothing sources,
    that nobody asked for, from a command nobody ran.
    """

    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore)
        (self.home / ".bashrc").write_text("# mine\n", encoding="utf-8")
        self.assertEqual(main(["install", "--shell", "bash"]), 0)

    def _restore(self) -> None:
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home

    def hook(self, shell: str) -> Path:
        return store.data_dir() / main_module.HOOKS[shell]

    def test_a_refresh_never_creates_a_second_hook(self) -> None:
        # A `.zshrc` as well, so that *detection* would say zsh -- the refresh
        # must go on what is installed, not on what the machine looks like.
        # Without this the test passes against a refresh that consults
        # `detect_shells`, which is the wrong rule reached by a plausible route.
        (self.home / ".zshrc").write_text("# mine\n", encoding="utf-8")
        self.hook("bash").write_bytes(b"# an older woswoar\n")

        self.assertTrue(main_module._refresh_hook(), "the stale bash hook was not refreshed")
        self.assertEqual(self.hook("bash").read_bytes(), main_module._hook_bytes("bash"))
        self.assertFalse(self.hook("zsh").exists(), "the refresh planted a hook nobody installed")

    def test_a_stale_zsh_hook_is_refreshed(self) -> None:
        """The other direction: once zsh *is* installed, it heals like bash.

        Restricting staleness to bash would leave a zsh user running whatever
        shell code they installed on the day they set up, forever, with
        `doctor` reporting green.
        """
        self.assertEqual(main(["install", "--shell", "zsh"]), 0)
        self.hook("zsh").write_bytes(b"# an older woswoar\n")

        self.assertTrue(main_module._refresh_hook())
        self.assertEqual(self.hook("zsh").read_bytes(), main_module._hook_bytes("zsh"))

    def test_a_stale_zsh_hook_makes_doctor_say_so(self) -> None:
        self.assertEqual(main(["install", "--shell", "zsh"]), 0)
        self.hook("zsh").write_bytes(b"# an older woswoar\n")
        ran = support.run_cli("doctor")
        self.assertIn("older than this woswoar", ran.out)
        self.assertIn(main_module.HOOKS["zsh"], ran.out)


if __name__ == "__main__":
    unittest.main()
