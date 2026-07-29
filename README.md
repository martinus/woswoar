# woswoar

Distributed shell history — Austrian dialect for *"Was war?"*, "what was it
again?", which is exactly what you ask when you need that command from last
Tuesday on the other machine.

A lighter alternative to Atuin: no server, no database, `fzf` for search, git
for sync. Python standard library only.

```
$ woswoar install
$ woswoar import bash
# open a new shell, press Ctrl-R
```

```
  woswoar (global) > docker
   2m  docker compose up -d --build
   3h  docker logs -f api
   6d  docker system prune -af
  ctrl-g global | ctrl-h host | ctrl-s session
```

## What it does

- **Ctrl-R** opens fzf over your whole history, deduplicated and newest-first.
- **Scopes** — everything (`ctrl-g`), this machine (`ctrl-h`), or this shell
  (`ctrl-s`), switchable without leaving the picker.
- **Records exit code, duration, and working directory** alongside the command.
- **Costs 28 µs per command and forks nothing.** The hook is pure bash; Python
  never runs on your prompt.
- **Imports** your existing bash, zsh, or **atuin** history, idempotently.

## Requirements

- bash 5.0+ (Linux)
- [fzf](https://github.com/junegunn/fzf)
- Python 3.10+

## Install

```bash
pipx install .          # or: pip install --user .
woswoar install         # writes the hook and sources it from ~/.bashrc
woswoar import bash     # optional: bring your existing history along
```

### Coming from atuin

```bash
woswoar import atuin --dry-run   # see what would happen, changes nothing
woswoar import atuin
```

atuin keeps every machine it has synced with in one sqlite database, so an
import can carry history from several hosts. woswoar keeps them apart rather
than flattening them, so `--scope host` and `stats` stay truthful: each atuin
machine gets its own host entry, and commands from *this* machine merge into
its existing history.

The database is opened read-only — it is very likely a running atuin's live
database, and woswoar has no business writing to it.

**If you sync several woswoar machines, use `--this-host-only` on each.** Sync
publishes only this machine's own commands, so importing every atuin host on
every machine means each peer's history exists twice: once imported locally,
once arriving over sync. Letting each machine import just its own keeps one
copy of everything.

Importing everything is the right choice when only one machine runs woswoar —
you get all nine machines' history, it just stays local.

woswoar reuses a peer's real host id when that peer is already known locally, so
importing *after* your machines have synced also avoids duplicates. Importing
before they have met cannot: the ids were assigned independently.

`woswoar doctor` checks the installation if something looks wrong.

## Commands

| | |
|---|---|
| `woswoar search` | interactive picker (what Ctrl-R runs) |
| `woswoar list` | plain output, used by fzf's scope-switch reload |
| `woswoar import bash\|zsh\|atuin` | import an existing history |
| `woswoar stats` | entry counts, date range, most-used commands |
| `woswoar doctor` | check bash version, fzf, hook, cache |
| `woswoar init [url]` | create or join an encrypted history repo |
| `woswoar sync` | exchange history with the remote |
| `woswoar reencrypt` | re-seal keys after enrolling a new machine |
| `woswoar compact` | merge old chunks to reduce file count |

## Configuration

| variable | meaning |
|---|---|
| `WOSWOAR_DIR` | data directory (default `~/.local/share/woswoar`) |
| `WOSWOAR_IGNORE` | extended regex of commands never to record |
| `WOSWOAR_SCOPE` | default scope for Ctrl-R (default `global`) |
| `WOSWOAR_NO_BIND` | set to skip binding Ctrl-R |

Your existing `HISTCONTROL`, `HISTIGNORE`, and `ignorespace` settings are
honoured automatically — anything bash declines to put in history is invisible
to woswoar too.

## Multi-machine sync

History is synced through an ordinary git repo, encrypted with
[age](https://github.com/FiloSottile/age). There is no server. **Nothing
readable ever reaches the remote** — not commands, not paths, not usernames or
hostnames — so the repo can live anywhere you can push to.

Create an empty repo somewhere (`woswoar-history` on GitHub, a bare repo on a
NAS, anything), then on your first machine:

```bash
woswoar init git@github.com:you/woswoar-history.git
woswoar sync
```

On each additional machine:

```bash
woswoar init git@github.com:you/woswoar-history.git   # enrols this machine
woswoar sync
```

Then, **once**, on a machine that was already enrolled:

```bash
woswoar reencrypt && woswoar sync
```

That step exists because history sealed before the new machine joined was
encrypted to a recipient list that didn't include it. `reencrypt` re-seals the
small per-day keys — not the history itself — so it takes seconds even with
years of commands. Until you run it the new machine syncs fine, and simply
reports how many days it cannot read yet. Nothing is lost in the meantime.

### Keys

Each machine keeps its own key and **no secret is ever copied between
machines**. `init` reuses your existing SSH key when it can, and falls back to a
dedicated age key when it can't — notably when your SSH key has a passphrase,
since age cannot use ssh-agent and would fail from an unattended timer. Force
either with `--new-identity` or `--identity <path>`.

### Automatic syncing

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/woswoar-sync.* ~/.config/systemd/user/
systemctl --user enable --now woswoar-sync.timer
```

Five-minute interval by default. Sync never runs on your prompt — a git push
must not be able to block a shell.

## Status

Both milestones are done: recording and search, and encrypted git sync.

- [docs/woswoar_design_summary.md](docs/woswoar_design_summary.md) — architecture, record
  format, and the sync/encryption design with measured numbers.
- [docs/milestone-1-plan.md](docs/milestone-1-plan.md) — the implementation plan milestone 1
  was built from.

## Development

```bash
python -m unittest discover -s . -t . -p 'test_*.py'
WOSWOAR_BENCH=1 python -m unittest tests.test_perf   # latency on 52k entries
ruff check . && ruff format --check . && mypy woswoar tests
```

## License

Apache-2.0
