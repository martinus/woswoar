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
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def watching(self, pid: int, match: str = ".", stale: float = watch.STALE) -> watch.Watch:
        return watch.Watch(pid, self.log, self.done, re.compile(match), stale)

    def stuck_for(self, seconds: float, stale: float = 60.0) -> watch.Watch:
        """A live watch whose count has not moved for ``seconds``.

        Back-dated rather than waited out, as the elapsed-time tests here are:
        the arithmetic is the same and the suite does not spend five minutes
        proving a five-minute threshold. The first `step` is taken first, so the
        clock this rewinds is the one a real run would be looking at -- rewinding
        a freshly built `Watch` would test a state no job reaches, since the
        opening poll always counts as movement.
        """
        watching = self.watching(os.getpid(), stale=stale)
        watching.step()
        watching.moved -= seconds
        return watching

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

    def running(self) -> subprocess.Popen[bytes]:
        """A process that will still be there in a second unless something kills it.

        SIGHUP is reset to its default explicitly, and that line is the whole
        test below. A disposition of SIG_IGN is inherited across fork *and*
        exec, so a suite launched under `nohup` -- which is how a detached
        mutation run gets started, and how this one was -- hands every child an
        ignored SIGHUP. Without the reset the sweep reported "watching cannot
        kill the job" as unproven on the machine that most needed to know it,
        and would have proven it on CI by luck.
        """
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal, sys, time\n"
                "signal.signal(signal.SIGHUP, signal.SIG_DFL)\n"
                "sys.stdout.write('x')\n"
                "sys.stdout.flush()\n"
                "time.sleep(30)\n",
            ],
            stdout=subprocess.PIPE,
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        assert child.stdout is not None
        # Blocks until the reset above has actually run. Without this the caller
        # signals a process that is still importing, the default is not restored
        # yet, and the inherited SIG_IGN swallows it -- a fixture that proves the
        # opposite of what it says while staying green. The sweep found it: the
        # reset alone left the SIGHUP mutant alive.
        self.assertEqual(child.stdout.read(1), b"x")
        return child

    def command(self, *extra: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "tools.watch",
            *extra,
            "--log",
            str(self.log),
            "--done",
            str(self.done),
        ]

    #: The tree, for a subprocess that must import `tools.watch` by module path.
    @property
    def repo(self) -> Path:
        return Path(watch.__file__).resolve().parent.parent


