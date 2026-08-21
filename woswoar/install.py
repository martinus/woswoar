"""Installing the shell hook, and keeping it current.

Everything about which shells woswoar is responsible for on this machine, what
their hooks should contain, and how the rc files find them. It decides; the CLI
prints. Nothing here writes to a stream.

That mattered enough to be worth a module. `refresh_hook` is **policy that runs
unattended**: the hook starts a `woswoar sync` about once a minute, and that sync
rewrites the hook file when it is behind. Its rule -- only ever *re*-write, never
create -- was held by one test asserting on the CLI's stdout, which is a thin
thread for the one function here that edits a file nobody asked it to.

The `Check` producers are here for the reason `doctor` records: they are the
installer's judgements, and they lived in `__main__` only because the installer
did. `cmd_doctor` splices `shell_checks` and `hook_checks` into the middle of its
report, because the shell version leads and the rc file comes between `machine`
and `age`.

`hook_bytes` is deliberately one function returning bytes, and must stay that
way -- see its docstring for the `as_file` trap that shape exists to close.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import NamedTuple

from . import store
from .errors import WoswoarError
from .report import Check
from .store import Machine

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

BEGIN = "# >>> woswoar >>>"
_END = "# <<< woswoar <<<"
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(_END) + r"\n?", re.DOTALL)

#: The line inside the block that loads the hook, as `write_block` writes it and
#: as a shell would accept it -- `.` is `source`, and the quotes are optional.
#: Only ever matched *within* a block, so it cannot pick up a `source` line
#: someone else's installer put in the same rc file.
_SOURCE_LINE = re.compile(r'^[ \t]*(?:source|\.)[ \t]+"?(?P<path>[^"\n]+?)"?[ \t]*$', re.M)


def rcfile_for(shell: str) -> Path:
    return Path.home() / RCFILES[shell]


def installed_shells() -> list[str]:
    """Every shell this machine has a hook copied for.

    The hooks in the data directory, not what is on PATH and not what
    `.bashrc` says: `install` writing the file is what makes a shell one woswoar
    is responsible for, and it is the same fact `refresh_hook` acts on.
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


def hook_bytes(shell: str) -> bytes:
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


def shells_from(choice: str | None, rcfile: str | None) -> list[str]:
    """Which shells this invocation is about.

    `--shell` wins outright. Otherwise `--rcfile` decides, because a file named
    `.zshrc` is not an ambiguous thing to be handed: taking the *detected* shell
    there would let someone with a `.bashrc` beside it get the bash hook sourced
    from their zsh startup file, which loads nothing and says nothing. When the
    name settles nothing either -- `--rcfile /tmp/rc` -- detection has the only
    other opinion available.

    Two plain arguments rather than the `argparse.Namespace` this used to take.
    It read them with `getattr(args, "shell", None)`, so a caller that did not
    have the flag got a silent "auto" instead of an error -- the same thing #202
    is about, one layer down from the forged namespaces.
    """
    choice = choice or "auto"
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


def write_block(rcfile: Path, target: Path) -> str:
    """Put the sourced line into ``rcfile``, and say what that did.

    Creating the file when it is absent is correct *here* and only here: a
    caller has already decided this shell is one to install for, either by
    detection -- which needs the file to exist -- or because it was named.
    """
    block = f'{BEGIN}\nsource "{portable_hook_path(target)}"\n{_END}\n'
    existing = rcfile.read_text(encoding="utf-8") if rcfile.is_file() else ""
    if _BLOCK.search(existing):
        # A *function* replacement, because the second argument to `sub` is a
        # template and `block` holds a filesystem path the user controls through
        # `WOSWOAR_DIR` or `XDG_DATA_HOME`. A backslash in it is read as an
        # escape: `\i` raises `re.error` and `\1` substitutes a group from the
        # pattern, writing a `source` line that points somewhere else and
        # reporting success. Only the *second* install reaches this, since the
        # first appends through the branch below -- so it worked once and then
        # broke, or worked once and then lied.
        #
        # A lambda rather than `block.replace("\\", "\\\\")`: escaping is
        # correct and is one refactor away from being lost, while a callable
        # cannot be template-parsed at all. `re.escape` is the reflex and is the
        # wrong tool -- it escapes for a *pattern*, and would put literal
        # backslashes into the rc file.
        updated = _BLOCK.sub(lambda _: block, existing)
        action = "updated"
    else:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        updated = f"{existing}{separator}\n{block}"
        action = "added"

    if updated == existing:
        return "already current"
    rcfile.write_text(updated, encoding="utf-8")
    return action


