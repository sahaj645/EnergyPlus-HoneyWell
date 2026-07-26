"""Hypothesis profiles, registered once at collection time.

``dev`` (500 examples, no deadline) is the default - a thorough local run of the adversarial
guardian properties. CI selects the much smaller ``ci`` profile via the ``HIVE_HYPOTHESIS_PROFILE``
env var (set in ``.github/workflows/ci.yml``), so the property suite stays fast and non-flaky on
every push instead of spending CI minutes re-deriving what a 500-example local run already
confirmed. Deliberately no per-test ``@settings(max_examples=...)`` overrides anywhere in the
suite - that would silently defeat this cap, since an explicit decorator setting always wins
over the loaded profile.
"""

from __future__ import annotations

import os

from hypothesis import settings

settings.register_profile("dev", max_examples=500, deadline=None)
settings.register_profile("ci", max_examples=25, deadline=None)
settings.load_profile(os.environ.get("HIVE_HYPOTHESIS_PROFILE", "dev"))
