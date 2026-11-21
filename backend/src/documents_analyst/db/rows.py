"""Frozen dataclasses returned by repos.

Not Pydantic: these never cross a boundary, and validating them twice buys
nothing.
"""

from __future__ import annotations
