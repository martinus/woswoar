"""The release job must install whatever the suite needs to actually run.

`release.yml` re-runs the whole suite at the tagged commit, and that run is what
decides whether a release is cut. It is a *different file* from `ci.yml` with
its own install steps, so anything the suite grows a dependency on has to be
added in two places -- and three times it was added in one:

- fzf, where the suite did not skip but took a different branch, so the two
  jobs ran different code;
- zsh, where `doctor` reported "zsh not found" here and nowhere else, and
  v0.10.0 failed at `tests` with every check on both pull requests green;
- hypothesis, where the eight property-test modules failed to *import*, and
  v0.12.0 failed at `tests` the same way.

Every one was found by a failed release rather than by a red test, which is the
most expensive place to find it: the tag is already pushed and public.
`release.yml` has stated the rule in prose since the second one -- "whatever
`ci.yml`'s `test` job installs, this has to install" -- and prose did not stop
the third. This is that sentence as an assertion.

Read textually rather than with a YAML parser, and that is the point rather than
a shortcut: PyYAML is not in `[test]`, so importing it here would fail to import
in exactly the job this module exists to protect -- the same defect, one level
up. A workflow file is also the one kind of input where the surface syntax is
what a reviewer reads, so matching on it is matching on what they see.

The cost of reading it textually is `_uncommented`, and that is worth reading
before trusting anything here: these workflows quote the commands they run
inside their own comments, so the first version of this module passed with the
`pip install` line deleted.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

#: The job in `ci.yml` whose installs `release.yml` has to mirror. Only this
#: one: `hook` installs strace and locales for tests that legitimately skip
#: without them, and the release job is not obliged to carry those.
MIRRORED_JOB = "test"


def _uncommented(block: str) -> str:
    """The block with whole-line `#` comments removed.

    Not tidiness -- without it every assertion below is satisfiable by prose.
    These files argue for themselves at length and quote the very commands they
    run, so `release.yml`'s own comment contains the string
    `pip install -e .[test]`; the first version of this module matched it and
    passed with the `run:` line deleted. The mutation that should have been the
    easiest to catch was the one that survived.

    Whole-line only. A `#` inside a `run:` line is shell, not YAML, and cutting
    from it would silently truncate a command.
    """
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


def _job(workflow: str, name: str) -> str:
    """One job's block out of a workflow file, comments stripped.

    Jobs are the two-space keys under `jobs:`, so a block runs until the next
    one. Splitting on that rather than tracking indentation, because a run step's
    own YAML is arbitrarily deep and the only thing that reliably ends a job is
    the start of the next.
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    _, _, jobs = text.partition("\njobs:\n")
    for block in re.split(r"\n(?=  [\w-]+:\n)", jobs):
        if block.strip().split(":", 1)[0].strip() == name:
            return _uncommented(block)
    raise AssertionError(f"no job {name!r} in {workflow}")


def _apt(block: str) -> set[str]:
    """Package names from every `apt-get install` in a block."""
    found: set[str] = set()
    for args in re.findall(r"apt-get install((?:[^\n]|\\\n)*)", block):
        for word in args.replace("\\\n", " ").split():
            # `-y`, `-qq` and the `; }` that closes the retry fallback.
            if not word.startswith("-") and word.isidentifier():
                found.add(word)
    return found


def _pip_extras(block: str) -> set[str]:
    """The extras named in every `pip install -e .[...]` in a block."""
    return {
        extra.strip()
        for group in re.findall(r"pip install -e \.\[([^\]]+)\]", block)
        for extra in group.split(",")
    }


class TestReleaseInstallsWhatCiInstalls(unittest.TestCase):
    def setUp(self) -> None:
        self.ci = _job("ci.yml", MIRRORED_JOB)
        self.release = _job("release.yml", "release")

    def test_the_extractor_found_something_to_compare(self) -> None:
        """Every assertion below is a subset check, and a subset check against
        an empty set passes while proving nothing. This is the guard: if either
        workflow is reshaped so the blocks stop being found, that shows up here
        rather than as three silently vacuous tests."""
        self.assertIn("apt-get install", self.ci)
        self.assertIn("apt-get install", self.release)
        self.assertTrue(_apt(self.ci), "no apt packages parsed out of ci.yml")
        self.assertTrue(_pip_extras(self.ci), "no pip extras parsed out of ci.yml")

    def test_every_apt_package_is_installed_for_the_release_too(self) -> None:
        """zsh, and the release it cost."""
        missing = _apt(self.ci) - _apt(self.release)
        self.assertEqual(
            missing, set(), f"ci.yml's {MIRRORED_JOB} job installs these, release.yml does not"
        )

    def test_every_pip_extra_is_installed_for_the_release_too(self) -> None:
        """hypothesis, and the release it cost. The extra rather than the
        package: `[test]` is where a future dependency will be added, and
        naming the package here would pass the day someone adds a second one."""
        missing = _pip_extras(self.ci) - _pip_extras(self.release)
        self.assertEqual(
            missing, set(), f"ci.yml's {MIRRORED_JOB} job installs these, release.yml does not"
        )

    def test_the_fzf_pin_is_the_same_on_both(self) -> None:
        """Both fetch fzf from upstream rather than apt, because ubuntu-latest
        ships one release below `search.TRANSFORM_SINCE`. Two hand-written pins
        of the same fact drift, and the release is the copy nobody runs until
        it matters."""
        pins = [re.findall(r"FZF_VERSION: *([\d.]+)", b) for b in (self.ci, self.release)]
        self.assertTrue(all(pins), f"an FZF_VERSION pin went missing: {pins}")
        self.assertEqual(set(pins[0]), set(pins[1]), "the fzf pins have drifted apart")

    def test_the_release_suite_runs_the_derandomised_profile(self) -> None:
        """`ci.yml` sets `WOSWOAR_HYPOTHESIS_PROFILE: ci` for every job. Without
        it `tests/profiles.py` loads the randomised `dev` profile, so the one run
        that decides whether a release is cut could fail on an example no pull
        request ever drew -- and pass on the re-run."""
        self.assertIn("WOSWOAR_HYPOTHESIS_PROFILE: ci", self.release)


if __name__ == "__main__":
    unittest.main()
