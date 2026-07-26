"""L3 self-heal: turn a filtered EnergyPlus error log into a reviewable :class:`PatchSpec`.

This is :mod:`agent.planner`'s sibling for a different target: instead of digest-in / ``Plan``-
out, it is error-log-in / ``PatchSpec``-out. Structurally identical on purpose - static-first
prompt, ``PatchSpec.model_json_schema()`` as the constrained-decoding grammar, ``temperature=0``,
one-shot L1 schema repair, every call logged - because the shape that keeps a planner reliable
(schema, not prose) applies just as well to a repair.

``patch_model`` "has a blast radius the guardian does not cover" (CLAUDE.md): the guardian
reviews *plans*, not patches. This module does not close that gap - it produces a candidate
``PatchSpec`` and nothing more. Whether that candidate is safe to apply is
:func:`simulation.patching.apply_patch`'s job (validate-before-accept, never edit in place), and
whether the fix actually *worked* is the caller's job (re-run the chunk, roll back if the error
persists). ``experiments/self_heal_demo.py`` wires all three together.

``ollama`` is imported lazily, exactly like :mod:`agent.planner`, so this module costs nothing to
import on a machine with no model runtime.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pydantic import ValidationError

from common.log import get_logger
from common.models import PatchSpec, PreparedModel
from common.store import TelemetryStore

log = get_logger("agent.repair")

PATCH_SCHEMA = PatchSpec.model_json_schema()

REPAIR_SYSTEM_PROMPT = """\
You are the self-repair layer of HIVE, a building-energy control system. EnergyPlus just failed
to run its model and logged Severe/Fatal errors. You receive the filtered error log plus a list
of the model's known schedules, and you output ONE JSON PatchSpec that fixes the problem.

HARD RULES
- Output a single JSON PatchSpec object and nothing else. No prose, no markdown.
- You may only use three operations: set_field (change one field on an existing object),
  add_object (create a missing one), remove_object (delete one). Pick whichever actually
  addresses the error - if the log says a schedule cannot be found, the fix is almost always
  add_object: recreate a SCHEDULE:CONSTANT with that exact name and a sane Hourly_Value.
- `object_name` must match the exact name the error log gives - a close guess still leaves the
  reference dangling.
- Prefer the smallest fix that removes the error. Do not touch objects the log does not mention.
- The patched model is re-parsed before it is accepted; a patch that does not produce a valid
  IDF is rejected outright and nothing changes, so an incomplete-but-parseable fix is still
  useless if the same error recurs on the next run - make sure the fix is actually complete.
"""


def build_repair_digest(error_lines: list[str], model: PreparedModel) -> str:
    """Render the filtered error log + known-schedule hints. Pure, deterministic, string-only.

    Mirrors :mod:`agent.digest`'s shape (fixed sections, terse) for the same reason: a small,
    predictable prompt is easier for a 7B model to act on than a full error log dump.
    """
    lines = ["ERROR LOG (filtered, most recent last):"]
    lines.extend(f"  {line}" for line in error_lines) if error_lines else lines.append("  (none)")
    lines.append("")
    lines.append("KNOWN SCHEDULES (name: baseline value):")
    if model.constant_schedules:
        for name, value in sorted(model.constant_schedules.items()):
            lines.append(f"  {name}: {value:.2f}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("TASK: emit a PatchSpec that restores a valid model so EnergyPlus runs clean.")
    return "\n".join(lines)


@dataclass
class RepairAttempt:
    """Outcome of one repair-planning cycle, mirroring :class:`agent.planner.PlanAttempt`."""

    patch: PatchSpec | None
    retries: int
    latency_ms: float
    ok: bool
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class RepairPlanner:
    """Turns a repair digest into a validated :class:`~common.models.PatchSpec`."""

    def __init__(
        self,
        *,
        model: str = "qwen2.5:7b-instruct-q4_K_M",
        host: str = "http://localhost:11434",
        timeout_s: float = 30.0,
        keep_alive: str = "30m",
        store: TelemetryStore | None = None,
        run_id: str = "",
    ) -> None:
        self.model = model
        self.host = host
        self.timeout_s = timeout_s
        self.keep_alive = keep_alive
        self.store = store
        self.run_id = run_id

    def _client(self):
        from ollama import Client

        return Client(host=self.host, timeout=self.timeout_s)

    def plan(self, digest: str) -> PatchSpec | None:
        """Generate one repair for ``digest``. Returns a validated ``PatchSpec`` or ``None``."""
        attempt = self._generate(digest)
        if self.store is not None:
            self.store.write_llm_call(
                model=f"{self.model}(repair)",
                latency_ms=attempt.latency_ms,
                ok=attempt.ok and attempt.patch is not None,
                error=attempt.error,
                prompt=digest,
                response=attempt.patch.model_dump_json() if attempt.patch else attempt.error,
                run_id=self.run_id,
                prompt_tokens=attempt.prompt_tokens,
                completion_tokens=attempt.completion_tokens,
                retries=attempt.retries,
            )
        return attempt.patch

    def _generate(self, digest: str) -> RepairAttempt:
        try:
            client = self._client()
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed attempt, not a crash
            return RepairAttempt(
                patch=None, retries=0, latency_ms=0.0, ok=False, error=f"client: {exc}"
            )

        messages = [
            {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": digest},
        ]

        start = time.monotonic()
        raw = ""
        prompt_tokens = completion_tokens = None
        for retry in range(2):  # attempt 0 + one repair, same L1 shape as agent.planner
            try:
                response = client.chat(
                    model=self.model,
                    messages=messages,
                    format=PATCH_SCHEMA,
                    options={"temperature": 0},
                    keep_alive=self.keep_alive,
                )
            except Exception as exc:  # noqa: BLE001 - network/timeouts -> failed attempt
                latency = (time.monotonic() - start) * 1000
                return RepairAttempt(
                    patch=None, retries=retry, latency_ms=latency, ok=False, error=f"chat: {exc}"
                )

            raw = response.get("message", {}).get("content", "") or ""
            prompt_tokens = response.get("prompt_eval_count", prompt_tokens)
            completion_tokens = response.get("eval_count", completion_tokens)

            try:
                patch = PatchSpec.model_validate_json(raw)
            except ValidationError as exc:
                if retry == 0:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "That did not validate against the PatchSpec schema. Fix "
                                f"exactly these errors and resend the JSON PatchSpec only:\n{exc}"
                            ),
                        }
                    )
                    log.warning("repair patch failed validation; retrying once")
                    continue
                latency = (time.monotonic() - start) * 1000
                return RepairAttempt(
                    patch=None,
                    retries=retry,
                    latency_ms=latency,
                    ok=True,
                    error=f"validation: {exc}",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )

            latency = (time.monotonic() - start) * 1000
            return RepairAttempt(
                patch=patch,
                retries=retry,
                latency_ms=latency,
                ok=True,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        latency = (time.monotonic() - start) * 1000
        return RepairAttempt(patch=None, retries=1, latency_ms=latency, ok=True, error="exhausted")


__all__ = [
    "PATCH_SCHEMA",
    "REPAIR_SYSTEM_PROMPT",
    "RepairAttempt",
    "RepairPlanner",
    "build_repair_digest",
]
