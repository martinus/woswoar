"""The credential filter, asked about commands nobody wrote down.

`tests/test_credentials.py` holds the filter to a corpus: every shape in
`SECRET_SHAPES` is caught, every shape in `INNOCENT_SHAPES` is not. That pins
the shapes. What it cannot pin is the *context* they arrive in -- and context is
where this filter actually fails, because every rule in it is a regex with
boundaries. `--auth-mode` is kept out of the option rule by a trailing boundary;
a JWT is anchored with ``\\b``. A rule that matches a shape standing alone and
misses it inside a real command line would pass the corpus and lose the secret.

So the question here is metamorphic: not *is this a credential*, which has no
oracle, but *does the answer survive things that must not change it*.

One direction only, deliberately. `docs/security.md:350` says the filter is
best-effort by design, so "no secret gets through" is not a claim the project
makes and must not be one a test makes on its behalf -- it would freeze
whatever the regex happens to do today into a guarantee. What is claimed, and
what is asserted here, is that a shape the filter *does* recognise stays
recognised when a shell line is built around it.
"""

from __future__ import annotations

import os
import re
import unittest
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.credential_shapes import INNOCENT_SHAPES, SECRET_SHAPES
from woswoar import credentials

settings.register_profile("woswoar-credentials", max_examples=400, deadline=None)
settings.load_profile("woswoar-credentials")

#: The one shape whose match does not survive a metacharacter, and the reason
#: is #312: the option rule's boundary is ``(?:[=\s]|$)``, so a *bare* flag
#: ending a word before ``;`` or ``|`` falls out of the rule.
#:
#: Pinned rather than quietly excluded, and pinned as a live assertion below
#: rather than as a comment. `tests/credential_shapes.py` already argues why:
#: an unbacked claim in a security document is worse than a known gap, because
#: a later change falsifies it with CI green and nobody looks. The same holds
#: for a property with an unexplained hole in it.
ESCAPES_A_TRAILING_METACHARACTER = "gh auth login --with-token"

#: Everything the embedding properties are allowed to assert about. When #312
#: is fixed this list becomes `SECRET_SHAPES` again and the pin below goes red.
EMBEDDABLE = [s for s in SECRET_SHAPES if s != ESCAPES_A_TRAILING_METACHARACTER]

SECRET = st.sampled_from(EMBEDDABLE)
INNOCENT = st.sampled_from(INNOCENT_SHAPES)

#: What can stand before a command on one shell line. Every one of these ends in
#: a separator, and that is the whole point rather than a convenience: several
#: rules are anchored -- a JWT on ``\b``, the option rule on a leading ``--`` --
#: so gluing an arbitrary byte directly onto a shape breaks the match honestly.
#: ``aeyJhbGci...`` is not a JWT and the filter is right to say so. The claim
#: being tested is about a shape embedded in a *command line*, not about one
#: with characters welded to its front.
BEFORE = st.sampled_from(
    [
        "",
        "sudo ",
        "env FOO=bar ",
        "time ",
        "true && ",
        "false || ",
        "echo hi; ",
        "cd /tmp && ",
        "  ",
        "\t",
        "nice -n 19 ",
    ]
)

#: And after. A trailing redirect, a pipe, a comment, another command.
AFTER = st.sampled_from(
    [
        "",
        " ",
        " > /dev/null",
        " 2>&1",
        " | grep -v x",
        " # deploying",
        " && echo done",
        "; echo done",
        " &",
        "\n",
    ]
)


class TestASecretStaysRecognised(unittest.TestCase):
    """Recall, under everything that must not change the answer."""

    @given(SECRET, BEFORE, AFTER)
    def test_a_shape_inside_a_command_line_is_still_caught(
        self, secret: str, before: str, after: str
    ) -> None:
        """The failure this file exists for. A rule that needs its shape to
        start the line, or to end it, passes the corpus and drops nothing when
        the command is `sudo ... | tee log`."""
        line = before + secret + after
        self.assertTrue(
            credentials.looks_like_credential(line),
            f"a known secret shape survived being embedded: {line!r}",
        )

    @given(SECRET, INNOCENT)
    def test_a_secret_joined_to_an_ordinary_command_is_still_caught(
        self, secret: str, innocent: str
    ) -> None:
        """Both orders. A `;` list is one history entry, and the whole entry is
        what gets published -- so a filter that only looks at the first command
        publishes the second."""
        self.assertTrue(credentials.looks_like_credential(f"{innocent}; {secret}"))
        self.assertTrue(credentials.looks_like_credential(f"{secret}; {innocent}"))

    @given(SECRET, st.text(alphabet=" \t", max_size=4))
    def test_leading_whitespace_does_not_hide_it(self, secret: str, pad: str) -> None:
        """`HIST_IGNORE_SPACE` means a space-prefixed command is a shape users
        type on purpose, so the filter meets it often."""
        self.assertTrue(credentials.looks_like_credential(pad + secret))


