from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from falkordb import FalkorDB

from provenance.config import settings
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph, module_for_path


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


def test_module_path_mapping() -> None:
    assert module_for_path("homeassistant/components/demo/sensor.py") == {
        "key": "integration:demo",
        "kind": "integration",
        "name": "demo",
    }
    assert module_for_path("tests/components/demo/test_sensor.py")["key"] == "integration:demo"
    assert module_for_path("homeassistant/helpers/utility.py") == {
        "key": "homeassistant/helpers",
        "kind": "core",
        "name": "helpers",
    }
    assert module_for_path(".github/workflows/ci.yaml") == {
        "key": "infra:.github",
        "kind": "infra",
        "name": ".github",
    }


def test_fixture_graph_bot_filtering_and_author_review_exclusion() -> None:
    graph_name = f"provenance_test_{uuid4().hex}"
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph(graph_name)
    try:
        summary, _ = load_graph(
            graph_name=graph_name,
            batch_size=500,
            prs_jsonl=FIXTURE_ROOT / "prs" / "pages.jsonl",
            core_root=FIXTURE_ROOT / "core",
            architecture_root=FIXTURE_ROOT / "architecture",
            database=database,
            run_verification=False,
        )
        graph = database.select_graph(graph_name)

        module_result = graph.ro_query(
            "MATCH (module:Module {key: $key}) RETURN module.kind, module.name",
            {"key": "integration:demo"},
        ).result_set
        bot_author = graph.ro_query(
            "MATCH (person:Person {login: $login}) RETURN person.is_bot",
            {"login": "fixture-bot"},
        ).result_set
        author_reviews = graph.ro_query(
            """
            MATCH (person:Person {login: $login})-[review:REVIEWED]->(pr:PR {number: $number})
            RETURN count(review)
            """,
            {"login": "author-user", "number": 501},
        ).result_set
        bot_reviews = graph.ro_query(
            """
            MATCH (person:Person {login: $login})-[review:REVIEWED]->(:PR)
            RETURN count(review)
            """,
            {"login": "review-bot"},
        ).result_set
        human_reviews = graph.ro_query(
            """
            MATCH (person:Person {login: $login})-[review:REVIEWED]->(pr:PR {number: $number})
            RETURN count(review)
            """,
            {"login": "human-reviewer", "number": 501},
        ).result_set
        owner_counts = graph.ro_query(
            """
            MATCH (owner)-[declared:DECLARED_OWNER]->(module:Module {key: $key})
            RETURN count(declared)
            """,
            {"key": "integration:demo"},
        ).result_set
        dependency_counts = graph.ro_query(
            """
            MATCH (source:Module {key: $source})-[edge:DEPENDS_ON]->(target:Module)
            RETURN edge.kind, target.key
            ORDER BY edge.kind, target.key
            """,
            {"source": "integration:demo"},
        ).result_set
        discussion_reference = graph.ro_query(
            """
            MATCH (pr:PR {number: $number})-[reference:REFERENCES]->(decision:Decision)
            RETURN decision.id, decision.source
            ORDER BY decision.id
            """,
            {"number": 501},
        ).result_set

        assert module_result == [["integration", "demo"]]
        assert bot_author == [[True]]
        assert author_reviews == [[0]]
        assert bot_reviews == [[0]]
        assert human_reviews == [[1]]
        assert owner_counts == [[2]]
        assert {tuple(row) for row in dependency_counts} == {
            ("dependency", "integration:dependency"),
            ("after_dependency", "integration:after_dependency"),
            ("python_import", "integration:dependency"),
        }
        assert {row[1] for row in discussion_reference} == {
            "architecture-reference",
            "adr/0001-example.md",
        }
        assert summary["node_counts"]["PR"] == 2
    finally:
        delete_graph_if_exists(database, graph_name)