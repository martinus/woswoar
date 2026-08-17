"""What `woswoar doctor` found, as values rather than as printed lines.

Every verdict here used to be derived inside `cmd_doctor`, interleaved with the
`print` that showed it. Four of the eighteen came from `sync` as
`(ok, detail)` records -- with docstrings saying they lived there so that "the
judgement is testable and stated once" -- and the other fourteen did not, so the
only way to test them was to run the command and grep its output. That is a poor
way to hold up the command people run *because* something is already wrong.

So this module answers the questions and `__main__` prints the answers. Nothing
here writes to a stream, and nothing here decides what a marker looks like;
`report` owns that.

The checks the *installer* owns -- which shell, whether the hook is current,
whether the rc file sources it -- live in `install` rather than here, because
they are its judgements and it owns the constants they are stated against. Both
sets are spliced together by `cmd_doctor`, which is where the order of the whole
report lives: the shell version leads, and the hook and rc file come between
`machine` and `age`.
"""

from __future__ import annotations

import os
import shutil
import time

from . import cache, crypto, deps, search, store
from .report import Check


def fzf_check() -> Check:
    """Whether the picker is there, and whether it can offer every key.

    Not just "is fzf on PATH". An fzf below 0.45 gets a working picker with
    several keys missing -- correct, silent, and impossible to tell from a bug
    unless something says so. Reported from a fleet running 0.73 on one machine
    and Debian's 0.44.1 on another.

    The list of keys comes from `search.GATED_KEYS` rather than being written
    out here: this sentence and the bindings it describes are the same fact in
    two modules, and it was already wrong once -- it still named two keys after
    the preview pane became the third.
    """
    fzf = shutil.which("fzf")
    if fzf is None:
        return Check("fzf", f"not found - {deps.advice([deps.FZF])}", ok=False)

    said, _ = search.fzf_version()
    shown = said or "version unknown"
    if search.fzf_supports_transform():
        return Check("fzf", f"{fzf}  ({shown})", ok=True)
    floor = ".".join(str(part) for part in search.TRANSFORM_SINCE)
    missing = ", ".join(search.GATED_KEYS[:-1]) + f" and {search.GATED_KEYS[-1]}"
    return Check(
        "fzf",
        f"{fzf}  ({shown} - searching works; {missing} need {floor}+)",
        ok=True,
    )


def _has_machine() -> bool:
    """Whether an identity file exists, asked without creating one.

    Read before anything calls `store.machine()`, which would create it -- which
    is why this is a function of its own rather than a line inside
    `machine_check`: two later checks need the same answer, and asking again
    after something has run is asking a different question.
    """
    return store.machine_file().is_file()


def machine_check() -> Check:
    return Check("machine", str(store.machine_file()), ok=_has_machine())


def age_check() -> Check:
    """Whether `age` is there, works, and starts fast enough to be usable.

    Not just "is age on PATH". A sandboxed age -- snap, flatpak, anything
    confined -- answers `--version` perfectly and then cannot open a key, which
    is a real failure that used to reach the user as an unexplained "permission
    denied" from `init` with doctor reporting nothing at all.
    """
    age_path = shutil.which("age")
    if age_path is None:
        return Check(
            "age",
            f"not found, needed for 'woswoar sync' - {deps.advice([deps.AGE])}",
            ok=None,
        )

    failure = crypto.selftest()
    if failure:
        return Check("age", age_path, ok=False, note=failure)

    cost = crypto.startup_ms()
    detail = f"{age_path}  ({cost:.0f} ms to start)"
    if cost < crypto.SLOW_MS:
        return Check("age", detail, ok=True)

    # Not a broken age -- a slow one, which is worse to diagnose because
    # everything works. woswoar runs it about twice per day of history, so this
    # is the difference between a grant taking three seconds and taking six
    # minutes, and nothing on screen would say why.
    if "/snap/" in age_path:
        advice = (
            "This one is a snap, which sets up a sandbox on every call.\n"
            "Install the distribution package or the release binary instead:\n"
            f"  sudo snap remove age  &&  {deps.advice([deps.AGE])}"
        )
    else:
        advice = (
            f"A distribution binary starts in a millisecond or two:\n  {deps.advice([deps.AGE])}"
        )
    return Check(
        "age",
        detail,
        ok=False,
        note=f"age takes {cost:.0f} ms to start. woswoar runs it about twice per\n"
        f"day of recorded history, so a year costs about "
        f"{cost * 2 * 365 / 1000:.0f} s per sync,\n"
        f"grant or accept that has work to do.\n{advice}",
    )


