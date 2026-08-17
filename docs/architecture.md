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

```
errors ─┬─ credentials ─┐
        └─ crypto ──────┼─ sync ─┐
entry ──┬─ store ───────┘        ├─ prove
        ├─ cache ── search ──────┼─ __main__
        └─ importer ─────────────┘
deps, progress ─────────────────── (leaves, asked things by anyone)
```

| layer | modules | what it may know |
|---|---|---|
| format | `entry`, `errors` | nothing else in the package |
| platform | `store`, `crypto`, `deps`, `progress`, `credentials` | the format layer |
| derived | `cache` | the two above |
| domains | `search`, `sync`, `importer` | everything below, and **never each other** |
| composition | `prove`, `__main__` | everything |

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

`errors`, `deps` and `progress` are deliberately leaves. Each is asked things by
the layers above without knowing anything about them, so any module can raise a
`WoswoarError`, report a missing tool, or tick a counter without dragging a
domain in behind it.

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
`_hook_bytes` reaches `importlib.resources` only as a fallback (8.7 ms), and why
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
| a chunk is never read unverified | `sync.open_chunk` | `compact` reading chunks directly, which laundered planted ones |
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

**`sync.py` is 3234 lines holding five separable concerns** — the recipient-file
grammar, trust and pinning, manifests, the chunk codec, and git plumbing — plus
the orchestration that drives them and the status queries `doctor` asks. The
visible symptom is `sync.Report`: seventeen fields, each a failure from a
different one of those layers, rendered by twelve consecutive `if` blocks in
`cmd_sync`. A new failure mode therefore means editing `Report`, `run`,
`cmd_sync` and `test_sync.py` together.

**`__main__.py` is 2102 lines and inverts pattern 4 in two places.** An installer
(`installed_shells`, `detect_shells`, `shells_from`, `_hook_bytes`,
`_write_block`, `_stale_hooks`, `_refresh_hook`) and a setup wizard
(`_importable`, `_untouched`, `_offer_imports`, `_offer_remote`) are both
subsystems living in the argparse module, reachable by a test only through
stdout. The wizard builds `argparse.Namespace` objects by hand to call sibling
commands, which is the CLI using its own argument format as an internal API.

**`store.py` carries three vocabularies**: paths under `logs/`, paths under
`history/`, and filesystem primitives. The middle one is `sync`'s alone, which is
why everything that wants `logs_dir()` currently depends on the chunk layout too.

**Outcome reporting has five spellings.** `sync` returns a `Report` dataclass and
`IdentityStatus` records; `search.empty_note` returns prose; `importer` returns
counts; `deps.report` returns prose; `progress` uses a `Protocol` with two
implementations. Each is defensible alone. Together they mean there is no answer
to "how should a new check report itself?", which is the gap `doctor` falls into
— four of its lines come from `sync` as `IdentityStatus` values, and the rest of
its eighteen `check` calls are derived inline in the CLI, where only a test that
greps stdout can reach them.

These are tracked as issues rather than fixed in passing, and the layering above
is what makes them fixable one at a time.
[#203](https://github.com/martinus/woswoar/issues/203) carries the argument for
treating them as one piece of work — they are two half-finished ideas rather than
four chores — along with what "done" looks like and what must not change on the
way. The order is not the order they are listed in above: the two cheap ones come
first because they make the expensive one smaller.

| | issue |
|---|---|
| 1 | [#200](https://github.com/martinus/woswoar/issues/200) — split the repo layout out of `store.py`. Mechanical, and it takes the chunk layout out of `search`'s dependency cone. |
| 2 | [#199](https://github.com/martinus/woswoar/issues/199) — one shape for outcome reporting, `doctor` first. This is what shrinks `Report`. |
| 3 | [#202](https://github.com/martinus/woswoar/issues/202) — lift the installer and the setup wizard out of `__main__.py`. |
| 4 | [#201](https://github.com/martinus/woswoar/issues/201) — split `sync.py`, in slices, after the three above. |
