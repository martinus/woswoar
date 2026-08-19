"""The parallel runner's job is to refuse to call a partial run green.

CI's verdict rests on this script, so the interesting tests are the ones where
something goes wrong underneath it: a batch that dies without reporting, a class
that never runs, a suite that is green only because a tool was missing. Each of
those is a way to get a passing build out of tests that did not happen.

The cases are driven through fixture modules built in a scratch directory rather
than through real classes from this suite. Two reasons: a real class ties these
tests to a file someone may rename, and one case needs a test that *skips*,
which would otherwise mean depending on `tests.test_perf` skipping -- so a
developer with WOSWOAR_BENCH exported would get a failure here that has nothing
to do with the runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import run_tests  # noqa: E402

PASSES = "    def test_ok(self) -> None:\n        pass\n"
FAILS = "    def test_bad(self) -> None:\n        self.assertEqual(1, 2)\n"
SKIPS = "    @unittest.skip('a tool is missing')\n    def test_gone(self) -> None:\n        pass\n"


@contextmanager
def fixture(*bodies: str) -> Iterator[list[str]]:
    """Build throwaway test modules and make the runner discover exactly those.

    Yields their class names. PYTHONPATH is what carries them into the worker,
    which is a separate interpreter; patching `discover` only reaches the parent.
    """
    with tempfile.TemporaryDirectory(prefix="woswoar-runner-test-") as tmp:
        classes: dict[str, list[str]] = {}
        for index, body in enumerate(bodies):
            module = f"zz_fixture_{index}"
            Path(tmp, f"{module}.py").write_text(
                f"import unittest\n\n\nclass TestFixture(unittest.TestCase):\n{body}",
                encoding="utf-8",
            )
            name = f"{module}.TestFixture"
            classes[name] = [f"{name}.{body.split('def ')[1].split('(')[0]}"]
        path = os.pathsep.join(filter(None, [tmp, os.environ.get("PYTHONPATH")]))
        # sys.path as well as PYTHONPATH, so a test may also load the fixture
        # in-process; sys.modules is purged after, or the next fixture's
        # identically named module would resolve to this one's stale copy.
        sys.path.insert(0, tmp)
        try:
            with (
                mock.patch.dict(os.environ, {"PYTHONPATH": path}),
                mock.patch.object(
                    run_tests, "discover", lambda: run_tests.Found(dict(classes), {})
                ),
            ):
                yield list(classes)
        finally:
            sys.path.remove(tmp)
            for name in classes:
                sys.modules.pop(name.split(".")[0], None)


def run_main(*argv: str) -> tuple[int, str]:
    """Call main(), returning its exit code and everything it printed.

    Both streams, because both land in the CI log and that is what a human
    actually reads: the accounting on stdout, tracebacks on stderr.
    """
    out = StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        code = run_tests.main(list(argv))
    return code, out.getvalue()


@contextmanager
def tampering(mutate: Callable[[list[str]], list[str]]) -> Iterator[None]:
    """Let each worker run for real, then rewrite what it reported having run."""
    real = subprocess.run

    def wrapper(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        done = real(argv, **kwargs)  # type: ignore[call-overload]
        out = Path(argv[argv.index("--out") + 1])
        if out.exists():
            report = json.loads(out.read_text(encoding="utf-8"))
            report["ran"] = mutate(report["ran"])
            out.write_text(json.dumps(report), encoding="utf-8")
        return done  # type: ignore[no-any-return]

    with mock.patch.object(subprocess, "run", wrapper):
        yield


class TestDiscovery(unittest.TestCase):
    def test_every_test_is_assigned_to_exactly_one_batch(self) -> None:
        """The batching must partition the real suite -- no gaps, no double runs."""
        classes = run_tests.discover().classes
        ids = [tid for group in classes.values() for tid in group]
        self.assertEqual(len(ids), len(set(ids)), "a test landed in two batches")

        serial = unittest.defaultTestLoader.discover(
            str(ROOT), pattern="test_*.py", top_level_dir=str(ROOT)
        )
        self.assertEqual(set(ids), {t.id() for t in run_tests._walk(serial)})

    def test_a_module_that_cannot_be_imported_is_a_red_test_not_a_smaller_suite(self) -> None:
        """unittest represents a load error as a _FailedTest; it must survive _walk.

        If it were dropped, a module that stopped importing would quietly shrink
        the suite and the build would stay green.
        """
        loaded = unittest.defaultTestLoader.loadTestsFromNames(["tests.not_a_real_module"])
        self.assertEqual(len(run_tests._walk(loaded)), 1)


class TestPacking(unittest.TestCase):
    def test_every_class_lands_in_exactly_one_batch(self) -> None:
        classes = {f"m.C{i}": ["x"] * (i % 5 + 1) for i in range(20)}
        batches = run_tests.pack(classes, 6)
        placed = [name for batch in batches for name in batch]
        self.assertEqual(sorted(placed), sorted(classes))
        self.assertLessEqual(len(batches), 6)

    def test_more_batches_than_classes_does_not_produce_empty_batches(self) -> None:
        """An empty batch would start an interpreter to run nothing."""
        for classes, bins in (({"m.C": ["x"]}, 32), ({f"m.C{i}": ["x"] for i in range(3)}, 40)):
            batches = run_tests.pack(classes, bins)
            self.assertTrue(all(batches), f"an empty batch in {batches}")
            self.assertEqual(len(batches), len(classes))

    def test_selection_is_anchored_so_a_similar_name_is_not_dragged_in(self) -> None:
        self.assertTrue(run_tests.selects("tests.test_sync", "tests.test_sync"))
        self.assertTrue(run_tests.selects("tests.test_sync.TestX", "tests.test_sync"))
        self.assertFalse(run_tests.selects("tests.test_sync_chunks.TestX", "tests.test_sync"))


class TestShardingSplitsOneSuiteOverSeveralMachines(unittest.TestCase):
    """`--shard I/N` is what stops one slow suite from being a job's whole cost.

    The property to hold is coverage, not speed: run every shard of a split and
    each test must have run exactly once. A test that only checked "a shard runs
    fewer tests" would pass against a partition that dropped a class on the
    floor -- and that failure is green, which is the one this file exists for.

    Driven through classes that *fail* on purpose, because the summary names the
    ids it failed and so says which tests actually ran. Counting them would not:
    two shards running the same four tests and two shards splitting eight both
    add up to eight.
    """

    def failures(self, text: str) -> list[str]:
        return [line.split("FAIL: ")[1] for line in text.splitlines() if "FAIL: " in line]

    def test_the_shards_together_run_every_test_exactly_once(self) -> None:
        seen: list[str] = []
        with fixture(FAILS, FAILS, FAILS, FAILS) as classes:
            for index in (1, 2, 3):
                code, text = run_main("--jobs", "2", "--shard", f"{index}/3")
                self.assertEqual(code, 1, text)
                # No shard may be empty either: one that ran nothing is the
                # partial run that passes, and here it would hide in the union.
                self.assertTrue(self.failures(text), f"shard {index}/3 ran nothing:\n{text}")
                seen.extend(self.failures(text))
        self.assertEqual(len(seen), len(set(seen)), f"a test ran in two shards: {seen}")
        self.assertEqual({tid.rsplit(".", 1)[0] for tid in seen}, set(classes))

    def test_one_shard_runs_less_than_the_whole(self) -> None:
        """Otherwise the union above is satisfied by a --shard that does nothing."""
        with fixture(PASSES, PASSES, PASSES, PASSES):
            code, whole = run_main("--jobs", "2")
            _, part = run_main("--jobs", "2", "--shard", "1/4")
        self.assertEqual(code, 0, whole)
        self.assertIn("Ran 4 tests", whole)
        self.assertIn("Ran 1 tests in", part)
        # Named in the summary: four green jobs that each ran a quarter of the
        # suite read exactly like four that each ran all of it.
        self.assertIn("(shard 1/4)", part)

    def test_more_shards_than_classes_is_fatal_rather_than_an_empty_green_run(self) -> None:
        """A matrix that outgrew the suite must be red, not quietly idle."""
        with fixture(PASSES, PASSES):
            code, text = run_main("--jobs", "2", "--shard", "3/3")
        self.assertEqual(code, 1, text)
        self.assertIn("more shards than there are classes", text)

    def test_a_malformed_or_out_of_range_shard_is_fatal(self) -> None:
        for spec in ("1", "0/2", "3/2", "1/0", "one/two", "/2", "2/"):
            with self.subTest(spec=spec), fixture(PASSES, PASSES):
                code, text = run_main("--jobs", "2", "--shard", spec)
            self.assertEqual(code, 1, text)
            self.assertIn("--shard", text)

    def test_sharding_composes_with_only_and_no_skips(self) -> None:
        """The macOS job passes all three at once, so the combination is the case.

        The skipping class is selected *into* the shard being run, so --no-skips
        still has something to catch -- a shard that quietly dropped it would
        otherwise turn this green.
        """
        with fixture(SKIPS, SKIPS) as classes:
            code, text = run_main(
                "--jobs", "2", "--no-skips", "--only", classes[0], "--shard", "1/1"
            )
        self.assertEqual(code, 1, text)
        self.assertIn("tests were skipped", text)


class TestExclusionKeepsTheRestUnderNoSkips(unittest.TestCase):
    """The macOS case: one class cannot run there, and the guard must survive it.

    `strace` does not exist on macOS, so `TestForkFree` cannot run -- and
    dropping `--no-skips` for the whole module to accommodate it is exactly the
    silently-green suite that flag exists to prevent.
    """

    def test_an_excluded_class_does_not_run_and_does_not_count_as_a_skip(self) -> None:
        with fixture(PASSES, SKIPS) as classes:
            code, text = run_main("--jobs", "2", "--no-skips", "--exclude", classes[1])
        self.assertEqual(code, 0, text)
        self.assertIn("Ran 1 test", text)

    def test_without_the_exclusion_the_same_run_is_red(self) -> None:
        """Otherwise the test above passes against an `--exclude` that does
        nothing, because the class it names never skipped."""
        with fixture(PASSES, SKIPS):
            code, _ = run_main("--jobs", "2", "--no-skips")
        self.assertEqual(code, 1)

    def test_an_exclusion_that_matches_nothing_is_fatal(self) -> None:
        """A renamed class would otherwise stop being excluded silently, and the
        job that needs it is the one running where the tool is absent."""
        with fixture(PASSES):
            code, text = run_main("--jobs", "2", "--exclude", "zz_fixture_0.TestGone")
        self.assertEqual(code, 1)
        self.assertIn("no test class matches --exclude", text)

    def test_one_stale_pattern_beside_a_live_one_is_still_fatal(self) -> None:
        """The shape the caller actually uses: the macOS job passes two.

        Asking only whether the *set* shrank cannot see this -- the live pattern
        shrinks it -- so a single-pattern test passes against a check that lets a
        rename go by silently. This is the case that distinguishes them.
        """
        with fixture(PASSES, SKIPS) as classes:
            code, text = run_main(
                "--jobs", "2", "--exclude", classes[1], "--exclude", "zz_fixture_0.TestGone"
            )
        self.assertEqual(code, 1)
        self.assertIn("TestGone", text)


class TestABatchThatDies(unittest.TestCase):
    """The failure a serial runner cannot have: a worker that never reports."""

    def test_a_worker_that_writes_no_report_fails_the_run(self) -> None:
        def dies(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            # Segfault, OOM kill, or a crash in setUpClass: the process is gone
            # and --out was never written.
            return subprocess.CompletedProcess(argv, returncode=-9)

        with fixture(PASSES), mock.patch.object(subprocess, "run", dies):
            code, text = run_main("--jobs", "2")

        self.assertEqual(code, 1, "a run whose batches all died reported success")
        self.assertIn("never ran", text)
        self.assertIn("batch died without reporting", text)

    def test_a_worker_that_reports_fewer_tests_than_it_was_given_fails_the_run(self) -> None:
        """The count is not enough: the ids must match by name.

        A batch that silently runs half its classes and reports success is the
        subtle version of the same bug.
        """
        with fixture(PASSES, PASSES), tampering(lambda ran: ran[:-1]):
            code, text = run_main("--jobs", "2")

        self.assertEqual(code, 1, "a batch that dropped a test reported success")
        self.assertIn("never ran", text)

    def test_a_test_counted_twice_fails_the_run(self) -> None:
        """The mirror of the missing case, and the one the id set alone misses.

        If two batches ever overlap, every expected id is present and only the
        duplicate shows it. A run that double-counts is a run whose totals
        cannot be believed.
        """
        with fixture(PASSES), tampering(lambda ran: ran + ran[:1]):
            code, text = run_main("--jobs", "2")

        self.assertEqual(code, 1, "a run that counted a test twice reported success")
        self.assertIn("ran more than once", text)


class TestSkipsCanBeFatal(unittest.TestCase):
    """`age` missing must not look like a passing suite -- the sync job's rule."""

    def test_a_skipped_test_fails_the_run_under_no_skips(self) -> None:
        with fixture(SKIPS):
            code, text = run_main("--jobs", "2", "--no-skips")
        self.assertEqual(code, 1)
        self.assertIn("were skipped", text)

    def test_the_same_run_is_green_when_skips_are_allowed(self) -> None:
        with fixture(SKIPS):
            code, _ = run_main("--jobs", "2")
        self.assertEqual(code, 0)


