# woswoar — Design

Lightweight, git-based, multi-machine shell history. Inspired by Atuin, with
different goals:

- Search uses `fzf`.
- Synchronisation is git-based. No dedicated server.
- Onboarding a new machine is a clone.
- Minimal dependencies: Python standard library only, plus `fzf` for the UI and
  `age` for sync encryption.

The name is Austrian dialect for *"Was war?"* — "what was it again?" — which is
exactly the question this tool answers.

**Status.** Milestone 1 (record, search, import) is implemented. Milestone 2
(git sync with encryption) is designed below but not yet built; `woswoar sync`
exits with a message saying so.

---

## Architecture

```
bash hook  ──►  plaintext TSV logs  ──►  pickle cache  ──►  scope filter  ──►  fzf
   (record)          (truth)             (speed only)        (Python)         (UI)
                        │
                        └──►  age-encrypted chunks  ──►  git  ──►  remote   [milestone 2]
```

Two rules that everything else follows from:

1. **Git is transport and storage.** The cache exists only for speed and is
   disposable; anything it holds can be rebuilt from the logs.
2. **Python is the search engine, fzf is the UI.** Python loads, filters, sorts,
   deduplicates, and formats. fzf fuzzy-matches, displays, and selects.

---

## Layout

```
~/.local/share/woswoar/       $WOSWOAR_DIR
  logs/                       PLAINTEXT. The shell appends here. Not a git repo.
    hosts/<id>/2026-07-29.tsv
    hosts/<id>/.name          friendly label for display
  woswoar.bash                installed copy of the hook
  history/                    git working tree, ciphertext only   [milestone 2]
  state.json                                                      [milestone 2]
~/.config/woswoar/machine     id + name
~/.config/woswoar/imported.json
~/.cache/woswoar/cache.pickle
```

**Machine identity is an opaque random hex string**, not `user@hostname`. Path
components are never encrypted, so naming the directory `martin@desktop` would
publish usernames and hostnames in a synced repo even though the contents are
sealed. The friendly name lives locally in `~/.config/woswoar/machine` and is
mirrored to `hosts/<id>/.name`.

---

## Record format

Version 1: six tab-separated fields, command last.

```
ts <TAB> session <TAB> cwd <TAB> exit <TAB> duration_ms <TAB> command
```

The host is deliberately *not* a field — it is derived from the path, which
keeps every line shorter and makes a file trivially attributable.