def shell_version(shell: str) -> str:
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


def stale_hooks() -> list[str]:
    """Every *installed* shell whose hook is some older woswoar's copy of it.

    Installed, so a missing hook is never stale: that is a different problem
    with a different fix, and `doctor` already has a line for it. It is also the
    whole of what keeps `refresh_hook` from planting a zsh hook on a machine
    that has never asked for one.
    """
    return [
        shell
        for shell in installed_shells()
        if (store.data_dir() / HOOKS[shell]).read_bytes() != hook_bytes(shell)
    ]


def hook_is_stale() -> bool:
    return bool(stale_hooks())


def refresh_hook() -> list[Path]:
    """Bring the installed hook up to this version, if it is behind.

    Returns the hooks it rewrote, and prints nothing: this runs unattended from a
    background sync, and what to say about it is the caller's -- `cmd_sync` is
    the only one that has a person in front of it.

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
    sources, that nobody asked for, from a command nobody ran. `stale_hooks`
    only ever names hooks that are already there, and
    `test_a_refresh_never_creates_a_second_hook` is what holds that.
    """
    refreshed = []
    for shell in stale_hooks():
        hook = store.data_dir() / HOOKS[shell]
        # The mode was set when `install` created it and `write_bytes` truncates
        # rather than recreates, so it survives -- and the hook is public code
        # anyway. Failure is not fatal: a read-only data directory is a real
        # situation and it must not stop the sync this command exists for. Per
        # hook, so one unwritable file does not stop the other being fixed.
        try:
            hook.write_bytes(hook_bytes(shell))
        except OSError:
            continue
        # Shells already running keep the code they sourced, exactly as they do
        # after a hand-run `install`. New ones get this. Nothing to do about the
        # difference, and nothing anyone needs to do.
        refreshed.append(hook)
    return refreshed


def shell_checks() -> list[Check]:
    """The installed shells and their versions.

    The shells this machine has hooks for, or -- on one that has not run
    `install` yet -- the ones it would get if it did. Reporting on every shell
    woswoar *could* support instead would fail a perfectly good bash-only
    machine for not having zsh.
    """
    out = []
    for shell in installed_shells() or detect_shells():
        version = shell_version(shell)
        major = int(version.split(".")[0]) if version and version[0].isdigit() else 0
        out.append(
            Check(
                shell,
                f"{version or 'not found'} ({_VERSION_FLOOR}.0+ required)",
                ok=major >= _VERSION_FLOOR,
            )
        )
    return out


def hook_checks() -> list[Check]:
    """Whether each installed hook is current, and whether its rc file loads it.

    `install` copies each hook into the data directory rather than sourcing it
    out of the package, so upgrading woswoar leaves the old shell code running
    -- and nothing said so. That was survivable while the hook only recorded;
    syncing lives in it now, so a machine that never re-ran `install` silently
    keeps whatever sync arrangement it had.
    """
    stale = stale_hooks()
    present = installed_shells()
    out = []
    if not present:
        out.append(Check("hook", f"no hook installed in {store.data_dir()}", ok=False))
    for shell in present:
        hook = store.data_dir() / HOOKS[shell]
        if shell in stale:
            out.append(
                Check(
                    "hook",
                    f"{hook} is older than this woswoar - run 'woswoar install'",
                    ok=False,
                )
            )
        else:
            out.append(Check("hook", str(hook), ok=True))

    # One line per rc file, and per *installed* shell rather than per rc file
    # that exists: a `.zshrc` on a machine woswoar was only ever installed into
    # bash for is not a problem, and reporting it as one would send someone
    # looking for a block that is correctly absent.
    out += [rcfile_check(shell) for shell in present or detect_shells()]
    return out


def _sourced_path(rcfile: Path) -> str | None:
    """What ``rcfile``'s woswoar block tells the shell to load, verbatim.

    ``None`` when there is no block, and the empty string when there is one that
    sources nothing -- three states, because `doctor` says something different
    about each, and collapsing them into "is there a block" was how it came to
    say nothing useful about any of them.

    The search is anchored *inside* the block, which is the whole reason the
    marked block exists: an rc file is full of `source` lines, and only ours is
    ours to judge.
    """
    text = rcfile.read_text(encoding="utf-8") if rcfile.is_file() else ""
    block = _BLOCK.search(text)
    if not block:
        return None
    line = _SOURCE_LINE.search(block.group())
    return line.group("path") if line else ""


