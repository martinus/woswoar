"""How many CPUs this process may actually use.

One function, in its own module, because three tools that size a pool need it
and a copy in each is how the answers drift. By the time this module was made
(following `martinus/tupferl`, which extracted it first for the same reason),
the two copies here had already diverged -- `run_tests` fell back to 2 and the
`sandbox` helper to 4 -- and neither number was chosen against the other.

What each caller does with the answer is *not* shared, because the workloads
differ and the difference is the interesting part:

- `run_tests` doubles it. Its batches spend most of their wall clock waiting on
  subprocesses, so more workers than cores is a win (measured on four cores:
  jobs=8 beats jobs=4 by ~9%, and jobs=16 regresses).
- `mutate` caps it at 16 and shrinks it further by available memory, because a
  mutation lane hosts a whole suite rather than a fraction of one.
"""

from __future__ import annotations

import os


def usable_cpus() -> int:
    """Not `os.cpu_count()`, which answers about the *host*.

    `os.cpu_count()` ignores an affinity mask and a container's quota, so on a
    two-core-limited runner on a sixteen-core host it answers sixteen -- and a
    pool sized from that number spends its time context-switching. CI runners
    are exactly that shape, which is where it matters most.

    `os.process_cpu_count` is 3.13+; this project supports 3.10, so the lookup
    is by name rather than a version check. The fallback of 4 is a guess for the
    case where the interpreter will not say at all, and it is deliberately not 1:
    a wrong small answer serialises the suite silently, where a wrong large one
    is merely slower.
    """
    counter = getattr(os, "process_cpu_count", os.cpu_count)
    return counter() or 4
