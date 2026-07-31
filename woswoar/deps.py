"""Which external tools are missing, and what to type to get them.

woswoar needs no Python packages, but it does need a few ordinary binaries, and
``fzf: command not found`` three steps later is a worse thing to hand someone
than the command that installs it. Kept dependency-free and tiny so the places
that report a missing tool can all say the same thing.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import NamedTuple


class Tool(NamedTuple):
    #: Binary on PATH, and the package name wherever `packages` does not say
    #: otherwise.
    name: str
    #: Filled into "needed for ...", so it reads as a reason, not a label.
    needed_for: str
    #: Distro family -> package name, for the tools whose binary and package are
    #: not called the same thing. `name`'s comment used to promise this mapping
    #: would appear rather than callers learning about packaging; ssh-keygen is
    #: what made it necessary, since it ships inside openssh-client(s).
    packages: tuple[tuple[str, str], ...] = ()

    @property
    def present(self) -> bool:
        return shutil.which(self.name) is not None

    def package(self, family: str) -> str:
        return dict(self.packages).get(family, self.name)


#: fzf is listed first because it is the one whose absence breaks the feature
#: people install woswoar for. age and git matter only once you sync.
FZF = Tool("fzf", "the Ctrl-R picker")
AGE = Tool("age", "encrypting history before it is synced")
GIT = Tool("git", "moving encrypted history between machines")
SSH_KEYGEN = Tool(
    "ssh-keygen",
    "signing history, so machines can tell which one wrote it",
    packages=(
        ("fedora", "openssh-clients"),
        ("rhel", "openssh-clients"),
        ("centos", "openssh-clients"),
        ("debian", "openssh-client"),
        ("ubuntu", "openssh-client"),
    ),
)

TOOLS = (FZF, AGE, GIT, SSH_KEYGEN)

#: Distro family -> the command that installs a package there. Deliberately
#: short: printing a confidently wrong command for a distro nobody tested is
#: worse than admitting we do not know, which is what the fallback does.
_INSTALLERS = {
    "fedora": "sudo dnf install",
    "rhel": "sudo dnf install",
    "centos": "sudo dnf install",
    "debian": "sudo apt install",
    "ubuntu": "sudo apt install",
}

_OS_RELEASE = Path("/etc/os-release")


def _os_release(path: Path | None = None) -> dict[str, str]:
    """Parse ``/etc/os-release``, tolerating its absence.

    The format is shell-ish ``KEY=value`` with optional quotes; only ID and
    ID_LIKE are read, so a full shell parser would be effort spent on fields
    nobody looks at.
    """
    try:
        text = (path or _OS_RELEASE).read_text(encoding="utf-8")
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if value:
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def family(path: Path | None = None) -> str:
    """This machine's distro family, or ``""`` if it is not one we know.

    ``ID_LIKE`` is consulted after ``ID`` so derivatives are covered without
    naming each one: Linux Mint reports ``ID=linuxmint ID_LIKE=ubuntu``.
    """
    release = _os_release(path)
    candidates = [release.get("ID", ""), *release.get("ID_LIKE", "").split()]
    for candidate in candidates:
        if candidate in _INSTALLERS:
            return candidate
    return ""


def installer(path: Path | None = None) -> str:
    """The install command for this machine, or ``""`` if we do not know it."""
    return _INSTALLERS.get(family(path), "")


def missing(tools: tuple[Tool, ...] = TOOLS) -> list[Tool]:
    return [tool for tool in tools if not tool.present]


def advice(tools: list[Tool] | tuple[Tool, ...], path: Path | None = None) -> str:
    """A ready-to-paste install line for ``tools``, or the honest fallback.

    Package names, not binary names: ``sudo dnf install ssh-keygen`` is a
    command that fails, and a fix that does not work is worse than no fix.
    """
    where = family(path)
    names = " ".join(dict.fromkeys(tool.package(where) for tool in tools))
    command = installer(path)
    if command:
        return f"{command} {names}"
    return f"install with your package manager: {names}"


def report(tools: list[Tool] | tuple[Tool, ...], path: Path | None = None) -> str:
    """The whole message: what is missing, why it matters, how to fix it."""
    if not tools:
        return ""
    lines = ["woswoar needs these and could not find them:"]
    width = max(len(tool.name) for tool in tools)
    lines += [f"  {tool.name:<{width}}  {tool.needed_for}" for tool in tools]
    lines += ["", f"  {advice(tools, path)}"]
    return "\n".join(lines)
