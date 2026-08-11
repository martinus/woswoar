# Verify it yourself

[The security model](security.md) states what woswoar guarantees, and CI pins
each claim with a test. This page is for the moment before you believe any of
that: every section is a check **you run, on your machine, against the copy of
woswoar you actually installed**. None of them asks you to trust a document,
a badge, or the person who wrote this sentence.

First, be clear about what there is to verify. The entire thing you are
trusting is:

- **this repository** — about 4,300 lines of implementation (twice that
  counting its deliberately dense comments), readable in an afternoon, with
  [zero runtime dependencies](../pyproject.toml): no PyPI packages, no
  transitive tree, no post-install scripts;
- **four system binaries** you install from your distribution and that
  woswoar merely runs: `age`, `git`, `ssh-keygen`, `fzf`. All of the
  cryptography happens inside the first three; woswoar composes no primitive
  of its own.

That is a small enough surface that "verify" can mean something. The checks
below are ordered by effort, starting at one command.

## 🐤 The one-command proof

```console
$ woswoar doctor --prove
[--] sandbox      /tmp/woswoar-prove-... - a throwaway install; your real history is not touched
[ok] recorded     the canary 'woswoar-canary-52bc...' sits in the sandbox's plaintext log, exactly as logs/ holds your own
[ok] published    1 line(s) sealed into 1 chunk(s) and pushed to the sandbox's remote
[ok] sealed       the pushed chunk decrypts back to the canary -- with the sandbox's private key, which never left this machine
[ok] unreadable   the canary appears in none of the 35,308 bytes on the remote
[ok] anonymous    neither is any of: 'you@yourhost', 'you', 'yourhost'
```

This builds a complete throwaway installation in a temporary directory — its
own identity, signing key, and a bare git "remote" that is just another
directory — records one canary command, syncs, and then walks the round trip:

- **recorded**: the canary is in the sandbox's plaintext log, because
  [local logs are plaintext](security.md#%EF%B8%8F-what-is-not-protected) and
  pretending otherwise would be the opposite of this page;
- **sealed**: the chunk that reached the remote decrypts back to the canary,
  so the proof is about your history, not about an empty repository;
- **unreadable** and **anonymous**: neither the canary nor this machine's
  username or hostname appears in *any* byte on the remote — including every
  git object, decompressed, which is where committed bytes actually live.

It runs the installed code, needs no network, and touches nothing of your real
installation. A `FAIL` is a defect in woswoar itself — please
[report it](https://github.com/martinus/woswoar/issues). Re-run it after every
upgrade; it is the upgrade you are re-deciding to trust.

## 🕵️ The same thing by hand, on your real history

The sandbox proves the code. Your own repository is the thing you care about,
and the same canary walk works there — type a marker, sync, and search every
byte your repo holds:

```console
$ echo woswoar-canary-look-for-me
$ woswoar sync
$ grep -r woswoar-canary ~/.local/share/woswoar/logs
.../2026-08-04.tsv:1785843371	s1	~	0	1	echo woswoar-canary-look-for-me
$ git -C ~/.local/share/woswoar/history cat-file --batch-all-objects --batch | grep -a woswoar-canary
$
```

The first search finds the plaintext log, which is local, `0600`, and
documented. The second inflates **every git object in the repository** — every
byte a `git push` has ever sent or will send — and finds nothing.

`cat-file --batch-all-objects` rather than `grep -r`, because git stores
objects zlib-compressed: a raw byte scan of the repository would come back
clean *even if your history were committed in plaintext*. Inflating the
objects first is the difference between a scan and a ritual.

## 🔓 Read your history without woswoar

The strongest evidence that the encryption is real, standard age — and that
your data is not held hostage by this tool — is decrypting a chunk with no
woswoar in the pipeline:

```sh
H=~/.local/share/woswoar/history
ID=$(sed -n 's/^id=//p' ~/.config/woswoar/machine)          # this machine's opaque host id
KEY=$(sed -n 's/^identity=//p' ~/.config/woswoar/machine)   # the identity it decrypts with
DAY=$(date +%F)

for chunk in "$H/hosts/$ID/$DAY/"*.age; do
  age -d -i <(age -d -i "$KEY" "$H/hosts/$ID/keys/$DAY.age") "$chunk" |
    python3 -c 'import sys,zlib; sys.stdout.buffer.write(zlib.decompress(sys.stdin.buffer.read()))'
done
```

That prints today's commands as tab-separated plaintext, and everything in the
pipeline is somebody else's audited tool. The two `age -d` calls are the two
layers of [the design](security.md): each day has its own key, sealed to your
machines' keys — the inner call opens the day key, the outer opens the chunk
with it. The `zlib` line exists because chunks are compressed *before*
sealing; ciphertext does not compress, so it is then or never.

Nothing above needed woswoar installed at all. If this project is abandoned
tomorrow, that loop — or the plain TSV files in `logs/` — is your history.

## 🔌 It cannot phone home if there is no phone

woswoar has no server and no account; the only thing that ever touches a
network is `git` talking to the remote *you* configured. A remote can be a
directory, so the whole system runs with no network at all — which you can
make a hard fact rather than a claim:

```console
$ unshare -rn woswoar sync        # a network namespace with no interfaces
in sync with the remote
```

Or watch the syscalls: against a directory remote, a full sync makes **zero**
`connect()` calls —

```console
$ strace -f -e trace=connect -o /tmp/trace woswoar sync && grep -c connect /tmp/trace
0
```

With a remote on a git host, every connection you see in that trace is git
reaching the address you gave `woswoar init`, and nothing else.

## 🧪 Run the guarantee suite where you can watch

The tables in [security.md](security.md#-guarantees-pinned-by-tests-not-by-prose) —
forged history is refused, tampering is refused, a revoked machine stays
revoked — are each backed by a test, and the suite runs anywhere:

```sh
git clone https://github.com/martinus/woswoar && cd woswoar
python -m tools.run_tests
```

The tests drive real `age`, real `git` and a real `bash` rather than mocks —
two simulated machines exchanging history through a bare repository, attacks
mounted and refused. A claim you can watch fail when you break the code is a
different thing from a claim in prose.

## 📦 Know what you installed

woswoar is pure Python installed from a git checkout — no build step, no
binary artifact between the code you can read and the code that runs. So the
two can be compared directly:

```sh
SRC=$(pipx runpip woswoar show woswoar | sed -n 's/^Location: //p')
VERSION=$(pipx runpip woswoar show woswoar | sed -n 's/^Version: //p')
git clone -q --branch "v$VERSION" https://github.com/martinus/woswoar /tmp/woswoar-src
diff -r -x __pycache__ "$SRC/woswoar" /tmp/woswoar-src/woswoar && echo identical
```

Release artefacts (the `.tar.gz` and `.whl` on each GitHub release, from
v0.8.1 on) additionally carry a **build provenance attestation**: a
Sigstore-backed statement, signed by the release workflow's own identity,
that these exact bytes were built by this repository's public pipeline at
this commit — the pipeline that refuses a tag not on the protected branch and
re-runs the whole suite first. There is no key anyone holds, so there is no
key anyone can lose. Verify a download with:

```console
$ gh attestation verify woswoar.tar.gz --repo martinus/woswoar
✓ Verification succeeded
```

What that does not prove is intent: anyone with push access to the
repository could cut a release through the same pipeline, and it would attest
cleanly. It rules out the quieter failures — an asset replaced after
publication, or a tarball built on somebody's compromised laptop rather than
in the audited workflow.

The install line in the README is `pipx install woswoar`, which takes the
latest release from PyPI — uploaded by that same workflow over Trusted
Publishing, so no long-lived token exists that could publish a release
nobody tagged. Convenient, and it still means whoever controls the GitHub
repository controls what your next upgrade installs. If that is a trade you
do not want, pin the exact commit you audited; a full commit hash is a handle
nobody can quietly move, where a version number is one somebody else chooses:

```sh
pipx install --force "git+https://github.com/martinus/woswoar.git@<full 40-character sha>"
```

## ⚠️ What none of this can show

Honesty about limits is part of the story, here as in
[the security model](security.md#%EF%B8%8F-what-is-not-protected). These
checks demonstrate what this install did on the paths you exercised. They
cannot prove the absence of a channel that hides deliberately — behavior that
triggers on a date, a hostname, the thousandth sync. No black-box test can;
that assurance only comes from reading the code, which is why the size of the
trust surface is the load-bearing fact on this page. What the checks do is
shrink the leap of faith to: *these few thousand dependency-free lines, whose
observable behavior I just tested, do not contain a bomb.* For a local tool,
that is as good as verifiable trust gets.
