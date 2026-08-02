<div align="center">

# woswoar

**Your shell history, on every machine, encrypted, without a server.**

[![CI](https://github.com/martinus/woswoar/actions/workflows/ci.yml/badge.svg)](https://github.com/martinus/woswoar/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Runtime dependencies: 0](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](docs/security.md#-minimal-supply-chain)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-lightgrey)](LICENSE)

*Austrian for "Was war?"* — "what was it again?" — which is exactly what you ask
when you need that one command from last Tuesday, on the other machine.

</div>

```
  woswoar (global) > docker
   2m  docker compose up -d --build
   3h  docker logs -f api
   6d  docker system prune -af
  ctrl-g global | ctrl-h host | ctrl-s session
```

## Quick start

```bash
pipx install --force "git+https://github.com/martinus/woswoar.git@stable"
woswoar install                  # hook it into ~/.bashrc
woswoar import bash              # optional: bring your existing history along
```

Open a new shell, press <kbd>Ctrl</kbd>+<kbd>R</kbd>. That is the whole thing on
one machine.

> [!TIP]
> **The same line upgrades an existing install** — run it again whenever you want
> the latest release. `stable` tracks the most recent tag, so the command never
> changes and you never edit a version number on five machines. Swap `@stable`
> for `@main` to track the tip, or `@v0.4.0` to pin exactly.

**Needs:** bash 5.0+ (Linux) · Python 3.10+ ·
[fzf](https://github.com/junegunn/fzf) ·
[age](https://github.com/FiloSottile/age) and git *(sync only)*.
`woswoar install` checks for these and prints the install command for your
distribution. `woswoar doctor` diagnoses anything else that looks wrong.

## Adding another machine

Sync goes through **an ordinary git repository you already own** — no server, no
account, no daemon. Create an empty one (`woswoar-history` on GitHub, a bare repo
on a NAS, a folder on a USB stick). You do that once, ever.

Then on **every** machine, first or fifth, the same four lines:

```bash
pipx install --force "git+https://github.com/martinus/woswoar.git@stable"
woswoar install                                       # hook it into bash
woswoar import atuin --this-host-only                 # optional: this machine's past
woswoar init git@github.com:you/woswoar-history.git   # join, and sync
```

`init` does the first sync itself, so the new machine starts publishing straight
away — it signs its own commands and waits for nobody.

From the **second** machine onwards, one more line, on each machine you already
use:

```bash
woswoar accept
```

That is the whole of it. `accept` lists what is new, says what accepting it
does, and asks:

```console
$ woswoar accept
1 machine(s) not yet accepted here:

  'martin@work-laptop'
      reads with                  age1qjg…
      signs with                  SHA256:2xQ5…

Accepting does two separate things:

  read     1 machine(s) get to read your ENTIRE history, including
           days recorded before they existed. This is published, so it
           applies everywhere — and it cannot be taken back for what
           they have already read.

  believe  this machine will accept what 1 machine(s) publish.
           Local only: every other machine of yours has to be told
           separately, because the repository is the thing that decision
           defends against.

A name is free text written by whoever added the key. The fingerprints are
not — run these on the machine they belong to and compare:

    reads with   age-keygen -y ~/.config/woswoar/identity
    signs with   ssh-keygen -lf ~/.config/woswoar/signing_key.pub

Accept 1 machine(s)? [y/N]
```

Two keys, because there really are two questions, and they stay separate
commands — [`grant`](docs/security.md) for who may *read*, `trust` for whose
word this machine *believes*. `accept` is both at once for the ordinary case
where the machine is yours. The second one is why it has to be run on each
machine you already own rather than once: the repository is somewhere anyone
with push access can write, so what a machine believes cannot be decided by
anything kept inside it. Revoking removes that decision everywhere
automatically, since taking trust away can only ever cause a refusal.

> [!TIP]
> `.bashrc` is written with `$HOME` rather than your username, so one shared
> dotfiles `.bashrc` works on every machine.

### Sync automatically

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/woswoar-sync.* ~/.config/systemd/user/
systemctl --user enable --now woswoar-sync.timer
```

One-minute interval by default, which costs about **6 MB of repository per
machine per year** — real typing is bursty, so a minute rather than five roughly
doubles the syncs that carry anything, not quintuples them. Sync never runs on
your prompt: a `git push` must not be able to block a shell.

## Why woswoar?

Press <kbd>Ctrl</kbd>+<kbd>R</kbd> and fuzzy-search **every command from every
machine you own** — deduplicated, newest first, with the working directory, exit
code and duration recorded alongside. Pick one and it lands on your prompt for
editing, never executed behind your back.

|  | woswoar |
|---|---|
| 🔐 **Encrypted end to end** | commands, paths, hostnames — nothing readable reaches the remote |
| 🧩 **No server, no database** | a git repo and plain text files you can `grep` |
| 📦 **Zero Python dependencies** | standard library only — nothing to audit but this repo |
| ⚡ **~150 µs per command, zero forks** | the hook is pure bash; Python never runs on your prompt |
| 🔎 **fzf as the UI** | the fuzzy finder you already know, not a bespoke TUI |
| 🚚 **Imports what you have** | bash, zsh and atuin histories, idempotently |
| 🧱 **~3500 lines of implementation** | small enough to read in an afternoon |

> [!NOTE]
> woswoar is a lighter alternative to [atuin](https://github.com/atuinsh/atuin).
> If you want a sync server, a rich TUI and cross-platform support, atuin is the
> better tool. woswoar trades those for a design you can hold in your head.

## Security

Everything that leaves your machine is encrypted with
[age](https://github.com/FiloSottile/age) — commands, paths, hostnames, even the
directory names in the repo. Each machine keeps its own private key and no secret
is ever copied between them. There is no crypto code here at all: age does it,
and woswoar's wrapper is a few dozen lines of `subprocess`.

Your **local** history is plaintext, though, and metadata like "how many machines
and how often they sync" is visible to anyone holding the repo.

📖 **[The full security model](docs/security.md)** — what is protected, what is
not, and the guarantees CI asserts on every push.

## Coming from atuin

```bash
woswoar import atuin --dry-run   # see what would happen, changes nothing
woswoar import atuin
```

The database is opened **read-only** — it is very likely a running atuin's live
database. atuin keeps every machine it has synced with in one file, and woswoar
keeps those apart rather than flattening them, so `--scope host` and `stats` stay
truthful. Re-running an import is idempotent.

> [!TIP]
> **Syncing several woswoar machines? Use `--this-host-only` on each.** Sync
> publishes only a machine's own commands, so importing every atuin host on every
> machine would leave each peer's history stored twice. Import everything only if
> this stays your single woswoar machine.

## Reference

| command | |
|---|---|
| `woswoar search` | interactive picker (what <kbd>Ctrl</kbd>+<kbd>R</kbd> runs) |
| `woswoar list` | plain output, used by fzf's scope-switch reload |
| `woswoar import bash\|zsh\|atuin` | import an existing history |
| `woswoar stats` | entry counts, date range, most-used commands |
| `woswoar doctor` | check the installation and the tools it needs |
| `woswoar init [url]` | create or join an encrypted history repo |
| `woswoar sync` | exchange history with the remote |
| `woswoar accept` | add a machine you own: `grant` and `trust` at once |
| `woswoar grant` | let newly enrolled machines read the older history |
| `woswoar trust` | accept another machine's published history here |
| `woswoar compact` | merge old chunks to reduce the working-tree file count |

| variable | meaning |
|---|---|
| `WOSWOAR_DIR` | data directory (default `~/.local/share/woswoar`) |
| `WOSWOAR_IGNORE` | extended regex of commands never to record |
| `WOSWOAR_IGNORE_EXTRA` | extra regex joined onto the default, instead of replacing it |
| `WOSWOAR_SCOPE` | default scope for <kbd>Ctrl</kbd>+<kbd>R</kbd> (default `global`) |
| `WOSWOAR_NO_BIND` | set to skip binding <kbd>Ctrl</kbd>+<kbd>R</kbd> |

## Uninstalling

There is no `woswoar uninstall`, because every step is one you should see. In
order, and each is independent:

```sh
# 1. Stop the timer, if you installed one.
systemctl --user disable --now woswoar-sync.timer
rm -f ~/.config/systemd/user/woswoar-sync.{service,timer}

# 2. Remove the hook from your shell. `woswoar install` wrote a marked block;
#    delete the three lines between the markers, or:
sed -i '/# >>> woswoar >>>/,/# <<< woswoar <<</d' ~/.bashrc

# 3. Remove the program.
pipx uninstall woswoar

# 4. Remove its data. THIS DELETES YOUR RECORDED HISTORY -- see below first.
rm -rf ~/.local/share/woswoar ~/.config/woswoar ~/.cache/woswoar
```

Open a new shell afterwards; the current one still has the hook loaded.

### Before you run step 4

`~/.local/share/woswoar/logs/` is the **only** plaintext copy of what this
machine recorded. `history/` beside it is the encrypted git checkout, and
`~/.config/woswoar/` holds this machine's identity and its signing key.

- **Keeping the history?** Copy `logs/` somewhere first. It is TSV, one command
  per line, readable without woswoar.
- **Other machines still syncing?** Deleting local files does not remove this
  machine from the shared repository, and it does not stop peers accepting what
  it published. Run `woswoar revoke <fingerprint>` from **another** machine,
  otherwise its key stays in `recipients.txt` as a machine that can still read
  everything. `woswoar grant` on that other machine lists every enrolled
  machine by fingerprint and name, which is where that value comes from — it
  asks before changing anything, and does nothing at all if nothing is new.
  (`woswoar accept` shows the same fingerprints, but only for machines it has
  something left to do about.)
- **Reinstalling later?** Deleting `~/.config/woswoar/` discards the identity.
  The machine can rejoin with `woswoar init <url>`, but it enrols as a *new*
  machine: `woswoar accept` has to be run again on every machine you keep.
  Keep that directory if you only meant to move the data.

The remote repository is untouched by all of this. Delete it separately if you
want it gone, remembering that other machines still hold their own copies of
everything in it.

## How it works

```
bash hook  ──►  plaintext TSV logs  ──►  parse cache  ──►  scope filter  ──►  fzf
                        │
                        └──►  age-encrypted chunks  ──►  git  ──►  remote
```

The hot path is a fork-free bash hook using bash 5 builtins that appends one
escaped line to a per-day TSV file. Nothing else touches your prompt. Everything
expensive — parsing, caching, encrypting, git — happens when you search, or when
the timer fires.

- 🐚 **[Living in your shell](docs/shell-integration.md)** — what it does to your
  bash, how it coexists with ble.sh, atuin and prompt frameworks, and what
  Ctrl-R costs.
- 🔐 **[Security model](docs/security.md)** — threat model, guarantees, limits.
- 📐 **[Design summary](docs/woswoar_design_summary.md)** — architecture, record
  format, the sync and encryption design, with measured numbers and the mistakes
  that shaped them.

## Development

```bash
python -m tools.run_tests                               # 301 tests, sharded, ~6s
python -m unittest discover -s . -t . -p 'test_*.py'    # the same suite, serially
WOSWOAR_BENCH=1 python -m unittest tests.test_perf      # latency on 52k entries
ruff check . && ruff format --check . && mypy woswoar tests tools
```

The suite is about 88% subprocess wait — it drives real `age`, `git` and
`ssh-keygen` rather than mocking them — so sharding it across processes takes it
from ~19s to ~6s. Both commands run the same tests; the runner additionally
fails if any test it discovered never reported back, which is a way a parallel
run can be green that a serial one cannot.

CI runs lint, tests on Python 3.10/3.12/3.14, the shell-hook and fork-free
checks, a two-machine end-to-end sync against real `age` and real `git`,
immutability and repo-growth assertions, and an install smoke test.

<details>
<summary>Cutting a release</summary>

The version lives in `woswoar/__init__.py` and nowhere else — `pyproject.toml`
reads it from there, so the two cannot disagree.

```bash
# 1. bump __version__, open a PR, merge it (main is protected)
# 2. tag the merged commit:
git tag v0.2.0 && git push origin v0.2.0
```

Everything after that is automatic. `.github/workflows/release.yml` refuses the
tag unless it matches `__version__` and sits on `main`, re-runs the whole suite
at that exact commit, builds the sdist and wheel, publishes a GitHub release
with generated notes, and fast-forwards `stable` — which is what the install
command tracks. The `stable` push is not forced, so tagging an older commit
fails loudly rather than moving everyone backwards.

</details>

## License

[Apache-2.0](LICENSE)
