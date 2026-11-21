"""Fact-key embeddings.

Chunks used to be embedded here too. Nothing ever read those vectors — the
extractor works per section and there was no retrieval query — so writing them
was a paid round trip per document for a column with no consumer. Removed in
the schema; `document_chunks` still exists, because its offsets are what a
citation resolves against.
"""
