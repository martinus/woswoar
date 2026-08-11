# Security policy

woswoar encrypts, signs and publishes your shell history to a git remote you
choose. If you have found a way to break one of those, please tell me privately
first.

## Reporting a vulnerability

**[Open a private advisory](https://github.com/martinus/woswoar/security/advisories/new)**
— GitHub's private vulnerability reporting is enabled on this repository, so
that form is a channel only the maintainer can read. Do not open a public issue
for something exploitable; a public issue is a disclosure.

If the form is not available to you for any reason, email
<martin.ankerl@gmail.com> instead.

Useful in a report, in rough order of usefulness:

- what an attacker gets, and what they need in order to get it — push access to
  the repository, an account on the machine, a network position;
- the output of `woswoar doctor`, and `woswoar --version`;
- anything that reproduces it, however rough.

**Expected response: best effort, usually within a week.** This is one person's
project. That is a real number rather than a comfortable one — if a report is
urgent enough that a week is too slow, say so in the first line and I will treat
it that way.

Nothing here is paid. There is no bounty programme, and I would rather say so
than have you find out afterwards.

## What is in scope

Anything that breaks a claim in **[the security model](docs/security.md)** — most
usefully the ones in
[Guarantees pinned by tests](docs/security.md#-guarantees-pinned-by-tests-not-by-prose), which
is the list of things this project asserts and backs with a test.

The clearest shapes of a real finding:

- **plaintext or identifying metadata reaching the remote** — a command, a
  directory, a username, a hostname, in any git object;
- **history from a machine you never trusted appearing in your Ctrl-R**, or a
  signature check that can be made to pass without the signing key;
- **a revoked machine still able to read or publish**;
- **a secret woswoar was supposed to skip being recorded anyway**, where the
  shape is one `WOSWOAR_IGNORE` claims to cover;
- **anything in the picker that lets another machine's recorded text drive your
  terminal** — the display line and the details pane both render peer-supplied
  strings, and `make_inert` is what stands between them and your ANSI parser.

## What is not in scope

`docs/security.md` has a section titled
[**What is *not* protected**](docs/security.md#%EF%B8%8F-what-is-not-protected),
and it is the authoritative list — deliberately one copy, so it cannot drift from
this file. Read it before reporting; everything in it is documented behaviour,
argued for, and not a finding. In particular, the one people notice first:

> **Local history is plaintext.** `~/.local/share/woswoar/logs/` holds your
> commands unencrypted, `0700`/`0600`, the same as `~/.bash_history`. Encryption
> protects the *synced* copy, not your disk. Use full-disk encryption for that.

Also out of scope: findings in `age`, `git`, `ssh-keygen` or `fzf` themselves.
woswoar composes those four and implements no cryptography of its own, so those
belong upstream — though if woswoar *uses* one of them wrongly, that is very much
in scope and is exactly the kind of report worth having.

## Supported versions

The most recent release, which is what `pipx install woswoar` gives you and what
`stable` points at. There are no maintained branches behind it: a fix ships as a
new release.
