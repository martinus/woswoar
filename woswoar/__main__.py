"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, cache, importer, install, search, store
from .entry import make_inert
from .errors import WoswoarError
from .report import Check, paragraphs

if TYPE_CHECKING:  # `sync` is imported lazily; only the annotations need it.
    from .sync import Failure, Reader, ReencryptReport


def _install(shell: str | None, rcfile: str | None) -> int:
    """Run the install and report it. The half `setup` shares with `install`.

    The command's own two flags, which is what each of these five cores takes --
    rather than the `argparse.Namespace` `setup` used to forge. Adding a flag to
    `install` now means adding a parameter here, and mypy says so at the call
    site that forgot it; a `Namespace` literal never could, because
    `getattr(args, "shell", None)` swallowed the omission and chose a default.

    The flags raw rather than resolved, which is the second half of the same
    point. An earlier turn of this had the caller run `shells_from` and expand
    `~` itself, so both callers repeated two steps and either could have done
    one of them differently -- mypy sees a `Path` from a forgotten `expanduser`
    exactly as happily as one from a remembered one. `shells_from` is a few
    `stat` calls, so the wizard calling it again for its own message is cheaper
    than the chance of the two disagreeing.
    """
    override = Path(rcfile).expanduser() if rcfile else None
    done = install.install_hooks(install.shells_from(shell, rcfile))

    print(f"machine : {done.machine.name} ({done.machine.id})")
    print(f"logs    : {store.logs_dir()}")
    # `installed` rather than `shell`: the parameter of that name is the *flag*,
    # which may be None or "both", and rebinding it to the last shell in the loop
    # is a trap for whoever next needs it after this point.
    for installed, hook in done.hooks.items():
        print(f"hook    : {hook}" + (f"  ({installed})" if len(done.hooks) > 1 else ""))

    # Reported before the rc files are touched, and that ordering is the reason
    # `install_hooks` and `wire_rcfiles` are two calls: an unwritable `.zshrc`
    # still leaves a person knowing the hook is there and can be sourced by hand.
    for rc, action in install.wire_rcfiles(done.hooks, override).values():
        print(f"rcfile  : {rc} ({action})")

    # One line per shell, never `source a b`: a second word there is not a
    # second file to read, it is `$1` for the first one. The pair only ever
    # differ by which shell you are standing in, so each is offered on its own.
    if len(done.hooks) == 1:
        print("\nOpen a new shell, or run:  source", *done.hooks.values())
    else:
        print("\nOpen a new shell. To pick it up in one already open:")
        for installed, hook in done.hooks.items():
            print(f"    source {hook}   # in {installed}")

    # The moment someone finds out, so the moment to say it. Recording works
    # without any of these, which is why this warns rather than fails -- but
    # without fzf there is no Ctrl-R, and Ctrl-R is what woswoar is for.
    from . import deps

    absent = deps.missing()
    if absent:
        print(f"\n{deps.report(absent)}", file=sys.stderr)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Set up machine identity, install each shell's hook, and wire up its rcfile."""
    return _install(args.shell, args.rcfile)


def cmd_list(args: argparse.Namespace) -> int:
    if args.show is not None:
        # The preview pane, one row at a time. Ahead of everything else because
        # it shares none of it: no display line, no width, and an empty answer
        # is a cursor sitting past the end of a list that moved under it -- a
        # blank pane, not a message. `--colour` it does share, and means the
        # same thing by it.
        block = search.detail(
            args.show,
            args.scope,
            dedup=not args.no_dedup,
            around=args.around,
            colour=args.colour,
        )
        if block is not None:
            print(block)
        return 0
    if args.print_anchor:
        # Where fzf should park the cursor once it has unfolded the timeline.
        # A separate invocation because `transform` composes `reload(...)` and
        # `pos(...)` in one string, so the position has to be known before the
        # reload it belongs to has run.
        print(search.anchor_position(args.around or 0, args.scope, dedup=not args.no_dedup))
        return 0
    lines = search.lines_for(
        args.scope,
        dedup=not args.no_dedup,
        limit=args.limit,
        colour=args.colour,
        host_width=args.host_width,
        around=args.around,
    )
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    elif sys.stdout.isatty() and (note := search.empty_note(args.scope)):
        # Gated on **stdout** being a terminal, not stderr, which is the half of
        # this decision that belongs here. `woswoar list | wc -l` must stay
        # clean, and the picker's reload runs this with stdout on a pipe but
        # stderr still on the terminal -- a note printed there would land in the
        # middle of fzf's screen. Which scopes have anything to say is
        # `search`'s business, not the CLI's.
        print(note, file=sys.stderr)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    selection = search.interactive(args.scope, query=args.query, dedup=not args.no_dedup)
    if selection is None:
        return 1
    print(selection)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    return _import(
        args.kind,
        Path(args.file).expanduser() if args.file else None,
        dry_run=args.dry_run,
        this_host_only=args.this_host_only,
    )


