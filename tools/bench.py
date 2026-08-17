"""Time the working tree against another revision, without fooling yourself.

    python -m tools.bench --importtime woswoar.__main__
    python -m tools.bench --base main -- list --show 20

Two revisions, one machine, and a difference small enough that the machine's own
drift is the same size as the effect. That combination produced a wrong number
here three times over, in three different ways, and this exists to close all
three: where the two checkouts sat, the order they were measured in, and how the
readings were summarised.

**The filesystem trap.** ``/tmp`` is tmpfs on the machine this was written on and
the checkout is on btrfs, so a worktree in the obvious place reads out of RAM
against a tree reading off an SSD. That is a constant advantage to one side which
no amount of counterbalancing removes, because it is not drift -- and it was
enough to report a resolved 0.08 ms difference between a tree and *itself*. Both
checkouts go on one filesystem, and `same_filesystem` *checks* rather than
assuming, because "beside the repository" is a remedy and not the invariant.

**The ordering trap.** Running A,B,A,B,... looks fair and is not: within every
pair A is always measured first, so anything that makes the machine slower over
time -- thermal throttling, another process starting, the page cache filling --
lands entirely on B. A run structured that way reports a slowdown that is really
just the shape of the afternoon. So the blocks here are **ABBA** and **BAAB**,
alternating: inside one block each side is measured once early and once late, so
a drift that is linear over the block cancels exactly rather than on average.
Alternating the two block shapes balances whichever side goes first as well.

**The statistics trap.** Interleaving the runs and then comparing
``median(A)`` against ``median(B)`` throws the pairing away -- which is the whole
reason for interleaving. It also produces the puzzle of a median gap that is
smaller than the gap between the two minima, because a difference of order
statistics is not an order statistic of differences. What is reported here is the
**median of the per-pair differences**, plus their spread, plus how many pairs
each side won. When the spread straddles zero the honest answer is "not resolved
by this many pairs", and this says so rather than quoting three decimal places.

Nothing here is a substitute for `tests/test_perf.py`, which guards the budget
that matters. This answers the narrower question a refactor raises: did *this
change* cost anything measurable?
"""

from __future__ import annotations

import argparse
import math
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

from tools.sandbox import same_filesystem, sandbox_env, seed_machine, worktree

#: One block measures each side twice. ABBA puts A at the ends and B in the
#: middle; BAAB is its mirror. Either one balances a linear drift on its own;
#: alternating them also balances anything tied to which side opens a block.
BLOCKS = ("ABBA", "BAAB")


class Pair(NamedTuple):
    """One measurement of each side, adjacent in time."""

    base: float
    tree: float
    #: Which side was measured first. Counted into `Summary.tree_first` and
    #: printed, because a counterbalance nobody checks is a claim rather than a
    #: control -- the report used to derive that line as `pairs // 2`, which is
    #: the same sentence with nothing behind it.
    tree_first: bool

    @property
    def delta(self) -> float:
        """How much slower the working tree was. Negative means faster."""
        return self.tree - self.base


def sign_p(favouring: int, total: int) -> float:
    """Two-sided sign test: the chance of a split this lopsided by luck alone.

    The standard non-parametric test for paired measurements, and it needs
    nothing about the shape of the timings -- which is the point, because
    process timings are not normal, have a hard floor and a long right tail, and
    a t-test on them quietly assumes otherwise.
    """
    if total == 0:
        return 1.0
    extreme = max(favouring, total - favouring)
    tail = sum(math.comb(total, count) for count in range(extreme, total + 1))
    return min(1.0, float(2 * tail / 2**total))


