<div align="center">

# woswoar

**Your shell history, on every machine, encrypted, without a server.**

[![CI](https://github.com/martinus/woswoar/actions/workflows/ci.yml/badge.svg)](https://github.com/martinus/woswoar/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Runtime dependencies: 0](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](docs/security.md#-minimal-supply-chain)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-lightgrey)](LICENSE)

*Austrian for "Was war?"* — "what was it again?" — which is exactly what you ask
when you need that one command from last Tuesday, on the other machine.

<!-- Absolute, not `docs/demo.gif`: this file is also the PyPI project page, and
     PyPI resolves a relative image against pypi.org, where it is a broken icon.
     `tests/test_demo.py` checks the URL against the file it names. -->
<img alt="Ctrl-R searching one history from three machines: a docker command found on all of them, narrowed to this machine and then to this directory, the timeline unfolded around a hit, and the chosen command landing on the prompt unexecuted" src="https://raw.githubusercontent.com/martinus/woswoar/main/docs/demo.gif" width="900">

<sub>Re-record it with `tools/demo/record.sh` — the
[tape](tools/demo/demo.tape) is checked in, the history in it is
[generated](tools/demo/seed.py), and nobody's real commands are on screen.</sub>

</div>

## What it is

Press <kbd>Ctrl</kbd>+<kbd>R</kbd> and fuzzy-search **every command from every
machine you own** — deduplicated, newest first, with the working directory, exit
code and duration recorded alongside. Pick one and it lands on your prompt for
editing, never executed behind your back.

Machines exchange history through a git repository you already own. Nothing
readable ever reaches it, there is no server and no account, and the thing that
runs on your prompt is a fork-free bash hook that appends one line to a file.
Half of what you want from history is the command *after* the one you remember,
so <kbd>Ctrl</kbd>+<kbd>T</kbd> turns whatever you found into
[the timeline around it](docs/searching.md#find-one-command-then-read-around-it).

|  | woswoar |
|---|---|
| 🔐 **Encrypted end to end** | commands, paths, hostnames — nothing readable reaches the remote |
| 🧩 **No server, no database** | a git repo and plain text files you can `grep` |
| 📦 **Zero Python dependencies** | standard library only — nothing to audit but this repo |
| ⚡ **~150 µs per command, zero forks** | the hook is pure shell — bash or zsh; Python never runs on your prompt |
| 🔎 **fzf as the UI** | the fuzzy finder you already know, not a bespoke TUI |
| 🚚 **Imports what you have** | bash, zsh and atuin histories, idempotently |
| 🐚 **Records from bash and zsh** | one history per machine, whichever shell you are standing in |
| 🧱 **~4300 lines of implementation** | small enough to read in an afternoon |
| 🐤 **Verifiable on your machine** | `woswoar doctor --prove` demonstrates, not asserts — see [verify it yourself](docs/verify.md) |

> [!NOTE]
> woswoar is a lighter alternative to [atuin](https://github.com/atuinsh/atuin).
> If you want a sync server, a rich TUI and cross-platform support, atuin is the
> better tool. woswoar trades those for a design you can hold in your head.

## Install

```bash
pipx install woswoar
woswoar
```

Open a new shell, press <kbd>Ctrl</kbd>+<kbd>R</kbd>. That is the whole thing on
one machine. `woswoar` on its own is the only command you have to remember: it
sets up when there is nothing installed, and afterwards says where this machine
stands and names the one command to run next, if there is one.

**Needs:** bash 5.0+ or zsh 5.0+ · Linux or macOS · Python 3.10+ ·
[fzf](https://github.com/junegunn/fzf) ·
[age](https://github.com/FiloSottile/age) and git *(sync only)* — but **not `age`
as a snap**, which costs about 250 ms per call against 2 ms and turns a `sync`
into minutes. `woswoar doctor` measures it and says so.

📦 [Upgrading, importing an existing history, uninstalling](docs/install.md)

## More than one machine

Sync goes through **an ordinary git repository you already own** — no server, no
account, no daemon. Create an empty one (`woswoar-history` on GitHub, a bare repo
on a NAS, a folder on a USB stick), once, ever. Then on every machine:

```bash
# on the new machine — or just paste the URL when `woswoar` asks for it
woswoar init git@github.com:you/woswoar-history.git

# on each machine you already use
woswoar accept
```

`accept` is `grant` and `trust` at once — who may read your history, and whose
published history this machine believes. It prints both fingerprints and asks.

🔄 [Enrolment, revoking, and keeping an idle machine current](docs/sync.md)

## Security

Everything that leaves your machine is encrypted with
[age](https://github.com/FiloSottile/age) — commands, paths, hostnames, even the
directory names in the repo. Each machine keeps its own private key and no secret
is ever copied between them. There is no crypto code here at all: age does it,
and woswoar's wrapper is a few dozen lines of `subprocess`.

Your **local** history is plaintext, though, and metadata like "how many machines
and how often they sync" is visible to anyone holding the repo.

None of that has to be taken on faith. `woswoar doctor --prove` records a
canary command in a throwaway sandbox, syncs it, and shows you that it reaches
the remote unreadable — and that is only the first of the checks you can run
yourself, decrypting a chunk with stock `age` and no woswoar in the pipeline
among them.

🔐 [The full security model](docs/security.md) ·
🐤 [Verify it yourself](docs/verify.md)

## How it works

```
shell hook  ──►  plaintext TSV logs  ──►  parse cache  ──►  scope filter  ──►  fzf
                        │
                        └──►  age-encrypted chunks  ──►  git  ──►  remote
```

The hot path is a fork-free shell hook — one for bash built on bash 5 builtins,
one for zsh built on zsh's — that appends one escaped line to a per-day TSV
file. Both write into the same per-machine history. Nothing else touches your prompt. Everything
expensive — parsing, caching, encrypting, git — happens when you search, or when
the timer fires.

## Documentation

| | |
|---|---|
| 🔎 [**Searching your history**](docs/searching.md) | the machine column, `^name`, the <kbd>Ctrl</kbd>+<kbd>T</kbd> timeline, the details pane |
| 📦 [**Installing, upgrading, uninstalling**](docs/install.md) | the first run, `pipx` upgrades, importing atuin, and removing every part of it again |
| 🔄 [**Adding another machine**](docs/sync.md) | enrolment, `accept`/`grant`/`trust`, background sync, a systemd timer |
| 🐚 [**Living in your shell**](docs/shell-integration.md) | bash and zsh, how it coexists with ble.sh, atuin and prompt frameworks, what <kbd>Ctrl</kbd>+<kbd>R</kbd> costs, and what is never recorded |
| 🔐 [**Security model**](docs/security.md) | threat model, guarantees, limits |
| 🐤 [**Verify it yourself**](docs/verify.md) | checks you run on your own machine, none of which ask you to believe a document |
| 📖 [**Reference**](docs/reference.md) | every command and environment variable |
| 📐 [**Design summary**](docs/woswoar_design_summary.md) | architecture, record format, the sync and encryption design, with measured numbers and the mistakes that shaped them |
| 🛠️ [**Contributing**](CONTRIBUTING.md) | running the tests, what a patch needs, cutting a release |

## License

[Apache-2.0](LICENSE)