class TestWhetherAProcessIsThere(Fixture):
    def test_this_process_is_alive(self) -> None:
        self.assertTrue(watch.alive(os.getpid()))

    def test_a_reaped_process_is_not(self) -> None:
        self.assertFalse(watch.alive(self.reaped()))

    def test_asking_does_not_disturb_the_job(self) -> None:
        """Signal 0 is the *only* number that asks without doing.

        1 is SIGHUP and its default action is to terminate, so a watcher that
        reached for it would kill the sweep it was hired to report on -- and
        would then correctly report it dead, which is the worst available
        outcome. Driven against a real child because the property is the
        kernel's, not the code's: `wait` timing out is the child surviving.
        """
        child = self.running()
        self.assertTrue(watch.alive(child.pid))
        with self.assertRaises(subprocess.TimeoutExpired):
            child.wait(timeout=0.5)

    def test_one_is_a_pid_and_not_a_group(self) -> None:
        """The guard's boundary, pinned where the kernel will answer anywhere.

        The `PermissionError` test below is skipped as root, which left the
        guard's `<= 0` free to become `<= 1` -- refusing init -- with the whole
        suite still green. This asks only that 1 gets through, which is true on
        every machine.
        """
        self.assertEqual(watch.a_pid("1"), 1)
        watch.alive(1)  # must not raise; what it answers is the next test's business

    def test_a_process_that_is_not_ours_is_alive(self) -> None:
        """The `PermissionError` branch, forced.

        Mocked, unlike everything else here, because as root there is no process
        this one may not signal -- the test below skips for exactly that reason
        and takes the branch's only coverage with it. What this pins is the
        mapping from that error to an answer, which is where the bug would be;
        that the kernel raises it at all is the other test's job.
        """
        with mock.patch("os.kill", side_effect=PermissionError):
            self.assertTrue(watch.alive(4242))

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

    def test_it_says_how_long_the_job_had_been_running(self) -> None:
        """Back-dated rather than waited out: an hour of test suite to pin a
        number is not a trade anyone would take, and the arithmetic is the same.

        Without it every assertion here reads "0m", which is what `// 60`
        becoming `* 60` also produces at these timescales -- so the minutes
        figure in the DIED line, the one thing telling a reader how much work
        was lost, was unguarded.
        """
        watching = self.watching(self.reaped())
        watching.began -= 3600.0
        line, _ = watching.step() or ("", 0)
        self.assertIn("after 60m", line)

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

    def test_a_log_that_already_has_rows_still_gets_an_opening_line(self) -> None:
        """The sentinel's *sign*, which its magnitude was standing in for.

        `last` starts negative so the opening poll is always a change. Any
        non-negative start silently swallows the first line for a log that
        already holds exactly that many rows -- and attaching a watcher to a job
        already under way is the ordinary case, not a corner.

        Found by #274's `sign` operator: `-1` becoming `1` survived every test
        in this file, because none of them started the watcher against a log
        with anything in it.
        """
        self.log.write_text("row\n", encoding="utf-8")
        line, status = self.watching(os.getpid()).step() or ("", 0)
        self.assertEqual((line, status), ("working: 1 rows after 0m", -1))

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

    def test_a_log_holding_bytes_that_are_not_utf8_is_still_counted(self) -> None:
        """`errors="replace"` rather than strict decoding, and why it matters.

        The log belongs to the job, not to the watcher, and a job killed
        mid-write leaves a partial multi-byte sequence at the end of it. Strict
        decoding raises `UnicodeDecodeError` there -- a `ValueError`, so the
        `except OSError` above does *not* catch it, and it propagates out of
        `main` as a traceback. The watcher would die of the thing it was hired
        to report on, at exactly the moment it was about to report it.

        From #272: mutmut mutates string literals and found this unguarded. We
        do not generate that mutant and #274 argues we should not, so this test
        guards a property no sweep of ours will check -- which is the reason to
        say all of that here rather than leave it to the next reader.
        """
        self.log.write_bytes(b"caught one\ncaught \xff\xfe two\n")
        self.assertEqual(watch.counted(self.log, re.compile("^caught")), 2)

    def test_a_log_that_does_not_exist_yet_is_zero_rather_than_an_error(self) -> None:
        """A job that has not opened its log is at zero rows. Treating absence as
        a failure would put a line on the stream nobody can act on."""
        self.assertEqual(watch.counted(self.root / "not-yet", ANY), 0)


