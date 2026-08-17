"""What `woswoar setup` works out without asking, and the two ways it asks.

Small on purpose, and worth being exact about what is here, because the obvious
reading of the name is wrong: this is **not** the wizard. The questions' wording,
the four numbered steps and the paragraphs that frame them are in `__main__`,
under `_setup` and the two functions it calls. That is where #202 asked for them
-- "leaving `cmd_install` and `cmd_setup` in the CLI as the render-and-dispatch
half" -- and it is also forced: a dialog has to print each paragraph before the
`input` under it, so its prose and its control flow are one thing, and separating
them needs either a cycle back into `__main__` or the four commands passed in as
callables.

What is here is everything the wizard needs that is *not* a print:

- `ask` and `ask_yes`, the two input primitives. Not `_confirm` -- see `ask_yes`.
- `importable`, which histories exist on this machine and how big they are.
- `untouched`, whether anything has been set up here at all. `cmd_status` asks
  this too, to decide whether to hand over to `setup`; it is the reason this is a
  function rather than three lines inside the wizard.
- `why_those_shells`, one clause naming the rule that chose the shells.

None of them prints, and none of them calls another, so each is testable on its
own -- which the wizard, driven through mocked `input` and scraped stdout, is
not. That gap is real and named in #202; closing it needs the callable-injection
shape above, and is not what this module is.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from . import install, store

if TYPE_CHECKING:  # `importer` is imported lazily; only the annotation needs it.
    from . import importer


def ask(question: str, default: str = "") -> str:
    """Free text, with a default shown. Empty answer takes the default."""
    shown = f" [{default}]" if default else ""
    return input(f"{question}{shown}: ").strip() or default


def ask_yes(question: str, default: bool = True) -> bool:
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


def importable() -> list[tuple[importer.Kind, Path, int]]:
    """``(kind, path, size)`` for each history on this machine, largest first.

    The kinds are `importer.Kind` rather than plain strings, so the caller can
    hand one straight to `importer.run` and mypy agrees. Spelling it `str` here
    was what let the wizard's forged namespace carry anything at all.
    """
    from . import importer

    found: list[tuple[importer.Kind, Path, int]] = []
    for kind in importer.KINDS:
        path = importer.atuin_db() if kind == "atuin" else importer.default_path(kind)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size:
            found.append((kind, path, size))
    return sorted(found, key=lambda item: item[2], reverse=True)


def untouched() -> bool:
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

    if install.installed_shells() or sync.is_repo():
        return False
    return not any(store.iter_log_files())


def why_those_shells(choice: str | None, rcfile: str | None, chosen: list[str]) -> str:
    """One clause saying which rule picked `chosen`, for `setup`'s step 2.

    Worth a line because the answer differs per machine and the wrong one is
    silent: a hook written into the shell you do not use looks exactly like a
    hook that works.
    """
    if choice and choice != "auto":
        return f"--shell {choice}"
    if rcfile:
        return f"from the name of {Path(rcfile).name}"
    if any(install.rcfile_for(shell).is_file() for shell in chosen):
        return "found " + " and ".join(f"~/{install.RCFILES[shell]}" for shell in chosen)
    return f"no rc file here, so $SHELL ({os.environ.get('SHELL', 'unset')})"