def identity_check() -> Check:
    """Whether this machine could actually decrypt during an unattended sync.

    Checked whether or not a repo exists: `init` is exactly when this breaks,
    and gating it behind `is_repo()` meant doctor was silent in the one state
    where someone would think to run it.
    """
    from . import sync

    if not (_has_machine() and store.machine().identity):
        return Check("identity", "none yet - chosen by 'woswoar init'", ok=None)
    return sync.identity_status(store.machine())


def repo_checks() -> list[Check]:
    """Everything about the history repository, or the note that there is none."""
    from . import sync

    out: list[Check] = []
    if shutil.which("git") is None:
        advice = deps.advice([deps.GIT])
        out.append(Check("git", f"not found, needed for 'woswoar sync' - {advice}", ok=None))

    if not sync.is_repo():
        out.append(
            Check("sync", "no history repo - run 'woswoar init <url>' to sync machines", ok=None)
        )
        return out

    known = store.machine()
    out.append(Check("remote", sync.remote_summary(), ok=None))

    # Taken from `sync` whole -- label included. The repo format marker is a
    # file nobody would think to `cat` and `sync` mentions it only by refusing,
    # so this is the one place a person can see it; that the name of the check
    # travels with the verdict is what keeps `sync`'s claim that the judgement
    # is "testable and stated once" true of the whole judgement.
    #
    # The three below are the ones this module still derives itself, and they
    # are the next thing to move: they read `sync`'s internals and word `sync`'s
    # prose from outside it.
    out += [sync.repo_format_status(), sync.signing_status(known), sync.trust_status(known)]

    # One stat per day this machine has published. Worth doing every time
    # because `export` only revisits a day that still has lines to publish, so a
    # day finished before its manifest went is never looked at again.
    unmanifested = sync.days_missing_a_manifest()
    out.append(
        Check(
            "manifests",
            f"{len(unmanifested)} published day(s) have no signed list, e.g."
            f" {unmanifested[0]} - peers refuse every chunk this machine"
            " published on them"
            if unmanifested
            else "all present",
            ok=not unmanifested,
        )
    )

    # A listing, no decryption, so it is cheap enough to do every time -- and the
    # state is otherwise silent, which is the whole problem with it.
    orphaned = sync.orphaned_days()
    if orphaned:
        host, day = orphaned[0]
        detail = (
            f"{len(orphaned)} sealed key(s) missing, e.g. {host[:8]}/{day}"
            " - chunks encrypted to them cannot be read by any machine"
        )
    else:
        detail = "all sealed"
    out.append(Check("day keys", detail, ok=not orphaned))

    # `sync` says this once, on the pass that first sees the file, and then the
    # day settles and it stops -- which is right for a one-minute timer and no
    # use to somebody asking afterwards. Costs no subprocess unless there is
    # something to find; see `unlisted_chunks`.
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
    out.append(Check("chunks", detail, ok=not planted))
    return out


def local_checks() -> list[Check]:
    """What is on this disk: how much history, who can read it, how fast."""
    logs = list(store.iter_log_files())

    # Recorded history is more than ~/.bash_history holds -- the command, the
    # directory, the exit status, and every other machine's history once sync
    # has run -- so anything another user can read is a finding, not a note.
    exposed = store.readable_by_others()
    if exposed:
        private = f"{len(exposed)} path(s) other users can read, e.g. {exposed[0]}"
        private += " - run 'woswoar install' to fix"
    else:
        private = f"{store.data_dir()} is owner-only"

    started = time.perf_counter()
    entries = cache.load_entries()
    elapsed_ms = (time.perf_counter() - started) * 1000

    return [
        Check("logs", f"{len(logs)} file(s) in {store.logs_dir()}", ok=None),
        Check("private", private, ok=not exposed),
        Check("cache", f"{len(entries)} entries loaded in {elapsed_ms:.0f} ms", ok=None),
        Check(
            "session",
            "set" if os.environ.get("WOSWOAR_SESSION") else "unset (hook not loaded here)",
            ok=None,
        ),
    ]


#: Deliberately no `checks()` that returns the whole report. The installer's
#: three do not sit in one block -- the shell version leads, and the hook and rc
#: file come between `machine` and `age` -- so a list assembled here could not be
#: spliced into the right order anyway. `cmd_doctor` composes it, which keeps the
#: order of the report a single readable literal in one place.
