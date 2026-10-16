from __future__ import annotations

import re
from typing import Any

from provenance.tools.config import DEFAULT_TOOL_CONFIG
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
            identity = int(value)
        except (TypeError, ValueError):
            continue
        if identity >= 0:
            result.add(identity)
    return sorted(result)


def _ordered_ids(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    seen = set()
    for value in values:
        try:
            identity = int(value)
        except (TypeError, ValueError):
            continue
        if isinstance(value, bool) or identity < 0 or identity in seen:
            continue
        seen.add(identity)
        result.append(identity)
    return result


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
    priority_node_ids: list[int] | None = None,
    priority_edge_ids: list[int] | None = None,
    node_weights: dict[int, float] | None = None,
    edge_weights: dict[int, float] | None = None,
) -> dict[str, Any]:
    node_limit = max(0, min(int(max_nodes), MAX_NODES))
    edge_limit = max(0, min(int(max_edges), MAX_EDGES))
    requested_nodes = _ids(proof_bundle.get("node_ids", []))
    requested_edges = _ids(proof_bundle.get("edge_ids", []))
    requested_node_set = set(requested_nodes)
    requested_edge_set = set(requested_edges)
    priority_nodes = [
        node_id
        for node_id in _ordered_ids(priority_node_ids or [])
        if node_id in requested_node_set
    ]
    priority_edges = [
        edge_id
        for edge_id in _ordered_ids(priority_edge_ids or [])
        if edge_id in requested_edge_set
    ]
    priority_node_rank = {node_id: rank for rank, node_id in enumerate(priority_nodes)}
    priority_edge_rank = {edge_id: rank for rank, edge_id in enumerate(priority_edges)}
    node_weights = node_weights or {}
    edge_weights = edge_weights or {}

    def node_sort_key(node_id: int) -> tuple[Any, ...]:
        rank = priority_node_rank.get(node_id)
        if rank is not None:
            return (0, rank, -float(node_weights.get(node_id, 0.0)), node_id)
        return (
            1,
            -float(node_weights.get(node_id, 0.0)),
            node_id,
        )

    selected_node_ids = sorted(requested_nodes, key=node_sort_key)[:node_limit]
    selected_node_set = set(selected_node_ids)
    selected_node_rows: list[list[Any]] = []
    if selected_node_ids:
        selected_node_rows = graph.ro_query(
            QUERY_REGISTRY["api.subgraph.nodes_by_id@1"],
            {"ids": selected_node_ids},
        ).result_set

    def edge_id_sort_key(edge_id: int) -> tuple[Any, ...]:
        rank = priority_edge_rank.get(edge_id)
        if rank is not None:
            return (0, rank, -float(edge_weights.get(edge_id, 0.0)), edge_id)
        return (1, -float(edge_weights.get(edge_id, 0.0)), edge_id)

    selected_edge_ids = sorted(requested_edges, key=edge_id_sort_key)[:edge_limit]
    edge_rows: list[list[Any]] = []
    if selected_edge_ids:
        edge_rows = graph.ro_query(
            QUERY_REGISTRY["api.subgraph.edges_by_id@1"],
            {"ids": selected_edge_ids},
        ).result_set

    node_count_total = len(requested_nodes)
    edge_count_total = len(requested_edges)

    nodes = []
    for row in selected_node_rows:
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
    eligible_edge_rows = [
        row
        for row in edge_rows
        if int(row[2]) in selected_node_set and int(row[3]) in selected_node_set
    ]
    selected_edge_rows = sorted(
        eligible_edge_rows,
        key=lambda row: edge_id_sort_key(int(row[0])),
    )[:edge_limit]

    edges = []
    for row in selected_edge_rows:
        source_id, target_id = int(row[2]), int(row[3])
        edges.append(
            {
                "id": int(row[0]),
                "type": str(row[1]),
                "source": source_id,
                "target": target_id,
                "props": _props(tuple(row[4:7]), _EDGE_PROPS),
            }
        )
    node_count_returned = len(nodes)
    edge_count_returned = len(edges)
    truncated = (
        node_count_returned < node_count_total
        or edge_count_returned < edge_count_total
    )
    if truncated:
        note = (
            f"Showing {node_count_returned:,} of {node_count_total:,} evidence nodes and "
            f"{edge_count_returned:,} of {edge_count_total:,} evidence edges, "
            "prioritizing the claim subject and directly cited entities."
        )
    else:
        note = f"Showing all {node_count_total:,} evidence nodes and {edge_count_total:,} evidence edges."
    return {
        "nodes": nodes,
        "edges": edges,
        "node_count_total": node_count_total,
        "edge_count_total": edge_count_total,
        "node_count_returned": node_count_returned,
        "edge_count_returned": edge_count_returned,
        "truncated": truncated,
        "note": note,
    }


