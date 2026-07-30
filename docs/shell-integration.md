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
