"""What the parse cache promises about a history it did not choose.

`cache.txt` is derived and rebuildable (CLAUDE.md rule 8), so the stakes here
are not data loss. They are worse in one specific way: a cache that is *wrong*
rather than absent makes Ctrl-R show something other than what was typed, and
nothing downstream can tell. The module says so itself, rejecting a repair that
"made the same command render one way from a warm cache and another after a
rebuild, and a derived artefact must never change what you see".

`tests/test_cache.py` round-trips the format with entries someone chose. These
ask the same question of entries nobody chose -- which is where the format's
three separators live, since `\\x00`, `\\x01` and `\\x02` are all things a peer's
command can contain and a local one can too.
"""

from __future__ import annotations

import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.test_record_properties import ENTRIES
from woswoar import cache
from woswoar.entry import Entry

settings.load_profile("woswoar")  # registered beside `ENTRIES`, which this imports.

#: A log's path inside `logs/`, in the shape `store` produces. Not generated
#: text: a separator in a *relpath* corrupts the header rather than a row, which
#: is a different claim from the one these properties are about, and the code
#: that builds relpaths cannot produce one.
RELPATH = st.builds("{}/{}.log".format, st.sampled_from(["a", "b"]), st.sampled_from(["1", "2"]))

HOST = st.sampled_from(["hostA", "hostB"])

#: One log file's worth of the cache: which file, whose machine, what is in it.
FILES = st.lists(
    st.tuples(RELPATH, HOST, st.lists(ENTRIES, max_size=4)),
    max_size=3,
).map(lambda rows: {relpath: (host, entries) for relpath, host, entries in rows})

SEPARATORS = ("\x00", "\x01", "\x02")

#: One ordinary entry, for the pin below. Fixed rather than generated: it needs
#: a timestamp whose digits can be found in the dump by `bytes.index`.
_SAMPLE = Entry(
    ts=1700000000, host="h", session="s", cwd="/tmp", exit_code=0, duration_ms=5, cmd="echo hi"
)


def built(files: dict[str, tuple[str, list[Entry]]]) -> cache.Cache:
    """A `Cache` holding exactly ``files``, as `refresh` would leave it."""
    out = cache.Cache()
    for relpath, (host, entries) in files.items():
        flat: list[str] = []
        for item in entries:
            flat.extend(cache.fields_of(item))
        out.files[relpath] = flat
        out.meta[relpath] = cache.FileMeta(
            size=len(flat), mtime_ns=1, offset=len(flat), head=b"\xde\xad", host=host
        )
    return out


def has_separator(flat: list[str]) -> bool:
    return any(sep in field for field in flat for sep in SEPARATORS)


class TestTheFormatRoundTrips(unittest.TestCase):
    @given(FILES)
    def test_a_file_comes_back_exactly_or_not_at_all(
        self, files: dict[str, tuple[str, list[Entry]]]
    ) -> None:
        """The claim that matters, and it is deliberately two-sided.

        A file whose fields hold a separator is *omitted* -- the module argues
        for that over stripping the byte, because stripping changes what the
        user sees. So the property is not "everything round-trips": it is that
        whatever does come back is byte-identical, and whatever does not is
        exactly the files that could not be written unambiguously. A one-sided
        version would pass against a `dumps` that silently dropped everything.
        """
        original = built(files)
        back = cache.loads(cache.dumps(original))
        for relpath, flat in original.files.items():
            if has_separator(flat):
                self.assertNotIn(relpath, back.files, f"{relpath} holds a separator")
            else:
                self.assertEqual(back.files.get(relpath), flat)
                self.assertEqual(back.meta.get(relpath), original.meta[relpath])

    @given(FILES)
    def test_nothing_is_invented(self, files: dict[str, tuple[str, list[Entry]]]) -> None:
        original = built(files)
        back = cache.loads(cache.dumps(original))
        self.assertTrue(set(back.files) <= set(original.files))
        self.assertEqual(set(back.files), set(back.meta) & set(back.files))

    @given(FILES)
    def test_the_entries_come_back_as_entries(
        self, files: dict[str, tuple[str, list[Entry]]]
    ) -> None:
        """`entries()` rebuilds `Entry` objects from the flat fields, and `host`
        comes from the file header rather than from the row -- the whole reason
        the format stopped carrying a copy of it on every one of a hundred
        thousand rows. If that lookup were wrong, every entry would be
        attributed to the wrong machine and still parse."""
        original = built(files)
        expected = [
            item._replace(host=host)
            for relpath, (host, entries) in files.items()
            if not has_separator(original.files[relpath])
            for item in entries
        ]
        back = cache.loads(cache.dumps(original))
        self.assertEqual(sorted(back.entries()), sorted(expected))


