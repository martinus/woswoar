"""Run the same commands against two revisions and diff what they printed.

The check a refactor that is meant to change nothing has to pass, and the one
that is tedious enough by hand that it gets skipped. Three slices of the #199 to
#202 work each rebuilt this from scratch -- a worktree at the base revision, a
throwaway ``$HOME`` for each side, a normalisation pass, a ``diff`` -- and the
normalisation is the part where a mistake is invisible, because scrubbing too
much makes everything match.

    python -m tools.compare --base main -- install doctor status

Each argument after ``--`` is a woswoar command line, run in order in one
``$HOME`` so that state accumulates exactly as it would for a person. Both sides
get the same fresh ``$HOME``, seeded with the same machine id, and the output of
every command -- stdout, stderr and exit status -- is concatenated and compared.

**On scrubbing.** Only two things are normalised by default: the throwaway home
and the path of the tree being run. Both are pure environment and carry no
information about the program. Anything else has to be asked for with
``--scrub``, and the active list is printed in the header of the report -- so
that a pull request quoting "byte-identical" says underneath it what was
normalised to get there. A claim of identical output after normalising the
interesting parts away is worth nothing, and there is no way to tell from the
outside which kind you are reading.

Exits 0 when the two agree and 1 when they do not, printing a unified diff.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from difflib import unified_diff
from pathlib import Path

from tools.sandbox import sandbox_env, seed_machine, worktree


def _scrubs(parser: argparse.ArgumentParser, given: Sequence[str]) -> list[tuple[str, str]]:
    """``REGEX=REPLACEMENT`` pairs, in the order given."""
    out = []
    for text in given:
        pattern, sep, replacement = text.partition("=")
        if not sep:
            parser.error(f"--scrub wants REGEX=REPLACEMENT, got {text!r}")
        out.append((pattern, replacement))
    return out


def _record(
    tree: Path, commands: Sequence[str], show: Sequence[str], scrubs: Sequence[tuple[str, str]]
) -> str:
    """Everything ``tree`` printed for ``commands``, normalised."""
    with tempfile.TemporaryDirectory(prefix="woswoar-compare-") as home:
        root = Path(home)
        seed_machine(root, "compare@sandbox")
        env = sandbox_env(tree, root)
        out = []
        for command in commands:
            argv = shlex.split(command)
            out.append(f"===== woswoar {' '.join(argv)}")
            done = subprocess.run(
                [sys.executable, "-m", "woswoar", *argv],
                cwd=env["HOME"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            # Interleaved as one block rather than two sections: several commands
            # put the interesting half on stderr, and which stream a line went to
            # is itself something a refactor can change by accident.
            out.append(done.stdout + done.stderr)
            out.append(f"----- exit {done.returncode}")
        for name in show:
            path = Path(env["HOME"]) / name
            out.append(f"===== file {name}")
            out.append(path.read_text(encoding="utf-8") if path.is_file() else "<absent>")

        text = "\n".join(out)
        # The two the caller never has to ask for, because neither is about the
        # program: where the sandbox happened to land, and where the tree is.
        text = text.replace(env["HOME"], "$HOME").replace(str(tree), "$TREE")
        for pattern, replacement in scrubs:
            text = re.sub(pattern, replacement, text)
        return text


def compare(
    revision: str,
    commands: Sequence[str],
    show: Sequence[str] = (),
    scrubs: Sequence[tuple[str, str]] = (),
) -> tuple[bool, str]:
    """``(agreed, report)`` for the working tree against ``revision``."""
    here = Path.cwd()
    with worktree(revision) as (base, sha):
        before = _record(base, commands, show, scrubs)
        after = _record(here, commands, show, scrubs)
    return verdict(before, after, f"{revision} ({sha})", len(commands), scrubs)


def verdict(
    before: str,
    after: str,
    label: str,
    commands: int,
    scrubs: Sequence[tuple[str, str]] = (),
) -> tuple[bool, str]:
    """``(agreed, report)`` for two recordings. Pure, and separate on purpose.

    This is the half that can lie -- by calling two different things identical,
    or by reporting a comparison without saying what was normalised to reach it
    -- and it is the half a test can reach without a git worktree and a
    subprocess for each side.
    """
    active = "\n".join(f"  {pattern} -> {value}" for pattern, value in scrubs) or "  (none)"
    header = (
        f"{commands} command(s) against {label}, {len(before.splitlines())} lines\n"
        f"scrubs beyond $HOME and $TREE:\n{active}"
    )
    if before == after:
        return True, f"{header}\nidentical"
    diff = unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=label,
        tofile="working tree",
    )
    return False, f"{header}\n\n" + "".join(diff)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.compare",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default="main", help="revision to compare against")
    parser.add_argument(
        "--show",
        action="append",
        default=[],
        metavar="PATH",
        help="also compare this file's contents, relative to the sandbox home "
        "(repeatable) -- .bashrc, .zshrc",
    )
    parser.add_argument(
        "--scrub",
        action="append",
        default=[],
        metavar="REGEX=REPLACEMENT",
        help="normalise something else before comparing (repeatable). Listed in "
        "the report, because what was normalised is half of what 'identical' means",
    )
    parser.add_argument("commands", nargs="+", help="woswoar command lines, run in order")
    args = parser.parse_args(list(argv) if argv is not None else None)

    agreed, report = compare(args.base, args.commands, args.show, _scrubs(parser, args.scrub))
    print(report)
    return 0 if agreed else 1


if __name__ == "__main__":
    raise SystemExit(main())
