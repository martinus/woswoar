"""Why a mutation survived: nothing ran the line, or nothing asserted on it.

A survivor list is not a finding. Run the generator over a whole package and it
answers with hundreds of rows, and the shape of that pile is not visible from
the pile: `progress.py` and `store.py` survive at the same rate and mean
opposite things. Reading it unaided produced two confident, wrong triages here
before this module existed -- first by grepping the mutated *line* for words
like `mkdir`, then by ranking functions by survival ratio. Each found real gaps
and each presented a sample as an accounting.

The partition that does hold is against **coverage**:

- a survivor on a line no test executes is a **missing test**. Nothing reaches
  the code, so no fixture could have caught anything;
- a survivor on a line the suite *does* execute is a **weak fixture or an
  equivalent mutant**. The test got there and said nothing about that effect,
  which is `CLAUDE.md` rule 3's "suspect the fixture before the mutation".

Those are different problems with different answers, and the second is much
larger -- 590 of 751 on the run this was written for. Conflating them is what
makes a survivor list read as several hundred bugs.

**Coverage is wrong here, and the mutation data corrects it.** In-process
coverage cannot see a line executed by a real subprocess, and this repository
tests its shell hook by running a real `bash` that runs `python -m woswoar`. On
the run above, 111 mutants were *caught* on lines coverage called unexecuted --
impossible unless the line ran. A caught mutant is proof of execution, so
`repair` folds that evidence back in before anything is partitioned. Without it
those lines read as "no test reaches this" and the missing-test list is a third
noise.

**No dependency on `coverage`.** `pyproject.toml` declares none and this is not
the module to start, so the map arrives as the JSON `coverage json` already
emits. Produce it however you like; this only reads `files.*.executed_lines`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple


class Row(NamedTuple):
    """One mutation and what became of it, as `tools/mutate.py --json` writes it."""

    label: str
    path: str
    line: int
    outcome: str

    @property
    def survived(self) -> bool:
        return self.outcome == "survived"

    @property
    def answered(self) -> bool:
        """Caught or survived. `broke` and `timeout` say nothing about a line.

        They are excluded from `repair` for that reason: a row that never got to
        ask is not evidence its line ran.
        """
        return self.outcome in ("caught", "survived")


def rows_from(results: dict[str, Any]) -> list[Row]:
    """The rows of a `--json` report, ignoring anything without a position."""
    out = []
    for item in results.get("results", []):
        line = item.get("line")
        if line is None:
            continue
        out.append(Row(item["label"], item["path"], int(line), item["outcome"]))
    return out


def executed_lines(coverage: dict[str, Any]) -> dict[str, set[int]]:
    """`{path: {line, ...}}` from a `coverage json` report.

    Paths are taken as the report spells them; `coverage` writes them relative
    to the run's root, which is what a mutation label carries too.
    """
    return {
        path.replace("\\", "/"): set(info.get("executed_lines", []))
        for path, info in coverage.get("files", {}).items()
    }


def repair(executed: dict[str, set[int]], rows: list[Row]) -> dict[str, set[int]]:
    """`executed`, plus every line a mutation proves ran by being caught.

    The correction the docstring above argues for. Only `caught` counts: a
    survivor is exactly the case in doubt, and `broke`/`timeout` never reached
    the question.
    """
    fixed = {path: set(lines) for path, lines in executed.items()}
    for row in rows:
        if row.outcome == "caught":
            fixed.setdefault(row.path, set()).add(row.line)
    return fixed


class Split(NamedTuple):
    """Survivors, partitioned by whether anything runs the line."""

    unreached: list[Row]
    weak: list[Row]

    @property
    def total(self) -> int:
        return len(self.unreached) + len(self.weak)


def partition(rows: list[Row], executed: dict[str, set[int]]) -> Split:
    unreached: list[Row] = []
    weak: list[Row] = []
    for row in rows:
        if not row.survived:
            continue
        (weak if row.line in executed.get(row.path, set()) else unreached).append(row)
    return Split(unreached, weak)


def _summarise(rows: list[Row], split: Split, corrected: int) -> None:
    counts = Counter(row.outcome for row in rows)
    print(f"{'ALL':<46}{len(rows):>6}")
    print(f"{'  caught':<46}{counts['caught']:>6}")
    for outcome in ("broke", "timeout"):
        if count := counts[outcome]:
            print(f"{'  ' + outcome + ' (asked nothing)':<46}{count:>6}")
    print(f"{'  SURVIVED':<46}{split.total:>6}")
    print(f"{'    on a line NO test executes  (missing test)':<46}{len(split.unreached):>6}")
    print(f"{'    on a line tests DO execute  (weak/equiv)':<46}{len(split.weak):>6}")
    if corrected:
        # Said out loud, because it is the difference between this partition and
        # a naive one, and because a large number here means the coverage map
        # was taken from a different tree than the run.
        print(
            f"\n{corrected} line(s) had a caught mutation but were absent from the "
            "coverage map,\nso the suite reaches them through a subprocess. Folded in."
        )
    if not any(row.answered for row in rows):
        print("\nnothing was answered: no partition is possible.")


def _by_file(rows: list[Row]) -> None:
    # `sorted` rather than `most_common`, which does not tie-break by path and
    # would make two runs of the same state print in different orders.
    for path, n in sorted(
        Counter(row.path for row in rows).items(), key=lambda kv: (-kv[1], kv[0])
    ):
        print(f"  {n:5d}  {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.reached",
        description="Split mutation survivors into missing tests and weak fixtures.",
        epilog=(
            "Produce the coverage map with:\n"
            "  coverage run --source=woswoar -m unittest discover -s . -t . -p 'test_*.py'\n"
            "  coverage json -o coverage.json\n"
            "and the results with `python -m tools.mutate --base main --json results.json`.\n"
            "Both must come from the same tree: a fix that shifts line numbers "
            "silently unmatches them."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("results", type=Path, help="a --json report from tools.mutate")
    parser.add_argument("coverage", type=Path, help="a `coverage json` report")
    parser.add_argument("--list", action="store_true", help="print the unreached survivors")
    args = parser.parse_args(argv)

    try:
        rows = rows_from(json.loads(args.results.read_text(encoding="utf-8")))
        raw = executed_lines(json.loads(args.coverage.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read the inputs: {exc}") from None
    if not rows:
        raise SystemExit(f"{args.results} holds no positioned rows to classify.")

    fixed = repair(raw, rows)
    corrected = sum(len(fixed[p] - raw.get(p, set())) for p in fixed)
    split = partition(rows, fixed)
    _summarise(rows, split, corrected)
    if split.unreached:
        print("\nunreached survivors by file:")
        _by_file(split.unreached)
    if args.list:
        print("\nunreached survivors:")
        for row in sorted(split.unreached, key=lambda r: (r.path, r.line)):
            print(f"  {row.label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
