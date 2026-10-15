from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from falkordb import FalkorDB

from provenance.config import settings
from provenance.eval.reviewer_eval import (
    HistoryFeatures,
    expertise_scores,
    fetch_history,
    rank_candidates,
)
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


def test_history_is_strictly_as_of_and_candidates_exclude_author_and_bots() -> None:
    graph_name = f"provenance_eval_test_{uuid4().hex}"
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
        graph = database.select_graph(graph_name)
        target_time = graph.ro_query(
            "MATCH (pr:PR {number: $number}) RETURN pr.merged_ts",
            {"number": 501},
        ).result_set[0][0]
        graph.query(
            """
            MATCH (author:Person {login: $author}),
                  (reviewer:Person {login: $reviewer}),
                  (module:Module {key: $module_key})
            CREATE (prior:PR {number: $number, merged_ts: $merged_ts})
            CREATE (author)-[:AUTHORED {ts: $merged_ts}]->(prior)
            CREATE (reviewer)-[:REVIEWED {state: $state, ts: $review_ts}]->(prior)
            CREATE (prior)-[:TOUCHES {files: 1}]->(module)
            """,
            {
                "author": "fixture-bot",
                "reviewer": "author-user",
                "module_key": "integration:demo",
                "number": 500,
                "merged_ts": 1790848800,
                "review_ts": 1790845200,
                "state": "APPROVED",
            },
        )
        graph.query(
            """
            MATCH (author:Person {login: $author}),
                  (reviewer:Person {login: $reviewer}),
                  (module:Module {key: $module_key})
            CREATE (later:PR {number: $number, merged_ts: $merged_ts})
            CREATE (author)-[:AUTHORED {ts: $merged_ts}]->(later)
            CREATE (reviewer)-[:REVIEWED {state: $state, ts: $review_ts}]->(later)
            CREATE (later)-[:TOUCHES {files: 1}]->(module)
            """,
            {
                "author": "fixture-bot",
                "reviewer": "human-reviewer",
                "module_key": "integration:demo",
                "number": 503,
                "merged_ts": int(target_time) + 86400,
                "review_ts": int(target_time) + 3600,
                "state": "APPROVED",
            },
        )
        history = fetch_history(
            graph,
            as_of=int(target_time),
            module_keys={"integration:demo"},
            integration_keys={"integration:demo"},
        )

        assert 500 in history.evidence_prs[("review", "author-user", "integration:demo")]
        evidence_numbers = sorted(
            {
                pr_number
                for pr_numbers in history.evidence_prs.values()
                for pr_number in pr_numbers
            }
        )
        evidence_times = graph.ro_query(
            "MATCH (pr:PR) WHERE pr.number IN $numbers RETURN pr.number, pr.merged_ts",
            {"numbers": evidence_numbers},
        ).result_set
        assert all(int(merged_ts) < int(target_time) for _, merged_ts in evidence_times)
        assert history.popularity == {"author-user": 1}
        assert history.integration_pr_count == 1
        candidates = rank_candidates(
            {"author-user": 10.0, "human-reviewer": 5.0, "fixture-bot": 9.0},
            popularity={"author-user": 10, "human-reviewer": 2, "fixture-bot": 20},
            is_bot={"author-user": False, "human-reviewer": False, "fixture-bot": True},
            author="author-user",
        )
        assert candidates == ["human-reviewer"]
    finally:
        delete_graph_if_exists(database, graph_name)


def test_expertise_scores_use_one_unit_weight_per_module() -> None:
    features = HistoryFeatures(
        review_expertise={
            ("reviewer", "integration:demo"): {30: 1.5},
            ("author-only", "integration:demo"): {30: 0.0},
        },
        author_expertise={
            ("reviewer", "integration:demo"): {30: 0.5},
            ("author-only", "integration:demo"): {30: 2.0},
        },
    )
    scores = expertise_scores(
        {"modules": {"integration:demo": "integration", "infra:docs": "infra"}},
        features,
        half_life=30,
        author_weight=0.5,
    )

    assert scores == {"reviewer": 1.75, "author-only": 1.0}