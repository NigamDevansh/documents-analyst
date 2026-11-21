"""documents_analyst_core — the shared library.

Two zones, and the boundary between them is the whole point:

  portable/          zero internal imports; liftable into another project as-is
  everything else    free to import within core; used by the worker only

Nothing here imports FastAPI, SQLAlchemy or LangGraph. This is a library of
capabilities, not application wiring.

See docs/CORE_INTERNALS.md
"""

__version__ = "0.1.0"
