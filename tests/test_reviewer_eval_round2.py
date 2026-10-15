from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from falkordb import FalkorDB

from provenance.config import settings
from provenance.eval.reviewer_eval_round2 import _g3_ranking, _metrics, load_fold_history
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


def test_fold_history_excludes_prs_at_or_after_window_start() -> None:
    graph_name = f"provenance_eval_round2_{uuid4().hex}"
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
        window_start = int(
            graph.ro_query(
                "MATCH (pr:PR {number: $number}) RETURN pr.merged_ts",
                {"number": 501},
            ).result_set[0][0]
        )
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
                "reviewer": "human-reviewer",
                "module_key": "integration:demo",
                "number": 599,
                "merged_ts": window_start - 1,
                "review_ts": window_start - 2,
                "state": "APPROVED",
            },
        )
        graph.query(
            """
            MATCH (author:Person {login: $author}),
                  (reviewer:Person {login: $reviewer}),
                  (module:Module {key: $module_key})
            CREATE (exact:PR {number: $number, merged_ts: $merged_ts})
            CREATE (author)-[:AUTHORED {ts: $merged_ts}]->(exact)
            CREATE (reviewer)-[:REVIEWED {state: $state, ts: $review_ts}]->(exact)
            CREATE (exact)-[:TOUCHES {files: 1}]->(module)
            """,
            {
                "author": "fixture-bot",
                "reviewer": "human-reviewer",
                "module_key": "integration:demo",
                "number": 600,
                "merged_ts": window_start,
                "review_ts": window_start - 1,
                "state": "APPROVED",
            },
        )
        graph.query(
            """
            MATCH (author:Person {login: $author}),
                  (reviewer:Person {login: $reviewer}),
                  (module:Module {key: $module_key})
            CREATE (future:PR {number: $number, merged_ts: $merged_ts})
            CREATE (author)-[:AUTHORED {ts: $merged_ts}]->(future)
            CREATE (reviewer)-[:REVIEWED {state: $state, ts: $review_ts}]->(future)
            CREATE (future)-[:TOUCHES {files: 1}]->(module)
            """,
            {
                "author": "fixture-bot",
                "reviewer": "human-reviewer",
                "module_key": "integration:demo",
                "number": 601,
                "merged_ts": window_start + 86400,
                "review_ts": window_start + 1,
                "state": "APPROVED",
            },
        )
        history = load_fold_history(
            graph,
            as_of=window_start,
            module_keys={"integration:demo"},
            targets=[{"number": 501, "integration_keys": {"integration:demo"}}],
        )

        evidence_numbers = set(history.global_review_prs["human-reviewer"])
        evidence_numbers.update(
            history.review_prs_by_module["integration:demo"]["human-reviewer"]
        )
        evidence_times = graph.ro_query(
            "MATCH (pr:PR) WHERE pr.number IN $numbers RETURN pr.number, pr.merged_ts",
            {"numbers": sorted(evidence_numbers)},
        ).result_set
        assert 600 not in evidence_numbers
        assert 601 not in evidence_numbers
        assert 501 not in evidence_numbers
        assert all(int(merged_ts) < window_start for _, merged_ts in evidence_times)
        assert 599 in evidence_numbers
    finally:
        delete_graph_if_exists(database, graph_name)


def test_g3_normalizes_within_union_candidate_set() -> None:
    record = {
        "target": {"author": "author", "modules": {"integration:demo": "integration"}},
        "owner_is_bot": {},
        "history": type(
            "History",
            (),
            {
                "popularity": {"g1": 3, "b3": 10},
                "is_bot": {},
                "global_review_prs": {"g1": [1], "b3": [2, 3]},
                "review_prs_by_module": {"integration:demo": {"g1": [1]}},
                "author_prs_by_module": {},
            },
        )(),
    }
    base = {
        "scores": {"G1": {"g1": 4.0}, "B3": {"g1": 2.0, "b3": 8.0}},
        "rankings": {"G1": ["g1"], "B3": ["b3", "g1"]},
    }
    ranking, evidence = _g3_ranking(record, base, 0.5, include_evidence=True)

    assert set(ranking) == {"g1", "b3"}
    assert evidence["g1"]["score"] == 1.125
    assert evidence["b3"]["score"] == 0.5
    assert evidence["b3"]["review_prs_global"] == [2, 3]


def test_round2_metrics_use_distinct_reviewers_and_expected_ranking_metrics() -> None:
    metrics = _metrics(
        [
            {
                "target": {"true_reviewers": {"reviewer"}},
                "rankings": {"G3": ["other", "reviewer"]},
            }
        ],
        ("G3",),
    )

    assert metrics == {
        "n": 1,
        "methods": {
            "G3": {
                "hit@1": 0.0,
                "hit@3": 1.0,
                "hit@5": 1.0,
                "recall@5": 1.0,
                "MRR": 0.5,
            }
        },
    }