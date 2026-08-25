"""The CPU count every pool here is sized from.

Moved out of `tests/test_sandbox.py` when `usable_cpus` moved out of
`tools/sandbox.py`: `tools/mutants.py` maps a source file to its tests by
module name, so the tests live where the generated sweep will look for them.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tools.cpus import usable_cpus


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
