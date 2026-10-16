from __future__ import annotations

from typing import Any

import pytest
from falkordb import FalkorDB

from provenance.config import settings


_GRAPH_REACHABLE: bool | None = None
_GRAPH_SKIP_MESSAGE = (
    f"FalkorDB not reachable on {settings.FALKORDB_HOST}:{settings.FALKORDB_PORT} "
    "- run docker compose up -d"
)
_GRAPH_METHODS = {"FalkorDB", "load_graph", "select_graph", "ro_query", "query"}


def _needs_falkordb(item: Any) -> bool:
    if item.get_closest_marker("requires_falkordb") is not None:
        return True
    if any(
        name == "graph" or name.endswith("_graph")
        for name in item.fixturenames
    ):
        return True
    function = getattr(item, "function", None)
    return bool(function and _GRAPH_METHODS.intersection(function.__code__.co_names))


def _falkordb_reachable() -> bool:
    try:
        database = FalkorDB(
            host=settings.FALKORDB_HOST,
            port=settings.FALKORDB_PORT,
        )
        return bool(database.connection.ping())
    except Exception:
        return False


def pytest_runtest_setup(item: Any) -> None:
    global _GRAPH_REACHABLE
    if not _needs_falkordb(item):
        return
    if _GRAPH_REACHABLE is None:
        _GRAPH_REACHABLE = _falkordb_reachable()
    if not _GRAPH_REACHABLE:
        pytest.skip(_GRAPH_SKIP_MESSAGE)