`cwd` and `command` are escaped so a field can never contain a literal
separator: `\` → `\\`, TAB → `\t`, LF → `\n`, CR → `\r`. Backslash must be
replaced first. Decoding is a single left-to-right scan, **not** chained
`str.replace` calls — replacing `\\t` before `\\\\` (or the reverse) corrupts a
literal backslash followed by the letter `t`.

Exit code and duration are included from the start rather than deferred:
the shell hook that captures them is being written anyway, and adding fields
later would force a format migration. If the layout ever does change, new files
get a `.tsv2` suffix and the parser handles both; existing logs are never
rewritten.

Commands over 8000 characters are truncated. Linux serialises `O_APPEND` writes
to a regular file under the inode lock, so concurrent shells cannot interleave
lines regardless of size; the cap is a sanity bound against pathological pastes
and against filesystems (NFS) that make no such promise.

---

## Recording: the hot path

Recording runs on *every* prompt, so it must not fork and must not exec. That
rules out Python entirely — a bare interpreter start is ~30–50 ms — so the hook
is pure bash and writes the log line itself.

### Capturing the command

Three options were measured (2000 iterations each, bash 5.3):

| approach | cost/command | forks | fidelity |
|---|---|---|---|
| `$BASH_COMMAND` | 2 µs | 0 | **lossy** |
| `$(history 1)` | 380 µs | 1 clone | full |
| `history 1 > f; read < f` | **28 µs** | **0** | **full** |

`$BASH_COMMAND` looks free but is unusable: it fires once per *simple* command,
so `true a && true b` records as `true a`, and `for i in 1 2; do true $i; done`
records as `for i in 1 2` — a useless history entry. Command substitution is
faithful but forks. Redirecting the `history` builtin to a scratch file and
reading it back with `read` is faithful, fork-free, and 13× cheaper than the
fork.

Everything else uses builtins: `$EPOCHSECONDS` for the timestamp,
`printf -v day '%(%F)T' -1` for the filename, `$EPOCHREALTIME` for millisecond
durations, `${var//x/y}` for escaping. `mkdir -p` runs once at shell startup,
not per command — the directory is stable across midnight, only the filename
rolls.

The result is verified rather than asserted: CI runs the hook under `strace` with
3 commands and with 30, and requires the clone count to be *identical*. Not zero
— startup legitimately forks for `mkdir` and one `trap -p` subshell — but flat,
which is the property that actually matters.

> **`$EPOCHREALTIME` honours `LC_NUMERIC`.** Under a `de_AT` locale it reads
> `1785321992,048777`, with a comma. Stripping only `.` silently yields garbage
> durations. The hook strips `[.,]`, and a test pins it under `de_AT.UTF-8`.

### What gets skipped

The hook tracks the history *number* and skips when it has not advanced. That
one check subsumes blank lines, `HISTCONTROL=ignorespace`, `ignoredups`, and
`HISTIGNORE` — deferring to bash's own history rules rather than reimplementing
them, so the user's existing configuration just works.

On top of that, `$WOSWOAR_IGNORE` (an extended regex, overridable) drops
credential-shaped commands so they never reach a file that will be synced. It
defaults to things like `…TOKEN=`, `…PASSWORD=`, `--password`, `--token`.

### Known limitation

The hook records what *bash* recorded. Bash normalises a multi-line command into
one line when storing it, so `for i in 1 2;\ndo true;\ndone` comes back
semicolon-joined. This is bash's behaviour, not something woswoar can recover.

---

## Cache

```python
{"version": 1,
 "files": {relpath: [Entry, ...]},                 # grouped per file
 "meta":  {relpath: (size, mtime_ns, offset, head)}}
```

`Entry` is a `NamedTuple`, not the `@dataclass(slots=True)` originally sketched:
identical attribute access, materially cheaper to unpickle, and the whole
history is loaded on every Ctrl-R.

Entries are grouped **per file** rather than kept in one flat list. That costs a
`chain.from_iterable` on read and buys O(1) invalidation of a single file —
exactly what milestone 2 needs when a sync appends decrypted lines to one host's
log.

Update rules per file:

- unknown → parse fully
- `size` and `mtime_ns` both unchanged → skip without opening the file
- otherwise compare a hash of the first 256 bytes; if it changed, or if the file
  is now shorter than the consumed offset, the file was **rewritten** → drop its
  entries and reparse
- otherwise seek to the stored offset and parse only the appended tail
- file gone → drop its entries

`offset` tracks bytes *consumed*, which differs from `size` when the file ends
in a partially written line — a shell killed mid-append. Only whole lines are
ever consumed, so the partial line is picked up correctly once its writer
finishes it.

The cache is disposable by design: corruption, a version mismatch, or an
unreadable file all fall back to a full rebuild rather than raising. Writes go
through the shared atomic helper in `store.py`, because a half-written cache is
worse than none.

### Writing is deferred

Pickling costs ~48 ms because it is proportional to the whole history, not to
what changed. Saving on every run would put that on the Ctrl-R path
*permanently*: you always type a command before you search, so "one new line
since last time" is the normal case, not an edge case.

Measured: 42 ms without a save, 98 ms with one — the target blown by the cache
that exists to make things fast.

So the cache is only rewritten once ~2000 freshly parsed entries have
accumulated (and always on the first build, which is what makes every later run
cheap). Skipping a write just means the next run re-parses that tail at ~2 µs
per entry — about 4 ms at the threshold, buying back far more than it costs.

**Measured on 51,688 entries across 730 daily files:** cold build 112 ms, warm
load **28 ms**, `list --scope global` **62 ms**, and — the case that matters —
**42 ms for a search immediately after a new command**. Targets were 50 ms and
100 ms, so no index and no SQLite are needed. CI re-measures every run,
including the after-a-new-command path, because measuring only the unchanged
path is exactly what hid this.

### Why not SQLite

Filtering, indexing and FTS were tempting, but the dataset is small, and SQLite
adds schema management, migrations, and a dependency for no measured benefit.
The numbers above confirm it. *Do not introduce a database until profiling
proves it is required.*

---

## Search

Load → filter by scope → sort newest-first → deduplicate → render → fzf.

**Scopes.** `global` (everything synced), `host` (this machine), `session` (this
shell, via `$WOSWOAR_SESSION` exported by the hook).

**Deduplication** is on by default, keeping the most recent occurrence of each
command. On a real history this is the single biggest usability win — 51,688
entries collapse to 17,480. `--no-dedup` disables it.

**Relative times** are computed at render time from the raw timestamp, never
stored, so they cannot go stale. The column is capped at four characters, which
matters because recovering the command from what fzf prints back is a fixed
slice rather than a parse.

Each display line is `<4-char age><2 spaces><escaped command>`. The command is
re-escaped for display so one entry is always exactly one line — otherwise a
multi-line command would appear as several unrelated candidates.

**fzf flags that matter:**

- `--nth=2..` — match against the command only. Without it, typing `3d` matches
  the relative-time column and surfaces unrelated entries.
- `--tiebreak=begin,index` — preserve newest-first when match scores tie.
- `--bind=ctrl-g/ctrl-h/ctrl-s:reload(woswoar list --scope …)` — switch scope
  without leaving the picker. This is why `list` exists as its own subcommand.

Ctrl-R is bound with `bind -x`, setting `READLINE_LINE`/`READLINE_POINT` so the
selection lands on the prompt for editing rather than executing immediately.
fzf exit codes 1 (no match) and 130 (Esc) are ordinary outcomes, not failures.

---

## Import

`woswoar import bash|zsh` reads `~/.bash_history` or `~/.zsh_history`. A history
tool that starts empty is useless on day one.

- **bash** with `HISTTIMEFORMAT` has `#<epoch>` lines; without it there are no
  timestamps at all, so they are synthesised one second apart ending at the
  file's mtime. That is wrong in absolute terms — it compresses years into hours
  — but it preserves *order*, which is what ranking and deduplication depend on.
- **zsh** extended history is `: <start>:<elapsed>;<command>`, with backslash
  continuation for multi-line entries. The elapsed field becomes the duration.

Re-running is idempotent via two independent guards. A per-source count handles
the untimed case, where synthesised timestamps shift as the source grows and so
cannot identify an entry. A `(ts, cmd)` check handles everything else, and
covers the count going stale when a history file is rotated or trimmed.

---

## Synchronisation and encryption  [milestone 2]

### Threat model

A private GitHub repo protects against the public, but not against a leaked
token, an accidental flip to public, or the host itself. Shell history leaks
internal hostnames, paths, ticket IDs, and the occasional pasted credential, so
the synced form is encrypted.

Python's standard library has **no cipher** — only `hashlib`, `hmac`, `secrets`.
Encryption therefore requires an external tool. `age` was chosen: a single small
binary that accepts **SSH public keys as recipients**, so every machine uses the
keypair it already pushes to GitHub with and no secret is ever copied between
machines.

### Why the obvious approach fails

Re-encrypting a whole day file on every sync writes a fresh random blob each
time. Random ciphertext delta-compresses to nothing, so `git gc` cannot reclaim
it:

| | per day | per year |
|---|---|---|
| plaintext (baseline) | 14 KB | ~5 MB |
| whole-file re-encrypt, 40 syncs/day | ~280 KB | **~100 MB** |
| immutable chunks (below) | ~22 KB | **~8 MB** |

### Immutable, write-once chunks

Each sync encrypts only the lines added since the last sync into a brand-new,
never-modified file:

```
history/hosts/<id>/2026/07/29/<synctime>-<rand>.age   one plain age file
history/hosts/<id>/keys/2026-07-29.age                that day's identity, sealed to all recipients
```

A chunk covers exactly one plaintext day file, so a machine offline for five
days emits five chunks. Date sharding keeps ~40 files per leaf directory.

Nothing in the repo is ever modified or deleted, and that is what makes this
simpler than the alternatives:

- **No container format.** No length framing, no partial-read logic, no
  truncated-segment failure mode. A chunk is a plain `age` file.
- **Growth is deterministic.** An append-only single file would depend on git
  finding good pack deltas for *binary* blobs. Write-once files need no delta at
  all: growth is exactly the bytes written.
- **`git pull --rebase` cannot conflict.** Not "rarely" — a commit that only
  adds paths under a host-owned prefix has nothing to conflict with.
- **State is two integers.** Bytes encrypted so far (own files) and last chunk
  merged per (host, day).

**Per-day key.** One X25519 identity per host per day, sealed to all recipients
in `keys/<date>.age`; chunks are encrypted to that day's public key alone. This
halves per-chunk header overhead (~200 B instead of ~450 B with three
`ssh-ed25519` recipients), and more importantly makes onboarding cheap: adding a
machine re-seals ~730 tiny key files rather than ~35,000 chunks, so `reencrypt`
takes seconds instead of rewriting the archive.

**Cost: inodes.** At a 5-minute timer and ~40 content-bearing syncs a day, one
machine produces ~35k chunk files over two years. Git copes, but a fresh clone
writes a lot of small files. The sync interval is configurable — 15 minutes
roughly halves both file count and header overhead — and an opt-in
`woswoar compact` can merge a completed past day's own chunks into one. Compaction
is the only operation that deletes files, which is exactly why it stays outside
the core loop.

### Sync

1. `flock` a lock file (concurrent shells, timer).
2. For each own log file with unencrypted tail bytes: encrypt that tail into a
   new chunk; `git add`; commit; `git pull --rebase`; push.
3. For each other host: decrypt chunks newer than the merge watermark and append
   the plaintext to `logs/`.
4. Update the cache incrementally.

Each chunk is decrypted **exactly once, ever** — the plaintext under `logs/` is
the working copy. This is also why git clean/smudge filters were rejected: they
re-encrypt whole files on every `git add`, which is precisely the failure mode
above.

Triggering is manual (`woswoar sync`) plus an optional systemd `--user` timer.
Never on a prompt: a git push must not be able to block a shell.

### Residual leakage

Encrypted contents and opaque machine ids still leave the number of machines,
commit timestamps, and approximate command volume per day visible. Accepted.
The one shared mutable file is `recipients.txt` (plaintext SSH *public* keys,
changed only at onboarding); `merge=union` in `.gitattributes` resolves it.

---

## Dependencies

Runtime: **Python standard library only** — `dataclasses`, `pathlib`, `pickle`,
`subprocess`, `tempfile`, `time`, `secrets`, `hashlib`, `json`, `argparse`.

External binaries: `fzf` (the UI), `age` (milestone 2 only), `git` (milestone 2
only). Development only: `ruff`, `mypy`, `shellcheck`.

Supported: **bash 5.0+ on Linux.** bash 5 is required for `$EPOCHSECONDS` and
`$EPOCHREALTIME`; without them the hot path would have to fork `date` on every
command, giving up the property the whole design is built around. macOS ships
bash 3.2 and is out of scope.

---

## Guiding principle

Keep it absurdly simple until profiling proves complexity is required — and when
a claim matters (fork-free, fast enough, repo growth), pin it with a test rather
than asserting it in a document.
