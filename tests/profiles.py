"""Every Hypothesis profile, in one place, selected by one environment variable.

Ported from `martinus/tupferl` (Apache-2.0), where the reason was learned the
expensive way: without a `mutation` profile, every mutant in a sweep pays the
full example budget for no extra signal. The other half of the reason is less
obvious -- a mutation run compares a *baseline* against a mutant, so the two must
draw the same examples. A randomised profile makes "the baseline passed and the
mutant failed" mean "they were given different inputs" some of the time, and
that is indistinguishable from a catch. So both non-interactive profiles are
derandomised.

Import this module for its side effect and then use `settings` normally:

    from tests import profiles  # noqa: F401  -- registers and loads the profile

One module rather than a `register_profile` call at the top of each property
test, and here that is more than tidiness. The per-module profiles this replaces
-- `woswoar` at 400 examples, `woswoar-export` and `woswoar-merge` at 300 --
never actually applied per module: `load_profile` sets one process-wide default,
resolved when a test *runs*, so under `discover` every property test ran at
whichever profile the last-imported module happened to load. One budget, stated
once, is the same behaviour with the pretence removed.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

#: Which profile to use. Named for the project so it cannot collide with another
#: tool's variable in a shell where both are set.
ENV = "WOSWOAR_HYPOTHESIS_PROFILE"

#: The default. Randomised, because on a developer's machine finding a new
#: falsifying example is the whole point of running these at all.
#:
#: 400 examples rather than Hypothesis's 100 because `test_record_properties`
#: measured the difference: at 100 the unescaped `session` field survived, and
#: it only fell out on the longer search.
#:
#: `derandomize=False` is stated rather than left out, and that is not
#: redundancy. **Hypothesis registers and loads a profile of its own when it
#: sees `CI` in the environment**, and that one is derandomised -- so a field a
#: profile here does not state is inherited from whatever is default at
#: registration time. Left implicit, `dev` is randomised on a laptop and
#: derandomised on a runner: the same name for two different things, decided by
#: an environment variable nobody here mentions. tupferl's CI caught exactly
#: that on the first run.
settings.register_profile("dev", max_examples=400, deadline=None, derandomize=False)

#: CI: derandomised, so a failure a contributor is asked to reproduce reproduces.
#: The name collides with the profile Hypothesis registers for itself when `CI`
#: is set, deliberately: registering it again replaces theirs, and the
#: `load_profile` at the bottom of this module is what decides which one is in
#: force either way.
#: `deadline=None` on every profile, and this is not laziness -- these properties
#: drive real `age` and `git` subprocesses, so a per-example deadline measures
#: the runner's load rather than the code.
settings.register_profile("ci", max_examples=400, deadline=None, derandomize=True)

#: Under `tools/mutate.py`. Few examples and no health checks: a mutant that
#: makes generation slow would otherwise fail the `too_slow` health check, which
#: reports as an *error* rather than as a test noticing the mutation -- the
#: harness would call that `BROKE` and the row would go unanswered.
settings.register_profile(
    "mutation",
    max_examples=20,
    deadline=None,
    derandomize=True,
    suppress_health_check=list(HealthCheck),
)

_CHOSEN = os.environ.get(ENV, "dev")

#: ``(max_examples, stateful_step_count)`` for the two stateful machines. Not
#: `max_examples` on the profile, because one stateful example is a run of the
#: whole machine -- real `age`, real stores -- where one example of an ordinary
#: property is a function call. A profile budget that suited one would make the
#: other either useless or unbearable.
#:
#: Two pairs rather than tupferl's one because the two machines here were tuned
#: separately before this module existed, and this module's job is to add the
#: `mutation` row, not to re-tune them. Keyed by the same variable that picks
#: the profile, so the two cannot disagree about which run this is. `mutation`
#: gets three examples: a sweep runs the suite once per mutant, and a machine
#: that took its full budget would put a sweep into hours for signal the
#: example tests already carry.
EXPORT_MACHINE = {"mutation": (3, 4)}.get(_CHOSEN, (200, 20))
IMPORT_MACHINE = {"mutation": (3, 4)}.get(_CHOSEN, (250, 15))

#: Every profile above states every field it cares about, so that this line is
#: the only thing that decides which settings are in force.
#: `tests/test_profiles.py` asserts that by running this module in a subprocess
#: with and without `CI`.
settings.load_profile(_CHOSEN)
