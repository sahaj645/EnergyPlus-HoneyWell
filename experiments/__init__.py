"""Evaluation harness: A/B runs, endurance runs, KPI extraction.

The claim this project has to defend is "the agent beats the baseline without breaking
comfort". These modules are how that claim gets measured rather than asserted: identical
weather, identical model, one arm with the agent and one without.
"""

__all__ = ["ab_harness", "endurance", "kpi_extract"]
