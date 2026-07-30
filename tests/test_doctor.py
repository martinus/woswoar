"""`woswoar doctor` has to notice the failures people actually hit.

The one that prompted this: an `age` that runs perfectly and then cannot open a
key, because it is sandboxed. `age --version` succeeds, so doctor reported
nothing at all -- in the exact state, before `init` has worked, where someone
would think to run it.
"""

from __future__ import annotations

import io
import os
import stat
import unittest
from contextlib import redirect_stdout

from woswoar import crypto
from woswoar.__main__ import main

from .support import WoswoarTestCase, requires_age


@requires_age
class TestSelftest(unittest.TestCase):
    def test_a_working_age_reports_nothing_wrong(self) -> None:
        self.assertEqual(crypto.selftest(), "")


class TestDoctorWithABrokenAge(WoswoarTestCase):
    """A stand-in for a confined age: on PATH, runs, refuses to do the work."""

    def setUp(self) -> None:
        super().setUp()
        fake = self.root / "bin"
        fake.mkdir()
        for name in ("age", "age-keygen"):
            script = fake / name
            script.write_text(
                "#!/bin/sh\n"
                f'echo "{name}: error: failed to open input file: permission denied" >&2\n'
                "exit 1\n"
            )
            script.chmod(script.stat().st_mode | stat.S_IXUSR)

        self._path = os.environ["PATH"]
        os.environ["PATH"] = f"{fake}{os.pathsep}{self._path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._path))

    def doctor(self) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["doctor"])
        return code, buffer.getvalue()

    def test_selftest_reports_what_age_said(self) -> None:
        failure = crypto.selftest()
        self.assertIn("permission denied", failure)

    def test_doctor_fails_and_shows_the_reason(self) -> None:
        code, out = self.doctor()
        self.assertNotEqual(code, 0, out)
        self.assertRegex(out, r"\[FAIL\] age", out)
        self.assertIn("permission denied", out)

    def test_doctor_does_not_need_a_repo_to_notice(self) -> None:
        """The whole point. This used to be gated behind `sync.is_repo()`, so
        the machine where `init` had just failed got a clean bill of health."""
        _, out = self.doctor()
        self.assertIn("no history repo", out, "precondition: no repo in this sandbox")
        self.assertRegex(out, r"\[FAIL\] age")


if __name__ == "__main__":
    unittest.main()
