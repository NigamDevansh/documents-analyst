"""The product mark exists in two places; they must not drift apart."""

from pathlib import Path

import pytest

from documents_analyst.api import brand


def test_the_api_and_the_dashboard_share_one_mark() -> None:
    """The status page carries a copy of the tab icon; if the dashboard's mark is
    redrawn and this copy is not, the two tabs would show different logos.

    Skipped inside the runtime image, which ships only the built dashboard; CI
    mounts the frontend sources, so the check still runs there."""
    dashboard = Path(__file__).parents[1] / "frontend/src/assets/icon.svg"
    if not dashboard.is_file():
        pytest.skip("frontend sources are not in the runtime image")
    assert dashboard.read_text().strip() == brand.ICON_SVG
