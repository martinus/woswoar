"""Tests for the survivor partition.

Pure -- JSON in, buckets out -- so this runs in milliseconds with no sandbox
and no suite, the same line `tools/mutants.py` is split along.

The case worth reading is `TestCaughtMutantsRepairTheMap`. Coverage measured
in-process cannot see a line executed by a subprocess, and this repository
tests its shell hook by running a real `bash`; on the run this module was
written for, 111 mutants were *caught* on lines coverage called unexecuted.
Without folding that back in, those lines read as "no test reaches this" and a
third of the missing-test list is noise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools import reached

REPO_ROOT = Path(__file__).resolve().parent.parent


def results(*rows: tuple[str, int, str]) -> dict[str, Any]:
    """A `--json` report holding `(path, line, outcome)` rows."""
    return {
        "baseline_red": False,
        "results": [
            {
                "label": f"{path}:{line} in f() -- something",
                "path": path,
                "line": line,
                "tests": "t",
                "operator": "boundary",
                "outcome": outcome,
            }
            for path, line, outcome in rows
        ],
    }


def coverage(**files: list[int]) -> dict[str, Any]:
    """A `coverage json` report. `a__b=[3]` means `a/b.py` ran line 3.

    The `.py` matters and this helper forgot it at first: the paths then matched
    nothing, every survivor read as unreached, and two of the assertions below
    failed for a reason that had nothing to do with the code. A fixture whose
    keys silently miss is the failure this module exists to find.
    """
    return {
        "files": {
            path.replace("__", "/") + ".py": {"executed_lines": lines}
            for path, lines in files.items()
        }
    }


class TestPartition(unittest.TestCase):
    def test_a_survivor_on_an_executed_line_is_a_weak_fixture(self) -> None:
        rows = reached.rows_from(results(("a/b.py", 10, "survived")))
        split = reached.partition(rows, {"a/b.py": {10}})
        self.assertEqual((len(split.unreached), len(split.weak)), (0, 1))

    def test_a_survivor_on_an_unexecuted_line_is_a_missing_test(self) -> None:
        rows = reached.rows_from(results(("a/b.py", 10, "survived")))
        split = reached.partition(rows, {"a/b.py": {99}})
        self.assertEqual((len(split.unreached), len(split.weak)), (1, 0))

    def test_caught_rows_are_not_survivors(self) -> None:
        """The partition is of survivors only; a caught row is already an answer."""
        rows = reached.rows_from(results(("a/b.py", 10, "caught"), ("a/b.py", 11, "survived")))
        split = reached.partition(rows, {})
        self.assertEqual(split.total, 1)

    def test_a_file_absent_from_the_map_is_not_executed(self) -> None:
        rows = reached.rows_from(results(("never/seen.py", 3, "survived")))
        self.assertEqual(len(reached.partition(rows, {"other.py": {3}}).unreached), 1)


class TestCaughtMutantsRepairTheMap(unittest.TestCase):
    """A caught mutation is proof its line ran, whatever coverage says."""

    def test_a_caught_row_adds_its_line(self) -> None:
        rows = reached.rows_from(results(("a/b.py", 10, "caught")))
        self.assertEqual(reached.repair({}, rows), {"a/b.py": {10}})

    def test_a_survivor_does_not_add_its_line(self) -> None:
        """The whole question. A survivor on an unexecuted line is the finding;
        letting it mark its own line executed would erase every one of them."""
        rows = reached.rows_from(results(("a/b.py", 10, "survived")))
        self.assertEqual(reached.repair({}, rows), {})

    def test_broke_and_timeout_prove_nothing(self) -> None:
        """They never got to ask, so they are not evidence the line ran."""
        rows = reached.rows_from(results(("a/b.py", 10, "broke"), ("a/b.py", 11, "timeout")))
        self.assertEqual(reached.repair({}, rows), {})

    def test_repair_changes_the_answer(self) -> None:
        """End to end: the same survivor lands in a different bucket once a
        sibling row proves the line is reachable by a subprocess-driven test."""
        rows = reached.rows_from(results(("a/b.py", 10, "survived"), ("a/b.py", 10, "caught")))
        naive = reached.partition(rows, {})
        fixed = reached.partition(rows, reached.repair({}, rows))
        self.assertEqual(len(naive.unreached), 1, "without the repair it reads as unreached")
        self.assertEqual(len(fixed.weak), 1, "with it, the line is known to run")

    def test_it_does_not_discard_what_coverage_already_knew(self) -> None:
        rows = reached.rows_from(results(("a/b.py", 10, "caught")))
        self.assertEqual(reached.repair({"a/b.py": {7}}, rows), {"a/b.py": {7, 10}})


class TestReadingTheInputs(unittest.TestCase):
    def test_a_row_without_a_position_is_skipped(self) -> None:
        """A hand-written spec row has no line, and cannot be classified."""
        report = results(("a/b.py", 10, "survived"))
        report["results"].append(
            {
                "label": "by hand",
                "path": "a/b.py",
                "line": None,
                "tests": "t",
                "operator": "",
                "outcome": "survived",
            }
        )
        self.assertEqual(len(reached.rows_from(report)), 1)

    def test_coverage_paths_are_normalised(self) -> None:
        got = reached.executed_lines({"files": {"a\\b.py": {"executed_lines": [1, 2]}}})
        self.assertEqual(got, {"a/b.py": {1, 2}})

    def test_a_file_with_no_executed_lines_is_empty_not_missing(self) -> None:
        self.assertEqual(reached.executed_lines({"files": {"a.py": {}}}), {"a.py": set()})


class TestTheCommandLine(unittest.TestCase):
    """Driven as a subprocess, because the exit status and the printed
    partition are what a person actually uses."""

    def run_it(
        self, report: dict[str, Any], cov: dict[str, Any], *extra: str
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as box:
            r, c = Path(box) / "r.json", Path(box) / "c.json"
            r.write_text(json.dumps(report), encoding="utf-8")
            c.write_text(json.dumps(cov), encoding="utf-8")
            return subprocess.run(
                [sys.executable, "-m", "tools.reached", str(r), str(c), *extra],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )

    def counts(self, out: str) -> dict[str, int]:
        """The numbers, not the labels.

        Asserting that the words "missing test" appear is what the first version
        of this test did, and every count mutation survived it -- the partition
        could report 0 and 0 and still pass. The numbers are the output.
        """
        found = {}
        for line in out.splitlines():
            hit = re.match(
                r"(ALL|caught|broke.*?|timeout.*?|SURVIVED|on a line.*?)\s{2,}(\d+)$", line.strip()
            )
            if hit:
                found[hit.group(1).strip()] = int(hit.group(2))
        return found

    def test_it_counts_both_buckets(self) -> None:
        done = self.run_it(
            results(
                ("a/b.py", 10, "survived"), ("a/b.py", 20, "survived"), ("a/b.py", 30, "caught")
            ),
            coverage(a__b=[10]),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        got = self.counts(done.stdout)
        self.assertEqual(got["ALL"], 3, done.stdout)
        self.assertEqual(got["caught"], 1, done.stdout)
        self.assertEqual(got["SURVIVED"], 2, done.stdout)
        self.assertEqual(got["on a line NO test executes  (missing test)"], 1, done.stdout)
        self.assertEqual(got["on a line tests DO execute  (weak/equiv)"], 1, done.stdout)

    def test_rows_that_asked_nothing_are_counted_apart(self) -> None:
        """`broke` and `timeout` are neither caught nor survived, and folding
        them into either would misstate the partition they sit outside."""
        done = self.run_it(
            results(("a/b.py", 10, "broke"), ("a/b.py", 11, "timeout"), ("a/b.py", 12, "survived")),
            coverage(a__b=[]),
        )
        got = self.counts(done.stdout)
        self.assertEqual(got["ALL"], 3, done.stdout)
        self.assertEqual(got["SURVIVED"], 1, done.stdout)
        self.assertEqual(got.get("broke (asked nothing)"), 1, done.stdout)
        self.assertEqual(got.get("timeout (asked nothing)"), 1, done.stdout)

    def test_the_repair_is_reported_when_it_changes_something(self) -> None:
        """Silent repair would make the partition unauditable: a reader could
        not tell a clean map from one this had to correct 111 times."""
        loud = self.run_it(results(("a/b.py", 10, "caught")), coverage(a__b=[]))
        self.assertIn("1 line(s) had a caught mutation", loud.stdout)
        quiet = self.run_it(results(("a/b.py", 10, "caught")), coverage(a__b=[10]))
        self.assertNotIn("had a caught mutation", quiet.stdout)

    def test_the_unreached_tally_names_each_file(self) -> None:
        done = self.run_it(
            results(
                ("a/one.py", 1, "survived"),
                ("a/one.py", 2, "survived"),
                ("b/two.py", 1, "survived"),
            ),
            coverage(),
        )
        self.assertRegex(done.stdout, r"2\s+a/one\.py")
        self.assertRegex(done.stdout, r"1\s+b/two\.py")

    def test_a_run_where_nothing_was_answered_says_so(self) -> None:
        """All broke: there is no partition, and printing 0 and 0 would read as
        a clean bill of health."""
        done = self.run_it(results(("a/b.py", 10, "broke")), coverage(a__b=[]))
        self.assertIn("nothing was answered", done.stdout)

    def test_the_unreached_list_is_optional_and_ordered(self) -> None:
        """Ordered by file then line, so two runs of the same state produce the
        same text and a diff between them is a change rather than a shuffle.
        The fixture lists them backwards for that reason."""
        report = results(
            ("b/z.py", 9, "survived"), ("a/b.py", 20, "survived"), ("a/b.py", 3, "survived")
        )
        quiet = self.run_it(report, coverage())
        loud = self.run_it(report, coverage(), "--list")
        self.assertNotIn("a/b.py:20", quiet.stdout)
        self.assertIn("unreached survivors by file:", quiet.stdout)

        listed = [
            line.strip().split(" ")[0]
            for line in loud.stdout.splitlines()
            if line.strip().startswith(("a/", "b/")) and ":" in line
        ]
        self.assertEqual(listed, ["a/b.py:3", "a/b.py:20", "b/z.py:9"], loud.stdout)
        self.assertIn("unreached survivors:", loud.stdout)

    def test_a_report_with_nothing_to_classify_is_refused(self) -> None:
        """Silence would read as "no gaps" -- the failure mode this whole
        module exists to prevent one level up."""
        done = self.run_it({"results": []}, coverage(a__b=[1]))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no positioned rows", done.stderr + done.stdout)

    def test_unreadable_inputs_say_so(self) -> None:
        with tempfile.TemporaryDirectory() as box:
            done = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tools.reached",
                    str(Path(box) / "absent.json"),
                    str(Path(box) / "gone.json"),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("cannot read the inputs", done.stderr + done.stdout)


if __name__ == "__main__":
    unittest.main()
