"""How identities reach `age`.

The rule these pin: woswoar reads key files itself and hands `age` the bytes.
Passing a path instead makes every operation depend on age being able to open
that path, which is a different question from whether the user can -- a
sandboxed age (snap, flatpak, anything confined) is refused access to
``~/.config`` and ``~/.ssh`` and reports "permission denied" for a file its
owner can plainly read.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from woswoar import crypto

requires_age = unittest.skipUnless(crypto.available(), "age required")
requires_ssh_keygen = unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen required")


class ArgvSpy:
    """Records every argv crypto hands to a subprocess."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._real = crypto._run

    def __enter__(self) -> ArgvSpy:
        def spy(
            argv: list[str],
            data: bytes | None = None,
            pass_fds: tuple[int, ...] = (),
        ) -> bytes:
            self.calls.append(list(argv))
            return self._real(argv, data, pass_fds)

        crypto._run = spy
        return self

    def __exit__(self, *exc: object) -> None:
        crypto._run = self._real

    def mentions(self, needle: str) -> bool:
        return any(needle in arg for call in self.calls for arg in call)


@requires_age
class TestIdentitiesAreNeverPassedAsPaths(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="woswoar-crypto-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def age_identity(self) -> Path:
        path = self.root / "identity"
        identity = crypto.generate_identity()
        path.write_text(identity.secret, encoding="utf-8")
        return path

    def test_decrypt_with_file_does_not_name_the_identity(self) -> None:
        path = self.age_identity()
        sealed = crypto.encrypt_to(b"payload", crypto.recipient_for(path))
        with ArgvSpy() as spy:
            self.assertEqual(crypto.decrypt_with_file(sealed, path), b"payload")
        self.assertFalse(spy.mentions(str(path)), f"identity path leaked into argv: {spy.calls}")

    def test_recipient_for_does_not_name_the_identity(self) -> None:
        path = self.age_identity()
        with ArgvSpy() as spy:
            self.assertTrue(crypto.recipient_for(path).startswith("age1"))
        self.assertFalse(spy.mentions(str(path)), f"identity path leaked into argv: {spy.calls}")

    @requires_ssh_keygen
    def test_an_unencrypted_ssh_key_is_usable(self) -> None:
        """The case that sent this investigation the wrong way.

        `usable()` reported an unencrypted ed25519 key as needing a passphrase,
        because age could not open it and the only failure the code knew about
        was a passphrase.
        """
        key = self.root / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q", "-C", "woswoar-test"],
            check=True,
            timeout=60,
        )
        self.assertEqual(crypto.why_unusable(key), "")

        sealed = crypto.encrypt_to(b"payload", crypto.recipient_for(key))
        with ArgvSpy() as spy:
            self.assertEqual(crypto.decrypt_with_file(sealed, key), b"payload")
        self.assertFalse(spy.mentions(str(key)), f"ssh key path leaked into argv: {spy.calls}")

    def test_an_unreadable_identity_is_reported_as_unreadable(self) -> None:
        # Not as "needs a passphrase", which is what it used to say and which
        # points at a fix that cannot work.
        missing = self.root / "gone"
        self.assertIn("cannot be read", crypto.why_unusable(missing))


if __name__ == "__main__":
    unittest.main()