def _import(kind: importer.Kind, path: Path | None, *, dry_run: bool, this_host_only: bool) -> int:
    """Import one history and report it. Shared with `setup`'s step 3."""
    try:
        result = importer.run(kind, path, dry_run=dry_run, this_host_only=this_host_only)
    except FileNotFoundError as exc:
        print(f"woswoar: {exc}", file=sys.stderr)
        return 1
    prefix = "would import" if dry_run else "imported"
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
    """Render what `doctor` found. Every verdict below is decided elsewhere.

    Printed **as each group comes back**, not after all of them. That is not
    presentation: the slowest checks are the ones a broken installation makes
    slow, so accumulating first puts the blank screen exactly where the command
    is most needed. A snap-packaged `age` takes ~500 ms per spawn and `age_check`
    spawns it eight times, which is four seconds of nothing -- while the check
    that would explain it sits undisplayed in a list. It also costs the thing a
    person does when a command hangs, which is read the last line it printed.

    The order of the report is the loop below, and it is the one thing that
    genuinely belongs here: `doctor` owns most of the checks and the installer
    owns three, and neither can state the order the other's lines sit in. It is
    also the order somebody fixes things in -- the shell before the hook before
    the rc file -- which is why it is written down rather than sorted.
    """
    from . import doctor, report

    marks = report.markers()
    found: list[Check] = []

    def show(produced: Check | Iterable[Check]) -> None:
        """Collect a group and put it on screen before the next one is asked."""
        group = [produced] if isinstance(produced, Check) else list(produced)
        found.extend(group)
        for line in report.lines(group, marks):
            print(line, flush=True)

    if args.prove:
        from . import prove

        for check in prove.run():
            show(check)
        if report.failed(found):
            # Unlike the checks below, nothing here is the user's to fix: the
            # sandbox is built from scratch, so a FAIL can only be woswoar
            # publishing something it promised not to.
            print(
                "\nA FAIL above is a defect in woswoar itself, not in your setup."
                "\nPlease report it: https://github.com/martinus/woswoar/issues"
            )
            return 1
        return 0

    show(install.shell_checks())
    show(doctor.fzf_check())
    show(doctor.machine_check())
    show(install.hook_checks())
    show(doctor.age_check())
    show(doctor.identity_check())
    show(doctor.repo_checks())
    show(doctor.local_checks())

    if report.failed(found):
        print("\nRun 'woswoar install' to fix identity/hook/bashrc problems.")
        return 1
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    return _init(
        args.remote,
        new_identity=args.new_identity,
        identity=Path(args.identity).expanduser() if args.identity else None,
        no_sync=args.no_sync,
    )


