"""Unit coverage for migration-history safety independent of Postgres."""

from __future__ import annotations

from pathlib import Path

import pytest

from documents_analyst.db import migrate


@pytest.mark.parametrize("status_only", [False, True])
def test_checksum_mismatch_fails_before_any_migration_is_applied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_only: bool,
) -> None:
    migration = tmp_path / "001_example.sql"
    migration.write_text("SELECT 1;")

    monkeypatch.setattr(migrate, "discover", lambda: [migration])
    monkeypatch.setattr(migrate, "applied", lambda _engine: {migration.stem: "not-the-real-hash"})
    monkeypatch.setattr(migrate, "apply_one", lambda _engine, _path: pytest.fail("must not apply"))

    assert migrate._run(object(), status_only=status_only) == 2  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert "CHECKSUM MISMATCH" in captured.err
    # `--status` still has to report. A diagnostic that goes silent exactly when
    # there is something to diagnose is worse than no diagnostic at all.
    if status_only:
        assert "DRIFTED" in captured.out
        assert "1 drifted" in captured.out
