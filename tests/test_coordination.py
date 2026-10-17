from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from falkordb import FalkorDB

from provenance.agents.coordination import (
    HistorianAgent,
    ReviewerAgent,
    RiskAgent,
    _persist,
    ensure_coordination_schema,
    run_orchestration,
    task_graph,
)
from provenance.agents.router import run_question
from provenance.config import settings
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph
from provenance.tools.queries import QUERY_REGISTRY
from scripts.tenant_demo import copy_module_slice


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


@pytest.fixture
def coordination_graph():
    graph_name = f"provenance_coord_{uuid4().hex}"
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    try:
        load_graph(
            graph_name=graph_name,
            batch_size=100,
            prs_jsonl=FIXTURE_ROOT / "prs" / "pages.jsonl",
            core_root=FIXTURE_ROOT / "core",
            architecture_root=FIXTURE_ROOT / "architecture",
            database=database,
            run_verification=False,
        )
        yield database.select_graph(graph_name), database
    finally:
        delete_graph_if_exists(database, graph_name)


@pytest.mark.requires_falkordb
def test_three_agents_persist_tasks_evidence_dependency_and_cross_run_memory(
    coordination_graph,
) -> None:
    graph, _ = coordination_graph
    runs = run_orchestration(
        graph,
        pr_number=501,
        module_key="integration:demo",
        person_login="author-user",
    )

    assert [run.agent_name for run in runs] == [
        "ReviewerAgent",
        "RiskAgent",
        "HistorianAgent",
        "HistorianAgent",
    ]
    assert runs[-1].prior_episode_id == runs[-2].episode_id
    assert "Prior episode" in runs[-1].answer_summary
    persisted = task_graph(graph, {run.task_id for run in runs})
    assert len(persisted) == 4
    by_kind = {row["kind"]: row for row in persisted if row["kind"] != "maintainer_history"}
    assert by_kind["reviewer_recommendation"]["status"] == "completed"
    assert by_kind["module_risk_assessment"]["depends_on"] == runs[0].task_id
    risk_edges = by_kind["module_risk_assessment"]["edges"]
    assert {"type": "DEPENDS_ON", "target": runs[0].task_id} in risk_edges
    assert any(edge["type"] == "USED_FACT" for edge in risk_edges)

    episode = graph.ro_query(
        QUERY_REGISTRY["coordination.episode.latest_subject@1"],
        {"agent_name": "HistorianAgent", "subject": "author-user"},
    ).result_set
    assert episode[0][0] == runs[-1].episode_id
    episode_record = graph.ro_query(
        QUERY_REGISTRY["coordination.episode.by_id@1"],
        {"episode_id": runs[0].episode_id},
    ).result_set[0]
    stored_facts = json.loads(episode_record[0])
    assert stored_facts
    assert all(item["query_id"] in QUERY_REGISTRY for item in stored_facts)
    assert all(int(node_id) >= 0 for node_id in episode_record[1])
    assert all(int(edge_id) >= 0 for edge_id in episode_record[2])
    used_fact_count = graph.ro_query(
        QUERY_REGISTRY["coordination.episode.used_fact_count@1"],
        {"episode_id": runs[0].episode_id},
    ).result_set[0][0]
    assert used_fact_count > 0


@pytest.mark.requires_falkordb
def test_tenant_graph_copies_route_and_persist_independent_state(coordination_graph) -> None:
    source, database = coordination_graph
    tenant_names = [f"provenance_tenant_test_{uuid4().hex}" for _ in range(2)]
    try:
        tenants = []
        for tenant_name in tenant_names:
            tenant = database.select_graph(tenant_name)
            assert copy_module_slice(
                source, tenant, ["integration:demo", "integration:dependency"]
            ) >= 2
            tenants.append(tenant)
        question = "What is the risk of changing integration:demo?"
        routed = [run_question(tenant, question) for tenant in tenants]
        assert all(result.answer is not None for result in routed)
        assert routed[0].answer.final_answer == routed[1].answer.final_answer
        runs = [
            _persist(
                tenant,
                agent_name="TenantTestAgent",
                kind="tenant_test",
                subject="integration:demo",
                question=question,
                answer_summary=result.answer.final_answer,
                tool_results=result.tool_results,
            )
            for tenant, result in zip(tenants, routed, strict=True)
        ]
        assert runs[0].task_id != runs[1].task_id
        task_counts = [
            tenant.ro_query(
                QUERY_REGISTRY["coordination.graph.task_count@1"], {}
            ).result_set[0][0]
            for tenant in tenants
        ]
        assert task_counts == [1, 1]
    finally:
        for tenant_name in tenant_names:
            delete_graph_if_exists(database, tenant_name)


@pytest.mark.requires_falkordb
def test_twenty_parallel_graph_task_writes_succeed(coordination_graph) -> None:
    graph, _ = coordination_graph

    def write_one(index: int) -> str:
        result = _persist(
            graph,
            agent_name="ConcurrentTestAgent",
            kind="concurrency_test",
            subject=f"test:{index}",
            question=f"Persist test task {index}.",
            answer_summary=f"Completed test task {index}.",
            tool_results=[],
        )
        return result.task_id

    with ThreadPoolExecutor(max_workers=20) as executor:
        task_ids = list(executor.map(write_one, range(20)))

    assert len(set(task_ids)) == 20
    assert graph.ro_query(
        QUERY_REGISTRY["coordination.graph.task_count@1"], {}
    ).result_set[0][0] == 20


@pytest.mark.requires_falkordb
def test_agent_task_dependency_requires_existing_task(coordination_graph) -> None:
    graph, _ = coordination_graph
    reviewer = ReviewerAgent(graph).run(501)
    with pytest.raises(ValueError, match="was not found"):
        RiskAgent(graph).run(
            "integration:demo", depends_on_task_id="not-a-task"
        )
    assert task_graph(graph, {reviewer.task_id})[0]["status"] == "completed"


@pytest.mark.requires_falkordb
def test_historian_first_run_has_no_prior_episode(coordination_graph) -> None:
    graph, _ = coordination_graph
    first = HistorianAgent(graph).run("author-user")
    assert first.prior_episode_id is None
    second = HistorianAgent(graph).run("author-user")
    assert second.prior_episode_id == first.episode_id


@pytest.mark.requires_falkordb
def test_coordination_schema_adds_missing_indexes_idempotently(coordination_graph) -> None:
    _, database = coordination_graph
    graph_name = f"provenance_coord_schema_{uuid4().hex}"
    graph = database.select_graph(graph_name)
    try:
        graph.query(QUERY_REGISTRY["coordination.tenant.index.module@1"], {})
        graph.query(
            QUERY_REGISTRY["coordination.tenant.copy.modules@1"],
            {"rows": [{"key": "integration:probe", "kind": "integration", "name": "probe"}]},
        )
        ensure_coordination_schema(graph)
        ensure_coordination_schema(graph)
        indexes = graph.ro_query(
            QUERY_REGISTRY["coordination.schema.indexes@1"], {}
        ).result_set
        indexed_keys = {(str(row[0]), tuple(row[1])) for row in indexes}
        assert ("Task", ("id",)) in indexed_keys
        assert ("Episode", ("id",)) in indexed_keys
    finally:
        delete_graph_if_exists(database, graph_name)
