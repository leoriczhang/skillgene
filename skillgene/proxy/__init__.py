"""Client-side proxy package.

Exposes :class:`ProxyServer` plus a small set of private helpers that the
test suite imports directly for unit coverage.
"""

from __future__ import annotations

from .attribution import (
    _extract_modified_skills_from_tool_calls,
    _extract_read_skills_from_tool_calls,
)
from .server import ProxyServer
from .session import _classify_raw_turn_kind, _is_user_turn_boundary

__all__ = [
    "ProxyServer",
    "_classify_raw_turn_kind",
    "_is_user_turn_boundary",
    "_extract_read_skills_from_tool_calls",
    "_extract_modified_skills_from_tool_calls",
]
