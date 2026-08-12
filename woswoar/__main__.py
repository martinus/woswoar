"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, cache, importer, search, store
from .entry import make_inert
from .errors import WoswoarError

if TYPE_CHECKING:  # `sync` is imported lazily; only the annotation needs it.
    from .sync import Failure, Reader, ReencryptReport

#: Both "no repo key yet" and "some days unreadable" have the same fix, and
#: used to say so in two independently worded paragraphs.
_GRANT_REMEDY = (
    "On a machine that is already enrolled run:\n"
    "    woswoar accept\n"
    "then sync here again. Nothing is lost in the meantime."
)

#: The hook each shell sources, and the file each shell reads at the start of an
#: interactive session. Two hooks rather than one that branches: the bash one is
#: built on bash 5 builtins and the zsh one on zsh's, and the interesting half of
#: either is what its shell does *not* offer.
#:
#: Ordered, because it decides the order things are printed and installed in, and
#: a report whose lines move around between runs is harder to diff than it needs
#: to be.
HOOKS = {"bash": "woswoar.bash", "zsh": "woswoar.zsh"}
RCFILES = {"bash": ".bashrc", "zsh": ".zshrc"}

#: What each shell must be able to do, and how to ask it. bash 5 for
#: $EPOCHSECONDS and $EPOCHREALTIME; zsh 5 for `zsh/datetime` and
#: `add-zsh-hook`. Each hook enforces its own floor and switches itself off
#: below it -- this is how `doctor` says the same thing before you find out.
_VERSION_QUERY = {
    "bash": "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}",
    "zsh": "echo $ZSH_VERSION",
}
_VERSION_FLOOR = 5

