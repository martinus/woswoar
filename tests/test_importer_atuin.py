"""Importing atuin's sqlite history.

The fixture mirrors the schema and the quirks of a real atuin database
(18.16.1): nanosecond timestamps, ``-1`` for a duration it never saw finish,
the literal string ``unknown`` for an unknown directory, and a ``host:user``
hostname carrying history from every machine atuin has synced with.
"""

from __future__ import annotations

import os
import sqlite3
import unittest
from collections.abc import Sequence
from pathlib import Path

from woswoar import cache, importer, search, store
from woswoar.entry import MAX_CMD_CHARS

from .support import WoswoarTestCase

SCHEMA = """
CREATE TABLE history (
    id text primary key,
    timestamp integer not null,
    duration integer not null,
    exit integer not null,
    command text not null,
    cwd text not null,
    session text not null,
    hostname text not null,
    deleted_at integer,
    author text,
    intent text,
    unique(timestamp, cwd, command)
)
"""

SECOND = 1_000_000_000
BASE = 1_700_000_000 * SECOND


class AtuinTestCase(WoswoarTestCase):
    def atuin(self, rows: Sequence[tuple[object, ...]], name: str = "history.db") -> Path:
        """Build an atuin database. Rows are (ts_ns, dur_ns, exit, cmd, cwd, session, host)."""
        path = self.root / name
        db = sqlite3.connect(path)
        db.execute(SCHEMA)
        db.executemany(
            "INSERT INTO history (id, timestamp, duration, exit, command, cwd, session, hostname)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(f"id{i}", *row) for i, row in enumerate(rows)],
        )
        db.commit()
        db.close()
        return path

    def this_machine(self) -> str:
        """The `host:user` atuin would record on the machine running the tests."""
        user, _, host = store.default_machine_name().partition("@")
        return f"{host}:{user}"


class TestConversion(AtuinTestCase):
    def test_nanosecond_timestamp_becomes_seconds(self) -> None:
        db = self.atuin([(BASE, 5 * SECOND, 0, "git status", "/tmp", "s1", "box:someone")])
        importer.run("atuin", db)
        entry = cache.load_entries()[0]
        self.assertEqual(entry.ts, 1_700_000_000)

    def test_nanosecond_duration_becomes_milliseconds(self) -> None:
        db = self.atuin([(BASE, 1_500_000_000, 0, "sleep 1.5", "/tmp", "s1", "box:someone")])
        importer.run("atuin", db)
        self.assertEqual(cache.load_entries()[0].duration_ms, 1500)

    def test_unfinished_command_keeps_unknown_duration(self) -> None:
        # atuin writes -1 when it never saw the command finish. woswoar uses the
        # same convention, so it must survive rather than become -1000 ms.
        db = self.atuin([(BASE, -1, 0, "vim", "/tmp", "s1", "box:someone")])
        importer.run("atuin", db)
        self.assertEqual(cache.load_entries()[0].duration_ms, -1)

    def test_unknown_cwd_becomes_empty(self) -> None:
        db = self.atuin([(BASE, 0, 0, "ls", "unknown", "s1", "box:someone")])
        importer.run("atuin", db)
        self.assertEqual(cache.load_entries()[0].cwd, "")

    def test_exit_code_is_preserved(self) -> None:
        db = self.atuin([(BASE, 0, 130, "sleep 99", "/tmp", "s1", "box:someone")])
        importer.run("atuin", db)
        self.assertEqual(cache.load_entries()[0].exit_code, 130)

    def test_commands_with_newlines_and_tabs_survive(self) -> None:
        tricky = "for i in 1 2; do\n\techo $i\ndone"
        db = self.atuin([(BASE, 0, 0, tricky, "/tmp", "s1", "box:someone")])
        importer.run("atuin", db)
        self.assertEqual(cache.load_entries()[0].cmd, tricky)

    def test_session_is_shortened_but_stays_distinct(self) -> None:
        db = self.atuin(
            [
                (BASE, 0, 0, "a", "/tmp", "0191f0e6-1111-7000-8000-000000000001", "box:someone"),
                (BASE + SECOND, 0, 0, "b", "/tmp", "0191f0e6-2222-7000-8000-000000000002", "b:s"),
            ]
        )
        importer.run("atuin", db)
        sessions = {e.session for e in cache.load_entries()}
        self.assertEqual(len(sessions), 2)
        # Repeated on every line, so it is hashed to the same width the hook writes.
        self.assertTrue(all(len(s) == 14 for s in sessions), sessions)

    def test_deleted_rows_are_not_imported(self) -> None:
        db = self.atuin([(BASE, 0, 0, "kept", "/tmp", "s1", "box:someone")])
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO history (id, timestamp, duration, exit, command, cwd, session,"
            " hostname, deleted_at) VALUES ('x', ?, 0, 0, 'forgotten', '/tmp', 's1',"
            " 'box:someone', 12345)",
            (BASE + SECOND,),
        )
        conn.commit()
        conn.close()

        importer.run("atuin", db)
        self.assertEqual({e.cmd for e in cache.load_entries()}, {"kept"})

    def test_empty_commands_are_skipped(self) -> None:
        db = self.atuin(
            [
                (BASE, 0, 0, "   ", "/tmp", "s1", "box:someone"),
                (BASE + SECOND, 0, 0, "real", "/tmp", "s1", "box:someone"),
            ]
        )
        importer.run("atuin", db)
        self.assertEqual({e.cmd for e in cache.load_entries()}, {"real"})


