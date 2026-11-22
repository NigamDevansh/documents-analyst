"""Suite-wide guarantees. Chiefly: the tests cannot reach a paid API.

`docs/TESTING.md` §1 states one rule — *the whole suite runs with no API keys
and no network*.

The hole is subtle. `Settings` reads `.env`, so a developer with a working key
would get a **live** LLM client inside a unit test. Nothing fails; the suite just
quietly starts spending real money. On CI, with no `.env`, it would pass — so
the failure only exists on the machine of the person least likely to look for it.

Clearing the variable here makes the offline guarantee a property of the test
run rather than a habit of whoever wrote the last test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_outbound_credentials(request: pytest.FixtureRequest) -> Iterator[None]:
    """Blank every credential for the duration of the suite.

    Set to `""` rather than deleted, and that detail is the whole fixture.
    Deleting the variable leaves `Settings` free to read the key out of `.env`,
    because a dotenv value is a *fallback* for a variable that is absent — an
    empty environment variable is not absent, so it wins.

    Blanked rather than replaced with a plausible fake: code that degrades on a
    missing key is code we want exercised, and a fake key would reach the
    network and come back 401 — a different path, tested by accident.

    `live`-marked tests are the exception; they are skipped by default and read
    their key from the environment they were launched with.
    """
    if request.node.get_closest_marker("live") and os.environ.get("RUN_LIVE_LLM") == "1":
        yield
        return
    saved = {
        name: os.environ.get(name)
        for name in (
            "GEMINI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        )
    }
    for name in saved:
        os.environ[name] = ""
    from documents_analyst.settings import get_settings

    get_settings.cache_clear()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int) -> None:
    """Say plainly when the database-backed half did not run.

    **A silent skip is the failure mode this whole suite exists to avoid.**
    Without `DATABASE_URL` the crash-and-resume, two-workers-one-pile and
    injection-reporting tests skip — roughly a third of the suite — and pytest
    ends on a green line that reads exactly like a full pass. Anyone checking
    this system's claims would have checked the half that needs no database.

    Both documented commands (`make test`, `make test-local`) supply the URL, so
    this is for the reviewer who runs `pytest` directly.
    """
    if os.environ.get("DATABASE_URL"):
        return
    skipped = len(terminalreporter.stats.get("skipped", []))
    if not skipped:
        return
    terminalreporter.write_sep("=", "database-backed tests did not run", red=True)
    terminalreporter.write_line(
        f"{skipped} tests skipped: DATABASE_URL is not set, so nothing that needs Postgres ran — "
        "crash-and-resume, concurrent workers, epoch fencing and the graph end to end."
    )
    terminalreporter.write_line("Run the whole suite with:  make test-local   (or: make test)")
