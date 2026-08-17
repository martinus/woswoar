# Architecture

How the code is arranged, and the handful of ideas it keeps reusing.

This is the contributor's companion to the [design summary](woswoar_design_summary.md).
That document is about the *product*: the record format, the sync protocol, the
encryption, and the measurements behind each. This one is about the *package* —
which module may know about which, what the recurring shapes are called, and
where the arrangement is currently under strain. Read that one to understand
what woswoar does; read this one before moving code.

---

## Layers

Who sits on whom. Indentation is "imports"; the three at the bottom are what the
domains are made of.

```
entry, errors        the record format, and the exception every layer raises
  store              logs/, machine identity, the filesystem primitives
    archive          history/ -- the repo layout    (sync and prove only)
    cache            the parse index
      search         Ctrl-R
  crypto             age and ssh-keygen
  credentials
    importer         bash, zsh, atuin
  install            which shells get a hook, and keeping it current
    setup            what the guided first run asks
deps, progress,      leaves: asked things by anyone, knowing nobody
report

sync      <- archive, crypto, progress, report, store, entry, errors
doctor    <- report, search, cache, crypto, deps, store, entry, errors
prove     <- sync, and everything sync is made of, plus deps and report
__main__  <- search, cache, importer, install, setup, report, store,
             entry, errors
             (sync, crypto, archive, doctor and prove only inside the
              commands that need them -- see "Two costs shape everything")
```

| layer | modules | what it may know |
|---|---|---|
| format | `entry`, `errors` | nothing else in the package |
| platform | `store`, `crypto`, `deps`, `progress`, `report`, `credentials` | the format layer |
| derived | `cache`, `archive` | the two above |
| domains | `search`, `sync`, `importer`, `install` | everything below, and **never each other** |
| composition | `setup`, `doctor`, `prove`, `__main__` | everything |

`store` and `archive` are the two halves of the filesystem: `store` owns
``logs/`` — the plaintext truth — plus the primitives and machine identity;
`archive` owns the layout of the encrypted repository under ``history/`` and its
self-description. Only `sync` and `prove` may reach `archive`. `history_dir` is
the one path that stays in `store`, because `store._private_paths` has to prune
that directory by name when it walks for `harden` and `readable_by_others`.

Three rules follow, and [`tests/test_architecture.py`](../tests/test_architecture.py)
holds all three against the real import graph rather than against the `import`
lines, so a lazy import inside a function correctly counts as no edge at all:

1. **No cycles.** Every box is movable on its own, which is the whole point of
   drawing them.
2. **Domains do not import domains.** `search` must not reach `sync` or
   `importer`; `sync` must not reach `search`. Where they appear to need each
   other, what they actually need is something further down — `entry.home_relative`
   lives in the format layer today precisely because `search` needed it and it
   was in `importer`.
3. **The table is exact.** A new edge is a one-line edit to `LAYERS` and a
   sentence in the pull request. That edit is the review.

`errors`, `deps`, `progress` and `report` are deliberately leaves. Each is asked
things by the layers above without knowing anything about them, so any module can
raise a `WoswoarError`, report a missing tool, tick a counter or hand back a
verdict without dragging a domain in behind it. `report` in particular has to be
free: a check is a value any module might return, and a value type that cost you
an import of the CLI would not get used.

---

## Two costs shape everything

Most of the non-obvious code in this repository is one of these two constraints
showing through. Knowing which one you are looking at is usually enough to
explain a decision that reads as strange.

**A keypress is a cold process.** Ctrl-R runs `woswoar list` in a fresh
interpreter, so anything imported at the top of the CLI module is paid for on
every search — about 29 ms of a ~105 ms total. That is why `sync` is imported
inside fourteen functions rather than at the top of `__main__.py`, why
`store.write_atomic` imports `tempfile` at the point of use (3.1 ms), why
`install.hook_bytes` reaches `importlib.resources` only as a fallback
(8.7 ms), and why
`cache.Cache` is a plain class rather than a dataclass (~4 ms, via `inspect`).
Each of those has its measurement in the comment beside it;
`tests/test_architecture.py` pins the ones that are otherwise only a convention,
and `tests/test_perf.py` guards the total they add up to.

**The repository is append-only.** Nothing under `history/` is ever modified or
deleted — `compact` is the single, opt-in exception — so a wrong byte is
permanent. That is why `export` extends the manifest it last signed rather than
listing the directory, why the recipient file uses tombstones rather than
deletions, and why every access-changing command fetches *before* it reads
`recipients.txt`. See the design summary for the reasoning; the shape to
recognise here is that these operations are ordered, and the ordering is the
correctness argument.

---

## The five recurring shapes

