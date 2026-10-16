from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from falkordb import FalkorDB

from provenance.config import settings
from provenance.eval.reviewer_eval import (
    load_ground_truth,
    load_static_graph_data,
    load_targets,
    prepare_split,
)
from provenance.eval.reviewer_eval_round2 import (
    ROUND2_RESULTS_PATH,
    _per_pr_rankings,
)
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph
from provenance.tools.base import (
    ToolContext,
    extract_bus_module_summary,
    extract_module_contributors,
    extract_ownership_drift,
    recompute_fact,
)
from provenance.tools.config import DEFAULT_TOOL_CONFIG
from provenance.tools.queries import QUERY_REGISTRY
from provenance.tools.registry import (
    TOOL_REGISTRY,
    mcp_tool_definitions,
    openai_tool_definitions,
)
from provenance.tools.analysis import (
    blast_radius,
    bus_factor,
    criticality,
    decision_trace,
    maintainer_loss,
    module_profile,
    ownership_drift,
    suggest_reviewers,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


@pytest.fixture(scope="module")
def tool_graph():
    graph_name = f"provenance_tool_base_{uuid4().hex}"
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


def test_context_pins_max_timestamp_and_activity_query_filters_bots(tool_graph) -> None:
    context = ToolContext(
        tool_graph,
        tool_name="test",
        args={},
        config=DEFAULT_TOOL_CONFIG,
    )
    rows = context.query(
        "activity.rows@1",
        {
            "activity_start_ts": 0,
            "as_of_ts": context.as_of_ts,
            "module_key": "integration:demo",
            "person_login": None,
            "module_keys": None,
            "top_n": DEFAULT_TOOL_CONFIG.default_top_n,
        },
    )

    assert context.as_of_ts == context.graph_max_ts
    assert {row[2] for row in rows} == {"author-user", "human-reviewer"}
    assert all(row[2] not in {"fixture-bot", "review-bot"} for row in rows)
    assert context.warnings


def test_tool_fact_reexecutes_its_registry_query(tool_graph) -> None:
    context = ToolContext(tool_graph, tool_name="test", args={})
    params = {
        "module_key": "integration:demo",
        "activity_start_ts": 0,
        "as_of_ts": context.as_of_ts - 1,
        "extract_column": 0,
    }
    fact = context.fact(
        text="The module's PR count is reproduced by its registered query.",
        unit="PRs",
        query_id="module.pr_count@1",
        params=params,
        extractor="scalar.column@1",
    )

    assert fact.value == 1
    assert recompute_fact(tool_graph, fact) == fact.value


def test_registry_extractors_filter_bots_and_do_not_count_team_as_person(tool_graph) -> None:
    context = ToolContext(tool_graph, tool_name="test", args={})
    rows = context.query(
        "activity.rows@1",
        {
            "activity_start_ts": 0,
            "as_of_ts": context.as_of_ts,
            "module_key": "integration:demo",
            "person_login": None,
            "module_keys": None,
            "top_n": 15,
        },
    )
    contributors = extract_module_contributors(
        rows,
        {"extract_module_key": "integration:demo", "extract_top_n": 5},
    )
    assert {person["login"] for person in contributors} == {
        "author-user",
        "human-reviewer",
    }

    drift_rows = context.query(
        "ownership.drift.inputs@1",
        {"module_key": "integration:demo", "activity_start_ts": 0, "as_of_ts": context.as_of_ts},
    )
    drift = extract_ownership_drift(
        drift_rows,
        {"module_key": "integration:demo", "extract_top_n": 5},
    )
    team_handles = {owner["handle"] for owner in drift["owners"] if owner["kind"] == "team"}
    assert team_handles
    assert not (team_handles & set(drift["declared_person_owners"]))
    assert "review-bot" not in {person["login"] for person in drift["behavioral_top"]}

    bus_rows = context.query(
        "bus_factor.inputs@1",
        {
            "module_key": "integration:demo",
            "activity_start_ts": 0,
            "as_of_ts": context.as_of_ts,
            "top_n": 15,
            "bus_factor_share_threshold": 0.5,
            "bus_factor_min_prs": 10,
        },
    )
    bus_summary = extract_bus_module_summary(
        bus_rows,
        {
            "module_key": "integration:demo",
            "bus_factor_share_threshold": 0.5,
            "bus_factor_min_prs": 10,
        },
    )
    assert bus_summary["pr_count"] == 1


def test_module_profile_and_ownership_drift_facts_replay(tool_graph) -> None:
    for tool_result in (
        module_profile(tool_graph, module_key="integration:demo"),
        ownership_drift(tool_graph, module_key="integration:demo"),
    ):
        assert tool_result.as_of_ts > 0
        assert tool_result.warnings
        assert tool_result.facts
        for fact in tool_result.facts:
            assert recompute_fact(tool_graph, fact) == fact.value

    profile = module_profile(tool_graph, module_key="integration:demo")
    assert profile.result["pr_count_in_window"] == 1
    assert {owner["kind"] for owner in profile.result["declared_owners"]} == {
        "person",
        "team",
    }
    drift = ownership_drift(tool_graph, module_key="integration:demo")
    assert drift.result["flags"]["ownerless"] is False


def test_unknown_module_returns_close_matches(tool_graph) -> None:
    result = module_profile(tool_graph, module_key="integration:dem")

    assert "error" in result.result
    assert "integration:demo" in result.result["suggestions"]
    assert any(fact.extractor == "module.suggestions@1" for fact in result.facts)


def test_bus_factor_facts_replay_on_tiny_graph(tool_graph) -> None:
    result = bus_factor(tool_graph, module_key="integration:demo")

    assert result.result["modules"][0][0] == "integration:demo"
    assert result.result["modules"][0][1] == 1
    for fact in result.facts:
        assert recompute_fact(tool_graph, fact) == fact.value


def test_blast_radius_facts_replay_on_tiny_graph(tool_graph) -> None:
    result = blast_radius(
        tool_graph,
        module_key="integration:dependency",
        max_depth=2,
    )

    assert result.result["dependent_count"] == 1
    assert result.result["dependents"][0]["module_key"] == "integration:demo"
    assert result.result["dependents"][0]["depth"] == 1
    assert result.result["dependents"][0]["recent_pr_count"] == 1
    assert result.result["dependents"][0]["recent_human_count"] == 2
    assert result.result["dependents"][0]["activity_weight"] > 0
    for fact in result.facts:
        assert recompute_fact(tool_graph, fact) == fact.value


def test_criticality_facts_replay_on_tiny_graph(tool_graph) -> None:
    result = criticality(tool_graph, top_n=5)

    assert result.result["modules"]
    assert {item["module_key"] for item in result.result["modules"]} >= {
        "integration:demo",
        "integration:dependency",
    }
    for fact in result.facts:
        assert recompute_fact(tool_graph, fact) == fact.value


def test_suggest_reviewers_facts_replay_on_tiny_graph(tool_graph) -> None:
    by_modules = suggest_reviewers(
        tool_graph,
        modules=["integration:demo"],
        author_login="author-user",
    )
    by_pr = suggest_reviewers(tool_graph, pr_number=502)

    for result in (by_modules, by_pr):
        assert result.result["reviewers"][0]["login"] == "human-reviewer"
        assert "author-user" not in {
            reviewer["login"] for reviewer in result.result["reviewers"]
        }
        assert result.warnings
        for fact in result.facts:
            assert recompute_fact(tool_graph, fact) == fact.value
    target = next(fact.value for fact in by_pr.facts if fact.extractor == "reviewer.target@1")
    assert by_pr.as_of_ts == target["merged_ts"] - 1
    assert by_pr.result["reviewers"][0]["global_review_prs"] == [501]


def test_suggest_reviewers_matches_real_held_out_g3_top_three() -> None:
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph("provenance_ha")
    bounds = graph.ro_query(QUERY_REGISTRY["meta.graph_bounds@1"], {}).result_set
    if not bounds or bounds[0][1] is None:
        pytest.skip("The real provenance_ha graph is not loaded.")

    results = json.loads(ROUND2_RESULTS_PATH.read_text(encoding="utf-8"))
    fold = results["folds"]["B"]
    window_start = int(datetime.fromisoformat(fold["test_start_utc"].replace("Z", "+00:00")).timestamp())
    window_end = int(datetime.fromisoformat(fold["test_end_utc"].replace("Z", "+00:00")).timestamp())
    targets = load_targets(graph, window_start, window_end)
    reviewers, _ = load_ground_truth(graph, window_start, window_end)
    owners, dependencies, owner_is_bot = load_static_graph_data(graph)
    held_out = [
        target
        for number, target in sorted(
            targets.items(), key=lambda item: (item[1]["merged_ts"], item[0])
        )
        if not target["author_is_bot"]
        and reviewers.get(number)
        and any(kind == "integration" for kind in target["modules"].values())
    ][:3]
    assert len(held_out) == 3

    for target in held_out:
        merged_ts = target["merged_ts"]
        evaluator_records = prepare_split(
            graph,
            window_start=merged_ts,
            window_end=merged_ts + 1,
            primary_only=True,
            owners=owners,
            dependencies=dependencies,
            owner_is_bot=owner_is_bot,
        )
        evaluator_record = next(
            record for record in evaluator_records
            if record["target"]["number"] == target["number"]
        )
        expected = _per_pr_rankings(
            evaluator_record,
            float(fold["lambda"]),
        )["G3"][:3]
        actual = suggest_reviewers(
            graph,
            pr_number=target["number"],
            top_n=3,
        )
        assert [item["login"] for item in actual.result["reviewers"]] == expected


def test_maintainer_loss_facts_replay_on_tiny_graph(tool_graph) -> None:
    merged_ts = int(
        tool_graph.ro_query(QUERY_REGISTRY["meta.graph_bounds@1"], {}).result_set[0][1]
    )
    share_prs = list(range(96001, 96011))
    sole_prs = list(range(96011, 96014))
    tool_graph.query(
        """
        UNWIND $module_keys AS module_key
        CREATE (:Module {key: module_key, kind: 'integration', name: module_key})
        """,
        {
            "module_keys": [
                "integration:maintainer-share",
                "integration:maintainer-sole",
                "integration:maintainer-dependent",
            ]
        },
    )
    tool_graph.query(
        """
        UNWIND $logins AS login
        CREATE (:Person {login: login, is_bot: false})
        """,
        {"logins": ["solo-owner", "review-a", "review-b", "review-c"]},
    )
    tool_graph.query(
        """
        UNWIND $numbers AS number
        CREATE (:PR {number: number, title: 'maintainer fixture', merged_ts: $merged_ts})
        """,
        {"numbers": share_prs + sole_prs, "merged_ts": merged_ts},
    )
    tool_graph.query(
        """
        MATCH (author:Person {login: 'solo-owner'})
        UNWIND $numbers AS number
        MATCH (pr:PR {number: number})
        CREATE (author)-[:AUTHORED {ts: $merged_ts}]->(pr)
        """,
        {"numbers": share_prs + sole_prs, "merged_ts": merged_ts},
    )
    tool_graph.query(
        """
        UNWIND $touches AS item
        MATCH (pr:PR {number: item.number})
        MATCH (module:Module {key: item.module_key})
        CREATE (pr)-[:TOUCHES]->(module)
        """,
        {
            "touches": [
                *[
                    {"number": number, "module_key": "integration:maintainer-share"}
                    for number in share_prs
                ],
                *[
                    {"number": number, "module_key": "integration:maintainer-sole"}
                    for number in sole_prs
                ],
            ]
        },
    )
    tool_graph.query(
        """
        UNWIND $reviews AS item
        MATCH (reviewer:Person {login: item.login})
        MATCH (pr:PR {number: item.number})
        CREATE (reviewer)-[:REVIEWED {state: 'APPROVED', ts: $merged_ts}]->(pr)
        """,
        {
            "reviews": [
                *[{"login": "review-a", "number": number} for number in share_prs[:4]],
                *[{"login": "review-b", "number": number} for number in share_prs[4:7]],
                *[{"login": "review-c", "number": number} for number in share_prs[7:]],
            ],
            "merged_ts": merged_ts,
        },
    )
    tool_graph.query(
        """
        MATCH (dependent:Module {key: 'integration:maintainer-dependent'})
        MATCH (target:Module {key: 'integration:maintainer-share'})
        MERGE (dependent)-[edge:DEPENDS_ON {kind: 'dependency'}]->(target)
        RETURN id(edge)
        """
    )
    assert tool_graph.ro_query(
        QUERY_REGISTRY["maintainer.dependents.depth2@1"],
        {"module_keys": ["integration:maintainer-share"], "max_depth": 2},
    ).result_set

    reviewer_loss = maintainer_loss(tool_graph, person_login="review-c")
    reviewer_module = reviewer_loss.result["modules"][0]
    assert reviewer_module["module_key"] == "integration:maintainer-share"
    assert reviewer_module["review_share_crossed_threshold"] is True
    assert reviewer_module["dependent_count"] == 1
    assert reviewer_module["review_share_before"] == pytest.approx(0.4)
    assert reviewer_module["review_share_after"] == pytest.approx(4 / 7)

    owner_loss = maintainer_loss(tool_graph, person_login="solo-owner")
    sole_module = next(
        item for item in owner_loss.result["modules"]
        if item["module_key"] == "integration:maintainer-sole"
    )
    assert sole_module["became_uncovered"] is True
    for result in (reviewer_loss, owner_loss):
        for fact in result.facts:
            assert recompute_fact(tool_graph, fact) == fact.value


def test_decision_trace_facts_replay_on_tiny_graph(tool_graph) -> None:
    tool_graph.query(
        """
        UNWIND $decisions AS item
        CREATE (:Decision {
            id: item.id,
            title: item.title,
            status: 'accepted',
            source: item.source
        })
        """,
        {
            "decisions": [
                {
                    "id": "fixture-adr-new",
                    "title": "New fixture decision",
                    "source": "fixture/adr/0002.md",
                },
                {
                    "id": "fixture-adr-old",
                    "title": "Old fixture decision",
                    "source": "fixture/adr/0001.md",
                },
            ]
        },
    )
    tool_graph.query(
        """
        MATCH (newer:Decision {id: 'fixture-adr-new'})
        MATCH (older:Decision {id: 'fixture-adr-old'})
        MERGE (newer)-[edge:SUPERSEDES]->(older)
        RETURN id(edge)
        """
    )
    tool_graph.query(
        """
        MATCH (pr:PR {number: 501})
        MATCH (decision:Decision {id: 'fixture-adr-new'})
        MERGE (pr)-[edge:DECIDED_AS]->(decision)
        RETURN id(edge)
        """
    )

    by_module = decision_trace(tool_graph, module_key="integration:demo")
    by_pr = decision_trace(tool_graph, pr_number=501)
    for result in (by_module, by_pr):
        assert result.result["decisions"]
        assert all(item["source"] for item in result.result["decisions"])
        for fact in result.facts:
            assert recompute_fact(tool_graph, fact) == fact.value

    traced = next(
        item for item in by_pr.result["decisions"]
        if item["decision_id"] == "fixture-adr-new"
    )
    assert traced["relation"] == "DECIDED_AS"
    assert traced["source"] == "fixture/adr/0002.md"
    assert traced["supersedes"] == [
        {
            "decision_id": "fixture-adr-old",
            "title": "Old fixture decision",
            "status": "accepted",
            "source": "fixture/adr/0001.md",
        }
    ]
    assert any(item["relation"] == "REFERENCES" for item in by_pr.result["decisions"])


def test_tool_registry_exports_eight_function_schemas() -> None:
    expected = {
        "module_profile",
        "ownership_drift",
        "bus_factor",
        "blast_radius",
        "criticality",
        "suggest_reviewers",
        "maintainer_loss",
        "decision_trace",
    }
    assert set(TOOL_REGISTRY) == expected
    assert {tool["name"] for tool in mcp_tool_definitions()} == expected
    assert {
        tool["function"]["name"] for tool in openai_tool_definitions()
    } == expected
    assert all(
        tool["description"].strip()
        and "\n" not in tool["description"]
        and tool["inputSchema"]["type"] == "object"
        for tool in TOOL_REGISTRY.values()
    )