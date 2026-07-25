"""EnergyPlus assets and run scripts.

Model versioning convention: ``baseline.idf`` is never edited. Every change the agent makes
via ``patch_model`` is written as ``v1_<slug>.idf``, ``v2_<slug>.idf``, ... so any result can
be traced back to the exact model that produced it.

``pyenergyplus`` is not pip-installable - see :mod:`common.eplus_path`.
"""

__all__ = ["run_baseline"]
