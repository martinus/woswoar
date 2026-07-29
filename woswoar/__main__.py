"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from . import __version__, cache, search, store
from .errors import WoswoarError

#: Mirrors ``importer.KINDS``. Spelled out so building the parser -- which every
#: Ctrl-R does -- need not import the importer, and with it sqlite3: importing
#: this module drops from 26.6 to 24.6 ms. That is a small share of the ~105 ms
#: search path, and the same lazy-import reasoning already applies to `sync`.
#: Pinned by a test, the way the hook's copy of MAX_CMD_CHARS is.
IMPORT_KINDS = ("bash", "zsh", "atuin")

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


def cmd_install(args: argparse.Namespace) -> int:
    """Set up machine identity, install the hook, and wire up .bashrc."""
    import shutil

    machine = store.machine()
    target = store.data_dir() / HOOK_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_hook_source(), target)

    print(f"machine : {machine.name} ({machine.id})")
    print(f"logs    : {store.logs_dir()}")
    print(f"hook    : {target}")

    rcfile = Path(args.rcfile).expanduser() if args.rcfile else Path.home() / ".bashrc"
    block = f'{_BEGIN}\nsource "{target}"\n{_END}\n'

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
    from . import importer

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

    from . import sync

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
    check("fzf", fzf is not None, fzf or "not found - needed for 'woswoar search'")

    machine_file = store.config_dir() / "machine"
    check("identity", machine_file.is_file(), str(machine_file))

    hook = store.data_dir() / HOOK_NAME
    check("hook", hook.is_file(), str(hook))

    rcfile = Path.home() / ".bashrc"
    sourced = rcfile.is_file() and _BEGIN in rcfile.read_text(encoding="utf-8")
    detail = "sources the hook" if sourced else "has no woswoar block"
    check("bashrc", sourced, f"{rcfile} {detail}")

    info("age", shutil.which("age") or "not found - needed for 'woswoar sync' only")

    if sync.is_repo():
        status = sync.identity_status(store.machine())
        check("sync", status.ok, status.detail)
        info("remote", sync.remote_summary())
    else:
        info("sync", "no history repo - run 'woswoar init <url>' to sync machines")

    logs = list(store.iter_log_files())
    info("logs", f"{len(logs)} file(s) in {store.logs_dir()}")

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
        print("On an already-enrolled machine, run 'woswoar reencrypt' so this")
        print("one can read history sealed before it joined.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from . import sync

    report = sync.run(push=not args.no_push)
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
            "this machine - it joined after they were written. On a machine that was\n"
            "already enrolled run:\n"
            "    woswoar reencrypt\n"
            "then sync here again. Nothing is lost in the meantime.",
            file=sys.stderr,
        )
    return 0


def cmd_reencrypt(args: argparse.Namespace) -> int:
    from . import sync

    report = sync.reencrypt()
    print(f"re-sealed {report.resealed} key file(s) to the current recipients")
    if report.pushed:
        print("pushed to remote")
    else:
        print("no remote configured; nothing published")
    if report.skipped:
        # Almost always means this machine is the new one. Saying so beats
        # letting "re-sealed 0 key files" read as success.
        print(
            f"\n{report.skipped} key file(s) could not be opened by this machine, so\n"
            "they were left alone. Re-sealing a key means opening it first, which\n"
            "only a machine that was already a recipient can do. Run this on one of\n"
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
    p_import.add_argument("kind", choices=IMPORT_KINDS)
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

    p_reencrypt = subparsers.add_parser(
        "reencrypt", help="re-seal keys after enrolling a new machine"
    )
    p_reencrypt.set_defaults(func=cmd_reencrypt)

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
