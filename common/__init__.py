"""Shared contracts for HIVE.

Everything in this package is imported by more than one other package. The plan schema in
:mod:`common.models` is the single source of truth (see CLAUDE.md, rule R4) - planner,
guardian, MCP tool surface, journal and dashboard all import it and none of them redefine it.
"""

__all__ = ["config", "eplus_path", "log", "models", "store"]