class TestAJobCanStopWithoutEnding(Fixture):
    """The third ending, and the one the first two versions of this tool missed.

    `DIED` and `FINISHED` were events; being alive and wedged was silence -- the
    same silence a job that is merely slow produces. So the reader was back to
    guessing from the other side, which is the failure this whole tool is about.
    It happened here: a mutation sweep went quiet at 58 of 68 rows and was called
    dead, and it was still running.
    """

    def test_a_long_enough_silence_is_reported(self) -> None:
        line, status = self.stuck_for(120.0).step() or ("", 0)
        self.assertIn("STALLED", line)
        self.assertEqual(status, -1)

    def test_it_says_the_process_is_still_there(self) -> None:
        """The half that separates this from `DIED`, and the half no amount of
        waiting gives the reader. "Nothing is happening" and "nothing is
        happening and the job is gone" call for different actions."""
        line, _ = self.stuck_for(120.0).step() or ("", 0)
        self.assertIn("alive and not working", line)

    def test_it_says_how_long_and_how_far(self) -> None:
        line, _ = self.stuck_for(120.0).step() or ("", 0)
        self.assertIn("120s", line)
        self.assertIn("0 rows", line)

    def test_a_short_silence_is_not(self) -> None:
        """The threshold is the whole value of the line. One that fires on an
        ordinary gap trains the reader to skip it, and the next real one goes
        with it."""
        self.assertIsNone(self.stuck_for(30.0).step())

    def test_it_does_not_repeat_itself_every_poll(self) -> None:
        """A stall line every twenty seconds is the per-poll noise this tool was
        built to avoid, arriving through the feature meant to fix it."""
        watching = self.stuck_for(120.0)
        self.assertIsNotNone(watching.step())
        self.assertIsNone(watching.step())

    def test_it_speaks_again_when_the_stall_doubles(self) -> None:
        """One line at minute two is undatable by a reader arriving at minute
        forty. A stall twice as long as the last report is new information."""
        watching = self.stuck_for(120.0)
        watching.step()
        watching.moved -= 130.0
        line, _ = watching.step() or ("", 0)
        self.assertIn("250s", line)

    def test_progress_starts_the_clock_again(self) -> None:
        """Otherwise a job that recovers stays branded stalled for as long as it
        runs, which is a lie that gets louder the longer it works."""
        watching = self.stuck_for(120.0)
        watching.step()
        self.log.write_text("row\n", encoding="utf-8")
        line, _ = watching.step() or ("", 0)
        self.assertIn("working", line)
        self.assertIsNone(watching.step())

    def test_a_recovered_job_stalls_again_on_its_own_terms(self) -> None:
        """Progress resets what was *said*, not only when work last moved.

        The doubling is measured from the last report, so a job that stalled for
        ten minutes and then recovered would need twenty minutes of silence
        before anyone heard about its next stall -- the interval getting longer
        exactly because the job had already been in trouble once.

        The sibling above pins the clock; this pins the counter. Both are reset
        in the same line, and the sweep found that only one of them was tested.
        """
        watching = self.stuck_for(600.0)
        watching.step()
        self.log.write_text("row\n", encoding="utf-8")
        watching.step()
        watching.moved -= 120.0
        line, _ = watching.step() or ("", 0)
        self.assertIn("STALLED", line)

    def test_zero_turns_it_off(self) -> None:
        """For a job whose work genuinely arrives in one lump at the end, where
        every poll before it is a true stall and none of them is news."""
        self.assertIsNone(self.stuck_for(9999.0, stale=0.0).step())

    def test_a_finish_outranks_a_stall(self) -> None:
        """`done` is checked first, and a stalled job that has since written its
        report is a finished one. Reporting the stall would send the reader to
        look at a run that is over."""
        watching = self.stuck_for(9999.0)
        self.done.write_text("{}", encoding="utf-8")
        line, status = watching.step() or ("", -1)
        self.assertIn("FINISHED", line)
        self.assertEqual(status, 0)

    def test_a_death_outranks_a_stall(self) -> None:
        """Both are true of a dead job -- it has certainly stopped progressing --
        and only one of them tells the reader to stop waiting."""
        watching = self.watching(self.reaped(), stale=60.0)
        watching.moved -= 9999.0
        line, status = watching.step() or ("", 0)
        self.assertIn("DIED", line)
        self.assertEqual(status, 1)


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
    def ran(self, *extra: str) -> subprocess.CompletedProcess[str]:
        """Every invocation here is one that must *return*, so a hang is a
        failure and not something to sit through.

        The bound is not politeness. `main` is a `while True`, and the mutations
        that stop it ever leaving -- dropping the `if` that returns on an event,
        say -- turn a test without one into a test that never finishes. A
        mutation run of this file spent ten minutes on its last ten rows for
        exactly that reason, with the tool's own per-mutant timeout the only
        thing left to catch them.
        """
        return subprocess.run(
            self.command(*extra), cwd=self.repo, capture_output=True, text=True, timeout=30
        )

    def test_it_exits_zero_when_the_job_finished(self) -> None:
        self.done.write_text("{}", encoding="utf-8")
        ran = self.ran(str(self.reaped()))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("FINISHED", ran.stdout)

    def test_it_exits_one_when_the_job_died(self) -> None:
        """The exit status matters as much as the line: a caller that only reads
        the status still learns the difference."""
        ran = self.ran(str(self.reaped()))
        self.assertEqual(ran.returncode, 1)
        self.assertIn("DIED", ran.stdout)

    def test_it_repeats_nothing_while_nothing_changes(self) -> None:
        """Twenty polls, one line -- the promise the whole tool rests on.

        Only reachable end to end: `step` returning None is the ordinary quiet
        poll, and every other test here stops at the first event, so the code
        that has to *survive* a None had no coverage at all. Run at 50ms so a
        second of wall clock is twenty of them, and terminated rather than
        waited for, because a watcher of something still running never exits.

        A traceback would also be one line per poll, so both halves are checked:
        the count, and that the stream is clean.
        """
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
        watcher = subprocess.Popen(
            self.command(str(self.running().pid), "--interval", "0.05"),
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(watcher.kill)
        time.sleep(1.0)
        # Before the terminate, because "it exited on its own" and "it was still
        # watching" are the two answers here and terminating erases the
        # difference. Returning on a `working` line is a watcher that stops at
        # the first sign of life, which is worse than not running it.
        self.assertIsNone(watcher.poll(), "the watcher returned instead of carrying on")
        watcher.terminate()
        out = watcher.communicate(timeout=10)[0]
        self.assertNotIn("Traceback", out)
        self.assertEqual(out.count("working:"), 1, out)
        # The sleep, measured rather than read. Dropping it changes no output at
        # all -- same lines, same order -- and turns the tool into a spin on a
        # machine that is already busy with the job it is watching, which is the
        # one promise its docstring makes that nothing else here checks.
        #
        # `RUSAGE_CHILDREN` counts children that have been waited for, and
        # `communicate` waited for this one; the sleeping process from
        # `running()` is not reaped until cleanup. A loaded machine can only
        # give the watcher *less* CPU than this, so the threshold has no false
        # failure in it.
        burned = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
        self.assertLess(burned, 0.5, f"a second of watching cost {burned:.2f}s of CPU")

    def test_the_stall_threshold_reaches_the_watch(self) -> None:
        """End to end, because a flag wired to nothing is invisible to every
        test that builds a `Watch` itself -- which is how both survivors of this
        tool's first sweep got in.

        Also the only place the doubling is driven by a real clock. A tenth of a
        second against twenty polls: on a per-poll report that is nineteen stall
        lines, on a doubling one it is a handful, and on a flag that never
        arrived it is none. The three are far enough apart that a loaded machine
        cannot turn one into another.
        """
        watcher = subprocess.Popen(
            self.command(str(self.running().pid), "--interval", "0.05", "--stale", "0.1"),
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(watcher.kill)
        time.sleep(1.0)
        watcher.terminate()
        out = watcher.communicate(timeout=10)[0]
        self.assertNotIn("Traceback", out)
        said = out.count("STALLED")
        self.assertGreaterEqual(said, 1, out)
        self.assertLess(said, 10, out)

    def test_it_reads_the_pid_the_job_wrote(self) -> None:
        """The step that feeds this tool, which never worked by hand.

        `... & echo $! > sweep.pid` looks equivalent to the job writing it and
        is not -- in one session it produced no file, and the pid was recovered
        from the process table fifteen runs running, by a *pattern*, which is
        exactly the `pgrep -f` hole this tool exists to close.
        """
        self.done.write_text("{}", encoding="utf-8")
        pidfile = self.root / "sweep.pid"
        pidfile.write_text(f"{self.reaped()}\n", encoding="utf-8")
        ran = self.ran("--pidfile", str(pidfile))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("FINISHED", ran.stdout)

    def test_it_waits_for_a_pidfile_that_is_not_there_yet(self) -> None:
        """The watcher is started in the same breath as the job and can win the
        race. Waiting is the difference between that being fine and being a
        usage error the caller has to sleep around."""
        self.done.write_text("{}", encoding="utf-8")
        pidfile = self.root / "late.pid"
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import time,pathlib\ntime.sleep(0.4)\n"
                f"pathlib.Path({str(pidfile)!r}).write_text('{os.getpid()}')\n",
            ]
        )
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        ran = self.ran("--pidfile", str(pidfile))
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_a_pidfile_that_never_arrives_is_an_error_not_a_wait(self) -> None:
        """A watcher that settled into watching nothing would be reporting the
        silence it was built to break. Bounded, and it says which file."""
        ran = subprocess.run(
            self.command("--pidfile", str(self.root / "never"), "--interval", "0.05"),
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(self.repo)},
        )
        self.assertEqual(ran.returncode, 1)
        self.assertIn("no usable pid", ran.stderr)

    def test_a_pid_and_a_pidfile_together_are_refused(self) -> None:
        """Two answers to one question, and no way to tell which the caller
        meant. argparse says so before anything runs."""
        ran = self.ran("123", "--pidfile", str(self.root / "p"))
        self.assertEqual(ran.returncode, 2)

    def test_neither_a_pid_nor_a_pidfile_is_refused(self) -> None:
        ran = self.ran()
        self.assertEqual(ran.returncode, 2)

    def test_a_pid_that_is_a_group_is_a_usage_error(self) -> None:
        ran = self.ran("0")
        self.assertEqual(ran.returncode, 2)
        self.assertIn("process group", ran.stderr)


if __name__ == "__main__":
    unittest.main()