class TestAModuleThatWillNotImport(unittest.TestCase):
    """#221: "this module contributed nothing" is not "a test failed".

    `unittest.loader` substitutes a synthetic `_FailedTest` for a module it
    cannot import, and that placeholder **is** a `TestCase` -- so before this it
    packed into a batch as an ordinary class, ran nothing, and came out the far
    end as `1 discovered test never ran`, naming `unittest.loader._FailedTest`.
    Red, which is why nobody was hurt, and wrong about what happened.
    """

    @contextmanager
    def tree(self, *modules: str) -> Iterator[Path]:
        """A throwaway package of test modules, discovered for real.

        For real, rather than through `fixture`'s patched `discover`, because
        discovery is the thing under test here. In a directory of its own, and
        that is not fastidiousness: writing a deliberately broken module into
        this repository's `tests/` would be found by the 64 workers running this
        very suite.
        """
        with tempfile.TemporaryDirectory(prefix="woswoar-unloadable-") as tmp:
            root = Path(tmp)
            for index, body in enumerate(modules):
                (root / f"test_mod{index}.py").write_text(body, encoding="utf-8")
            yield root

    def test_a_broken_module_is_reported_as_unloadable_not_as_a_test(self) -> None:
        with self.tree("import nosuchmodule_xyz\n") as root:
            found = run_tests.discover(root)
        self.assertEqual(found.classes, {})
        self.assertEqual(list(found.unloadable), ["test_mod0"])
        self.assertIn("Failed to import test module", found.unloadable["test_mod0"])

    def test_the_modules_that_did_import_are_still_discovered(self) -> None:
        """The half that fails if the placeholder is filtered by class rather
        than by module: everything else must survive the filter."""
        with self.tree(
            "import nosuchmodule_xyz\n",
            f"import unittest\n\n\nclass TestFixture(unittest.TestCase):\n{PASSES}",
        ) as root:
            found = run_tests.discover(root)
        self.assertEqual(list(found.classes), ["test_mod1.TestFixture"])
        self.assertEqual(list(found.unloadable), ["test_mod0"])

    def test_the_run_is_red_and_says_which_module(self) -> None:
        with mock.patch.object(
            run_tests,
            "discover",
            lambda: run_tests.Found({}, {"tests.test_gone": "Failed to import test module: x"}),
        ):
            code, text = run_main("--jobs", "2")
        self.assertEqual(code, 1)
        self.assertIn("could not import tests.test_gone", text)
        # Not the other thing. A reader who sees this looks for an assertion.
        self.assertNotIn("never ran", text)

    def test_only_naming_a_broken_module_says_so(self) -> None:
        """`--only` on a module that will not import used to answer "no test
        class matches" -- true, and the opposite of what the person typing it is
        trying to find out."""
        with mock.patch.object(
            run_tests,
            "discover",
            lambda: run_tests.Found({}, {"tests.test_gone": "Failed to import test module: x"}),
        ):
            code, text = run_main("--only", "tests.test_gone", "--jobs", "2")
        self.assertEqual(code, 1)
        self.assertIn("could not import tests.test_gone", text)
        self.assertNotIn("no test class matches", text)

    def test_a_batch_reports_one_that_only_it_can_see(self) -> None:
        """The belt to discovery's brace: a module can import in the parent and
        not in the worker, and the batch must still run the classes that loaded.
        `ran` stays honest -- the placeholder is not a test that ran."""
        with fixture(PASSES) as names, tempfile.TemporaryDirectory() as out_dir:
            out = Path(out_dir, "report.json")
            with redirect_stderr(StringIO()):
                code = run_tests.run_batch([*names, "zz_no_such_fixture"], out)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(list(report["unloadable"]), ["zz_no_such_fixture"])
        self.assertEqual(len(report["ran"]), 1)
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["errors"], [])


