"""Prices are reporting, never a gate: warnings for missing or stale rates, nothing more."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from documents_analyst.pricing import price_warnings


def settings(path: Path, *, embedding_key: str = "set") -> Any:
    return SimpleNamespace(
        model_prices_path=path,
        resolved_model_extract="extract-model",
        resolved_model_judge="judge-model",
        embedding_api_key=embedding_key,
        resolved_embedding_model="embed-model",
    )


def write(tmp_path: Path, table: dict[str, Any]) -> Path:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(table))
    return path


def test_current_prices_raise_no_warning(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {
            "extract-model": {"input": "1", "output": "1", "valid_until": "2027-06-30"},
            "judge-model": {"input": "1", "output": "1"},
            "embed-model": {"input": "1", "output": "0"},
        },
    )
    assert price_warnings(settings(path), today=date(2026, 11, 22)) == []


def test_missing_expiring_and_expired_prices_are_each_named(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {
            "extract-model": {"input": "1", "output": "1", "valid_until": "2026-12-01"},
            "judge-model": {"input": "1", "output": "1", "valid_until": "2026-11-01"},
        },
    )
    warnings = price_warnings(settings(path), today=date(2026, 11, 22))
    assert [(w["model"], w["state"]) for w in warnings] == [
        ("extract-model", "expiring"),
        ("judge-model", "expired"),
        ("embed-model", "missing"),
    ]
    assert all("config/model_prices.json" in w["message"] for w in warnings)


def test_embedding_model_is_only_checked_when_embeddings_are_configured(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {
            "extract-model": {"input": "1", "output": "1"},
            "judge-model": {"input": "1", "output": "1"},
        },
    )
    assert price_warnings(settings(path, embedding_key=""), today=date(2026, 11, 22)) == []


def test_the_shipped_gemini_price_warns_before_it_expires_and_never_blocks() -> None:
    """The shipped table's Gemini entry carries a `valid_until`; past it, the worker
    keeps running and only the report is flagged."""
    shipped = Path(__file__).resolve().parents[1] / "config" / "model_prices.json"
    table = json.loads(shipped.read_text())
    gemini = next(m for m, rate in table.items() if rate.get("valid_until"))
    s = settings(shipped)
    s.resolved_model_extract = s.resolved_model_judge = gemini
    s.embedding_api_key = ""
    expires = date.fromisoformat(table[gemini]["valid_until"])
    after = date(expires.year + 1, 1, 15)
    assert [w["state"] for w in price_warnings(s, today=after)] == ["expired"]
