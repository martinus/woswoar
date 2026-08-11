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
       2m  thinkpad  docker compose up -d --build
    3h12m  DT-24YYQ3 docker logs -f api
     6d4h  thinkpad  docker system prune -af
  ctrl-r global → host → session → dir, or ctrl-g/h/s, ctrl-o dir | ctrl-t timeline | ^name one machine
```

The machine column appears once you have more than one, and fzf matches on it —
so typing `thinkpad` narrows to that machine. It shows the *host* part of each
machine's name, because that is usually what differs between your own machines.
The age of a command that exited non-zero is red, and <kbd>Ctrl</kbd>+<kbd>R</kbd>
again cycles global → host → session → dir — where **dir** is this directory and
everything below it, on every machine, because `~/src/woswoar` on your laptop and
on your desktop is the same project.

**A short machine name is also an ordinary word.** If yours is `box`, typing it
finds `sandbox` and `~/dropbox` too — so anchor it: **`^box`** matches only the
machine, because the search starts at the machine column and `^` sticks to the
front of it. It composes with everything else, so `^box docker` is "docker, on
box" and `^box !docker` is "on box, but not docker".
### Find one command, then read around it

Half of what you want from history is not a command but the *next* one — you
remember running the migration, and what you actually need is what you ran after
it. Find anything, press <kbd>Ctrl</kbd>+<kbd>T</kbd>, and the list becomes the
timeline either side of it, with the cursor still on what you found:

```
  woswoar (timeline global) >
    3h41m  git commit -m wip
    3h44m  git add -A
    3h58m  cargo test
     4h2m  vim src/lib.rs          <- where you were
    4h15m  cargo test
    4h15m  cargo build
    4h17m  cd ~/proj
```

Newest first, like every other list here. Scroll up into what came next, down
into what led there, and press <kbd>Enter</kbd> on any of them. The search box
is cleared, so typing now filters *the timeline* rather than repeating the search
that got you here. Repeats are kept — running `cargo test` twice is the shape of
what happened, and the deduplicated search list hides it.

### See the rest of what was recorded

Six fields go into every record and a list line has room for two. Press
<kbd>Ctrl</kbd>+<kbd>/</kbd> for the other four on whichever row you are on:

```
when     2026-08-10 14:32:07  (3h12m ago)
dir      ~/src/woswoar
host     thinkpad
session  6a79f245-36ea53
exit     0
took     1.2 s

docker compose up -d --build
```

Which directory, which machine, which shell, how long it took, and the command
in full rather than clipped at the window edge. `dir`, `host` and `session` are
also three of the four scopes, so the pane says which key would narrow the list
to whatever it is pointing at. A field nobody recorded says so rather than going
missing — most of a freshly imported history.

The pane starts hidden and costs nothing until you ask for it — see
[the numbers](docs/shell-integration.md#the-details-pane).

## Quick start

```bash
pipx install woswoar
woswoar
```

**`woswoar` on its own is the only command you have to remember.** On a machine
with nothing installed it sets up; after that it tells you where you stand and
names the one command to run next, if there is one:

```console
$ woswoar
woswoar 0.7.2 — 54,804 commands from 3 machines

1 machine(s) waiting to be accepted here:
    'martin@laptop'

Next:  woswoar accept
```

It reads only what is already here and never widens who can read your history —
it names `accept`, it does not ask. Deciding that stays something you go and do,
so the moment you are asked is one you chose.

`woswoar setup` asks four questions — it checks the tools, installs the shell hook,
offers to import whatever history it finds, and asks for a sync repository (leave
it blank to stay on one machine). Every step is a command you can also run
yourself: `install`, `import`, `init`.

Open a new shell, press <kbd>Ctrl</kbd>+<kbd>R</kbd>. That is the whole thing on
one machine.

> [!TIP]
> **`pipx upgrade woswoar` for the next release.** The shell hook is a copy
> rather than the packaged file, but it brings itself up to date on the next
> background sync, so there is nothing else to run; open a new shell to pick it
> up.
>
> **Coming from 0.6.x or earlier, run `woswoar install` once.** Those versions
> had no background sync, so there is nothing running that could notice.
> `woswoar` and `woswoar doctor` both say so if it is skipped.

<details>
<summary>Installed from the git URL before woswoar was on PyPI?</summary>

`pipx upgrade` keeps whatever a package was installed *from*, so an install made
with `git+https://…` goes on cloning the repository rather than moving to PyPI.
Switch it over once:

