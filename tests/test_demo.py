"""The demo GIF, and the two ways it goes wrong silently.

A recording is documentation that cannot be proof-read: nothing in a diff shows
that the tape now presses a key the picker stopped binding, or that the image
the README points at is a 404 on the one page most readers see it from. Both
failures look like a working repository from the inside.

So the checkable parts are checked here. What the GIF *shows* still has to be
watched by a person -- that is what `tools/demo/record.sh` is for.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import mutants
from tools.demo import seed
from woswoar import search, store

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
TAPE = REPO / "tools" / "demo" / "demo.tape"
GIF = REPO / "docs" / "demo.gif"

#: `Ctrl+R` in a VHS tape, which is one keystroke sent to the picker.
_CTRL = re.compile(r"^Ctrl\+(\w)$", re.M)

#: The `raw.githubusercontent.com` form the README embeds the GIF with, and the
#: repo-relative path at the end of it.
_RAW = re.compile(r"https://raw\.githubusercontent\.com/[^/]+/[^/]+/[^/]+/(\S+?\.gif)")


def _in_the_sandbox(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run `argv` with the demo sandbox as its **whole** environment.

    The seeder was spawned here with no `env=` at all, so it began life holding
    the developer's real `WOSWOAR_DIR`, `HOME` and `XDG_*`, and the only thing
    between this suite and a live installation was `seed.build`'s own
    `os.environ.clear()` -- one line of the very file a mutation sweep rewrites.
    That is the arrangement that produced #245, and #246's guard is a second
    lock on the same door rather than a reason to leave the first one open. So
    the child now starts inside the sandbox and `build` merely confirms it.

    `store.sandbox_environ` rather than the five literals `listed` built by
    hand, for the reason `tools/sandbox.py` gives: a hand-kept list falls behind
    `store.ENV_KEYS` and a "sandbox" quietly points somewhere real. That one
    named three of the six -- it set `WOSWOAR_DIR`, so the missing
    `XDG_DATA_HOME` never decided anything, which is exactly why nothing said so.

    `cwd` is the repo because `-m tools.demo.seed` has to find `tools`, and
    `PATH` keeps `bin/woswoar` first so the sandbox's wrapper wins over any
    installed release.
    """
    env = store.sandbox_environ(root, root / "home")
    env["PATH"] = f"{root / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}"
    return subprocess.run(argv, cwd=REPO, env=env, check=True, capture_output=True, text=True)