def _expanded(raw: str) -> Path:
    """The file a shell would actually open for ``raw``.

    `os.path.expandvars` rather than the `$HOME` and `${HOME}` this file's own
    `portable_hook_path` writes, and that generality is the point: a
    hand-written block spelling the same file through `$XDG_DATA_HOME`, or a
    dotfiles repo that keeps its own variable, works in the shell and must not
    be reported as broken. A name that is *not* set expands to itself, so it
    still fails to match the hook -- which is the safe direction, since what
    `doctor` can honestly say is "I cannot see that this loads the hook".
    """
    return Path(os.path.expandvars(raw)).expanduser()


def rcfile_check(shell: str) -> Check:
    """Whether this shell's rc file loads *this machine's* hook.

    That the block was there used to be the whole test, and it is the wrong
    question: the marker says somebody ran `install` once, not that what they
    installed is still where the line points. A `.bashrc` was found here
    sourcing `/tmp/woswoar-test-.../woswoar.bash` -- a throwaway directory from
    a test run, deleted the moment that run ended. Every interactive shell
    printed `No such file or directory`, nothing was recorded for months, and
    `doctor` reported a green `bashrc ... sources the hook` throughout. It is
    the one line of the report that a person checks *because* their history
    stopped working.

    Compared as paths rather than by reading the file, because the failure is
    about where the line points: a hook that is missing, empty or from an older
    woswoar is a different verdict with a different remedy, and `hook_checks`
    prints it directly above this line.
    """
    rcfile = rcfile_for(shell)
    label = RCFILES[shell].lstrip(".")
    raw = _sourced_path(rcfile)
    if raw is None:
        return Check(label, f"{rcfile} has no woswoar block", ok=False)
    if not raw:
        return Check(label, f"{rcfile} has a woswoar block that sources nothing", ok=False)
    hook = store.data_dir() / HOOKS[shell]
    # `realpath` on both sides, and never `samefile`: the whole point is a line
    # pointing at something that no longer exists, and `samefile` raises there.
    # It also settles a home directory reached through a symlink, which is every
    # macOS sandbox and some real setups.
    if os.path.realpath(_expanded(raw)) != os.path.realpath(hook):
        return Check(
            label,
            f"{rcfile} sources {raw}, not the installed hook - run 'woswoar install'",
            ok=False,
        )
    return Check(label, f"{rcfile} sources the hook", ok=True)


class Installed(NamedTuple):
    """Where this machine's hooks are, and whose machine it is.

    A value rather than the prints that used to be interleaved with the work.
    `setup` shows the same install as `woswoar install` does, and before this it
    got there by forging an `argparse.Namespace` and calling the CLI command --
    so the two could only ever agree by both going through argparse.
    """

    machine: Machine
    #: shell -> the hook copied into the data directory. Ordered as installed,
    #: which is the order it is reported in.
    hooks: dict[str, Path]


def install_hooks(shells: list[str]) -> Installed:
    """Copy each shell's hook into the data directory.

    Half of an install. `wire_rcfiles` is the other half and is deliberately a
    second call: the caller reports the hooks between the two, so an rc file that
    cannot be written still leaves a person knowing the hook itself landed. That
    was the order the prints were in when this was one function in the CLI, and
    it is the useful one -- the hook is what records, and sourcing it by hand is
    a workaround someone can act on.
    """
    machine = store.machine()
    store.private_dir(store.data_dir())

    hooks = {}
    for shell in shells:
        target = store.data_dir() / HOOKS[shell]
        target.write_bytes(hook_bytes(shell))
        hooks[shell] = target
    # After the copies, not before: `write_bytes` creates at the ambient umask,
    # so hardening first would leave the files install itself writes as the ones
    # its own migration missed. This is also what re-tightens a tree from a
    # woswoar that predated owner-only directories -- `install` is the command
    # people re-run to upgrade.
    store.harden()
    return Installed(machine, hooks)


def wire_rcfiles(hooks: dict[str, Path], rcfile: Path | None = None) -> dict[str, tuple[Path, str]]:
    """Make each shell's startup file load its hook. shell -> (file, what changed).

    `rcfile` overrides every shell's own, and a caller that passes one has
    already reduced `hooks` to a single entry -- `shells_from` refuses the
    combination, because one file cannot hold two shells' blocks: each replaces
    the marked block the last one wrote.
    """
    out = {}
    for shell, target in hooks.items():
        chosen = rcfile if rcfile is not None else rcfile_for(shell)
        out[shell] = (chosen, write_block(chosen, target))
    return out
