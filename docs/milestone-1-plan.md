# woswoar — implementation plan

## Context

`woswoar` is a Git-based, multi-machine shell history tool (an Atuin alternative with no server, `fzf` as the UI, minimal dependencies). The repo today contains only `README.md` and `woswoar_design_summary.md` — 452 lines of converged design, zero code.

**Milestone 1** = record + search, local only: bash hooks, TSV logs, incremental cache, scope filtering, Ctrl-R via fzf, import of existing bash/zsh history, plus CI.
**Milestone 2** = git sync with append-only `age` encryption (designed here in full so milestone 1 doesn't paint us into a corner; not built yet).

Decisions resolved with the user:

| Decision | Choice |
|---|---|
| Record path | Pure shell append — no Python on the hot path |
| Milestone 1 scope | Record + search + import + CI; sync deferred |
| Shells | bash only |
| Support floor | bash 5.0+, Linux only |
| Sync trigger | Manual `woswoar sync` + optional systemd `--user` timer |
| Encryption | `age`, recipients are the machines' SSH public keys |
| Ciphertext layout | **Immutable write-once chunk files with a per-host-per-day key** (see below) |

## Gaps in the current design doc that this plan closes

1. **Escaping** — `ts<TAB>session<TAB>cwd<TAB>command` breaks on any command containing a tab or newline (multi-line `for` loops are routine).
2. **Fork-free recording** — `$(date +%F)` in a prompt hook forks per command; everything hot must be a bash builtin.
3. **Exit code + duration** — listed as "future additions", but the hook is being written *now*; adding them later forces a format migration.
4. **File rewrite detection** — the cache keys on byte offsets, and a pull can make a file *shrink*. Size-only comparison silently corrupts.
5. **Secrets** — no filter means `export AWS_SECRET=…` gets committed to a synced repo forever.
6. **Encryption + repo growth** — the doc doesn't cover confidentiality at all, and the naive approach makes the repo unusable at a 5-minute sync interval.

---

# Encryption design (the repo-growth problem)

Python's standard library has **no cipher** — only `hashlib`/`hmac`/`secrets`. Encryption therefore requires an external tool; `age` is the choice (single static binary, accepts SSH public keys as recipients, so no secret is ever copied between machines).

## Why the obvious approach fails

Re-encrypting the whole day file on every sync produces a fresh random blob each time. Random ciphertext delta-compresses to nothing, so `git gc` can't reclaim it:

| | per day | per year |
|---|---|---|
| plaintext (baseline) | 14 KB | ~5 MB |
| whole-file re-encrypt, 40 syncs/day | ~280 KB | **~100 MB** |
| immutable chunk files (below) | ~22 KB | **~8 MB** |

## The fix: immutable, write-once chunk files

Each sync encrypts **only the lines added since last sync** into a brand-new, never-modified file named for the sync time:

```
history/hosts/<id>/2026/07/29/1753781234-a3f2.age     one plain age file. That's the whole format.
history/hosts/<id>/keys/2026-07-29.age                that day's identity, encrypted to all recipients
```

A chunk covers exactly one plaintext day file, so it lands in that day's directory; a machine that was offline for five days emits five chunks on its next sync. Date sharding keeps ~40 files per leaf directory instead of tens of thousands in one.

Nothing in the repo is ever modified or deleted. That is what makes this simpler than every alternative:

- **No container format.** No `u32be length` framing, no partial-read logic, no truncated-segment failure mode, no `crypto.py` framing tests. A chunk is a plain `age` file — `age -d` it and you have lines.
- **Growth is deterministic.** The append-only variant's ~8 MB/year depended on git finding good pack deltas for *binary* blobs. Write-once files need no delta at all — repo growth is exactly the sum of the bytes written.
- **`git pull --rebase` cannot conflict.** Not "rarely" — a commit that only ever adds new paths under a host-owned prefix has nothing to conflict with.
- **State shrinks to two integers per file.** "Which plaintext bytes have I encrypted" (own files) and "which chunk did I last merge" per (host, day). No byte offsets into ciphertext.

Ordering within a day doesn't matter — entries carry their own timestamps and are sorted in Python regardless — but zero-padded epoch filenames sort chronologically anyway, which makes the merge watermark a simple string comparison.

### Per-host-per-day key

One X25519 identity per host per day, wrapped for all recipients in `keys/<date>.age`; chunks are encrypted to that day's public key alone. Two reasons to keep this indirection:

1. **Size** — encrypting each chunk directly to all recipients costs an `age` stanza per recipient per chunk (~110 B for `ssh-ed25519`, ~380 B for `ssh-rsa`). With a day key it's ~200 B/chunk instead of ~450 B, roughly halving overhead on 14 KB/day of data.
2. **Cheap onboarding** — adding a machine re-encrypts only the ~730 tiny key files, never the ~35k chunks. `woswoar reencrypt` stays seconds rather than an archive rewrite.

The byte figures are estimates from the age v1 spec — `age` isn't installed on this machine. The CI size test below measures them for real and is what the design is held to.

### The cost: inode count

Write-once chunks trade bytes for files. At a 5-minute timer and ~40 content-bearing syncs a day, one machine produces ~35k chunk files over two years; three machines ≈ 90k paths in the repo. Git copes (index ~10 MB, `git status` a few hundred ms), but a fresh clone writes 90k small files.

Mitigations, in order of preference:
- The sync interval is configurable and the trade-off documented — 15 minutes roughly halves the file count and the header overhead. Default stays 5 minutes as requested.
- `woswoar compact` (optional, not run automatically): merge a *completed past* day's own chunks into one. Only touches files this host owns, so it stays conflict-free. It's the one operation that deletes files, which is exactly why it's opt-in and not part of the core loop.

CI measures the file count so it can't drift unnoticed.

## Two directories, not git filters

```
~/.local/share/woswoar/
  logs/                     PLAINTEXT. The shell appends here. Not a git repo.
    hosts/<id>/2026-07-29.tsv
  history/                  the git working tree — ciphertext only, write-once
    recipients.txt          plaintext SSH *public* keys (not secret)
    hosts/<id>/keys/2026-07-29.age
    hosts/<id>/2026/07/29/<synctime>-<rand>.age
  state.json                own: plaintext bytes encrypted so far
                            others: last chunk merged, per (host, day)
~/.cache/woswoar/cache.pickle
```

`woswoar sync` is then:

1. `flock` a lock file (concurrent shells / timer).
2. For each own log file with unencrypted tail bytes: encrypt that tail into a new chunk file; `git add` the new paths; commit; `git pull --rebase`; push.
3. For each *other* host: decrypt chunks newer than the merge watermark → append plaintext to `logs/`.
4. Update the cache incrementally.

Each chunk is decrypted **exactly once, ever** — the plaintext under `logs/` is the working copy, and it's what milestone 1's cache already reads. This is also why git clean/smudge filters were rejected: they'd re-encrypt whole files on every `git add`, which is precisely the failure mode above.

A fresh clone is the one bulk decrypt (2 years ≈ 35k `age` invocations); run it parallelised across cores, once.

The plaintext side stays a simple per-day append file — the shell hook must remain fork-free and dumb. Chunking exists only at the git boundary.

## Conflicts and metadata leakage

Each host only ever *adds* files under its own `hosts/<id>/` prefix, so merge conflicts are structurally impossible — there is no modified blob for git to reconcile. Even two concurrent syncs on the same machine produce distinct filenames (`flock` serialises them anyway). The single shared mutable file is `recipients.txt`, which changes only at onboarding — with `merge=union` in `.gitattributes`, even that resolves itself.

Path names leak: `hosts/martin@desktop/` publishes your usernames and machine names in cleartext even though the contents are encrypted. So the machine id is an **opaque random hex string** generated once at install, with the friendly name kept locally and mirrored into an encrypted `hosts/<id>/name.age`. (This is decided now because milestone 1 already writes these paths.) Remaining, accepted leakage: number of machines, commit timestamps, and approximate command volume per day.

---

# Milestone 1 — record and search

## Record format (v1, 6 tab-separated fields, command last)

```
ts <TAB> session <TAB> cwd <TAB> exit <TAB> duration_ms <TAB> command
```

Escaping on `cwd` and `command`, in order: `\`→`\\`, TAB→`\t`, LF→`\n`. Parse with `line.split("\t", 5)`. Lossless round-trip, files stay grep-able. A future format change writes a `.tsv2` suffix rather than rewriting existing logs.

Commands over 4000 bytes are truncated with a trailing marker, so one `printf` stays under `PIPE_BUF` and concurrent appends from multiple shells remain atomic.

## Code structure

Stdlib only at runtime. `fzf` required; `age` required only for milestone 2.

```
pyproject.toml            hatchling, zero runtime deps, console_script `woswoar`
woswoar/
  __main__.py             argparse dispatch
  entry.py                Entry NamedTuple, escape/unescape, parse/format line
  store.py                paths, opaque machine id, log enumeration
  cache.py                load / incremental update / save
  search.py               scope filter, dedup, relative time, fzf
  importer.py             bash + zsh history import
  crypto.py               milestone 2 — chunk encrypt/decrypt via `age`, behind a clean seam
shell/woswoar.bash
tests/
.github/workflows/ci.yml
```

`Entry` is a `NamedTuple` rather than the doc's `@dataclass(slots=True)` — same attribute access, materially cheaper to unpickle.

CLI: `search`, `list`, `import`, `install`, `stats`, `doctor`; `sync`/`reencrypt` stubbed with a clear "milestone 2" error.

## 1. `shell/woswoar.bash` — the hot path

Entirely fork-free using bash 5 builtins: `$EPOCHSECONDS`, `printf -v day '%(%F)T' -1`, `$EPOCHREALTIME` for millisecond durations, `${var//x/y}` for escaping. `mkdir -p` runs **once at startup**, not per command (the directory is stable across midnight; only the filename rolls).

preexec/precmd without the `bash-preexec` dependency, ~50 lines:
- `trap … DEBUG` captures `$BASH_COMMAND` and the start time; guarded against `$COMP_LINE` (completion) and against re-firing per simple command in a pipeline.
- `PROMPT_COMMAND` prepends `__woswoar_precmd`, which reads `$?` as its very first statement, then writes the line.

Skip rules, all in-shell: empty command; leading space (the `HISTCONTROL=ignorespace` convention); match against `$WOSWOAR_IGNORE`, an extended regex tested with `[[ =~ ]]`, defaulting to common secret shapes (`export …TOKEN|SECRET|PASSWORD|KEY`, `--password`, `curl -u`).

Ctrl-R uses `bind -x` setting `READLINE_LINE`/`READLINE_POINT` — the selection lands on the prompt for editing, not executed. `woswoar install` copies the hook to `$WOSWOAR_DIR` and appends one marker-guarded `source` line to `.bashrc`; sourcing a static file keeps Python out of shell startup too.

## 2. `cache.py` — incremental load

```python
{"version": 1,
 "files": {relpath: [Entry, …]},     # grouped per file, not one flat list
 "meta":  {relpath: (size, mtime_ns)}}
```

Per-file grouping (vs the doc's flat list) makes single-file invalidation O(1) — exactly what milestone 2's pulls need. Update rules: unknown → full read; `size >` recorded → seek and parse the tail only; `size <` recorded or mtime moved backwards → **rewritten**, drop and re-read; missing → drop. Written via `tempfile` + `os.replace`. Any corruption or version mismatch falls back to a full rebuild — the cache is disposable by design.

## 3. `search.py`

Load cache → scope filter (`global` / `host` / `session` via `$WOSWOAR_SESSION`) → sort newest-first → dedup → render → fzf.

- **Dedup** on by default (`--no-dedup` to disable), keeping the most recent of each identical command. On a 52k history this is the single biggest usability win.
- Relative times (`2m`, `1h`, `3d`) computed at render time from raw timestamps, padded to fixed width.
- fzf: `--delimiter='\t' --with-nth=1,2` to display, and critically **`--nth=2..`** so fuzzy matching hits the command only — without it, typing `3d` matches timestamps. Plus `--ansi` to dim the time column, `--tiebreak=index` to hold newest-first on ties, `--query` seeded from the readline buffer, and `--bind 'ctrl-g:reload(woswoar list --scope=global)'` (likewise `ctrl-h`, `ctrl-s`) for scope switching without leaving the picker — which is why `list` exists as its own subcommand. Exit code 130 (Esc) is a clean no-op.

## 4. `importer.py`

- **bash** `~/.bash_history`: with `HISTTIMEFORMAT`, entries are preceded by `#<epoch>` lines; without it there are no timestamps, so assign synthetic ones spaced backwards from the file mtime to preserve ordering.
- **zsh** `~/.zsh_history`: extended format `: <epoch>:<elapsed>;<cmd>`, including `\`-continued multi-line entries.

Imported entries land in normal `hosts/<id>/<date>.tsv` files bucketed by day, session `import`, exit/duration `-1`. Re-import is idempotent (same ts + command is skipped).

## 5. Update `woswoar_design_summary.md`

Fold the resolved decisions back in so the doc stays the source of truth: the 6-field format and escape scheme, the fork-free hot path, per-file cache grouping with rewrite detection, ignore patterns, opaque machine ids, and the full encryption/sync section above.

---

# CI and regression tests

`.github/workflows/ci.yml`, ubuntu-latest. Runtime deps stay zero; `ruff` and `mypy` are dev-only.

| Job | What it does |
|---|---|
| `lint` | `ruff check`, `ruff format --check`, `mypy --strict`, `shellcheck shell/woswoar.bash` |
| `test` | `python -m unittest discover` on Python 3.10 / 3.12 / 3.14 |
| `hook` | Drives the real hook in a bash 5 subshell against a temp `$WOSWOAR_DIR`; runs a command containing a literal tab and a newline and asserts `parse_line()` recovers it byte-for-byte. **This is what keeps the shell and Python escaping implementations from drifting.** |
| `fork-free` | Runs a command under the hook with `strace -f -e trace=clone,fork,execve` and asserts the record path spawns **zero** processes. |
| `perf` | Generates 52k entries (the doc's real-world figure); asserts cache load < 50 ms and `list --scope=global` < 100 ms. Prints actuals; fails at 3× target so shared runners don't flake. |
| `e2e` | *(milestone 2)* Installs `age` + `fzf`, builds two fake machines with separate SSH keys against a bare repo on disk, and runs record → sync → pull → decrypt → search, asserting machine B sees machine A's commands. The regression test for the entire premise. |
| `immutability` | *(milestone 2)* After a simulated multi-day, multi-machine run, asserts `git log --diff-filter=MD --name-only -- 'hosts/**/*.age'` is **empty** — no chunk was ever modified or deleted. This is an exact invariant, not a heuristic, and it's what makes a future refactor unable to silently reintroduce whole-file re-encryption. |
| `repo-growth` | *(milestone 2)* Simulates 40 syncs × 200 lines over several days, runs `git gc`, and asserts packed `.git` stays within ~2× the plaintext size and the working-tree file count matches the predicted chunk count. Emits the measured per-chunk `age` overhead so the estimates above get replaced by real numbers. |

Unit tests (stdlib `unittest`): escape/unescape round-trip over tabs, newlines, backslashes, unicode and oversized commands; cache fresh-build / append / truncation / deleted-file / corrupt-pickle paths; scope filtering and dedup ordering; importer for bash with and without `HISTTIMEFORMAT`, zsh extended format, multi-line entries, idempotent re-import.

## Manual verification

`woswoar install` into a scratch `$HOME`, open a fresh bash, run several commands (including a failing one and a `sleep 2` to confirm exit code and duration land), press Ctrl-R, fuzzy-search, cycle scopes with Ctrl-G/H/S, select, and confirm the command arrives on the prompt un-executed. Then `woswoar doctor` (checks bash ≥ 5, fzf, dirs, hook installed, cache health) and `woswoar stats`.
