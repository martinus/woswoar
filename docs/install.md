# Installing, upgrading, uninstalling

```bash
pipx install woswoar
woswoar
```

Open a new shell, press <kbd>Ctrl</kbd>+<kbd>R</kbd>. That is the whole thing on
one machine; [adding a second](sync.md) is two more lines. This page is the rest
of it — what the first run does, how to upgrade, and how to remove every part of
it again.

**Needs:** bash 5.0+ (Linux) · Python 3.10+ ·
[fzf](https://github.com/junegunn/fzf) ·
[age](https://github.com/FiloSottile/age) and git *(sync only)*.
`woswoar install` checks for these and prints the install command for your
distribution. `woswoar doctor` diagnoses anything else that looks wrong.

**bash and zsh both work.** `install` writes to every shell whose rc file
already exists, and never creates one — see
[Living in your shell](shell-integration.md#zsh).

> [!WARNING]
> **Do not install `age` as a snap.** It works, and it starts a sandbox on every
> call — about 250 ms against 2 ms for a distribution binary. woswoar runs `age`
> roughly twice per day of recorded history, so on two years of history that is
> the difference between a `sync` taking three seconds and taking six minutes.
> `woswoar doctor` measures it and says so.

## The first run

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

## Upgrading

> [!TIP]
> **`pipx upgrade woswoar` for the next release.** The shell hooks are copies
> rather than the packaged files, but they bring themselves up to date on the
> next background sync, so there is nothing else to run; open a new shell to
> pick it up.
>
> **Started using a second shell since you installed?** Run `woswoar install`
> once. The background refresh updates the hooks that are there and deliberately
> creates none, so a shell woswoar has never been installed for stays that way
> until you say otherwise.
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

## Uninstalling

There is no `woswoar uninstall`, because every step is one you should see. In
order, and each is independent:

```sh
# 1. Stop the timer, if you installed one. Syncing from the shell hook stops
#    with the hook itself, in step 2.
systemctl --user disable --now woswoar-sync.timer
rm -f ~/.config/systemd/user/woswoar-sync.{service,timer}

# 2. Remove the hook from your shell(s). `woswoar install` wrote a marked block
#    into each rc file it touched; delete the three lines between the markers, or:
for rc in ~/.bashrc ~/.zshrc; do
    [ -f "$rc" ] && sed -i '/# >>> woswoar >>>/,/# <<< woswoar <<</d' "$rc"
done

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
