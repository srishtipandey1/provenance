from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

import pytest
from falkordb import FalkorDB

from provenance.config import settings
from provenance.ingest.graph_loader import delete_graph_if_exists, load_graph
from provenance.tools.analysis import module_profile, ownership_drift
from provenance.tools.base import Fact
from provenance.tools.config import DEFAULT_TOOL_CONFIG
from provenance.agents.verifier import (
    AnswerDraft,
    Claim,
    CONTRADICTED,
    UNSUPPORTED,
    VERIFIED,
    qualified_fact_id,
    render_claim_proof,
    verify,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "graph_loader"


@pytest.fixture(scope="module")
def verifier_graph():
    graph_name = f"provenance_verifier_{uuid4().hex}"
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


def _fact(result, extractor: str):
    return next(fact for fact in result.facts if fact.extractor == extractor)


def test_good_claim_verifies_with_proof_and_snapshot_caveat(verifier_graph) -> None:
    result = ownership_drift(verifier_graph, module_key="integration:demo")
    fact = _fact(result, "ownership.drift_score@1")
    claim = Claim(
        claim_id="C1",
        text="The ownership drift score is supported by the graph snapshot.",
        fact_ids=[fact.fact_id],
    )

    verified = verify(AnswerDraft("What is the drift?", [claim], [result]), verifier_graph)

    assert verified.claims[0].status == VERIFIED
    assert verified.summary_counts[VERIFIED] == 1
    assert verified.final_answer.startswith(claim.text)
    assert verified.claims[0].caveats
    bundle = verified.proof_bundle["C1"]
    assert "ownership.drift.inputs@1" in bundle["query_ids"]
    assert bundle["params"]
    assert bundle["node_ids"]
    assert bundle["edge_ids"]
    snippet = render_claim_proof(verified.claims[0])
    assert "MATCH" in snippet
    assert "$module_key" not in snippet


def test_tampered_fact_is_contradicted(verifier_graph) -> None:
    result = module_profile(verifier_graph, module_key="integration:demo")
    fact = _fact(result, "module.pr_count_value@1")
    tampered_result = copy.deepcopy(result)
    tampered_fact = next(item for item in tampered_result.facts if item.fact_id == fact.fact_id)
    tampered_fact.value = 999
    claim = Claim("C2", "integration:demo has 1 recent PR.", [fact.fact_id])

    verified = verify(
        AnswerDraft("How many PRs?", [claim], [tampered_result]),
        verifier_graph,
    )

    assert verified.claims[0].status == CONTRADICTED
    assert "stored=999" in verified.claims[0].reasons[0]
    assert "recomputed=1" in verified.claims[0].reasons[0]
    assert verified.final_answer == ""
    assert verified.removed_claims[0]["claim_id"] == "C2"


def test_missing_fact_and_ungrounded_number_are_unsupported(verifier_graph) -> None:
    result = module_profile(verifier_graph, module_key="integration:demo")
    count_fact = _fact(result, "module.pr_count_value@1")
    missing = Claim("C3", "There is a supported PR count.", ["absent:F99"])
    ungrounded = Claim("C4", "integration:demo has 999 recent PRs.", [count_fact.fact_id])
    no_citation = Claim("C5", "The module is important.", [])

    verified = verify(
        AnswerDraft("Check claims", [missing, ungrounded, no_citation], [result]),
        verifier_graph,
    )

    assert [claim.status for claim in verified.claims] == [UNSUPPORTED] * 3
    assert any("absent:F99" in reason for reason in verified.claims[0].reasons)
    assert any("999" in reason for reason in verified.claims[1].reasons)
    assert any("no cited facts" in reason for reason in verified.claims[2].reasons)
    assert verified.summary_counts[UNSUPPORTED] == 3
    assert [item["claim_id"] for item in verified.removed_claims] == ["C3", "C4", "C5"]


def test_float_tolerance_and_fact_id_qualification(verifier_graph) -> None:
    first = ownership_drift(verifier_graph, module_key="integration:demo")
    second = module_profile(verifier_graph, module_key="integration:demo")
    score_fact = _fact(first, "ownership.drift_score@1")
    assert score_fact.value != 0.0
    score_fact.value += abs(score_fact.value) * 5e-10
    count_fact = _fact(second, "module.pr_count_value@1")
    results = [first, second]
    claim = Claim(
        "C6",
        "The drift score and PR count are grounded.",
        [
            qualified_fact_id(0, first, score_fact),
            qualified_fact_id(1, second, count_fact),
        ],
    )
    rounded_percent = Claim(
        "C8",
        f"The ownership drift score is {round(score_fact.value * 100):.0f}%.",
        [qualified_fact_id(0, first, score_fact)],
    )

    verified = verify(
        AnswerDraft("Check tolerance", [claim, rounded_percent], results),
        verifier_graph,
    )

    assert verified.claims[0].status == VERIFIED
    assert verified.claims[1].status == VERIFIED
    assert verified.claims[0].proof_bundle["as_of_ts"] == first.as_of_ts


def test_conflicting_as_of_pins_are_reported(verifier_graph) -> None:
    maximum = module_profile(verifier_graph, module_key="integration:demo")
    earlier = module_profile(
        verifier_graph,
        module_key="integration:demo",
        as_of_ts=maximum.as_of_ts - 1,
    )
    claim = Claim(
        "C7",
        "The profile facts have compatible timestamps.",
        [
            qualified_fact_id(0, maximum, _fact(maximum, "module.pr_count_value@1")),
            qualified_fact_id(1, earlier, _fact(earlier, "module.pr_count_value@1")),
        ],
    )

    verified = verify(AnswerDraft("Check timestamps", [claim], [maximum, earlier]), verifier_graph)

    assert verified.claims[0].status == UNSUPPORTED
    assert any("disagree on as_of_ts" in reason for reason in verified.claims[0].reasons)


@pytest.mark.real_graph
def test_real_graph_verifier_replays_profile_fact() -> None:
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph("provenance_ha")
    result = module_profile(graph, module_key="integration:sensor")
    count_fact = _fact(result, "module.pr_count_value@1")
    metadata_fact = _fact(result, "module.metadata_value@1")
    claim = Claim(
        "REAL-C1",
        f"{metadata_fact.value[0]} has {count_fact.value} recent PRs.",
        [count_fact.fact_id, metadata_fact.fact_id],
    )

    verified = verify(AnswerDraft("Real graph verifier smoke", [claim], [result]), graph)

    assert verified.claims[0].status == VERIFIED
    assert verified.summary_counts[VERIFIED] == 1
