"""How key material reaches `age`.

The rule: woswoar reads key files itself and hands `age` the bytes. See
:func:`woswoar.crypto.decrypt_with_file` for why, and the design document for
the incident that established it.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from woswoar import crypto, store

from .support import requires_age, requires_ssh_keygen

#: The one path age is still given. It is a kernel object holding an inherited
#: pipe, not a file in $HOME, which is what the rule is actually about.
_ALLOWED_PATH_PREFIX = "/dev/fd/"


@requires_age
class TestNoAgeCallNamesAFile(unittest.TestCase):
    """Asserted at the seam every age invocation passes through.

    Per-function tests would only cover the functions someone remembered to
    write one for -- and that is not hypothetical: the first version of this
    fix left ``encrypt_to_recipients`` passing ``-R recipients.txt``, so the
    reported machine would still have failed, one step later.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="woswoar-crypto-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def age_identity(self) -> Path:
        path = self.tmp / "identity"
        path.write_text(crypto.generate_identity().secret, encoding="utf-8")
        return path

    def ssh_identity(self) -> Path:
        key = self.tmp / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q", "-C", "woswoar-test"],
            check=True,
            timeout=60,
        )
        return key

    def assert_named_no_file(self, spy: mock.Mock) -> None:
        for call in spy.call_args_list:
            for arg in call.args[0]:
                if arg.startswith(_ALLOWED_PATH_PREFIX):
                    continue
                self.assertFalse(
                    Path(arg).exists(),
                    f"age was given a real path: {arg!r} in {call.args[0]!r}",
                )

    def exercise(self, identity: Path) -> None:
        """Every entry point that touches key material, once."""
        recipient = crypto.recipient_for(identity)
        sealed = crypto.encrypt_to(b"payload", recipient)
        self.assertEqual(crypto.decrypt_with_file(sealed, identity), b"payload")

        to_many = crypto.encrypt_to_recipients(b"payload", [recipient])
        self.assertEqual(crypto.decrypt_with_file(to_many, identity), b"payload")

        self.assertEqual(crypto.why_unusable(identity), "")

    def test_age_identity(self) -> None:
        identity = self.age_identity()
        with mock.patch.object(crypto, "_run", wraps=crypto._run) as spy:
            self.exercise(identity)
        self.assert_named_no_file(spy)

    @requires_ssh_keygen
    def test_ssh_identity(self) -> None:
        """The case that sent the investigation the wrong way.

        `why_unusable` reported an unencrypted ed25519 key as needing a
        passphrase, because age could not open it and a passphrase was the only
        failure the code modelled.
        """
        identity = self.ssh_identity()
        with mock.patch.object(crypto, "_run", wraps=crypto._run) as spy:
            self.exercise(identity)
        self.assert_named_no_file(spy)

    def test_sealing_to_several_recipients_reaches_all_of_them(self) -> None:
        # `-R <file>` became repeated `-r <key>`; this is what that has to keep
        # doing.
        first, second = self.age_identity(), self.tmp / "second"
        second.write_text(crypto.generate_identity().secret, encoding="utf-8")
        sealed = crypto.encrypt_to_recipients(
            b"payload", [crypto.recipient_for(first), crypto.recipient_for(second)]
        )
        self.assertEqual(crypto.decrypt_with_file(sealed, first), b"payload")
        self.assertEqual(crypto.decrypt_with_file(sealed, second), b"payload")

    def test_sealing_to_nobody_says_so(self) -> None:
        with self.assertRaises(crypto.AgeError) as caught:
            crypto.encrypt_to_recipients(b"payload", [])
        self.assertIn("no recipients", str(caught.exception))


@requires_age
class TestWhyUnusable(unittest.TestCase):
    def test_an_unreadable_identity_is_not_reported_as_a_passphrase(self) -> None:
        # "needs a passphrase" points at --new-identity, which cannot help when
        # the real problem is that the file could not be opened at all.
        reason = crypto.why_unusable(Path("/nonexistent/woswoar/identity"))
        self.assertIn("cannot be read", reason)
        self.assertNotIn("passphrase", reason)


class TestChunkFraming(unittest.TestCase):
    def test_the_tag_width_store_uses_matches_the_one_crypto_produces(self) -> None:
        """`store` deliberately does not import `crypto`.

        `crypto` pulls in subprocess and shutil at module scope, and `store` is
        imported on every Ctrl-R -- so the width is spelled out in both, and the
        only thing keeping them equal is this. If they drift, every chunk in an
        append-only repo is framed at one width and read at another.
        """
        self.assertEqual(store._TAG_BYTES, crypto.TAG_BYTES)
        self.assertEqual(len(crypto.tag(crypto.new_mac_key(), b"x")), store._TAG_BYTES)

    def test_a_chunk_round_trips_through_its_frame(self) -> None:
        key = crypto.new_mac_key()
        sealed = b"pretend-this-is-age-output"
        blob = store.frame_chunk(sealed, crypto.tag(key, sealed))
        back, tag = store.split_chunk(blob)
        self.assertEqual(back, sealed)
        self.assertTrue(crypto.tag_matches(key, back, tag))

    def test_a_blob_too_short_to_be_framed_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            store.split_chunk(b"x" * store._TAG_BYTES)


if __name__ == "__main__":
    unittest.main()
