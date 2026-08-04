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

The one file that is deliberately plaintext, `recipients.txt`, holds public
keys and nothing else. It used to carry `$USER@$(uname -n)` beside each one so
that `woswoar grant` had a name to show, and an SSH key's own trailing comment
went with it — which published exactly what the opaque directories were there
to withhold. The name comes from the sealed `name.age` now, and the comment is
stripped before the key is written; a test walks every committed byte and
fails if a machine name appears in any of them.

## 🧊 No self-rolled cryptography

Almost no crypto in this codebase, and none of the parts that are easy to get
wrong. Python's standard library has no cipher at all — only hashing — so instead
of reaching for a third-party library and composing primitives by hand, woswoar
shells out to
[age](https://github.com/FiloSottile/age): a small, audited, widely deployed
tool with one job. woswoar's crypto module is a
[small subprocess wrapper](../woswoar/crypto.py). There is no key derivation, no
nonce management and no mode selection to get wrong, because none of it lives
here. Signatures are the same story: `ssh-keygen -Y sign` and `-Y verify`, the
tool that is already on the machine because you push to git with it. woswoar
composes no primitive of its own — the manifest it signs is a list of filenames
and SHA-256 digests, and everything cryptographic about it happens inside
somebody else's audited binary.

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

So every machine signs what it publishes. Each one holds an Ed25519 key that
never leaves it, and per day it signs a **manifest** — the list of chunks it
published that day and their digests. A chunk is opened only if a manifest
signed by the machine whose directory it sits in vouches for exactly those
bytes. Signing and verification are `ssh-keygen -Y`, which you already have.

Once per day and machine, not once per chunk: an earlier attempt signed each
chunk and cost 3.3 ms every time, so a machine waiting to be granted access
re-checked the whole archive on every timer tick and never finished. A manifest
turns a year of three machines from nearly two minutes into about 3.6 seconds.

<details>
<summary>Why not one shared key, which is simpler?</summary>

Because it was, and it could not be made to work. Until #38 the repo held one
HMAC key sealed to all recipients, and any machine that had ever been enrolled
kept those 32 bytes forever — so a machine you had **revoked** could still
publish history every other machine believed.

That is not a bug in the arrangement, it is the arrangement: with any shared
secret, whoever can *check* a chunk can *forge* one. Rotating the key does not
help, because every existing chunk was tagged with the old one and a machine
cloning fresh would refuse the whole archive. Only asymmetric signatures
separate "can verify" from "can sign", which is why each machine now has a key
of its own and why nobody else ever needs the private half.

</details>

<details>
<summary>What this does and does not stop</summary>

Stopped: anyone with push access to the repo — a stolen token, a mis-scoped
deploy key, the git host itself — fabricating history or tampering with what a
machine already published. Also stopped, and this is what #38 added: a machine
you have revoked, which still holds its own signing key and every manifest it
ever signed, but no longer has a peer willing to accept them.

Not stopped: one of your *own* currently enrolled machines, publishing under its
own name. It is a machine you trust; that is what trusting it means.

</details>

## 🤝 Each machine decides for itself which machines it believes

The signing keys are published in the repo — and the repo is exactly what this
defends against, since anyone who can push can rewrite `hosts/<id>/signer.pub`
along with the history it vouches for. So what a machine *believes* is kept
outside the repo, in its own `state.json`, and:

> **Repo state may only ever remove trust, never add it.**

Adding needs a person at that machine (`woswoar trust`, which shows a
fingerprint and writes nothing to the repository). Removing is safe to do
automatically, because it can only ever cause a refusal and never an injection —
so a revocation published by any of your machines takes effect on all of them at
their next sync, with nobody having to run anything.

The cost is real and worth stating: **enrolling a machine now needs `woswoar
trust` on each machine that will read it**, on top of `woswoar grant` once. A
machine that clones later pins whatever it finds at that moment, so the ceremony
only runs in one direction.

`woswoar accept` runs both for a machine you own, which is what almost everyone
is doing. It does not merge the two decisions — it shows both keys, says what
each half does, and asks once. The one thing it deliberately will not do is
accept a *changed* signing key: that is either a machine you re-enrolled or
someone rewriting the repository, nothing can tell those apart, and rolling it
into the newcomer prompt is how it would get agreed to by someone answering a
different question. It stays behind `woswoar trust --replace`.

## 🔑 No secret is ever copied between machines

age takes **SSH public keys as recipients**, so each machine encrypts to the
other machines' public keys and keeps its own private key to itself, forever.
Enrolling a new laptop means publishing a public key — nothing sensitive travels,
and nothing sensitive is stored in the repo.

Widening who can read the archive is therefore a deliberate act: `woswoar grant`
lists the machines and asks before it re-seals anything. Narrowing it is
`woswoar revoke <fingerprint>`.

<details>
<summary>What revoking does, and the three things it cannot do</summary>

`woswoar revoke` appends a **tombstone** to `recipients.txt` rather than
deleting the line. The file is `merge=union` — the property that makes two
machines enrolling at once conflict-free — and union keeps both sides of every
difference, so a deleted line comes straight back from any peer that still has
it. Every machine subtracts the tombstoned key on its next fetch, and the
withdrawal is permanent: a key that reappears below its own tombstone stays out,
because whoever the revocation was aimed at has push access by assumption.

The remaining sealed keys are then re-sealed without it, so a copy of the repo
taken afterwards cannot be opened with that key at all, and every day key minted
from then on excludes it.

What it does **not** do, both said on screen before you confirm:

- **It does not un-publish anything.** History already in the repo stays
  readable by that key if it kept a copy.
- **It does not revoke git access.** If the key got in through a stolen token or
  a mis-scoped deploy key, that credential needs rotating too, or it can simply
  fetch again.

What it **does** do, and this is what #38 added: from that moment nothing that
machine publishes is accepted by any of your machines, under its own name or
anyone else's. It keeps its signing key — nobody can take that back — but every
peer drops it on the next sync, with nobody having to run anything. The cost is
that history it published *before* the revocation and your machines have not
merged yet is refused too, so sync them first if you want it.

There is also a bounded window on reads: a day key is minted once and every
chunk of that day is sealed to it, so commands recorded on a day whose key
already exists stay readable by the revoked machine. In practice that is the
rest of today, and `revoke` prints exactly which days. Rotating those mid-day
would strand the chunks already sealed to them on every machine that has not
merged them yet.

</details>

<details>
<summary>What that prompt shows, and why not just the names</summary>

`recipients.txt` is plain text and `merge=union`, so anyone who can push can
append a key labelled `martin@laptop` alongside your real one. A confirmation
that shows only names is therefore a confirmation of attacker-supplied text, and
approving it hands over the whole archive without breaking any encryption.

So the prompt leads with a **fingerprint**, which is derived from the key and
cannot be chosen: for an SSH key it is exactly what `ssh-keygen -lf` prints on
the machine itself, and an age recipient is already its own. Names are printed
inert and quoted — a label cannot carry an escape sequence that erases the line
above it — and two keys sharing one name are marked as such.

Only **additions** are put to you. Re-sealing to a set you already approved
widens nothing, so it does not ask; a prompt that fires when there is nothing to
decide is one people learn to answer without reading. What you last approved is
remembered locally, on purpose: a record kept in the repo could be edited by the
attacker the prompt exists to catch.

</details>

<details>
<summary>What if my SSH key has a passphrase?</summary>

age cannot use ssh-agent, so a passphrase-protected key would break unattended
syncing from a timer. `woswoar init` detects this by doing a real
encrypt/decrypt round trip — not by inspecting the key file — and falls back to
a dedicated age identity at `~/.config/woswoar/identity`. Force either choice
with `--identity <path>` or `--new-identity`.

</details>

## 🧭 `grant` and `trust` answer different questions

Two commands, both asking a human, and it is easy to assume one could replace
the other. They are on different axes and neither can absorb the other.

`woswoar accept` is both of these at once, for the ordinary case where the
machine in question is yours. The distinction below is still what it does.

| | `woswoar grant` | `woswoar trust` |
|---|---|---|
| The question | who may **read** what already exists? | whose new history do I **believe**? |
| Acts on | age recipients — the per-day keys are re-sealed | one Ed25519 verify key, pinned |
| Runs | **once**, on a machine that can already decrypt | on **each** machine that will read the new one |
| Touches the repo | yes: it rewrites and pushes the key files | no, ever |
| If you skip it | the new machine sees only history from its enrolment on | the new machine's history is refused, and said so |

Reading and writing are separate because the cryptography separates them.
Chunks are sealed to a day key, and the day key is sealed to the recipient list
— so **reading** is a question about age recipients. Nothing about age says who
*wrote* a chunk, so each machine signs a manifest — and **believing** is a
question about signing keys. A machine can be able to read everything and be
believed by nobody, or the reverse.

### Why each one has to ask a person

`recipients.txt` is plaintext and `merge=union`, and `hosts/<id>/signer.pub` is
an ordinary committed file. **Anyone who can push can add a line to either.**
That is the assumption the whole design is built on, and it is what makes both
confirmations load-bearing rather than ceremonial:

- If re-sealing happened **automatically** whenever the recipient list changed,
  then appending one key would be enough: the next sync on any enrolled machine
  would re-seal every day key to include the attacker, who could then decrypt the
  entire archive. No signature is forged and nothing looks wrong. The
  confirmation in `grant` *is* the defence.
- If a published `signer.pub` were **believed on sight**, anyone who can push
  could have history accepted under any machine's name. Hence the local pin, and
  hence the rule above: **repo state may only ever remove trust, never add
  it.**

### Why `grant` cannot quietly do `trust` as well

The tempting simplification is that granting a machine could pin it at the same
time — one confirmation instead of two. It cannot, for two reasons, and the
second is the sharp one.

The dull reason: `grant` runs once, anywhere; the pin is needed on *every* other
machine. Pinning at grant time would fix only the machine you happened to run it
on.

The sharp reason: **at grant time the human is checking a different key.** The
prompt shows an *age recipient* fingerprint. What links that recipient to a
signing key is `signer.pub`, a file in the repo. So someone with push access
could point their own host directory at the recipient you are about to approve —
they still could not decrypt anything, because they do not hold that private
key, but their *verify key* would be pinned as that host's signer and you would
accept history they published. Disclosure would be prevented and injection
introduced. `trust` avoids it by showing the verify key's own fingerprint, which
is the thing being decided.

`woswoar accept` shows both keys at once, which reopened the same question in a
new form: a fingerprint you *can* check sitting directly above one you cannot
reads as the first vouching for the second, and what puts them on the same line
is the `owner` field of `signer.pub` — the very file this is about.

Two things answer it. The pairing is **labelled as the repository's claim**
where it is shown, rather than left to look like a fact. And a recipient that
**more than one host directory claims** is paired with neither and reported:
the mapping used to keep the first claim in sorted order and drop the rest, so
a host id chosen to sort first could take another machine's recipient — and the
name attached to it — without anything saying so. Two hosts claiming one key is
not an ambiguity to resolve, because nothing in the repository could resolve it.

> Asserted by `tests/test_sync.py::TestAHostCannotClaimAnotherMachinesKey`,
> which mounts exactly that attack: a host directory whose id sorts first,
> naming another machine's recipient as its own.

### What that costs, and what it does not

Adding a laptop is `grant` once, and `trust` on the machines that will read it —
`trust` offers every unpinned machine at once, so it is one command per machine,
not one per newcomer. That cost is the price of an anchor the remote cannot
rewrite, and it is listed under *What is not protected* rather than
hidden.

What it is *not* is a choice between security and convenience in the usual
sense. Dropping `grant` entirely would be perfectly safe — a new machine would
simply never read history recorded before it enrolled. What is unsafe is keeping
the capability and removing the person.

## 🌐 Can I use a public repository?

You can, and the encryption holds — a public repository is still a pile of
unreadable blobs, and because "public" on a git host means *readable*, not
writable, push access is unchanged and so is everything the signatures
guarantee.

**Use a private one anyway.** Three things change, and two of them cannot be
undone afterwards.

### Your SSH public key names you

By default the recipient woswoar enrols is your existing **SSH public key** —
preferred because it means no new secret exists to lose. GitHub publishes every
user's SSH public keys at `github.com/<username>.keys`, and GitLab does the
same. So a public history repository can be matched to your account, and to
every other place that key is used, by anyone who cares to look.

The opaque host directories exist so that an archive does not publish which
machines you have. This would hand over who you are instead.

`woswoar init --new-identity` avoids it: a dedicated age key that exists nowhere
else, and is therefore not a handle to anything.

### Harvest now, decrypt later

Against a private repository an attacker needs access **and** a key. Public
removes the first requirement permanently and retroactively: anyone can take a
copy today and keep it. If a key is ever compromised, or age's primitives are
ever broken, everything published becomes readable back to the first commit.

And there is no taking it back. Forks, mirrors and archives mean "I will make it
private later" does not undo a day of it being public.

### The metadata becomes a continuous public record

Encryption covers file contents. It does not cover file *names*, and those carry:

```
hosts/6941894815e14751/2023-11-14/1700000500-f400be.age
                       ^ which days   ^ the sync time, to the second
```

Which days you worked, how many chunks each day, and the second at which every
sync ran — plus a commit timestamp beside it. Over months that is working hours,
timezone, sleep, and holidays, published continuously. *What is not protected*
below already lists metadata as something the design does not hide; a public
repository turns it from *what a leak would reveal* into *a feed*.

### And one that compounds

The credential filter is best-effort by design, and says so. In a private
repository a command that slips past it still has both encryption and access
control in front of it. Public removes one of those, and combines with
harvest-now-decrypt-later above.

### If you want a public one anyway

For a demonstration repository — showing what the format looks like, say — use a
throwaway with `--new-identity` and history you do not mind publishing. That is
a different thing from syncing the machines you actually work on.

## 📦 Minimal supply chain

No on-disk format woswoar reads can execute code. The parse cache under
`~/.cache` is read on every Ctrl-R and used to be a pickle, which runs whatever
the file says *before* any validation the caller might do; it is now plain text
that a `split` cannot execute. That file is `0600`, so this was never a
cross-user hole — what it removes is the step from "something could write one
file in your home directory" to "something runs as you".

The runtime dependency list is **empty**, and
[intended to stay that way](../pyproject.toml). No PyPI packages, no transitive
tree, no post-install scripts, no crate lockfile to audit — the only Python that
runs is the code in this repository. `ruff`, `mypy` and `shellcheck` are
development-only, and `fzf`, `git` and `age` are ordinary system binaries you
install from your distribution.

## ✅ Guarantees pinned by tests, not by prose

Claims rot. The ones that matter are asserted in CI on every push — and the
central one does not even ask you to trust CI: `woswoar doctor --prove` walks
a canary command through a sandboxed install on *your* machine and shows it
reaching the remote unreadable. [Verify it yourself](verify.md) has that and
every other check you can run without believing anyone.

**Nobody else can put history in yours**

| Claim | How it is enforced |
|---|---|
| Forged history is refused | a chunk sealed to a host's published day key, but absent from that host's signed manifest, is rejected and reported |
| Tampering is refused | flipping one byte of a real chunk fails authentication, not merely decryption |
| A manifest cannot be moved | a real, correctly signed manifest copied to another day is refused; host and day are inside the signed bytes |
| A machine never signs what it did not write | a chunk planted under a machine's own id stays out of the manifest it signs, and is reported |
| A changed signing key is never waved through | the peer refuses, keeps the old pin, and says so |
| A chunk cannot exhaust a peer | a chunk that unpacks past the cap is refused and reported, and the peak allocation is measured to stay bounded rather than the payload being materialised first |

**A revoked machine stays revoked**

| Claim | How it is enforced |
|---|---|
| A revoked machine cannot publish | it keeps its signing key and its old manifests, signs a chunk with them, pushes -- and a third machine accepts none of it |
| Nor under another machine's name | the same, written into a peer's host directory, where that peer never looks |
| A revoked machine stops receiving history | two real machines through a bare repo: after `revoke`, the revoked one still has what came before and never gets what comes after |
| A revocation cannot be undone by pushing | re-adding the key, by command or by appending the line, leaves it subtracted |

**A leaked repository says as little as possible**

| Claim | How it is enforced |
|---|---|
| The repo names no machine | every committed byte is searched for the username and hostname; a leaked archive says how many machines there are, not which |
| age is never given a file path | every age invocation is inspected; no argument may be an existing path outside `/dev/fd` |
| A remote is an address, never an option to git | `woswoar init -- --upload-pack=<command>` is refused, and both git calls that are handed the remote pass `--` first; the command never runs |

**Your own history is not quietly lost or doubled**

| Claim | How it is enforced |
|---|---|
| History is never rewritten | `git log --diff-filter=MD` over chunk files must be **empty** |
| A failed chunk is retried, not dropped | an earlier chunk that fails while a later one succeeds is merged once it is repaired |
| A compacted day survives gaining more history | a chunk written for a day after it was compacted leaves every peer holding that day's earlier commands as well, not only the new one |
| Compaction does not duplicate history | a peer that already merged a day, and one that merged only part of it, both end up holding each command exactly once |
| A day whose signed list is gone is refused, not re-signed | deleting a manifest and syncing publishes nothing further for that day, rather than signing a replacement that names only the newest chunk and disowns every earlier one |
| A day whose sealed key is gone is refused, not written over | deleting one and syncing publishes nothing further for that day and says so, rather than adding chunks no machine could ever read |
| This machine never writes a chunk its peers would refuse | a tail past the export budget is split into several chunks and a peer merges every line of it; compaction, the other producer, leaves a day alone rather than merging it past the same budget |

**Nothing woswoar reads can run**

| Claim | How it is enforced |
|---|---|
| The cache cannot execute | a pickle that would run code on load is written to the cache path; it is refused and a witness file never appears |
| A recalled command is one command | control characters never survive from the picker into the shell buffer |
| Nothing woswoar prints can drive your terminal | no C0 byte reaches the terminal raw from `list`, `search`, `stats` or `import` -- a peer's command is made inert as it leaves the cache, a peer's machine name where it is read |
| Shell and Python escaping agree | a command containing a literal tab and newline must round-trip byte-for-byte |

**It stays yours, and stays fast**

| Claim | How it is enforced |
|---|---|
| History is never readable by others | every path woswoar creates is walked under a stock `umask 022`, on both the Python and the shell-hook side |
| The hook's scratch file is never in a directory another user can write | `TMPDIR` and `/tmp` are never consulted; an `XDG_RUNTIME_DIR` this user cannot write falls back to woswoar's own `0700` tree instead of switching recording off |
| Recording forks nothing | the clone count under `strace` must be *identical* for 3 commands and for 30, so nothing on the record path scales with use. Syncing is the one exception and is deliberate: at most one fork per `WOSWOAR_SYNC_INTERVAL`, asserted separately |
| Search stays fast | latency measured on 52,000 entries |
| The repo does not blow up | a simulated multi-day, multi-machine run is measured after `git gc` |

## ⚠️ What is *not* protected

Being straight about the limits is part of the security story:

> [!IMPORTANT]
> - **Local history is plaintext.** `~/.local/share/woswoar/logs/` holds your
>   commands unencrypted. woswoar's own directories are created `0700` and its
>   files `0600` — the same as `~/.bash_history` — and `woswoar doctor` fails if
>   anything under them is readable by another user. The encrypted `history/`
>   checkout is excluded from that walk: it is ciphertext, and the directory
>   above it is owner-only. All of which only keeps out other accounts on the
>   same machine. Encryption protects the *synced copy*,
>   not your disk. Use full-disk encryption for that.
> - **Metadata leaks.** Anyone with the repo can see how many machines you have,
>   when they synced, and roughly how many commands you ran per day. Not *which*
>   machines: no username or hostname is committed.
> - **Trust on first use.** Cloning pins whatever machines the repository shows
>   at that moment. A machine planted *before* you clone is accepted; one that
>   appears afterwards is refused until you say otherwise. `woswoar init` prints
>   the fingerprints it pinned, and that printout is the only thing standing
>   between you and a repository that lied at exactly the right moment.
> - **A confirmation on every machine.** Enrolling a laptop needs `woswoar
>   trust` on each machine that will read it. That is the price of an anchor the
>   remote cannot rewrite.
> - **Secrets you type are still secrets.** `WOSWOAR_IGNORE` skips
>   credential-shaped commands by default — assignments like
>   `AWS_SECRET_ACCESS_KEY=`, options like `--password`, credentials inside a
>   URL, and `Authorization` headers — and bash's own `HISTCONTROL`/`HISTIGNORE`
>   are honoured for free. `woswoar import` applies the same rules to history
>   recorded long before woswoar existed, and additionally recognises well-known
>   token formats, which is where the risk is concentrated. But **no pattern
>   catches everything**. [What it does and does not catch](shell-integration.md#commands-that-are-never-recorded)
>   is written down; read it before relying on it.
>
>   For a sense of scale rather than a promise: measured against one
>   maintainer's real atuin history of **55,017 commands**, the rules skipped 32
>   and missed **one** shape -- a Slack webhook URL, where the address *is* the
>   credential. That rule now exists, and the shape is in the corpus. One
>   history is not a survey, and the number that matters for you is the one from
>   your own; `woswoar import --dry-run` prints what it would skip.
> - **Lose your key, lose your access.** There is no recovery service, because
>   there is no service. If a machine loses its identity, re-enrol it and run
>   `woswoar grant` from another machine. If it loses its *signing* key it mints
>   a new one and carries on recording, but every peer refuses its history until
>   a human there runs `woswoar trust --replace` — deliberately, because a
>   changed signing key and an impersonation look identical from the outside.
> - **Revoking cannot take back what was already published.** `woswoar revoke`
>   stops a machine both reading and publishing from that moment on, but a copy
>   it already made is a copy it keeps. It also refuses history that machine
>   published *before* the revocation which your other machines had not merged
>   yet, so sync them first if you want it. A key that has genuinely fallen into
>   someone else's hands is still a reason to rebuild the repo.
