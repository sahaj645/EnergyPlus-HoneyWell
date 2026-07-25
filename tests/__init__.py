"""Test suite.

Two standing constraints:

* Nothing here may require an EnergyPlus install - CI has none.
* Tests import plan types from :mod:`common.models`. They never define their own
  plan-shaped fixtures (CLAUDE.md, rule R4): a test that redefines the schema will keep
  passing after the schema breaks.
"""
