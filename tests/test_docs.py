"""The parts of the documentation that can be wrong in a checkable way.

Prose can be reviewed by reading it. A command block cannot: the systemd one
told people to `cp contrib/systemd/woswoar-sync.*`, which is a path that exists
only in a checkout, and every reader who installed the documented way -- `pipx
install`, no clone -- got `cp: cannot stat`. It read correctly for months.

So the block is run, verbatim, the way a reader would run it: real `bash`, a
scratch `$HOME`, and a stub `systemctl` standing in for the one piece that needs
a running system. Following the repository's habit of driving the real thing --
the shell hook is tested against a real `bash`, sync against real `age` and
`git`.

A link is the same kind of claim and fails the same way: silently, and only for
the reader. Two were broken when this file grew its second class.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import unquote

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


#: Markdown inline links, `[text](target)`. Reference-style links are not used
#: anywhere in these files, and adding the second syntax to this pattern for a
#: form nobody writes would only make it harder to read.
_LINK = re.compile(r"\]\(([^)\s]+)\)")

#: A heading, and the text GitHub turns into its anchor.
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading.

    Lowercase, spaces to hyphens, and everything that is neither alphanumeric
    nor a hyphen or underscore dropped -- *except* combining marks and format
    characters, which survive. That last clause is not pedantry: `## ⚠️ What is
    not protected` anchors at `%EF%B8%8F-what-is-not-protected`, because U+26A0
    is a symbol and goes, while the U+FE0F variation selector welded to it is a
    nonspacing mark and stays. A link that drops it lands nowhere, silently.

    Derived from the rendered page rather than from a specification -- GitHub
    publishes no algorithm -- and checked against every heading in `docs/`, the
    ticks and boxes and warning signs included.
    """
    out = []
    for char in heading.lower():
        if char.isalnum() or char in "-_":
            out.append(char)
        elif char == " ":
            out.append("-")
        elif unicodedata.category(char) in ("Mn", "Cf"):
            out.append(char)
    return "".join(out)


def markdown_files() -> list[Path]:
    # Dot-directories are other tools' working space, `.github` excepted --
    # `.pytest_cache/README.md` was being read and checked here, and a failure
    # in somebody else's generated file is one nobody in this repository can act
    # on. Whether the cache exists at all depends on how the suite was last run,
    # which is not a thing a test should vary with.
    return sorted(
        p
        for p in REPO.glob("**/*.md")
        if not any(part.startswith(".") and part != ".github" for part in p.parts)
    )


class TestEveryDocumentLinkLandsSomewhere(unittest.TestCase):
    """A relative link in the docs points at a file, and at a heading in it.

    Both halves have been wrong here. `docs/verify.md` pointed at
    `#-guarantees-pinned-by-tests` for a heading that ends `, not by prose`, so
    the reader sent to "the list of things this project asserts" arrived at the
    top of the page instead. Nothing said so: a browser given an anchor it
    cannot find does not complain, it just does not move.

    Only relative links. An external URL needs the network to check, and a test
    that fails when GitHub is slow is worse than no test.
    """

    def links(self) -> list[tuple[Path, str]]:
        found = []
        for path in markdown_files():
            for target in _LINK.findall(path.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                found.append((path, target))
        return found

    def test_there_are_links_to_check(self) -> None:
        """Without this the two tests below are vacuous if the pattern rots."""
        self.assertGreater(len(self.links()), 25)

    def test_each_one_names_a_file_that_exists(self) -> None:
        missing = []
        for source, target in self.links():
            path, _, _ = target.partition("#")
            if path and not (source.parent / path).exists():
                missing.append(f"{source.relative_to(REPO)} -> {target}")
        self.assertEqual(missing, [])

    def test_each_anchor_names_a_heading_that_exists(self) -> None:
        broken = []
        for source, target in self.links():
            path, _, anchor = target.partition("#")
            if not anchor:
                continue
            doc = (source.parent / path) if path else source
            headings = {slug(h) for h in _HEADING.findall(doc.read_text(encoding="utf-8"))}
            # Percent-encoded, because that is how a `⚠️` anchor has to be
            # written in a link and how the two in this repository are written.
            if anchor not in headings and unquote(anchor) not in headings:
                broken.append(f"{source.relative_to(REPO)} -> {target}")
        self.assertEqual(broken, [])


if __name__ == "__main__":
    unittest.main()
