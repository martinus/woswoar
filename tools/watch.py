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

    def __init__(self, pid: int, log: Path, done: Path, pattern: re.Pattern[str]) -> None:
        self.pid = pid
        self.log = log
        self.done = done
        self.pattern = pattern
        self.began = time.monotonic()
        #: -1 rather than 0, so a job that is already at zero rows still gets its
        #: first "working" line. Starting at 0 would make the opening silence
        #: indistinguishable from a job that never starts.
        self.last = -1

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
            return f"working: {rows} rows after {self.minutes()}m", -1
        return None


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
        # A report left by an earlier run reads as an instant finish, because
        # this asks whether the file is there and not who wrote it. Cheaper to
        # say so than to date-stamp it and be wrong about clock skew.
        help="the file it writes when it finishes; remove a previous run's first",
    )
    parser.add_argument(
        "--match", default=".", help="count lines of --log matching this regex (default: all)"
    )
    parser.add_argument("--interval", type=float, default=INTERVAL, help="seconds between polls")
    args = parser.parse_args(argv)

    watch = Watch(args.pid, args.log, args.done, re.compile(args.match))
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
