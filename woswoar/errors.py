"""The one exception type the CLI has to know about.

Deliberately its own module with no imports: ``__main__`` needs to catch these
at top level, and importing ``sync`` or ``crypto`` just to name an exception
would put ~6 ms of ``dataclasses`` and ``subprocess`` on the Ctrl-R path, where
neither module is otherwise used.
"""

from __future__ import annotations


class WoswoarError(RuntimeError):
    """An expected failure with a message worth showing the user as-is.

    Missing age, an unreadable identity, a repo that was never initialised: all
    actionable, none worth a traceback. Sync in particular runs unattended from
    a timer, where nobody ever reads one.
    """
