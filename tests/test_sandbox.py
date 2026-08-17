"""Tests for the shared sandbox helpers the comparison tools are built on.

One of these is a regression test for a real defect: `tools/bench.py` built its
child environment as ``{**os.environ, ...}`` and overrode four keys, while
`store.ENV_KEYS` has six. ``WOSWOAR_DIR`` beats ``XDG_DATA_HOME`` in
`store.data_dir`, and the installed hook exports ``WOSWOAR_SESSION`` in every
interactive shell -- so a benchmark run from a normal terminal measured the
developer's own history in both trees and reported the difference as a result.

That is the failure `store.ENV_KEYS`' comment was written for, and the reason the
environment is now built from nothing rather than inherited: a key woswoar reads
and this does not set is simply absent, so `store` falls back to its
``$HOME``-relative default, which is inside the sandbox.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.sandbox import (
    MACHINE_ID,
    same_filesystem,
    sandbox_env,
    seed_machine,
    usable_cpus,
    worktree,
)
from woswoar import store

REPO = Path(__file__).resolve().parent.parent


def _in_a_git_repo() -> bool:
    """Whether `worktree` has anything to check out from.

    Asked rather than assumed, because two situations run this suite outside a
    repository and both are legitimate: a copy of the tree made by
    `tools/mutate.py`, which excludes `.git` on purpose, and an installed sdist.
    A test that fails there would be reporting the absence of git rather than a
    defect -- and in the mutate case it fails the *baseline*, which makes every
    mutation result in the run meaningless.
    """
    return (
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=REPO,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


class TestTheEnvironmentIsBuiltNotInherited(unittest.TestCase):
    def test_no_woswoar_variable_leaks_in_from_the_caller(self) -> None:
        """The defect, stated as the property that prevents it.

        Every name in `store.ENV_KEYS` is either set by the sandbox or absent.
        Nothing is inherited, so nothing can point at the real installation.
        """
        poisoned = {key: f"/somewhere/real/{key}" for key in store.ENV_KEYS}
        with mock.patch.dict(os.environ, poisoned):
            env = sandbox_env(REPO, Path("/tmp/sandbox-home"))
        for key in store.ENV_KEYS:
            with self.subTest(key=key):
                self.assertNotEqual(
                    env.get(key),
                    poisoned[key],
                    f"{key} was inherited from the caller's environment",
                )

    def test_woswoar_dir_is_absent_rather_than_pointed_somewhere(self) -> None:
        """`WOSWOAR_DIR` wins over `XDG_DATA_HOME` in `store.data_dir`, so setting
        the latter is not enough -- the former has to not be there at all."""
        with mock.patch.dict(os.environ, {"WOSWOAR_DIR": "/somewhere/real"}):
            env = sandbox_env(REPO, Path("/tmp/sandbox-home"))
        self.assertNotIn("WOSWOAR_DIR", env)

    def test_the_session_the_hook_exports_does_not_reach_the_child(self) -> None:
        """Always inherited in practice: the hook exports it in every interactive
        shell, so any tool run from a terminal with woswoar installed had it."""
        with mock.patch.dict(os.environ, {"WOSWOAR_SESSION": "s-real"}):
            env = sandbox_env(REPO, Path("/tmp/sandbox-home"))
        self.assertNotIn("WOSWOAR_SESSION", env)

    def test_a_new_key_added_to_store_needs_no_edit_here(self) -> None:
        """Why building from nothing is the *structural* fix and not just a sweep.

        A key woswoar starts reading tomorrow is absent from this environment, so
        `store` falls back to its `$HOME`-relative default -- which is in the
        sandbox. The inherit-and-override shape fails the other way, towards the
        real installation, which is why it fails silently.
        """
        with mock.patch.dict(os.environ, {"WOSWOAR_SOMETHING_NEW": "/real"}):
            env = sandbox_env(REPO, Path("/tmp/sandbox-home"))
        self.assertNotIn("WOSWOAR_SOMETHING_NEW", env)

    def test_path_is_carried_because_age_and_git_have_to_be_found(self) -> None:
        env = sandbox_env(REPO, Path("/tmp/sandbox-home"))
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_the_home_is_where_woswoar_will_look(self) -> None:
        home = Path("/tmp/sandbox-home")
        env = sandbox_env(REPO, home)
        self.assertEqual(env["HOME"], str(home))
        self.assertTrue(env["XDG_DATA_HOME"].startswith(str(home)))


class TestBytecodeStaysOutOfTheTree(unittest.TestCase):
    """A `.pyc` is validated against `(mtime_seconds, size)`.

    So an edit that changes a file by no bytes within one second leaves a cache
    that still looks valid for the new source. `compare` hit exactly that and
    reported a difference between two identical trees: `logs    :` had been edited
    to `LOGS    :` and back, and the fresh checkout it was compared against had no
    cache while the working tree's still held the old bytecode.

    `PYTHONDONTWRITEBYTECODE` does not fix it -- that stops writing, not reading.
    Redirecting the cache does, and symmetrically for both sides.
    """

    def test_the_cache_is_redirected_into_the_sandbox(self) -> None:
        home = Path("/tmp/sandbox-home")
        env = sandbox_env(REPO, home)
        self.assertTrue(env["PYTHONPYCACHEPREFIX"].startswith(str(home)))

    def test_running_under_it_leaves_no_pycache_in_the_tree(self) -> None:
        """The property rather than the variable: drives a real interpreter over a
        throwaway module and checks where the bytecode landed."""
        import tempfile

        with tempfile.TemporaryDirectory() as area:
            root = Path(area)
            tree, home = root / "tree", root / "home"
            tree.mkdir()
            home.mkdir()
            (tree / "victim.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(
                [sys.executable, "-c", "import victim"],
                env=sandbox_env(tree, home),
                cwd=tree,
                capture_output=True,
                check=True,
            )
            self.assertEqual(
                list(tree.rglob("__pycache__")), [], "bytecode was written into the tree"
            )
            self.assertTrue(
                any((home / "pycache").rglob("*.pyc")), "bytecode did not reach the sandbox"
            )


class TestSeeding(unittest.TestCase):
    def test_the_machine_file_is_where_store_reads_it(self) -> None:
        """Written to `config/woswoar/machine` because that is what
        `store.config_dir` resolves to under `XDG_CONFIG_HOME`."""
        import tempfile

        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            seed_machine(root, "test@sandbox")
            written = (root / "config" / "woswoar" / "machine").read_text(encoding="utf-8")
        self.assertIn(f"id={MACHINE_ID}", written)
        self.assertIn("name=test@sandbox", written)

    def test_the_id_is_the_shape_a_real_one_is(self) -> None:
        """A short id would be caught by nothing here and would then not resemble
        what it stands in for."""
        self.assertEqual(len(MACHINE_ID), 16)
        self.assertTrue(all(char in "0123456789abcdef" for char in MACHINE_ID))


class TestUsableCpus(unittest.TestCase):
    def test_it_prefers_the_count_that_respects_an_affinity_mask(self) -> None:
        """`os.cpu_count()` reports the host's CPUs and ignores both an affinity
        mask and a container quota, which is exactly the CI case: on a two-core
        runner on a sixteen-core host it answers sixteen.

        The two are patched to *disagree*, because on an unrestricted machine they
        return the same number -- so a test comparing against the real
        `process_cpu_count` would pass just as happily against the wrong one.
        """
        with (
            mock.patch.object(os, "process_cpu_count", return_value=3, create=True),
            mock.patch.object(os, "cpu_count", return_value=99),
        ):
            self.assertEqual(usable_cpus(), 3)

    def test_it_never_answers_zero(self) -> None:
        """Both counters may return None, and a lane count of zero is a hang."""
        with mock.patch.object(os, "process_cpu_count", return_value=None, create=True):
            self.assertGreaterEqual(usable_cpus(), 1)


class TestSameFilesystem(unittest.TestCase):
    def test_a_path_shares_a_filesystem_with_itself(self) -> None:
        self.assertTrue(same_filesystem(REPO, REPO))

    def test_tmp_and_the_repo_are_told_apart_when_they_differ(self) -> None:
        """The check exists because `/tmp` is tmpfs on the machine this was
        written on while the checkout is on btrfs -- reading out of RAM against an
        SSD, which no counterbalancing removes. Skipped where they happen to
        match, since then there is nothing to distinguish."""
        if Path("/tmp").stat().st_dev == REPO.stat().st_dev:
            self.skipTest("/tmp and the repo are on one filesystem here")
        self.assertFalse(same_filesystem(Path("/tmp"), REPO))


@unittest.skipUnless(_in_a_git_repo(), "no git repository to check a revision out of")
class TestWorktree(unittest.TestCase):
    def test_it_checks_out_and_cleans_up_after_itself(self) -> None:
        with worktree("HEAD") as (path, sha):
            self.assertTrue((path / "woswoar" / "__init__.py").is_file())
            self.assertTrue(sha)
            inside = path
        self.assertFalse(inside.exists(), "the worktree outlived its context")

    def test_git_no_longer_lists_it_afterwards(self) -> None:
        """`rmtree` alone leaves a stale entry under `.git/worktrees` that makes
        the next `worktree add` on the same path fail, which is why the teardown
        is both halves."""
        with worktree("HEAD") as (path, _):
            pass
        listed = subprocess.run(
            ["git", "worktree", "list"], capture_output=True, text=True, check=True, cwd=REPO
        ).stdout
        self.assertNotIn(str(path), listed)

    def test_beside_puts_it_on_the_named_filesystem(self) -> None:
        with worktree("HEAD", beside=REPO.parent) as (path, _):
            self.assertEqual(path.parent, REPO.parent)
            self.assertTrue(same_filesystem(path, REPO))


if __name__ == "__main__":
    unittest.main()
