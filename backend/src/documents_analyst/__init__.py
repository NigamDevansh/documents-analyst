"""Document Analyst — reads a pile of documents, reconciles them, writes the final document.

Four entrypoints over one package:

    uvicorn documents_analyst.api.main:create_app --factory     async, DB-only
    python -m documents_analyst.worker.main    sync, runs the graph and writes the final document
    python -m documents_analyst.mcp.server     an HTTP client of our own API
    python -m documents_analyst.db.migrate     one-shot

See docs/BACKEND_INTERNALS.md
"""

__version__ = "0.1.0"
