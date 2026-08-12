"""Shared test scaffolding."""

from __future__ import annotations

import fcntl
import io
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from typing import NamedTuple

from woswoar import crypto, store
from woswoar.__main__ import main
from woswoar.entry import Entry

MACHINE_ID = "0123456789abcdef"

#: External tools some suites need. Defined once so that a check which has to
#: grow later -- an age version floor, say -- cannot be updated in one file and
#: forgotten in the other.
requires_age = unittest.skipUnless(crypto.available(), "age required")
requires_git = unittest.skipUnless(shutil.which("git"), "git required")
requires_ssh_keygen = unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen required")
requires_bash = unittest.skipUnless(shutil.which("bash"), "bash required")
requires_zsh = unittest.skipUnless(shutil.which("zsh"), "zsh required")


def bash_major() -> int:
    """The major version of the `bash` on PATH, or 0 if there is none.

    Here rather than in the hook suite because it is not only that suite's
    question any more: macOS ships bash 3.2, so *any* test that drives a real
    bash has to say so or it fails there rather than skipping. `requires_bash`
    on its own is not enough -- macOS has a bash, just not one woswoar records
    from.
    """
    bash = shutil.which("bash")
    if not bash:
        return 0
    out = subprocess.run(
        [bash, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False
    )
    return int(out.stdout.strip() or 0)


requires_bash5 = unittest.skipUnless(bash_major() >= 5, "bash 5.0+ required")


def zsh_major() -> int:
    """The major version of the `zsh` on PATH, or 0 if there is none.

    Beside `bash_major` for the same reason it is here rather than in the hook
    suite: any test that drives a real zsh has to say which zsh, because the
    hook refuses below 5 and a test that only asked for `zsh` would fail rather
    than skip.
    """
    zsh = shutil.which("zsh")
    if not zsh:
        return 0
    out = subprocess.run(
        [zsh, "-f", "-c", "echo ${ZSH_VERSION%%.*}"], capture_output=True, text=True, check=False
    )
    return int(out.stdout.strip() or 0)


requires_zsh5 = unittest.skipUnless(zsh_major() >= 5, "zsh 5.0+ required")
requires_fzf = unittest.skipUnless(shutil.which("fzf"), "fzf required")


class Ran(NamedTuple):
    """What one CLI invocation did."""

    code: int
    out: str
    err: str


class Captured(io.StringIO):
    """A captured stream that can claim to be a terminal, which StringIO cannot.

    Several commands branch on `isatty` -- `doctor` picks its markers, `sync`
    decides whether a failure was witnessed, `progress` decides whether to draw
    at all -- and capturing the output is exactly what turns that answer to
    False. So the harness has to be able to say otherwise.
    """

    def __init__(self, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def run_cli(*argv: str, tty: bool = False) -> Ran:
    """Drive the real CLI in process, capturing both streams.

    Both, because several commands put the thing a test cares about on stderr --
    `sync`'s warning that a day could not be authenticated, `grant`'s refusal to
    act without a terminal. Four modules used to spell this out separately and
    every one of them captured stdout alone, so a test meaning "it warned" could
    only check the exit code, and would have passed had the warning been
    deleted. That is the failure mode `CLAUDE.md` rule 3 is about, and it was
    invisible at each individual call site.

    `tty` makes both streams answer `isatty` the way a real terminal would.
    Patching `sys.stderr.isatty` from a test does not work and fails quietly:
    the patch lands on whatever object was installed at the time, and then this
    function replaces it.
    """
    out, err = Captured(tty), Captured(tty)
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return Ran(code, out.getvalue(), err.getvalue())


class Typed(NamedTuple):
    """One exchange with a program running under a terminal.

    ``send`` is typed at it verbatim -- control characters included, so a
    keypress is just ``"\\x12"`` -- and then output is read until ``until``
    shows up. Reading to a marker rather than for a fixed time is what keeps
    this from being a sleep race: a step that never produces its marker fails
    with the whole transcript rather than passing on an empty read.

    ``until`` must be a string the program *prints*, not one the harness typed:
    a terminal echoes what you type back at you, so waiting for your own input
    is satisfied instantly and asserts nothing. `CLAUDE.md` rule 3, fourth
    bullet. The trick used here and in the tests is `echo MAR''KER` -- typed
    with the quotes, printed without.
    """

    send: str
    until: str


def run_in_pty(
    argv: Sequence[str],
    steps: Sequence[Typed],
    *,
    env: dict[str, str],
    size: tuple[int, int] = (40, 120),
    timeout: float = 30.0,
) -> list[str]:
    """Drive ``argv`` under a real pseudo-terminal, one exchange at a time.

    Returns what the program wrote during each step, in order, so a test can
    assert about the state of the screen *between* two keypresses -- which is
    where "the recalled command is sitting on the prompt and has not run" lives.
    ``timeout`` is per step, not for the run.

    A pty rather than pipes because the things worth testing about a shell only
    exist when it has a terminal: readline does no line editing without one, and
    the Ctrl-R widget reads its picker's input from `/dev/tty`, which a pipe
    cannot provide. `pty.fork` rather than `pty.openpty` plus `Popen` because it
    also makes the slave the child's *controlling* terminal, and a GitHub runner
    has none to inherit.

    The window size is set explicitly and before the exec, because a pty starts
    at 0x0 and a program that believes it has no room draws almost nothing --
    fzf, which #152 will drive through here, renders into three lines. Readline
    covers for it by falling back to 80x24, so no test that only runs bash could
    notice this going missing; `TestThePtyHarness` asserts it directly instead.
    """
    rows, cols = size
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - the child execs or dies
        # Nothing here may raise back into the test process: after the fork
        # there are two of those, and the second one continuing to run the suite
        # would be a mess to even diagnose.
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.execvpe(argv[0], list(argv), env)
        except BaseException:
            pass
        os._exit(127)

    transcript: list[str] = []
    # Whatever arrived *after* the previous step's marker. A single `os.read`
    # takes up to 64 KiB, so a shell that prints its output and redraws its
    # prompt quickly enough delivers both in one chunk -- and a step waiting for
    # that prompt would then wait for something it had already been handed, until
    # the timeout. It is a load-dependent flake: rare on an idle machine, and
    # reproducible under the parallel runner.
    #
    # Offered only to a step that sends *nothing*, which is the only shape that
    # flake takes. A step that types something must wait for output produced
    # after its keypress, or its marker stops witnessing that the keypress was
    # processed at all -- and two consecutive steps sharing a marker (`~/d2` at
    # `tests/test_search.py`, where Ctrl-T waits for the row it was pressed on)
    # would then let the second one return before fzf had seen the key. What is
    # dropped is only ever a tail the previous step already read.
    carry = b""
    try:
        for index, step in enumerate(steps):
            if step.send:
                os.write(master, step.send.encode())
                carry = b""
            seen, carry = _read_until(master, step.until, timeout, index, transcript, carry)
            transcript.append(seen)
        return transcript
    finally:
        os.close(master)
        # SIGKILL, not SIGTERM: an interactive bash ignores SIGTERM, and a test
        # that leaves one running holds a worker of the parallel runner forever.
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def _read_until(
    fd: int, marker: str, timeout: float, index: int, before: list[str], carry: bytes = b""
) -> tuple[str, bytes]:
    """Read from ``fd`` until ``marker`` appears, or fail with what did appear.

    Returns what was read up to and including the marker, and whatever came
    after it -- which the caller hands back as ``carry`` on the next step, so
    that output the marker shared a read with is not thrown away.

    An `AssertionError` rather than a `TimeoutError`, and carrying every step's
    output rather than this one's: a terminal test that goes wrong says nothing
    about *why* from its exception alone, and the answer is almost always
    several steps back -- a prompt that never came, a shell that died at the
    line before.
    """
    deadline = time.monotonic() + timeout
    buffer = carry
    needle = marker.encode()

    def stalled(reason: str) -> AssertionError:
        seen = [*before, buffer.decode("utf-8", "replace")]
        return AssertionError(
            f"step {index} waited for {marker!r} and {reason}. Transcript:\n" + "\n---\n".join(seen)
        )

    while needle not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise stalled("timed out")
        if not select.select([fd], [], [], remaining)[0]:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            # EIO on Linux is how a pty reports that the child is gone; it is an
            # ordinary end of stream here, not a fault of its own.
            chunk = b""
        if not chunk:
            raise stalled("the program exited")
        if _CURSOR_QUERY in chunk:
            # A real terminal answers this, and a program that asks waits. fzf
            # asks as soon as it starts, because `--height` draws an inline
            # window and it has to know which row it is on -- so without a reply
            # it never paints, and the failure looks like a picker that produced
            # nothing rather than one that is still waiting for us.
            os.write(fd, _CURSOR_REPORT)
        buffer += chunk
    cut = buffer.index(needle) + len(needle)
    return buffer[:cut].decode("utf-8", "replace"), buffer[cut:]


#: "Where is the cursor?", and an answer. Row 1 column 1 is a lie in general and
#: exactly right here: nothing has been printed above the program under test, so
#: it has the whole window.
_CURSOR_QUERY = b"\x1b[6n"
_CURSOR_REPORT = b"\x1b[1;1R"


def loose_paths() -> list[str]:
    """Paths another account could read, walked independently of the code.

    Deliberately not `store.readable_by_others()`: asking the helper about
    itself could not catch it forgetting a path, which is the failure this
    guards against. `history/` is excluded -- it is ciphertext arriving from
    `git clone`, and the directory above it is owner-only.
    """
    roots = [store.data_dir(), store.config_dir(), store.cache_dir()]
    seen = [r for r in roots if r.is_dir()]
    seen += [p for r in list(seen) for p in r.rglob("*")]
    return sorted(
        f"{p} is {oct(p.stat().st_mode & 0o777)}"
        for p in seen
        if p.stat().st_mode & 0o077 and store.history_dir() not in [p, *p.parents]
    )


def make_entry(ts: int, cmd: str, host: str = MACHINE_ID, session: str = "s1") -> Entry:
    """An Entry with plausible defaults, so fixture fields live in one place."""
    return Entry(ts=ts, host=host, session=session, cwd="/tmp", exit_code=0, duration_ms=1, cmd=cmd)


#: Every environment variable store.py consults, from the module that owns the
#: list -- so a variable added there cannot be isolated here and leak in
#: `prove`'s sandbox, or the reverse. This alias keeps the suite's historical
#: import path working.
ENV_KEYS = store.ENV_KEYS


class WoswoarTestCase(unittest.TestCase):
    """Runs each test against a throwaway WOSWOAR_DIR.

    Every path in :mod:`woswoar.store` is resolved from the environment on each
    call rather than at import time, which is what makes this isolation work.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="woswoar-test-")
        # Resolved, which is a no-op on Linux and load-bearing on macOS: there
        # `$TMPDIR` is `/var/folders/...` and `/var` is a symlink to
        # `/private/var`. So a sandbox `$HOME` spelled `/var/...` and an
        # `os.getcwd()` inside it spelled `/private/var/...` are the same
        # directory under two names -- and `search`'s `dir` scope, which
        # deliberately treats a symlinked path and its target as *different*
        # keys, then finds nothing. That is correct behaviour meeting a fixture
        # that did not mean to exercise it, and it cost two failures on the
        # first macOS run.
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        self.addCleanup(self._restore_env)

        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["WOSWOAR_DIR"] = str(self.root / "data")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        os.environ["XDG_CACHE_HOME"] = str(self.root / "cache")

        (store.config_dir()).mkdir(parents=True, exist_ok=True)
        (store.config_dir() / "machine").write_text(
            f"id={MACHINE_ID}\nname=test@machine\n", encoding="utf-8"
        )

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_log(self, host: str, day: str, lines: list[str]) -> Path:
        """Write raw log lines for ``host`` on ``day`` and return the path."""
        path = store.logs_dir() / "hosts" / host / f"{day}.tsv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return path

    entry = staticmethod(make_entry)
