from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from falkordb import FalkorDB

from provenance.config import settings
from provenance.ingest.graph_loader import DEFAULT_GRAPH
from provenance.tools.analysis import blast_radius, bus_factor, criticality, maintainer_loss, ownership_drift
from provenance.tools.base import Fact, ToolContext
from provenance.tools.config import DEFAULT_TOOL_CONFIG
from provenance.tools.queries import QUERY_REGISTRY


RESULTS_PATH = ROOT / "results" / "risk_report.json"


def _fact(result: Any, extractor: str) -> Fact:
    return next(fact for fact in result.facts if fact.extractor == extractor)


def _fact_record(fact: Fact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "text": fact.text,
        "value": fact.value,
        "query_id": fact.query_id,
    }


def _timed_call(
    timings: list[dict[str, Any]],
    tool: str,
    args: dict[str, Any],
    call: Callable[[], Any],
) -> Any:
    started = time.perf_counter()
    result = call()
    timing = {key: value for key, value in args.items() if key in {"module_key", "person_login", "top_n", "max_depth"}}
    timings.append({"tool": tool, **timing, "seconds": round(time.perf_counter() - started, 4)})
    return result


def _criticality_fact(graph: Any, module_key: str, as_of_ts: int, timings: list[dict[str, Any]]) -> Fact:
    context = ToolContext(
        graph,
        tool_name="risk_report_criticality_fact",
        args={"module_key": module_key, "as_of_ts": as_of_ts},
        requested_as_of_ts=as_of_ts,
    )
    return _timed_call(
        timings,
        "criticality.score_fact",
        {"module_key": module_key},
        lambda: context.fact(
            text=f"PageRank score for {module_key} in the dependency graph.",
            unit="page_rank_score",
            query_id="criticality.page_rank@1",
            params={
                "module_label": "Module",
                "relationship_type": "DEPENDS_ON",
                "module_kinds": ["integration", "core"],
                "extract_module_key": module_key,
            },
            extractor="criticality.score@1",
        ),
    )