class TestDamageBecomesARebuild(unittest.TestCase):
    """`load` turns a broken cache into an empty one and re-parses. That is only
    true if `loads` confines itself to the exceptions `load` catches."""

    #: What `woswoar/cache.py:425` catches. Anything else reaches the user as a
    #: traceback on a path where the correct answer is "rebuild it".
    CAUGHT = (OSError, ValueError, IndexError)

    def whole(self, loaded: cache.Cache) -> None:
        """Whatever `loads` returns must be *shaped* like a cache.

        Permitting it to succeed is not enough on its own -- that is a property
        with no teeth, and a mutation removing the "not a whole number of
        entries" check survived the first version of this file. Downstream
        slices the flat list six at a time and zips it `strict=True`, so a
        length that is not a multiple is a `ValueError` raised somewhere no one
        is catching it.
        """
        for relpath, flat in loaded.files.items():
            self.assertEqual(len(flat) % 6, 0, f"{relpath}: {len(flat)} fields")
            self.assertIn(relpath, loaded.meta)

    @given(st.binary(max_size=120))
    def test_arbitrary_bytes_raise_only_what_load_catches(self, blob: bytes) -> None:
        try:
            loaded = cache.loads(blob)
        except self.CAUGHT:
            return
        self.whole(loaded)

    @given(FILES, st.integers(0, 400), st.binary(min_size=1, max_size=3))
    def test_a_corrupted_dump_raises_only_what_load_catches(
        self, files: dict[str, tuple[str, list[Entry]]], at: int, junk: bytes
    ) -> None:
        """Damage a real dump rather than only feeding it noise. Random bytes
        almost never get past the magic, so a test built on those alone never
        reaches the parsing below it."""
        blob = cache.dumps(built(files))
        if not blob:
            return
        cut = at % len(blob)
        for damaged in (blob[:cut] + junk + blob[cut:], blob[:cut], blob[:cut] + blob[cut + 1 :]):
            try:
                loaded = cache.loads(damaged)
            except self.CAUGHT:
                continue
            self.whole(loaded)


class TestTheKnownGap(unittest.TestCase):
    """#314, held open so that closing it is noticed.

    `loads` leaves the numeric entry fields as strings and says a bad one
    "surfaces as a rebuild rather than a traceback, which is what every other
    kind of damage to this file already does". It does not: `loads` accepts the
    cache, and the `int()` happens later in `entries()`, outside `load`'s
    `try`. Fix #314 and this test fails, and whoever fixed it deletes the class.
    """

    def test_one_byte_of_ordinary_corruption_reaches_it(self) -> None:
        """The route that matters, and the reason #314 is P2 rather than P3.

        A cache row is a ten-digit timestamp, a session, a cwd, two more
        numbers and the command -- so a large share of the file's bytes *are*
        digits in a numeric field, and changing one is the most ordinary thing
        corruption does. Fuzzing a real dump at one to three byte positions,
        294 of 4000 trials were accepted by `loads` and then raised in
        `entries()`. This is one of them, made deterministic.
        """
        original = built({"a/1.log": ("h", [_SAMPLE])})
        blob = cache.dumps(original)
        stamp = str(_SAMPLE.ts).encode("utf-8")
        at = blob.index(stamp)
        damaged = blob[: at + 3] + b"?" + blob[at + 4 :]

        back = cache.loads(damaged)
        with self.assertRaises(ValueError, msg="#314 looks fixed -- delete this class"):
            back.entries()

    def test_loads_accepts_a_field_that_is_not_a_number(self) -> None:
        broken = cache.Cache()
        broken.meta["a/1.log"] = cache.FileMeta(size=1, mtime_ns=1, offset=1, head=b"", host="h")
        broken.files["a/1.log"] = ["not-a-number", "s", "/tmp", "0", "5", "echo hi"]
        back = cache.loads(cache.dumps(broken))
        self.assertEqual(back.files["a/1.log"][0], "not-a-number")
        with self.assertRaises(ValueError, msg="#314 looks fixed -- delete this class"):
            back.entries()


if __name__ == "__main__":
    unittest.main()
