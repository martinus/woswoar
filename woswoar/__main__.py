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
    print(f"{result.source}: {result.parsed} parsed, {prefix} {result.imported}", end="")
    print(f", {', '.join(notes)}" if notes else "")

    if len(result.per_host) > 1:
        print("\nper machine:")
        width = max(len(name) for name, _ in result.per_host)
        for name, count in result.per_host:
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
    labels = {host: names.get(host, host) for host, _ in per_host.most_common()}
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
        status = sync.repo_key_status(store.machine())
        check("repo key", status.ok, status.detail)
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
    from . import sync

    known, identity = sync.initialise(
        remote=args.remote,
        new_identity=args.new_identity,
        identity=Path(args.identity).expanduser() if args.identity else None,
    )
    print(f"machine  : {known.name} ({known.id})")
    print(f"identity : {identity}")
    print(f"repo     : {store.history_dir()}")
    print(f"remote   : {sync.remote_summary()}")
    print(f"\nRecipients now enrolled ({store.recipients_file()}):")
    for kind, key in sync.list_recipients():
        print(f"  {kind} {key[:24]}...")
    if args.remote:
        print("\nNext: 'woswoar sync'.")
        print("On a machine that already has access, run 'woswoar grant' so this")
        print("one can read history sealed before it joined.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from . import sync

    report = sync.run(push=not args.no_push)
    if report.needs_grant:
        print(
            "This machine cannot publish or read history yet: the repo's\n"
            f"authentication key was sealed before it enrolled.\n{_GRANT_REMEDY}\n"
            "Commands are still being recorded locally and will be published in\n"
            "full once this is done.",
            file=sys.stderr,
        )
        return 0
    print(
        f"exported {report.lines_exported} line(s) in {report.chunks_written} chunk(s); "
        f"merged {report.lines_imported} line(s) from {report.chunks_merged} chunk(s) "
        f"across {len(report.hosts_seen)} other host(s)"
    )
    if report.pushed:
        print("pushed to remote")
    elif not sync.has_remote():
        print("no remote configured - history is local only")

    if report.unreadable:
        days = len(report.unreadable)
        print(
            f"\n{days} day(s) of history are sealed to recipients that do not include\n"
            f"this machine - it joined after they were written.\n{_GRANT_REMEDY}",
            file=sys.stderr,
        )

    if report.unauthenticated:
        print(
            f"\nWARNING: {len(report.unauthenticated)} day(s) of history could not be\n"
            "authenticated and were refused. Every chunk carries a tag computed with\n"
            "a key only your enrolled machines can open, so this means the repo\n"
            "contains history none of them wrote - someone else can write to it.",
            file=sys.stderr,
        )
    return 0


def _show_readers(readers: list[Reader], mine: str, duplicated: set[str]) -> None:
    """One line per machine: fingerprint first, then the name.

    The fingerprint leads because it is the only part of the line the repo
    cannot choose. A label is printed with `repr`, so leading spaces, a tab, or
    anything Python considers unprintable -- a bidi override, say -- shows up as
    an escape instead of rearranging the line it is on.
    """
    for reader in readers:
        notes = []
        if reader.key == mine:
            notes.append("this machine")
        if reader.label in duplicated:
            notes.append("SAME NAME AS ANOTHER KEY")
        suffix = f"   ({', '.join(notes)})" if notes else ""
        print(f"  {reader.fingerprint}  {reader.label!r}{suffix}")


def cmd_grant(args: argparse.Namespace) -> int:
    """Let every enrolled machine read the whole history."""
    from . import crypto, sync

    readers = sync.readers()
    if not readers:
        print("no machines enrolled yet; run 'woswoar init <url>' first", file=sys.stderr)
        return 1

    try:
        mine = crypto.recipient_for(sync.identity_path(store.machine())).strip()
    except (WoswoarError, OSError):
        mine = ""

    # Two keys may legitimately share a name -- two machines really can both be
    # called `martin@laptop`. Saying so is the point: it is also what a key
    # added by someone else looks like, and the fingerprints then differ.
    counts = Counter(reader.label for reader in readers)
    duplicated = {label for label, count in counts.items() if count > 1}

    new = [reader for reader in readers if reader.is_new]
    known = [reader for reader in readers if not reader.is_new]

    if new:
        print(f"{len(new)} machine(s) NOT yet granted. Granting lets each of them read")
        print("your ENTIRE history, including days recorded before it ever existed:\n")
        _show_readers(new, mine, duplicated)
        if known:
            print(f"\nAlready granted, and unchanged ({len(known)}):\n")
            _show_readers(known, mine, duplicated)
    else:
        print(f"No machine is new since you last granted. Re-sealing to the same {len(readers)}:\n")
        _show_readers(readers, mine, duplicated)

    # Named for the kinds actually listed. A blanket ssh-keygen line is wrong
    # advice on a fleet that uses `--new-identity` everywhere, and advice that
    # does not fit what is on screen is advice nobody follows.
    kinds = {reader.fingerprint.startswith("SHA256:") for reader in readers}
    checks = [
        command
        for is_ssh, command in (
            (True, "ssh-keygen -lf ~/.ssh/id_ed25519.pub"),
            (False, "age-keygen -y ~/.config/woswoar/identity"),
        )
        if is_ssh in kinds
    ]
    print(
        "\nThe name beside a key is free text written by whoever added it, so check"
        f"\nthe key itself on the machine it belongs to:  {'  or  '.join(checks)}"
        "\n\nGranting re-seals the small per-day keys -- not the history itself -- and"
        "\npublishes them. Nothing is decrypted, and nothing else is re-uploaded."
    )

    # Only additions are put to a human. Re-sealing to a set that was already
    # approved widens nothing, and a prompt that fires when there is nothing to
    # decide is a prompt people learn to answer without reading.
    if new and not args.yes:
        if not sys.stdin.isatty():
            print(
                "\nRefusing to grant access without confirmation. "
                "Re-run with --yes if you mean it.",
                file=sys.stderr,
            )
            return 1
        if input(f"\nGrant {len(new)} new machine(s) full access? [y/N] ").strip().lower() not in (
            "y",
            "yes",
        ):
            print("Nothing changed.")
            return 1

    report = sync.grant(confirmed=[reader.key for reader in readers])
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


def cmd_compact(args: argparse.Namespace) -> int:
    from . import sync

    days, replaced = sync.compact(before=args.before or time.strftime("%Y-%m-%d"))
    if not days:
        print("nothing to compact")
    else:
        print(f"compacted {replaced} chunk(s) into {days} (one per day)")
        print("Run 'woswoar sync' to publish. Note this rewrites history.")
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