None of these is exotic. They are worth naming only because they recur, and
because a new module that invents a sixth spelling of one of them costs the next
reader more than the code itself does.

### 1. Truth and derived

`logs/` is the plaintext history and the only irreplaceable thing woswoar owns.
`history/`, `cache.txt` and `state.json` are all derived from it or from the
remote, and can be rebuilt.

The consequence is a **degradation rule**, and it is the reason so many readers
in this codebase swallow errors that look like they should be raised: anything
reading a derived artefact degrades towards "do the work again" rather than
failing. `cache.load` answers an empty cache for any damage at all;
`State.load` degrades field by field, with a comment on each saying which
direction of wrongness is the cheap one; `store.repo_format` reads garbage as
"unmarked" specifically so that four bytes written by anyone with push access
cannot stop every machine syncing.

If you are writing a reader, decide which side of this line its input is on
before you decide what it does with a bad value.

### 2. One place spells it

The single most common comment in this repository is some form of "there used to
be two of these". `store.ENV_KEYS` ("three copies of this tuple existed before it
moved here"), `sync.signing_public` ("`export`, `compact` and `signing_status`
each had their own"), `sync._append_recipient_line` ("the only writer"),
`sync._parse` ("the file's grammar, stated once"), `search.KEYS` (one table for
the `--bind` and the header that advertises it), `entry._INERT_TABLE` (reads its
replacements out of `_ESCAPES` so a round trip cannot drift).

The failure this prevents is always the same and always silent: two copies of a
fact, one of them updated.

### 3. Choke points

The strongest version of the rule above. Where an invariant has to hold at every
call site, the code is arranged so that violating it is *not expressible*, and
the test is written against the seam rather than against each caller. The
argument for testing the seam is in the design summary and is not hypothetical:
the first attempt at the `age` rule converted two call sites and missed a third,
and only a seam test would have caught it.

| invariant | seam | what it replaced |
|---|---|---|
| a chunk is never read unverified | `manifest.open_chunk` | `compact` reading chunks directly, which laundered planted ones |
| every git fork is countable | `gitrepo.git` | nothing yet; the fork-count tests already assumed it, and now it is checked |
| `age` is never handed a path in `$HOME` | `crypto._run` | per-function conversion, which missed `encrypt_to_recipients` |
| a peer's history is neutralised once | `cache._read_from`, via `parse_line(inert=True)` | neutralising per display site, forgotten once already |
| access never widens without a human | `__main__._confirm` | one `isatty` branch copied per command |
| access changes fetch before they act | `sync._access_change` | the ordering copied per command |

Adding a row here is a good thing. Adding a *caller* that bypasses one is the
thing to catch in review.

### 4. Decide in the domain, render in the CLI

150 of the package's 158 `print` calls are in `__main__.py`, and that is
deliberate. Anything a message asserts should be decided by a value the domain
returns, so that a test can reach it without grepping stdout:
`sync.Reader`, `sync.Candidate`, `sync.Newcomer` and `sync.IdentityStatus` all
exist for exactly this reason and say so. `Reader.display_name` is a method
rather than a rule about remembering `!r`, which is the same idea one notch
further in.

This is the pattern with the most unfinished business — see below.

### 5. Comment as decision record

Every non-obvious choice carries the alternative that was rejected and, where the
choice was about speed or size, the measurement that decided it. This is not
decoration: it is how a reader tells a deliberate oddity from an accident, and
it is what stops a "cleanup" reintroducing a bug that was fixed two years ago.
Match the density. [`CLAUDE.md`](../CLAUDE.md) and
[`CONTRIBUTING.md`](../CONTRIBUTING.md) have the rest of the working rules.

---

## Where the shape is under strain

The layering above is real and holds. The *sizes* do not, and this section
exists so that nobody has to rediscover it.

**`sync.py` is 3512 lines holding five separable concerns** — the recipient-file
grammar, trust and pinning, manifests, the chunk codec, and git plumbing — plus
the orchestration that drives them and the status queries `doctor` asks.

The visible symptom *was* `sync.Report`: seventeen fields, each a failure from a
different one of those layers. #199 has collapsed it. The ten fields that were
each a *kind* of outcome are one dictionary keyed by `sync.Outcome` — a declared
constant carrying its own name, its severity and its paragraph — so `Report` is
seven fields that name no kind at all, `notices()` is a loop over the kinds in
declaration order, and a new failure mode is one `record` call beside the code
that notices it.

That did not make the file shorter; it is a hundred lines longer, because each
kind now carries the comment that used to sit on its field. Shortening it was
never the point. The prose is **movable** now: a kind is a constant, its prose
and one call site, so each slice #201 carves out of this module takes its own
kinds with it instead of leaving a 143-line method behind that binds all five
concerns at once.