_BEGIN = "# >>> woswoar >>>"
_END = "# <<< woswoar <<<"
_BLOCK = re.compile(re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?", re.DOTALL)


def rcfile_for(shell: str) -> Path:
    return Path.home() / RCFILES[shell]


def installed_shells() -> list[str]:
    """Every shell this machine has a hook copied for.

    The hooks in the data directory, not what is on PATH and not what
    `.bashrc` says: `install` writing the file is what makes a shell one woswoar
    is responsible for, and it is the same fact `_refresh_hook` acts on.
    """
    return [shell for shell in HOOKS if (store.data_dir() / HOOKS[shell]).is_file()]


def detect_shells() -> list[str]:
    """The shells `install` writes to when nobody said which.

    **Every shell whose rc file already exists**, which is the rule that decides
    the two things this must not do. It must not need a flag from someone who
    uses both shells -- `install` already edits `.bashrc` without being asked,
    so an existing `.zshrc` is no different. And it must not *create* a
    `~/.zshrc` for someone who has never run zsh, which is what "already exists"
    is doing here and why this is not simply "write both".

    `$SHELL` is the fallback and deliberately not the detector: it names the
    login shell, and someone whose login shell is zsh but who works in bash
    would get the hook in the shell they are not typing into. It only decides
    the case where there is no rc file to go on at all -- a container, a fresh
    account -- and there bash is the last resort, because it is the shell whose
    hook has been shipped longest.
    """
    present = [shell for shell in HOOKS if rcfile_for(shell).is_file()]
    if present:
        return present
    login = Path(os.environ.get("SHELL", "")).name
    return [login] if login in HOOKS else ["bash"]


def _hook_bytes(shell: str) -> bytes:
    """The hook as this version ships it.

    The bytes rather than a path, which is what replaced an `as_file` dance
    here. `as_file` hands back a *temporary* path for a zipped install and
    deletes it when its context exits, so the previous version of this returned
    a path that no longer existed and `install` copied from it. Nothing noticed,
    because nobody installs woswoar as a zipapp -- but `doctor` now compares
    what is installed against what is packaged, and one function returning one
    thing is what makes that comparison true by construction rather than by
    hoping two readers agree.

    Beside this file first, and `importlib.resources` only if that is not
    there. `resources` costs a measured 8.7 ms to import -- a fifth of an idle
    sync, which now runs once a minute and calls this every time -- while the
    package layout puts the hook next to this module in every install that has
    a filesystem at all, wheel or editable. The fallback is for a zipapp, where
    `__file__` is a path inside the archive and nothing can open it.
    """
    beside = Path(__file__).resolve().parent / "shell" / HOOKS[shell]
    if beside.is_file():
        return beside.read_bytes()

    from importlib import resources

    return (resources.files("woswoar") / "shell" / HOOKS[shell]).read_bytes()


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


def shells_from(args: argparse.Namespace) -> list[str]:
    """Which shells this invocation is about.

    `--shell` wins outright. Otherwise `--rcfile` decides, because a file named
    `.zshrc` is not an ambiguous thing to be handed: taking the *detected* shell
    there would let someone with a `.bashrc` beside it get the bash hook sourced
    from their zsh startup file, which loads nothing and says nothing. When the
    name settles nothing either -- `--rcfile /tmp/rc` -- detection has the only
    other opinion available.
    """
    choice = getattr(args, "shell", None) or "auto"
    rcfile = getattr(args, "rcfile", None)
    if rcfile and choice == "both":
        # Refused rather than resolved, because both resolutions are wrong and
        # one of them is silent: writing two blocks into one file leaves only
        # the second, since each replaces the marked block the last one wrote --
        # so `--rcfile ~/.bashrc --shell both` would end with a `.bashrc`
        # sourcing the *zsh* hook.
        raise WoswoarError(
            "--rcfile names one file, so it cannot be combined with --shell both.\n"
            "Run install once per shell, or drop --rcfile to use each shell's own rc file."
        )
    if choice in HOOKS:
        return [choice]
    if choice == "both":
        return list(HOOKS)
    if rcfile:
        named = [shell for shell, name in RCFILES.items() if Path(rcfile).name == name]
        return named or detect_shells()[:1]
    return detect_shells()


def _write_block(rcfile: Path, target: Path) -> str:
    """Put the sourced line into ``rcfile``, and say what that did.

    Creating the file when it is absent is correct *here* and only here: a
    caller has already decided this shell is one to install for, either by
    detection -- which needs the file to exist -- or because it was named.
    """
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
        return "already current"
    rcfile.write_text(updated, encoding="utf-8")
    return action


def cmd_install(args: argparse.Namespace) -> int:
    """Set up machine identity, install each shell's hook, and wire up its rcfile."""
    machine = store.machine()
    store.private_dir(store.data_dir())

    shells = shells_from(args)
    targets = {}
    for shell in shells:
        target = store.data_dir() / HOOKS[shell]
        target.write_bytes(_hook_bytes(shell))
        targets[shell] = target
    # After the copies, not before: `write_bytes` creates at the ambient umask,
    # so hardening first would leave the files install itself writes as the ones
    # its own migration missed. This is also what re-tightens a tree from a
    # woswoar that predated owner-only directories -- `install` is the command
    # people re-run to upgrade.
    store.harden()

    print(f"machine : {machine.name} ({machine.id})")
    print(f"logs    : {store.logs_dir()}")
    for shell, target in targets.items():
        print(f"hook    : {target}" + (f"  ({shell})" if len(shells) > 1 else ""))

    # One `--rcfile` cannot mean two files, and `shells_from` has already
    # reduced to a single shell when it was given.
    override = Path(args.rcfile).expanduser() if getattr(args, "rcfile", None) else None
    for shell, target in targets.items():
        rcfile = override if override is not None else rcfile_for(shell)
        print(f"rcfile  : {rcfile} ({_write_block(rcfile, target)})")

    # One line per shell, never `source a b`: a second word there is not a
    # second file to read, it is `$1` for the first one. The pair only ever
    # differ by which shell you are standing in, so each is offered on its own.
    if len(targets) == 1:
        print("\nOpen a new shell, or run:  source", *targets.values())
    else:
        print("\nOpen a new shell. To pick it up in one already open:")
        for shell, target in targets.items():
            print(f"    source {target}   # in {shell}")

    # The moment someone finds out, so the moment to say it. Recording works
    # without any of these, which is why this warns rather than fails -- but
    # without fzf there is no Ctrl-R, and Ctrl-R is what woswoar is for.
    from . import deps

    absent = deps.missing()
    if absent:
        print(f"\n{deps.report(absent)}", file=sys.stderr)
    return 0


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


#: What `doctor` puts at the front of each line, plain and coloured.
#:
#: The plain forms are byte-for-byte what this printed before there was a
#: coloured one, and they are what everything that is not a person sees: the
#: suite asserts on `[FAIL] day keys`, and `woswoar doctor | grep FAIL` is a
#: reasonable thing to have in a script. A tick that only a terminal renders
#: must not become the thing those depend on.
#:
#: The coloured forms are one column wide rather than four or six, so the labels
#: line up -- which the plain `[ok]`/`[FAIL]` never have.
_PLAIN_MARKERS = {"ok": "[ok]", "fail": "[FAIL]", "info": "[--]"}
_COLOUR_MARKERS = {
    "ok": "\x1b[32m\u2714\x1b[0m",
    "fail": "\x1b[31m\u2718\x1b[0m",
    "info": "\x1b[2m\u00b7\x1b[0m",
}


def _markers(stream: object | None = None) -> dict[str, str]:
    """Coloured markers for a terminal, the plain ones for anything else.

    `NO_COLOR` is honoured because it costs one condition and someone always
    has a reason -- a terminal that renders neither colour nor the glyphs, a log
    being captured through a pty, a screen reader.
    """
    out = sys.stdout if stream is None else stream
    watched = bool(getattr(out, "isatty", lambda: False)())
    if watched and not os.environ.get("NO_COLOR"):
        return _COLOUR_MARKERS
    return _PLAIN_MARKERS


def cmd_doctor(args: argparse.Namespace) -> int:
    import shutil

    from . import crypto, deps, sync

    ok = True
    marker = _markers()

    def check(label: str, good: bool, detail: str) -> None:
        """A pass/fail condition: failing means something needs fixing."""
        nonlocal ok
        ok = ok and good
        print(f"{marker['ok' if good else 'fail']} {label:<12} {detail}")

    def info(label: str, detail: str) -> None:
        """Context that cannot fail, kept visually distinct from a real check."""
        print(f"{marker['info']} {label:<12} {detail}")

    if args.prove:
        from . import prove

        prove.run(check, info)
        if not ok:
            # Unlike the checks below, nothing here is the user's to fix: the
            # sandbox is built from scratch, so a FAIL can only be woswoar
            # publishing something it promised not to.
            print(
                "\nA FAIL above is a defect in woswoar itself, not in your setup."
                "\nPlease report it: https://github.com/martinus/woswoar/issues"
            )
        return 0 if ok else 1

    # The shells this machine has hooks for, or -- on one that has not run
    # `install` yet -- the ones it would get if it did. Reporting on every shell
    # woswoar *could* support instead would fail a perfectly good bash-only
    # machine for not having zsh.
    for shell in installed_shells() or detect_shells():
        version = _shell_version(shell)
        major = int(version.split(".")[0]) if version and version[0].isdigit() else 0
        check(
            shell,
            major >= _VERSION_FLOOR,
            f"{version or 'not found'} ({_VERSION_FLOOR}.0+ required)",
        )

    fzf = shutil.which("fzf")
    if fzf is None:
        check("fzf", False, f"not found - {deps.advice([deps.FZF])}")
    else:
        # Which keys this fzf can actually offer, not just that it is there.
        # An fzf below 0.45 gets a working picker with several keys missing --
        # correct, silent, and impossible to tell from a bug unless something
        # says so. Reported from a fleet running 0.73 on one machine and
        # Debian's 0.44.1 on another.
        #
        # The list comes from `search.GATED_KEYS` rather than being written out
        # here: this sentence and the bindings it describes are the same fact in
        # two modules, and it was already wrong once -- it still named two keys
        # after the preview pane became the third.
        said, _ = search.fzf_version()
        shown = said or "version unknown"
        if search.fzf_supports_transform():
            check("fzf", True, f"{fzf}  ({shown})")
        else:
            floor = ".".join(str(part) for part in search.TRANSFORM_SINCE)
            missing = ", ".join(search.GATED_KEYS[:-1]) + f" and {search.GATED_KEYS[-1]}"
            check(
                "fzf",
                True,
                f"{fzf}  ({shown} - searching works; {missing} need {floor}+)",
            )

    machine_file = store.machine_file()
    # Read before anything calls store.machine(), which would create it.
    has_machine = machine_file.is_file()
    check("machine", has_machine, str(machine_file))

    # `install` copies each hook into the data directory rather than sourcing it
    # out of the package, so upgrading woswoar leaves the old shell code running
    # -- and nothing said so. That was survivable while the hook only recorded;
    # syncing lives in it now, so a machine that never re-ran `install` silently
    # keeps whatever sync arrangement it had.
    stale = _stale_hooks()
    present = installed_shells()
    if not present:
        check("hook", False, f"no hook installed in {store.data_dir()}")
    for shell in present:
        hook = store.data_dir() / HOOKS[shell]
        if shell in stale:
            check("hook", False, f"{hook} is older than this woswoar - run 'woswoar install'")
        else:
            check("hook", True, str(hook))

    # One line per rc file, and per *installed* shell rather than per rc file
    # that exists: a `.zshrc` on a machine woswoar was only ever installed into
    # bash for is not a problem, and reporting it as one would send someone
    # looking for a block that is correctly absent.
    for shell in present or detect_shells():
        rcfile = rcfile_for(shell)
        sourced = rcfile.is_file() and _BEGIN in rcfile.read_text(encoding="utf-8")
        detail = "sources the hook" if sourced else "has no woswoar block"
        check(RCFILES[shell].lstrip("."), sourced, f"{rcfile} {detail}")

    # Not just "is age on PATH". A sandboxed age -- snap, flatpak, anything
    # confined -- answers `--version` perfectly and then cannot open a key,
    # which is a real failure that used to reach the user as an unexplained
    # "permission denied" from `init` with doctor reporting nothing at all.
    age_path = shutil.which("age")
    if age_path is None:
        info("age", f"not found, needed for 'woswoar sync' - {deps.advice([deps.AGE])}")
    else:
        failure = crypto.selftest()
        cost = crypto.startup_ms() if not failure else -1.0
        detail = age_path if cost < 0 else f"{age_path}  ({cost:.0f} ms to start)"
        check("age", not failure and 0 <= cost < crypto.SLOW_MS, detail)
        for line in failure.splitlines():
            print(f"     {line}")
        if not failure and cost >= crypto.SLOW_MS:
            # Not a broken age -- a slow one, which is worse to diagnose because
            # everything works. woswoar runs it about twice per day of history,
            # so this is the difference between a grant taking three seconds and
            # taking six minutes, and nothing on screen would say why.
            print(
                f"     age takes {cost:.0f} ms to start. woswoar runs it about twice per\n"
                f"     day of recorded history, so a year costs about "
                f"{cost * 2 * 365 / 1000:.0f} s per sync,\n"
                "     grant or accept that has work to do.",
            )
            if "/snap/" in age_path:
                print(
                    "     This one is a snap, which sets up a sandbox on every call.\n"
                    "     Install the distribution package or the release binary instead:\n"
                    f"       sudo snap remove age  &&  {deps.advice([deps.AGE])}"
                )
            else:
                print(
                    "     A distribution binary starts in a millisecond or two:\n"
                    f"       {deps.advice([deps.AGE])}"
                )

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

        # The one fact about the repository that decides whether this machine
        # may write to it at all, and the only place a person can see it: the
        # marker is a file nobody would think to `cat`, and `sync` mentions it
        # only by refusing. The verdict comes from `sync` rather than being
        # re-derived here, like every other line in this block.
        repo_format = sync.repo_format_status()
        check("repo format", repo_format.ok, repo_format.detail)
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

        # `sync` says this once, on the pass that first sees the file, and then
        # the day settles and it stops -- which is right for a one-minute timer
        # and no use to somebody asking afterwards. Costs no subprocess unless
        # there is something to find; see `unlisted_chunks`.
        planted = sync.unlisted_chunks()
        if planted:
            host, day, count = planted[0]
            detail = (
                f"{sum(n for _, _, n in planted)} chunk(s) in no signed list, e.g."
                f" {count} under {host[:8]}/{day} - no machine will read them,"
                " and this one did not write them"
            )
        else:
            detail = "all accounted for"
        check("chunks", not planted, detail)
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

    if not args.remote:
        return 0

    # The sync `init` used to tell you to run. Nothing about it is a separate
    # decision -- joining a repository and then not exchanging anything with it
    # is not a state anyone wants -- and leaving it to a second command meant
    # the machine published nothing until one was run. `--no-sync` is there for
    # the case where the remote is not reachable yet.
    if args.no_sync:
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
    from . import sync

    # Before the sync, not after: a remote that is unreachable must not be able
    # to keep the hook out of date, and these two have nothing to do with each
    # other beyond both being things this command is well placed to do.
    _refresh_hook()

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
        report = sync.run(push=not args.no_push)
    except Exception as exc:
        if not watched:
            sync.record_failure(str(exc) or exc.__class__.__name__)
        raise
    # Cleared whoever was watching: a sync that worked is proof the recorded
    # failure is stale, and someone who fixed the problem by hand should not
    # have to wait for a background run to stop being told about it.
    sync.clear_failure()
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
            "    woswoar accept",
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
            f"\n{len(report.foreign)} chunk(s) sit under this machine's own id that it\n"
            "never signed. They are inert: no machine will read them, including this\n"
            "one, and nothing was lost -- whatever was in them has been published again\n"
            "under a chunk that is signed.\n"
            "Two things look like this and cannot be told apart from here: a run of\n"
            "this machine that was killed between writing a chunk and signing for it,\n"
            "and someone else writing into the repository. A power cut is the ordinary\n"
            "explanation. 'woswoar doctor' lists them under 'chunks'.",
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

    _report_reseal(sync.grant(approved=[reader.key for reader in readers]))
    return 0


def _shell_version(shell: str) -> str:
    """What ``shell`` reports as its version, or "" if it cannot be asked.

    Asked of the binary rather than read from a package manager, because the
    thing that matters is the one that will source the hook. Every failure --
    not on PATH, refuses to start, times out -- collapses to "" and is reported
    as "not found": from `doctor`'s side those are one situation, and the
    remedy for all of them is to install the shell.
    """
    import shutil
    import subprocess

    binary = shutil.which(shell)
    if not binary:
        return ""
    try:
        out = subprocess.run(
            [binary, "-c", _VERSION_QUERY[shell]],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _stale_hooks() -> list[str]:
    """Every *installed* shell whose hook is some older woswoar's copy of it.

    Installed, so a missing hook is never stale: that is a different problem
    with a different fix, and `doctor` already has a line for it. It is also the
    whole of what keeps `_refresh_hook` from planting a zsh hook on a machine
    that has never asked for one.
    """
    return [
        shell
        for shell in installed_shells()
        if (store.data_dir() / HOOKS[shell]).read_bytes() != _hook_bytes(shell)
    ]


def _hook_is_stale() -> bool:
    return bool(_stale_hooks())


def _refresh_hook() -> bool:
    """Bring the installed hook up to this version, if it is behind.

    `install` copies the hook rather than sourcing it out of the package, so
    upgrading the program used to leave the old shell code running until
    somebody re-ran `install`. Since the hook now starts a `woswoar sync` about
    once a minute, that sync is the thing best placed to notice -- an upgrade
    heals itself within a minute and the reinstall step goes away.

    Deliberately *not* a symlink into the installed package, which was the first
    idea and is worse. That path contains the Python version
    (`.../lib/python3.14/site-packages/...`), so a distribution's Python bump or
    a `pipx reinstall` leaves it dangling -- and a dangling `source` prints an
    error at every shell start and switches recording off, where a merely stale
    hook still records and still searches. It would also make `doctor` report
    the packaged file's mode as an exposure, because its permission walk uses
    `stat`, which follows links.

    Only ever *re*-writes, and now that there are two hooks that rule is
    load-bearing rather than tidy. This runs unattended, from a background sync
    a prompt started. A version of it that *created* would put a `woswoar.zsh`
    on a bash-only machine at the first sync after an upgrade -- a file nothing
    sources, that nobody asked for, from a command nobody ran. `_stale_hooks`
    only ever names hooks that are already there, and
    `test_a_refresh_never_creates_a_second_hook` is what holds that.
    """
    refreshed = []
    for shell in _stale_hooks():
        hook = store.data_dir() / HOOKS[shell]
        # The mode was set when `install` created it and `write_bytes` truncates
        # rather than recreates, so it survives -- and the hook is public code
        # anyway. Failure is not fatal: a read-only data directory is a real
        # situation and it must not stop the sync this command exists for. Per
        # hook, so one unwritable file does not stop the other being fixed.
        try:
            hook.write_bytes(_hook_bytes(shell))
        except OSError:
            continue
        refreshed.append(hook)
    if not refreshed:
        return False
    # Shells already running keep the code they sourced, exactly as they do
    # after a hand-run `install`. New ones get this. Nothing to do about the
    # difference, and nothing anyone needs to do.
    for hook in refreshed:
        print(f"updated the shell hook at {hook} to {__version__}")
    return True


def _report_stale_hook() -> None:
    if not _hook_is_stale():
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


def _ask(question: str, default: str = "") -> str:
    """Free text, with a default shown. Empty answer takes the default."""
    shown = f" [{default}]" if default else ""
    return input(f"{question}{shown}: ").strip() or default


def _ask_yes(question: str, default: bool = True) -> bool:
    """A yes/no with a default, for questions that change nothing dangerous.

    Deliberately not `_confirm`: that one is a security control with an
    `isatty` branch and a fixed refusal message about widening access. These
    are "shall I install the shell hook" -- reusing the access gate for them
    would blunt the thing it is for.
    """
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


#: What `setup` offers to import, in the order it offers them. `importer` owns
#: where each one lives; this only asks whether it is there.
def _importable() -> list[tuple[str, Path, int]]:
    """``(kind, path, size)`` for each history on this machine, largest first."""
    from . import importer

    found = []
    for kind in importer.KINDS:
        path = importer.atuin_db() if kind == "atuin" else importer.default_path(kind)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size:
            found.append((kind, path, size))
    return sorted(found, key=lambda item: item[2], reverse=True)


def _untouched() -> bool:
    """Is there nothing here at all yet?

    Three cheap questions, and it takes any of them as a yes: the shell hook,
    a history repo, or a single recorded line. Not "has `install` run" -- a
    machine that imported a history without wiring the hook, or joined a repo
    without it, is set up enough to be told where it stands, and running `setup`
    at it would be answering a question it did not ask.

    `store.machine_file` is deliberately not one of them: `store.machine()`
    creates it, so almost any command makes it exist and it says nothing about
    whether anyone has set anything up.
    """
    from . import sync

    if installed_shells() or sync.is_repo():
        return False
    return not any(store.iter_log_files())


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
    from . import cache, sync

    if _untouched():
        print("Nothing installed here yet -- setting up.\n")
        return cmd_setup(argparse.Namespace(rcfile=None, shell=None))

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
    """Walk a fresh machine through the whole thing, asking as it goes."""
    from . import deps

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
        if not _ask_yes("     Carry on without them?", default=False):
            return 1
    else:
        print("     everything woswoar needs is installed")

    print("\n2/4  Shell hook")
    # Named before it is written, because it is now more than one file and the
    # rule that chose them is not obvious from the output alone.
    chosen = shells_from(args)
    print(f"     {' and '.join(chosen)} -- {_why_those_shells(args, chosen)}")
    cmd_install(argparse.Namespace(rcfile=args.rcfile, shell=getattr(args, "shell", None)))

    print("\n3/4  Existing history")
    _offer_imports()

    print("\n4/4  Sync with your other machines")
    return _offer_remote(args)


def _why_those_shells(args: argparse.Namespace, chosen: list[str]) -> str:
    """One clause saying which rule picked `chosen`, for `setup`'s step 2.

    Worth a line because the answer differs per machine and the wrong one is
    silent: a hook written into the shell you do not use looks exactly like a
    hook that works.
    """
    if getattr(args, "shell", None) and args.shell != "auto":
        return f"--shell {args.shell}"
    if getattr(args, "rcfile", None):
        return f"from the name of {Path(args.rcfile).name}"
    if any(rcfile_for(shell).is_file() for shell in chosen):
        return "found " + " and ".join(f"~/{RCFILES[shell]}" for shell in chosen)
    return f"no rc file here, so $SHELL ({os.environ.get('SHELL', 'unset')})"


def _offer_imports() -> None:
    """Offer each history found on this machine, one at a time."""
    found = _importable()
    if not found:
        print("     nothing to import (no bash, zsh or atuin history found)")
        return

    for kind, path, size in found:
        if not _ask_yes(f"     Import {kind} history from {path} ({size / 1e6:.1f} MB)?"):
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
            this_host_only = _ask_yes("       Import only this machine's history?")
        cmd_import(
            argparse.Namespace(
                kind=kind, file=str(path), dry_run=False, this_host_only=this_host_only
            )
        )


def _offer_remote(args: argparse.Namespace) -> int:
    """Join a history repo, or finish without one."""
    from . import sync

    if sync.is_repo():
        print("     already joined a history repo; syncing")
        return cmd_sync(argparse.Namespace(no_push=False))

    print(
        "     woswoar syncs through an ordinary git repository you own -- an\n"
        "     empty GitHub repo, a bare repo on a NAS, a folder on a USB stick.\n"
        "     There is no server and no account. Leave blank to stay on this\n"
        "     machine only; you can run 'woswoar init <url>' any time later."
    )
    remote = _ask("     Repository URL")
    if not remote:
        print("\n     Staying local. Open a new shell and press Ctrl-R.")
        return 0

    # Through `cmd_init`, so the sequence has one source of truth: whatever it
    # prints as the next step is what someone following this reads, and there
    # is no second copy here to disagree with it.
    return cmd_init(
        argparse.Namespace(remote=remote, new_identity=False, identity=None, no_sync=False)
    )


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
    p_setup.add_argument("--rcfile", help="file to modify (default: the rc file of each shell)")
    p_setup.add_argument(
        "--shell",
        choices=["auto", *HOOKS, "both"],
        default="auto",
        help="which shell(s) to install for (default: auto -- every shell whose "
        "rc file already exists, or $SHELL when there is none)",
    )
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
    p_install.add_argument("--rcfile", help="file to modify (default: the rc file of each shell)")
    p_install.add_argument(
        "--shell",
        choices=["auto", *HOOKS, "both"],
        default="auto",
        help="which shell(s) to install for (default: auto -- every shell whose "
        "rc file already exists, or $SHELL when there is none)",
    )
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
