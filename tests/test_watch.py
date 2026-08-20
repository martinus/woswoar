"""`tools/watch.py`: the three answers, and the two ways they were got wrong.

The tool exists because a mutation sweep was reported alive twice after it had
died, so these tests are mostly about the *negative* cases -- a job that is gone
must be called gone, and a job that finished must not be called gone.

Driven against real processes and real files. The subject is the kernel's answer
about a pid and the presence of a file on disk, and a fixture that mocked either
would be asserting the belief that was wrong in the first place.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import watch

ANY = re.compile(".")


class Fixture(unittest.TestCase):
    """A scratch log and report path, and a way to make a pid that is really gone."""

    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        self.root = Path(box.name)
        self.log = self.root / "run.log"
        self.done = self.root / "report.json"

    def watching(self, pid: int, match: str = ".") -> watch.Watch:
        return watch.Watch(pid, self.log, self.done, re.compile(match))

    def reaped(self) -> int:
        """A pid that has certainly exited *and been waited for*.

        Waited for, which is the part that matters: an unreaped child is a
        zombie, and signal 0 succeeds for one, so a fixture that only killed
        would be asserting the opposite of what it meant. `Popen.wait` reaps, so
        what this returns is a pid the kernel no longer knows.

        `CompletedProcess` has no `pid`, which is why this is `Popen` and not
        the shorter `subprocess.run` -- the first version of this file used the
        latter and every test here errored.
        """
        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait()
        return child.pid


class TestWhetherAProcessIsThere(Fixture):
    def test_this_process_is_alive(self) -> None:
        self.assertTrue(watch.alive(os.getpid()))

    def test_a_reaped_process_is_not(self) -> None:
        self.assertFalse(watch.alive(self.reaped()))

    def test_pid_one_is_alive_though_it_is_not_ours(self) -> None:
        """`PermissionError` means the process exists and belongs to somebody
        else, which is alive for this purpose. Reporting it dead would be the
        same false negative the pattern match was a false positive.

        Skipped where this process *can* signal pid 1 -- running as root, or in
        a container where the suite is pid 1's own tree -- because then the call
        answers through the ordinary path and pins nothing about the branch.
        """
        try:
            os.kill(1, 0)
        except PermissionError:
            pass
        except (ProcessLookupError, OSError):
            self.skipTest("no pid 1 to ask about")
        else:
            self.skipTest("this process may signal pid 1, so the branch is not reached")
        self.assertTrue(watch.alive(1))


class TestAPidThatIsNotOne(Fixture):
    """The false positive that survives the move off `pgrep`.

    `os.kill(0, 0)` signals the caller's own process group and always succeeds,
    so a watcher handed a 0 -- from a launcher whose pid variable never got
    set -- would report alive for ever while its job was gone. The point of the
    tool is that liveness cannot be answered by accident; a group is an accident.
    """

    def test_pid_zero_is_refused_rather_than_answered(self) -> None:
        with self.assertRaises(ValueError):
            watch.alive(0)

    def test_a_negative_pid_is_refused(self) -> None:
        """Negatives are the explicit spelling of the same thing: `kill(-n, 0)`
        addresses process group n."""
        with self.assertRaises(ValueError):
            watch.alive(-1)

    def test_the_message_says_what_is_wrong_with_it(self) -> None:
        """A `ValueError` reading "0" tells the reader nothing they did not
        type."""
        with self.assertRaises(ValueError) as raised:
            watch.alive(0)
        self.assertIn("process group", str(raised.exception))

    def test_the_command_line_refuses_it_as_a_usage_error(self) -> None:
        """Through argparse rather than as a traceback: the whole failure this
        tool addresses is a reader who has stopped watching closely, and a
        stack trace twenty seconds in is not where they are looking."""
        ran = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.watch",
                "0",
                "--log",
                str(self.log),
                "--done",
                str(self.done),
            ],
            cwd=Path(watch.__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(ran.returncode, 2)
        self.assertIn("process group", ran.stderr)

    def test_an_ordinary_pid_still_gets_through(self) -> None:
        """The guard above passes on a converter that rejects everything."""
        self.assertEqual(watch.a_pid(str(os.getpid())), os.getpid())


class TestTheJobIsGone(Fixture):
    """The failure the tool was written for: a death that looked like patience."""

    def test_a_dead_pid_is_reported_dead(self) -> None:
        line, status = self.watching(self.reaped()).step() or ("", -1)
        self.assertIn("DIED", line)
        self.assertEqual(status, 1)

    def test_it_says_gone_rather_than_slow(self) -> None:
        """The wording is the point. "No report yet" reads as something to wait
        out, which is exactly what the reader did for an hour."""
        line, _ = self.watching(self.reaped()).step() or ("", -1)
        self.assertIn("gone, not slow", line)

    def test_it_says_how_far_the_job_got(self) -> None:
        self.log.write_text("row\nrow\nrow\n", encoding="utf-8")
        line, _ = self.watching(self.reaped()).step() or ("", -1)
        self.assertIn("3 rows", line)


class TestAFinishedJobIsNotADeadOne(Fixture):
    """`done` is checked before `alive`, and this is the test for that order.

    A job's last two acts are to write its report and to exit, so there is a
    window where it is finished *and* gone. Asking about the process first
    reports a successful run as a death -- the same false alarm as the original
    bug, arriving from the other side.
    """

    def test_a_report_from_a_process_that_has_exited_is_a_finish(self) -> None:
        self.done.write_text("{}", encoding="utf-8")
        line, status = self.watching(self.reaped()).step() or ("", -1)
        self.assertIn("FINISHED", line)
        self.assertEqual(status, 0)

    def test_without_the_report_the_same_pid_is_a_death(self) -> None:
        """The fixture that keeps the test above from passing on a `step` that
        can no longer report a death at all."""
        line, status = self.watching(self.reaped()).step() or ("", -1)
        self.assertIn("DIED", line)
        self.assertEqual(status, 1)


class TestWhatCountsAsProgress(Fixture):
    def test_the_first_look_is_an_event_even_at_zero(self) -> None:
        """`last` starts at -1 so that a job with nothing done yet still says so.
        At 0 the opening silence would be indistinguishable from a job that never
        starts, which is the shape this tool exists to remove."""
        line, status = self.watching(os.getpid()).step() or ("", 0)
        self.assertEqual((line, status), ("working: 0 rows after 0m", -1))

    def test_an_unchanged_count_is_not_an_event(self) -> None:
        """One line per twenty seconds for forty minutes is the same as no
        signal, by a different route."""
        watching = self.watching(os.getpid())
        watching.step()
        self.assertIsNone(watching.step())

    def test_a_changed_count_is(self) -> None:
        watching = self.watching(os.getpid())
        watching.step()
        self.log.write_text("row\n", encoding="utf-8")
        line, _ = watching.step() or ("", 0)
        self.assertIn("1 rows", line)

    def test_only_matching_lines_are_counted(self) -> None:
        self.log.write_text("caught one\nnoise\ncaught two\n", encoding="utf-8")
        line, _ = self.watching(os.getpid(), match="^caught").step() or ("", 0)
        self.assertIn("2 rows", line)

    def test_a_log_that_does_not_exist_yet_is_zero_rather_than_an_error(self) -> None:
        """A job that has not opened its log is at zero rows. Treating absence as
        a failure would put a line on the stream nobody can act on."""
        self.assertEqual(watch.counted(self.root / "not-yet", ANY), 0)


class TestItForksNothing(Fixture):
    """The property that keeps the original bug from coming back.

    `pgrep`, `ps` and every other name-matching answer to "is it alive" shares
    the hole that started this: they match on a command line, and the checker's
    own command line is on it. `os.kill(pid, 0)` asks the kernel about one pid
    and cannot be confused by what anything is called.

    Asserted structurally, because the failure is a *future* edit reaching for a
    subprocess again, and no behavioural test would notice until it had already
    reported something dead as alive. The suite does the same for the shell
    hook, which must not fork on the record path.
    """

    def parsed(self) -> ast.Module:
        """The module as code, not as text.

        Through `ast` rather than a grep, for the reason `test_docs` gives for
        the same choice: the source is full of strings that are *prose*, and
        this module's docstring names `pgrep`, `ps` and `/proc` at length
        precisely because it is explaining why it does not use them. A grep
        cannot tell the warning from the deed, and the first version of this
        test failed on its own explanation.
        """
        return ast.parse(Path(watch.__file__).read_text(encoding="utf-8"))

    def test_it_imports_nothing_that_starts_a_process(self) -> None:
        imported = {
            name.name.split(".")[0]
            for node in ast.walk(self.parsed())
            if isinstance(node, ast.Import)
            for name in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(self.parsed())
            if isinstance(node, ast.ImportFrom) and node.module
        }
        for forbidden in ("subprocess", "multiprocessing", "pty"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imported)

    def test_it_calls_nothing_that_starts_a_process(self) -> None:
        """`os` is imported for `os.kill`, so the import check above cannot see
        `os.system` or `os.popen`. These are the attribute names that would
        bring a shell back."""
        reached = {node.attr for node in ast.walk(self.parsed()) if isinstance(node, ast.Attribute)}
        for forbidden in ("system", "popen", "spawnv", "fork"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, reached)

    def test_the_guard_can_actually_fail(self) -> None:
        """Both checks above pass on a module that does nothing at all, so this
        pins that they are looking at something: `os.kill` is the one call the
        tool is allowed, and it must be there."""
        reached = {node.attr for node in ast.walk(self.parsed()) if isinstance(node, ast.Attribute)}
        self.assertIn("kill", reached)


class TestTheCommandLine(Fixture):
    def test_it_exits_zero_when_the_job_finished(self) -> None:
        self.done.write_text("{}", encoding="utf-8")
        ran = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.watch",
                str(self.reaped()),
                "--log",
                str(self.log),
                "--done",
                str(self.done),
            ],
            cwd=Path(watch.__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("FINISHED", ran.stdout)

    def test_it_exits_one_when_the_job_died(self) -> None:
        """The exit status matters as much as the line: a caller that only reads
        the status still learns the difference."""
        ran = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.watch",
                str(self.reaped()),
                "--log",
                str(self.log),
                "--done",
                str(self.done),
            ],
            cwd=Path(watch.__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(ran.returncode, 1)
        self.assertIn("DIED", ran.stdout)


if __name__ == "__main__":
    unittest.main()