class TestTheChildrenOfThisSuiteStartInsideTheSandbox(unittest.TestCase):
    """What `_in_the_sandbox` actually hands a child, asked of a real one.

    Driving a child rather than reading the dict back, because the dict is not
    what decides: dropping `env=` from the `subprocess.run` above leaves the
    dict correct and the child holding the developer's environment, and only a
    process can tell those two apart.
    """

    #: A `WOSWOAR_DIR` that is not the developer's and not the sandbox's, so
    #: that "the child saw the parent" and "the child saw nothing" are distinct
    #: answers. A real one would make this pass on a machine with no woswoar.
    DECOY = "/nonexistent/a-real-looking-installation"

    def child_environment(self, root: Path) -> dict[str, str]:
        dumped = _in_the_sandbox(
            root, sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"
        )
        # Annotated rather than asserted: `python -O` strips an `assert`, and
        # `tests/support.py` carries the same note for the same reason.
        loaded: dict[str, str] = json.loads(dumped.stdout)
        return loaded

    def test_it_carries_nothing_of_the_environment_it_was_spawned_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with mock.patch.dict(
                os.environ, {"WOSWOAR_DIR": self.DECOY, "HOME": self.DECOY, "WOSWOAR_SESSION": "s"}
            ):
                inherited = self.child_environment(root)
        # Names, never values. This assertion firing prints its container, and
        # the container is a real environment: the run that first proved this
        # test works spilled an ssh-agent socket, an Atlassian token and an
        # Anthropic session key into the log. A CI log is public.
        self.assertEqual(
            sorted(name for name, value in inherited.items() if self.DECOY in value),
            [],
            "the child was handed the environment this suite was spawned from",
        )
        self.assertNotIn("WOSWOAR_SESSION", sorted(inherited))

    def test_it_still_carries_what_a_sandbox_cannot_invent(self) -> None:
        """The direction an *empty* environment also passes, which is why it is
        a separate assertion (#251): a sandbox has no answer for where `env`,
        `git` and `age` live, and `bin/woswoar` is a `/bin/sh` script that
        `exec env`. Dropping `PATH` fails on a machine whose tools are outside
        `/usr/bin` -- Homebrew, Nix, most containers -- and nowhere else, which
        in this repository means macOS CI against a green local run.
        """
        with tempfile.TemporaryDirectory() as tmp:
            inherited = self.child_environment(Path(tmp).resolve())
        for name in store.SANDBOX_CARRIES:
            if name not in os.environ:
                continue
            # `in` rather than `==` for the one name this file prefixes: `PATH`
            # arrives with the sandbox's own `bin` in front of what it had.
            self.assertIn(os.environ[name], inherited.get(name, ""), f"{name} was dropped")

    def test_every_store_path_the_child_resolves_is_inside_the_sandbox(self) -> None:
        """The other half: not merely *different* from the parent's, but under
        the directory this suite is about to delete."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            inherited = self.child_environment(root)
            with mock.patch.dict(os.environ, inherited, clear=True):
                resolved = [store.data_dir(), store.config_dir(), store.logs_dir()]
            for where in resolved:
                self.assertTrue(where.resolve().is_relative_to(root), f"{where} is outside {root}")


class TestTheSeederCannotReachARealStore(unittest.TestCase):
    """#245: this script builds a sandbox and writes a woswoar store into it.

    One line of `_sandbox_env` is what points `store` at that sandbox, and a
    mutation sweep rewrites this module -- so when that line broke, the seeder
    wrote into the maintainer's live installation: three fabricated machines in
    399 days of real history, a `machine` file overwritten with `THIS_HOST`, and
    one of them published to a shared remote under the real machine's own
    signing key.

    Asserted against a poisoned environment rather than a broken `_sandbox_env`,
    because that is the shape the accident had: the environment is what decides,
    and the guard has to hold whatever put it there.
    """

    def test_it_refuses_a_store_outside_its_own_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sandbox"
            root.mkdir()
            (root / seed.MARKER).touch()
            elsewhere = Path(tmp) / "a-real-looking-installation"
            elsewhere.mkdir()
            with (
                mock.patch.dict(os.environ, {"WOSWOAR_DIR": str(elsewhere)}),
                self.assertRaises(SystemExit) as refused,
            ):
                seed._refuse_a_real_store(root)
        self.assertIn("outside", str(refused.exception))

    def test_it_refuses_a_directory_it_did_not_build(self) -> None:
        """A developer who points `root` at their own store gets the same
        answer: the marker is written by the run that built the sandbox, so its
        absence means this is somebody's directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "not-a-sandbox"
            root.mkdir()
            with self.assertRaises(SystemExit) as refused:
                seed._refuse_a_real_store(root)
        self.assertIn(seed.MARKER, str(refused.exception))

    def test_a_real_sandbox_passes(self) -> None:
        """The other half, or the guard could be `raise SystemExit` and pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "sandbox"
            root.mkdir()
            (root / seed.MARKER).touch()
            with mock.patch.dict(
                os.environ,
                {
                    "WOSWOAR_DIR": str(root / "data"),
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "HOME": str(root / "home"),
                },
            ):
                seed._refuse_a_real_store(root)

    def test_build_refuses_before_it_writes_anything(self) -> None:
        """The call site, not just the guard.

        Removing `_refuse_a_real_store(root)` from `build` survived a table that
        only called the guard directly -- and the call site is the whole of the
        protection. Driven by breaking `_sandbox_env` the way a mutant does,
        since with a working one the environment always takes and the guard can
        never fire.
        """
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "a-real-looking-installation"
            real.mkdir()
            root = Path(tmp) / "sandbox"
            leaky = {**os.environ, "WOSWOAR_DIR": str(real)}
            with (
                mock.patch.object(seed, "_sandbox_env", lambda root, repo: leaky),
                self.assertRaises(SystemExit) as refused,
            ):
                seed.build(root=root, repo=Path(tmp), entries=1, seed=1, now=1_700_000_000)
            self.assertIn("outside", str(refused.exception))
            self.assertEqual(list(real.iterdir()), [], "it wrote before refusing")


class TestTheDemoIsNeverMutated(unittest.TestCase):
    """The second lock, and the reason there are two.

    A mutant can break the guard above as easily as the environment line it
    guards -- they are both in the file being mutated. So the sweep does not
    generate rows for `tools/demo/` at all, and nothing is lost: 68 of its rows
    ran in the sweep that caused #245, and every one was an equivalent mutant in
    a recording script nothing asserts on.
    """

    def test_the_demo_is_not_mutable(self) -> None:
        self.assertFalse(mutants.mutable("tools/demo/seed.py"))
        self.assertFalse(mutants.mutable("tools/demo/anything_else.py"))

    def test_the_rest_of_tools_still_is(self) -> None:
        """Excluding a directory must not quietly exclude the package."""
        self.assertTrue(mutants.mutable("tools/mutate.py"))
        self.assertTrue(mutants.mutable("woswoar/store.py"))


class TestTheTapePressesKeysThePickerBinds(unittest.TestCase):
    """Every `Ctrl+x` in the tape is a key woswoar actually binds.

    The way a demo rots is not that it breaks: it is that a key is renamed, the
    tape goes on pressing the old one, and the recording quietly shows a picker
    that ignores a keystroke. Nobody re-watches twenty seconds of GIF looking
    for the beat that stopped happening.
    """

    def keys(self) -> list[str]:
        return [key.lower() for key in _CTRL.findall(TAPE.read_text(encoding="utf-8"))]

    def test_the_tape_presses_something(self) -> None:
        """Without this, a tape that lost its beats passes the test below."""
        self.assertGreaterEqual(len(self.keys()), 4)

    def test_every_key_is_one_the_picker_answers(self) -> None:
        # `r` opens the picker and cycles it; `t` unfolds the timeline. The rest
        # are the scope keys, and those come from `search` rather than from a
        # copy here, so renaming one fails this instead of the recording.
        bound = {"r", "t", *search.KEYS.values()}
        self.assertEqual([key for key in self.keys() if key not in bound], [])

    def test_the_scope_tour_is_still_in_it(self) -> None:
        """The beat the tape exists for: `dir` is the scope that spans machines,
        and it is the one a recording is most likely to lose, because it is the
        one that looks like the list did not change."""
        self.assertIn(search.KEYS["dir"], self.keys())


class TestTheReadmeShowsAGifThatExists(unittest.TestCase):
    def url_path(self) -> str:
        found = _RAW.findall(README.read_text(encoding="utf-8"))
        self.assertEqual(len(found), 1, f"expected one raw GIF URL in the README, found {found}")
        return str(found[0])

    def test_the_url_names_a_file_in_this_repository(self) -> None:
        """An absolute URL is invisible to the link tests in `test_docs.py`,
        which skip anything starting `https://` -- so the one link that is
        absolute on purpose is checked here instead. Renaming the file would
        otherwise break the README on github.com and on PyPI at once, and
        neither says so."""
        self.assertTrue((REPO / self.url_path()).is_file(), f"{self.url_path()} is not in the repo")

    def test_it_is_the_size_of_a_demo_and_not_of_a_mistake(self) -> None:
        """A few MB, per the issue this came from. The lower bound is the real
        guard: `gifsicle` writing an empty file, or a tape that recorded a shell
        that never started, both leave something small and plausible behind."""
        size = GIF.stat().st_size
        self.assertGreater(size, 50_000, "too small to be a 20-second recording")
        self.assertLess(size, 4_000_000, "big enough that readers on a phone pay for it")


class TestTheSandboxIsBuiltAndSearchable(unittest.TestCase):
    """`tools/demo/seed.py` run for real, the way `record.sh` runs it.

    Following the repository's habit of driving the real thing: this builds a
    sandbox, then asks the sandbox's own `woswoar` what is in it. A stub would
    not have caught the two things that actually broke here -- a history whose
    machine column never appears because every entry came from one host, and a
    hook that reads its data directory before the rc file exports it.
    """

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="woswoar-demo-test-")).resolve()
        # Small: this is a test of the plumbing, not of the 54,000-command
        # default, which takes a second to write and proves nothing more.
        _in_the_sandbox(
            cls.root, sys.executable, "-m", "tools.demo.seed", str(cls.root), "--entries", "400"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        __import__("shutil").rmtree(cls.root, ignore_errors=True)

    def listed(self, scope: str = "global") -> list[str]:
        done = _in_the_sandbox(
            self.root, str(self.root / "bin" / "woswoar"), "list", "--scope", scope
        )
        return list(done.stdout.splitlines())

    def test_the_history_is_there_and_the_newest_command_is_the_planted_one(self) -> None:
        """The first frame of the GIF is the top of this list, so it is worth
        one assertion that it is what the tape's comments say it is."""
        lines = self.listed()
        self.assertGreater(len(lines), 50)
        self.assertIn("docker compose up -d --build", lines[0])

    def machine_column(self) -> set[str]:
        """What is in the machine column, which is not the same as what is on
        the line. Substring-matching the whole output for `nuc` passes on a
        single-machine history: the *commands* say `ssh nuc` and
        `ansible-playbook -l nuc`. That version of this test survived the
        mutation that put every entry on one host -- see CLAUDE.md rule 3 on
        fixtures too weak to tell the two answers apart.

        Every line is a fixed-width age, two spaces, then the column -- when
        there is one at all. `search._PREFIX` rather than a 9 here, so a change
        to the layout moves both.
        """
        return {line[search._PREFIX :].split()[0] for line in self.listed() if line.strip()}

    def test_more_than_one_machine_is_in_it(self) -> None:
        """The machine column only exists when there are two machines to tell
        apart -- `host_width_for` returns 0 below that -- so a demo history that
        drifted to one host would film the picker with its whole argument
        missing, and nothing else here would notice."""
        known = {
            name.split("@")[1] for name in ("martin@thinkpad", "martin@nuc", "martin@DT-24YYQ3")
        }
        self.assertGreaterEqual(
            len(known & self.machine_column()),
            2,
            "the machine column does not hold two machines, so the picker has no column at all",
        )

    def test_the_host_scope_is_a_smaller_list_than_the_global_one(self) -> None:
        """The other half of the same claim, and the one that does not depend on
        how a line is laid out: `host` is a strict subset of `global` exactly
        when the history has more than this machine in it."""
        self.assertLess(len(self.listed("host")), len(self.listed()))

    def test_the_shell_hook_was_installed_into_the_sandbox_rc(self) -> None:
        """Without it the recording is of a bash that never binds Ctrl-R, which
        is twenty seconds of nothing happening."""
        rc = (self.root / "home" / ".bashrc").read_text(encoding="utf-8")
        self.assertIn("woswoar.bash", rc)
        # The hook reads WOSWOAR_DIR when it is sourced, so the exports have to
        # come first. Getting this backwards points the demo shell at the
        # recording machine's real history -- which is the one outcome this
        # whole sandbox exists to prevent.
        self.assertLess(rc.index("export WOSWOAR_DIR"), rc.index("woswoar.bash"))


if __name__ == "__main__":
    unittest.main()
