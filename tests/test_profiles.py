"""The Hypothesis profiles, and the wiring that makes the `mutation` one apply.

Ported from `martinus/tupferl` (Apache-2.0) with the numbers re-pointed at this
project's profiles.

Two of these assertions look like restatements of the constants and are not. The
`mutation` profile only ever takes effect if `tools/mutate.py` sets the same
environment variable `tests/profiles.py` reads -- two strings in two files with
nothing but a convention between them. If they drift, every mutant silently pays
the full example budget, a sweep takes hours instead of minutes, and *nothing
fails*: the run is still correct, just slow enough that nobody runs it.

The derandomisation is the half that would be wrong rather than slow. A
randomised baseline and a randomised mutant draw different examples, so "the
baseline passed and the mutant failed" stops meaning "a test noticed".
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from hypothesis import settings

from tests import profiles
from tools import mutate

ROOT = Path(__file__).resolve().parent.parent


class TestTheProfilesExist(unittest.TestCase):
    def test_all_three_are_registered(self) -> None:
        for name in ("dev", "ci", "mutation"):
            with self.subTest(profile=name):
                self.assertIsNotNone(settings.get_profile(name))

    def test_the_mutation_profile_is_the_cheap_one(self) -> None:
        """Cheaper than `ci`, which is the point of having it."""
        self.assertLess(
            settings.get_profile("mutation").max_examples,
            settings.get_profile("ci").max_examples,
        )

    def test_the_non_interactive_profiles_are_derandomised(self) -> None:
        for name in ("ci", "mutation"):
            with self.subTest(profile=name):
                self.assertTrue(settings.get_profile(name).derandomize)

    def test_the_default_is_not(self) -> None:
        """A developer's run should find new falsifying examples; that is the
        whole reason to run these at all locally."""
        self.assertFalse(settings.get_profile("dev").derandomize)

    def test_the_stateful_machines_shrink_under_mutation_too(self) -> None:
        """The machine budgets live beside the profiles and are keyed by the
        same variable, so a sweep that shrank `max_examples` but left a
        250-example state machine running would still take hours."""
        code = (
            "from tests import profiles;"
            "print(profiles.EXPORT_MACHINE, profiles.IMPORT_MACHINE)"
        )
        said = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env={**os.environ, profiles.ENV: "mutation"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual("(3, 4) (3, 4)", said.strip())


#: Asks a fresh interpreter what each profile says, so the answer is the module's
#: import-time behaviour rather than this process's already-loaded state.
ASK = (
    "from hypothesis import settings;"
    "from tests import profiles;"
    "print({n: (settings.get_profile(n).derandomize, settings.get_profile(n).max_examples)"
    " for n in ('dev', 'ci', 'mutation')})"
)


class TestTheProfilesDoNotDependOnTheAmbientEnvironment(unittest.TestCase):
    """The failure this file exists for, found by tupferl's CI rather than here.

    Hypothesis registers and loads a derandomised profile of its own when it sees
    `CI` in the environment, and a profile that leaves a field unstated inherits
    whatever is default when it is registered. So a `dev` that did not state
    `derandomize` was randomised locally and derandomised on a runner -- one
    name, two meanings, decided by a variable nothing in this project mentions.

    Run in a subprocess because the interesting behaviour happens at import, and
    `tests.profiles` is imported long before any test runs.
    """

    def ask(self, **env: str) -> str:
        done = subprocess.run(
            [sys.executable, "-c", ASK],
            cwd=ROOT,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def test_the_profiles_are_the_same_with_and_without_ci(self) -> None:
        without = self.ask(CI="")
        with_ci = self.ask(CI="true")
        self.assertEqual(without, with_ci)

    def test_and_they_are_the_ones_this_module_asserts(self) -> None:
        """The precondition: two identical answers prove nothing if the answer
        itself is wrong, or if the subprocess failed to say anything useful."""
        self.assertIn("'dev': (False, 400)", self.ask(CI="true"))


class TestTheWiring(unittest.TestCase):
    def test_the_harness_and_the_profiles_agree_on_the_variable(self) -> None:
        """The drift that would cost hours per sweep and fail nothing."""
        self.assertEqual(profiles.ENV, mutate._PROFILE)

    def test_the_harness_asks_for_a_profile_that_exists(self) -> None:
        """The name the harness actually passes, not one retyped here.

        `load_profile` on an unregistered name raises inside the probe, where it
        surfaces as `BROKE` on every row rather than as the typo it is.
        """
        self.assertIsNotNone(settings.get_profile(mutate._MUTATION_PROFILE))


if __name__ == "__main__":
    unittest.main()
