"""Planning layer: build a digest, ask a local model for a :class:`common.models.Plan`.

The planner runs on its own cadence, **never** inside the EnergyPlus callback (CLAUDE.md,
rule R1). It produces plans; it does not actuate. Everything it emits is untrusted until the
guardian has ruled on it.
"""

__all__ = ["digest", "ollama_client", "plan_cache", "prompts"]
