from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from falkordb import FalkorDB

from provenance.config import settings
from provenance.ingest.graph_loader import (
    GraphDataset,
    _scan_component_imports,
    delete_graph_if_exists,
    load_graph,
    module_for_path,
)


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


def test_import_scan_recovers_syntax_errors_and_counts_unreadable_files(
    tmp_path: Path, monkeypatch
) -> None:
    core_root = tmp_path
    component_root = core_root / "homeassistant" / "components"
    source_root = component_root / "source"
    source_root.mkdir(parents=True)
    (source_root / "invalid.py").write_text(
        "from homeassistant.components.target_one.api import Client\n"
        "import homeassistant.components.target_two\n"
        "def invalid[T]():\n    pass\n",
        encoding="utf-8",
    )
    (source_root / "parsed.py").write_text(
        "import homeassistant.components.target_three\n", encoding="utf-8"
    )
    unreadable_path = source_root / "unreadable.py"
    unreadable_path.write_text("pass\n", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        if path == unreadable_path:
            raise PermissionError("fixture is unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    dataset = GraphDataset()

    _scan_component_imports(dataset, core_root, component_root)

    assert dataset.dependency_edges_by_source["python_imports"] == 3
    assert dataset.source_stats["python_files_parsed_by_ast"] == 1
    assert dataset.source_stats["python_files_recovered_by_regex"] == 1
    assert dataset.source_stats["python_files_unreadable"] == 1
    assert dataset.parse_failure_messages["expected '('"] == 1


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