class Summary(NamedTuple):
    pairs: int
    median: float
    low: float
    high: float
    tree_slower: int
    base_median: float
    tree_median: float
    #: How many pairs measured the working tree first. Counted, not assumed: if
    #: the block scheduling ever stops alternating, this is where it shows.
    tree_first: int

    @property
    def p(self) -> float:
        return sign_p(self.tree_slower, self.pairs)

    @property
    def resolved(self) -> bool:
        """Whether the split between the two sides is more than luck.

        The question the two hand-rolled benchmarks this replaces could not
        answer. Deliberately a test of *direction* and not of size: a real effect
        far below the budget still resolves here, and it is the magnitude beside
        it that says whether anyone should care. `CLAUDE.md` has the rule for
        that -- a millisecond of fifteen is where an optimisation sequence
        stops.
        """
        return self.p < 0.05


def summarise(pairs: Sequence[Pair]) -> Summary:
    """The paired statistics. Pure, so the design above can be tested."""
    deltas = sorted(pair.delta for pair in pairs)
    quarter = len(deltas) // 4
    return Summary(
        pairs=len(deltas),
        median=statistics.median(deltas),
        low=deltas[quarter],
        high=deltas[-1 - quarter],
        tree_slower=sum(1 for value in deltas if value > 0),
        base_median=statistics.median([pair.base for pair in pairs]),
        tree_median=statistics.median([pair.tree for pair in pairs]),
        tree_first=sum(1 for pair in pairs if pair.tree_first),
    )


def _pairs_from(block: str, samples: Sequence[float]) -> list[Pair]:
    """Turn one block's four readings into two adjacent, oppositely ordered pairs.

    ``ABBA`` gives (A1,B1) -- A first -- and (A2,B2) -- B first. The two pairs
    are what makes the block self-cancelling, so they are formed here rather than
    by taking the block's two A's and two B's as unrelated samples.
    """
    first, second, third, fourth = samples
    if block == "ABBA":
        return [Pair(first, second, tree_first=False), Pair(fourth, third, tree_first=True)]
    return [Pair(second, first, tree_first=True), Pair(third, fourth, tree_first=False)]


def _importtime(tree: Path, module: str, env: dict[str, str]) -> tuple[float, int]:
    """Milliseconds to import ``module``, and the exit status of asking.

    Far quieter than wall clock -- it excludes interpreter startup, which is most
    of a short run and none of the question -- and it is the number
    `docs/woswoar_design_summary.md` budgets against.
    """
    done = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        cwd=tree,
        env=env,
        check=False,
    )
    for line in reversed(done.stderr.splitlines()):
        if line.rstrip().endswith(module):
            return int(line.split("|")[1]) / 1000, done.returncode
    raise SystemExit(f"no importtime line for {module}; is it importable from {tree}?")


def _wall(tree: Path, argv: Sequence[str], env: dict[str, str]) -> tuple[float, int]:
    """Milliseconds of wall clock for one `woswoar` run, and its exit status."""
    started = time.perf_counter()
    done = subprocess.run(
        [sys.executable, "-m", "woswoar", *argv],
        capture_output=True,
        cwd=env["HOME"],
        env=env,
        check=False,
    )
    return (time.perf_counter() - started) * 1000, done.returncode


def run(
    revision: str, blocks: int, module: str | None, argv: Sequence[str], warmup: int
) -> tuple[Summary, str, str]:
    """``(summary, sha, note)`` for the working tree against ``revision``."""
    here = Path.cwd()
    # Beside the repository, so that the checkout and the tree it is compared
    # against are on one filesystem -- and then *checked*, because "beside" is a
    # remedy for the tmpfs trap rather than the invariant, and it fails silently
    # wherever the parent directory is a different mount.
    with (
        worktree(revision, beside=here.parent) as (base, sha),
        tempfile.TemporaryDirectory(dir=here.parent, prefix=".woswoar-bench-home-") as holder,
    ):
        if not same_filesystem(base, here):
            raise SystemExit(
                f"{base} and {here} are on different filesystems, so one side would "
                f"be measured against faster storage than the other. That is not "
                f"drift and no amount of counterbalancing removes it."
            )
        trees = {"base": base, "tree": here}
        envs = {}
        for side in trees:
            home = Path(holder) / side
            home.mkdir()
            seed_machine(home, "bench@sandbox")
            # Once per side, outside every timed region.
            envs[side] = sandbox_env(trees[side], home)

        codes: set[int] = set()

        def once(side: str) -> float:
            if module:
                elapsed, code = _importtime(trees[side], module, envs[side])
            else:
                elapsed, code = _wall(trees[side], argv, envs[side])
            codes.add(code)
            return elapsed

        # Thrown away rather than counted: the first run of each side pays for
        # cold page cache and a `__pycache__` that does not exist yet, and that
        # cost belongs to whichever side happened to go first.
        for _ in range(warmup):
            once("base")
            once("tree")

        pairs: list[Pair] = []
        for index in range(blocks):
            block = BLOCKS[index % len(BLOCKS)]
            samples = [once("base" if letter == "A" else "tree") for letter in block]
            pairs += _pairs_from(block, samples)

    return summarise(pairs), sha, comparable(codes)


