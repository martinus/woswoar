"""Recognise credential-shaped commands, for the paths that are not the hook.

Each shell hook under ``woswoar/shell/`` carries its own copy of these rules, as
one POSIX ERE. It has to: it runs on every prompt, must not fork, and cannot
call Python. ``tests/test_credentials.py`` holds every hook against
``_SHARED``, so a rule can never be added to one shell alone.

This module is for ``woswoar import``, which reads history recorded long before
woswoar existed -- years of it, filtered by nothing at all.

Two things are different here, both because an importer has no per-prompt
budget to spend:

- the rules are written as separate readable patterns with a comment each,
  rather than as one dense line tuned for compile time. They are still joined
  into a single compiled alternation, which measured no slower than trying
  them one at a time and keeps the call site simple.
- it also recognises **token formats**. `docs/shell-integration.md` lists "a
  secret with no tell" as something the hook cannot catch, because
  distinguishing `AKIAIOSFODNN7EXAMPLE` from any other argument costs more than
  a prompt can pay. An importer can pay it, so on that path the well-known ones
  stop being a gap.

`tests/credential_shapes.py` is the corpus both implementations are held to, so
the rules they share cannot drift apart.

Deliberately *not* here: an entropy test. Shell history is full of high-entropy
strings that are not secrets -- git SHAs, checksums, UUIDs, base64 payloads --
and dropping a command is silent and unrecoverable. Precision matters more than
recall for something that edits a user's history behind their back.
"""

from __future__ import annotations

import os
import re

from .errors import WoswoarError

#: Shapes the bash hook also catches. Kept in the same order as the alternatives
#: in `WOSWOAR_IGNORE` so the two can be read side by side.
_SHARED = (
    # An assignment whose name reads like a credential. KEY, PASS and AUTH only
    # after `_`, or MONKEY=, KEYS= and PASSAGE= would all be dropped.
    r"(?:TOKEN|SECRET|PASSW|CREDENTIAL|APIKEY|_KEY|_PASS|_AUTH)[A-Za-z0-9_]*=",
    # A long option that names one. The trailing boundary is what keeps
    # `--auth-mode` (a real azure flag, not a secret) out of it.
    r"--(?:[a-z]+-)*(?:(?:passw|token|secret|credential)[a-z-]*|api-?key|access-?key|auth)"
    r"(?:[=\s]|$)",
    r"--from-literal=",
    # Credentials inside a URL. Two shapes: a `user:pass@` prefix, and a token
    # that *is* a path component -- a webhook URL, where holding the address is
    # holding the credential. Anchored on the known hosts rather than on
    # "looks random", for the reason the module docstring gives.
    #
    # The whole host, not a path under it. Measured against real history, the
    # one that turned up was `hooks.slack.com/triggers/...`, not the
    # `/services/...` an incoming webhook uses -- and every path on that host
    # carries a trigger a holder can fire.
    r"://[^/\s]+:[^/@\s]+@",
    r"://(?:hooks\.slack\.com|discord(?:app)?\.com/api/webhooks)/\S",
    r"[Aa]uthorization:\s*[A-Za-z]",
    # The three tools that exist to take a password on the command line.
    r"(?:sshpass|htpasswd|openssl passwd)\s",
    # Short options that carry a secret value. `[^|;&]` bounds each to one
    # pipeline stage; `mysql -p` with no value attached is a prompt, not a
    # secret, which is why that one requires a following character.
    r"curl[^|;&]*\s-u\s",
    r"mysql[a-z]*[^|;&]*\s-p[^-\s]",
    r"docker login[^|;&]*\s-p",
    r"ssh-keygen[^|;&]*\s-N\s",
)

#: Secrets passed as a positional argument, or on a short flag of some specific
#: program. `docs/shell-integration.md` lists these as things the hook does not
#: catch, and they are the clearest example of why: matching them needs a list
#: of program names, and the hook pays for every character of it on every
#: prompt. An importer runs once, so it can carry the list.
_IMPORT_ONLY = (
    r"aws[^|;&]*\sconfigure\s+set\s+[a-z_.]*(?:secret|password|token|key)",
    r"vault\s+(?:kv\s+)?(?:put|write)\b[^|;&]*\s\S+=",
    r"redis-cli[^|;&]*\s-a\s*\S",
    r"(?:mongo(?:sh)?|az\s+login)[^|;&]*\s-p\s*\S",
    r"smbclient[^|;&]*\s-U\s*\S+%",
    r"pscp[^|;&]*\s-pw\s*\S",
)

#: Token formats distinctive enough to name on sight. Each is anchored on a
#: vendor prefix and a length, not on looking random, so a git SHA or a base64
#: blob does not trip them.
_TOKENS = (
    r"\bA[KS]IA[0-9A-Z]{16}\b",  # AWS access key id, permanent (AKIA) and temporary (ASIA)
    r"\bgh[pousr]_[A-Za-z0-9]{36,}",  # GitHub personal access token
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bglpat-[A-Za-z0-9_-]{20,}",  # GitLab
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",  # Slack
    r"\b[sprk]k_(?:live|test)_[A-Za-z0-9]{16,}",  # Stripe
    r"\bsk-[A-Za-z0-9]{32,}",  # OpenAI-style
    r"\bAIza[A-Za-z0-9_-]{35}",  # Google API key
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    # A JWT: three dot-separated base64url segments starting with the encoding
    # of `{"`. Two segments would match far too much.
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+",
)

#: Compiled on first use, not at import. `woswoar list` is re-run by fzf on
#: every scope switch and imports this module transitively through `importer`,
#: and compiling the alternation measured ~0.6ms -- paid on a keystroke path by
#: the one module whose docstring says it is not on one.
_PATTERN: re.Pattern[str] | None = None


def _pattern() -> re.Pattern[str]:
    global _PATTERN
    if _PATTERN is None:
        _PATTERN = re.compile("|".join(_SHARED + _IMPORT_ONLY + _TOKENS))
    return _PATTERN


#: Set by the user, so it is theirs to get right. Read once per import run --
#: `looks_like_credential` takes the compiled result rather than re-reading it.
_EXTRA = "WOSWOAR_IGNORE_EXTRA"


class BadExtraPattern(WoswoarError):
    """`WOSWOAR_IGNORE_EXTRA` is set to something Python cannot compile."""


def user_pattern() -> re.Pattern[str] | None:
    """The user's own additions, or None if they set none.

    Only `WOSWOAR_IGNORE_EXTRA` is read, never `WOSWOAR_IGNORE`. The default
    value of `WOSWOAR_IGNORE` is a POSIX ERE using `[[:space:]]`, which Python's
    `re` cannot compile at all -- so reading it would make every import warn
    about a variable the user never touched. `_EXTRA` is unset unless someone
    chose to set it.
    """
    raw = os.environ.get(_EXTRA)
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as exc:
        # Loudly. Silently ignoring it would leave the user believing their own
        # rule was applied to the import when it was not.
        raise BadExtraPattern(f"{_EXTRA} is not a valid Python regex: {exc}") from exc


def looks_like_credential(command: str, extra: re.Pattern[str] | None = None) -> bool:
    """Would publishing this command publish a secret with it?"""
    return bool(_pattern().search(command) or (extra and extra.search(command)))
