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
    edge_id = int(
        subgraph_graph.ro_query(
            "MATCH (pr:PR)-[touch:TOUCHES]->(module:Module) "
            "RETURN id(touch) LIMIT 1",
            {},
        ).result_set[0][0]
    )

    result = export_subgraph(
        subgraph_graph,
        {"node_ids": [module_id], "edge_ids": [edge_id]},
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


def test_subgraph_caps_requested_ids_and_sets_truncated(subgraph_graph) -> None:
    module_id = int(
        subgraph_graph.ro_query(
            "MATCH (module:Module {key: $key}) RETURN id(module)",
            {"key": "integration:demo"},
        ).result_set[0][0]
    )
    requested_nodes = [module_id, *range(1_000_000, 1_000_149)]
    requested_edges = list(range(2_000_000, 2_000_301))

    result = export_subgraph(
        subgraph_graph,
        {"node_ids": requested_nodes, "edge_ids": requested_edges},
    )

    assert len(result["nodes"]) <= MAX_NODES
    assert len(result["edges"]) <= MAX_EDGES
    assert result["truncated"] is True