class TestHostAttribution(AtuinTestCase):
    def test_each_machine_gets_its_own_host(self) -> None:
        db = self.atuin(
            [
                (BASE, 0, 0, "on laptop", "/tmp", "s1", "laptop:martin"),
                (BASE + SECOND, 0, 0, "on desktop", "/tmp", "s2", "desktop:martin"),
            ]
        )
        importer.run("atuin", db)
        # Flattening these into one host would make --scope host and stats lie.
        hosts = {e.host for e in cache.load_entries()}
        self.assertEqual(len(hosts), 2)

    def test_machine_names_are_recorded_for_display(self) -> None:
        db = self.atuin([(BASE, 0, 0, "cmd", "/tmp", "s1", "laptop:martin")])
        importer.run("atuin", db)
        self.assertIn("martin@laptop", store.host_names().values())

    def test_host_ids_are_stable_across_imports(self) -> None:
        db = self.atuin([(BASE, 0, 0, "cmd", "/tmp", "s1", "laptop:martin")])
        importer.run("atuin", db)
        first = {e.host for e in cache.load_entries()}

        other = self.atuin([(BASE + SECOND, 0, 0, "later", "/tmp", "s1", "laptop:martin")], "b.db")
        importer.run("atuin", other)
        # Derived from the name, not random, so a second import lands in the
        # same host directory instead of forking a duplicate machine.
        self.assertEqual({e.host for e in cache.load_entries()}, first)

    def test_this_machines_history_merges_into_its_own_host(self) -> None:
        db = self.atuin([(BASE, 0, 0, "mine", "/tmp", "s1", self.this_machine())])
        importer.run("atuin", db)
        entry = next(e for e in cache.load_entries() if e.cmd == "mine")
        # So it sits with what the hook records, and syncs normally.
        self.assertEqual(entry.host, store.machine().id)

    def test_host_scope_separates_imported_machines(self) -> None:
        db = self.atuin(
            [
                (BASE, 0, 0, "mine", "/tmp", "s1", self.this_machine()),
                (BASE + SECOND, 0, 0, "theirs", "/tmp", "s2", "elsewhere:someone"),
            ]
        )
        importer.run("atuin", db)
        entries = cache.load_entries()
        self.assertEqual(len(search.filter_scope(entries, "global")), 2)
        self.assertEqual([e.cmd for e in search.filter_scope(entries, "host")], ["mine"])

    def test_own_paths_are_stored_home_relative(self) -> None:
        home = str(Path.home())
        db = self.atuin(
            [
                (BASE, 0, 0, "mine", f"{home}/src", "s1", self.this_machine()),
                (BASE + SECOND, 0, 0, "theirs", "/home/other/src", "s2", "elsewhere:other"),
            ]
        )
        importer.run("atuin", db)
        by_cmd = {e.cmd: e for e in cache.load_entries()}
        self.assertEqual(by_cmd["mine"].cwd, "~/src")
        # Another machine's home is a guess; a wrong ~ is worse than a long path.
        self.assertEqual(by_cmd["theirs"].cwd, "/home/other/src")

    def test_hostname_without_a_username(self) -> None:
        db = self.atuin([(BASE, 0, 0, "cmd", "/tmp", "s1", "plainhost")])
        importer.run("atuin", db)
        self.assertIn("plainhost", store.host_names().values())


