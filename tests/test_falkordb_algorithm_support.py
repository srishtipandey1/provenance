from __future__ import annotations

from uuid import uuid4

from falkordb import FalkorDB

from provenance.config import settings
from provenance.ingest.graph_loader import delete_graph_if_exists


def test_documented_cypher_functions_paths_and_algorithms() -> None:
    graph_name = f"provenance_algo_probe_{uuid4().hex}"
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph(graph_name)
    try:
        for key in ("a", "b", "c", "d"):
            graph.query("CREATE (:Probe {key: $key})", {"key": key})
        graph.query(
            """
            MATCH (a:Probe {key: $a}), (b:Probe {key: $b}), (c:Probe {key: $c})
            CREATE (a)-[:LINK]->(b), (b)-[:LINK]->(c)
            """,
            {"a": "a", "b": "b", "c": "c"},
        )

        math_result = graph.ro_query(
            "RETURN exp(0), log(exp(1)), toInteger(2.8)"
        ).result_set
        variable_path = graph.ro_query(
            """
            MATCH path=(a:Probe {key: $source})-[:LINK*1..2]->(target:Probe {key: $target})
            RETURN count(path)
            """,
            {"source": "a", "target": "c"},
        ).result_set
        shortest_path = graph.ro_query(
            """
            MATCH (source:Probe {key: $source}), (target:Probe {key: $target})
            RETURN length(shortestPath((source)-[:LINK*1..]->(target)))
            """,
            {"source": "a", "target": "c"},
        ).result_set
        page_rank = graph.ro_query(
            """
            CALL algo.pageRank($label, $relationship_type)
            YIELD node, score
            RETURN node.key, score
            ORDER BY node.key
            """,
            {"label": "Probe", "relationship_type": "LINK"},
        ).result_set
        bfs = graph.ro_query(
            """
            MATCH (source:Probe {key: $key})
            CALL algo.BFS(source, $max_depth, $relationship_type)
            YIELD nodes, edges
            RETURN [node IN nodes | node.key], size(edges)
            """,
            {"key": "a", "max_depth": 2, "relationship_type": "LINK"},
        ).result_set
        betweenness = graph.ro_query(
            """
            CALL algo.betweenness($config)
            YIELD node, score
            RETURN node.key, score
            ORDER BY node.key
            """,
            {
                "config": {
                    "nodeLabels": ["Probe"],
                    "relationshipTypes": ["LINK"],
                    "samplingSize": 3,
                    "samplingSeed": 1,
                }
            },
        ).result_set
        wcc = graph.ro_query(
            """
            CALL algo.WCC($config)
            YIELD node, componentId
            RETURN count(node), count(DISTINCT componentId)
            """,
            {"config": {"nodeLabels": ["Probe"], "relationshipTypes": ["LINK"]}},
        ).result_set
        cdlp = graph.ro_query(
            """
            CALL algo.labelPropagation($config)
            YIELD node, communityId
            RETURN count(node), count(DISTINCT communityId)
            """,
            {
                "config": {
                    "nodeLabels": ["Probe"],
                    "relationshipTypes": ["LINK"],
                    "maxIterations": 5,
                }
            },
        ).result_set

        assert math_result == [[1.0, 1.0, 2]]
        assert variable_path == [[1]]
        assert shortest_path == [[2]]
        assert len(page_rank) == 4
        assert bfs and bfs[0][1] == 2
        assert len(betweenness) == 4
        assert wcc == [[4, 2]]
        assert cdlp and cdlp[0][0] == 4
    finally:
        delete_graph_if_exists(database, graph_name)