def build_report(graph: Any, *, output_path: Path = RESULTS_PATH) -> dict[str, Any]:
    timings: list[dict[str, Any]] = []
    bus_all = _timed_call(
        timings,
        "bus_factor",
        {"top_n": DEFAULT_TOOL_CONFIG.default_top_n},
        lambda: bus_factor(graph, top_n=DEFAULT_TOOL_CONFIG.default_top_n),
    )
    as_of_ts = bus_all.as_of_ts
    criticality_all = _timed_call(
        timings,
        "criticality",
        {"top_n": 2000, "as_of_ts": as_of_ts},
        lambda: criticality(graph, top_n=2000, as_of_ts=as_of_ts),
    )

    bus_candidates = [item[0] for item in bus_all.result["modules"][:15]]
    criticality_candidates = [
        item["module_key"] for item in criticality_all.result["modules"][:15]
    ]
    candidate_keys = sorted(set(bus_candidates) | set(criticality_candidates))
    criticality_by_key = {
        item["module_key"]: float(item["page_rank"])
        for item in criticality_all.result["modules"]
    }
    max_criticality = max(criticality_by_key.values(), default=0.0)

    candidates: list[dict[str, Any]] = []
    tool_results: dict[str, dict[str, Any]] = {}
    for module_key in candidate_keys:
        module_results = {
            "bus_factor": _timed_call(
                timings,
                "bus_factor",
                {"module_key": module_key, "as_of_ts": as_of_ts},
                lambda module_key=module_key: bus_factor(
                    graph, module_key=module_key, as_of_ts=as_of_ts
                ),
            ),
            "ownership_drift": _timed_call(
                timings,
                "ownership_drift",
                {"module_key": module_key, "as_of_ts": as_of_ts},
                lambda module_key=module_key: ownership_drift(
                    graph, module_key=module_key, as_of_ts=as_of_ts
                ),
            ),
            "blast_radius": _timed_call(
                timings,
                "blast_radius",
                {"module_key": module_key, "max_depth": 2, "as_of_ts": as_of_ts},
                lambda module_key=module_key: blast_radius(
                    graph, module_key=module_key, max_depth=2, as_of_ts=as_of_ts
                ),
            ),
        }
        tool_results[module_key] = module_results
        bus_records = module_results["bus_factor"].result["modules"]
        bus_record = next((item for item in bus_records if item[0] == module_key), None)
        top_share = float(bus_record[5]) if bus_record else 0.0
        bus_flag = bool(bus_record[6]) if bus_record else False
        ownership_result = module_results["ownership_drift"].result
        ownership_flags = ownership_result["flags"]
        stale_owners = ownership_flags["stale_declared_owners"]
        ownerless = bool(ownership_flags["ownerless"])
        drift_score = float(ownership_result["drift_score"])
        declared_owners = [
            {
                "kind": owner["kind"],
                "identity": owner.get("handle") or owner.get("login"),
            }
            for owner in ownership_result["declared_owners"]
        ]
        top_behavioral_contributor = (
            ownership_result["behavioral_top5"][0]
            if ownership_result["behavioral_top5"]
            else None
        )
        page_rank = criticality_by_key.get(module_key, 0.0)
        dependent_count = int(module_results["blast_radius"].result["dependent_count"])
        candidates.append(
            {
                "module_key": module_key,
                "bus_factor_share": top_share,
                "bus_factor_risk": bus_flag,
                "ownership_drift": drift_score,
                "ownerless": ownerless,
                "stale_declared_owners": stale_owners,
                "declared_owners": declared_owners,
                "top_behavioral_contributor": top_behavioral_contributor,
                "page_rank": page_rank,
                "dependent_count": dependent_count,
                "tools": module_results,
            }
        )

    max_dependents = max((item["dependent_count"] for item in candidates), default=0)
    weights = {
        "bus_factor": DEFAULT_TOOL_CONFIG.risk_bus_factor_weight,
        "ownership_drift": DEFAULT_TOOL_CONFIG.risk_ownership_drift_weight,
        "criticality": DEFAULT_TOOL_CONFIG.risk_criticality_weight,
        "blast_radius": DEFAULT_TOOL_CONFIG.risk_blast_radius_weight,
    }
    weight_total = sum(weights.values())
    for item in candidates:
        normalized_criticality = item["page_rank"] / max_criticality if max_criticality else 0.0
        normalized_blast = item["dependent_count"] / max_dependents if max_dependents else 0.0
        weighted_score = (
            weights["bus_factor"] * item["bus_factor_share"]
            + weights["ownership_drift"] * item["ownership_drift"]
            + weights["criticality"] * normalized_criticality
            + weights["blast_radius"] * normalized_blast
        )
        item["components"] = {
            "bus_factor_concentration": item["bus_factor_share"],
            "ownership_drift": item["ownership_drift"],
            "criticality_normalized": normalized_criticality,
            "blast_radius_normalized": normalized_blast,
        }
        item["risk_score"] = weighted_score / weight_total if weight_total else 0.0
        item["flags"] = {
            "bus_factor": item["bus_factor_risk"],
            "missing_or_stale_owner": item["ownerless"] or bool(stale_owners),
            "criticality_top_15": item["module_key"] in set(criticality_candidates),
            "has_reverse_dependents": item["dependent_count"] > 0,
        }
        del item["tools"]
    candidates.sort(key=lambda item: (-item["risk_score"], item["module_key"]))
    top_modules = candidates[:15]

    reviewer_context = ToolContext(
        graph,
        tool_name="risk_report_top_reviewers",
        args={"as_of_ts": as_of_ts},
        requested_as_of_ts=as_of_ts,
    )
    reviewer_fact = _timed_call(
        timings,
        "maintainer.top_reviewers",
        {"top_n": 10, "as_of_ts": as_of_ts},
        lambda: reviewer_context.fact(
            text="Ten most active human reviewers in the configured activity window.",
            unit="reviewer_records",
            query_id="maintainer.top_reviewers@1",
            params={
                "activity_start_ts": reviewer_context.activity_start_ts,
                "as_of_ts": as_of_ts,
                "extract_top_n": 10,
            },
            extractor="maintainer.top_reviewers@1",
        ),
    )
    maintainer_risks = []
    for reviewer in reviewer_fact.value:
        login = reviewer["login"]
        result = _timed_call(
            timings,
            "maintainer_loss",
            {"person_login": login, "as_of_ts": as_of_ts},
            lambda login=login: maintainer_loss(graph, person_login=login, as_of_ts=as_of_ts),
        )
        impacted = result.result.get("modules", [])
        uncovered = sorted(
            item["module_key"] for item in impacted if item["became_uncovered"]
        )
        distinct_dependents = int(result.result["distinct_dependents_affected"])
        summed_dependent_edges = int(result.result["summed_dependent_edges"])
        top_affected = sorted(
            impacted,
            key=lambda item: (-int(item["dependent_count"]), item["module_key"]),
        )[:3]
        maintainer_risks.append(
            {
                "person_login": login,
                "reviewed_pr_count": reviewer["reviewed_pr_count"],
                "uncovered_module_count": len(uncovered),
                "uncovered_modules": uncovered,
                "distinct_dependents_affected": distinct_dependents,
                "summed_dependent_edges": summed_dependent_edges,
                "top_three_affected_modules": [
                    {
                        "module_key": item["module_key"],
                        "dependent_count": item["dependent_count"],
                        "became_uncovered": item["became_uncovered"],
                    }
                    for item in top_affected
                ],
            }
        )
    maintainer_risks.sort(
        key=lambda item: (
            -item["distinct_dependents_affected"],
            -item["uncovered_module_count"],
            item["person_login"],
        )
    )

    def ownership_drift_top_five() -> list[dict[str, Any]]:
        context = ToolContext(
            graph,
            tool_name="risk_report_live_ownership_drift",
            args={"as_of_ts": as_of_ts},
            requested_as_of_ts=as_of_ts,
        )
        module_catalog = context.fact(
            text="All module keys in the selected graph.",
            unit="module_keys",
            query_id="modules.list@1",
            params={"extract_column": 0},
            extractor="rows.column_values@1",
        )
        module_pr_counts = context.fact(
            text="Recent distinct PR counts for every module in the graph.",
            unit="module_pr_counts",
            query_id="module.pr_counts@1",
            params={
                "module_keys": module_catalog.value,
                "activity_start_ts": context.activity_start_ts,
                "as_of_ts": as_of_ts,
            },
            extractor="module.pr_count_map@1",
        )
        eligible_keys = sorted(
            key for key, count in module_pr_counts.value.items() if count >= 10
        )
        if not eligible_keys:
            return []
        drift_fact = context.fact(
            text="Live ownership drift records for modules with at least ten recent PRs.",
            unit="ownership_drift_records",
            query_id="ownership.drift.inputs.all@1",
            params={
                "module_keys": eligible_keys,
                "activity_start_ts": context.activity_start_ts,
                "as_of_ts": as_of_ts,
                "extract_top_n": 5,
                "ownership_drift_ownerless_weight": DEFAULT_TOOL_CONFIG.ownership_drift_ownerless_weight,
                "ownership_drift_stale_weight": DEFAULT_TOOL_CONFIG.ownership_drift_stale_weight,
                "ownership_drift_outside_top5_weight": DEFAULT_TOOL_CONFIG.ownership_drift_outside_top5_weight,
            },
            extractor="ownership.drift_many@1",
        )
        return sorted(
            [
                {
                    **item,
                    "recent_pr_count": module_pr_counts.value[item["module_key"]],
                    "top_behavioral_contributors": item["behavioral_top"][:5],
                }
                for item in drift_fact.value
            ],
            key=lambda item: (-item["drift_score"], item["module_key"]),
        )[:5]

    ownership_context_result = _timed_call(
        timings,
        "ownership_drift.top_five_live",
        {"minimum_recent_prs": 10},
        ownership_drift_top_five,
    )

    top5_evidence = []
    for module in top_modules[:5]:
        module_key = module["module_key"]
        results = tool_results[module_key]
        criticality_fact = _criticality_fact(graph, module_key, as_of_ts, timings)
        facts = [
            _fact(results["bus_factor"], "bus.ranking@1"),
            _fact(results["ownership_drift"], "ownership.drift_flags@1"),
            _fact(results["ownership_drift"], "ownership.drift_score@1"),
            criticality_fact,
            _fact(results["blast_radius"], "blast.dependent_count@1"),
        ]
        top5_evidence.append(
            {
                "module_key": module_key,
                "risk_score": module["risk_score"],
                "flags": module["flags"],
                "facts": [_fact_record(fact) for fact in facts],
            }
        )

    report = {
        "graph": DEFAULT_GRAPH,
        "as_of_ts": as_of_ts,
        "candidate_pool": {
            "rule": "Union of the top 15 modules by review concentration and top 15 by PageRank.",
            "module_count": len(candidate_keys),
        },
        "score_formula": "weighted average of bus-factor top-person review share, ownership drift score, PageRank normalized by the candidate maximum, and distinct reverse-dependent count normalized by the candidate maximum",
        "weights": weights,
        "normalization": {
            "criticality_max_page_rank": max_criticality,
            "blast_radius_max_dependent_count": max_dependents,
        },
        "top_15_modules": top_modules,
        "top_5_live_ownership_drift": ownership_context_result,
        "top_5_exact_facts": top5_evidence,
        "single_person_risks": maintainer_risks[:5],
        "top_10_reviewers_fact": _fact_record(reviewer_fact),
        "tool_call_timings": timings,
        "tool_calls_over_2_seconds": [item for item in timings if item["seconds"] > 2.0],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph(DEFAULT_GRAPH)
    report = build_report(graph)
    print(
        json.dumps(
            {
                "output": str(RESULTS_PATH.relative_to(ROOT)),
                "candidate_pool": report["candidate_pool"],
                "top_15_modules": [
                    {
                        "rank": rank,
                        "module_key": item["module_key"],
                        "risk_score": item["risk_score"],
                        "components": item["components"],
                        "declared_owners": item["declared_owners"],
                        "top_behavioral_contributor": item["top_behavioral_contributor"],
                    }
                    for rank, item in enumerate(report["top_15_modules"], start=1)
                ],
                "top_5_exact_facts": report["top_5_exact_facts"],
                "five_single_person_risks": report["single_person_risks"],
                "top_5_live_ownership_drift": report["top_5_live_ownership_drift"],
                "tool_calls_over_2_seconds": report["tool_calls_over_2_seconds"],
                "tool_call_count": len(report["tool_call_timings"]),
                "max_tool_seconds": max(
                    (item["seconds"] for item in report["tool_call_timings"]),
                    default=0.0,
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
