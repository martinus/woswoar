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

Almost no crypto in this codebase, and none of the parts that are easy to get
wrong. Python's standard library has no cipher at all — only hashing — so instead
of reaching for a third-party library and composing primitives by hand, woswoar
shells out to
[age](https://github.com/FiloSottile/age): a small, audited, widely deployed
tool with one job. woswoar's crypto module is a
[small subprocess wrapper](../woswoar/crypto.py). There is no key derivation, no
nonce management and no mode selection to get wrong, because none of it lives
here. The one primitive woswoar does call directly is `hmac` from the standard
library, for the chunk tags above — the one with no nonce, no mode, no padding
and a constant-time comparison provided for you.

woswoar also never hands age a *path* — it reads key files itself and pipes the
bytes. That sounds like a detail until you meet a sandboxed age that can run
perfectly and still not open `~/.ssh`; see
[the design summary](woswoar_design_summary.md#age-never-gets-a-path) for the
incident that established the rule.

## ✅ Only your own machines can put history in your Ctrl-R

Encryption answers *"who may read this?"*. It does not answer *"who wrote
this?"* — age has no notion of a sender, `recipients.txt` publishes every
machine's public key, and each day's *public* key sits in the clear so that
writing a chunk never has to open the sealed one. On its own that means
**anyone who can push to the repo could seal a chunk every machine would open
and offer you in Ctrl-R**, one keypress from running.

So the repo also holds a random authentication key, sealed to the recipients
exactly like a day key, and every chunk carries an HMAC-SHA256 tag over its
ciphertext. Chunks are authenticated *before* they are decrypted, so a chunk
your machines did not write is never opened at all.

Holding that key is the same thing as being one of your enrolled machines, so
this needs no new command and no new key to manage: `woswoar grant`, which
already re-seals the per-day keys to a newly enrolled machine, re-seals this one
too.

<details>
<summary>What this does and does not stop</summary>

Stopped: anyone with push access to the repo — a stolen token, a mis-scoped
deploy key, the git host itself — fabricating history or tampering with what a
machine already published.

Not stopped: one of your *own* enrolled machines. The key is shared across them,
so a compromised machine could publish history attributed to another of your
machines. It could already publish anything under its own name and read
everything, so the marginal loss is attribution between machines you own — the
deliberate trade for having no per-machine keys to accept, compare or revoke.

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
| Forged history is refused | a chunk sealed to a host's published day key, but untagged, is rejected and reported |
| Tampering is refused | flipping one byte of a real chunk fails authentication, not merely decryption |
| The repo key never leaks | the key's bytes appear nowhere in the committed tree |
| A recalled command is one command | control characters never survive from the picker into the shell buffer |
| The repo does not blow up | a simulated multi-day, multi-machine run is measured after `git gc` |
| History is never world-readable | every file and directory woswoar writes is checked under a stock `umask 022` |
| Search stays fast | latency measured on 52,000 entries |

## ⚠️ What is *not* protected

Being straight about the limits is part of the security story:

> [!IMPORTANT]
> - **Local history is plaintext.** `~/.local/share/woswoar/logs/` holds your
>   commands unencrypted. woswoar's own directories are created `0700` and its
>   files `0600` — the same as `~/.bash_history`, and `woswoar doctor` fails if
>   anything under them is readable by another user — but that only keeps out
>   other accounts on the same machine. Encryption protects the *synced copy*,
>   not your disk. Use full-disk encryption for that.
> - **Metadata leaks.** Anyone with the repo can see how many machines you have,
>   when they synced, and roughly how many commands you ran per day.
> - **One key across your machines.** Authentication proves a chunk came from
>   your fleet, not which machine in it. See the note above.
> - **Secrets you type are still secrets.** `WOSWOAR_IGNORE` skips
>   credential-shaped commands by default (`*TOKEN=`, `*SECRET=`, `--password`,
>   …), and bash's own `HISTCONTROL`/`HISTIGNORE` are honoured for free — but no
>   pattern catches everything.
> - **Lose your key, lose your access.** There is no recovery service, because
>   there is no service. If a machine loses its identity, re-enrol it and run
>   `woswoar grant` from another machine.
