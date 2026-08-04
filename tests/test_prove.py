"""`doctor --prove` must catch the leak it exists to catch.

The proof is fault injection: strip the sealing out from under the real sync
and watch the scan go red. A prove that stays green with encryption disabled
would be decoration -- on the one command whose entire job is to be believed.

The machine name is pinned to ``canaryuser@canaryhost`` throughout, because the
sandbox otherwise names itself after the account running the suite, and whether
that name is searchable (length, collision with woswoar's own boilerplate)
would then vary by machine -- the anonymity assertions here must not.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from woswoar import crypto, prove, store, sync

from . import support
from .support import WoswoarTestCase, requires_age, requires_bash, requires_git, requires_ssh_keygen


@requires_age
@requires_git
@requires_ssh_keygen
class ProveTestCase(WoswoarTestCase):
    def prove(self, name: str = "canaryuser@canaryhost") -> support.Ran:
        with mock.patch.object(store, "default_machine_name", return_value=name):
            return support.run_cli("doctor", "--prove")


class TestACleanInstallProves(ProveTestCase):
    def test_every_check_passes(self) -> None:
        ran = self.prove()
        self.assertEqual(ran.code, 0, ran.out + ran.err)
        for label in ("recorded", "published", "sealed", "unreadable", "anonymous"):
            self.assertIn(f"[ok] {label}", ran.out)
        # Both name halves were really searched, not quietly skipped.
        self.assertIn("'canaryuser'", ran.out)
        self.assertIn("'canaryhost'", ran.out)
        self.assertNotIn("not searched", ran.out)

    def test_the_real_installation_is_not_touched(self) -> None:
        """The sandbox must be a world of its own, and must be gone afterwards.

        `mkdtemp` is watched rather than the temp directory listed, because
        other suites run in parallel and their sandboxes come and go.
        """
        made: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def watched(prefix: str = "") -> str:
            path = real_mkdtemp(prefix=prefix)
            made.append(path)
            return path

        saved = {key: os.environ.get(key) for key in prove._ENV_KEYS}
        with mock.patch.object(tempfile, "mkdtemp", side_effect=watched):
            ran = self.prove()

        self.assertEqual(ran.code, 0, ran.out + ran.err)
        self.assertEqual(saved, {key: os.environ.get(key) for key in prove._ENV_KEYS})
        self.assertEqual(len(made), 1)
        self.assertFalse(Path(made[0]).exists())
        # This test environment's own store: prove created nothing in it.
        self.assertFalse(store.history_dir().exists())
        self.assertFalse(store.logs_dir().exists())


class TestAPlantedLeakIsCaught(ProveTestCase):
    """Encryption stripped out from under the real pipeline.

    Chunks, day keys and the sealed machine name all reach the remote as
    plaintext -- through the real export, the real git, the real push. The scan
    of the remote's decompressed objects is the only thing left standing
    between that and a green run, which is exactly the claim under test.
    """

    def test_readable_bytes_on_the_remote_go_red(self) -> None:
        with (
            mock.patch.object(crypto, "encrypt_to", lambda data, recipient: data),
            mock.patch.object(crypto, "encrypt_to_recipients", lambda data, recipients: data),
            mock.patch.object(sync, "pack", lambda data: data),
        ):
            ran = self.prove()
        self.assertEqual(ran.code, 1, ran.out + ran.err)
        self.assertIn("[FAIL] unreadable", ran.out)
        self.assertIn("[FAIL] anonymous", ran.out)


class TestNothingPublishedGoesRed(ProveTestCase):
    """An export that silently does nothing must not leave an absence proof
    standing: a repo that never held the canary proves nothing by lacking it."""

    def test_an_empty_sync_cannot_pass(self) -> None:
        with mock.patch.object(sync, "export", lambda known, state, report, now: False):
            ran = self.prove()
        self.assertEqual(ran.code, 1, ran.out + ran.err)
        self.assertIn("[FAIL] published", ran.out)


class TestAWrongChunkGoesRed(ProveTestCase):
    """Chunks that seal, push and decrypt perfectly -- holding the wrong bytes.

    Catches a `sealed` check that trusts the plumbing instead of reading what
    came back: every step of the pipeline succeeds here except the one fact the
    check is supposed to establish.
    """

    def test_a_chunk_without_the_canary_fails_sealed(self) -> None:
        with mock.patch.object(
            sync, "pack", lambda data: zlib.compress(b"1700000000\tprove\t~\t0\t1\tnot it\n")
        ):
            ran = self.prove()
        self.assertEqual(ran.code, 1, ran.out + ran.err)
        self.assertIn("[FAIL] sealed", ran.out)
        # The canary genuinely never reached the remote, so the absence scans
        # still hold; only the presence proof may be what failed.
        self.assertIn("[ok] unreadable", ran.out)


class TestACollidingNameIsSkippedNotFailed(ProveTestCase):
    """A user or host that collides with woswoar's own boilerplate.

    `hosts/` and `keys/` sit in every tree object, "localhost" is in the commit
    email -- a machine actually named after any of those must not be told the
    proof failed and to file a bug. The token is skipped, *said* to be skipped,
    and the full `user@host` form is searched regardless. This is the branch
    the pinned fixture name everywhere else deliberately avoids.
    """

    def test_boilerplate_and_short_tokens_are_reported_not_searched(self) -> None:
        ran = self.prove(name="keys@localhost")
        self.assertEqual(ran.code, 0, ran.out + ran.err)
        self.assertIn("[ok] anonymous", ran.out)
        self.assertIn("'keys@localhost'", ran.out)
        self.assertIn("'keys' was not searched for", ran.out)
        self.assertIn("'localhost' was not searched for", ran.out)

    def test_a_short_token_is_reported_not_searched(self) -> None:
        ran = self.prove(name="ab@canaryhost")
        self.assertEqual(ran.code, 0, ran.out + ran.err)
        self.assertIn("'ab' was not searched for", ran.out)
        self.assertIn("'canaryhost'", ran.out)


@requires_age
@requires_git
@requires_ssh_keygen
@requires_bash
class TestTheDocsRecipeStillDecrypts(unittest.TestCase):
    """docs/verify.md's "read your history without woswoar" block, executed.

    The recipe hardcodes the repo layout -- `hosts/<id>/keys/<day>.age`, the
    `machine` kv file, the zlib step -- because its whole point is to work with
    no woswoar in the pipeline. Nothing else would notice that layout moving
    under it, and a silently broken recipe is the worst outcome for a page
    whose premise is that you should not have to trust the page.
    """

    @staticmethod
    def _recipe() -> str:
        text = (Path(__file__).parent.parent / "docs" / "verify.md").read_text(encoding="utf-8")
        blocks: list[str] = [
            b for b in re.findall(r"```sh\n(.*?)```", text, re.S) if "for chunk in" in b
        ]
        assert len(blocks) == 1, "expected exactly one decrypt recipe in docs/verify.md"
        return blocks[0]

    def test_the_recipe_prints_the_recorded_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="woswoar-docs-") as tmp:
            root = Path(tmp)
            (root / "home").mkdir()
            (root / "home" / ".gitconfig").write_text(prove.QUIET_MAINTENANCE, encoding="utf-8")
            saved = {key: os.environ.get(key) for key in prove._ENV_KEYS}
            try:
                # Only HOME is set: the recipe spells paths as `~/...`, so the
                # fixture must resolve them the way a stock install would.
                for key in prove._ENV_KEYS:
                    os.environ.pop(key, None)
                os.environ["HOME"] = str(root / "home")
                origin = root / "origin.git"
                subprocess.run(
                    ["git", "init", "--quiet", "--bare", "--template=", str(origin)],
                    check=True,
                    timeout=60,
                )
                sync.initialise(remote=str(origin), new_identity=True)
                known = store.machine()

                # Twice at most: the recipe reads `date +%F` on its own clock,
                # so a run that straddles midnight records into yesterday's
                # chunk and finds nothing. Re-recording is then enough; a
                # recipe that is genuinely broken fails both times.
                for _ in range(2):
                    entry = support.make_entry(
                        int(time.time()), "echo docs-recipe-canary", host=known.id
                    )
                    store.append_entries(known.id, [entry])
                    sync.run()
                    ran = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", self._recipe()],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if ran.returncode == 0 and "echo docs-recipe-canary" in ran.stdout:
                        break
                self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
                self.assertIn("echo docs-recipe-canary", ran.stdout)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