def _append_unique(target: list[Any], values: list[Any]) -> None:
    for value in values:
        if value is not None and value not in target:
            target.append(value)


def _claim_priority_ids(graph: Any, claim: Any) -> tuple[list[int], list[int], dict[int, float], dict[int, float]]:
    proof = claim.proof_bundle
    facts = [item["fact"] for item in proof.get("facts", [])]
    subject_module_keys: list[str] = []
    direct_module_keys: list[str] = []
    loss_root_keys: list[str] = []
    subject_logins: list[str] = []
    direct_logins: list[str] = []
    subject_pr_numbers: list[int] = []
    reviewer_person_ids: list[int] = []
    root_weights: dict[str, float] = {}
    dependent_weights: dict[str, float] = {}
    other_module_weights: dict[str, float] = {}
    dependent_edge_weights: dict[int, float] = {}
    reviewer_edge_priority: list[int] = []
    reviewer_support_pr_ids: list[int] = []
    reviewer_node_weights: dict[int, float] = {}

    for fact in facts:
        params = fact.params
        query_id = fact.query_id
        module_key = params.get("module_key")
        if isinstance(module_key, str) and query_id not in {
            "maintainer.dependents.depth1@1",
            "maintainer.dependents.depth2@1",
        }:
            _append_unique(subject_module_keys, [module_key])
        extract_module_key = params.get("extract_module_key")
        if isinstance(extract_module_key, str):
            _append_unique(subject_module_keys, [extract_module_key])
        for name in ("person_login", "removed_login"):
            login = params.get(name)
            if isinstance(login, str):
                _append_unique(subject_logins, [login.casefold()])
        if isinstance(params.get("pr_number"), int):
            _append_unique(subject_pr_numbers, [int(params["pr_number"])])
        if fact.extractor in {"reviewer.target_pr@1", "reviewer.target_pr@2"} and isinstance(fact.value, dict):
            author = fact.value.get("author")
            if isinstance(author, str):
                _append_unique(subject_logins, [author.casefold()])
            modules = fact.value.get("modules", {})
            if isinstance(modules, dict):
                _append_unique(subject_module_keys, [str(key) for key in modules])
        if query_id == "reviewer.g3.bundle@1" and isinstance(params.get("module_keys"), list):
            _append_unique(direct_module_keys, [str(key) for key in params["module_keys"]])

        if fact.extractor == "maintainer.loss_modules@1" and isinstance(fact.value, list):
            _append_unique(loss_root_keys, [
                str(item["module_key"])
                for item in fact.value
                if item.get("became_uncovered") and item.get("module_key")
            ])
        elif fact.extractor == "maintainer.dependent_impact@1" and isinstance(fact.value, dict):
            counts = fact.value.get("dependent_counts_by_module", {})
            dependent_modules = fact.value.get("dependent_modules_by_module", {})
            uncovered = params.get("extract_uncovered_module_keys", [])
            roots = sorted(
                dependent_modules,
                key=lambda key: (-float(counts.get(key, 0)), str(key)),
            )
            uncovered_order = sorted(
                (str(key) for key in params.get("extract_uncovered_module_keys", [])),
                key=lambda key: (-float(counts.get(key, 0)), key),
            )
            loss_root_keys = uncovered_order
            _append_unique(direct_module_keys, [str(key) for key in roots])
            for root in roots:
                root_weight = float(counts.get(root, 0))
                root_weights[str(root)] = root_weight
                for dependent in dependent_modules.get(root, []):
                    name = str(dependent)
                    dependent_weights[name] = dependent_weights.get(name, 0.0) + root_weight
            for root in uncovered:
                root_weights[str(root)] = max(
                    root_weights.get(str(root), 0.0),
                    float(counts.get(root, 0)),
                )
            priority_roots = uncovered_order[:3]
            if priority_roots:
                dependency_rows = graph.ro_query(
                    QUERY_REGISTRY[fact.query_id],
                    {
                        "module_keys": priority_roots,
                        "max_depth": int(params.get("max_depth", 2)),
                    },
                ).result_set
                for row in dependency_rows:
                    dependent_key = str(row[1])
                    score = dependent_weights.get(dependent_key, 0.0)
                    edge_ids = row[4] if isinstance(row[4], (list, tuple)) else [row[4]]
                    for edge_id in edge_ids:
                        if edge_id is not None:
                            edge_id = int(edge_id)
                            dependent_edge_weights[edge_id] = max(
                                dependent_edge_weights.get(edge_id, 0.0), score
                            )
            loss_root_keys = sorted(
                loss_root_keys,
                key=lambda key: (-float(counts.get(key, 0)), key),
            )
        elif fact.extractor == "module.owner_list@1" and isinstance(fact.value, list):
            for owner in fact.value:
                if isinstance(owner, dict):
                    login = owner.get("login")
                    handle = owner.get("handle")
                    if login:
                        _append_unique(direct_logins, [str(login).casefold()])
                    if handle:
                        _append_unique(direct_logins, [str(handle).casefold()])
        elif fact.extractor == "ownership.drift_summary@1" and isinstance(fact.value, dict):
            for owner in fact.value.get("owners", []):
                login = owner.get("login")
                handle = owner.get("handle")
                if login:
                    _append_unique(direct_logins, [str(login).casefold()])
                if handle:
                    _append_unique(direct_logins, [str(handle).casefold()])
        elif fact.extractor == "reviewer.g3_candidates@1" and isinstance(fact.value, list):
            for index, candidate in enumerate(fact.value[:8]):
                login = candidate.get("login")
                if login:
                    _append_unique(direct_logins, [str(login).casefold()])
        elif fact.extractor == "reviewer.support_edges@1" and isinstance(fact.value, list):
            for index, event in enumerate(fact.value):
                _append_unique(reviewer_person_ids, [int(event["person_id"])])
                _append_unique(reviewer_support_pr_ids, [int(event["pr_id"])])
                _append_unique(reviewer_edge_priority, [int(event["review_edge_id"])])
                if event.get("module_id") is not None:
                    _append_unique(direct_module_keys, [str(event["module_key"])])
                if event.get("touch_edge_id") is not None:
                    _append_unique(reviewer_edge_priority, [int(event["touch_edge_id"])])
                reviewer_node_weights[int(event["person_id"])] = max(
                    reviewer_node_weights.get(int(event["person_id"]), 0.0),
                    120.0 - index,
                )
                reviewer_node_weights[int(event["pr_id"])] = max(
                    reviewer_node_weights.get(int(event["pr_id"]), 0.0),
                    120.0 - index,
                )
                if event.get("module_id") is not None:
                    reviewer_node_weights[int(event["module_id"])] = max(
                        reviewer_node_weights.get(int(event["module_id"]), 0.0),
                        120.0 - index,
                    )
        elif fact.extractor == "criticality.top_scores@1" and isinstance(fact.value, list):
            for key, _, score in fact.value:
                other_module_weights[str(key)] = float(score)
        elif fact.extractor == "bus.ranking@1" and isinstance(fact.value, list):
            for row in fact.value:
                if row:
                    other_module_weights[str(row[0])] = float(row[5]) if len(row) > 5 else 0.0
                    if len(row) > 3 and row[3]:
                        _append_unique(direct_logins, [str(row[3]).casefold()])
        elif fact.extractor == "blast.dependent_count@1" and int(fact.value or 0) > 0:
            module_key = str(params.get("module_key", ""))
            query_params = {
                "module_key": module_key,
                "max_depth": int(params.get("max_depth", 2)),
            }
            rows = graph.ro_query(QUERY_REGISTRY[query_id], query_params).result_set
            dependent_counts: dict[str, int] = {}
            edges_by_dependent: dict[str, set[int]] = {}
            for row in rows:
                dependent_key = str(row[0])
                edge_ids = row[4] if isinstance(row[4], (list, tuple)) else [row[4]]
                valid_edges = {int(edge_id) for edge_id in edge_ids if edge_id is not None}
                dependent_counts[dependent_key] = dependent_counts.get(dependent_key, 0) + 1
                edges_by_dependent.setdefault(dependent_key, set()).update(valid_edges)
            ranked = sorted(
                dependent_counts,
                key=lambda key: (-dependent_counts[key], key),
            )
            _append_unique(direct_module_keys, ranked)
            for dependent_key, count in dependent_counts.items():
                dependent_weights[dependent_key] = max(
                    dependent_weights.get(dependent_key, 0.0), float(count)
                )
                for edge_id in edges_by_dependent[dependent_key]:
                    dependent_edge_weights[edge_id] = max(
                        dependent_edge_weights.get(edge_id, 0.0), float(count)
                    )
        elif fact.extractor == "blast.dependent_list@1" and isinstance(fact.value, list):
            ranked_dependents = sorted(
                fact.value,
                key=lambda item: (
                    -float(item.get("activity_weight", 0.0)),
                    str(item.get("module_key", "")),
                ),
            )
            _append_unique(
                direct_module_keys,
                [str(item.get("module_key", "")) for item in ranked_dependents if item.get("module_key")],
            )
            for dependent in fact.value:
                key = str(dependent.get("module_key", ""))
                if key:
                    dependent_weights[key] = max(
                        dependent_weights.get(key, 0.0),
                        float(dependent.get("activity_weight", 0.0)),
                    )
                    for edge_id in dependent.get("supporting_dependency_edge_ids", []):
                        dependency_edge_weights[int(edge_id)] = float(
                            dependent.get("activity_weight", 0.0)
                        )

    direct_module_keys = list(dict.fromkeys([*loss_root_keys, *direct_module_keys]))
    primary_direct_modules = direct_module_keys[:80]
    module_keys_for_owners = list(dict.fromkeys([*subject_module_keys, *loss_root_keys]))
    module_rows = graph.ro_query(QUERY_REGISTRY["modules.list@1"], {}).result_set
    module_ids = {str(row[0]): int(row[3]) for row in module_rows}
    owner_rows: list[list[Any]] = []
    owner_edge_ids: list[int] = []
    owner_node_ids: list[int] = []
    if module_keys_for_owners:
        min_as_of = int(proof.get("as_of_ts") or 0)
        try:
            min_ts = int(
                graph.ro_query(QUERY_REGISTRY["meta.graph_bounds@1"], {}).result_set[0][0]
            )
        except (IndexError, TypeError, ValueError):
            min_ts = min_as_of
        activity_start_ts = max(
            min_ts,
            min_as_of - DEFAULT_TOOL_CONFIG.activity_window_days * 86400,
        )
        owner_rows = graph.ro_query(
            QUERY_REGISTRY["ownership.drift.inputs.all@1"],
            {
                "module_keys": module_keys_for_owners,
                "activity_start_ts": activity_start_ts,
                "as_of_ts": min_as_of,
            },
        ).result_set
        for row in sorted(owner_rows, key=lambda item: (str(item[0]), str(item[2] or item[3] or ""))):
            if row[1] == "owner" and row[7] is not None:
                owner_node_ids.append(int(row[7]))
                if row[9] is not None:
                    owner_edge_ids.append(int(row[9]))

    person_ids_by_login: dict[str, int] = {}
    for login in dict.fromkeys([*subject_logins, *direct_logins]):
        rows = graph.ro_query(
            QUERY_REGISTRY["person.by_login@1"], {"person_login": login}
        ).result_set
        if rows:
            person_ids_by_login[login] = int(rows[0][2])

    pr_ids: list[int] = []
    for pr_number in subject_pr_numbers:
        rows = graph.ro_query(
            QUERY_REGISTRY["reviewer.g3.target_pr@2"], {"pr_number": pr_number}
        ).result_set
        pr_ids.extend(int(row[6]) for row in rows if row[6] is not None)

    dependent_order = [
        key
        for key, _ in sorted(
            dependent_weights.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    other_modules = [
        key
        for key, _ in sorted(
            other_module_weights.items(), key=lambda item: (-item[1], item[0])
        )
        if key in module_ids
    ]
    primary_dependents = dependent_order[:80]
    remaining_dependents = dependent_order[80:]
    node_weights: dict[int, float] = {}
    node_weights.update(reviewer_node_weights)
    for module_key, weight in {**other_module_weights, **root_weights, **dependent_weights}.items():
        node_id = module_ids.get(module_key)
        if node_id is not None:
            node_weights[node_id] = max(node_weights.get(node_id, 0.0), float(weight))

    priority_node_ids: list[int] = []
    _append_unique(priority_node_ids, [person_ids_by_login[key] for key in subject_logins if key in person_ids_by_login])
    for module_key in subject_module_keys:
        node_id = module_ids.get(module_key)
        if node_id is not None:
            _append_unique(priority_node_ids, [node_id])
    _append_unique(priority_node_ids, pr_ids)
    _append_unique(priority_node_ids, reviewer_person_ids)
    _append_unique(priority_node_ids, reviewer_support_pr_ids)
    for module_key in primary_direct_modules:
        node_id = module_ids.get(module_key)
        if node_id is not None:
            _append_unique(priority_node_ids, [node_id])
    for module_key in primary_dependents:
        node_id = module_ids.get(module_key)
        if node_id is not None:
            _append_unique(priority_node_ids, [node_id])
    _append_unique(priority_node_ids, owner_node_ids)
    _append_unique(priority_node_ids, [person_ids_by_login[key] for key in direct_logins if key in person_ids_by_login])
    for module_key in [*remaining_dependents, *other_modules]:
        node_id = module_ids.get(module_key)
        if node_id is not None:
            _append_unique(priority_node_ids, [node_id])

    edge_weights = {
        int(row[9]): 1000.0
        for row in owner_rows
        if row[1] == "owner" and row[9] is not None
    }
    for edge_id, weight in dependent_edge_weights.items():
        edge_weights[edge_id] = max(edge_weights.get(edge_id, 0.0), weight)
    priority_edges = list(dict.fromkeys([*reviewer_edge_priority, *owner_edge_ids]))
    return priority_node_ids, priority_edges, node_weights, edge_weights


def export_claim_subgraph(graph: Any, claim: Any) -> dict[str, Any]:
    proof = claim.proof_bundle
    priorities = _claim_priority_ids(graph, claim)
    return export_subgraph(
        graph,
        {"node_ids": proof.get("node_ids", []), "edge_ids": proof.get("edge_ids", [])},
        priority_node_ids=priorities[0],
        priority_edge_ids=priorities[1],
        node_weights=priorities[2],
        edge_weights=priorities[3],
    )