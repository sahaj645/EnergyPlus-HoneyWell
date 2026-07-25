"""Deterministic safety layer.

Nothing in this package may call an LLM, sleep, or perform I/O beyond journalling. It is
plain, auditable, testable Python: given a plan and a state, it returns the same verdict every
time.

It is also the *only* producer of :class:`common.models.ApprovedPlan`, which is the only type
the actuator accepts (CLAUDE.md, rule R2). There is no bypass path, and none may be added.
"""

__all__ = ["fallback", "limits", "supervisor", "watchdog"]
