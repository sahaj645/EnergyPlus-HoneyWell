"""The LLM planner: a digest in, a validated :class:`~common.models.Plan` out (or ``None``).

Ollama, local, constrained decoding. The plan schema is handed to Ollama as ``format`` (a JSON
Schema grammar), so the model is *decoded into* the shape rather than merely asked for it. On
top of that:

* **Static-first prompt.** System prompt (a byte-identical constant) leads every call and the
  digest trails it, so the prompt-prefix cache and ``keep_alive`` keep the model warm between
  cycles. ``temperature=0`` makes a given digest map to a stable plan.
* **L1 auto-repair, one shot.** If the returned JSON fails Pydantic validation, the planner
  retries exactly once, appending the validator's own error message, then gives up and returns
  ``None`` - at which point the guardian simply keeps the baseline. One retry catches the
  occasional schema slip without turning a wedged model into an unbounded loop.
* **Every call is logged** to ``llm_calls`` (tokens in/out, latency, retries, outcome), because
  when a plan does not appear the first question is always "did the model answer at all?".

``ollama`` is imported lazily inside :meth:`Planner.plan`, so importing this module costs
nothing and works on a machine with no model runtime (CI, the dashboard). Nothing here runs on
the EnergyPlus callback thread - the scheduler calls it from a worker (rule R1).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from agent.prompts import SYSTEM_PROMPT
from common.log import get_logger
from common.models import Plan, TriggerEnum
from common.store import TelemetryStore

log = get_logger("agent.planner")

# Computed once at import: the constrained-decoding grammar. Stable across calls.
PLAN_SCHEMA = Plan.model_json_schema()


class PlannerUnavailableError(RuntimeError):
    """Ollama was unreachable, timed out, or returned nothing usable."""


@dataclass
class PlanAttempt:
    """Outcome of one planning cycle, for logging and the smoke summary."""

    plan: Plan | None
    retries: int
    latency_ms: float
    ok: bool
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class Planner:
    """Turns a digest into a validated :class:`~common.models.Plan`."""

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

    def health(self) -> bool:
        """True if the server answers and the model is available. Never raises."""
        try:
            client = self._client()
            names = {m.get("model") or m.get("name") for m in client.list().get("models", [])}
            return self.model in names or any(str(n).startswith(self.model) for n in names if n)
        except Exception as exc:  # noqa: BLE001 - health is best-effort
            log.warning("planner health check failed: %s", exc)
            return False

    def plan(
        self,
        digest: str,
        *,
        now: datetime,
        trigger: TriggerEnum = TriggerEnum.HOURLY,
    ) -> Plan | None:
        """Generate one plan for ``digest``. Returns a validated ``Plan`` or ``None``.

        ``now`` is the current *simulation* time; the returned plan is stamped with it so its
        action windows are anchored in sim time, not the wall clock.
        """
        attempt = self._generate(digest)
        if self.store is not None:
            self.store.write_llm_call(
                model=self.model,
                latency_ms=attempt.latency_ms,
                ok=attempt.ok and attempt.plan is not None,
                error=attempt.error,
                prompt=digest,
                response=attempt.plan.model_dump_json() if attempt.plan else attempt.error,
                run_id=self.run_id,
                at=now,
            )
        if attempt.plan is None:
            return None
        # Anchor the plan in sim time and record provenance, whatever the model emitted.
        return attempt.plan.model_copy(
            update={"created_at": now, "trigger": trigger, "planner_model": self.model}
        )

    # -- internals ---------------------------------------------------------------------

    def _generate(self, digest: str) -> PlanAttempt:
        """Call the model, validating into a Plan with exactly one repair retry."""
        try:
            client = self._client()
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed attempt, not a crash
            return PlanAttempt(
                plan=None, retries=0, latency_ms=0.0, ok=False, error=f"client: {exc}"
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": digest},
        ]

        start = time.monotonic()
        raw = ""
        prompt_tokens = completion_tokens = None
        for retry in range(2):  # attempt 0 + one repair
            try:
                response = client.chat(
                    model=self.model,
                    messages=messages,
                    format=PLAN_SCHEMA,
                    options={"temperature": 0},
                    keep_alive=self.keep_alive,
                )
            except Exception as exc:  # noqa: BLE001 - network/timeouts -> failed attempt
                latency = (time.monotonic() - start) * 1000
                return PlanAttempt(
                    plan=None, retries=retry, latency_ms=latency, ok=False, error=f"chat: {exc}"
                )

            raw = response.get("message", {}).get("content", "") or ""
            prompt_tokens = response.get("prompt_eval_count", prompt_tokens)
            completion_tokens = response.get("eval_count", completion_tokens)

            try:
                plan = Plan.model_validate_json(raw)
            except ValidationError as exc:
                if retry == 0:
                    # L1 repair: hand the model its own validator error and try once more.
                    messages.append({"role": "assistant", "content": raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "That did not validate against the Plan schema. Fix exactly "
                                f"these errors and resend the JSON Plan only:\n{exc}"
                            ),
                        }
                    )
                    log.warning("plan failed validation; retrying once")
                    continue
                latency = (time.monotonic() - start) * 1000
                return PlanAttempt(
                    plan=None,
                    retries=retry,
                    latency_ms=latency,
                    ok=True,  # the model answered; it just could not be made valid
                    error=f"validation: {exc}",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )

            latency = (time.monotonic() - start) * 1000
            return PlanAttempt(
                plan=plan,
                retries=retry,
                latency_ms=latency,
                ok=True,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        # Unreachable (the loop always returns), but keeps the type checker honest.
        latency = (time.monotonic() - start) * 1000
        return PlanAttempt(plan=None, retries=1, latency_ms=latency, ok=True, error="exhausted")


__all__ = ["PLAN_SCHEMA", "PlanAttempt", "Planner", "PlannerUnavailableError"]
