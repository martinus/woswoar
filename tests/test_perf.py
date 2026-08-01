"""Latency regression test.

Ctrl-R must feel instant, which makes cache load time a hard requirement rather
than a nice-to-have. The sizes here match the design document's real-world
figure: roughly 52,000 commands over two years.

Opt-in, because generating that much data takes a few seconds:

    WOSWOAR_BENCH=1 python -m unittest tests.test_perf
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from woswoar import cache, search, store
from woswoar.entry import Entry, format_line

from .support import MACHINE_ID, WoswoarTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent

ENTRIES = 52_000
DAYS = 730

#: Targets from the plan. CI fails at 3x to stay honest on a shared runner
#: while still catching a real regression -- a change that makes loading twice
#: as slow will not slip through, but a noisy neighbour will not fail the build.

#: The full parse of every log file, with no cache to start from -- paid on the
#: first Ctrl-R after an install, an import, or a cache the user deleted. It had
#: no bound at all: `warm < cold` holds however slow the cold path becomes.
#:
#: What this catches is a gross regression, at 3x like everything else here. It
#: would *not* have caught the one that prompted it -- #25 cost 22% of cold
#: build, 75 ms to 92 ms -- and no wall-clock bound safe on a shared runner
#: would: cold build reads ~750 files, and five consecutive local runs on an
#: otherwise idle machine spread 80-94 ms by themselves. A ratio against the
#: warm load in the same run is no steadier (2.24-2.66 over those five). So this
#: is a floor under "somebody made it several times slower", and a 20% one still
#: needs a person measuring, as #25's did.
COLD_BUILD_TARGET_MS = 150.0
CACHE_LOAD_TARGET_MS = 50.0
LIST_TARGET_MS = 100.0
#: The whole process, not just the functions. About double LIST_TARGET_MS,
#: because a keypress also pays for an interpreter, the imports, and argparse.
END_TO_END_TARGET_MS = 200.0
TOLERANCE = 3.0

COMMANDS = [
    "git status",
    "git rebase -i origin/main",
    "ninja -C build",
    "cd ~/src/woswoar",
    "rg --hidden --glob '!.git' TODO",
    "python -m unittest discover",
    "docker compose up -d --build",
    "ssh martin@laptop 'systemctl --user status'",
]


@unittest.skipUnless(os.environ.get("WOSWOAR_BENCH"), "set WOSWOAR_BENCH=1 to run benchmarks")
class TestPerformance(WoswoarTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.generate()

    def generate(self) -> None:
        """Write ENTRIES commands spread over DAYS daily files."""
        per_day = ENTRIES // DAYS
        start = 1_700_000_000
        root = store.logs_dir() / "hosts" / MACHINE_ID
        root.mkdir(parents=True, exist_ok=True)

        for day in range(DAYS):
            ts = start + day * 86400
            lines = []
            for i in range(per_day):
                cmd = COMMANDS[i % len(COMMANDS)]
                # Vary a fraction of commands so deduplication has realistic
                # work to do instead of collapsing everything to eight lines.
                if i % 3 == 0:
                    cmd = f"{cmd} # {day}-{i}"
                lines.append(
                    format_line(Entry(ts + i, MACHINE_ID, f"sess{day}", "/src/woswoar", 0, 12, cmd))
                )
            name = time.strftime("%Y-%m-%d", time.localtime(ts))
            (root / f"{name}.tsv").write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")

    @staticmethod
    def timed(fn: object) -> tuple[float, object]:
        started = time.perf_counter()
        result = fn()  # type: ignore[operator]
        return (time.perf_counter() - started) * 1000, result

    def append_one_command(self) -> None:
        """Simulate the shell hook recording a single command."""
        newest = sorted((store.logs_dir() / "hosts" / MACHINE_ID).glob("*.tsv"))[-1]
        entry = Entry(2_000_000_000, MACHINE_ID, "live", "/src/woswoar", 0, 7, "git push")
        with newest.open("a", encoding="utf-8") as handle:
            handle.write(format_line(entry) + "\n")

    def test_latency(self) -> None:
        cold_ms, entries = self.timed(cache.load_entries)
        count = len(entries)  # type: ignore[arg-type]
        self.assertGreater(count, ENTRIES * 0.9, "generator produced too few entries")

        warm_ms, _ = self.timed(cache.load_entries)
        list_ms, lines = self.timed(lambda: search.lines_for("global"))

        # The case that actually matters. You always type a command before you
        # press Ctrl-R, so "nothing changed since last time" never happens in
        # real use. Measuring only the unchanged path once hid a full cache
        # rewrite -- ~48 ms -- on every single search.
        self.append_one_command()
        after_ms, _ = self.timed(lambda: search.lines_for("global"))

        print(
            f"\n  entries       {count}"
            f"\n  cold build    {cold_ms:8.1f} ms  (full parse, no cache)"
            f"\n  warm load     {warm_ms:8.1f} ms  (target {CACHE_LOAD_TARGET_MS:.0f} ms)"
            f"\n  list global   {list_ms:8.1f} ms  (target {LIST_TARGET_MS:.0f} ms,"
            f" {len(lines)} lines)"  # type: ignore[arg-type]
            f"\n  list after 1  {after_ms:8.1f} ms  (target {LIST_TARGET_MS:.0f} ms,"
            " the real Ctrl-R case)"
        )

        self.assertLess(
            warm_ms,
            CACHE_LOAD_TARGET_MS * TOLERANCE,
            f"cache load regressed: {warm_ms:.1f} ms",
        )
        self.assertLess(
            list_ms,
            LIST_TARGET_MS * TOLERANCE,
            f"list regressed: {list_ms:.1f} ms",
        )
        self.assertLess(
            after_ms,
            LIST_TARGET_MS * TOLERANCE,
            f"list after a new command regressed: {after_ms:.1f} ms",
        )

    def test_end_to_end_latency(self) -> None:
        """The whole process, which is what a keypress actually pays for.

        Everything else here measures functions in a warm interpreter. That
        misses a fresh interpreter start, the imports, and building the argparse
        parser -- together about half the real cost, which is precisely the half
        an in-process benchmark can never see.
        """
        cache.load_entries()  # warm the cache, as any real shell would have

        argv = [sys.executable, "-m", "woswoar", "list", "--scope", "global"]
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        run = subprocess.run(argv, capture_output=True, env=env, check=True, timeout=120)
        lines = run.stdout.count(b"\n")

        # capture_output on the timed runs too: letting 1.5 MB reach the test
        # runner's own stdout measures the terminal, not woswoar.
        runs = sorted(
            self.timed(lambda: subprocess.run(argv, capture_output=True, env=env, timeout=120))[0]
            for _ in range(5)
        )
        median = runs[len(runs) // 2]

        print(f"\n  Ctrl-R end to end {median:8.1f} ms  ({lines} lines, whole process)")
        self.assertGreater(lines, 0)
        self.assertLess(
            median,
            END_TO_END_TARGET_MS * TOLERANCE,
            f"end-to-end search regressed: {median:.1f} ms",
        )

    def test_a_single_new_command_does_not_rewrite_the_cache(self) -> None:
        cache.load_entries()
        before = store.cache_file().stat().st_mtime_ns

        self.append_one_command()
        cache.load_entries()

        # One new line is nowhere near the save threshold, so the cache must be
        # left alone; rewriting it here is what put ~48 ms on every search.
        self.assertEqual(store.cache_file().stat().st_mtime_ns, before)

    def test_warm_load_beats_cold_build(self) -> None:
        cold_ms, _ = self.timed(cache.load_entries)
        warm_ms, _ = self.timed(cache.load_entries)
        print(f"\n  cold build    {cold_ms:8.1f} ms  (target {COLD_BUILD_TARGET_MS:.0f} ms)")

        # If this ever fails, the incremental path is not being taken at all.
        self.assertLess(warm_ms, cold_ms)
        # And a bound on the cold path itself, which the comparison above cannot
        # give: it stays true however slow both become.
        self.assertLess(
            cold_ms,
            COLD_BUILD_TARGET_MS * TOLERANCE,
            f"cold cache build regressed: {cold_ms:.1f} ms",
        )


if __name__ == "__main__":
    unittest.main()
