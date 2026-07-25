"""Streamlit dashboard.

Strictly a **read-only** view: it opens the SQLite file with ``PRAGMA query_only=ON`` and has
no path to an actuator. Nothing here may import the guardian's write side or the plan cache.
Run it with ``streamlit run dashboard/app.py``.
"""

__all__ = ["app"]
