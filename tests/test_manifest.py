"""The authenticity layer, reached directly rather than through `sync.run()`.

That is the whole point of #201 splitting this out, and the first thing it bought
is this file: `manifest.signs_and_verifies` is the check behind `doctor`'s
``signing`` line, and until it was a function of its own **nothing tested it**.
Its logic sat inline in `sync.signing_status`, `doctor.repo_checks` returns before
reaching that line whenever there is no repository, and no test in the suite ever
got as far as the round trip. Mutating the verify call to ``return False``
survived the entire suite.

So this is not a unit test written for tidiness. It covers a `doctor` verdict a
user reads, on the path where "this machine cannot sign" is the difference
between publishing history the fleet accepts and publishing history every peer
silently refuses.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from woswoar import crypto, manifest

from .support import requires_ssh_keygen


@requires_ssh_keygen
class TestTheSigningSelfTest(unittest.TestCase):
    """Driving real `ssh-keygen`, as the rest of this suite does.

    A mock would assert that `crypto.sign` was called with `_MAGIC`, which is a
    restatement of the code. What matters is whether an unattended sync will
    actually be able to sign a manifest, and only the real tool answers that.
    """

    def test_a_fresh_key_signs_and_verifies_against_its_own_public_half(self) -> None:
        with tempfile.TemporaryDirectory() as area:
            key = Path(area) / "signing_key"
            verify_key = crypto.generate_signing_key(key)
            self.assertTrue(manifest.signs_and_verifies(key, verify_key))

    def test_it_is_false_against_another_machines_public_half(self) -> None:
        """The half that makes the `[ok]` mean something.

        A check that only ever ran the happy path would pass just as well if it
        ignored the verify key -- and the failure it exists to catch is precisely
        a signing key whose signatures nobody, including this machine, accepts.
        """
        with tempfile.TemporaryDirectory() as area:
            mine = Path(area) / "mine"
            theirs = Path(area) / "theirs"
            crypto.generate_signing_key(mine)
            other_public = crypto.generate_signing_key(theirs)
            self.assertFalse(manifest.signs_and_verifies(mine, other_public))


if __name__ == "__main__":
    unittest.main()