class TestIdempotency(AtuinTestCase):
    def test_reimport_adds_nothing(self) -> None:
        db = self.atuin(
            [
                (BASE, 0, 0, "one", "/tmp", "s1", "box:someone"),
                (BASE + SECOND, 0, 0, "two", "/tmp", "s1", "box:someone"),
            ]
        )
        first = importer.run("atuin", db)
        again = importer.run("atuin", db)

        self.assertEqual(first.imported, 2)
        self.assertEqual(again.imported, 0)
        self.assertEqual(again.skipped, 2)
        self.assertEqual(len(cache.load_entries()), 2)

    def test_reimport_picks_up_backfilled_older_rows(self) -> None:
        # atuin backfills *older* entries whenever it syncs from another
        # machine, so a "everything after the last row I saw" watermark would
        # silently miss them. Dedup is by (timestamp, command) for that reason.
        db = self.atuin([(BASE + 10 * SECOND, 0, 0, "newer", "/tmp", "s1", "box:someone")])
        importer.run("atuin", db)

        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO history (id, timestamp, duration, exit, command, cwd, session,"
            " hostname) VALUES ('back', ?, 0, 0, 'older', '/tmp', 's1', 'box:someone')",
            (BASE,),
        )
        conn.commit()
        conn.close()

        result = importer.run("atuin", db)
        self.assertEqual(result.imported, 1)
        self.assertEqual({e.cmd for e in cache.load_entries()}, {"newer", "older"})

    def test_overlong_command_is_not_reimported_forever(self) -> None:
        # Regression, found against a real 54,943-row atuin database: commands
        # past MAX_CMD_CHARS are truncated on write, so a dedup key built from
        # the original text never matched what was stored, and those entries
        # were re-appended on every single import.
        huge = "echo " + "x" * (MAX_CMD_CHARS + 5000)
        db = self.atuin(
            [
                (BASE, 0, 0, huge, "/tmp", "s1", "box:someone"),
                (BASE + SECOND, 0, 0, "normal", "/tmp", "s1", "box:someone"),
            ]
        )
        self.assertEqual(importer.run("atuin", db).imported, 2)
        self.assertEqual(importer.run("atuin", db).imported, 0)
        self.assertEqual(len(cache.load_entries()), 2)

    def test_dry_run_writes_no_history(self) -> None:
        db = self.atuin([(BASE, 0, 0, "cmd", "/tmp", "s1", "box:someone")])
        result = importer.run("atuin", db, dry_run=True)
        self.assertEqual(result.imported, 1)
        self.assertEqual(cache.load_entries(), [])

    def test_per_host_counts_are_reported(self) -> None:
        db = self.atuin(
            [
                (BASE, 0, 0, "a", "/tmp", "s1", "laptop:martin"),
                (BASE + SECOND, 0, 0, "b", "/tmp", "s1", "laptop:martin"),
                (BASE + 2 * SECOND, 0, 0, "c", "/tmp", "s2", "desktop:martin"),
            ]
        )
        result = importer.run("atuin", db)
        self.assertEqual(dict(result.per_host), {"martin@laptop": 2, "martin@desktop": 1})
        # Biggest first, so the summary reads usefully with nine machines.
        self.assertEqual([n for n, _ in result.per_host], ["martin@laptop", "martin@desktop"])