```bash
pipx install --force woswoar
```

If that fails with **"A virtual environment already exists"**, your pipx is using
`uv` as its backend and cannot reuse the old venv. Then:

```bash
pipx uninstall woswoar
pipx install woswoar
```

Neither touches your history: it lives in `~/.local/share/woswoar`, not in the
venv.

To track the tip instead of releases, the git URL is still there —
`pipx install "git+https://github.com/martinus/woswoar.git@main"`, or `@stable`
for the most recent tag, or `@v0.9.0` to pin exactly. That form needs `git` on
the machine; `pipx install woswoar` does not.
</details>

**Needs:** bash 5.0+ (Linux) · Python 3.10+ ·
[fzf](https://github.com/junegunn/fzf) ·
[age](https://github.com/FiloSottile/age) and git *(sync only)*.
`woswoar install` checks for these and prints the install command for your
distribution. `woswoar doctor` diagnoses anything else that looks wrong.

> [!WARNING]
> **Do not install `age` as a snap.** It works, and it starts a sandbox on every
> call — about 250 ms against 2 ms for a distribution binary. woswoar runs `age`
> roughly twice per day of recorded history, so on two years of history that is
> the difference between a `sync` taking three seconds and taking six minutes.
> `woswoar doctor` measures it and says so.

## Adding another machine

Sync goes through **an ordinary git repository you already own** — no server, no
account, no daemon. Create an empty one (`woswoar-history` on GitHub, a bare repo
on a NAS, a folder on a USB stick). You do that once, ever.

Then on **every** machine, first or fifth, the same two lines:

```bash
pipx install woswoar
woswoar                          # paste the repository URL when it asks
```

`setup` joins the repo and does the first sync itself, so the new machine starts
publishing straight away — it signs its own commands and waits for nobody.

> [!TIP]
> **Importing atuin on more than one machine?** `setup` asks whether to import
> only *this* machine's history, and on a fleet the answer is yes. atuin keeps
> every machine it has synced with in one database, and woswoar publishes only a
> machine's own commands — so importing all of them everywhere stores each
> machine's history once per machine. Let each machine import its own.

<details>
<summary>The same thing without the questions</summary>

```bash
woswoar install
woswoar import atuin --this-host-only     # optional
woswoar init git@github.com:you/woswoar-history.git
```

`setup` calls exactly these. It needs a terminal, so this is also what to use
from a script or a dotfiles bootstrap.
</details>

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

Nothing to install — the shell hook does it. At most once a minute, and only on
a machine somebody is actually typing on, it starts a `woswoar sync` in the
background. Your prompt never waits for it: the shell hands the work to a
detached process and returns immediately, so a slow `git push` cannot hold up a
shell, and neither can a laptop that woke up on the wrong network.

```bash
# Sync at most every 5 minutes instead of every minute.
export WOSWOAR_SYNC_INTERVAL=300
```

An idle machine costs nothing, which is the point: a timer firing every minute
on four machines is 5,760 fetches a day whether or not anyone typed anything.
Recorded history reaches your other machines within a minute of you typing it,
and about 6 MB of repository per machine per year — real typing is bursty, so a
minute rather than five roughly doubles the syncs that carry anything, not
quintuples them.

<details>
<summary>Keeping a machine current while nobody is using it</summary>

The trade is that a machine nobody types on never syncs, so it never *receives*
either — a laptop left shut for a week is a week stale until the first command
is typed. Opening a shell syncs, so in practice you are current by the time you
have a prompt. If you would rather have a machine stay current while idle, a
systemd timer does it. Paste the whole block:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/woswoar-sync.service <<'UNIT'
[Unit]
Description=woswoar shell history sync
Documentation=https://github.com/martinus/woswoar
# Pointless and noisy without a network; the timer will try again.
After=network-online.target

[Service]
Type=oneshot
# /usr/bin/env so this works wherever woswoar was installed (pipx, --user, venv)
# without hardcoding a path into the unit.
ExecStart=/usr/bin/env woswoar sync

# Sync holds a lock and talks to a remote; if it hangs, fail rather than pile up.
TimeoutStartSec=10min

# It only ever needs its own data directory, an ssh key, and the network.
PrivateTmp=true
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
UNIT

cat > ~/.config/systemd/user/woswoar-sync.timer <<'UNIT'
[Unit]
Description=Sync woswoar shell history periodically

[Timer]
# Wait a little after login rather than competing with everything else starting.
OnStartupSec=2min
# A minute, because it turns out to be nearly free -- real typing is bursty, so
# five minutes does not carry five times less. Raise it for the bytes back.
OnUnitActiveSec=1min

# Catch up after the machine was asleep or off, rather than silently skipping.
Persistent=true

# Every machine syncing on the same wall-clock tick is how you manufacture
# push races. A minute of jitter costs nothing and avoids them.
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT

systemctl --user enable --now woswoar-sync.timer
export WOSWOAR_SYNC_INTERVAL=0   # and turn the hook's off, or you pay twice
```

Written out in full rather than copied out of the repository, because a `pipx`
install leaves no checkout on the machine to copy from. Put the `export` in your
`.bashrc` as well — pasted into a shell it lasts only as long as that shell, and
the hook reads it on every prompt.

The two are safe to run together — syncs take a non-blocking lock, so whichever
arrives second exits immediately — but there is no reason to.

</details>

If a background sync starts failing, nothing is on screen to say so. Typing
`woswoar` on its own reports it, because a detached sync's error message would
otherwise go nowhere at all.

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
| 🧱 **~4300 lines of implementation** | small enough to read in an afternoon |
| 🐤 **Verifiable on your machine** | `woswoar doctor --prove` demonstrates, not asserts — see [verify it yourself](docs/verify.md) |

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

None of that has to be taken on faith. `woswoar doctor --prove` records a
canary command in a throwaway sandbox, syncs it, and shows you that it reaches
the remote unreadable — and that is only the first of the checks you can run
yourself, decrypting a chunk with stock `age` and no woswoar in the pipeline
among them.

📖 **[The full security model](docs/security.md)** — what is protected, what is
not, and the guarantees CI asserts on every push.
🐤 **[Verify it yourself](docs/verify.md)** — checks you run on your own
machine, none of which ask you to believe a document.

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
| `woswoar` | where this machine stands, and what to run next |
| `woswoar setup` | guided first run: tools, hook, import, sync repo |
| `woswoar search` | interactive picker (what <kbd>Ctrl</kbd>+<kbd>R</kbd> runs) |
| `woswoar list` | plain output, used by fzf's scope-switch reload |
| `woswoar import bash\|zsh\|atuin` | import an existing history |
| `woswoar stats` | entry counts, date range, most-used commands |
| `woswoar doctor` | check the installation and the tools it needs |
| `woswoar doctor --prove` | demonstrate in a sandbox that nothing readable is published |
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
| `WOSWOAR_SYNC_INTERVAL` | seconds between background syncs; `0` turns them off (default `60`) |
| `WOSWOAR_SCOPE` | default scope for <kbd>Ctrl</kbd>+<kbd>R</kbd>: `global`, `host`, `session` or `dir` (default `global`) |
| `WOSWOAR_NO_BIND` | set to skip binding <kbd>Ctrl</kbd>+<kbd>R</kbd> |

## Uninstalling

There is no `woswoar uninstall`, because every step is one you should see. In
order, and each is independent:

```sh
# 1. Stop the timer, if you installed one. Syncing from the shell hook stops
#    with the hook itself, in step 2.
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
at that exact commit, builds the sdist and wheel, attests both, uploads them to
PyPI over Trusted Publishing — no token, an identity minted for that one run —
publishes a GitHub release with generated notes, and fast-forwards `stable`,
which is what the `git+https://…@stable` form tracks. The `stable` push is not
forced, so tagging an older commit fails loudly rather than moving everyone
backwards.

PyPI is the one step that cannot be undone: a version can be yanked but never
reused. It runs before the GitHub release for that reason, and
`.github/workflows/publish-testpypi.yml` is the rehearsal, run by hand against
TestPyPI whenever the packaging changes.

</details>

## License

[Apache-2.0](LICENSE)
