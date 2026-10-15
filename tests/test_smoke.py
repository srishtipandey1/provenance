from uuid import uuid4

from falkordb import FalkorDB

from provenance.config import settings


def test_falkordb_smoke() -> None:
    graph_name = f"smoke-{uuid4().hex}"
    database = FalkorDB(
        host=settings.FALKORDB_HOST,
        port=settings.FALKORDB_PORT,
    )
    graph = database.select_graph(graph_name)

    try:
        graph.query(
            """
            CREATE (source:SmokeNode {name: $source_name})
            CREATE (target:SmokeNode {name: $target_name})
            CREATE (source)-[:CONNECTS_TO]->(target)
            """,
            {
                "source_name": "source",
                "target_name": "target",
            },
        )

        result = graph.ro_query(
            """
            MATCH (source:SmokeNode)-[:CONNECTS_TO]->(target:SmokeNode)
            RETURN source.name, target.name
            """
        )

        assert result.result_set == [["source", "target"]]
    finally:
        graph.delete()