**`__main__.py` was 1813 lines and inverted pattern 4 in two places.** #202 has
split both out. `install.py` owns which shells woswoar is responsible for, what
their hooks contain and how they are kept current — including `refresh_hook`,
which is policy that runs unattended from a background sync and was held up by a
single test asserting on stdout.

`setup.py` is smaller than its name suggests, and the boundary is worth stating
because the obvious reading is wrong. It holds what the wizard works out *without
asking* — which histories exist, whether anything is set up here, which rule
picked the shells — plus the two `input` primitives. The questions' wording and
the four numbered steps stay in `__main__`, because a dialog prints each
paragraph before the `input` under it: its prose and its control flow are one
thing. Separating them needs the four commands passed in as callables, which is a
different change from this one.

What stayed is the dispatch: `cmd_setup` still calls install, import, init and
sync in order, because running CLI commands is the CLI's job and a module below
it that called back up would be a cycle. What is gone is the way it called them —
four hand-built `argparse.Namespace` objects, where a flag added to `install`
had to be remembered three screens away and `getattr(args, "shell", None)`
swallowed the omission. Each command now has a keyword-argument half that
`cmd_setup` and the command itself both go through, so mypy names the call site
that forgot one. The test for the wizard was forging a namespace too, and had
been missing `shell` entirely since it was written.

**`store.py` used to carry three vocabularies** — paths under `logs/`, paths
under `history/`, and the filesystem primitives — so everything that wanted
`logs_dir()` also depended on the chunk layout. The middle one is
`woswoar/archive.py` now (#200), and `cache` and `search` no longer reach it.
What is left in `store` that does not belong to it is four per-machine files
`sync` owns — `state_file`, `sync_stamp_file`, `sync_failure_file`,
`signing_key_file`. They stayed deliberately: none of them is *in* the
repository, so filing them under `archive` would be worse than leaving them, and
their home is whatever #201 carves out of `sync`.

**Outcome reporting had five spellings**, and `report` is now the answer (#199).
It holds two, and the distinction between them is real rather than a compromise:

- **`Check`** is a verdict — one line, a label, a marker, and pass/fail/info.
  `doctor`, `prove` and `sync`'s four `*_status` functions return them, and
  `report.lines` is the only thing that renders one. `sync.IdentityStatus` is
  gone.
- **`Notice`** is an explanation — a paragraph with no label and nothing a marker
  could usefully say, because several of them carry a recovery recipe.
  `Report.notices()` decides which apply and `report.paragraphs` renders them.
  `cmd_sync`'s eleven `if report.X:` blocks became eleven in `notices()` and are
  now a loop over `sync.OUTCOMES`; the twelfth, `if report.pushed`, is a summary
  line and stayed. The prose lives on the kind, so neither `Report` nor
  `notices()` mentions any individual one.

Stretching `Check` to cover both was the other option and it was worse: the label
column and the marker would have been dead weight on every notice, and `lines()`
would have grown a mode. Two shapes, one module, and a rule for picking — if it
fits a line and can fail, it is a check.

What remains is smaller and is *not* exempt by the rule above, which is worth
saying plainly rather than implying: `deps.report` returns preformatted
paragraphs and is Notice-shaped; `search.empty_note` is a single line that cannot
fail and is Check-shaped without a label; `importer.Result` is counts the CLI
words, and `progress` is a different thing altogether — a live Protocol, not a
result. The first two could convert and have not.

These are tracked as issues rather than fixed in passing, and the layering above
is what makes them fixable one at a time.
[#203](https://github.com/martinus/woswoar/issues/203) carries the argument for
treating them as one piece of work — they are two half-finished ideas rather than
four chores — along with what "done" looks like and what must not change on the
way. The order is not the order they are listed in above: the two cheap ones come
first because they make the expensive one smaller.

| | issue |
|---|---|
| 1 | ~~[#200](https://github.com/martinus/woswoar/issues/200) — split the repo layout out of `store.py`~~ — **done**, as `woswoar/archive.py`. |
| 2 | ~~[#199](https://github.com/martinus/woswoar/issues/199) — one shape for outcome reporting~~ — **done**, in three parts: `report.Check`, `report.Notice`, and `sync.Outcome` collapsing `Report`'s seventeen fields to seven. |
| 3 | ~~[#202](https://github.com/martinus/woswoar/issues/202) — lift the installer and the setup wizard out of `__main__.py`~~ — **done**, as `woswoar/install.py` and `woswoar/setup.py`. |
| 4 | [#201](https://github.com/martinus/woswoar/issues/201) — split `sync.py`, in slices, after the three above. |