def _init(remote: str | None, *, new_identity: bool, identity: Path | None, no_sync: bool) -> int:
    """Join a history repo and report it. Shared with `setup`'s step 4."""
    from . import archive, crypto, gitrepo, sync

    known, identity_used, pinned = sync.initialise(
        remote=remote, new_identity=new_identity, identity=identity
    )
    print(f"machine  : {known.name} ({known.id})")
    print(f"identity : {identity_used}")
    print(f"repo     : {store.history_dir()}")
    print(f"remote   : {gitrepo.remote_summary()}")
    print(f"\nRecipients now enrolled ({archive.recipients_file()}):")
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

    if not remote:
        return 0

    # The sync `init` used to tell you to run. Nothing about it is a separate
    # decision -- joining a repository and then not exchanging anything with it
    # is not a state anyone wants -- and leaving it to a second command meant
    # the machine published nothing until one was run. `--no-sync` is there for
    # the case where the remote is not reachable yet.
    if no_sync:
        print("\nNext: 'woswoar sync'.")
    else:
        print()
        try:
            report = sync.run()
        except WoswoarError as exc:
            # The join itself is done and durable by this point -- the identity
            # is saved, the repo is cloned, the peers are pinned. A remote that
            # blinked must not make all of that report failure, because the
            # obvious response to a failed `init` is to run it again.
            print(f"joined, but the first sync failed:\n  {exc}", file=sys.stderr)
            print("Nothing is lost; run 'woswoar sync' when the remote is reachable.")
        else:
            print(
                f"published {report.lines_exported} line(s) of this machine's history; "
                f"merged {report.lines_imported} line(s) from "
                f"{len(report.hosts_seen)} other machine(s)"
            )

    # Asked of `sync`, which already knows how to fail softly at working out
    # this machine's own key: open-coding `crypto.recipient_for` here turned an
    # unreadable identity into a failed `init` that had actually succeeded.
    if sync.others_enrolled():
        # The step that is easy to miss, because it happens somewhere else. Said
        # in terms of what is missing rather than of the commands: `accept` is
        # one thing to run, and it names both halves itself.
        print(
            "\nLast step, on each machine you already use:\n"
            "    woswoar accept\n"
            "That is what lets this machine read history from before it joined, and"
            "\nwhat tells the others to accept what it publishes."
        )
    else:
        print("\nThis is the first machine here. Run 'woswoar init' on the next one.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    return _sync(no_push=args.no_push)


def _sync(*, no_push: bool) -> int:
    """Sync and report it. Shared with `setup`'s step 4."""
    from . import gitrepo, sync

    # Before the sync, not after: a remote that is unreachable must not be able
    # to keep the hook out of date, and these two have nothing to do with each
    # other beyond both being things this command is well placed to do.
    for hook in install.refresh_hook():
        print(f"updated the shell hook at {hook} to {__version__}")

    # The shell hook fires this detached, with its output going nowhere, so a
    # failure has to be left somewhere the bare `woswoar` can find it.
    #
    # Gated on whether anyone is watching, not on which command this is. The
    # first version asked the latter -- recorded from `sync`, not from `init`,
    # `accept` or `grant` -- and got the common case backwards: somebody who
    # types `woswoar sync`, reads the error and deals with it was then told by
    # every later `woswoar` that a *background* sync had been failing for days,
    # with "run woswoar sync to see it in full" as the remedy. That is the
    # command they had just run.
    #
    # `stderr.isatty()` is the same question `progress.to_terminal` asks, and
    # for the same reason: a terminal means the message has already been read
    # by the person it was for.
    watched = sys.stderr.isatty()
    try:
        report = sync.run(push=not no_push)
    except Exception as exc:
        if not watched:
            sync.record_failure(str(exc) or exc.__class__.__name__)
        raise
    # Cleared whoever was watching: a sync that worked is proof the recorded
    # failure is stale, and someone who fixed the problem by hand should not
    # have to wait for a background run to stop being told about it.
    sync.clear_failure()

    # The summary is stdout and the notices are stderr, which is the one thing
    # this function still decides. Everything they *say* is `Report.notices`,
    # because what a field means is sync's to know -- twelve `if report.X:`
    # blocks lived here, so "does this run warn about a changed signer" was a
    # question only a test that grepped stderr could ask.
    if not report.silent:
        print(
            f"exported {report.lines_exported} line(s) in {report.chunks_written} chunk(s); "
            f"merged {report.lines_imported} line(s) from {report.chunks_merged} chunk(s) "
            f"across {len(report.hosts_seen)} other host(s)"
        )
        if report.pushed:
            print("in sync with the remote")
        elif not gitrepo.has_remote():
            print("no remote configured - history is local only")

    for block in paragraphs(report.notices()):
        print(block, file=sys.stderr)
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

    _report_reseal(sync.grant(approved=[reader.key for reader in readers]))
    return 0


def _report_stale_hook() -> None:
    if not install.hook_is_stale():
        return
    print(
        "\nThe shell hook here was installed by an older woswoar, so this"
        "\nmachine is still running that version's shell code -- including"
        "\nwhatever it did or did not do about syncing."
        "\n\nNext:  woswoar install   then open a new shell"
    )


def _report_sync_failure(failure: Failure | None) -> None:
    """Surface a background sync that has been failing where nobody could see.

    The one thing the systemd timer had over a detached fork from the prompt:
    its stderr went to the journal. This is the replacement, and it is put in
    front of someone who typed `woswoar` rather than someone who thought to run
    `journalctl --user -u woswoar-sync`, so it is the better half of the trade.
    """
    if failure is None:
        return
    print(f"\nBackground sync has been failing for {search.relative_time(int(failure.when))}:")
    print(f"    {make_inert(failure.message)}")
    # The remedy is the same one every time and it is not "run sync again":
    # what a detached sync hides is the *message*, so the fix is to look at it.
    print("\nNext:  woswoar sync   to see it in full")


def _report_reseal(report: ReencryptReport, waiting: bool = True) -> None:
    """Say what re-sealing did. Shared, because the half that is easy to omit
    is the half that matters.

    `skipped` is what distinguishes "nothing needed doing" from "this machine
    could not do it" -- the state a *newly joined* machine is in, which is
    exactly where someone lands after being told to run this. `accept` reported
    only `resealed`, so on that machine it said "re-sealed 0 key file(s), so
    they can read the older history", which is false in both halves.
    """
    if report.resealed or report.skipped:
        print(f"\nre-sealed {report.resealed} key file(s)")
    else:
        # Not "re-sealed 0", which reads as a failure. Every key file is already
        # sealed to exactly these machines, so there was nothing to do.
        print("\nnothing to do: every key is already sealed to these machines")
    if report.pushed:
        print("published to the remote")
        if waiting:
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


def cmd_status(args: argparse.Namespace) -> int:
    """`woswoar` on its own: where you are, and the one command to run next.

    The single entry point. Everything else is still there and still does
    exactly what it did; this is so that nobody has to know which of them
    applies before they can find out.

    What it deliberately does *not* do is act on anything that widens who can
    read your history. It names `accept` and shows what is waiting; it does not
    ask. A prompt that appears because you typed the bare command is a prompt
    someone else chose the moment for -- anyone who can push to the repository
    can enrol a machine, and then the next `woswoar` you type for any reason has
    a consent question in it. The one thing that makes that question mean
    anything is that you went looking for it.

    `setup` is the exception, and only when nothing is installed: it is all
    questions already, and there is nothing here yet to widen access to.
    """
    from . import cache, setup, sync

    if setup.untouched():
        print("Nothing installed here yet -- setting up.\n")
        return _setup(shell=None, rcfile=None)

    loaded = cache.load_columns()
    stamps, _ = loaded.stamps_and_commands()
    hosts = {meta.host for meta in loaded.meta.values() if meta.host}
    machines = f"{len(hosts)} machine{'s' if len(hosts) != 1 else ''}"
    print(f"woswoar {__version__} -- {len(stamps)} commands from {machines}")

    # Ahead of everything else, because it is the condition under which the rest
    # of this report is describing a machine that is not running this version.
    # `install` copies the hook, so upgrading the Python leaves the old shell
    # code in place -- and syncing lives in the hook, so the visible symptom is
    # that nothing syncs while every other line here says all is well. `doctor`
    # says so too, but only somebody who already suspects a problem runs doctor.
    _report_stale_hook()

    if not sync.is_repo():
        print(
            "\nThis machine keeps its history to itself.\n"
            "Next:  woswoar init <url>   to share it with your other machines"
        )
        return 0

    _report_sync_failure(sync.last_failure())

    # Local only: see `sync.local_newcomers`. Typing this must not reach the
    # network, and what has not arrived here is not yet this machine's decision.
    pending = sync.local_newcomers().machines
    if not pending:
        print("\nNothing to do. Press Ctrl-R.")
        return 0

    waiting = [n for n in pending if not n.changed_key]
    changed = [n for n in pending if n.changed_key]
    if waiting:
        print(f"\n{len(waiting)} machine(s) waiting to be accepted here:")
        for machine in waiting:
            # Fingerprint first, then the name, matching `_show_readers`: it is
            # the only part of the line the repository cannot choose. It also
            # has to be here at all, because on a machine that has not been
            # granted yet *every* name is unreadable -- reported from a fresh
            # install as three consecutive lines reading `'(unnamed)'`, which
            # is not a list of three machines so much as a list of nothing.
            print(f"    {machine.fingerprint}  {machine.display_name()}")
        if any(machine.named for machine in waiting):
            print("\nNext:  woswoar accept")
        else:
            # Why they have no names, and the half of the job that happens
            # somewhere else. `init` says this once and then nobody sees it
            # again, which is exactly when it is needed.
            #
            # Both commands named in one breath, and the explanation before
            # rather than after them: an earlier version printed the ordinary
            # `Next: woswoar accept` and *then* said accepting here changes
            # nothing about what this machine can read, which reads as a
            # contradiction and leaves someone unsure which half to believe.
            print(
                "\nNone of them could be named here: a name is sealed to the machines"
                "\nthat can read this history, and nothing has granted that to this one"
                "\nyet. So it takes the same command in two places --"
                "\n\nNext:  woswoar accept   here, so they can read what this machine"
                "\n                        publishes"
                "\n       woswoar accept   on a machine you already use, so this one can"
                "\n                        read the history from before it joined"
            )
    if changed:
        print(
            f"\n{len(changed)} machine(s) changed their signing key, which is either a"
            "\nre-enrolment or someone rewriting the repository:"
            "\n\nNext:  woswoar trust --replace"
        )
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    return _setup(args.shell, args.rcfile)


def _setup(shell: str | None, rcfile: str | None) -> int:
    """Walk a fresh machine through the whole thing, asking as it goes.

    The wizard's *dispatch*: which command runs, in what order, and what it is
    handed. The questions and the prose are here too; `setup` holds what the
    wizard works out without asking -- see that module for where the line falls.
    """
    from . import deps, setup

    if not sys.stdin.isatty():
        # It asks questions, so it cannot run unattended -- and silently
        # choosing defaults for someone's script would be worse than refusing.
        print(
            "woswoar setup asks questions, so it needs a terminal.\n"
            "Without one, the same thing is:\n"
            "    woswoar install\n"
            "    woswoar import bash|zsh|atuin      # optional\n"
            "    woswoar init <url>                 # optional",
            file=sys.stderr,
        )
        return 1

    print("Setting up woswoar. Everything here can be re-run safely.\n")

    print("1/4  Tools")
    absent = deps.missing()
    if absent:
        print(f"     {deps.report(absent)}")
        if not setup.ask_yes("     Carry on without them?", default=False):
            return 1
    else:
        print("     everything woswoar needs is installed")

    print("\n2/4  Shell hook")
    # Named before it is written, because it is now more than one file and the
    # rule that chose them is not obvious from the output alone.
    chosen = install.shells_from(shell, rcfile)
    print(f"     {' and '.join(chosen)} -- {setup.why_those_shells(shell, rcfile, chosen)}")
    _install(shell, rcfile)

    print("\n3/4  Existing history")
    _offer_imports()

    print("\n4/4  Sync with your other machines")
    return _offer_remote()


def _offer_imports() -> None:
    """Offer each history found on this machine, one at a time."""
    from . import setup

    found = setup.importable()
    if not found:
        print("     nothing to import (no bash, zsh or atuin history found)")
        return

    for kind, path, size in found:
        if not setup.ask_yes(f"     Import {kind} history from {path} ({size / 1e6:.1f} MB)?"):
            continue
        this_host_only = False
        if kind == "atuin":
            # atuin keeps every machine it has synced with in one database, and
            # woswoar publishes only this machine's own commands -- so importing
            # all of them on every machine stores each machine's history once
            # per machine. Asked rather than assumed: on a single woswoar
            # machine, importing the lot is exactly what you want.
            print(
                "       atuin keeps other machines' history in the same database.\n"
                "       If you will run woswoar on those machines too, import only\n"
                "       this one's here and let each machine import its own --\n"
                "       otherwise every machine stores every other machine twice."
            )
            this_host_only = setup.ask_yes("       Import only this machine's history?")
        _import(kind, path, dry_run=False, this_host_only=this_host_only)


def _offer_remote() -> int:
    """Join a history repo, or finish without one."""
    from . import setup, sync

    if sync.is_repo():
        print("     already joined a history repo; syncing")
        return _sync(no_push=False)

    print(
        "     woswoar syncs through an ordinary git repository you own -- an\n"
        "     empty GitHub repo, a bare repo on a NAS, a folder on a USB stick.\n"
        "     There is no server and no account. Leave blank to stay on this\n"
        "     machine only; you can run 'woswoar init <url>' any time later."
    )
    remote = setup.ask("     Repository URL")
    if not remote:
        print("\n     Staying local. Open a new shell and press Ctrl-R.")
        return 0

    # Through the same function `woswoar init` runs, so the sequence has one
    # source of truth: whatever it prints as the next step is what someone
    # following this reads, and there is no second copy here to disagree.
    return _init(remote, new_identity=False, identity=None, no_sync=False)


def cmd_accept(args: argparse.Namespace) -> int:
    """`grant` and `trust` for a machine you own, in one step.

    The two stay separate commands: they answer different questions, and someone
    sharing a repository with a colleague may well answer them differently. This
    is for the common case, where the answer to both is "yes, that one is mine"
    -- reported as the thing that made setting up a second machine feel like too
    much. Both are still named and described here, because what is being agreed
    to is exactly what it was before.
    """
    from . import crypto, sync

    pending = sync.newcomers()
    if not pending.machines:
        print("nothing to accept; every machine here is already granted and trusted")
        return 0

    # A partition, not two overlapping filters. A machine can be new to read
    # *and* changed to sign -- the ordinary state on a machine that has just
    # joined, where TOFU pinned every peer and nothing has been granted yet --
    # and listing it among the newcomers printed its unpinned new key under
    # "already accepted here" on the same screen that said its key had changed.
    changed = [n for n in pending.machines if n.changed_key]
    fresh = [n for n in pending.machines if not n.changed_key]

    if not fresh:
        print(
            f"{len(changed)} machine(s) now sign with a different key than the one\n"
            "accepted here. That is either a machine you re-enrolled or someone\n"
            "rewriting the repository, and nothing can tell those apart:\n"
            "    woswoar trust --replace",
            file=sys.stderr,
        )
        return 1

    print(f"{len(fresh)} machine(s) not yet accepted here:\n")
    for machine in fresh:
        # `shares_name` is why this cannot just print the name: two machines
        # really can both be `martin@laptop`, and so can a key someone else
        # added. `grant` has said this since it existed.
        note = "   (SAME NAME AS ANOTHER KEY)" if machine.shares_name else ""
        print(f"  {machine.display_name()}{note}")
        if machine.reader is not None:
            said = "reads with" if machine.needs_grant else "already reads your history"
            print(f"      {said:<28}{machine.reader.fingerprint}")
        if machine.candidate is not None:
            said = "signs with" if machine.needs_trust else "already accepted here"
            print(f"      {said:<28}{machine.candidate.fingerprint}")
        if machine.reader is not None and machine.candidate is not None:
            # That these two keys belong to one machine is something the
            # *repository* says, in a file anyone who can push can write. Left
            # unsaid, a fingerprint the reader can verify sitting above one they
            # cannot reads as the first vouching for the second (#110).
            print("      (that these are one machine is the repository's claim)")

    granting = [n for n in fresh if n.needs_grant]
    trusting = [n for n in fresh if n.needs_trust]
    print("\nAccepting does two separate things:\n")
    if granting:
        print(
            f"  read     {len(granting)} machine(s) get to read your ENTIRE history, including"
            "\n           days recorded before they existed. This is published, so it"
            "\n           applies everywhere -- and it cannot be taken back for what"
            "\n           they have already read.\n"
        )
    if trusting:
        print(
            f"  believe  this machine will accept what {len(trusting)} machine(s) publish."
            "\n           Local only: every other machine of yours has to be told"
            "\n           separately, because the repository is the thing that decision"
            "\n           defends against.\n"
        )
    # One line per *kind* of key on screen, not one blanket command. There are
    # two here and they are checked differently -- `grant` sends you to the age
    # identity, `trust` to woswoar's own signing key -- and advice that does not
    # match what is above it is advice nobody follows.
    checks = []
    if granting:
        reads = dict.fromkeys(
            crypto.how_to_check(n.reader.fingerprint) for n in granting if n.reader is not None
        )
        checks += [f"    reads with   {command}" for command in reads]
    if trusting:
        checks.append(f"    signs with   {crypto.how_to_check_signer()}")
    print(
        "A name is free text written by whoever added the key. The fingerprints are"
        "\nnot -- run these on the machine they belong to and compare:\n"
    )
    print("\n".join(checks))
    if changed:
        # Deliberately not part of the prompt above. Accepting a *new* machine
        # and accepting a new key for one already accepted are different
        # decisions, and rolling them together is how the second gets made by
        # someone answering the first. Both fingerprints, as `trust --replace`
        # shows them: which key is being left behind is half the question.
        print(f"\nSeparately, {len(changed)} machine(s) changed their signing key:\n")
        for machine in changed:
            print(f"  {machine.display_name()}")
            if machine.candidate is not None:
                print(f"      now signs with              {machine.candidate.fingerprint}")
                print(f"      accepted here              {machine.candidate.pinned_fingerprint}")
        print("\nThat is not part of this, and needs:  woswoar trust --replace")

    if pending.contested:
        # Not a warning about a machine -- a statement about the repository.
        # Two host directories claiming one recipient cannot both be right, and
        # nothing here can say which is, so neither was paired with it above.
        print(
            f"\n{len(pending.contested)} key(s) are claimed by more than one machine"
            " directory in the\nrepository. An honest repository does not do that."
            " Nothing here can tell you\nwhich claim is real, so those keys are"
            " listed on their own above rather than\nbeside a signing key."
            "\nCheck the fingerprints on the machines themselves before accepting"
            " anything.",
            file=sys.stderr,
        )

    if not _confirm(f"Accept {len(fresh)} machine(s)?", args.yes):
        return 1

    if granting:
        # `pending.enrolled`, from the fetch that produced what was on screen --
        # never a fresh `readers()` call. `grant` refuses when its own fetch
        # disagrees with what a human agreed to, and asking again *after* the
        # prompt makes that refusal unable to fire for the window it exists to
        # cover: a machine enrolling while the prompt is up would be picked up
        # by both reads, they would agree, and it would be sealed into the whole
        # history without ever having been shown.
        _report_reseal(sync.grant(approved=pending.enrolled), waiting=False)
    if trusting:
        sync.trust([n.candidate for n in trusting if n.candidate is not None])
        print(f"\naccepted the history published by {len(trusting)} machine(s)")

    print("\nNext: 'woswoar sync' to exchange history with them.")
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
    # Not required: `woswoar` on its own is the status line, which is the one
    # command somebody has to know. argparse's answer to a bare invocation was
    # a usage error listing fourteen names.
    parser.set_defaults(func=cmd_status)
    subparsers = parser.add_subparsers(dest="command")

    def sub(
        name: str, summary: str, detail: str, aliases: list[str] | None = None
    ) -> argparse.ArgumentParser:
        """One subcommand, with the same sentence in the listing and in `-h`.

        `help=` alone appears only in `woswoar --help`; `woswoar doctor -h` used
        to print a usage line, `-h, --help`, and nothing whatever about what
        doctor does. Every subcommand was like that. So `description` is not
        optional here, and `Raw...HelpFormatter` keeps the paragraphs from being
        reflowed into one block.
        """
        return subparsers.add_parser(
            name,
            help=summary,
            description=f"{summary[0].upper()}{summary[1:]}.\n\n{textwrap.dedent(detail).strip()}",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            aliases=aliases or [],
        )

    def add_install_flags(sub: argparse.ArgumentParser) -> None:
        """The two flags `install` and `setup` share, declared once.

        They were the same six lines twice, and #202 had to edit both copies to
        reach `install.HOOKS` -- which is the parser-level version of the forged
        namespaces that issue is about: a flag added to one and not the other
        leaves the wizard unable to pass something `install` accepts.
        """
        sub.add_argument("--rcfile", help="file to modify (default: the rc file of each shell)")
        sub.add_argument(
            "--shell",
            choices=["auto", *install.HOOKS, "both"],
            default="auto",
            help="which shell(s) to install for (default: auto -- every shell whose "
            "rc file already exists, or $SHELL when there is none)",
        )

    def add_scope(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--scope", choices=search.SCOPES, default="global")
        sub.add_argument(
            "--no-dedup", action="store_true", help="keep repeated commands instead of collapsing"
        )

    p_search = sub(
        "search",
        "pick a command interactively with fzf",
        """
        What Ctrl-R runs. The chosen command is placed on your prompt for
        editing, never executed.

        Ctrl-G, Ctrl-H, Ctrl-S and Ctrl-O switch between every machine, this
        machine, this shell session, and this directory and below, without
        leaving the picker.

        The directory scope spans machines on purpose: ~/src/woswoar on either
        of your boxes is the same project.

        Ctrl-/ shows the rest of what was recorded for the highlighted command
        -- its directory, session, exit code and duration.
        """,
    )
    add_scope(p_search)
    p_search.add_argument("--query", default="", help="initial fzf query")
    p_search.set_defaults(func=cmd_search)

    p_list = sub(
        "list",
        "print matching lines as plain text",
        """
        Mostly internal: this is what the picker re-runs when you switch scope
        with Ctrl-G, Ctrl-H, Ctrl-S or Ctrl-O, and what fills the Ctrl-/ pane.
        It is also how to read your history without fzf installed, and it pipes
        -- `woswoar list | grep docker`.
        """,
    )
    add_scope(p_list)
    p_list.add_argument("--limit", type=int, default=None)
    # Off by default, so `woswoar list | grep` stays plain text. The picker's
    # own reload bindings pass it, because that output goes into fzf.
    # Passed by the picker's reload bindings so both sides lay the line out the
    # same way. Not something to type by hand, hence no help text.
    p_list.add_argument("--host-width", type=int, default=None, help=argparse.SUPPRESS)
    p_list.add_argument(
        "--around",
        type=int,
        default=None,
        help="show the timeline either side of this row of the list, oldest first",
    )
    # Both passed by the picker's ctrl-t binding rather than typed.
    p_list.add_argument("--print-anchor", action="store_true", help=argparse.SUPPRESS)
    # The preview pane's own entry point: one row of the list this would print,
    # as a block. fzf's interface, not a person's, so it is suppressed like the
    # two above rather than documented.
    p_list.add_argument("--show", type=int, default=None, help=argparse.SUPPRESS)
    p_list.add_argument(
        "--colour",
        "--color",
        action="store_true",
        help="mark failed commands, for a terminal that understands it",
    )
    p_list.set_defaults(func=cmd_list)

    p_import = sub(
        "import",
        "import an existing shell history",
        """
        Idempotent: importing the same file twice adds nothing the second time,
        so it is safe to re-run. Commands that look like credentials are
        skipped; `--dry-run` lists what would be skipped without importing.

        For atuin on more than one woswoar machine, use `--this-host-only` on
        each. atuin keeps every machine it has synced with in one database, and
        sync publishes only this machine's own commands -- so importing all of
        them everywhere stores each machine's history once per machine.
        """,
    )
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

    p_status = sub(
        "status",
        "where this machine is, and what to run next",
        """
        What `woswoar` on its own prints. Everything else is still a command of
        its own; this is so that nobody has to know which one applies before
        they can find out.

        It reads only what is already here -- no network -- and it does not act
        on anything that widens who can read your history. It names the command
        and shows what is waiting; deciding is still something you go and do.
        """,
    )
    p_status.set_defaults(func=cmd_status)

    p_setup = sub(
        "setup",
        "set woswoar up from scratch, asking as it goes",
        """
        The guided version of `install`, `import` and `init`. Run it on a fresh
        machine and answer four questions; it calls the same commands you would
        have run yourself, so there is nothing it can do that they cannot.

        Safe to re-run: the hook is replaced rather than repeated, importing the
        same history twice adds nothing, and a machine that already joined a
        repository just syncs.

        Needs a terminal, because it asks. Without one it says what to run
        instead rather than choosing for you.
        """,
    )
    add_install_flags(p_setup)
    p_setup.set_defaults(func=cmd_setup)

    p_install = sub(
        "install",
        "install the shell hook into your rc file(s)",
        """
        Copies each shell's hook and adds a marked block to its rc file. Safe to
        re-run: the block is replaced rather than repeated, which is also how
        to upgrade the hook after installing a new woswoar.

        By default it installs for every shell whose rc file already exists, so
        a machine that has never run zsh does not acquire a ~/.zshrc. With
        neither rc file present it follows $SHELL and creates that one --
        otherwise a fresh account would get nothing.

        Reports any missing tool (fzf, age, git) and the command to install it.
        """,
    )
    add_install_flags(p_install)
    p_install.set_defaults(func=cmd_install)

    p_stats = sub(
        "stats",
        "summarise recorded history",
        """
        How much is recorded, over what period, per machine, and which commands
        you run most.
        """,
    )
    p_stats.add_argument("--top", type=int, default=10)
    p_stats.set_defaults(func=cmd_stats)

    p_doctor = sub(
        "doctor",
        "check the installation and report what is wrong",
        """
        Run this first when something is not working. Checks the tools woswoar
        needs, the shell hook, file permissions, the identity and signing keys,
        and the state of the history repo -- and prints what to do about each
        failure rather than only that it failed.

        Changes nothing. With --prove it instead demonstrates, in a throwaway
        sandbox that never touches your real history, that a recorded command
        reaches the remote unreadable: see docs/verify.md.
        """,
    )
    p_doctor.add_argument(
        "--prove",
        action="store_true",
        help="record a canary in a sandbox, sync it, and prove nothing readable was published",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub(
        "init",
        "create or join an encrypted history repo",
        """
        Run once per machine, with the git URL of a repository you own -- an
        empty GitHub repo, a bare repo on a NAS, a folder on a USB stick. There
        is no server and no account.

        It enrols this machine, does the first sync, and tells you the one
        remaining step. On the second and later machines that step is
        `woswoar accept`, run on a machine you already use.
        """,
    )
    p_init.add_argument("remote", nargs="?", help="git URL of the history repo")
    p_init.add_argument(
        "--new-identity",
        action="store_true",
        help="generate a dedicated age key instead of reusing an SSH key",
    )
    p_init.add_argument("--identity", help="use this private key")
    p_init.add_argument(
        "--no-sync",
        action="store_true",
        help="join the repo but do not exchange history yet",
    )
    p_init.set_defaults(func=cmd_init)

    p_sync = sub(
        "sync",
        "exchange history with the remote",
        """
        Publishes this machine's new commands and merges everyone else's. Safe
        to run at any time and safe to run concurrently; normally a systemd
        timer runs it once a minute.

        Only this machine's own commands are ever published from here.
        """,
    )
    p_sync.add_argument("--no-push", action="store_true", help="stay local; do not contact remote")
    p_sync.set_defaults(func=cmd_sync)

    p_grant = sub(
        "grant",
        "let newly enrolled machines read the older history",
        """
        Answers one question: who may READ what is already here. It re-seals
        the small per-day keys to every enrolled machine, so they can decrypt
        days recorded before they existed. The history itself is not rewritten.

        Widens access to everything, so it lists the machines and asks first.
        Run it once, on any machine that can already read the history.

        Most of the time you want `woswoar accept`, which does this and the
        other half together. Reach for `grant` on its own to let a machine read
        the history WITHOUT this machine believing what that machine publishes
        -- sharing a repository with someone else, rather than adding your own
        laptop.
        """,
    )
    p_grant.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_grant.set_defaults(func=cmd_grant)

    p_revoke = sub(
        "revoke",
        "withdraw a machine's access to history recorded from now on",
        """
        Permanent, and deliberately so: there is no un-revoke. Every other
        machine stops accepting what the revoked one publishes, automatically,
        at its next sync.

        Three things it cannot do. It cannot make the revoked machine forget
        history it has already read; it cannot recall what it already
        published; and it cannot re-key days it could already open. Take the
        machine's own copy seriously.

        Run `woswoar sync` before revoking if you still want the history that
        machine published and yours have not merged yet.
        """,
    )
    p_revoke.add_argument(
        "fingerprint",
        help="the fingerprint 'woswoar grant' shows, or an unambiguous prefix of it",
    )
    p_revoke.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_revoke.set_defaults(func=cmd_revoke)

    p_trust = sub(
        "trust",
        "accept another machine's published history on this machine",
        """
        Answers the other question: whose new history does THIS machine
        believe. Every machine signs what it publishes, and each of yours
        decides for itself whose signature it accepts.

        Local only. Nothing is written to the repository and nothing is
        published -- which is the point, because the repository is exactly what
        this decision defends against: anyone who can push to it can rewrite
        what it claims. So it has to be run on each machine that will read the
        new one.

        Most of the time you want `woswoar accept`. Use `--replace` for a
        machine whose signing key CHANGED, which `accept` refuses on purpose:
        that is either a machine you re-enrolled or someone rewriting the
        repository, and nothing can tell those apart -- you can.
        """,
    )
    p_trust.add_argument(
        "--replace",
        action="store_true",
        help="accept a new signing key for a machine already accepted",
    )
    p_trust.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_trust.set_defaults(func=cmd_trust)

    p_accept = sub(
        "accept",
        "add a machine you own: 'grant' and 'trust' in one step",
        """
        Run this on each machine you already use, after `woswoar init` on the
        new one. It does both halves of adding a machine:

          read     the new machine may read your entire history, including
                   days recorded before it existed. Published, so it applies
                   everywhere, and it cannot be taken back for what has already
                   been read.
          believe  this machine accepts what the new one publishes. Local only,
                   so every other machine of yours needs telling separately.

        Shows both keys and asks first. A name is free text written by whoever
        added the key; the fingerprints are not, so check those on the machine
        they belong to.

        It will not accept a CHANGED signing key -- see `woswoar trust
        --replace`.
        """,
    )
    p_accept.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_accept.set_defaults(func=cmd_accept)

    p_compact = sub(
        "compact",
        "merge old chunks to reduce the file count",
        """
        Each sync writes a small encrypted chunk, so a busy machine accumulates
        a lot of files. This merges each past day's chunks into one.

        Only days before today, and only this machine's own. Nothing is lost:
        the plaintext in logs/ is untouched, and the old chunks stay in git
        history like every other commit.
        """,
    )
    p_compact.add_argument("--before", help="only days before this YYYY-MM-DD (default: today)")
    p_compact.set_defaults(func=cmd_compact)

    return parser


def main(argv: list[str] | None = None) -> int:
    from . import progress

    args = build_parser().parse_args(argv)
    try:
        # Around every command, not only the slow ones. What a command costs
        # depends on how much history is behind it, and the reporter shows
        # nothing until a wait has already gone on too long -- so the ones that
        # are fast today stay silent, without anyone having to predict which
        # those are.
        with progress.to_terminal():
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
