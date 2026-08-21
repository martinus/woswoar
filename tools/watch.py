"""Whether a long job is still working, still there, or gone -- said out loud.

The other four tools in here do something. This one only watches, and it exists
because two ways of *not* watching both failed in the same session.

**Liveness came from `pgrep -f <name>`, and that answers about the asker.** A
mutation sweep was checked with ``pgrep -f tools.mutate`` from a shell whose own
command line contained ``tools.mutate`` -- so the check matched itself and
reported a process alive that had been dead for ten minutes. Twice, to somebody
who had asked twice whether it was stuck. Any liveness test that matches on a
command line has this hole; the fix is not a better pattern but a different
question, so this takes a **pid recorded at launch** and asks the kernel.

**Silence was the only other signal.** A job that dies quietly looks exactly
like a job still working, and the reader cannot tell them apart by waiting
longer -- waiting longer is what both of them look like. So a death is an
*event* here, with its own line and its own exit status, and the line says the
job is gone rather than slow. `Monitor`'s own guidance is the same rule from the
other end: a watcher that greps only for the success marker stays silent through
a crash, and silence reads identically to progress.

What that buys is a stream a person can leave running: one line when the count
of finished work changes, one line when it ends, nothing in between. Emitting
per poll instead would be a line every twenty seconds for forty minutes, which
is the same as no signal at all by a different route.

**A job can also stop without ending, and that was the gap the first two
versions left.** Death and completion are events here; *stalling* was not, so a
sweep that was alive and wedged printed one line and then nothing -- which is
exactly what a sweep that is alive and slow prints. The reader is back to
guessing, from the other side of the same silence. Measured on this repository:
a nine-minute mutation run emitted a row every thirty seconds or so, and the run
that hung went quiet for ten minutes and more. So a stall long enough to be
abnormal is now its own line, and it says the job is still alive -- which is the
half that distinguishes it from `DIED`, and the half a reader cannot get by
waiting.

Reported on a doubling interval rather than every poll or once. Every poll is
the noise this tool exists to avoid; once is a line at minute five that a reader
arriving at minute forty cannot date. Doubling gives four lines in forty minutes,
each of which is real news -- a stall twice as long as the last report is worth
saying, one ten per cent longer is not.

**It watches rather than wraps.** The obvious shape is to run the job itself and
report as it goes, and that is wrong here: these jobs are already started
detached, precisely so that a foreground call timing out does not take them
with it. A watcher that insisted on being their parent would reintroduce the
coupling they were detached to avoid.

**It forks nothing and reads no `/proc`.** `os.kill(pid, 0)` is the whole of the
liveness check, so this runs on macOS as well, and a watcher cannot itself
become a source of load on a machine already busy with the thing it is
watching. `tests/test_watch.py` asserts the no-fork property rather than
trusting it, because a future "just shell out to `ps`" would restore exactly the
class of bug the first paragraph is about.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path

#: Seconds between polls. Twenty rather than one: the events this reports are
#: minutes apart, and the point of the tool is to be cheap enough to leave
#: running beside a job that is already using the machine.
INTERVAL = 20.0

#: Seconds without new work before a job is called stalled. Five minutes is a
#: judgement, and it is this one: the longest gap between rows in a healthy
#: mutation sweep here was about a minute, and the wedged one was silent for ten
#: and counting. Anything in between is arbitrary, so the value is generous --
#: a threshold that cries wolf is worse than none, because the next real one is
#: read as noise too.
#:
#: No job's own rhythm is knowable from here, which is why this is a flag. Zero
#: turns it off, for a job whose work genuinely arrives in one lump at the end.
STALE = 300.0


def alive(pid: int) -> bool:
    """Whether ``pid`` still exists, asked of the kernel rather than of a name.

    Signal 0 is the documented "check, do not deliver" spelling. Three answers
    collapse into two on purpose:

    - `ProcessLookupError` is the only one that means gone;
    - `PermissionError` means the process is there and belongs to somebody else,
      which is *alive* for this purpose -- reporting a job dead because it is not
      ours would be the same false negative as the pattern match was a false
      positive;
    - anything else propagates, because a watcher that swallows the unexpected
      is back to being silence.

    Two honest limits. The first is why the jobs this watches are detached: a
    *child* of this process that has exited but not been reaped is a zombie, and
    signal 0 succeeds for a zombie. Nothing here reaps, so a watcher that had
    spawned its subject could report it alive for ever. Watching something
    started elsewhere has no such state -- see the module docstring on why it
    does not wrap.

    The second is unfixable from here: pids are recycled, so a long-dead job
    whose number has been handed to something else reads as alive. Nothing short
    of a start time from `/proc` distinguishes them, and that is Linux-only for
    a window measured in tens of thousands of spawns. Said rather than guarded.
    """
    # 0 and the negatives are refused rather than answered, because `os.kill`
    # reads them as *process groups* -- 0 meaning "every process in my own
    # group", which signal 0 succeeds for unconditionally. A watcher handed one
    # would report alive for ever: the same false positive as the pattern match,
    # arriving through the front door. Raising is right where returning True
    # would be a lie and returning False would be a different one.
    if pid <= 0:
        raise ValueError(f"{pid} names a process group, not a process")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def counted(log: Path, pattern: re.Pattern[str]) -> int:
    """How many lines of ``log`` match, or 0 while it does not exist yet.

    Absent and empty are the same answer deliberately: a job that has not opened
    its log is at zero rows, and distinguishing the two would put a line on the
    stream for something nobody can act on.
    """
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if pattern.search(line))


class Watch:
    """One job being watched. Yields the lines a reader should see, in order.

    A class rather than a loop with prints in it, so the decisions -- what counts
    as an event, and in which order the two endings are checked -- can be tested
    without a clock or a subprocess. `main` is the part that sleeps.
    """

    def __init__(
        self,
        pid: int,
        log: Path,
        done: Path,
        pattern: re.Pattern[str],
        stale: float = STALE,
    ) -> None:
        self.pid = pid
        self.log = log
        self.done = done
        self.pattern = pattern
        self.stale = stale
        self.began = time.monotonic()
        #: -1 rather than 0, so a job that is already at zero rows still gets its
        #: first "working" line. Starting at 0 would make the opening silence
        #: indistinguishable from a job that never starts.
        self.last = -1
        #: When the count last moved. `began` rather than 0, because nothing has
        #: moved yet and "since we started watching" is what that means. In
        #: practice the first `step` overwrites it before anything reads it --
        #: `last` starts at -1, so the opening poll is always a change -- which
        #: is why a job stuck at zero rows stalls a `stale` after the watcher
        #: starts rather than after the job did. The watcher cannot know the
        #: latter; it was not there.
        self.moved = self.began
        #: The stall this last spoke about, which is what makes the next report a
        #: doubling rather than a repeat. Zero means nothing said yet -- and, as
        #: with `moved`, the opening poll overwrites it before `stalling` can
        #: read it, so this value is the declaration rather than the behaviour.
        self.told = 0.0

    def minutes(self) -> int:
        return int((time.monotonic() - self.began) // 60)

    def step(self) -> tuple[str, int] | None:
        """The next line to print and an exit status, or None to keep watching.

        **`done` is checked before `alive`, and the order is the whole
        correctness of this function.** A job's last two acts are to write its
        report and to exit, so there is a window in which it is finished *and*
        gone. Asking about the process first reports a successful run as a death
        -- the exact false alarm this tool exists to prevent, arriving from the
        other direction.
        """
        rows = counted(self.log, self.pattern)
        if self.done.exists():
            return f"FINISHED after {self.minutes()}m: {rows} rows, report written", 0
        if not alive(self.pid):
            return (
                f"DIED after {self.minutes()}m: {rows} rows done, no report written "
                f"-- the job is gone, not slow",
                1,
            )
        if rows != self.last:
            self.last = rows
            self.moved = time.monotonic()
            self.told = 0.0
            return f"working: {rows} rows after {self.minutes()}m", -1
        return self.stalling(rows)

    def stalling(self, rows: int) -> tuple[str, int] | None:
        """The line for a job that is alive and getting nothing done.

        Status -1, like `working`: a stall is not a verdict. The job may still
        finish, and quite often does -- what the reader gains is the chance to
        go and look rather than to keep waiting on a guess.

        In seconds, where the rest of this speaks in minutes, and deliberately:
        this is the one figure a reader compares against `--stale`, which is a
        number of seconds they typed. Rendering it in a different unit from the
        flag that controls it is how a threshold gets read as not working.
        """
        if not self.stale:
            return None
        idle = time.monotonic() - self.moved
        # `told * 2` is the doubling. Below `stale` nothing is said at all, and
        # `told` is 0 until the first report, so that term cannot suppress it.
        if idle < self.stale or idle < self.told * 2:
            return None
        self.told = idle
        return (
            f"STALLED: no new rows for {int(idle)}s at {rows} rows, "
            f"{self.minutes()}m in -- the process is alive and not working",
            -1,
        )


def a_pid(text: str) -> int:
    """``int``, minus the two values that name a group instead of a process.

    A second copy of `alive`'s guard, on purpose: this one exists for the
    *message*. argparse turns an `ArgumentTypeError` into a usage error naming
    the argument, where letting `alive` raise would be a traceback arriving one
    poll after the reader looked away.
    """
    pid = int(text)
    if pid <= 0:
        raise argparse.ArgumentTypeError(f"{pid} names a process group, not a process")
    return pid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.watch",
        description="Report a long job's progress, and say plainly when it dies.",
    )
    parser.add_argument("pid", type=a_pid, help="the job's process id, recorded when it started")
    parser.add_argument("--log", type=Path, required=True, help="the file it appends progress to")
    parser.add_argument(
        "--done",
        type=Path,
        required=True,
        # A file left by an earlier run reads as an instant finish, because this
        # asks whether it is there and not who wrote it. Cheaper to say so than
        # to date-stamp it and be wrong about clock skew -- and `tools.mutate`
        # now clears its own marker before it starts, for that reason.
        #
        # It must be a file that means *finished*. A report written
        # incrementally does not: pointed at one, this announced a finish nine
        # minutes early. `tools.mutate --json r.json` writes `r.json.done` last.
        help="the file it writes when it finishes; not one it appends to as it goes",
    )
    parser.add_argument(
        "--match", default=".", help="count lines of --log matching this regex (default: all)"
    )
    parser.add_argument("--interval", type=float, default=INTERVAL, help="seconds between polls")
    parser.add_argument(
        "--stale",
        type=float,
        default=STALE,
        help="say so when --log has not grown for this long; 0 to never (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    watch = Watch(args.pid, args.log, args.done, re.compile(args.match), args.stale)
    while True:
        event = watch.step()
        if event is not None:
            line, status = event
            # Flushed, because this is read as it happens rather than afterwards
            # -- an unflushed pipe is a watcher that says nothing for an hour and
            # then everything at once, which is the failure it was written for.
            print(line, flush=True)
            if status >= 0:
                return status
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
