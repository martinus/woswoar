# Living in your shell

woswoar is never the only thing hooked into a real `.bashrc`, and it runs on
every prompt. Both of those constrain it more than anything else in the design.

There are two hooks: `woswoar.bash`, which `woswoar install` wires up, and
`woswoar.zsh`, which for now you [source by hand](#zsh). Everything below is
about the bash one unless it says otherwise; the zsh section covers what is
genuinely different, and it is almost all about *not* recording.

## Your normal bash is untouched

**Up-arrow and `~/.bash_history` still work exactly as before.** woswoar never
modifies `HISTFILE`, `HISTSIZE`, `HISTCONTROL` or the history list — it only
*reads* `history 1`. It is purely additive: a second, richer log next to the one
bash already keeps. `HISTTIMEFORMAT` is not disturbed either; the hook's
override is scoped to its own call.

## It shares the hooks rather than taking them

A terminal-title hook, a prompt framework, bash-preexec and atuin all want the
same two places: the `DEBUG` trap and `PROMPT_COMMAND`.

woswoar chains onto whatever owns the DEBUG trap **at the first prompt** — not
at the moment it is sourced — so it picks up whoever ended up owning it once
your whole `.bashrc` has run. That is not fussiness: a sourced file cannot see
the DEBUG trap at all, and doing it late means the order of lines in `.bashrc`
stops mattering.

It also restores `$?` after reading it, so an exit-code-colouring prompt
downstream still sees the real status rather than the 0 that woswoar's own
assignment would otherwise leave behind.

If something else claims the trap *after* woswoar, you lose the recorded
**duration** of those commands and nothing else — the commands themselves are
still recorded.

### ble.sh

[ble.sh](https://github.com/akinomyoga/ble.sh) needs a different mechanism
entirely, because it replaces bash's prompt machinery rather than hooking into
it: `PROMPT_COMMAND` runs once and never again, and the DEBUG trap only ever
sees ble.sh's own internals. woswoar registers with `blehook PREEXEC`/`PRECMD`
when it finds ble.sh loaded.

Nothing to configure. It works in either load order and alongside atuin, and it
is pinned by tests that drive a real interactive bash.

## Ctrl-R

A **half-typed line becomes the search query**. Type `docker comp`, press
<kbd>Ctrl</kbd>+<kbd>R</kbd>, and fzf opens already filtered to that. Escape
leaves your line byte-for-byte as it was; picking something replaces the line and
puts the cursor at the end, never executing it.

Switch scope without leaving the picker:

| key | scope |
|---|---|
| <kbd>Ctrl</kbd>+<kbd>G</kbd> | **global** — every machine |
| <kbd>Ctrl</kbd>+<kbd>H</kbd> | **host** — this machine |
| <kbd>Ctrl</kbd>+<kbd>S</kbd> | **session** — this shell |
| <kbd>Ctrl</kbd>+<kbd>O</kbd> | **dir** — this directory and below |

<kbd>Ctrl</kbd>+<kbd>R</kbd> inside the picker walks the same four in that order.

**`dir` spans machines, and that is deliberate.** `~/src/woswoar` on your laptop
and on your desktop is the same project, and that cross-machine question is the
one woswoar exists to answer — so the directory scope does *not* also narrow to
this machine. The machine column names where each row came from. (The scopes do
not compose, so making `dir` imply `host` would remove the only way to ask it.)

It matches what was *recorded*, which is the logical path: after `cd` through a
symlink the hook stores the path you typed, and so does the lookup.

`WOSWOAR_NO_BIND=1` skips the <kbd>Ctrl</kbd>+<kbd>R</kbd> binding entirely if
you would rather keep another tool's.

### The details pane

Six fields are recorded per command and the list has room for two. Press
<kbd>Ctrl</kbd>+<kbd>/</kbd> for the rest of the highlighted one:

```
when     2026-08-10 14:32:07  (3h12m ago)
dir      ~/src/woswoar
host     thinkpad
session  6a79f245-36ea53
exit     0
took     1.2 s

docker compose up -d --build
```

Every field is named, and one that was never recorded says `not recorded`
instead of vanishing — an imported history has no directory, no exit code, no
duration and no session, so a table with holes in it would be most of a fresh
install. The labels are `dir`, `host` and `session` because those are also three
of the four scopes: the pane names the key that would narrow the list to what it
is describing.

The exit code is green when it is zero and red when it is not; a code nobody
recorded is neither, for the same reason the list does not paint an imported
history red. The command comes last and outside the table — it is the one field
with no bound on its length, and the pane has a fixed number of rows. It is also
shown whole here, where the list clips it at the window edge.

**It starts hidden, and that is on purpose.** Rendering it forks a fresh
interpreter and reads the whole cache — **79 ms** on a 54,000-command history,
against 85 ms for the entire list — and that is paid again for every row the
cursor passes over. Visible by default it is felt as lag under a held arrow key;
hidden, it costs nothing at all until you ask.

It needs fzf 0.45+, like <kbd>Ctrl</kbd>+<kbd>R</kbd> cycling and
<kbd>Ctrl</kbd>+<kbd>T</kbd>. On anything older the key is neither bound nor
advertised.

## zsh

Add the hook **last** in `~/.zshrc`:

```zsh
source ~/.local/share/woswoar/woswoar.zsh
```

`woswoar install` does not write that line yet — it installs the bash hook and
edits `.bashrc` — so for now this one line is yours to add. Everything else is
shared: the same machine id, the same `logs/hosts/<id>/<day>.tsv`, the same
sync. A machine that runs both shells records into one history and every
<kbd>Ctrl</kbd>+<kbd>R</kbd> in either sees the other's commands, with nothing
to exchange in between.

**Last, because whoever binds <kbd>Ctrl</kbd>+<kbd>R</kbd> last wins.** Oh My
Zsh, Prezto and atuin all bind it when they load. `WOSWOAR_NO_BIND=1` keeps
theirs instead.

Needs **zsh 5.0+**. The hook says so and switches itself off on anything older
rather than half-working.

### What zsh records that bash does not

A multi-line command keeps its **newlines**. bash reads the line back out of
`history`, which joins it with semicolons; zsh hands the hook the buffer you
typed. Both are faithful to their shell. In the picker such a command shows a
visible `\n`, which is deliberate — see the note in `entry.make_inert`.

### The history rules are reimplemented, not inherited

This is the one place the zsh hook is *less* elegant than the bash one, and it
is worth knowing about rather than glossing.

The bash hook never decides what to skip: it checks whether bash's history
number moved, so `HISTCONTROL`, `ignoredups` and `HISTIGNORE` apply for free and
there is only ever one set of rules. zsh offers no equivalent signal. `preexec`
fires for every line, including the ones zsh has already thrown away; `$HISTCMD`
moves *backwards* over one of those, so watching it would drop the next real
command as well; and `$history` still holds a space-prefixed command at the
moment the hook runs, so reading that would publish exactly what you hid.

So the hook applies the rules itself. Mirrored:

| setting | what the hook does |
|---|---|
| `HIST_IGNORE_SPACE` | skips a line that starts with whitespace |
| `HIST_IGNORE_DUPS`, `HIST_IGNORE_ALL_DUPS` | skips a line identical to the one before it |
| `HISTORY_IGNORE` | skips a line matching the pattern, in your `EXTENDED_GLOB` dialect |

**Not** mirrored, in the same spirit as the list of what `$WOSWOAR_IGNORE` does
not catch: `HIST_NO_STORE`, `HIST_SAVE_NO_DUPS`, `HIST_EXPIRE_DUPS_FIRST` and
`HIST_REDUCE_BLANKS`. A command those would keep out of `~/.zsh_history` is
still recorded by woswoar. The first three are about pruning a fixed-size
history file, which a log that only appends does not have; the fourth
reformats rather than drops. If you rely on one of them to keep something out
of a synced file, use `WOSWOAR_IGNORE_EXTRA` instead, which both shells and
`woswoar import` honour.

`HIST_IGNORE_SPACE` is the one that matters most, and it is the one with the
most tests: a leading space is how people keep a secret out of history, and a
miss here is not merely recorded but encrypted, pushed and pulled onto every
other machine.

### Coexistence

`precmd` and `preexec` are lists in zsh, so nothing has to be chained or
restored: the hook registers with `add-zsh-hook` and every other participant
keeps working. zsh hands each `precmd` entry your real `$?`, so an
exit-code-colouring prompt downstream is unaffected — none of the
`PROMPT_COMMAND` apparatus the bash hook needs exists here.

Two known rough edges, neither of them verified against an install of the thing
in question:

- **zsh-autosuggestions** wraps the widgets it knows about, and one defined
  after it loads is not among them, so the ghost suggestion may linger after
  <kbd>Ctrl</kbd>+<kbd>R</kbd>. The remedy is
  `ZSH_AUTOSUGGEST_CLEAR_WIDGETS+=(__woswoar_widget)`.
- **Powerlevel10k's instant prompt** captures output written before the first
  prompt and warns about it. The hook prints at load time in exactly one case —
  the machine has no identity yet, so run `woswoar install`.

### macOS

Still unsupported, and zsh does not change that on its own. The hook's central
claim is that it does not fork on the per-command path, and CI proves that with
`strace`, which macOS does not have. That is a separate piece of work.

## Commands that are never recorded

Anything bash itself keeps out of history is invisible to woswoar, so
`HISTCONTROL=ignorespace` (a leading space) and `HISTIGNORE` work exactly as they
already do — a command bash declines to store is never seen here. Under zsh the
same three rules apply but are [reimplemented rather than
inherited](#the-history-rules-are-reimplemented-not-inherited).

On top of that, `$WOSWOAR_IGNORE` is an extended regex matched against every
command, and anything it matches is dropped before it reaches a file that gets
synced. What the default catches:

| | example |
|---|---|
| an assignment whose name reads like a credential | `AWS_SECRET_ACCESS_KEY=…`, `PGPASSWORD=…`, `API_KEY=…` |
| a long option that names one | `--password`, `--token`, `--secret-key`, `--with-token`, `--from-literal=` |
| credentials inside a URL | `https://user:token@github.com/…` |
| an `Authorization` header | `curl -H "Authorization: Bearer …"` |
| three tools that exist to take a password — and only these three | `sshpass`, `htpasswd`, `openssl passwd` |
| the short options that carry one | `curl -u`, `mysql -p<pw>`, `docker login -p`, `ssh-keygen -N` |

### What it does not catch

This list is the point of this section, and every line of it is pinned by a test
(`DOCUMENTED_GAPS` in `tests/credential_shapes.py`) so it cannot quietly stop being
true. **No pattern catches everything**:

- **A secret with no tell.** `deploy.sh AKIAIOSFODNN7EXAMPLE` looks like any
  other argument to the hook. (`woswoar import` recognises the well-known token
  formats — see below — but the hook cannot afford to.)
- **A tool that is not on the list above**, or one that takes its secret as a
  positional argument or a bare flag: `redis-cli -a`, `az login -p`,
  `mongosh -p`, `aws configure set aws_secret_access_key …`,
  `vault kv put … value=…`, `smbclient -U user%pass`, `pscp -pw`. These *are* secrets and
  the **hook** does record them. Chasing them means an unbounded list of program
  names, and the hook is priced per character on every prompt — so they are
  written down here instead of half-covered. `woswoar import` does catch them
  (see below), because it runs once rather than on every keystroke.
- **A bare `KEY=` or `PASS=`.** `KEY` and `PASS` are matched only after an
  underscore (`SSH_KEY=`, `DB_PASS=`), because matching them anywhere would eat
  `MONKEY=`, `KEYS=` and `PASSAGE=`. Deleting real history silently is the worse
  failure.
- **Lower-case assignments.** `token=abc` is not matched; `TOKEN=abc` is.
- **A heredoc or a pasted multi-line block**, where the secret is on a line bash
  never put in history as its own command.
- **Anything typed into a prompt** rather than onto the command line — which is
  why `mysql -p` with no value, `docker login` with no `-p`, and `gh auth login`
  without `--with-token` are all deliberately recorded: there is no secret on
  the line.

It errs the other way once: `docker login --password-stdin` is dropped even
though the password arrives on stdin. Over-matching a login command costs one
history entry, so it is not worth another alternative to exclude.

### `woswoar import` filters too, and looks harder

The same rules run over anything `woswoar import` reads. That matters more than
the hook does on day one: a `~/.bash_history` or an atuin database was recorded
over years with no filter of any kind, and importing it publishes the lot.

Because an import runs once instead of on every prompt, it can afford what the
hook cannot, and so it also recognises:

- **token formats on sight** — `AKIA…`, `ghp_…`, `glpat-…`, `xox…`,
  `sk_live_…`, `AIza…`, a JWT, a `-----BEGIN … PRIVATE KEY-----` block
- **the tools listed as gaps above**, which is all of them — `aws configure
  set …secret…`, `vault kv put … value=`, `redis-cli -a`, `mongosh -p`,
  `az login -p`, `smbclient -U user%pass`, `pscp -pw`

It reports what it dropped rather than doing it quietly:

```console
$ woswoar import bash
~/.bash_history: 8213 parsed, imported 8196, 17 skipped as credential-shaped
```

There is deliberately **no entropy heuristic**. Shell history is full of
high-entropy strings that are not secrets — git SHAs, checksums, UUIDs, base64
payloads — and dropping a command is silent and permanent, so precision matters
more than recall.

### Adding a rule of your own

`WOSWOAR_IGNORE_EXTRA` is joined onto the default, and applies to imports as
well as to the hook:

```bash
export WOSWOAR_IGNORE_EXTRA='deploy-to-prod|MYCORP_[A-Z_]*='
```

Prefer this to overriding `WOSWOAR_IGNORE`, for two reasons. Replacing the
default means copying ~450 characters into your `.bashrc` and never receiving
the next fix to them — and the default grew precisely because an earlier version
missed `AWS_SECRET_ACCESS_KEY=`.

> [!IMPORTANT]
> `WOSWOAR_IGNORE` reaches the **hook only**. It is a POSIX extended regex,
> which Python cannot compile, so `woswoar import` does not read it. A rule you
> put there filters what you type and *not* what you import. Put it in
> `WOSWOAR_IGNORE_EXTRA`, which both paths honour.

Setting `WOSWOAR_IGNORE=` empty disables the filter for the hook; the importer's
built-in rules always apply.

> [!TIP]
> The filter runs on every command and bash recompiles the regex each time, so
> its cost is proportional to the pattern's *length*, not to how much of it can
> match. Broadening the default to the table above measured **1.8×** the previous
> pattern's cost and **+12%** on the whole record path. A much longer custom
> pattern is felt on every prompt.

## What it costs

Measured on a real **54,943-entry** history across ~750 daily files:

| | |
|---|---|
| record a command | **~150 µs**, 0 forks |
| decide whether a sync is due | **~5 µs**, 0 forks |
| start a background sync | once per `WOSWOAR_SYNC_INTERVAL`, 2 forks, no wait |
| <kbd>Ctrl</kbd>+<kbd>R</kbd>, whole process | **~105 ms** |
| one <kbd>Ctrl</kbd>+<kbd>/</kbd> details pane, per row | **~79 ms**, and only while it is open |

No index, no SQLite — a parse cache that only re-reads what changed is enough,
and CI re-measures it on every push.

<details>
<summary>Where those 105 ms go</summary>

| | this step | cumulative |
|---|---|---|
| Python interpreter start | 8.8 ms | 8.8 ms |
| importing woswoar | 20.2 ms | 29 ms |
| building the argparse parser | 7 ms | 36 ms |
| loading the cache (deserialising 55k entries) | 31 ms | 67 ms |
| filter, sort, dedup, render | 20 ms | 87 ms |
| writing 1.5 MB to fzf | 18 ms | 105 ms |

Roughly half is fixed Python startup and half is proportional to history size.
Micro-optimising it was measured and did not help: the costs are structural, and
cutting them further would mean either a resident daemon (which the whole
no-server design exists to avoid) or an index the measurements do not justify.

The recording hook is a different story — 150 µs is imperceptible, and about
30 µs of it is the fork-free `history 1` capture that makes multi-line commands
survive intact. `$BASH_COMMAND` would be free but lossy: it reports
`for i in 1 2` for a loop and only the first element of `a && b`.

</details>
