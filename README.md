<div align="center">

# woswoar

**Your shell history, on every machine, encrypted, without a server.**

[![CI](https://github.com/martinus/woswoar/actions/workflows/ci.yml/badge.svg)](https://github.com/martinus/woswoar/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Runtime dependencies: 0](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](#security)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-lightgrey)](LICENSE)

*Austrian for "Was war?"* — "what was it again?" — which is exactly what you ask
when you need that one command from last Tuesday, on the other machine.

</div>

```console
$ woswoar install
$ woswoar import atuin      # or: bash, zsh
# open a new shell, press Ctrl-R
```

```
  woswoar (global) > docker
   2m  docker compose up -d --build
   3h  docker logs -f api
   6d  docker system prune -af
  ctrl-g global | ctrl-h host | ctrl-s session
```

---

## Why woswoar?

Press <kbd>Ctrl</kbd>+<kbd>R</kbd> and fuzzy-search **every command from every
machine you own** — deduplicated, newest first, with the working directory, exit
code and duration recorded alongside. Pick one and it lands on your prompt for
editing, never executed behind your back.

Sync goes through **an ordinary git repository you already own**. There is no
server to run, no account to create, no daemon in the background. Everything
that leaves your machine is encrypted with [age](https://github.com/FiloSottile/age).

|  | woswoar |
|---|---|
| 🔐 **Encrypted end to end** | commands, paths, hostnames — nothing readable reaches the remote |
| 🧩 **No server, no database** | a git repo and plain text files you can `grep` |
| 📦 **Zero Python dependencies** | standard library only — nothing to audit but this repo |
| ⚡ **~150 µs per command, zero forks** | the hook is pure bash; Python never runs on your prompt |
| 🔎 **fzf as the UI** | the fuzzy finder you already know, not a bespoke TUI |
| 🚚 **Imports what you have** | bash, zsh and atuin histories, idempotently |
| 🧱 **~2900 lines of implementation** | small enough to read in an afternoon |

> [!NOTE]
> woswoar is a lighter alternative to [atuin](https://github.com/atuinsh/atuin).
> If you want a sync server, a rich TUI and cross-platform support, atuin is the
> better tool. woswoar trades those for a design you can hold in your head.

---

## Install

**Requirements:** bash 5.0+ (Linux) · [fzf](https://github.com/junegunn/fzf) ·
Python 3.10+ · [age](https://github.com/FiloSottile/age) and git *(only for sync)*

```bash
pipx install .          # or: pip install --user .
woswoar install         # writes the hook, sources it from ~/.bashrc
woswoar import bash     # optional: bring your existing history along
```

Open a new shell and press <kbd>Ctrl</kbd>+<kbd>R</kbd>. That's it — everything
below is optional.

If something looks wrong, `woswoar doctor` checks the bash version, fzf, the
hook and the cache, and tells you what to fix.

<details>
<summary>Does this disturb my normal bash, or my other prompt tools?</summary>

**Up-arrow and `~/.bash_history` are untouched.** woswoar never modifies
`HISTFILE`, `HISTSIZE`, `HISTCONTROL` or the history list — it only *reads*
`history 1`. It is purely additive: a second, richer log next to the one bash
already keeps. Your `HISTTIMEFORMAT` is not disturbed either; the hook's
override is scoped to its own call.

**It shares the DEBUG trap and `PROMPT_COMMAND` rather than taking them.** A
terminal-title hook, a prompt framework, bash-preexec, atuin and ble.sh all want
the same two places. woswoar chains onto whatever owns the DEBUG trap *at the
first prompt* — not at the moment it is sourced — so it picks up whoever ended
up owning it once your whole `.bashrc` has run. It also restores `$?` after
reading it, so an exit-code-colouring prompt downstream still sees the real
status.

If something else claims the trap *after* that, you lose the recorded
**duration** of those commands and nothing else; the commands themselves are
still recorded. This is all pinned by tests that drive a real interactive bash.

**A half-typed line becomes the search query.** Type `docker comp`, press
<kbd>Ctrl</kbd>+<kbd>R</kbd>, and fzf opens already filtered to that. Escape
leaves your line byte-for-byte as it was; picking something replaces the line
and puts the cursor at the end, never executing it.

`WOSWOAR_NO_BIND=1` skips the <kbd>Ctrl</kbd>+<kbd>R</kbd> binding entirely if
you would rather keep another tool's.

</details>

---

## Security

The whole point of syncing history is that it ends up somewhere off your
machine. So the question is not *"do I trust GitHub?"* but *"what happens when
that repository leaks?"* — through a stolen token, a mis-clicked visibility
toggle, or a backup nobody thought about.

**With woswoar, a leaked repository is a pile of unreadable blobs.**

### 🔒 Nothing readable ever leaves your machine

Commands, arguments, working directories, usernames, hostnames — all of it is
encrypted before it is committed. Even the *directory names* in the repo are
opaque random hex, because paths are not encrypted by anything and
`hosts/martin@desktop/` would publish your machine names for free.

### 🧊 No self-rolled cryptography

Not one line of crypto in this codebase. Python's standard library has no cipher
at all — only hashing — so instead of reaching for a third-party library and
composing primitives by hand, woswoar shells out to
[age](https://github.com/FiloSottile/age): a small, audited, widely deployed
tool with one job. woswoar's crypto module is a
[173-line subprocess wrapper](woswoar/crypto.py). There is no key derivation, no
nonce management and no mode selection to get wrong, because none of it lives
here.

### 🔑 No secret is ever copied between machines

age takes **SSH public keys as recipients**, so each machine encrypts to the
other machines' public keys and keeps its own private key to itself, forever.
Enrolling a new laptop means publishing a public key — nothing sensitive travels,
and nothing sensitive is stored in the repo.

<details>
<summary>What if my SSH key has a passphrase?</summary>

age cannot use ssh-agent, so a passphrase-protected key would break unattended
syncing from a timer. `woswoar init` detects this by doing a real
encrypt/decrypt round trip — not by inspecting the key file — and falls back to
a dedicated age identity at `~/.config/woswoar/identity`. Force either choice
with `--identity <path>` or `--new-identity`.

</details>

### 📦 Minimal supply chain

The runtime dependency list is **empty**, and
[intended to stay that way](pyproject.toml). No PyPI packages, no transitive
tree, no post-install scripts, no crate lockfile to audit — the only Python that
runs is the code in this repository. `ruff`, `mypy` and `shellcheck` are
development-only, and `fzf`, `git` and `age` are ordinary system binaries you
install from your distribution.

### ✅ Guarantees pinned by tests, not by prose

Claims rot. The ones that matter are asserted in CI on every push:

| Claim | How it is enforced |
|---|---|
| The hook forks nothing | recording runs under `strace`; the clone/fork/execve count must be **0** |
| Shell and Python escaping agree | a command containing a literal tab and newline must round-trip byte-for-byte |
| History is never rewritten | `git log --diff-filter=MD` over chunk files must be **empty** |
| The repo does not blow up | a simulated multi-day, multi-machine run is measured after `git gc` |
| Search stays fast | latency measured on 52,000 entries |

### ⚠️ What is *not* protected

Being straight about the limits is part of the security story:

> [!IMPORTANT]
> - **Local history is plaintext.** `~/.local/share/woswoar/logs/` is readable by
>   anyone who can read your home directory — same as `~/.bash_history`.
>   Encryption protects the *synced copy*, not your disk. Use full-disk
>   encryption for that.
> - **Metadata leaks.** Anyone with the repo can see how many machines you have,
>   when they synced, and roughly how many commands you ran per day.
> - **Secrets you type are still secrets.** `WOSWOAR_IGNORE` skips
>   credential-shaped commands by default (`*TOKEN=`, `*SECRET=`, `--password`,
>   …), and bash's own `HISTCONTROL`/`HISTIGNORE` are honoured for free — but no
>   pattern catches everything.
> - **Lose your key, lose your access.** There is no recovery service, because
>   there is no service. If a machine loses its identity, re-enrol it and run
>   `woswoar reencrypt` from another machine.

---

## Multi-machine sync

Create an empty repository anywhere you can push to — `woswoar-history` on
GitHub, a bare repo on a NAS, a folder on a USB stick. Then:

```bash
# on your first machine
woswoar init git@github.com:you/woswoar-history.git
woswoar sync
```

```bash
# on every additional machine
woswoar init git@github.com:you/woswoar-history.git   # enrols this machine
woswoar sync
```

```bash
# then once, on a machine that was already enrolled
woswoar reencrypt
```

<details>
<summary>Why that last step exists — and why the new machine can't do it itself</summary>

History sealed *before* the new machine joined was encrypted to a recipient list
that did not include it. `reencrypt` re-seals the small per-day keys — not the
history itself — so it takes seconds even with years of commands. It fetches and
publishes on its own, so it is one command, not a sync sandwich.

**Only a machine that is already a recipient can do this**, and that is the
point rather than a limitation: re-sealing a key means *opening* it first. If a
machine nobody had granted access to could re-seal old keys for itself, the
encryption would not be worth anything. Run it on the new machine and it will
tell you it could not open those keys.

Until someone runs it, the new machine syncs fine and simply reports how many
days it cannot read yet. Nothing is lost in the meantime.

</details>

<details>
<summary>How the repository stays small (and conflict-free)</summary>

Each sync compresses **only the lines added since last time**, seals them, and
writes a brand-new file that is never modified again:

```
hosts/<id>/2026-07-29/<synctime>-<rand>.age    a compressed, age-encrypted block
hosts/<id>/keys/2026-07-29.age                 that day's key, sealed to all recipients
```

Re-encrypting a whole day file on every sync would write a fresh random blob
each time, and random data delta-compresses to nothing. Compressing *before*
sealing is the only chance there is — encrypted bytes are incompressible by
definition, so once a chunk is written, nothing can ever shrink it again.

Because every machine only ever *adds* files under its own prefix,
`git pull --rebase` has nothing to conflict over. Not "rarely" — structurally.

`woswoar compact` can later merge a finished day's chunks into one. It reduces
the *file count*, not the repository: git keeps the replaced chunks forever,
reachable from the commits that added them.

</details>

### Automatic syncing

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/woswoar-sync.* ~/.config/systemd/user/
systemctl --user enable --now woswoar-sync.timer
```

**One-minute interval by default**, because it turns out to be nearly free. Real
typing is bursty, so a minute rather than five roughly doubles the number of
syncs that actually carry something — not five times it. Sync never runs on your
prompt: a `git push` must not be able to block a shell.

<details>
<summary>What that costs, measured</summary>

Two years of one machine's real history — 25,997 commands, 3.61 MB of plaintext
— replayed through `woswoar sync` itself at a 1-minute cadence:

| | repo, packed | per year |
|---|---|---|
| before compression + flat paths | 16.22 MB | 7.9 MB |
| **now** | **12.24 MB** | **5.9 MB** |

About half of what remains is git's own bookkeeping rather than your commands:
per sync, 359 B of blob (200 B of that the `age` header), 196 B of tree, 129 B
of commit. That fixed part is why the interval matters at all — raise
`OnUnitActiveSec` in the timer if you would rather have the bytes back.

The per-year number tracks how much you type, not the timer; a busier stretch of
the same history extrapolates to about 16 MB/year.

</details>

---

## Coming from atuin

```bash
woswoar import atuin --dry-run   # see what would happen, changes nothing
woswoar import atuin
```

atuin keeps every machine it has synced with in one sqlite database, so an import
can carry history from several hosts. woswoar keeps them apart rather than
flattening them, so `--scope host` and `stats` stay truthful: each atuin machine
gets its own host entry, and commands from *this* machine merge into its existing
history.

The database is opened **read-only** — it is very likely a running atuin's live
database, and woswoar has no business writing to it.

> [!TIP]
> **If you sync several woswoar machines, use `--this-host-only` on each.**
> Sync publishes only this machine's own commands, so importing every atuin host
> on every machine means each peer's history exists twice: once imported
> locally, once arriving over sync. Letting each machine import just its own
> keeps exactly one copy of everything.
>
> Importing everything is the right choice when only one machine runs woswoar —
> you get all your machines' history, it just stays local.

Re-running an import is idempotent, and woswoar reuses a peer's real host id when
that peer is already known locally — so importing *after* your machines have
synced also avoids duplicates. Importing before they have met cannot: the ids
were assigned independently.

---

## Reference

### Commands

| | |
|---|---|
| `woswoar search` | interactive picker (what <kbd>Ctrl</kbd>+<kbd>R</kbd> runs) |
| `woswoar list` | plain output, used by fzf's scope-switch reload |
| `woswoar import bash\|zsh\|atuin` | import an existing history |
| `woswoar stats` | entry counts, date range, most-used commands |
| `woswoar doctor` | check bash version, fzf, hook, cache |
| `woswoar init [url]` | create or join an encrypted history repo |
| `woswoar sync` | exchange history with the remote |
| `woswoar reencrypt` | re-seal keys after enrolling a new machine |
| `woswoar compact` | merge old chunks to reduce the working-tree file count |

### Scopes

Switch without leaving the picker:

| key | scope |
|---|---|
| <kbd>Ctrl</kbd>+<kbd>G</kbd> | **global** — every machine |
| <kbd>Ctrl</kbd>+<kbd>H</kbd> | **host** — this machine |
| <kbd>Ctrl</kbd>+<kbd>S</kbd> | **session** — this shell |

### Configuration

| variable | meaning |
|---|---|
| `WOSWOAR_DIR` | data directory (default `~/.local/share/woswoar`) |
| `WOSWOAR_IGNORE` | extended regex of commands never to record |
| `WOSWOAR_SCOPE` | default scope for <kbd>Ctrl</kbd>+<kbd>R</kbd> (default `global`) |
| `WOSWOAR_NO_BIND` | set to skip binding <kbd>Ctrl</kbd>+<kbd>R</kbd> |

### Performance

Measured on a real **54,943-entry** history across ~750 daily files:

| | |
|---|---|
| record a command | **~150 µs**, 0 forks |
| <kbd>Ctrl</kbd>+<kbd>R</kbd>, whole process | **~105 ms** |

No index, no SQLite — a pickle cache that only re-reads what changed is enough,
and CI re-measures it on every push.

<details>
<summary>Where those 105 ms go</summary>

| | cumulative |
|---|---|
| Python interpreter start | 8.8 ms |
| importing woswoar | 29 ms |
| building the argparse parser | 36 ms |
| loading the cache (unpickling 55k entries) | 67 ms |
| filter, sort, dedup, render | 87 ms |
| writing 1.5 MB to fzf | 105 ms |

Roughly half is fixed Python startup and half is proportional to history size.
Micro-optimising it was measured and did not help: the costs are structural, and
cutting them further would mean either a resident daemon (which the whole
no-server design exists to avoid) or an index the measurements do not justify.

The recording hook is a different story — 150 µs is imperceptible, and about
30 µs of it is the fork-free `history 1` capture that makes multi-line commands
survive intact.

</details>

---

## How it works

```
bash hook  ──►  plaintext TSV logs  ──►  pickle cache  ──►  scope filter  ──►  fzf
                        │
                        └──►  age-encrypted chunks  ──►  git  ──►  remote
```

The hot path is a fork-free bash hook using bash 5 builtins (`$EPOCHSECONDS`,
`$EPOCHREALTIME`, `printf -v`) that appends one escaped line to a per-day TSV
file. Nothing else touches your prompt. Everything more expensive — parsing,
caching, encrypting, git — happens when you search or when the timer fires.

- 📐 [docs/woswoar_design_summary.md](docs/woswoar_design_summary.md) —
  architecture, record format, and the sync/encryption design with measured
  numbers and the mistakes that shaped them.
- 🗺️ [docs/milestone-1-plan.md](docs/milestone-1-plan.md) — the implementation
  plan milestone 1 was built from.

## Development

```bash
python -m unittest discover -s . -t . -p 'test_*.py'    # 142 tests
WOSWOAR_BENCH=1 python -m unittest tests.test_perf      # latency on 52k entries
ruff check . && ruff format --check . && mypy woswoar tests
```

CI runs lint, tests on Python 3.10/3.12/3.14, the shell-hook and fork-free
checks, a two-machine end-to-end sync against real `age` and real `git`,
immutability and repo-growth assertions, and an install smoke test.

## License

[Apache-2.0](LICENSE)
