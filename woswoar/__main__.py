"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, cache, importer, search, store
from .entry import make_inert
from .errors import WoswoarError

if TYPE_CHECKING:  # `sync` is imported lazily; only the annotation needs it.
    from .sync import Reader

#: Both "no repo key yet" and "some days unreadable" have the same fix, and
#: used to say so in two independently worded paragraphs.
_GRANT_REMEDY = (
    "On a machine that is already enrolled run:\n"
    "    woswoar grant\n"
    "then sync here again. Nothing is lost in the meantime."
)

HOOK_NAME = "woswoar.bash"
_BEGIN = "# >>> woswoar >>>"
_END = "# <<< woswoar <<<"
_BLOCK = re.compile(re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?", re.DOTALL)


def _hook_source() -> Path:
    # Imported here rather than at module scope: importlib.resources costs ~8 ms
    # of startup and only `install` needs it, while every Ctrl-R pays for
    # whatever this module imports eagerly.
    from importlib import resources

    with resources.as_file(resources.files("woswoar") / "shell" / HOOK_NAME) as path:
        return Path(str(path))


def portable_hook_path(target: Path) -> str:
    """The hook's location written so one ``.bashrc`` can serve every machine.

    A literal ``/home/martinus/...`` in the sourced line means the same shared
    ``.bashrc`` has to differ per machine as soon as two of them disagree about
    the username -- which is exactly what a dotfiles repo exists to avoid. The
    part before ``$HOME`` is the only part that varies, so that is the part
    that becomes a variable.

    Anything outside ``$HOME`` is written absolute: there is nothing portable
    to say about it, and guessing would be worse than being explicit.
    """
    home = Path.home()
    try:
        return f"$HOME/{target.relative_to(home).as_posix()}"
    except ValueError:
        return str(target)


def cmd_install(args: argparse.Namespace) -> int:
    """Set up machine identity, install the hook, and wire up .bashrc."""
    import shutil

    machine = store.machine()
    store.private_dir(store.data_dir())
    target = store.data_dir() / HOOK_NAME
    shutil.copyfile(_hook_source(), target)
    # After the copy, not before: `copyfile` creates at the ambient umask, so
    # hardening first would leave the one file install itself writes as the one
    # file its own migration missed. This is also what re-tightens a tree from
    # a woswoar that predated owner-only directories -- `install` is the
    # command people re-run to upgrade.
    store.harden()

    print(f"machine : {machine.name} ({machine.id})")
    print(f"logs    : {store.logs_dir()}")
    print(f"hook    : {target}")

    rcfile = Path(args.rcfile).expanduser() if args.rcfile else Path.home() / ".bashrc"
    block = f'{_BEGIN}\nsource "{portable_hook_path(target)}"\n{_END}\n'

    existing = rcfile.read_text(encoding="utf-8") if rcfile.is_file() else ""
    if _BLOCK.search(existing):
        updated = _BLOCK.sub(block, existing)
        action = "updated"
    else:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        updated = f"{existing}{separator}\n{block}"
        action = "added"

    if updated == existing:
        print(f"rcfile  : {rcfile} (already current)")
    else:
        rcfile.write_text(updated, encoding="utf-8")
        print(f"rcfile  : {rcfile} ({action})")

    print("\nOpen a new shell, or run:  source", target)

    # The moment someone finds out, so the moment to say it. Recording works
    # without any of these, which is why this warns rather than fails -- but
    # without fzf there is no Ctrl-R, and Ctrl-R is what woswoar is for.
    from . import deps

    absent = deps.missing()
    if absent:
        print(f"\n{deps.report(absent)}", file=sys.stderr)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    lines = search.lines_for(args.scope, dedup=not args.no_dedup, limit=args.limit)
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    selection = search.interactive(args.scope, query=args.query, dedup=not args.no_dedup)
    if selection is None:
        return 1
    print(selection)
    return 0


def _restore_remedy(path: str) -> str:
    """How to recover a file a peer deleted from the history repo.

    Deleting one is a commit like any other and woswoar never rewrites history,
    so the blob is still reachable in every clone. Shared by the two files this
    can happen to, so the recipe cannot drift between their messages.
    """
    return (
        "It is still in git history -- woswoar never rewrites it, so every clone\n"
        "has the blob. To put it back:\n"
        f"    git -C <history> log --diff-filter=D -- {path}\n"
        f"    git -C <history> show <commit>^:{path} > that path\n"
    )


def cmd_import(args: argparse.Namespace) -> int:
    try:
        result = importer.run(
            args.kind,
            Path(args.file).expanduser() if args.file else None,
            dry_run=args.dry_run,
            this_host_only=args.this_host_only,
        )
    except FileNotFoundError as exc:
        print(f"woswoar: {exc}", file=sys.stderr)
        return 1
    prefix = "would import" if args.dry_run else "imported"
    notes = []
    if result.skipped:
        notes.append(f"{result.skipped} already present")
    if result.collapsed:
        notes.append(f"{result.collapsed} same-second duplicates collapsed")
    if result.credentials:
        notes.append(f"{result.credentials} skipped as credential-shaped")
    print(f"{result.source}: {result.parsed} parsed, {prefix} {result.imported}", end="")
    print(f", {', '.join(notes)}" if notes else "")

    if result.dropped:
        # Only ever populated by --dry-run. Printed so a false positive can be
        # spotted before it is dropped for real, and to stderr so that piping
        # the summary somewhere does not write a secret to a file.
        print("\nwould skip as credential-shaped:", file=sys.stderr)
        for command in result.dropped:
            print(f"  {make_inert(command)}", file=sys.stderr)

    if len(result.per_host) > 1:
        print("\nper machine:")
        # atuin keeps every machine it has synced with, so these names come from
        # those machines rather than from this one -- same text `stats` prints,
        # same treatment.
        labels = [(make_inert(name), count) for name, count in result.per_host]
        width = max(len(name) for name, _ in labels)
        for name, count in labels:
            print(f"  {name:<{width}}  {count}")
        print(
            "\nOnly this machine's own commands are published by 'woswoar sync'.\n"
            "Run 'woswoar import atuin' on each machine to give them all the full set."
        )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    entries = cache.load_entries()
    if not entries:
        print("No history recorded yet. Try 'woswoar import bash'.")
        return 0

    names = store.host_names()
    per_host = Counter(e.host for e in entries)
    oldest = min(e.ts for e in entries)
    newest = max(e.ts for e in entries)
    unique = len({e.cmd for e in entries})

    print(f"entries  : {len(entries)} ({unique} unique)")
    print(f"range    : {store.day_for(oldest)} .. {store.day_for(newest)}")
    print("hosts    :")
    # A command arrives inert -- `cache` does that for every consumer. A name
    # does not: it is read straight out of a `.name` file that a peer wrote.
    labels = {host: make_inert(names.get(host, host)) for host, _ in per_host.most_common()}
    width = max((len(label) for label in labels.values()), default=0)
    for host, count in per_host.most_common():
        print(f"  {labels[host]:<{width}}  {count}")

    top = Counter(e.cmd for e in entries).most_common(args.top)
    if top:
        print(f"top {args.top}   :")
        width = len(str(top[0][1]))
        for cmd, count in top:
            print(f"  {count:>{width}}  {cmd[:70]}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import shutil
    import subprocess

    from . import crypto, deps, sync

    ok = True

    def check(label: str, good: bool, detail: str) -> None:
        """A pass/fail condition: failing means something needs fixing."""
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else 'FAIL'}] {label:<12} {detail}")

    def info(label: str, detail: str) -> None:
        """Context that cannot fail, kept visually distinct from a real check."""
        print(f"[--] {label:<12} {detail}")

    bash = shutil.which("bash")
    version = ""
    if bash:
        try:
            out = subprocess.run(
                [bash, "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            version = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            version = ""
    major = int(version.split(".")[0]) if version and version[0].isdigit() else 0
    check("bash", major >= 5, f"{version or 'not found'} (5.0+ required)")

    fzf = shutil.which("fzf")
    check("fzf", fzf is not None, fzf or f"not found - {deps.advice([deps.FZF])}")

    machine_file = store.machine_file()
    # Read before anything calls store.machine(), which would create it.
    has_machine = machine_file.is_file()
    check("machine", has_machine, str(machine_file))

    hook = store.data_dir() / HOOK_NAME
    check("hook", hook.is_file(), str(hook))

    rcfile = Path.home() / ".bashrc"
    sourced = rcfile.is_file() and _BEGIN in rcfile.read_text(encoding="utf-8")
    detail = "sources the hook" if sourced else "has no woswoar block"
    check("bashrc", sourced, f"{rcfile} {detail}")

    # Not just "is age on PATH". A sandboxed age -- snap, flatpak, anything
    # confined -- answers `--version` perfectly and then cannot open a key,
    # which is a real failure that used to reach the user as an unexplained
    # "permission denied" from `init` with doctor reporting nothing at all.
    age_path = shutil.which("age")
    if age_path is None:
        info("age", f"not found, needed for 'woswoar sync' - {deps.advice([deps.AGE])}")
    else:
        failure = crypto.selftest()
        check("age", not failure, age_path)
        for line in failure.splitlines():
            print(f"     {line}")

    # Checked whether or not a repo exists: `init` is exactly when this breaks,
    # and gating it behind is_repo() meant doctor was silent in the one state
    # where someone would think to run it.
    identity = store.machine().identity if has_machine else ""
    if identity:
        status = sync.identity_status(store.machine())
        check("identity", status.ok, status.detail)
    else:
        info("identity", "none yet - chosen by 'woswoar init'")

    git_path = shutil.which("git")
    if git_path is None:
        info("git", f"not found, needed for 'woswoar sync' - {deps.advice([deps.GIT])}")

    if sync.is_repo():
        info("remote", sync.remote_summary())
        status = sync.signing_status(store.machine())
        check("signing", status.ok, status.detail)

        trust = sync.trust_status(store.machine())
        check("trust", trust.ok, trust.detail)

        # One stat per day this machine has published. Worth doing every time
        # because `export` only revisits a day that still has lines to publish,
        # so a day finished before its manifest went is never looked at again.
        unmanifested = sync.days_missing_a_manifest()
        if unmanifested:
            detail = (
                f"{len(unmanifested)} published day(s) have no signed list, e.g."
                f" {unmanifested[0]} - peers refuse every chunk this machine"
                " published on them"
            )
        else:
            detail = "all present"
        check("manifests", not unmanifested, detail)

        # A listing, no decryption, so it is cheap enough to do every time --
        # and the state is otherwise silent, which is the whole problem with it.
        orphaned = sync.orphaned_days()
        if orphaned:
            host, day = orphaned[0]
            detail = (
                f"{len(orphaned)} sealed key(s) missing, e.g. {host[:8]}/{day}"
                " - chunks encrypted to them cannot be read by any machine"
            )
        else:
            detail = "all sealed"
        check("day keys", not orphaned, detail)
    else:
        info("sync", "no history repo - run 'woswoar init <url>' to sync machines")

    logs = list(store.iter_log_files())
    info("logs", f"{len(logs)} file(s) in {store.logs_dir()}")

    # Recorded history is more than ~/.bash_history holds -- the command, the
    # directory, the exit status, and every other machine's history once sync
    # has run -- so anything another user can read is a finding, not a note.
    exposed = store.readable_by_others()
    if exposed:
        detail = f"{len(exposed)} path(s) other users can read, e.g. {exposed[0]}"
        detail += " - run 'woswoar install' to fix"
    else:
        detail = f"{store.data_dir()} is owner-only"
    check("private", not exposed, detail)

    started = time.perf_counter()
    entries = cache.load_entries()
    elapsed_ms = (time.perf_counter() - started) * 1000
    info("cache", f"{len(entries)} entries loaded in {elapsed_ms:.0f} ms")

    session = "set" if os.environ.get("WOSWOAR_SESSION") else "unset (hook not loaded here)"
    info("session", session)

    if not ok:
        print("\nRun 'woswoar install' to fix identity/hook/bashrc problems.")
    return 0 if ok else 1


def cmd_init(args: argparse.Namespace) -> int:
    from . import crypto, sync

    known, identity, pinned = sync.initialise(
        remote=args.remote,
        new_identity=args.new_identity,
        identity=Path(args.identity).expanduser() if args.identity else None,
    )
    print(f"machine  : {known.name} ({known.id})")
    print(f"identity : {identity}")
    print(f"repo     : {store.history_dir()}")
    print(f"remote   : {sync.remote_summary()}")
    print(f"\nRecipients now enrolled ({store.recipients_file()}):")
    # By fingerprint, not by a truncated key. A prefix of a key looks checkable
    # and is not -- it is the abbreviation `grant` stopped using for exactly
    # that reason, and this is the listing someone reads first.
    for key in sync.recipients():
        print(f"  {crypto.fingerprint(key)}")
    if pinned:
        # Trust on first use: this is the moment, and the only moment, that a
        # machine accepts others without a human comparing anything. Printed so
        # there is something to compare against afterwards.
        print(f"\nAccepted the {len(pinned)} machine(s) already publishing here:")
        for verify_key in pinned:
            print(f"  {crypto.fingerprint(verify_key)}")
        print("Check these against the machines themselves if the repository is shared.")

    if args.remote:
        print("\nNext: 'woswoar sync'.")
        print("On a machine that already has access, run 'woswoar grant' so this")
        print("one can read history sealed before it joined.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from . import sync

    report = sync.run(push=not args.no_push)
    if report.revoked:
        print(
            "This machine's access to the shared history was revoked, so nothing\n"
            "is published from here and every other machine refuses what it already\n"
            "published. This is not something 'woswoar grant' can undo -- a\n"
            "revocation is permanent, deliberately.\n\n"
            "Commands are still being recorded locally, and 'woswoar list' still\n"
            "shows everything this machine had before. To take part again, enrol\n"
            "with a fresh identity:  woswoar init <url> --new-identity",
            file=sys.stderr,
        )
        return 0
    print(
        f"exported {report.lines_exported} line(s) in {report.chunks_written} chunk(s); "
        f"merged {report.lines_imported} line(s) from {report.chunks_merged} chunk(s) "
        f"across {len(report.hosts_seen)} other host(s)"
    )
    if report.pushed:
        print("in sync with the remote")
    elif not sync.has_remote():
        print("no remote configured - history is local only")

    if report.unreadable:
        days = len(report.unreadable)
        print(
            f"\n{days} day(s) of history are sealed to recipients that do not include\n"
            f"this machine - it joined after they were written.\n{_GRANT_REMEDY}",
            file=sys.stderr,
        )

    if report.stale:
        stale = ", ".join(sorted(report.stale))
        print(
            f"\n{len(report.stale)} day(s) could not be rebuilt, and were left exactly as\n"
            "they were rather than rewritten from the part that could be read:\n"
            f"{stale}\n"
            "Nothing was lost. If it persists, a chunk of that day is damaged in this\n"
            "checkout, or missing from it; woswoar never rewrites or deletes a chunk,\n"
            "so 'git -C ~/.local/share/woswoar/history log --diff-filter=MD' finds who\n"
            "did.",
            file=sys.stderr,
        )

    if report.untrusted:
        print(
            f"\n{len(report.untrusted)} machine(s) publish history this one has not been\n"
            "told to accept, so none of it was merged. That is what a machine enrolled\n"
            "since this one last looked is supposed to look like.\n"
            "    woswoar trust",
            file=sys.stderr,
        )

    if report.unpinned:
        print(
            f"\n{len(report.unpinned)} machine(s) were withdrawn by a revocation. Nothing\n"
            "they publish is accepted here any more, including history they published\n"
            "before it that this machine had not yet merged.",
            file=sys.stderr,
        )

    if report.changed_signer:
        print(
            f"\nWARNING: {len(report.changed_signer)} machine(s) now sign with a different\n"
            "key than the one accepted here, and nothing from them was merged. That is\n"
            "either a machine that was re-enrolled or someone rewriting the repository,\n"
            "and woswoar cannot tell which. If you re-enrolled it, accept the new key:\n"
            "    woswoar trust --replace",
            file=sys.stderr,
        )

    if report.unsignable:
        print(
            f"\nWARNING: {len(report.unsignable)} day(s) of this machine's own history\n"
            "could not be published, because the signed list already in the repository\n"
            "for them is not one this machine can verify. Publishing a replacement\n"
            "would disown everything it published earlier on those days.\n"
            "If this machine's signing key was replaced, that is why: days it signed\n"
            "with the old one stay as they are, and new days publish normally.",
            file=sys.stderr,
        )

    if report.orphaned:
        lost = ", ".join(sorted(report.orphaned))
        print(
            f"\nWARNING: {len(report.orphaned)} day(s) of this machine's own history\n"
            f"cannot be published: {lost}\n"
            "Their sealed key is missing from the repository while chunks encrypted to\n"
            "it are still there. Those chunks are unreadable by every machine, and a\n"
            "new key would not change that -- so nothing more is written for those\n"
            "days rather than adding chunks nobody will ever read.\n"
            "The commands themselves are still in this machine's own logs.\n"
            f"{_restore_remedy('hosts/<id>/keys/<day>.age')}"
            "'woswoar doctor' prints the host id and day. If it really is gone, delete\n"
            "keys/<day>.pub to write the day off: the old chunks stay unreadable, but\n"
            "this machine starts a new key and publishes again.",
            file=sys.stderr,
        )

    if report.manifest_missing:
        lost = ", ".join(sorted(report.manifest_missing))
        print(
            f"\nWARNING: {len(report.manifest_missing)} day(s) of this machine's own\n"
            f"history cannot be published: {lost}\n"
            "The signed list this machine published for them is gone from the\n"
            "repository. Signing a replacement would name only what is written now,\n"
            "so every chunk published earlier that day would stop being one any peer\n"
            "accepts. Nothing is written for those days instead.\n"
            f"{_restore_remedy('hosts/<id>/manifests/<day>')}"
            "'woswoar doctor' prints the days.",
            file=sys.stderr,
        )

    if report.foreign:
        print(
            f"\nWARNING: {len(report.foreign)} chunk(s) sit under this machine's own id\n"
            "that it never published. They were not signed and will not be offered to\n"
            "anyone, but someone else can write into this repository.",
            file=sys.stderr,
        )

    if report.unauthenticated:
        print(
            f"\nWARNING: {len(report.unauthenticated)} day(s) of history could not be\n"
            "authenticated and were refused. Every machine signs a list of the chunks\n"
            "it published, so this means the repo contains history none of your\n"
            "machines put its name to - someone else can write to it.",
            file=sys.stderr,
        )
    return 0


def _confirm(question: str, yes: bool) -> bool:
    """Whether a human here agreed to change who can read the history.

    One gate for every command that does, because the ``isatty`` branch is a
    security control and not formatting: nothing may widen or narrow access on
    an assumed answer, and an unattended caller has to say ``--yes`` and mean
    it. Copied per command, the third copy is the one that quietly drops it and
    still reads exactly like the other two.
    """
    if yes:
        return True
    if not sys.stdin.isatty():
        print(
            "\nRefusing to change who can read your history without confirmation. "
            "Re-run with --yes if you mean it.",
            file=sys.stderr,
        )
        return False
    if input(f"\n{question} [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing changed.")
        return False
    return True


def _show_readers(readers: list[Reader]) -> None:
    """One line per machine: fingerprint first, then the name.

    The fingerprint leads because it is the only part of the line the repo
    cannot choose. Everything the notes report is decided in `sync.readers`;
    this is only where it is worded.
    """
    for reader in readers:
        notes = []
        if reader.is_mine:
            notes.append("this machine")
        if reader.shares_name:
            notes.append("SAME NAME AS ANOTHER KEY")
        suffix = f"   ({', '.join(notes)})" if notes else ""
        print(f"  {reader.fingerprint}  {reader.display_name()}{suffix}")


def cmd_grant(args: argparse.Namespace) -> int:
    """Let every enrolled machine read the whole history."""
    from . import crypto, sync

    readers = sync.readers()
    if not readers:
        print("no machines enrolled yet; run 'woswoar init <url>' first", file=sys.stderr)
        return 1

    new = [reader for reader in readers if reader.is_new]
    known = [reader for reader in readers if not reader.is_new]

    if new:
        print(f"{len(new)} machine(s) NOT yet granted. Granting lets each of them read")
        print("your ENTIRE history, including days recorded before it ever existed:\n")
        _show_readers(new)
        if known:
            print(f"\nAlready granted, and unchanged ({len(known)}):\n")
            _show_readers(known)
    else:
        print(f"No machine is new since you last granted. Re-sealing to the same {len(known)}:\n")
        _show_readers(known)

    # Named for the kinds actually listed. A blanket ssh-keygen line is wrong
    # advice on a fleet that uses `--new-identity` everywhere, and advice that
    # does not fit what is on screen is advice nobody follows.
    checks = dict.fromkeys(crypto.how_to_check(reader.fingerprint) for reader in readers)
    print(
        "\nThe name beside a key is free text written by whoever added it, so check"
        f"\nthe key itself on the machine it belongs to:  {'  or  '.join(checks)}"
        "\n\nGranting re-seals the small per-day keys -- not the history itself -- and"
        "\npublishes them. Nothing is decrypted, and nothing else is re-uploaded."
    )

    # Only additions are put to a human. Re-sealing to a set that was already
    # approved widens nothing, and a prompt that fires when there is nothing to
    # decide is a prompt people learn to answer without reading.
    if new and not _confirm(f"Grant {len(new)} new machine(s) full access?", args.yes):
        return 1

    report = sync.grant(approved=[reader.key for reader in readers])
    print(f"\nre-sealed {report.resealed} key file(s)")
    if report.pushed:
        print("published to the remote")
        print("\nOn each machine that was waiting, run:  woswoar sync")
    else:
        print("no remote configured, so nothing was published")

    if report.skipped:
        # Almost always means this machine is the new one. Saying so beats
        # letting "re-sealed 0 key files" read as success.
        print(
            f"\n{report.skipped} key file(s) could not be opened by this machine, so\n"
            "they were left alone. Re-sealing a key means opening it first, which\n"
            "only a machine that already had access can do. Run this on one of\n"
            "those instead.",
            file=sys.stderr,
        )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    """Withdraw one machine's access to history recorded from now on."""
    from . import sync

    reader = sync.find_reader(args.fingerprint)

    print("This withdraws access for:\n")
    _show_readers([reader])
    print(
        "\nFrom now on it cannot read newly minted day keys, and a copy of the"
        "\nrepository taken after this cannot be opened with that key at all."
    )
    # Said before the prompt, not after it. These are the reasons someone might
    # answer no and go do something else first, so printing them alongside the
    # result would be printing them too late to act on.
    print(
        "\nWhat this does NOT do:"
        "\n  - It does not un-publish anything. Everything already in the repo"
        "\n    stays readable by that key if it kept a copy."
        "\n  - It does not revoke git access. If the key got in through a stolen"
        "\n    token or deploy key, rotate that too, or it can simply fetch again."
        "\n\nWhat it does do, from now on: nothing that machine publishes is accepted"
        "\nby any of your machines. That includes history it published before now"
        "\nwhich they have not merged yet, so run 'woswoar sync' on them first if"
        "\nyou want to keep it."
    )

    if not _confirm("Revoke this machine's access?", args.yes):
        return 1

    report = sync.revoke(reader)
    print(f"\nrevoked; re-sealed {report.resealed} key file(s)")
    if report.pushed:
        print("published to the remote")
        print("\nOn each machine that is still enrolled, run:  woswoar sync")
    else:
        print("no remote configured, so nothing was published")

    if report.still_readable:
        days = ", ".join(report.still_readable)
        print(
            f"\nCommands this machine records on {days} still go to a day key that\n"
            "was minted before the revocation, so they remain readable by the\n"
            "revoked key. Rotating it now would strand the chunks already sealed\n"
            "to it on every machine that has not merged them yet. From the next\n"
            "day onward, nothing new is readable by it.\n"
            "Your other machines have the same gap for the days they are recording\n"
            "into; this one cannot see which those are.",
            file=sys.stderr,
        )

    if report.skipped:
        print(
            f"\n{report.skipped} key file(s) could not be opened by this machine, so\n"
            "they are still sealed to the revoked key. Re-sealing means opening\n"
            "first, which only a machine that already had access can do. Run this\n"
            "on one of those as well.",
            file=sys.stderr,
        )
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    """Accept another machine's history on *this* machine."""
    from . import crypto, sync

    candidates = sync.trust_candidates()
    fresh = [c for c in candidates if not c.pinned]
    changed = [c for c in candidates if c.changed]

    if args.replace:
        candidates = changed
        heading = "These machines now sign with a different key than the one accepted here."
    else:
        candidates = fresh
        heading = "These machines publish history this one has not been told to accept."

    if not candidates:
        if changed and not args.replace:
            print(
                f"{len(changed)} machine(s) changed their signing key. Accepting a new key\n"
                "for a machine you already accepted needs:  woswoar trust --replace",
                file=sys.stderr,
            )
            return 1
        print("nothing to accept; every machine publishing here is already accepted")
        return 0

    print(f"{heading}\n")
    for candidate in candidates:
        print(f"  {candidate.fingerprint}  {candidate.display_name()}")
        if candidate.pinned:
            print(f"    replacing  {candidate.pinned_fingerprint}")
    print(
        "\nCheck a fingerprint on the machine it belongs to:"
        f"\n    {crypto.how_to_check_signer()}"
        "\n\nThis is a decision for this machine only. Nothing is published, and every"
        "\nother machine has to be told separately -- which is the point: the"
        "\nrepository can be rewritten by anyone who can push to it, so what a"
        "\nmachine accepts cannot be decided by anything kept in it."
    )
    if changed and args.replace:
        print(
            "\nA changed key is either a machine you re-enrolled or someone rewriting\nthe"
            " repository. woswoar cannot tell those apart; you can."
        )

    if not _confirm(f"Accept history from {len(candidates)} machine(s) here?", args.yes):
        return 1

    sync.trust(candidates)
    print(f"\naccepted {len(candidates)} machine(s); run 'woswoar sync' to merge their history")
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    from . import sync

    days, replaced, skipped = sync.compact(before=args.before)
    if not days:
        print("nothing to compact")
    else:
        print(f"compacted {replaced} chunk(s) into {days} (one per day)")
        print("Run 'woswoar sync' to publish. Note this rewrites history.")
    if skipped:
        # Not a failure: those days are already stored as chunks a peer can
        # read, which is the property that matters. Merging them would produce
        # one no peer would accept.
        print(
            f"{skipped} day(s) left alone: merging them would exceed the chunk size "
            "limit, so they stay as the smaller chunks they already are."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="woswoar",
        description="Distributed shell history over git, searched with fzf.",
    )
    parser.add_argument("--version", action="version", version=f"woswoar {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_scope(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--scope", choices=search.SCOPES, default="global")
        sub.add_argument(
            "--no-dedup", action="store_true", help="keep repeated commands instead of collapsing"
        )

    p_search = subparsers.add_parser("search", help="pick a command interactively with fzf")
    add_scope(p_search)
    p_search.add_argument("--query", default="", help="initial fzf query")
    p_search.set_defaults(func=cmd_search)

    p_list = subparsers.add_parser("list", help="print matching lines (used by fzf reload)")
    add_scope(p_list)
    p_list.add_argument("--limit", type=int, default=None)
    p_list.set_defaults(func=cmd_list)

    p_import = subparsers.add_parser("import", help="import an existing shell history")
    p_import.add_argument("kind", choices=importer.KINDS)
    p_import.add_argument(
        "--file",
        help="source (default: ~/.bash_history, ~/.zsh_history, "
        "or ~/.local/share/atuin/history.db)",
    )
    p_import.add_argument("--dry-run", action="store_true")
    p_import.add_argument(
        "--this-host-only",
        action="store_true",
        help="atuin: skip history belonging to other machines",
    )
    p_import.set_defaults(func=cmd_import)

    p_install = subparsers.add_parser("install", help="install the shell hook into .bashrc")
    p_install.add_argument("--rcfile", help="file to modify (default: ~/.bashrc)")
    p_install.set_defaults(func=cmd_install)

    p_stats = subparsers.add_parser("stats", help="summarise recorded history")
    p_stats.add_argument("--top", type=int, default=10)
    p_stats.set_defaults(func=cmd_stats)

    p_doctor = subparsers.add_parser("doctor", help="check the installation")
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = subparsers.add_parser("init", help="create or join a history repo")
    p_init.add_argument("remote", nargs="?", help="git URL of the history repo")
    p_init.add_argument(
        "--new-identity",
        action="store_true",
        help="generate a dedicated age key instead of reusing an SSH key",
    )
    p_init.add_argument("--identity", help="use this private key")
    p_init.set_defaults(func=cmd_init)

    p_sync = subparsers.add_parser("sync", help="exchange history with the remote")
    p_sync.add_argument("--no-push", action="store_true", help="stay local; do not contact remote")
    p_sync.set_defaults(func=cmd_sync)

    p_grant = subparsers.add_parser(
        "grant",
        # `reencrypt` named the mechanism, which hid what it does to the user's
        # history. Kept working so older notes and error messages do not rot.
        aliases=["reencrypt"],
        help="let newly enrolled machines read the older history",
    )
    p_grant.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_grant.set_defaults(func=cmd_grant)

    p_revoke = subparsers.add_parser(
        "revoke", help="withdraw a machine's access to history recorded from now on"
    )
    p_revoke.add_argument(
        "fingerprint",
        help="the fingerprint 'woswoar grant' shows, or an unambiguous prefix of it",
    )
    p_revoke.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_revoke.set_defaults(func=cmd_revoke)

    p_trust = subparsers.add_parser(
        "trust", help="accept another machine's published history on this machine"
    )
    p_trust.add_argument(
        "--replace",
        action="store_true",
        help="accept a new signing key for a machine already accepted",
    )
    p_trust.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_trust.set_defaults(func=cmd_trust)

    p_compact = subparsers.add_parser("compact", help="merge old chunks (reduces file count)")
    p_compact.add_argument("--before", help="only days before this YYYY-MM-DD (default: today)")
    p_compact.set_defaults(func=cmd_compact)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `woswoar list | head` is a normal thing to do.
        return 0
    except WoswoarError as exc:
        # These carry actionable messages -- a missing age, an unreadable
        # identity, a repo that was never initialised. A traceback would bury
        # them, and sync runs unattended from a timer where nobody reads one.
        print(f"woswoar: {exc}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    sys.exit(main())
