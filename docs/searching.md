# Searching your history

<kbd>Ctrl</kbd>+<kbd>R</kbd> opens fzf over every command from every machine you
own, deduplicated and newest first:

```
  woswoar (global) > docker
       2m  thinkpad  docker compose up -d --build
    3h12m  DT-24YYQ3 docker logs -f api
     6d4h  thinkpad  docker system prune -af
  ctrl-r global → host → session → dir, or ctrl-g/h/s, ctrl-o dir | ctrl-t timeline | ^name one machine
```

This page is what the footer line means. The keys themselves, what they cost and
how the picker coexists with the rest of your shell are in
[living in your shell](shell-integration.md#ctrl-r).

## Narrowing to one machine

The machine column appears once you have more than one, and fzf matches on it —
so typing `thinkpad` narrows to that machine. It shows the *host* part of each
machine's name, because that is usually what differs between your own machines.
The age of a command that exited non-zero is red, and <kbd>Ctrl</kbd>+<kbd>R</kbd>
again cycles global → host → session → dir — where **dir** is this directory and
everything below it, on every machine, because `~/src/woswoar` on your laptop and
on your desktop is the same project.

The prompt says which scope you are in, and for three of the four it also says
*which one*: the directory for `dir`, the machine for `host`, and for `session`
how long this shell has been open — `woswoar (session 3h42m)`. That last one is
the question the scope raises and nothing else on screen answers; the shell's
id is not shown, because nobody recognises it. The age is a snapshot taken when
the picker opened, so it does not tick while you are looking at it.

**A short machine name is also an ordinary word.** If yours is `box`, typing it
finds `sandbox` and `~/dropbox` too — so anchor it: **`^box`** matches only the
machine, because the search starts at the machine column and `^` sticks to the
front of it. It composes with everything else, so `^box docker` is "docker, on
box" and `^box !docker` is "on box, but not docker".

## Find one command, then read around it

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

## See the rest of what was recorded

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
[the numbers](shell-integration.md#the-details-pane).
