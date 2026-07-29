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
- **Imports** your existing `~/.bash_history` or `~/.zsh_history`, idempotently.

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

`woswoar doctor` checks the installation if something looks wrong.

## Commands

| | |
|---|---|
| `woswoar search` | interactive picker (what Ctrl-R runs) |
| `woswoar list` | plain output, used by fzf's scope-switch reload |
| `woswoar import bash\|zsh` | import an existing history |
| `woswoar stats` | entry counts, date range, most-used commands |
| `woswoar doctor` | check bash version, fzf, hook, cache |
| `woswoar sync` | git sync — **not implemented yet**, see below |

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

## Status

Milestone 1 — recording, search, and import — works. Milestone 2 is git sync
with `age` encryption, using append-only immutable chunks so a 5-minute sync
interval doesn't inflate the repository. It is fully designed but not built;
`woswoar sync` tells you so. See [woswoar_design_summary.md](woswoar_design_summary.md).

## Development

```bash
python -m unittest discover -s . -t . -p 'test_*.py'
WOSWOAR_BENCH=1 python -m unittest tests.test_perf   # latency on 52k entries
ruff check . && ruff format --check . && mypy woswoar tests
```

## License

Apache-2.0
