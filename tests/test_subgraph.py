from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from falkordb import FalkorDB

from provenance.api.subgraph import MAX_EDGES, MAX_NODES, export_subgraph
from provenance.config import settings
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph
from provenance.tools.queries import QUERY_REGISTRY


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


@pytest.fixture
def subgraph_graph():
    graph_name = f"provenance_subgraph_{uuid4().hex}"
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    try:
        load_graph(
            graph_name=graph_name,
            batch_size=500,
            prs_jsonl=FIXTURE_ROOT / "prs" / "pages.jsonl",
            core_root=FIXTURE_ROOT / "core",
            architecture_root=FIXTURE_ROOT / "architecture",
            database=database,
            run_verification=False,
        )
        yield database.select_graph(graph_name)
    finally:
        delete_graph_if_exists(database, graph_name)


def test_subgraph_matches_internal_ids_and_includes_edge_endpoints(subgraph_graph) -> None:
    module_id = int(
        subgraph_graph.ro_query(
            "MATCH (module:Module {key: $key}) RETURN id(module)",
            {"key": "integration:demo"},
        ).result_set[0][0]
    )
    edge_row = subgraph_graph.ro_query(
        "MATCH (pr:PR)-[touch:TOUCHES]->(module:Module) "
        "RETURN id(touch), id(pr), id(module) LIMIT 1",
        {},
    ).result_set[0]
    edge_id = int(
        edge_row[0]
    )
    edge_endpoint_ids = [int(edge_row[1]), int(edge_row[2])]

    result = export_subgraph(
        subgraph_graph,
        {"node_ids": [module_id, *edge_endpoint_ids], "edge_ids": [edge_id]},
    )

    assert QUERY_REGISTRY["api.subgraph.nodes_by_id@1"]
    assert "WHERE id(node) IN $ids" in QUERY_REGISTRY["api.subgraph.nodes_by_id@1"]
    assert module_id in {node["id"] for node in result["nodes"]}
    assert result["edges"][0]["id"] == edge_id
    assert result["edges"][0]["source"] in {node["id"] for node in result["nodes"]}
    assert result["edges"][0]["target"] in {node["id"] for node in result["nodes"]}
    assert all(
        set(node["props"]) <= {"login", "key", "name", "number", "title", "status", "kind", "ts"}
        for node in result["nodes"]
    )
    assert result["truncated"] is False


def test_subgraph_keeps_subject_direct_and_top_weighted_elements(subgraph_graph) -> None:
    keys = ["priority-subject", "priority-direct", "priority-top", "priority-low"]
    for key in keys:
        subgraph_graph.query(
            "CREATE (:Module {key: $key, kind: 'integration', name: $key})",
            {"key": key},
        )
    edge_ids = []
    for source, target in zip(keys, keys[1:]):
        subgraph_graph.query(
            "MATCH (source:Module {key: $source_key}), "
            "(target:Module {key: $target_key}) "
            "CREATE (source)-[edge:DEPENDS_ON {kind: 'fixture'}]->(target) "
            "RETURN id(edge)",
            {"source_key": source, "target_key": target},
        )
        edge_ids.append(
            int(
                subgraph_graph.ro_query(
                    "MATCH (source:Module {key: $source_key})-[edge:DEPENDS_ON]->"
                    "(target:Module {key: $target_key}) RETURN id(edge)",
                    {"source_key": source, "target_key": target},
                ).result_set[0][0]
            )
        )
    module_ids = {
        str(key): int(node_id)
        for key, node_id in subgraph_graph.ro_query(
            "MATCH (module:Module) WHERE module.key IN $keys "
            "RETURN module.key, id(module)",
            {"keys": keys},
        ).result_set
    }

    result = export_subgraph(
        subgraph_graph,
        {"node_ids": list(module_ids.values()), "edge_ids": edge_ids},
        max_nodes=3,
        max_edges=1,
        priority_node_ids=[module_ids["priority-subject"], module_ids["priority-direct"]],
        priority_edge_ids=[edge_ids[1]],
        node_weights={module_ids["priority-top"]: 100.0, module_ids["priority-low"]: 1.0},
    )

    returned_ids = {node["id"] for node in result["nodes"]}
    assert returned_ids == {
        module_ids["priority-subject"],
        module_ids["priority-direct"],
        module_ids["priority-top"],
    }
    assert [edge["id"] for edge in result["edges"]] == [edge_ids[1]]
    assert result["node_count_total"] == 4
    assert result["edge_count_total"] == 3
    assert result["node_count_returned"] == 3
    assert result["edge_count_returned"] == 1
    assert result["truncated"] is True
    assert "Showing 3 of 4 evidence nodes" in result["note"]
    assert "1 of 3 evidence edges" in result["note"]