class TestOrdinaryOutcomes(unittest.TestCase):
    def test_a_passing_class_is_green(self) -> None:
        with fixture(PASSES):
            code, text = run_main("--jobs", "2")
        self.assertEqual(code, 0)
        self.assertIn("OK", text)

    def test_a_failing_test_makes_the_run_red(self) -> None:
        with fixture(FAILS):
            code, text = run_main("--jobs", "2")
        self.assertEqual(code, 1)
        self.assertIn("1 failures", text)

    def test_only_ids_travel_back_from_a_batch_not_tracebacks(self) -> None:
        """The batch prints the traceback; the parent only lists the id.

        Sending the traceback back as well printed every failure twice in the
        CI log -- once from the batch's stderr, once from the summary.
        """
        with fixture(FAILS) as names, tempfile.TemporaryDirectory() as out_dir:
            out = Path(out_dir, "report.json")
            with redirect_stderr(StringIO()):
                run_tests.run_batch(names, out)
            report = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(len(report["failures"]), 1)
        self.assertNotIn("Traceback", report["failures"][0])
        self.assertNotIn("\n", report["failures"][0])

    def test_a_filter_matching_nothing_is_an_error_not_an_empty_pass(self) -> None:
        """`--only Typo` running zero tests must not be reported as success."""
        code, _ = run_main("--only", "tests.no_class_has_this_name", "--jobs", "2")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
