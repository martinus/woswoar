"""End-to-end sync: two machines exchanging history through a bare repo.

These drive the real ``age`` and the real ``git``. Mocking either would test the
mock -- the whole premise of sync is that ordinary git plus ordinary age
are enough, and that is only meaningful if it is actually demonstrated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from woswoar import cache, crypto, search, store, sync
from woswoar.entry import Entry, format_line

from . import support

requires_age = unittest.skipUnless(crypto.available(), "age required")
requires_git = unittest.skipUnless(shutil.which("git"), "git required")

#: One authoritative list, plus HOME which only these tests need to redirect.
_ENV = (*support.ENV_KEYS, "HOME")


class Fake:
    """One simulated machine with its own config, logs, and clone."""

    def __init__(self, root: Path, name: str) -> None:
        self.root = root / name
        self.name = name
        self._id: str | None = None
        for sub in ("data", "conf", "cache"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    @property
    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.root),
            "WOSWOAR_DIR": str(self.root / "data"),
            "XDG_CONFIG_HOME": str(self.root / "conf"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
        }

    @contextmanager
    def active(self) -> Iterator[Fake]:
        """Run a block as if we were sitting at this machine."""
        saved = {k: os.environ.get(k) for k in _ENV}
        os.environ.pop("WOSWOAR_SESSION", None)
        os.environ.update(self.env)
        try:
            yield self
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # -- convenience, all assuming `active()` is held ----------------------

    @property
    def id(self) -> str:
        """This machine's id, resolved once and remembered.

        Deliberately cached: store.machine() reads the *ambient* environment, so
        a live lookup would quietly return the wrong machine's id whenever one
        Fake is inspected while another is active -- which is most assertions in
        a two-machine test.
        """
        if self._id is None:
            self._id = store.machine().id
        return self._id

    def record(self, day: str, ts: int, cmd: str, session: str = "s1") -> None:
        """Append a line the way the shell hook would."""
        path = store.log_file(self.id, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = Entry(ts, self.id, session, "~/src", 0, 5, cmd)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(format_line(entry) + "\n")

    def commands(self) -> set[str]:
        return {e.cmd for e in cache.load_entries()}


@requires_age
@requires_git
class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="woswoar-sync-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.origin = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--quiet", "--bare", str(self.origin)], check=True, timeout=60
        )

    def machine(self, name: str, enrol: bool = True, display: str | None = None) -> Fake:
        fake = Fake(self.root, name)
        if enrol:
            with fake.active():
                if display:
                    # Set before init: name.age is sealed during enrolment.
                    store.save_machine(store.machine()._replace(name=display))
                # A dedicated identity, not the developer's real SSH key.
                sync.initialise(remote=str(self.origin), new_identity=True)
                _ = fake.id  # resolve while this machine's env is in effect
        return fake

    def git_in_repo(self, fake: Fake, *args: str) -> str:
        with fake.active():
            return sync.git(*args)


class TestSingleMachine(SyncTestCase):
    def test_export_seals_and_pushes(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            report = sync.run()
            self.assertEqual(report.chunks_written, 1)
            self.assertEqual(report.lines_exported, 1)
            self.assertTrue(report.pushed)

    def test_nothing_new_writes_no_chunk(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            again = sync.run()
            self.assertEqual(again.chunks_written, 0)
            self.assertEqual(again.lines_exported, 0)

    def test_nothing_readable_leaks_into_the_repo(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "sudo rm -rf /very-secret-path")
            sync.run()
            blob = b"".join(p.read_bytes() for p in store.history_dir().rglob("*") if p.is_file())
        self.assertNotIn(b"very-secret-path", blob)
        self.assertNotIn(b"sudo", blob)

    def test_partial_final_line_is_not_sealed_until_complete(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            path = store.log_file(alpha.id, "2023-11-14")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("1700000001\ts1\t~/src\t0\t5\tcomplete\n1700000002\ts1", "utf-8")

            sync.run()
            state = sync.State.load()
            relpath = f"hosts/{alpha.id}/2023-11-14.tsv"
            # The watermark stops at the last newline, so the half-written record
            # is left for next time rather than sealed into an immutable chunk.
            self.assertEqual(state.exported[relpath], path.stat().st_size - len("1700000002\ts1"))


class TestTwoMachines(SyncTestCase):
    def test_history_reaches_the_other_machine(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            alpha.record("2023-11-14", 1_700_000_002, "make -j8")
            sync.run()

        beta = self.machine("beta")
        with beta.active():
            sync.run()
        # beta joined after alpha sealed those lines, so it cannot read them yet.
        # One command on an already-enrolled machine is the whole fix, exactly
        # as documented -- no surrounding sync to fetch beta's key or publish.
        with alpha.active():
            sync.reencrypt()
        with beta.active():
            report = sync.run()
            self.assertEqual(report.chunks_merged, 1)
            self.assertEqual(beta.commands(), {"git status", "make -j8"})

    def test_reencrypt_alone_grants_access_without_a_surrounding_sync(self) -> None:
        """The recipient list is a file in the working tree.

        Re-sealing against a checkout taken before the new machine enrolled
        rewrites every key to the *old* recipients and reports full success,
        while granting nothing. So `reencrypt` has to fetch first, and this
        pins that it does -- alpha never syncs after beta joins.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        beta = self.machine("beta")  # init publishes beta's public key
        with alpha.active():
            report = sync.reencrypt()
            self.assertTrue(report.pushed)
            self.assertEqual(report.skipped, 0)
            self.assertGreater(report.resealed, 0)

        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"git status"})

    def test_a_new_machine_cannot_reencrypt_for_itself(self) -> None:
        """Re-sealing means opening the key first, which beta cannot do.

        This is the property the encryption rests on, not a missing feature:
        a machine nobody granted access to must not be able to grant itself
        access. It must say so rather than report a successful no-op.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        beta = self.machine("beta")
        with beta.active():
            sync.run()
            report = sync.reencrypt()
            # It re-seals what it owns -- its own name seal -- and skips
            # alpha's day key, so it still cannot read alpha's history.
            self.assertGreater(report.skipped, 0)
            self.assertEqual(sync.run().unreadable, {f"{alpha.id}/2023-11-14"})
            self.assertEqual(beta.commands(), set())

    def test_new_machine_reports_rather_than_failing(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        beta = self.machine("beta")
        with beta.active():
            beta.record("2023-11-15", 1_700_100_000, "beta's own command")
            report = sync.run()
            # Unreadable history must not stop this machine exporting its own.
            self.assertTrue(report.unreadable)
            self.assertEqual(report.lines_exported, 1)

    def test_bidirectional(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "from alpha")
            sync.run()
            sync.reencrypt()
        with beta.active():
            beta.record("2023-11-14", 1_700_000_050, "from beta")
            sync.run()
        with alpha.active():
            sync.run()
            self.assertEqual(alpha.commands(), {"from alpha", "from beta"})
        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"from alpha", "from beta"})

    def test_merging_is_idempotent(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            sync.reencrypt()
        with beta.active():
            sync.run()
            first = len(cache.load_entries())
            for _ in range(3):
                sync.run()
            self.assertEqual(len(cache.load_entries()), first, "re-sync duplicated entries")

    def test_offline_backlog_spanning_days(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            sync.run()
            sync.reencrypt()
        with beta.active():
            sync.run()

        with alpha.active():
            for day, ts in (("2023-11-14", 1_700_000_001), ("2023-11-15", 1_700_100_000)):
                alpha.record(day, ts, f"command on {day}")
            # One sync after several days offline: a chunk per day, because a
            # chunk always belongs to exactly one day file.
            report = sync.run()
            self.assertEqual(report.chunks_written, 2)

        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"command on 2023-11-14", "command on 2023-11-15"})

    def test_other_machines_name_is_learned(self) -> None:
        alpha = self.machine("alpha", display="alpha@laptop")
        beta = self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            sync.reencrypt()
        with beta.active():
            sync.run()
            # Without this, search would label alpha's entries with its opaque id.
            self.assertEqual(store.host_names().get(alpha.id), "alpha@laptop")

    def test_search_spans_hosts_but_host_scope_does_not(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "from alpha")
            sync.run()
            sync.reencrypt()
        with beta.active():
            beta.record("2023-11-14", 1_700_000_050, "from beta")
            sync.run()
            entries = cache.load_entries()
            self.assertEqual(len(search.filter_scope(entries, "global")), 2)
            self.assertEqual([e.cmd for e in search.filter_scope(entries, "host")], ["from beta"])


class TestImmutability(SyncTestCase):
    """The invariant the whole storage design rests on."""

    def test_no_chunk_is_ever_modified_or_deleted(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")

        with alpha.active():
            for i in range(4):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"alpha {i}")
                sync.run()
            sync.reencrypt()
        with beta.active():
            for i in range(3):
                beta.record("2023-11-15", 1_700_100_000 + i, f"beta {i}")
                sync.run()
        with alpha.active():
            sync.run()

        # Exact, not a heuristic: if any commit ever modified or deleted a
        # chunk, this lists it. An empty result is the guarantee that repo
        # growth equals the bytes written.
        touched = self.git_in_repo(
            alpha,
            "log",
            "--all",
            "--diff-filter=MD",
            "--name-only",
            "--pretty=format:",
            "--",
            "hosts",
        )
        rewritten = {line for line in touched.splitlines() if line.endswith(".age")}

        # Chunks live under hosts/<id>/YYYY/MM/DD/. Key files and name seals sit
        # elsewhere in the tree and *are* rewritten, by design: that is exactly
        # what makes onboarding cheap. Separating the two here keeps the
        # exception explicit rather than quietly widening the invariant.
        # Classified by store, not by a regex copied here: a hand-maintained
        # pattern would silently stop matching if the layout ever changed,
        # leaving this assertion vacuously true while real chunks were rewritten.
        chunks = {p for p in rewritten if store.is_chunk_path(p)}
        self.assertEqual(sorted(chunks), [], f"chunks were rewritten: {sorted(chunks)}")

        # And the only things that were rewritten are the ones allowed to be.
        self.assertTrue(
            all(("/keys/" in p or p.endswith("/name.age")) for p in rewritten),
            f"unexpected rewrites: {sorted(rewritten - chunks)}",
        )

    def test_repo_growth_tracks_bytes_written_not_sync_count(self) -> None:
        syncs = 20
        alpha = self.machine("alpha")
        with alpha.active():
            for i in range(syncs):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command number {i}")
                sync.run()

            self.git_in_repo(alpha, "gc", "--quiet")
            # git's own reported object size. Summing files under .git would
            # instead measure the sample hooks, which dwarf a repo this small.
            counts = dict(
                line.split(": ", 1)
                for line in self.git_in_repo(alpha, "count-objects", "-v").splitlines()
                if ": " in line
            )
            stored = int(counts["size-pack"]) * 1024 + int(counts["size"]) * 1024
            chunk_bytes = sum(c.path.stat().st_size for c in store.iter_chunks(alpha.id))
            chunks = len(list(store.iter_chunks(alpha.id)))

        self.assertEqual(chunks, syncs, "each sync must produce exactly one chunk")
        # Chunks are written once and never rewritten, so git stores each
        # exactly once: total objects stay close to the bytes actually written.
        # Re-encrypting the whole day file each sync would instead store
        # 1+2+...+N copies and blow straight past this.
        self.assertLess(
            stored,
            chunk_bytes * 4 + 64 * 1024,
            f"stored={stored} chunk_bytes={chunk_bytes} over {syncs} syncs",
        )


class TestCompact(SyncTestCase):
    def test_compact_merges_chunks_and_preserves_content(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            for i in range(5):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command {i}")
                sync.run()
            before = len(list(store.iter_chunks(alpha.id)))
            self.assertEqual(before, 5)

            days, replaced = sync.compact(before="2023-11-15")
            self.assertEqual((days, replaced), (1, 5))
            self.assertEqual(len(list(store.iter_chunks(alpha.id))), 1)

    def test_compacted_chunk_still_reaches_another_machine(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            for i in range(3):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command {i}")
                sync.run()
            sync.compact(before="2023-11-15")
            sync.reencrypt()
        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"command 0", "command 1", "command 2"})


class TestIdentityStatus(SyncTestCase):
    """The health check `doctor` reports. Testable because it lives in sync."""

    def test_healthy_identity(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            status = sync.identity_status(store.machine())
            self.assertTrue(status.ok, status.detail)

    def test_missing_identity_file_is_reported(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            known = store.machine()
            Path(known.identity).unlink()
            status = sync.identity_status(known)
            self.assertFalse(status.ok)
            self.assertIn("missing", status.detail)

    def test_unconfigured_identity_is_reported(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            status = sync.identity_status(store.machine()._replace(identity=""))
            self.assertFalse(status.ok)
            self.assertIn("woswoar init", status.detail)

    def test_passphrase_protected_key_is_reported(self) -> None:
        # The failure that only bites unattended: age cannot use ssh-agent, so
        # this key would work by hand and never from a systemd timer.
        alpha = self.machine("alpha")
        with alpha.active():
            locked = self.root / "locked"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "hunter2", "-f", str(locked)],
                check=True,
                timeout=60,
            )
            status = sync.identity_status(store.machine()._replace(identity=str(locked)))
            self.assertFalse(status.ok)
            self.assertIn("passphrase", status.detail)


class TestLocalOnly(SyncTestCase):
    def test_sync_without_a_remote_still_seals(self) -> None:
        alpha = Fake(self.root, "solo")
        with alpha.active():
            sync.initialise(new_identity=True)
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            report = sync.run()
            self.assertEqual(report.chunks_written, 1)
            self.assertFalse(report.pushed)

    def test_concurrent_sync_is_refused_not_corrupted(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active(), sync.lock(), self.assertRaises(sync.SyncError):
            sync.run()


if __name__ == "__main__":
    unittest.main()
