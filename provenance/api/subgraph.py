from __future__ import annotations

import re
from typing import Any

from provenance.tools.queries import QUERY_REGISTRY


MAX_NODES = 150
MAX_EDGES = 300
_NODE_PROPS = ("login", "key", "name", "number", "title", "status", "kind", "ts")
_EDGE_PROPS = ("kind", "status", "ts")
_EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_SECRET_PATTERN = re.compile(
    r"(?i)(?:gsk_[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{12,})"
)


def _ids(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result = set()
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(result)


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        if _EMAIL_PATTERN.search(value) or _SECRET_PATTERN.search(value):
            return "[REDACTED]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _props(values: tuple[Any, ...], names: tuple[str, ...]) -> dict[str, Any]:
    return {
        name: _safe_value(value)
        for name, value in zip(names, values)
        if value is not None
    }


def export_subgraph(
    graph: Any,
    proof_bundle: dict[str, Any],
    *,
    max_nodes: int = MAX_NODES,
    max_edges: int = MAX_EDGES,
) -> dict[str, Any]:
    node_limit = max(0, min(int(max_nodes), MAX_NODES))
    edge_limit = max(0, min(int(max_edges), MAX_EDGES))
    requested_nodes = _ids(proof_bundle.get("node_ids", []))
    requested_edges = _ids(proof_bundle.get("edge_ids", []))
    truncated = len(requested_nodes) > node_limit or len(requested_edges) > edge_limit

    edge_rows: list[list[Any]] = []
    if requested_edges and edge_limit:
        edge_rows = graph.ro_query(
            QUERY_REGISTRY["api.subgraph.edges_by_id@1"],
            {"ids": requested_edges[: edge_limit + 1]},
        ).result_set
    if len(edge_rows) > edge_limit:
        truncated = True
        edge_rows = edge_rows[:edge_limit]

    endpoints = [int(row[index]) for row in edge_rows for index in (2, 3)]
    ordered_node_ids = list(dict.fromkeys([*endpoints, *requested_nodes]))
    if len(ordered_node_ids) > node_limit:
        truncated = True
    node_rows: list[list[Any]] = []
    if ordered_node_ids and node_limit:
        node_rows = graph.ro_query(
            QUERY_REGISTRY["api.subgraph.nodes_by_id@1"],
            {"ids": ordered_node_ids[: node_limit + 1]},
        ).result_set
    if len(node_rows) > node_limit:
        truncated = True
        node_rows = node_rows[:node_limit]

    nodes = []
    node_ids: set[int] = set()
    for row in node_rows:
        node_id = int(row[0])
        labels = row[1] if isinstance(row[1], (list, tuple)) else []
        label = str(labels[0]) if labels else "Unknown"
        props = _props(tuple(row[2:10]), _NODE_PROPS)
        display = next(
            (
                str(props[key])
                for key in ("login", "key", "name", "number", "title")
                if key in props
            ),
            str(node_id),
        )
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "key": props.get("key"),
                "display": display,
                "props": props,
            }
        )
        node_ids.add(node_id)

    edges = []
    for row in edge_rows:
        source_id, target_id = int(row[2]), int(row[3])
        if source_id not in node_ids or target_id not in node_ids:
            truncated = True
            continue
        edges.append(
            {
                "id": int(row[0]),
                "type": str(row[1]),
                "source": source_id,
                "target": target_id,
                "props": _props(tuple(row[4:7]), _EDGE_PROPS),
            }
        )
    if len(edges) > edge_limit:
        truncated = True
        edges = edges[:edge_limit]
    return {"nodes": nodes, "edges": edges, "truncated": truncated}