class TestReviewRegressions(AtuinTestCase):
    """Each of these was a real defect found by review, not a hypothetical."""

    def test_fqdn_hostname_is_still_recognised_as_this_machine(self) -> None:
        # default_machine_name() uses only the first nodename component, so
        # comparing atuin's raw hostname filed one's *own* history under a
        # foreign host: invisible to --scope host, never published by sync.
        user, _, host = store.default_machine_name().partition("@")
        db = self.atuin([(BASE, 0, 0, "mine", "/tmp", "s1", f"{host}.example.com:{user}")])
        importer.run("atuin", db)
        self.assertEqual(cache.load_entries()[0].host, store.machine().id)

    def test_fqdn_and_short_hostname_are_the_same_machine(self) -> None:
        db = self.atuin(
            [
                (BASE, 0, 0, "a", "/tmp", "s1", "laptop.lan:martin"),
                (BASE + SECOND, 0, 0, "b", "/tmp", "s1", "laptop:martin"),
            ]
        )
        importer.run("atuin", db)
        self.assertEqual(
            len(
                {e.host for e in cache.load_entries()},
            ),
            1,
        )

    def test_a_peer_already_known_locally_is_not_duplicated(self) -> None:
        # Once a peer's history syncs in it has a real, random host id. A
        # derived id could never equal it, so the same machine would exist
        # twice under two directories sharing one label.
        peer_id = "aaaabbbbccccdddd"
        store.name_file(peer_id).parent.mkdir(parents=True, exist_ok=True)
        store.name_file(peer_id).write_text("martin@laptop\n", encoding="utf-8")

        db = self.atuin([(BASE, 0, 0, "cmd", "/tmp", "s1", "laptop:martin")])
        importer.run("atuin", db)
        self.assertEqual(cache.load_entries()[0].host, peer_id)

    def test_undecodable_command_does_not_abort_the_import(self) -> None:
        db = self.atuin([(BASE + SECOND, 0, 0, "fine", "/tmp", "s1", "box:someone")])
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO history (id, timestamp, duration, exit, command, cwd, session,"
            " hostname) VALUES ('bad', ?, 0, 0, CAST(x'6c73ff' AS TEXT), '/tmp', 's1',"
            " 'box:someone')",
            (BASE,),
        )
        conn.commit()
        conn.close()

        # Previously raised sqlite3.OperationalError straight out of the
        # generator, past the CLI's handler, killing the whole import.
        result = importer.run("atuin", db)
        self.assertEqual(result.imported, 2)
        self.assertIn("fine", {e.cmd for e in cache.load_entries()})

    def test_path_with_uri_metacharacters_is_read_not_created(self) -> None:
        # A '#' truncated the spliced URI, dropping mode=ro -- so sqlite
        # cheerfully created a new writable database at the truncated name,
        # the exact opposite of the read-only guarantee.
        db = self.atuin([(BASE, 0, 0, "cmd", "/tmp", "s1", "box:someone")], "od#d?b%1.db")
        before = sorted(p.name for p in self.root.iterdir() if p.is_file())

        result = importer.run("atuin", db)
        self.assertEqual(result.imported, 1, "the real database was not read")
        # A truncated URI made sqlite create a *new* writable database at the
        # cut-off name; only woswoar's own directories may appear.
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()), before)

    def test_first_import_into_an_empty_store_reports_no_false_skips(self) -> None:
        # Same second, same command, different directory: one row survives
        # woswoar's whole-second resolution. Counting that as "already
        # present" made a first-ever import claim entries it had never seen.
        db = self.atuin(
            [
                (BASE, 0, 0, "make", "/a", "s1", "box:someone"),
                (BASE, 0, 0, "make", "/b", "s1", "box:someone"),
            ]
        )
        result = importer.run("atuin", db)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.collapsed, 1)
        self.assertEqual(result.imported, 1)

    def test_a_missing_name_is_repaired_by_reimporting(self) -> None:
        db = self.atuin([(BASE, 0, 0, "cmd", "/tmp", "s1", "laptop:martin")])
        importer.run("atuin", db)

        host_id = next(h for h, n in store.host_names().items() if n == "martin@laptop")
        store.name_file(host_id).unlink()

        # The second run imports nothing, so a name write gated on fresh
        # entries would leave this host labelled by its opaque id forever.
        importer.run("atuin", db)
        self.assertEqual(store.host_names().get(host_id), "martin@laptop")

    def test_host_resolution_does_not_scale_with_row_count(self) -> None:
        many = [
            (BASE + i * SECOND, 0, 0, f"cmd {i}", "/tmp", "s1", f"host{i % 5}:user")
            for i in range(400)
        ]
        db = self.atuin(many)
        calls = 0
        real = store.default_machine_name

        def counted() -> str:
            nonlocal calls
            calls += 1
            return real()

        store.default_machine_name = counted
        self.addCleanup(lambda: setattr(store, "default_machine_name", real))
        importer.run("atuin", db)

        # Once for the whole import, not once per row: this was ~60% of runtime.
        self.assertLessEqual(calls, 2, f"resolved host identity {calls} times for 400 rows")


