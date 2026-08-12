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
                mock.patch.object(run_tests, "discover", lambda: dict(classes)),
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
    def test_every_test_is_assigned_to_exactly_one_shard(self) -> None:
        """The sharding must partition the real suite -- no gaps, no double runs."""
        classes = run_tests.discover()
        ids = [tid for group in classes.values() for tid in group]
        self.assertEqual(len(ids), len(set(ids)), "a test landed in two shards")

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
        self.assertIn("shard died without reporting", text)

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
