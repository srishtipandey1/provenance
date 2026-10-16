from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from falkordb import FalkorDB

import provenance.api.app as api_module
from provenance.agents.llm import LLMProviderError
from provenance.api.app import create_app
from provenance.config import settings
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


@pytest.fixture(scope="module")
def api_graph():
    graph_name = f"provenance_api_{uuid4().hex}"
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


@pytest.fixture
def api_client(api_graph, tmp_path):
    app = create_app(
        graph=api_graph,
        provider_probe=lambda: [
            {"provider": "mock", "model": "mock-model", "available": True}
        ],
        daily_budget_path=tmp_path / "daily.json",
    )
    with TestClient(app) as client:
        yield client


class UnavailableClient:
    config = SimpleNamespace(max_calls_per_question=3)

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        raise LLMProviderError(["provider unavailable"])


def test_all_api_endpoints_return_typed_json(api_client) -> None:
    health = api_client.get("/api/health", headers={"X-Request-ID": "api-test-1"})
    assert health.status_code == 200
    assert health.headers["X-Request-ID"] == "api-test-1"
    assert health.json()["graph_reachable"] is True
    assert health.json()["llm_providers"][0]["provider"] == "mock"

    tools = api_client.get("/api/tools")
    assert tools.status_code == 200
    assert len(tools.json()["tools"]) == 8

    tool_result = api_client.post(
        "/api/tool/module_profile",
        json={"args": {"module_key": "demo"}},
    )
    assert tool_result.status_code == 200
    assert tool_result.json()["tool"] == "module_profile"
    assert tool_result.json()["evidence"]["node_ids"]

    bad_tool = api_client.post(
        "/api/tool/module_profile",
        json={"args": {"module_key": "demo", "unexpected": True}},
    )
    assert bad_tool.status_code == 422
    assert "unexpected" not in bad_tool.text

    risk = api_client.get("/api/risk")
    assert risk.status_code == 200
    assert risk.json()["graph"] == "provenance_ha"

    scoreboard = api_client.get("/api/scoreboard")
    assert scoreboard.status_code == 200
    assert scoreboard.json()["regime"] == "per-target as-of, pooled Folds B+C"
    assert "rag_baseline" in scoreboard.json()

    modules = api_client.get("/api/search/modules", params={"q": "demo"})
    assert modules.status_code == 200
    assert modules.json()["results"][0]["key"] == "integration:demo"

    people = api_client.get("/api/search/people", params={"q": "a"})
    assert people.status_code == 200
    assert len(people.json()["results"]) <= 10

    neighborhood = api_client.get(
        "/api/graph/neighborhood",
        params={"module": "demo", "depth": 1},
    )
    assert neighborhood.status_code == 200
    assert neighborhood.json()["module"]["key"] == "integration:demo"
    assert len(neighborhood.json()["behavioral_people"]) <= 8
    assert len(neighborhood.json()["direct_dependents"]) <= 30
    assert all(
        set(node["props"]) <= {"login", "key", "name", "number", "title", "status", "kind", "ts"}
        for node in neighborhood.json()["subgraph"]["nodes"]
    )

    answer = api_client.post(
        "/api/ask",
        json={"question": "who owns demo?", "mode": "deterministic"},
    )
    assert answer.status_code == 200
    payload = answer.json()
    assert payload["claims"]
    assert payload["claims"][0]["status"] == "VERIFIED"
    assert payload["claims"][0]["proof"]["query_ids"]
    assert isinstance(payload["claims"][0]["proof"]["subgraph"]["nodes"], list)


def test_question_validation_tamper_and_deterministic_ttl_cache(
    api_client, monkeypatch
) -> None:
    too_long = api_client.post(
        "/api/ask",
        json={"question": "x" * 301, "mode": "deterministic"},
    )
    assert too_long.status_code == 422
    assert "x" * 20 not in too_long.text

    tampered = api_client.post(
        "/api/ask",
        json={"question": "what is the risk of changing demo?", "tamper": 1},
    )
    assert tampered.status_code == 200
    assert tampered.json()["tampered_fact_id"]
    assert any(claim["status"] == "CONTRADICTED" for claim in tampered.json()["claims"])

    calls = 0
    original = api_module.run_question

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(api_module, "run_question", counted)
    question = {"question": "who owns demo?", "mode": "deterministic"}
    assert api_client.post("/api/ask", json=question).status_code == 200
    assert api_client.post("/api/ask", json=question).status_code == 200
    assert calls == 1


