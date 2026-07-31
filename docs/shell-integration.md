# Living in your shell

woswoar is never the only thing hooked into a real `.bashrc`, and it runs on
every prompt. Both of those constrain it more than anything else in the design.

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

`WOSWOAR_NO_BIND=1` skips the <kbd>Ctrl</kbd>+<kbd>R</kbd> binding entirely if
you would rather keep another tool's.

## Commands that are never recorded

Anything bash itself keeps out of history is invisible to woswoar, so
`HISTCONTROL=ignorespace` (a leading space) and `HISTIGNORE` work exactly as they
already do — a command bash declines to store is never seen here.

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
survive intact. `$BASH_COMMAND` would be free but lossy: it reports
`for i in 1 2` for a loop and only the first element of `a && b`.

</details>
