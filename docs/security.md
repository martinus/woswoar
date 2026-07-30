# Security model

The whole point of syncing history is that it ends up somewhere off your
machine. So the question is not *"do I trust GitHub?"* but *"what happens when
that repository leaks?"* — through a stolen token, a mis-clicked visibility
toggle, or a backup nobody thought about.

**With woswoar, a leaked repository is a pile of unreadable blobs.**

## 🔒 Nothing readable ever leaves your machine

Commands, arguments, working directories, usernames, hostnames — all of it is
encrypted before it is committed. Even the *directory names* in the repo are
opaque random hex, because paths are not encrypted by anything and
`hosts/martin@desktop/` would publish your machine names for free.

## 🧊 No self-rolled cryptography

Not one line of crypto in this codebase. Python's standard library has no cipher
at all — only hashing — so instead of reaching for a third-party library and
composing primitives by hand, woswoar shells out to
[age](https://github.com/FiloSottile/age): a small, audited, widely deployed
tool with one job. woswoar's crypto module is a
[small subprocess wrapper](../woswoar/crypto.py). There is no key derivation, no
nonce management and no mode selection to get wrong, because none of it lives
here.

woswoar also never hands age a *path* — it reads key files itself and pipes the
bytes. That sounds like a detail until you meet a sandboxed age that can run
perfectly and still not open `~/.ssh`; see
[the design summary](woswoar_design_summary.md#age-never-gets-a-path) for the
incident that established the rule.

## ✍️ Every line is signed by the machine that wrote it

Encryption answers *"who may read this?"*. It does not answer *"who wrote
this?"* — age has no notion of a sender, and the recipient list is published in
the repo, so on its own **anyone who can push could seal a chunk every machine
would happily open and show you in Ctrl-R**. A history entry is one keypress
from being executed, so that gap mattered.

So each machine also has an ed25519 **signing key**, and every chunk carries a
detached `ssh-keygen -Y sign` signature over its ciphertext. Verification runs
*before* anything is decrypted, so a chunk a machine did not write is never
opened at all.

The key each host is held to is pinned locally the first time you accept it —
in `state.json`, which is the one thing the remote cannot rewrite. Adding a
machine therefore takes one confirmation on the machines that will read it:

```bash
woswoar trust      # shows each new machine's name and key fingerprint, and asks
```

If a host's published signing key ever stops matching the pinned one, sync
refuses that host's history and says so rather than guessing which of the two
is real.

<details>
<summary>What this does and does not stop</summary>

Stopped: anyone with push access to the repo — a stolen token, a mis-scoped
deploy key, the git host itself — fabricating history, or tampering with a
machine's existing history.

Not stopped: a machine you have already trusted, whose private key has been
stolen, publishing whatever it likes under its own name. Signing establishes
*which machine* wrote something, not that the machine is behaving.

</details>

## 🔑 No secret is ever copied between machines

age takes **SSH public keys as recipients**, so each machine encrypts to the
other machines' public keys and keeps its own private key to itself, forever.
Enrolling a new laptop means publishing a public key — nothing sensitive travels,
and nothing sensitive is stored in the repo.

Widening who can read the archive is therefore a deliberate act: `woswoar grant`
lists the machines by name and asks before it re-seals anything.

<details>
<summary>What if my SSH key has a passphrase?</summary>

age cannot use ssh-agent, so a passphrase-protected key would break unattended
syncing from a timer. `woswoar init` detects this by doing a real
encrypt/decrypt round trip — not by inspecting the key file — and falls back to
a dedicated age identity at `~/.config/woswoar/identity`. Force either choice
with `--identity <path>` or `--new-identity`.

</details>

## 📦 Minimal supply chain

The runtime dependency list is **empty**, and
[intended to stay that way](../pyproject.toml). No PyPI packages, no transitive
tree, no post-install scripts, no crate lockfile to audit — the only Python that
runs is the code in this repository. `ruff`, `mypy` and `shellcheck` are
development-only, and `fzf`, `git` and `age` are ordinary system binaries you
install from your distribution.

## ✅ Guarantees pinned by tests, not by prose

Claims rot. The ones that matter are asserted in CI on every push:

| Claim | How it is enforced |
|---|---|
| The hook forks nothing | recording runs under `strace`; the clone/fork/execve count must be **0** |
| Shell and Python escaping agree | a command containing a literal tab and newline must round-trip byte-for-byte |
| History is never rewritten | `git log --diff-filter=MD` over chunk files must be **empty** |
| age is never given a file path | every age invocation is inspected; no argument may be an existing path outside `/dev/fd` |
| Forged history is refused | a chunk sealed to a host's published day key, but unsigned, is rejected and reported |
| An untrusted machine is ignored | a fabricated host directory is skipped before its chunks are opened |
| A swapped signing key is refused | republishing `signer.pub` does not re-authorise anything |
| A recalled command is one command | control characters never survive from the picker into the shell buffer |
| The repo does not blow up | a simulated multi-day, multi-machine run is measured after `git gc` |
| Search stays fast | latency measured on 52,000 entries |

## ⚠️ What is *not* protected

Being straight about the limits is part of the security story:

> [!IMPORTANT]
> - **Local history is plaintext.** `~/.local/share/woswoar/logs/` is readable by
>   anyone who can read your home directory — same as `~/.bash_history`.
>   Encryption protects the *synced copy*, not your disk. Use full-disk
>   encryption for that.
> - **Metadata leaks.** Anyone with the repo can see how many machines you have,
>   when they synced, and roughly how many commands you ran per day.
> - **Trust is first-use.** The first time you accept a machine, you accept
>   whatever key the repo shows you. Compare the fingerprint `woswoar trust`
>   prints against the one on the machine itself if the repo is somewhere you
>   do not fully control.
> - **Secrets you type are still secrets.** `WOSWOAR_IGNORE` skips
>   credential-shaped commands by default (`*TOKEN=`, `*SECRET=`, `--password`,
>   …), and bash's own `HISTCONTROL`/`HISTIGNORE` are honoured for free — but no
>   pattern catches everything.
> - **Lose your key, lose your access.** There is no recovery service, because
>   there is no service. If a machine loses its identity, re-enrol it and run
>   `woswoar grant` from another machine.