def test_llm_rate_limit_and_global_budget_fallback(api_graph, tmp_path) -> None:
    limited_client = UnavailableClient()
    limited_app = create_app(
        graph=api_graph,
        llm_client=limited_client,
        provider_probe=lambda: [],
        llm_rate_limit=1,
        daily_limit=20,
        daily_budget_path=tmp_path / "rate.json",
    )
    with TestClient(limited_app) as client:
        first = client.post(
            "/api/ask",
            json={"question": "what is the risk of changing demo?", "mode": "llm"},
        )
        second = client.post(
            "/api/ask",
            json={"question": "what is the risk of changing demo?", "mode": "llm"},
        )
        assert first.status_code == 200
        assert first.json()["mode"] == "deterministic-fallback"
        assert second.status_code == 429

    exhausted_client = UnavailableClient()
    exhausted_app = create_app(
        graph=api_graph,
        llm_client=exhausted_client,
        provider_probe=lambda: [],
        daily_limit=0,
        daily_budget_path=tmp_path / "exhausted.json",
    )
    with TestClient(exhausted_app) as client:
        response = client.post(
            "/api/ask",
            json={"question": "who owns demo?", "mode": "auto"},
        )
    assert response.status_code == 200
    assert response.json()["mode"] == "deterministic-fallback (llm budget exhausted)"
    assert response.json()["calls"] == 0
    assert exhausted_client.calls == 0


@pytest.mark.real_graph
def test_real_graph_api_ask_smoke_routes_and_tamper() -> None:
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph("provenance_ha")
    app = create_app(graph=graph, provider_probe=lambda: [], daily_budget_path=Path(".pytest-temp/api-real.json"))
    measurements = []
    with TestClient(app) as client:
        for question in (
            "what is the risk of changing sensor?",
            "who should review PR 179264?",
            "what happens if epenet leaves?",
        ):
            response = client.post(
                "/api/ask",
                json={"question": question, "mode": "deterministic"},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["claims"]
            assert all(claim["status"] == "VERIFIED" for claim in payload["claims"])
            proof_counts = [
                {
                    "nodes": f"{claim['proof']['subgraph']['node_count_returned']}/"
                    f"{claim['proof']['subgraph']['node_count_total']}",
                    "edges": f"{claim['proof']['subgraph']['edge_count_returned']}/"
                    f"{claim['proof']['subgraph']['edge_count_total']}",
                }
                for claim in payload["claims"]
            ]
            assert all(
                item["proof"]["subgraph"]["node_count_returned"]
                + item["proof"]["subgraph"]["edge_count_returned"]
                > 0
                for item in payload["claims"]
                if item["proof"]["subgraph"]["node_count_total"]
                + item["proof"]["subgraph"]["edge_count_total"]
                > 0
            )
            rendered_lengths = [
                len(claim["proof"]["rendered_cypher"])
                for claim in payload["claims"]
            ]
            assert max(rendered_lengths, default=0) < 2000
            if question == "what happens if epenet leaves?":
                displays = {
                    node["props"].get("login") or node["props"].get("key")
                    for claim in payload["claims"]
                    for node in claim["proof"]["subgraph"]["nodes"]
                }
                assert "epenet" in displays
                assert {
                    "integration:binary_sensor",
                    "integration:light",
                    "integration:switch",
                } <= displays
            measurements.append(
                {
                    "question": question,
                    "claims": len(payload["claims"]),
                    "statuses": [claim["status"] for claim in payload["claims"]],
                    "rendered_cypher_total_chars": sum(rendered_lengths),
                    "rendered_cypher_max_chars": max(rendered_lengths, default=0),
                    "node_counts_returned_total": sum(
                        item["proof"]["subgraph"]["node_count_returned"]
                        for item in payload["claims"]
                    ),
                    "node_counts_total": sum(
                        item["proof"]["subgraph"]["node_count_total"]
                        for item in payload["claims"]
                    ),
                    "edge_counts_returned_total": sum(
                        item["proof"]["subgraph"]["edge_count_returned"]
                        for item in payload["claims"]
                    ),
                    "edge_counts_total": sum(
                        item["proof"]["subgraph"]["edge_count_total"]
                        for item in payload["claims"]
                    ),
                    "proof_counts_by_claim": proof_counts,
                    "response_kb": round(len(response.content) / 1024, 2),
                    "tool_timing_seconds": round(
                        sum(item["seconds"] for item in payload["tool_timings"]), 4
                    ),
                }
            )
        tampered = client.post(
            "/api/ask",
            json={"question": "what is the risk of changing sensor?", "tamper": 1},
        )
        assert tampered.status_code == 200
        assert tampered.json()["tampered_fact_id"]
        assert any(claim["status"] == "CONTRADICTED" for claim in tampered.json()["claims"])
    print(f"real_graph_api_proof_measurements={measurements}")
    assert len(measurements) == 3