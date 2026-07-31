"""The importer's credential filter, and its contract with the bash hook.

Two implementations of the same rules, in two languages, for two budgets. The
hook is tested against the shared corpus by running a real bash
(`tests/test_shell_hook.py`); this module runs the same corpus through Python.

The contract is deliberately not "identical". The importer is allowed to be
*stricter*, because it has no per-prompt budget and can afford token signatures
the hook cannot. It is never allowed to be looser, and it must never eat an
ordinary command that the hook keeps.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from woswoar import credentials

from .credential_shapes import DOCUMENTED_GAPS, INNOCENT_SHAPES, SECRET_SHAPES

#: Gaps in the *hook* that the importer is expected to close, because it can
#: afford to look for the token itself rather than the shape around it.
CLOSED_ON_IMPORT = {
    "deploy.sh AKIAIOSFODNN7EXAMPLE",
    "aws configure set aws_secret_access_key wJalr",
    "vault kv put secret/app value=hunter2",
    "redis-cli -a hunter2",
}

#: One command per rule in `_IMPORT_ONLY` and `_TOKENS`. Every entry here is a
#: claim `docs/shell-integration.md` makes about what an import catches; without
#: them, half those rules could be deleted with CI green while the document
#: kept promising them.
IMPORT_ONLY_SHAPES = [
    "aws configure set aws_secret_access_key wJalrXUtnFEMI",
    "aws configure set default.aws_session_token abc",
    "vault kv put secret/app value=hunter2",
    "vault write auth/approle/login secret_id=abc",
    "redis-cli -a hunter2 ping",
    "redis-cli -ahunter2",
    "mongosh -p hunter2 mydb",
    "mongo -phunter2",
    "az login -u me -p hunter2",
    "smbclient //host/share -U user%hunter2",
    "pscp -pw hunter2 file host:/tmp",
]

TOKEN_SHAPES = [
    "deploy.sh AKIAIOSFODNN7EXAMPLE",
    "aws sts assume-role ASIAY34FZKBOKMUTVV7A",
    "curl -H 'X-Api: ghp_" + "a" * 36 + "'",
    "echo github_pat_" + "a" * 22,
    "echo glpat-" + "a" * 20,
    "slack post xoxb-1234567890-abcdef",
    "stripe listen sk_live_" + "a" * 16,
    "stripe listen sk_test_" + "a" * 16,
    "openai --key sk-" + "a" * 32,
    "curl 'https://maps.googleapis.com/x?key=AIza" + "a" * 35 + "'",
    "echo '-----BEGIN OPENSSH PRIVATE KEY-----'",
    "echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
]


class TestTheSharedRules(unittest.TestCase):
    """Everything the hook catches, the importer must catch too."""

    def test_every_credential_shape_is_caught(self) -> None:
        missed = [c for c in SECRET_SHAPES if not credentials.looks_like_credential(c)]
        self.assertEqual(missed, [], f"{len(missed)} shapes the bash hook drops would be imported")

    def test_no_ordinary_command_is_caught(self) -> None:
        """The direction that loses data silently, so it is the one to over-test."""
        eaten = [c for c in INNOCENT_SHAPES if credentials.looks_like_credential(c)]
        self.assertEqual(eaten, [], f"{len(eaten)} ordinary commands would be dropped on import")

    def test_closed_on_import_names_real_documented_gaps(self) -> None:
        """These are literals retyped from the shared corpus; keep them paired.

        Rewording a gap example over there would otherwise silently unpair it,
        and the failure would surface in a different test as a puzzle.
        """
        self.assertLessEqual(CLOSED_ON_IMPORT, set(DOCUMENTED_GAPS))

    def test_the_corpus_is_not_empty(self) -> None:
        """Guard the guard: an empty import makes both assertions above vacuous."""
        self.assertGreater(len(SECRET_SHAPES), 20)
        self.assertGreater(len(INNOCENT_SHAPES), 20)


class TestWhereItGoesFurtherThanTheHook(unittest.TestCase):
    def test_it_closes_the_gaps_it_can_afford_to(self) -> None:
        """A secret with no tell is findable when you can pay for the lookup."""
        for command in sorted(CLOSED_ON_IMPORT):
            with self.subTest(command=command):
                self.assertTrue(credentials.looks_like_credential(command))

    def test_the_remaining_gaps_are_still_gaps_and_still_documented(self) -> None:
        """Whatever import cannot close stays true of both implementations."""
        for command in DOCUMENTED_GAPS:
            if command in CLOSED_ON_IMPORT:
                continue
            with self.subTest(command=command):
                self.assertFalse(
                    credentials.looks_like_credential(command),
                    "docs/shell-integration.md says this is recorded; update it in this commit",
                )

    def test_known_token_formats_are_recognised(self) -> None:
        for token in TOKEN_SHAPES:
            with self.subTest(token=token):
                self.assertTrue(credentials.looks_like_credential(token))

    def test_tools_that_take_a_secret_positionally_are_recognised(self) -> None:
        for command in IMPORT_ONLY_SHAPES:
            with self.subTest(command=command):
                self.assertTrue(credentials.looks_like_credential(command))

    def test_every_import_only_rule_earns_its_place(self) -> None:
        """Drop each rule in turn; some corpus line must stop being caught.

        A rule nothing exercises is one a refactor can delete with CI green,
        while `docs/shell-integration.md` goes on promising it.
        """
        corpus = SECRET_SHAPES + IMPORT_ONLY_SHAPES + TOKEN_SHAPES
        for group in (credentials._IMPORT_ONLY, credentials._TOKENS, credentials._SHARED):
            for rule in group:
                without = re.compile(
                    "|".join(
                        r
                        for r in credentials._SHARED
                        + credentials._IMPORT_ONLY
                        + credentials._TOKENS
                        if r != rule
                    )
                )
                with self.subTest(rule=rule):
                    self.assertTrue(
                        any(
                            credentials.looks_like_credential(c) and not without.search(c)
                            for c in corpus
                        ),
                        "no corpus line depends on this rule",
                    )

    def test_high_entropy_strings_that_are_not_secrets_survive(self) -> None:
        """Why there is no entropy heuristic: shell history is full of these.

        Dropping one of these would be silent and unrecoverable, and a user
        would have no way to tell it from a command they misremembered typing.
        """
        for command in (
            "git checkout 3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a",
            "git show 0163e08",
            "sha256sum -c 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            "docker pull ubuntu@sha256:82becede498899ec668628e7cb0ad87b"
            "6e1c371cb8a1e597d83a47fac21d6af3",
            "curl -o f https://example.com/pkg?token=abc",  # lower-case, and a URL query
            "uuidgen 550e8400-e29b-41d4-a716-446655440000",
            "base64 -d <<< aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBzZWNyZXQ=",
        ):
            with self.subTest(command=command):
                self.assertFalse(
                    credentials.looks_like_credential(command),
                    "an entropy-shaped but innocent command would be dropped",
                )


class TestTheUsersOwnPattern(unittest.TestCase):
    def test_an_extra_pattern_is_applied(self) -> None:
        with mock.patch.dict(os.environ, {"WOSWOAR_IGNORE_EXTRA": "deploy-to-prod"}):
            extra = credentials.user_pattern()
        assert extra is not None
        self.assertTrue(credentials.looks_like_credential("deploy-to-prod now", extra))
        self.assertFalse(credentials.looks_like_credential("deploy-to-prod now"))

    def test_no_extra_pattern_is_the_normal_case(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(credentials.user_pattern())

    def test_woswoar_ignore_itself_is_never_read(self) -> None:
        """It is a POSIX ERE. `re` cannot compile `[[:space:]]`, and reading it
        would make every import complain about a variable nobody set."""
        with mock.patch.dict(os.environ, {"WOSWOAR_IGNORE": "(x|y)[[:space:]]z"}, clear=True):
            self.assertIsNone(credentials.user_pattern())

    def test_an_uncompilable_extra_pattern_is_refused_not_ignored(self) -> None:
        """Silently dropping it would leave the user believing it was applied."""
        with (
            mock.patch.dict(os.environ, {"WOSWOAR_IGNORE_EXTRA": "([unclosed"}),
            self.assertRaises(credentials.BadExtraPattern),
        ):
            credentials.user_pattern()


HOOK = Path(__file__).resolve().parent.parent / "woswoar" / "shell" / "woswoar.bash"


def as_posix_ere(pattern: str) -> str:
    r"""Rewrite one Python pattern into the POSIX ERE dialect bash `[[ =~ ]]` uses.

    Only the two constructs `_SHARED` actually uses need translating: ERE has no
    non-capturing group, and no `\s`. Inside a bracket expression the class is
    spelled `[:space:]`; outside one it needs its own brackets.
    """
    pattern = pattern.replace("(?:", "(")
    pattern = re.sub(r"\[([^]]*?)\\s([^]]*?)\]", r"[\1[:space:]\2]", pattern)
    return pattern.replace(r"\s", "[[:space:]]")


class TestParityWithTheHook(unittest.TestCase):
    """The rules exist twice, so pin them to each other and not just to examples.

    A corpus catches drift only where an example happens to cover it: add an
    alternative to the hook for a user-reported command, give it its own test,
    and the Python side stays silently behind -- on the path issue #52 argues
    matters more. This compares the two sources directly, which is what
    `TestEscapeParity` and `TestConstantParity` already do for the hook's other
    duplicated logic.
    """

    def hook_pattern(self) -> str:
        source = HOOK.read_text(encoding="utf-8")
        match = re.search(r'^: "\$\{WOSWOAR_IGNORE=(.*)\}"$', source, re.MULTILINE)
        assert match is not None, "the hook's default WOSWOAR_IGNORE was not found"
        # `\$` is the bash escaping of the regex anchor, not part of the pattern.
        return match.group(1).replace("\\$", "$")

    def test_the_hook_ships_exactly_the_shared_rules(self) -> None:
        self.assertEqual(
            self.hook_pattern(),
            "|".join(as_posix_ere(rule) for rule in credentials._SHARED),
            "woswoar/shell/woswoar.bash and credentials._SHARED have diverged; "
            "a rule added to one must be added to the other",
        )

    def test_the_import_only_rules_are_deliberately_not_in_the_hook(self) -> None:
        """They are the ones the prompt path cannot afford; that is the design."""
        hook = self.hook_pattern()
        for rule in credentials._IMPORT_ONLY + credentials._TOKENS:
            with self.subTest(rule=rule):
                self.assertNotIn(as_posix_ere(rule), hook)


class TestTheRulesAreReadable(unittest.TestCase):
    def test_every_pattern_compiles(self) -> None:
        """A typo in one alternative would otherwise disable it silently."""
        for pattern in credentials._SHARED + credentials._IMPORT_ONLY + credentials._TOKENS:
            with self.subTest(pattern=pattern):
                re.compile(pattern)


if __name__ == "__main__":
    unittest.main()