class TestThisHostOnly(AtuinTestCase):
    def test_only_this_machines_history_is_imported(self) -> None:
        db = self.atuin(
            [
                (BASE, 0, 0, "mine", "/tmp", "s1", self.this_machine()),
                (BASE + SECOND, 0, 0, "theirs", "/tmp", "s2", "elsewhere:someone"),
            ]
        )
        result = importer.run("atuin", db, this_host_only=True)
        self.assertEqual(result.imported, 1)
        self.assertEqual({e.cmd for e in cache.load_entries()}, {"mine"})


class TestFailures(AtuinTestCase):
    def test_missing_database_is_reported_clearly(self) -> None:
        with self.assertRaises(FileNotFoundError):
            importer.run("atuin", self.root / "nope.db")

    def test_a_database_that_is_not_atuin_is_reported_clearly(self) -> None:
        path = self.root / "other.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE something (x int)")
        conn.commit()
        conn.close()
        with self.assertRaises(FileNotFoundError):
            importer.run("atuin", path)

    def test_the_database_is_never_written_to(self) -> None:
        db = self.atuin([(BASE, 0, 0, "cmd", "/tmp", "s1", "box:someone")])
        before = db.read_bytes()
        os.chmod(db, 0o444)
        self.addCleanup(os.chmod, db, 0o644)

        importer.run("atuin", db)
        # Opened mode=ro: this is very likely a running atuin's live database.
        self.assertEqual(db.read_bytes(), before)


class TestImportIsPrivate(AtuinTestCase):
    """`import` writes into logs/ too, and used to bypass the store helpers.

    It created a peer's `.name` with a hand-rolled mkdir plus write_text, so
    `woswoar import` followed by `woswoar doctor` reported an exposure that a
    current-version command had just caused -- and pointed the user at a
    migration to fix damage nothing had migrated away from.
    """

    def test_a_peers_name_file_is_owner_only(self) -> None:
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        db = self.atuin([(BASE, 0, 0, "make -j8", "/src", "s1", "elsewhere:someone")])

        importer.run("atuin", db)

        names = list(store.logs_dir().rglob(".name"))
        self.assertTrue(names, "no peer name file was written")
        for path in [*names, *(p.parent for p in names)]:
            self.assertEqual(
                path.stat().st_mode & 0o077, 0, f"{path} is {oct(path.stat().st_mode)}"
            )


if __name__ == "__main__":
    unittest.main()