class TestTheKnownGap(unittest.TestCase):
    """#312, held open so that closing it is noticed.

    This asserts the bug, which is the only way a pin is worth having: fix the
    boundary and this test fails, and whoever fixed it deletes this class and
    puts the shape back in `EMBEDDABLE`. A comment saying "known gap" would
    still be sitting here in a year.
    """

    def test_the_shape_is_dropped_when_it_ends_the_line(self) -> None:
        """Guard the guard. If this ever stops being a recognised shape, the
        pin below would pass for the wrong reason -- the rule not matching it
        at all rather than the boundary being narrow."""
        self.assertTrue(credentials.looks_like_credential(ESCAPES_A_TRAILING_METACHARACTER))

    @given(st.sampled_from([";", "|tee log", "&", ")", '"', "'", ">out", "&&x", "\\"]))
    def test_and_is_not_when_a_metacharacter_follows(self, tail: str) -> None:
        self.assertFalse(
            credentials.looks_like_credential(ESCAPES_A_TRAILING_METACHARACTER + tail),
            "#312 looks fixed -- delete this class and restore the shape to EMBEDDABLE",
        )


class TestAnOrdinaryCommandStaysOrdinary(unittest.TestCase):
    """Precision, which the module docstring says matters more than recall:
    dropping a command is silent and unrecoverable."""

    @given(INNOCENT, INNOCENT)
    def test_two_ordinary_commands_are_still_ordinary(self, one: str, two: str) -> None:
        """Composition cannot manufacture a credential. Both halves are drawn
        from the corpus rather than generated, because there is no oracle for
        "this arbitrary string is not a secret" -- generated text would be
        asserting the regex against itself."""
        for line in (f"{one}; {two}", f"{one} && {two}", f"{one} | {two}"):
            self.assertFalse(
                credentials.looks_like_credential(line),
                f"two ordinary commands became a credential: {line!r}",
            )

    @given(INNOCENT, BEFORE, AFTER)
    def test_wrapping_an_ordinary_command_does_not_condemn_it(
        self, innocent: str, before: str, after: str
    ) -> None:
        self.assertFalse(credentials.looks_like_credential(before + innocent + after))


class TestTheUsersOwnPattern(unittest.TestCase):
    """`WOSWOAR_IGNORE_EXTRA`, which is the user's to get right and the
    project's to be honest about."""

    @given(st.one_of(SECRET, INNOCENT, st.text(max_size=30)), st.text(max_size=30))
    def test_extra_only_ever_adds(self, command: str, extra: str) -> None:
        """Monotone: a user's own rule can widen what is dropped and can never
        narrow it. The opposite would be a setting that silently publishes
        something the defaults caught.

        `command` is drawn from the corpus and not only from `st.text`, and that
        is the whole strength of this property. Written with generated text
        alone the antecedent is almost never true -- random strings do not
        contain ``--password`` -- so the implication holds vacuously and a
        mutation turning the `or` into an `and` survived it.
        """
        try:
            pattern = re.compile(extra)
        except re.error:
            return
        if credentials.looks_like_credential(command):
            self.assertTrue(credentials.looks_like_credential(command, pattern))

    @given(
        # No NUL and no surrogates: `os.environ` refuses both with an
        # exception from the patch itself, which reads as the property failing
        # when it is the fixture that cannot be built.
        #
        # `codec="utf-8"` rather than excluding the surrogate category by name:
        # a lone surrogate is precisely what utf-8 cannot encode, so this says
        # the same thing in the terms the constraint actually has -- and the
        # category spelling is a tuple of `str` where the signature wants a
        # collection of literals, which mypy rejects on CI's hypothesis.
        st.text(
            alphabet=st.characters(codec="utf-8", exclude_characters="\x00"),
            min_size=1,
            max_size=30,
        )
    )
    def test_a_pattern_is_used_or_refused_but_never_dropped(self, raw: str) -> None:
        """Three outcomes and no fourth. The docstring's word is "loudly":
        silently ignoring an uncompilable rule leaves the user believing their
        own filter ran over the import when it did not.
        """
        with mock.patch.dict(os.environ, {"WOSWOAR_IGNORE_EXTRA": raw}, clear=False):
            try:
                got = credentials.user_pattern()
            except credentials.BadExtraPattern:
                with self.assertRaises(re.error):
                    re.compile(raw)
                return
            self.assertIsNotNone(got)
            assert got is not None
            self.assertEqual(got.pattern, re.compile(raw).pattern)

    @given(st.sampled_from(["", None]))
    def test_unset_or_empty_means_no_pattern(self, raw: str | None) -> None:
        """The default path, and the reason `user_pattern` reads `_EXTRA` and
        never `WOSWOAR_IGNORE`: the latter's default is a POSIX ERE Python
        cannot compile, so reading it would warn every user about a variable
        they never touched."""
        environ = dict(os.environ)
        environ.pop("WOSWOAR_IGNORE_EXTRA", None)
        if raw is not None:
            environ["WOSWOAR_IGNORE_EXTRA"] = raw
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertIsNone(credentials.user_pattern())


if __name__ == "__main__":
    unittest.main()
