# Woswoar - Design Summary

## Project Goal

Create a lightweight, Git-based, multi-machine shell history tool inspired by Atuin but with different design goals:

- Search uses `fzf`.
- Synchronization is Git-based.
- No dedicated server.
- Easy onboarding of new machines.
- Prefer minimal dependencies.
- Eventually target Python standard library only.

---

## Proposed Name

### Current Favorite: `woswoar`

Austrian dialect for:

> "Was war?"
> "What was it again?"

This directly matches the primary use case:

> "What was that command I used before?"

Examples:

```bash
woswoar
woswoar sync
woswoar search
```

Why it was chosen:

- Austrian-flavored.
- Short.
- Memorable.
- Unique.
- Closely matches the actual purpose of command history search.

---

## High-Level Architecture

```text
Git Repository
    ↓
Append-only history files
    ↓
Local cache (binary blob)
    ↓
Python scope filtering
    ↓
fzf
```

Important design decision:

Git is transport and storage.

The local cache exists only for performance.

---

## Repository Layout

Current preferred layout:

```text
history.git/
└── hosts/
    ├── martin@desktop/
    │   ├── 2026-07-29.tsv
    │   ├── 2026-07-30.tsv
    ├── martin@laptop/
    │   ├── 2026-07-29.tsv
```

Characteristics:

- Each machine only writes its own files.
- Files are append-only.
- Git merge conflicts should be extremely rare.
- Adding a new machine only requires cloning the repository.

Machine identity:

```text
user@hostname
```

---

## History File Format

Preferred format:

```text
timestamp<TAB>session<TAB>cwd<TAB>command
```

Example:

```text
1753781234\tabc123\t/src/foo\tgit status
1753781240\tabc123\t/src/foo\tninja -C build
```

Reasons:

- Human-readable.
- Easy to parse.
- Git-friendly.
- Smaller and simpler than JSON.

Metadata intentionally stored:

- timestamp
- session id
- working directory
- command

Possible future additions:

- exit code
- runtime

---

## Search Scopes

Three search scopes were identified as important:

### Global

Search across every synchronized machine.

### Host

Search only commands executed on the current host.

### Session

Search only commands executed in the current shell session.

Session IDs should be generated at shell startup.

---

## Search Design

### Initial Idea

Several approaches were discussed:

1. SQLite + FTS.
2. Generated indexes.
3. Pure file-based search.

### Current Conclusion

Given actual history size:

```text
52,000 commands
≈ 2 years
```

The project most likely does NOT need SQLite.

The data volume is small enough that simple in-memory structures are likely sufficient.

---

## Why SQLite Was Removed

SQLite was initially attractive because:

- filtering
- indexing
- FTS search

However:

- Current dataset is small.
- Adds complexity.
- Introduces migration and schema management.
- Does not fit the goal of minimal dependencies.

Current philosophy:

> Do not introduce a database until profiling proves it is required.

---

## Local Cache Design

Instead of SQLite:

```text
logs
 ↓
parse once
 ↓
pickle cache
 ↓
load quickly next startup
```

Possible cache contents:

```python
{
    "entries": [...],
    "file_offsets": {...}
}
```

Stored as:

```text
~/.cache/woswoar/cache.pickle
```

---

## Incremental Updates

Key idea:

The cache stores the last processed position of each file.

Example:

```python
file_offsets = {
    "hosts/desktop/2026-07-29.tsv": 12345,
}
```

On startup:

1. Load cache.
2. Check file sizes.
3. Read only appended content.
4. Add new entries.
5. Save cache.

No full re-import required.

---

## In-Memory Representation

Preferred model:

```python
@dataclass(slots=True)
class Entry:
    ts: int
    host: str
    session: str
    cwd: str
    cmd: str
```

Rationale:

- Simple.
- Fast.
- Standard-library only.

---

## Indexes

Current conclusion:

Do not generate persistent indexes.

Instead use Python structures:

```python
entries
entries_by_host
entries_by_session
```

or simply filter on demand.

Reason:

52k commands is tiny.

Premature indexing is unnecessary complexity.

---

## Relative Time Display

Store timestamps only:

```python
1753781234
```

Do NOT store:

```text
5m ago
```

because it becomes stale.

Instead generate relative times right before invoking fzf:

```text
2m  git status
1h  ninja -C build
3d  git rebase
```

---

## fzf Responsibilities

fzf should ONLY:

- fuzzy match
- display results
- allow selection

Python should handle:

- loading cache
- filtering by scope
- sorting
- relative date formatting
- ranking

Separation of concerns:

```text
Python = search engine
fzf    = UI
```

---

## Current Search Flow

```text
Ctrl-R
    ↓
load cache
    ↓
apply scope
    ↓
sort newest-first
    ↓
generate relative times
    ↓
pipe to fzf
    ↓
return selected command
```

---

## Synchronization Strategy

Periodic sync:

```bash
git pull --rebase
```

Update cache.

Then:

```bash
git add
git commit
git push
```

Important design principle:

Git is the synchronization mechanism.

No dedicated server.

No custom replication protocol.

No central database.

---

## Dependency Philosophy

Strong preference:

```text
Python standard library only
```

Expected modules:

- dataclasses
- pathlib
- pickle
- subprocess
- tempfile
- time
- uuid

External dependency accepted:

```text
fzf
```

because it is the user interface.

---

## Current Status

The design has converged toward:

```text
woswoar
    ↓
Git-backed
    ↓
Append-only TSV logs
    ↓
Pickle cache
    ↓
In-memory filtering
    ↓
fzf UI
```

Key philosophy:

Keep it absurdly simple until profiling proves complexity is required.
