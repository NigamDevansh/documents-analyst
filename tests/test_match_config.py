"""Configuration-sensitive unit tests for the matching node."""

from __future__ import annotations

from documents_analyst.graph.nodes.match import _by_subject


def test_open_subjects_below_configured_similarity_threshold_do_not_merge() -> None:
    """A weak vector match must not turn unrelated subjects into a conflict."""
    buckets = _by_subject(
        [
            {
                "subject_free": "data residency region",
                "fact_key_embedding": [1.0, 0.0],
            },
            {
                "subject_free": "audit rights",
                "fact_key_embedding": [0.5, 0.8660254],
            },
        ],
        threshold=0.86,
    )

    assert set(buckets) == {"~data residency region", "~audit rights"}
