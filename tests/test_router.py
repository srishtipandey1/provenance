from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from falkordb import FalkorDB

from provenance.config import settings
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph
from provenance.agents.router import run_question
from provenance.agents.verifier import VERIFIED
from provenance.tools.queries import QUERY_REGISTRY


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


@pytest.fixture(scope="module")
def router_graph():
    graph_name = f"provenance_router_{uuid4().hex}"
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
        merged_ts = int(
            graph.ro_query(QUERY_REGISTRY["meta.graph_bounds@1"], {}).result_set[0][1]
        )
        graph.query(
            "CREATE (:Person {login: $login, is_bot: false})",
            {"login": "solo-maintainer"},
        )
        numbers = [98001, 98002, 98003]
        graph.query(
            """
            UNWIND $numbers AS number
            CREATE (:PR {
                number: number,
                title: 'router maintainer fixture',
                merged_ts: $merged_ts
            })
            """,
            {"numbers": numbers, "merged_ts": merged_ts},
        )
        graph.query(
            """
            MATCH (person:Person {login: $login})
            UNWIND $numbers AS number
            MATCH (pr:PR {number: number})
            MERGE (person)-[edge:AUTHORED]->(pr)
            SET edge.ts = $merged_ts
            """,
            {"login": "solo-maintainer", "numbers": numbers, "merged_ts": merged_ts},
        )
        graph.query(
            """
            UNWIND $numbers AS number
            MATCH (pr:PR {number: number})
            MATCH (module:Module {key: $module_key})
            MERGE (pr)-[edge:TOUCHES]->(module)
            """,
            {
                "numbers": numbers,
                "module_key": "integration:demo",
            },
        )
        yield graph
    finally:
        delete_graph_if_exists(database, graph_name)


@pytest.mark.parametrize(
    "question, expected_tool",
    [
        ("what is the risk of changing the demo integration?", "module_profile"),
        ("who should review PR 502?", "suggest_reviewers"),
        ("who should review #502?", "suggest_reviewers"),
        ("what happens if solo-maintainer leaves?", "maintainer_loss"),
        ("who owns demo?", "module_profile"),
        ("what depends on demo?", "blast_radius"),
        ("which modules are most at risk?", "bus_factor"),
        ("why was demo decided?", "decision_trace"),
    ],
)
def test_supported_question_routes_verify_all_claims(
    router_graph,
    question: str,
    expected_tool: str,
) -> None:
    routed = run_question(router_graph, question)

    assert routed.unsupported_message is None
    assert routed.answer is not None
    assert routed.answer.claims
    assert routed.answer.summary_counts[VERIFIED] == len(routed.answer.claims)
    assert not routed.answer.removed_claims
    assert any(item["tool"] == expected_tool for item in routed.tool_timings)
    assert all(item["seconds"] >= 0 for item in routed.tool_timings)


def test_fuzzy_module_and_case_insensitive_login_resolution(router_graph) -> None:
    risk = run_question(router_graph, "what is the risk of changing the DEMO integration?")
    owner = run_question(router_graph, "who owns the demo integration?")
    leave = run_question(router_graph, "what happens if SOLO-MAINTAINER leaves?")

    assert risk.answer is not None and risk.answer.summary_counts[VERIFIED] == len(risk.answer.claims)
    assert owner.answer is not None and owner.answer.summary_counts[VERIFIED] == len(owner.answer.claims)
    assert leave.answer is not None and leave.answer.summary_counts[VERIFIED] == len(leave.answer.claims)
    assert "integration:demo" in risk.answer.final_answer
    assert "solo-maintainer" in leave.answer.final_answer


def test_unknown_question_returns_supported_question_list(router_graph) -> None:
    routed = run_question(router_graph, "Can you predict the weather next month?")

    assert routed.answer is None
    assert routed.unsupported_message
    assert "who should review PR <number>?" in routed.unsupported_message


def test_tamper_hook_contradicts_a_cited_fact(router_graph) -> None:
    routed = run_question(
        router_graph,
        "what is the risk of changing demo?",
        tamper_n=1,
    )

    assert routed.tampered_fact_id is not None
    assert routed.answer is not None
    assert any(claim.status == "CONTRADICTED" for claim in routed.answer.claims)
    assert routed.answer.removed_claims


@pytest.mark.real_graph
def test_real_graph_smoke_routes_1_2_3_with_timings() -> None:
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph("provenance_ha")
    routes = [
        run_question(graph, "what is the risk of changing sensor?"),
        run_question(graph, "who should review PR 179264?"),
        run_question(graph, "what happens if epenet leaves?"),
    ]
    timings = [
        {"tools": [item["tool"] for item in routed.tool_timings],
         "seconds": round(sum(item["seconds"] for item in routed.tool_timings), 4)}
        for routed in routes
    ]
    print(f"real_graph_router_timings={timings}")

    assert all(routed.answer is not None for routed in routes)
    assert all(
        routed.answer.summary_counts[VERIFIED] == len(routed.answer.claims)
        for routed in routes
        if routed.answer is not None
    )
    assert all(item["seconds"] > 0 for item in timings)
