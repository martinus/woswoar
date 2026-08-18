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


class TestTheSignedBytesAreDeterministic(unittest.TestCase):
    """The claim `_body` makes about itself, which nothing was checking.

    Its docstring says "Sorted, so the same set of chunks always produces
    byte-identical output and a sync that adds nothing rewrites nothing." That
    sort survived mutation to both `list(...)` and `reversed(...)` against the
    whole suite, because until now no test built a manifest at all -- the two
    tests above cover signing and never reach `_body`.

    Why it matters more than tidiness: these bytes are what `crypto.sign`
    covers. Lose the order and the same chunk set signs differently on each
    run, so a sync with nothing to say rewrites and re-signs every manifest it
    touches, and every peer sees churn it must re-verify.

    This is the first entry on `CLAUDE.md`'s weak-fixture list -- "two day
    directories cannot distinguish a sorted walk from a reversed one" -- which
    is why the fixture below uses three names whose insertion order is neither
    sorted nor its reverse. Two would pass against `reversed()` half the time.
    """

    def entries(self, names: list[str]) -> dict[str, manifest.ManifestEntry]:
        return {name: manifest.ManifestEntry(digest=f"d-{name}") for name in names}

    def test_insertion_order_does_not_change_the_bytes(self) -> None:
        scrambled = self.entries(["b.age", "c.age", "a.age"])
        ordered = self.entries(["a.age", "b.age", "c.age"])
        self.assertNotEqual(list(scrambled), list(ordered), "precondition: the dicts differ")
        self.assertEqual(
            manifest._body("host", "2026-08-18", scrambled),
            manifest._body("host", "2026-08-18", ordered),
        )

    def test_the_order_is_sorted_and_not_merely_stable(self) -> None:
        """`list(...)` would satisfy the test above whenever both dicts agree.

        So this one pins the actual sequence: a fixture inserted in reverse must
        still come out ascending.
        """
        body = manifest._body("host", "2026-08-18", self.entries(["c.age", "b.age", "a.age"]))
        names = [line.split(" ", 1)[0] for line in body.splitlines()[1:]]
        self.assertEqual(names, ["a.age", "b.age", "c.age"])

    def test_subsumes_is_ordered_too(self) -> None:
        """`sync.compact` builds this tuple, and `_body` writes it into the
        signed line. Unsorted there moves the churn one level in, where the
        entry looks identical but its bytes are not."""
        one = manifest.ManifestEntry(digest="d", subsumes=("b.age", "a.age"))
        other = manifest.ManifestEntry(digest="d", subsumes=("a.age", "b.age"))
        self.assertNotEqual(
            manifest._body("host", "2026-08-18", {"merged.age": one}),
            manifest._body("host", "2026-08-18", {"merged.age": other}),
            "if this ever passes, _body sorts subsumes itself and compact need not",
        )


if __name__ == "__main__":
    unittest.main()
