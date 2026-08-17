"""Mutations for #201: the two layers that left `sync`.

A pure move has no new behaviour to test, so the question rule 3 asks is a
different one: are the tests that guarded this code still *reaching* it? A move
that quietly orphaned a suite would look exactly like this one from the outside,
and two of the tests involved assert emptiness -- a count that stayed at zero --
which is the shape that passes vacuously when a spy is installed on the wrong
module. Those two are the reason this table exists.

The last three are the seams themselves: `mock.patch.object` now names `gitrepo`
and `manifest` rather than `sync`, and a `from` import in `sync` would have left
every one of those patches watching an object nothing calls.
"""

from tools.mutate import Mutation

MUTATIONS = [
    Mutation(
        "a second module builds a git argv",
        "woswoar/search.py",
        '["fzf", "--version"], capture_output=True, text=True, timeout=5, check=False',
        '["git", "--version"], capture_output=True, text=True, timeout=5, check=False',
        "tests.test_architecture.TestOneModuleSpawnsGit",
    ),
    Mutation(
        "manifest grows an edge the table does not allow",
        "woswoar/manifest.py",
        "from . import archive, crypto, store",
        "from . import archive, crypto, report, store",
        "tests.test_architecture.TestTheLayering",
    ),
    Mutation(
        "open_chunk stops checking the digest",
        "woswoar/manifest.py",
        "if digest_of(blob) != expected:",
        "if False:",
        "tests.test_sync.TestChunkAuthenticity",
    ),
    Mutation(
        "a manifest no longer has to name its own day",
        "woswoar/manifest.py",
        "if not lines or lines[0] != _header(host_id, day):",
        "if not lines:",
        "tests.test_sync.TestChunkAuthenticity",
    ),
    Mutation(
        "a deleted manifest reads as present",
        "woswoar/manifest.py",
        "return not archive.day_manifest(host_id, day).exists()",
        "return False",
        "tests.test_sync.TestADeletedManifest",
    ),
    Mutation(
        "resolve stops checking the shape of what rev-parse echoed",
        "woswoar/gitrepo.py",
        'return [line if len(line) in (40, 64) and _HEX.issuperset(line) else "" '
        "for line in printed]",
        "return printed",
        "tests.test_sync.TestResolvingSeveralRefsInOneFork",
    ),
    # The three seams. Each of these is caught only if the spy in the test is
    # installed on the module the call actually goes through.
    Mutation(
        "the commit identity is written on every sync (spy: gitrepo.git)",
        "woswoar/gitrepo.py",
        "    if repo.name != COMMIT_NAME:",
        "    if True:",
        "tests.test_sync.TestSyncDoesNotForkGitMoreThanItNeedsTo",
    ),
    Mutation(
        "an idle sync stages the whole tree again (spy: gitrepo.commit)",
        "woswoar/sync.py",
        "committed = gitrepo.commit() if published or exported or marked else False",
        "committed = gitrepo.commit()",
        "tests.test_sync.TestAFinderVisitDoesNotBreakSync",
    ),
    Mutation(
        "a day with nothing new reads its manifest anyway (spy: manifest.read)",
        "woswoar/sync.py",
        """        already = frozenset(state.merged.get(key, ()))
        fresh = [name for name in names if name not in already]
        if not fresh:""",
        """        already = frozenset(state.merged.get(key, ()))
        fresh = [name for name in names if name not in already]
        if False:""",
        "tests.test_sync.TestADayThatGainsAChunkAfterCompaction",
    ),
]
