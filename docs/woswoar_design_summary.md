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

**Status.** Both milestones are implemented: recording, search and import; and
git sync with age encryption.

---

## Architecture

```
bash hook  ──►  plaintext TSV logs  ──►  parse cache  ──►  scope filter  ──►  fzf
   (record)          (truth)             (speed only)        (Python)         (UI)
                        │
                        └──►  age-encrypted chunks  ──►  git  ──►  remote
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
  history/                    git working tree, ciphertext only
  state.json                  sync watermarks (local, never synced)
~/.config/woswoar/machine     id + name
~/.config/woswoar/imported.json
~/.cache/woswoar/cache.txt
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

### Two fields are stored compactly

Metadata repeats on every line and sync commits every byte to git permanently, so line size directly drives repo growth.

`session` is `<start second>-<pid>`, both in hex — `6a69f856-107de`. Unique per
host: two shells cannot share a pid at one instant, and a reused pid necessarily
starts in a later second. The earlier microsecond-clock form was 23 bytes for no
extra guarantee.

`cwd` is written home-relative as `~/src/woswoar` when the directory was under
the recording user's home. The `~` means *that machine's* home, not the home of
whoever later reads the file, so it is deliberately never expanded — two synced
machines can have different usernames.

> Matched anchored (`$cwd == "$HOME"/*`), not with `${PWD/#$HOME/~}`. The latter
> rewrites `/home/martinuscopy` to `~copy` when `$HOME` is `/home/martinus`.

Together these took a representative line from 117 to 95 bytes — **19% smaller,
no functionality lost**, and the command itself went from 25% to 31% of the line.

### Fields nothing reads yet

`cwd`, `exit`, and `duration` are recorded but not currently surfaced by search.
They are kept because metadata is write-once-or-never: a display feature can be
added next month, but the exit code of a command already run can never be
recovered. Directory-scoped search and failure filtering both become impossible
for all history preceding the day they start being recorded.

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
| `history 1 > f; read < f` | **30 µs** | **0** | **full** |

`$BASH_COMMAND` looks free but is unusable: it fires once per *simple* command,
so `true a && true b` records as `true a`, and `for i in 1 2; do true $i; done`
records as `for i in 1 2` — a useless history entry. Command substitution is
faithful but forks. Redirecting the `history` builtin to a scratch file and
reading it back with `read` is faithful, fork-free, and 13× cheaper than the
fork.

### What the hook costs as a whole

The 30 µs above is the capture step, not the hook. A/B-ing a real interactive
bash — 4000 distinct commands per session, with the hook sourced and without —
puts the whole thing at **~150 µs per recorded command**. The parts, measured
inside a live shell:

| | |
|---|---|
| capture (`history 1 > f; read -d '' < f`) | 30 µs |
| `WOSWOAR_IGNORE` regex test | 54 µs † |
| escaping (command and cwd) | 12 µs |
| strip the history number | 7 µs |
| the appending `printf` | 9 µs |
| `printf -v day '%(%F)T'` | 4 µs |
| cwd anchoring, timing, dispatch | ~10 µs |
| each extra `PROMPT_COMMAND` entry | ~8 µs |

† The one row not from the run above. The pattern was broadened in issue #23,
and re-measuring it on a different machine gave 30 µs for the old pattern and
54 µs for the new one, against a record path of 288 µs → 324 µs. Treat the ratio
(**1.8×**, **+12%** end to end) as the finding and the absolutes as
machine-dependent; the other rows in this table have not been re-measured on
that machine, so they are not directly comparable.

That last row is why the wiring below keeps its `PROMPT_COMMAND` footprint to
two entries: the status capture, which has to be there, and `__woswoar_precmd`.
A third entry that only re-tested an "already wired" flag measured ~12 µs on
every command for the life of the shell, so it deletes itself once it has run.

Worth stating plainly because an earlier version of this document quoted the
30 µs capture figure as the cost of recording, which understated it by 5x.
150 µs is still imperceptible — a thousand commands cost 0.15 s in total — and
the property that actually matters is the zero in the forks column, which CI
asserts under `strace`. There is no case for optimising this further.

Everything else uses builtins: `$EPOCHSECONDS` for the timestamp,
`printf -v day '%(%F)T' -1` for the filename, `$EPOCHREALTIME` for millisecond
durations, `${var//x/y}` for escaping. `mkdir -p` runs once at shell startup,
not per command — the directory is stable across midnight, only the filename
rolls.

The result is verified rather than asserted: CI runs the hook under `strace` with
3 commands and with 30, and requires the clone count to be *identical*. Not zero
— startup legitimately forks for `mkdir` and two `trap -p` subshells, one to see
whether the EXIT trap is free and one to read any prior DEBUG trap — but flat,
which is the property that actually matters.

### Sharing the shell with everything else

woswoar is never the only thing hooked into a real `.bashrc`. A terminal-title
hook, a prompt framework, bash-preexec, atuin and ble.sh all want the DEBUG trap
and `PROMPT_COMMAND`. Two bugs came out of getting this wrong, both found by
driving a real interactive bash rather than by reading the code.

**ble.sh needs a different mechanism entirely, and says nothing when it does not
get one.** It replaces bash's prompt machinery rather than hooking into it:
`PROMPT_COMMAND` runs once, at the prompt where `.bashrc` finishes, and never
again, while the DEBUG trap fires only on ble.sh's own internals. Measured with
a probe independent of woswoar — a plain `PROMPT_COMMAND` entry and a plain
DEBUG trap, three commands typed — plain bash fired the entry 4 times and ble.sh
fired it **once**, while the trap fired 71 times, none of them a user command.
So every part of the normal wiring looks installed and records nothing.

ble.sh offers `blehook PREEXEC` and `blehook PRECMD` instead, and they are a
better fit than what they replace: PREEXEC is handed the command line as `$1`,
and PRECMD sees the real `$?`. The hook registers with those when `$BLE_VERSION`
is set and falls back to trap-and-`PROMPT_COMMAND` otherwise.

This one is worth remembering as a *testing* lesson rather than a shell one. It
was originally reported as working, because the check piped its input — and
ble.sh only activates on a tty, so it silently never loaded. Everything about
that verification looked right except that it was not testing the thing it named.
Anything involving ble.sh has to run under a real pty.

**The DEBUG trap has to be chained, and cannot be chained at load time.** A bare
`trap … DEBUG` silently replaced whatever was there, so the other tool simply
stopped working. The obvious fix — read the existing trap and call it too —
does not work from the hook, because **a sourced file cannot see the DEBUG
trap at all**: bash gives sourced files their own trap scope, so `trap -p DEBUG`
reports nothing from inside one. It is visible from a `PROMPT_COMMAND` *string*,
which is evaluated at top level, so the wiring is deferred to the first prompt.
That delay turns out to be the better design anyway: by then the whole of
`.bashrc` has run, so woswoar chains onto whoever actually ended up owning the
trap, and the order of lines in `.bashrc` stops mattering.

The handler is recovered by re-splitting what `trap -p` prints as an array —
`trap -- 'handler' DEBUG` is already shell-quoted, so `eval "parts=($spec)"`
unquotes it exactly, which hand-written unquoting does not. Handlers containing
quotes are the common case, not the exotic one. Shadowing the `trap` builtin
with a function and re-running the spec also works and reads more directly, but
a shell function is process-global and ble.sh ships its own `trap` wrapper:
defining one and unsetting it afterwards would disable ble.sh's trap manager for
the rest of the session.

The two handlers are composed into a single trap string once, at wiring time,
rather than by a wrapper that `eval`s the prior handler on every command —
measured at ~10 µs per command, with `$BASH_COMMAND` identical either way.

**Recording must not depend on that trap.** It used to be gated on a flag the
DEBUG handler set, so anything claiming the trap *after* woswoar turned
recording off entirely and silently. The history number already distinguishes
"a command ran" from "nothing happened", so that is what gates recording now,
and a lost trap costs the **duration** of a command rather than the command.

**`$?` is only the user's exit status for the *first* `PROMPT_COMMAND` entry.**
After that it is the status of the previous entry. woswoar appends itself, so
any pre-existing entry — a title hook is enough — meant every command was
recorded as having succeeded. The fix is prepended ahead of everything else,
since that is the only slot from which the real status is visible. Reading `$?`
directly looked obviously correct and was wrong in every configuration but the
empty one.

That slot can only have one occupant, so taking it comes with an obligation:
an assignment *succeeds*, which would hand every entry downstream the same
wrong `$?` — an exit-code-colouring prompt, `__git_ps1` — that this fix exists
to stop woswoar getting. So the capture restores the status immediately, via a
one-line `return "$1"` helper. bash-preexec solves it the same way, and
`return` is a builtin, so it stays fork-free.

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
covers credential-shaped assignments (`AWS_SECRET_ACCESS_KEY=`), long options
(`--password`, `--token`), credentials in a URL, `Authorization` headers, and
the few short options that carry a secret. `docs/shell-integration.md` lists
what it deliberately does not catch.

Its cost is proportional to the pattern's *length*, because `[[ =~ ]]`
recompiles on every command and bash caches nothing. That is why it carries no
leading anchor, and why `--([a-z]+-)*` is spelled out rather than `--[a-z-]*`:
the flat form is quadratic in a run of dashes, and measured 654 ms for a single
8000-character command. The reasoning for both lives next to the pattern.

### Known limitation

The hook records what *bash* recorded. Bash normalises a multi-line command into
one line when storing it, so `for i in 1 2;\ndo true;\ndone` comes back
semicolon-joined. This is bash's behaviour, not something woswoar can recover.

---

## Cache

Plain text, not a pickle — it is read on every Ctrl-R, and unpickling executes
what it reads before any check can run. Separated by NUL and the two bytes after
it, which no field can hold, so nothing needs escaping:

```
woswoar-cache-2
<relpath> <host> <size> <mtime_ns> <offset> <head hex>     one header per file
<ts> <session> <cwd> <exit> <duration_ms> <cmd>            then its entries
```

`Entry` is a `NamedTuple`, not the `@dataclass(slots=True)` originally sketched:
identical attribute access, materially cheaper to deserialise, and the whole
history is loaded on every Ctrl-R.

Entries are grouped **per file** rather than kept in one flat list. That costs a
`chain.from_iterable` on read and buys O(1) invalidation of a single file —
exactly what sync needs when it appends decrypted lines to one host's
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

### What Ctrl-R costs in a process, not in a test

Those figures are in-process, and that turns out to understate the real thing by
about half: a keypress also pays for a fresh interpreter and for fzf. On a real
54,943-entry history, `woswoar list --scope global` is **~105 ms**, cumulative:

| | cumulative |
|---|---|
| interpreter start | 8.8 ms |
| import `woswoar.__main__` | 29 ms |
| build the argparse parser | 36 ms |
| `cache.load_entries()` | 67 ms |
| filter, sort, dedup, render | 87 ms |
| write 1.5 MB to fzf | 105 ms |

Roughly half is fixed startup and half scales with history size.

A round of micro-optimisation was measured against this. `gc.disable()` and lazy
`tempfile`/`hashlib` imports looked worth 1–10 ms each in a tight in-process
loop and were **reverted**: together with the two below they moved a 60-sample
interleaved A/B by **+0.1 ms**, because a repeated call in a warm process is
simply not the same machine as a cold one-shot process. `attrgetter` as a sort
key (2.3 → 1.5 ms on 52k entries) and an early return in `escape()` for the 96%
of commands that contain none of the four characters it rewrites (10.8 → 6.7 ms
over 52k calls) were **kept**: both are strictly less work for identical output,
and neither costs a line of clarity. Neither is visible end to end, and the
honest reading is that they are below this measurement's noise floor rather than
that they do nothing.

Deferring the `importer` import was kept for the same reason plus one more: it
matches how `sync` is already handled, and it moved importing the CLI module
from 26.6 to 24.6 ms.

Going meaningfully below this needs a structural change, and both candidates are
worse than the problem:

- **Stop materialising 55k `Entry` objects.** Deserialising plain tuples is
  16 ms against 34, but reconstructing the NamedTuples costs the 13 ms back, so
  the gain only exists if search indexes tuples positionally. Worth ~20 ms of
  105, paid for in readability across the whole search path.
- **A resident daemon**, which is what the no-server design exists to avoid.

So 105 ms stands. fzf renders as it reads, so what a user perceives is closer to
the 87 ms mark.

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
- `--tiebreak=index` — preserve newest-first when match scores tie, which for a
  one-word query is nearly all of them. `begin,index` was tried first and does
  the opposite: it ranks by where in the line the match starts, so a year-old
  `sudo sync; …` sat above a three-minute-old `woswoar sync`. It also let the
  right-aligned age column rank things, since fzf scores `begin` net of leading
  whitespace and `1y` is padded one space wider than `10h`.
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

### atuin

`woswoar import atuin` reads atuin's sqlite database directly — **read-only**,
since it is very likely the live database of a running atuin, and read-only also
means woswoar cannot trigger WAL recovery on someone else's file.

Conversions, all of which are quirks of the real format rather than guesses:

| atuin | woswoar |
|---|---|
| `timestamp` in nanoseconds | seconds |
| `duration` in nanoseconds, **`-1` when the command never finished** | milliseconds, `-1` preserved |
| `cwd` absolute, or the literal string **`"unknown"`** | home-relative for this machine, `""` for unknown |
| `hostname` as `host:user` | one woswoar host per machine |
| `session` UUID | hashed to 14 hex chars, matching the hook's width |
| `deleted_at` non-null | skipped |

**Host attribution is the interesting part.** atuin syncs every machine into one
database, so an import can carry commands from many hosts — nine, in the case
this was built against. Flattening them into the importing machine would make
`--scope host` and `stats` lie, so each atuin machine gets its own woswoar host.

Ids resolve in this order, once per distinct hostname:

1. **This machine** keeps its real id, so imported history sits alongside what
   the hook records and syncs normally.
2. **A machine already known locally** — because its history has synced in —
   reuses *its* id. Without this the same peer would exist twice, under its real
   random id and under a derived one, with the same label and no way to
   reconcile them.
3. **Anything else** gets an id derived from the label (`blake2b`, 16 hex
   chars), so a second import lands in the same place instead of forking a
   duplicate machine.

The label drops the DNS domain, matching `store.default_machine_name()`. Without
that, a host whose nodename is an FQDN would file *its own* history as a foreign
machine — invisible to `--scope host`, never published by sync.

Rule 2 only helps if the peers have already met. When several machines run
woswoar, `--this-host-only` is the reliable answer: each imports its own rows
and sync distributes them, so exactly one copy of everything exists.

Idempotency is by `(timestamp, command)` per host, **not** by a position
watermark: atuin backfills *older* rows whenever it syncs from another machine,
so "everything after the last row I saw" would silently miss them.

Since sync only publishes this machine's own commands, history imported for
*other* machines stays local. That is deliberate — each machine runs the import
against its own atuin database, which avoids two machines publishing overlapping
copies of the same history.

---

## Synchronisation and encryption

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

### Choosing an identity

age does **not** use ssh-agent. A passphrase-protected SSH key prompts on a
terminal and fails outright without one — verified: it exits 1 rather than
hanging, which is the one saving grace for a systemd timer, but it still means
sync would never succeed unattended.

So `init` resolves the identity **once** and records it in
`~/.config/woswoar/machine`, rather than re-detecting per sync. It prefers an
existing SSH key, falling back to a dedicated age identity when none works. The
check is a real encrypt/decrypt round trip, not an inspection of the key file,
because the question is not what format the key claims to be — it is whether an
unattended sync will actually work.

### age never gets a path

**woswoar reads key material itself and hands `age` the bytes. No age
invocation names a file in `$HOME`.**

The rule came from a machine where `woswoar init` failed with

```
age-keygen: error: failed to open input file ".../woswoar/identity":
  open .../woswoar/identity: permission denied
```

on a file that was mode 600 and owned by the user running the command. age was
sandboxed and denied the hidden directories under `$HOME`; whether *the user*
can read a file is simply a different question from whether *age* can, and
passing a path asks the second one.

Two things made it worse than a confusing error. The round-trip check above
runs through the same decrypt, so the machine's perfectly good unencrypted
`~/.ssh/id_ed25519` failed it and was silently rejected — and the only failure
the code modelled was a passphrase, so it told the user their unencrypted key
needed one and pointed at `--new-identity`, which cannot help. The passphrase
case is now classified where the evidence is, in `_run`, from age's own stderr,
rather than inferred from which step happened to fail.

One path remains: `decrypt_with_secret` passes `/dev/fd/N`. That is a kernel
object holding an inherited pipe rather than a file in `$HOME`, and it is the
assumption the whole arrangement rests on — worth knowing if a sandbox is ever
found that blocks it too.

The rule is asserted at the seam every age call passes through, not per
function, because per-function tests only cover what someone remembered to
write one for. That is not hypothetical: the first version of this fix
converted the two identity calls and left `encrypt_to_recipients` passing
`-R recipients.txt`, so the reported machine would still have failed one step
later, at sealing the name file during `init`. The seam test catches it; a
third per-function test would not have existed.

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
history/FORMAT                                        woswoar-repo-1, plaintext
history/hosts/<id>/2026-07-29/<synctime>-<rand>.age   one plain age file
history/hosts/<id>/keys/2026-07-29.age                that day's identity, sealed to all recipients
```

**`FORMAT` names the layout, and it is there before there is anything to
upgrade.** The cache carries `woswoar-cache-2` and a manifest carries
`woswoar-manifest-v1` inside its signed bytes; the repository was the one format
woswoar owns that said nothing about itself. That is only fixable early: a
machine still on the old version does not write the marker, so a layout change
that needed one would find the repositories it has to migrate silent about which
shape they are. Written by `init` and by the first `sync` that finds it absent,
so an existing installation acquires it with nothing to run.

It is read but barely enforced. A version *newer* than the running woswoar
refuses the whole sync -- nothing exported, nothing merged, because publishing
into a shape this version does not understand would be permanent in an
append-only repo. An older version, an absent file, or content that does not
parse all read as "this shape" and proceed: the file sits in a repository anyone
with push access can write, and refusing on garbage would hand any of them a way
to stop every machine syncing with four bytes.

A chunk covers exactly one plaintext day file, so a machine offline for five
days emits five chunks. One directory per day keeps a directory to a day's
worth of chunks.

The date is deliberately **one path component and not three**. Every commit
rewrites a tree object for each level it touches, so `2026/07/29` costs two
extra objects on every sync, forever, for directories that hold exactly as many
entries either way. Replaying two years of real history at a 1-minute timer,
flattening cut tree bytes by a third and the whole repo by 10%.

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
in `keys/<date>.age`; chunks are encrypted to that day's public key alone. The
day's *public* key is stored beside it in the clear, so writing a chunk never
has to open the sealed one.

Measured with age 1.3.1, three `ssh-ed25519` recipients:

| | overhead per chunk |
|---|---|
| sealed directly to 3 recipients | 432 B |
| sealed to the day key | **200 B** |

A sealed day key is 621 B, so re-sealing two years of them is ~450 KB — which is
what makes onboarding cheap. Adding a machine re-seals ~730 tiny key files
rather than ~35,000 chunks, so `grant` takes seconds instead of rewriting
the archive.

**Compress before sealing.** A chunk's plaintext is `<tag><body>`, where the tag
picks raw or `zlib`. This is the *only* moment compression is possible: age does
not compress, and encrypted output is incompressible by definition, so once the
bytes are sealed neither git's packfile nor anything else can ever shrink them
again. Shell history is extremely repetitive, so the win is large. Small chunks
deflate to *more* than they started as — a one-line chunk is 42 B and becomes
47 — so the smaller form wins and the tag records which, at a cost of one byte
on data that already carries a 200 B age header.

**Cost: a tag per chunk.** Each chunk carries a 32-byte HMAC-SHA256 prefix
proving one of your own machines wrote it — measured, about +12% on a real
chunk file (255 B for one line, 265 B for three) and roughly +3% on the yearly
repo growth below. It buys the property that a chunk nothing holding the repo
key wrote is never opened; a header rather than a sibling file keeps the file
count, the `*.age` gitattributes glob and the never-rewritten invariant intact.

**Cost: inodes.** At a 5-minute timer and ~40 content-bearing syncs a day, one
machine produces ~35k chunk files over two years. Git copes, but a fresh clone
writes a lot of small files. An opt-in `woswoar compact` merges a completed past
day's own chunks into one; it is the only operation that deletes files, which is
exactly why it stays outside the core loop.

> `compact` reduces the **working tree**, not the repository. The chunks it
> replaces stay reachable from the commits that added them, so git keeps them
> forever — measured, compacting a 200-chunk day *added* 4.7 KB while taking the
> directory from 200 files to 1. It is an inode and clone-time tool, and
> describing it as a way to shrink the repo would be wrong.

### What a sync actually costs

The reference workload is this project author's real history: **25,997 commands
over 754 days** on their busiest machine, 3.61 MB of plaintext, replayed at a
1-minute cadence. Driving `sync.run()` itself against a local bare remote, then
`gc`, then `git count-objects -v`:

| | before | after |
|---|---|---|
| sealed chunk bytes | 6.11 MB (1.69x plaintext) | **4.35 MB (1.20x)** |
| whole repo, packed | 16.22 MB | **12.24 MB** |
| per year | 7.9 MB | **5.9 MB** |

To attribute that, each change was also measured in isolation on the same
history through a standalone harness — same `age`, same `git`, one commit per
sync, `verify-pack` totals after `gc`. Absolute figures are lower than the table
above because the harness writes chunks only, without day keys or
`recipients.txt`; the point is the deltas:

| timer | format | repo |
|---|---|---|
| 5-minute | nested date, no compression | 7.57 MB |
| 5-minute | flat date | 6.94 MB |
| 5-minute | **flat date + zlib** | **4.48 MB** |
| 1-minute | nested date, no compression | 11.51 MB |
| 1-minute | flat date | 10.31 MB |
| 1-minute | **flat date + zlib** | **8.46 MB** |

Compression pays far better at 5 minutes than at 1 — 35% against 18% — because
larger chunks give deflate more to work with. A 1-minute chunk is a couple of
lines.

Two things fell out of this that the earlier estimates missed.

**Git's own bookkeeping is about half the cost.** Per sync at a 1-minute timer:
359 B of blob (200 B of that the age header), 196 B of tree, 129 B of commit. So
684 B to carry ~160 B of compressed commands, and the fixed part cannot be
reduced further without committing less often. It also means the earlier
"~8 MB/year" figure — which counted chunk bytes only — was optimistic.

**A 1-minute timer is far cheaper than the ratio suggests.** Not 5x the cost of
a 5-minute one but roughly 2x, because real typing is bursty: going from 5
minutes to 1 took content-bearing syncs from 8 a day to 16, not from 8 to 40.
Most minutes have nothing to ship and cost one `git fetch`. That is why the
shipped timer defaults to a minute.

The corollary is that the per-year figure tracks how much you type, not the
timer. The same machine's busiest recent 61 days ran at 45 content-bearing syncs
a day and would extrapolate to 16 MB/year. Per-sync cost is the stable number;
per-year is an illustration.

Two alternatives were measured and rejected:

- **Appending age blobs to one file per day** — the hope was that an unchanged
  prefix would let git delta the append. It does, partly, but git stores enough
  near-full copies that the result is *worse*: 5.50 MB against 3.36 MB for
  chunks over the same synthetic 20 days. It wins only on inode count.
- **A flat directory per host**, with the date in the filename, is cheaper on
  trees at small scale but puts 35k entries in one tree object after two years,
  which is the problem sharding exists to avoid.

Wall-clock is not the constraint at any of these intervals: against a local
remote a no-op sync is 21 ms and a sync carrying one command is 36 ms, of which
`age` is 2.2 ms per call. Over a real network the round trip dominates, and it
happens on a timer where nothing is waiting for it.

### Sync

1. `flock` a lock file (concurrent shells, timer).
2. **Fetch and rebase first.**
3. Seal each own log file's unsealed tail into a new chunk; commit.
4. Push, retrying once after a fetch/rebase if someone else pushed meanwhile.
5. For each other host, decrypt chunks it has not already merged and append
   the plaintext to `logs/`.

> Step 2 has to come before step 3, and this was learned the hard way. Creating
> a day key seals it to whatever `recipients.txt` says *at that moment*. Export
> first and you seal the day's key to a stale recipient list, so any machine
> enrolled since the last sync can never open that day — silently, and
> permanently, because the repo is append-only. The failure only shows up with
> two machines and a new day, which is exactly the case a single-machine test
> never reaches.

Fetch-then-rebase, rather than `git pull --rebase`, because cloning an *empty*
remote configures a tracking branch that points at nothing — the normal state
for the first machine to enrol — and pulling from that fails.

`init` publishes immediately for the same class of reason: until a new machine's
public key is on the remote, `grant` run elsewhere cannot include it, and
onboarding would appear to succeed while silently granting no access to older
history.

### `grant`

Re-sealing every day key to the current recipient list is what lets a newly
enrolled machine read *old* history. It was called `reencrypt`, which named the
mechanism and hid the consequence: what the command actually does is widen who
can read the whole archive. It now says so, lists the machines by name, and asks
before proceeding -- and because woswoar parses `recipients.txt` itself rather
than handing age the path, it can carry a human label next to each key, without
which the prompt would list opaque `age1...` strings nobody could consent to.
`--yes` skips the prompt; a non-interactive run without it refuses rather than
hanging or assuming.

The list is shown *after* fetching, and the fetched list is passed back into the
operation, which refuses if it changed in the meantime. A confirmation that can
under-report what it is about to authorise is worse than none. It is deliberately a separate command — it
is one of only two operations that rewrite an existing file — but it is a
*complete* one: it fetches, re-seals, commits and pushes under the same lock
`sync` takes.

> The fetch is the part that is easy to get wrong, and it is the same trap as
> exporting before fetching. The recipient list is a **file in the working
> tree**, and the entire purpose of the operation is that a machine enrolled
> since the last sync appears in it. Re-sealing against a stale checkout
> rewrites every key back to the *old* recipients, prints `re-sealed 730 key
> file(s)`, and grants exactly nothing. It looks like success from both
> machines: the old one reports a full re-seal, the new one keeps reporting
> unreadable days.

**Only a machine that is already a recipient can do it**, because re-sealing a
key means opening it first. That is the property the design rests on, not a
missing feature — a machine nobody granted access to must not be able to grant
itself access. Running it on the new machine re-seals only what that machine
owns and reports the rest as skipped, rather than reporting a successful no-op.

Commits use a fixed `woswoar <woswoar@localhost>` identity set on the repo.
Commit metadata is one of the few things that is not encrypted, so it should not
carry a real name and address.

Each chunk is decrypted **exactly once, ever** — the plaintext under `logs/` is
the working copy. This is also why git clean/smudge filters were rejected: they
re-encrypt whole files on every `git add`, which is precisely the failure mode
above.

Triggering is the shell hook, at most once per `WOSWOAR_SYNC_INTERVAL` (default
60s), plus `woswoar sync` by hand and an optional systemd `--user` timer for
machines that should stay current while nobody is typing on them.

Never *on* a prompt, which is the constraint that has not changed: a git push
must not be able to block a shell. The hook forks a subshell that backgrounds
the sync and exits, so the prompt waits for two forks and never for the network.
The due check ahead of that is one integer comparison against a shared stamp
file, so the per-command cost is nothing until the interval has actually passed.

Prompt-triggered rather than polled because an idle machine should cost nothing:
four machines on a one-minute timer are 5,760 fetches a day whether or not
anyone typed anything, and the number of syncs that carry something is set by
how much someone types, not by how often a timer fires.

### Residual leakage

Encrypted contents and opaque machine ids still leave the number of machines,
commit timestamps, and approximate command volume per day visible. Accepted.
The one shared mutable file is `recipients.txt` (plaintext SSH *public* keys,
changed only at onboarding); `merge=union` in `.gitattributes` resolves it.
`FORMAT` is plaintext too and adds nothing: it says which layout the directory
listing is already showing. It needs no merge rule — it is written once, only
when absent, so two machines racing to create it write the same bytes.

---

## Dependencies

Runtime: **Python standard library only** — `dataclasses`, `pathlib`,
`subprocess`, `tempfile`, `time`, `secrets`, `hashlib`, `json`, `argparse`.

External binaries: `fzf` (the UI), and `age` plus `git` (sync only). Development only: `ruff`, `mypy`, `shellcheck`.

Supported: **bash 5.0+ or zsh 5.0+, on Linux and macOS.** The version floors are
the same reason in two dialects: bash 5 for `$EPOCHSECONDS` and
`$EPOCHREALTIME`, zsh 5 for the `zsh/datetime` module that provides them.
Without them the hot path would have to fork `date` on every command, giving up
the property the whole design is built around.

**macOS arrives through zsh, not through bash.** It ships bash 3.2 — a 2007
release, kept for licensing reasons — so *bash on macOS* remains out of scope
and the hook there correctly refuses to load. It also ships zsh 5.9, which is
why the platform became reachable at all once the zsh hook existed. Two
consequences worth stating rather than discovering:

- **Fork-freedom is not verified on macOS.** There is no `strace`, and `dtruss`
  needs SIP disabled, so the one property the whole design rests on is asserted
  by CI on Linux and taken on the code's word on macOS. The hook is the same
  file on both.
- Python 3.10+ is a real obstacle there: the Xcode command line tools ship 3.9.
  Homebrew or `uv` is the way in, and `docs/install.md` says so.

---

## Guiding principle

Keep it absurdly simple until profiling proves complexity is required — and when
a claim matters (fork-free, fast enough, repo growth), pin it with a test rather
than asserting it in a document.
