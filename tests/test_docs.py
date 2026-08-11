"""Instructions in the README that a reader is expected to paste and run.

Prose can be reviewed by reading it. A command block cannot: the systemd one
told people to `cp contrib/systemd/woswoar-sync.*`, which is a path that exists
only in a checkout, and every reader who installed the documented way -- `pipx
install`, no clone -- got `cp: cannot stat`. It read correctly for months.

So the block is run, verbatim, the way a reader would run it: real `bash`, a
scratch `$HOME`, and a stub `systemctl` standing in for the one piece that needs
a running system. Following the repository's habit of driving the real thing --
the shell hook is tested against a real `bash`, sync against real `age` and
`git`.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"

#: The `<details>` the block lives in, named by its summary rather than by a
#: line number so that editing the README above it does not silently point this
#: at some other code block.
_SECTION = "Keeping a machine current while nobody is using it"

_FENCED = re.compile(r"```bash\n(.*?)```", re.S)


def systemd_block() -> str:
    """The one shell block under the idle-machine `<details>`."""
    _, marker, rest = README.read_text(encoding="utf-8").partition(_SECTION)
    if not marker:
        raise AssertionError(f"README has no section titled {_SECTION!r}")
    section, _, _ = rest.partition("</details>")
    blocks = _FENCED.findall(section)
    if len(blocks) != 1:
        raise AssertionError(f"expected one bash block in that section, found {len(blocks)}")
    return str(blocks[0])


class TestTheSystemdInstructionsWork(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="woswoar-readme-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        # Everything the block needs that a scratch machine would not have. A
        # stub rather than a skip: `systemctl --user` needs a running user
        # manager, which no CI runner has, and skipping would leave the whole
        # block unrun on exactly the machines that check it.
        stub_dir = self.home / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "systemctl"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.home}/systemctl.log"\n')
        stub.chmod(0o755)
        self.stub_dir = stub_dir

    def run_block(self) -> None:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        }
        done = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", systemd_block()],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, f"the block failed:\n{done.stderr}")

    def units(self) -> dict[str, str]:
        unit_dir = self.home / ".config/systemd/user"
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(unit_dir.glob("*"))}

    def test_pasting_the_block_leaves_both_units_on_disk(self) -> None:
        """The regression: `cp contrib/systemd/…` left nothing at all here, and
        nothing but a reader trying it would have said so."""
        self.run_block()
        self.assertEqual(sorted(self.units()), ["woswoar-sync.service", "woswoar-sync.timer"])

    def test_the_units_say_what_they_have_to_say(self) -> None:
        """Enough of each that a block which produced two empty files, or
        wrote the service's text into the timer, is not mistaken for a pass.

        `/usr/bin/env` and not a path: the unit has to find woswoar wherever it
        was installed -- pipx, `--user`, a venv -- and hardcoding one is how it
        works on the author's machine only.
        """
        self.run_block()
        service, timer = self.units().values()
        self.assertIn("ExecStart=/usr/bin/env woswoar sync", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("OnUnitActiveSec=", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_the_timer_is_the_thing_that_gets_enabled(self) -> None:
        """The service is `oneshot` and has no `[Install]` of its own, so
        enabling *it* installs a unit that never fires. The timer pulls the
        service in; that is the whole shape of a systemd timer, and getting it
        backwards leaves a machine that looks configured and syncs never."""
        self.run_block()
        log = (self.home / "systemctl.log").read_text(encoding="utf-8")
        self.assertIn("--user enable --now woswoar-sync.timer", log)

    def test_the_block_needs_nothing_that_is_not_on_the_machine(self) -> None:
        """The defect itself, stated directly. A `pipx install` leaves no
        checkout, so any path into this repository is a path the reader does not
        have -- and `cp` from one fails in a way that reads like their mistake.
        """
        block = systemd_block()
        self.assertNotIn("contrib/", block)
        self.assertNotIn("git clone", block)


if __name__ == "__main__":
    unittest.main()
