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

from woswoar import crypto

from .support import requires_age, requires_ssh_keygen

#: The one path age is still given. It is a kernel object holding an inherited
#: pipe, not a file in $HOME, which is what the rule is actually about.
_ALLOWED_PATH_PREFIX = "/dev/fd/"

#: A real ed25519 public key, and the same key with its last character changed.
#: Spelled this way so "these differ in one character of key material" is
#: visible rather than something a reader has to diff by eye.
_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB7Ep0mZ0mCwvvzMLbaXn8g4mBIhnCEE1zLLuqA6IGGz"
_SSH_KEY_ALMOST = _SSH_KEY[:-1] + "y"


def make_ssh_identity(directory: Path, comment: str = "woswoar-test") -> Path:
    """A throwaway ed25519 keypair, returning the private half's path."""
    key = directory / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q", "-C", comment],
        check=True,
        timeout=60,
    )
    return key


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
        return make_ssh_identity(self.tmp)

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


class TestFingerprint(unittest.TestCase):
    """What `grant` shows instead of a name anyone with push access can write."""

    @requires_ssh_keygen
    def test_an_ssh_key_gets_the_fingerprint_ssh_keygen_prints(self) -> None:
        """Not a detail: the value of the fingerprint is that it can be checked
        against the machine itself, with the tool already installed there."""
        with tempfile.TemporaryDirectory(prefix="woswoar-fp-") as tmp:
            key = make_ssh_identity(Path(tmp), comment="box")
            # Through `recipient_for`, so this exercises the rule the product
            # uses to find the .pub sibling rather than a second guess at it.
            pub = crypto.recipient_for(key)
            listed = subprocess.run(
                ["ssh-keygen", "-lf", str(key) + ".pub"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.split()[1]

        self.assertTrue(listed.startswith("SHA256:"))
        self.assertEqual(crypto.fingerprint(pub), listed)

    def test_the_comment_field_cannot_change_an_ssh_fingerprint(self) -> None:
        # The comment is the part a label is usually taken from, and the part
        # anyone editing recipients.txt can rewrite.
        self.assertEqual(
            crypto.fingerprint(f"{_SSH_KEY} martin@laptop"),
            crypto.fingerprint(f"{_SSH_KEY} martin@laptop-but-not-really"),
        )

    def test_two_keys_never_share_a_fingerprint(self) -> None:
        self.assertNotEqual(crypto.fingerprint(_SSH_KEY), crypto.fingerprint(_SSH_KEY_ALMOST))

    def test_an_age_recipient_is_its_own_fingerprint(self) -> None:
        # Short, canonical, and reproducible with `age-keygen -y`, so hashing it
        # would only make it harder to check.
        key = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqwwwwww"
        self.assertEqual(crypto.fingerprint(f"{key}  "), key)

    def test_a_line_that_parses_as_neither_is_still_derived_from_itself(self) -> None:
        """A hand-edited recipients.txt must not take the confirmation down with
        a traceback -- that is the one moment it has to still work -- and must
        not render two different lines as the same machine either.
        """
        first = crypto.fingerprint("ssh-rsa not-base64!! martin@laptop")
        second = crypto.fingerprint("ssh-rsa also-not-base64!! martin@laptop")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("SHA256:"))
        self.assertEqual(crypto.fingerprint(""), "")

    def test_the_check_command_matches_the_kind_of_fingerprint(self) -> None:
        # Telling someone to run ssh-keygen against an age identity is advice
        # that cannot work, on the one screen where advice has to.
        self.assertIn("ssh-keygen", crypto.how_to_check(crypto.fingerprint(_SSH_KEY)))
        self.assertIn("age-keygen", crypto.how_to_check(crypto.fingerprint("age1qqqqwwwwww")))


#: Any namespace will do for these; `sync` owns the real one.
NS = "woswoar-test"


@requires_ssh_keygen
class TestSigning(unittest.TestCase):
    """Authorship, which age cannot answer and a shared secret cannot either."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="woswoar-sign-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.key = self.tmp / "signing_key"
        self.public = crypto.generate_signing_key(self.key)

    def test_a_signature_round_trips(self) -> None:
        signature = crypto.sign(b"a manifest", self.key, NS)
        self.assertTrue(crypto.verify(b"a manifest", signature, self.public, NS))

    def test_tampered_data_does_not_verify(self) -> None:
        signature = crypto.sign(b"a manifest", self.key, NS)
        self.assertFalse(crypto.verify(b"a manifest!", signature, self.public, NS))

    def test_another_machines_signature_does_not_verify(self) -> None:
        # The property the whole design rests on: holding the *verify* half is
        # not holding the ability to sign, which is what a shared MAC could
        # never give and what makes revocation mean anything.
        other = self.tmp / "other_key"
        crypto.generate_signing_key(other)
        signature = crypto.sign(b"a manifest", other, NS)
        self.assertFalse(crypto.verify(b"a manifest", signature, self.public, NS))

    def test_a_signature_from_another_namespace_does_not_verify(self) -> None:
        """Namespaces stop a signature made for one purpose counting for another.

        Mutating the namespace to "" must break this, which is what pins that it
        is actually being passed.
        """
        signed = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.key), "-n", "a-different-namespace"],
            input=b"a manifest",
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
        self.assertFalse(crypto.verify(b"a manifest", signed, self.public, NS))

    def test_the_private_half_is_owner_only(self) -> None:
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
