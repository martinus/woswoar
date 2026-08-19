# Adding another machine

Sync goes through **an ordinary git repository you already own** — no server, no
account, no daemon. Create an empty one (`woswoar-history` on GitHub, a bare repo
on a NAS, a folder on a USB stick). You do that once, ever.

Then on **every** machine, first or fifth, the same two lines:

```bash
pipx install woswoar
woswoar                          # paste the repository URL when it asks
```

`setup` joins the repo and does the first sync itself, so the new machine starts
publishing straight away — it signs its own commands and waits for nobody.

> [!TIP]
> **Importing atuin on more than one machine?** `setup` asks whether to import
> only *this* machine's history, and on a fleet the answer is yes. atuin keeps
> every machine it has synced with in one database, and woswoar publishes only a
> machine's own commands — so importing all of them everywhere stores each
> machine's history once per machine. Let each machine import its own.

<details>
<summary>The same thing without the questions</summary>

```bash
woswoar install
woswoar import atuin --this-host-only     # optional
woswoar init git@github.com:you/woswoar-history.git
```

`setup` calls exactly these. It needs a terminal, so this is also what to use
from a script or a dotfiles bootstrap.
</details>

From the **second** machine onwards, one more line, on each machine you already
use:

```bash
woswoar accept
```

That is the whole of it. `accept` lists what is new, says what accepting it
does, and asks:

```console
$ woswoar accept
1 machine(s) not yet accepted here:

  'martin@work-laptop'
      reads with                  age1qjg…
      signs with                  SHA256:2xQ5…

Accepting does two separate things:

  read     1 machine(s) get to read your ENTIRE history, including
           days recorded before they existed. This is published, so it
           applies everywhere — and it cannot be taken back for what
           they have already read.

  believe  this machine will accept what 1 machine(s) publish.
           Local only: every other machine of yours has to be told
           separately, because the repository is the thing that decision
           defends against.

A name is free text written by whoever added the key. The fingerprints are
not — run these on the machine they belong to and compare:

    reads with   age-keygen -y ~/.config/woswoar/identity
    signs with   ssh-keygen -lf ~/.config/woswoar/signing_key.pub

Accept 1 machine(s)? [y/N]
```

Two keys, because there really are two questions, and they stay separate
commands — [`grant`](security.md) for who may *read*, `trust` for whose
word this machine *believes*. `accept` is both at once for the ordinary case
where the machine is yours. The second one is why it has to be run on each
machine you already own rather than once: the repository is somewhere anyone
with push access can write, so what a machine believes cannot be decided by
anything kept inside it. Revoking removes that decision everywhere
automatically, since taking trust away can only ever cause a refusal.

`woswoar fleet` says how far through that walk you are — rows are the machine
doing the accepting, columns the machine accepted:

```
$ woswoar fleet
who accepts whom, as each machine last published it

              mar  lap  pi
martin@desk    .   yes  no
martin@lapt   yes   .   yes
pi@shed       yes  yes   .    (unverified)
```

Only your own row is checked here; the others are what those machines published
about themselves, and `?` or `(unverified)` marks one whose signing key this
machine has not accepted. A cell is what a machine *says*, never what is true —
if it were the latter, the repository would be making the trust decision that
the paragraph above refuses to let it make.

> [!TIP]
> `.bashrc` is written with `$HOME` rather than your username, so one shared
> dotfiles `.bashrc` works on every machine.

## Sync automatically

Nothing to install — the shell hook does it. At most once a minute, and only on
a machine somebody is actually typing on, it starts a `woswoar sync` in the
background. Your prompt never waits for it: the shell hands the work to a
detached process and returns immediately, so a slow `git push` cannot hold up a
shell, and neither can a laptop that woke up on the wrong network.

```bash
# Sync at most every 5 minutes instead of every minute.
export WOSWOAR_SYNC_INTERVAL=300
```

An idle machine costs nothing, which is the point: a timer firing every minute
on four machines is 5,760 fetches a day whether or not anyone typed anything.
Recorded history reaches your other machines within a minute of you typing it,
and about 6 MB of repository per machine per year — real typing is bursty, so a
minute rather than five roughly doubles the syncs that carry anything, not
quintuples them.

<details>
<summary>Keeping a machine current while nobody is using it</summary>

The trade is that a machine nobody types on never syncs, so it never *receives*
either — a laptop left shut for a week is a week stale until the first command
is typed. Opening a shell syncs, so in practice you are current by the time you
have a prompt. If you would rather have a machine stay current while idle, a
systemd timer does it. Paste the whole block:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/woswoar-sync.service <<'UNIT'
[Unit]
Description=woswoar shell history sync
Documentation=https://github.com/martinus/woswoar
# Pointless and noisy without a network; the timer will try again.
After=network-online.target

[Service]
Type=oneshot
# /usr/bin/env so this works wherever woswoar was installed (pipx, --user, venv)
# without hardcoding a path into the unit.
ExecStart=/usr/bin/env woswoar sync

# Sync holds a lock and talks to a remote; if it hangs, fail rather than pile up.
TimeoutStartSec=10min

# It only ever needs its own data directory, an ssh key, and the network.
PrivateTmp=true
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
UNIT

cat > ~/.config/systemd/user/woswoar-sync.timer <<'UNIT'
[Unit]
Description=Sync woswoar shell history periodically

[Timer]
# Wait a little after login rather than competing with everything else starting.
OnStartupSec=2min
# A minute, because it turns out to be nearly free -- real typing is bursty, so
# five minutes does not carry five times less. Raise it for the bytes back.
OnUnitActiveSec=1min

# Catch up after the machine was asleep or off, rather than silently skipping.
Persistent=true

# Every machine syncing on the same wall-clock tick is how you manufacture
# push races. A minute of jitter costs nothing and avoids them.
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT

systemctl --user enable --now woswoar-sync.timer
export WOSWOAR_SYNC_INTERVAL=0   # and turn the hook's off, or you pay twice
```

Written out in full rather than copied out of the repository, because a `pipx`
install leaves no checkout on the machine to copy from. Put the `export` in your
`.bashrc` as well — pasted into a shell it lasts only as long as that shell, and
the hook reads it on every prompt.

The two are safe to run together — syncs take a non-blocking lock, so whichever
arrives second exits immediately — but there is no reason to.

</details>

If a background sync starts failing, nothing is on screen to say so. Typing
`woswoar` on its own reports it, because a detached sync's error message would
otherwise go nowhere at all.