def comparable(codes: set[int]) -> str:
    """One line about the exit statuses seen, or a refusal. Pure, so it is testable.

    The sides have to have done the same work, or the difference is not a
    difference in speed. Comparing against a revision where a flag did not exist
    yet is the ordinary way in: the base exits in 40 ms having printed a usage
    error while the tree does the job in 300 ms, and without this the report calls
    that a resolved 260 ms slowdown -- the same class as the three traps the
    module docstring names, and the one it left open. `compare` checks exit status
    already; the asymmetry was the tell.

    A consistently non-zero status is fine, and has to be: `doctor` exits 1
    whenever it has anything to report. What is refused is *disagreement*.
    """
    if len(codes) > 1:
        raise SystemExit(
            f"the two revisions exited differently ({sorted(codes)}), so they did "
            f"not do the same work and the timings are not comparable."
        )
    return f"both sides exited {codes.pop() if codes else 0}"


def report(summary: Summary, revision: str, sha: str, what: str, note: str = "") -> str:
    lines = [
        f"{what}: working tree vs {revision} ({sha})",
        f"  {summary.pairs} pairs in {summary.pairs // 2} ABBA/BAAB blocks, "
        f"{summary.tree_first} measured the working tree first" + (f"; {note}" if note else ""),
        f"  base median {summary.base_median:.2f} ms   tree median {summary.tree_median:.2f} ms",
        f"  paired delta: median {summary.median:+.2f} ms, middle half "
        f"{summary.low:+.2f} to {summary.high:+.2f} ms",
        f"  the tree was slower in {summary.tree_slower} of {summary.pairs} pairs "
        f"(sign test p={summary.p:.3f})",
    ]
    if summary.resolved:
        direction = "slower" if summary.median > 0 else "faster"
        lines.append(f"  => the working tree is {direction} by about {abs(summary.median):.2f} ms")
    else:
        # The answer the two earlier hand-rolled benchmarks should have given.
        lines.append(
            "  => not resolved: the two sides split about evenly, so this machine's "
            "own noise\n     is larger than whatever the difference is. Quote it as "
            "'below measurement',\n     not as a number, or run more blocks."
        )
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.bench",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default="main", help="revision to compare against")
    parser.add_argument(
        "--blocks", type=int, default=25, help="blocks of four runs (default: 25, so 50 pairs)"
    )
    parser.add_argument(
        "--importtime",
        metavar="MODULE",
        help="measure importing this module instead of wall clock -- much quieter, "
        "and the number the Ctrl-R budget is stated in",
    )
    parser.add_argument("--warmup", type=int, default=2, help="discarded runs per side")
    parser.add_argument("command", nargs="*", help="woswoar arguments, when not using --importtime")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.importtime and not args.command:
        parser.error("give --importtime MODULE, or a woswoar command to time")

    if args.blocks < 1:
        parser.error("--blocks must be at least 1; there is nothing to summarise from none")

    summary, sha, note = run(args.base, args.blocks, args.importtime, args.command, args.warmup)
    what = f"import {args.importtime}" if args.importtime else f"woswoar {' '.join(args.command)}"
    print(report(summary, args.base, sha, what, note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
