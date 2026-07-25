"""MCP tool surface exposed to the planner.

The planner reasons *through tools* rather than through bespoke function calls, which keeps
the observation boundary explicit and inspectable: every question the model asked and every
answer it got is a logged tool call.

Note the asymmetry - ``submit_plan`` does not actuate. It hands the plan to the guardian and
returns the verdict. There is no tool that writes an actuator (CLAUDE.md, rule R2).
"""

__all__ = ["server", "tools"]
