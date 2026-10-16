from __future__ import annotations

from typing import Any

from provenance.tools.base import ToolContext, ToolResult
from provenance.tools.config import DEFAULT_TOOL_CONFIG, ToolConfig
from provenance.tools.queries import QUERY_REGISTRY


def _context(
    graph: Any,
    tool_name: str,
    args: dict[str, Any],
    *,
    config: ToolConfig,
) -> ToolContext:
    return ToolContext(
        graph,
        tool_name=tool_name,
        args=args,
        requested_as_of_ts=args.get("as_of_ts"),
        config=config,
    )


def _unknown_module(context: ToolContext, module_key: str) -> ToolResult | None:
    rows = context.query("module.metadata@1", {"module_key": module_key})
    if rows:
        return None
    params = {"unknown_module_key": module_key}
    suggestions = context.fact(
        text="Closest known module keys for the requested module.",
        unit="module_keys",
        query_id="modules.list@1",
        params=params,
        extractor="module.suggestions@1",
    )
    message = f"Unknown module_key '{module_key}'."
    if suggestions.value:
        message += " Similar modules: " + ", ".join(suggestions.value)
    return context.finish({"error": message, "suggestions": suggestions.value})


def _activity_params(
    context: ToolContext,
    *,
    module_key: str | None = None,
    person_login: str | None = None,
    module_keys: list[str] | None = None,
    top_n: int | None = None,
) -> dict[str, Any]:
    return {
        "activity_start_ts": context.activity_start_ts,
        "as_of_ts": context.as_of_ts,
        "module_key": module_key,
        "person_login": person_login,
        "module_keys": module_keys,
        "top_n": top_n if top_n is not None else context.config.default_top_n,
    }


def module_profile(
    graph: Any,
    *,
    module_key: str,
    as_of_ts: int | None = None,
    config: ToolConfig = DEFAULT_TOOL_CONFIG,
) -> ToolResult:
    context = _context(
        graph,
        "module_profile",
        {"module_key": module_key, "as_of_ts": as_of_ts},
        config=config,
    )
    unknown = _unknown_module(context, module_key)
    if unknown:
        return unknown

    metadata = context.fact(
        text="Module key, kind, and display name from the graph.",
        unit="module_metadata",
        query_id="module.metadata@1",
        params={"module_key": module_key},
        extractor="module.metadata_value@1",
    )
    owners = context.fact(
        text="Declared Person and Team owners of this module; Teams are not treated as people.",
        unit="owners",
        query_id="module.owners@1",
        params={"module_key": module_key},
        extractor="module.owner_list@1",
    )
    pr_count = context.fact(
        text=f"Distinct PRs touching {module_key} in the configured activity window.",
        unit="PRs",
        query_id="module.pr_counts@1",
        params={
            "module_keys": [module_key],
            "activity_start_ts": context.activity_start_ts,
            "as_of_ts": context.as_of_ts,
            "extract_module_key": module_key,
        },
        extractor="module.pr_count_value@1",
    )
    contributor_params = _activity_params(context, module_key=module_key)
    contributors = context.fact(
        text=f"Top human contributors to {module_key}, with distinct authored/reviewed PR counts and review shares.",
        unit="people_and_review_share",
        query_id="activity.rows@1",
        params={**contributor_params, "extract_module_key": module_key, "extract_top_n": config.default_top_n},
        extractor="contributors.module@1",
    )
    decisions = context.fact(
        text=f"Decisions linked through REFERENCES or DECIDED_AS on PRs touching {module_key}.",
        unit="decisions",
        query_id="module.decisions@1",
        params={"module_key": module_key, "as_of_ts": context.as_of_ts, "extract_top_n": 25},
        extractor="module.decision_list@1",
    )
    if not contributors.value:
        context.warnings.append("This module has no human activity in the configured activity window.")
    return context.finish(
        {
            "module": metadata.value,
            "declared_owners": owners.value,
            "pr_count_in_window": pr_count.value,
            "top_behavioral_contributors": contributors.value,
            "linked_decisions": decisions.value,
        }
    )


def ownership_drift(
    graph: Any,
    *,
    module_key: str,
    as_of_ts: int | None = None,
    config: ToolConfig = DEFAULT_TOOL_CONFIG,
) -> ToolResult:
    context = _context(
        graph,
        "ownership_drift",
        {"module_key": module_key, "as_of_ts": as_of_ts},
        config=config,
    )
    unknown = _unknown_module(context, module_key)
    if unknown:
        return unknown
    params = {
        "module_key": module_key,
        "activity_start_ts": context.activity_start_ts,
        "as_of_ts": context.as_of_ts,
        "extract_top_n": 5,
        "ownership_drift_ownerless_weight": config.ownership_drift_ownerless_weight,
        "ownership_drift_stale_weight": config.ownership_drift_stale_weight,
        "ownership_drift_outside_top5_weight": config.ownership_drift_outside_top5_weight,
    }
    owner_fact = context.fact(
        text=f"Declared owners of {module_key}; Team entries remain separate from Person owners.",
        unit="owners",
        query_id="ownership.drift.inputs@1",
        params=params,
        extractor="ownership.owner_list@1",
    )
    top_fact = context.fact(
        text=f"Top five behavioral human contributors to {module_key} in the activity window.",
        unit="people_and_distinct_pr_counts",
        query_id="ownership.drift.inputs@1",
        params=params,
        extractor="ownership.behavioral_top@1",
    )
    flag_fact = context.fact(
        text="Ownerless, stale-owner, and declared-owner-outside-top-five flags for this module.",
        unit="flags_and_logins",
        query_id="ownership.drift.inputs@1",
        params=params,
        extractor="ownership.drift_flags@1",
    )
    score_fact = context.fact(
        text="Ownership drift score: weighted sum of ownerless status, stale-person-owner share, and person-owner-outside-top-five share; weights are pinned configuration facts.",
        unit="score_0_to_1",
        query_id="ownership.drift.inputs@1",
        params=params,
        extractor="ownership.drift_score@1",
    )
    summary = context.query("ownership.drift.inputs@1", params)
    from provenance.tools.base import extract_ownership_drift

    derived = extract_ownership_drift(summary, params)
    return context.finish(
        {
            "module_key": module_key,
            "declared_owners": owner_fact.value,
            "behavioral_top5": top_fact.value,
            "flags": {
                "ownerless": flag_fact.value[0],
                "stale_declared_owners": flag_fact.value[1],
                "declared_owner_not_in_top5": flag_fact.value[2],
            },
            "drift_score": score_fact.value,
            "drift_score_formula": "config.ownerless_weight*ownerless + config.stale_weight*stale_person_owner_share + config.outside_top5_weight*person_owner_outside_top5_share",
        }
    )


def bus_factor(
    graph: Any,
    *,
    module_key: str | None = None,
    top_n: int | None = None,
    as_of_ts: int | None = None,
    config: ToolConfig = DEFAULT_TOOL_CONFIG,
) -> ToolResult:
    top_n = top_n if top_n is not None else config.default_top_n
    context = _context(
        graph,
        "bus_factor",
        {"module_key": module_key, "top_n": top_n, "as_of_ts": as_of_ts},
        config=config,
    )
    if module_key is not None:
        unknown = _unknown_module(context, module_key)
        if unknown:
            return unknown
    params = {
        "module_key": module_key,
        "activity_start_ts": context.activity_start_ts,
        "as_of_ts": context.as_of_ts,
        "top_n": top_n,
        "bus_factor_share_threshold": config.bus_factor_share_threshold,
        "bus_factor_min_prs": config.bus_factor_min_prs,
        "extract_top_n": top_n,
    }
    ranking = context.fact(
        text="Modules ranked by the highest person's share of human review PR pairs; a risk flag uses configured minimum PR/share thresholds.",
        unit="module_review_concentration_records",
        query_id="bus_factor.inputs@1",
        params=params,
        extractor="bus.ranking@1",
    )
    return context.finish({"modules": ranking.value, "top_n": top_n})


def blast_radius(
    graph: Any,
    *,
    module_key: str,
    max_depth: int | None = None,
    as_of_ts: int | None = None,
    config: ToolConfig = DEFAULT_TOOL_CONFIG,
) -> ToolResult:
    max_depth = max_depth if max_depth is not None else config.default_max_depth
    if max_depth not in {1, 2}:
        raise ValueError("max_depth must be 1 or 2; the registry contains bounded depth-1/2 queries.")
    context = _context(
        graph,
        "blast_radius",
        {"module_key": module_key, "max_depth": max_depth, "as_of_ts": as_of_ts},
        config=config,
    )
    unknown = _unknown_module(context, module_key)
    if unknown:
        return unknown
    path_query = "blast.paths.depth1@1" if max_depth == 1 else "blast.paths.depth2@1"
    path_params = {"module_key": module_key, "max_depth": max_depth}
    count = context.fact(
        text=f"Distinct reverse dependency modules within {max_depth} dependency hops.",
        unit="modules",
        query_id=path_query,
        params={**path_params, "extract_target_key": module_key},
        extractor="blast.dependent_count@1",
    )
    dependent_rows = context.fact(
        text=f"Reverse dependency modules, nearest depth, and supporting dependency edge IDs for {module_key}.",
        unit="module_depth_and_edge_ids",
        query_id=path_query,
        params={**path_params, "extract_target_key": module_key},
        extractor="blast.dependent_list@1",
    )
    dependents = dependent_rows.value
    activity_fact_id = None
    if dependents:
        depth_by_module = {
            item["module_key"]: item["depth"] for item in dependents
        }
        activity = context.fact(
            text="Recent PR counts, distinct human counts, and depth-weighted activity for reverse dependents.",
            unit="module_activity_records",
            query_id="activity.rows@1",
            params={
                **_activity_params(context, module_keys=list(depth_by_module)),
                "extract_depth_by_module": depth_by_module,
            },
            extractor="blast.module_activities@1",
        )
        activity_by_module = {
            item["module_key"]: item for item in activity.value
        }
        activity_fact_id = activity.fact_id
    else:
        activity_by_module = {}
    dependents = []
    for dependent in dependent_rows.value:
        activity_record = activity_by_module[dependent["module_key"]]
        dependents.append(
            {
                "module_key": dependent["module_key"],
                "depth": dependent["depth"],
                "recent_pr_count": activity_record["recent_pr_count"],
                "recent_human_count": activity_record["recent_human_count"],
                "activity_weight": activity_record["weight"],
                "supporting_dependency_edge_ids": dependent["dependency_edge_ids"],
                "dependency_fact_id": dependent_rows.fact_id,
                "activity_fact_id": activity_fact_id,
            }
        )
    return context.finish(
        {
            "module_key": module_key,
            "max_depth": max_depth,
            "dependent_count": count.value,
            "dependents": dependents,
        }
    )


def criticality(
    graph: Any,
    *,
    top_n: int | None = None,
    as_of_ts: int | None = None,
    config: ToolConfig = DEFAULT_TOOL_CONFIG,
) -> ToolResult:
    top_n = top_n if top_n is not None else config.default_top_n
    context = _context(
        graph,
        "criticality",
        {"top_n": top_n, "as_of_ts": as_of_ts},
        config=config,
    )
    rank_params = {
        "module_label": "Module",
        "relationship_type": "DEPENDS_ON",
        "module_kinds": ["integration", "core"],
        "extract_top_n": top_n,
    }
    scores = context.fact(
        text="Top PageRank scores over DEPENDS_ON for integration/core modules.",
        unit="module_pagerank_records",
        query_id="criticality.page_rank@1",
        params=rank_params,
        extractor="criticality.top_scores@1",
    )
    module_keys = [item[0] for item in scores.value]
    degrees = context.fact(
        text="DEPENDS_ON in-degree for the PageRank-ranked integration/core modules.",
        unit="module_in_degree_records",
        query_id="criticality.in_degree@1",
        params={"module_kinds": ["integration", "core"], "extract_module_keys": module_keys, "extract_top_n": top_n},
        extractor="criticality.top_degrees@1",
    )
    degree_map = {item[0]: item[1] for item in degrees.value}
    modules = [
        {"module_key": key, "kind": kind, "page_rank": score, "in_degree": degree_map.get(key, 0)}
        for key, kind, score in scores.value
    ]
    return context.finish({"top_n": top_n, "modules": modules})


def suggest_reviewers(
    graph: Any,
    *,
    modules: list[str] | None = None,
    pr_number: int | None = None,
    author_login: str | None = None,
    top_n: int | None = None,
    as_of_ts: int | None = None,
    config: ToolConfig = DEFAULT_TOOL_CONFIG,
) -> ToolResult:
    if (modules is None) == (pr_number is None):
        raise ValueError("Provide exactly one of modules or pr_number.")
    top_n = top_n if top_n is not None else config.default_top_n
    actual_as_of_ts = as_of_ts
    target_rows: list[list[Any]] = []
    if pr_number is not None:
        target_query = QUERY_REGISTRY["reviewer.g3.target_pr@2"]
        target_rows = graph.ro_query(target_query, {"pr_number": pr_number}).result_set
        if target_rows:
            target_as_of_ts = int(target_rows[0][1]) - 1
            actual_as_of_ts = (
                target_as_of_ts
                if actual_as_of_ts is None
                else min(int(actual_as_of_ts), target_as_of_ts)
            )
    context = _context(
        graph,
        "suggest_reviewers",
        {
            "modules": modules,
            "pr_number": pr_number,
            "author_login": author_login,
            "top_n": top_n,
            "as_of_ts": actual_as_of_ts,
        },
        config=config,
    )
    target = None
    if pr_number is not None:
        target_fact = context.fact(
            text="Selected PR author, merge timestamp, and touched modules.",
            unit="target_pr_metadata",
            query_id="reviewer.g3.target_pr@2",
            params={"pr_number": pr_number},
            extractor="reviewer.target@1",
        )
        target = target_fact.value
        if not target:
            return context.finish({"error": f"PR {pr_number} was not found in the selected graph."})
        module_keys = [
            item["module_key"] for item in target["modules"]
            if item["kind"] != "infra"
        ]
        author_login = author_login or target["author"]
    else:
        module_keys = list(dict.fromkeys(str(module_key) for module_key in modules or []))
        if not module_keys:
            raise ValueError("modules must contain at least one module key.")
        for module_key in module_keys:
            unknown = _unknown_module(context, module_key)
            if unknown:
                return unknown
    module_keys = list(dict.fromkeys(module_keys))
    author_login = (author_login or "").casefold()
    params = {
        "expertise_start_ts": context.as_of_ts - config.expertise_window_days * 86400,
        "as_of_ts": context.as_of_ts,
        "b3_half_life_seconds": config.recent_half_life_days * 86400,
        "g1_half_life_seconds": config.expertise_half_life_days * 86400,
        "module_keys": module_keys,
        "author_weight": 0.5,
        "g3_lambda": config.g3_lambda,
        "excluded_author": author_login,
        "top_n": top_n,
    }
    reviewers = context.fact(
        text="G3 reviewer candidates ranked from as-of module expertise and recent global review activity, with supporting PRs.",
        unit="ranked_reviewer_records",
        query_id="reviewer.g3.bundle@1",
        params=params,
        extractor="reviewer.g3_candidates@1",
    )
    return context.finish(
        {
            "modules": module_keys,
            "pr_number": pr_number,
            "reviewers": reviewers.value,
            "top_n": top_n,
        }
    )


def maintainer_loss(
    graph: Any,
    *,
    person_login: str,
    as_of_ts: int | None = None,
    config: ToolConfig = DEFAULT_TOOL_CONFIG,
) -> ToolResult:
    person_login = person_login.casefold()
    context = _context(
        graph,
        "maintainer_loss",
        {"person_login": person_login, "as_of_ts": as_of_ts},
        config=config,
    )
    if not context.query("person.by_login@1", {"person_login": person_login}):
        return context.finish({"error": f"Unknown person_login '{person_login}'."})
    activity_params = _activity_params(context, person_login=person_login)
    affected_modules = context.fact(
        text=f"Modules with recent authored or reviewed PR activity by {person_login}.",
        unit="module_keys",
        query_id="activity.rows@1",
        params=activity_params,
        extractor="maintainer.affected_modules@1",
    )
    module_keys = affected_modules.value
    pr_counts = context.fact(
        text="Recent PR counts for modules touched by the maintainer.",
        unit="module_pr_counts",
        query_id="module.pr_counts@1",
        params={"module_keys": module_keys, "activity_start_ts": context.activity_start_ts, "as_of_ts": context.as_of_ts},
        extractor="module.pr_count_map@1",
    )
    loss_activity = context.fact(
        text=f"Counterfactual module coverage and review-share changes after removing {person_login}.",
        unit="maintainer_loss_records",
        query_id="activity.rows@1",
        params={
            **_activity_params(context, module_keys=module_keys),
            "removed_login": person_login,
            "extract_module_keys": module_keys,
            "extract_pr_counts": pr_counts.value,
            "uncovered_person_activity_prs": config.uncovered_person_activity_prs,
            "bus_factor_min_prs": config.bus_factor_min_prs,
            "bus_factor_share_threshold": config.bus_factor_share_threshold,
        },
        extractor="maintainer.loss_modules@1",
    )
    losses = loss_activity.value
    dependency_impact: dict[str, Any] = {
        "dependent_modules_by_module": {},
        "dependent_counts_by_module": {},
        "distinct_dependents_affected": 0,
        "summed_dependent_edges": 0,
    }
    dependents_fact_id = None
    if losses:
        loss_module_keys = [item["module_key"] for item in losses]
        depth = config.default_max_depth
        dependency_query = (
            "maintainer.dependents.depth1@1"
            if depth == 1
            else "maintainer.dependents.depth2@1"
        )
        dependents = context.fact(
            text=f"Distinct reverse dependents within {depth} dependency hops of impacted modules.",
            unit="module_dependent_sets_and_counts",
            query_id=dependency_query,
            params={
                "module_keys": loss_module_keys,
                "max_depth": depth,
                "extract_uncovered_module_keys": [
                    item["module_key"] for item in losses if item["became_uncovered"]
                ],
            },
            extractor="maintainer.dependent_impact@1",
        )
        dependency_impact = dependents.value
        dependents_fact_id = dependents.fact_id
    modules_at_risk = [
        {
            **item,
            "dependent_count": dependency_impact["dependent_counts_by_module"].get(
                item["module_key"], 0
            ),
            "loss_fact_id": loss_activity.fact_id,
            "dependents_fact_id": dependents_fact_id,
        }
        for item in losses
    ]
    return context.finish(
        {
            "person_login": person_login,
            "modules": modules_at_risk,
            "module_count": len(modules_at_risk),
            "distinct_dependents_affected": dependency_impact[
                "distinct_dependents_affected"
            ],
            "summed_dependent_edges": dependency_impact[
                "summed_dependent_edges"
            ],
        }
    )


def decision_trace(
    graph: Any,
    *,
    module_key: str | None = None,
    pr_number: int | None = None,
    as_of_ts: int | None = None,
    config: ToolConfig = DEFAULT_TOOL_CONFIG,
) -> ToolResult:
    if (module_key is None) == (pr_number is None):
        raise ValueError("Provide exactly one of module_key or pr_number.")
    context = _context(
        graph,
        "decision_trace",
        {"module_key": module_key, "pr_number": pr_number, "as_of_ts": as_of_ts},
        config=config,
    )
    if module_key is not None:
        unknown = _unknown_module(context, module_key)
        if unknown:
            return unknown
        decisions = context.fact(
            text=f"Decisions referenced by PRs touching {module_key}, including relation type and source.",
            unit="decision_records",
            query_id="module.decisions@1",
            params={"module_key": module_key, "as_of_ts": context.as_of_ts},
            extractor="module.decision_list@1",
        )
    else:
        decisions = context.fact(
            text=f"Decisions referenced or recorded by PR {pr_number}, including relation type and source.",
            unit="decision_records",
            query_id="decision.by_pr@1",
            params={"pr_number": pr_number, "as_of_ts": context.as_of_ts},
            extractor="module.decision_list@1",
        )
    decision_ids = sorted(
        {item["decision_id"] for item in decisions.value if item["decision_id"]}
    )
    chains = context.fact(
        text="ADR supersedes chains for the decisions found, including each older decision's source.",
        unit="decision_supersedes_chains",
        query_id="decision.supersedes_chain@1",
        params={"decision_ids": decision_ids},
        extractor="decision.supersedes_chain@1",
    )
    chains_by_id = {item["decision_id"]: item for item in chains.value}
    traced_decisions = [
        {
            **item,
            "supersedes": chains_by_id.get(
                str(item["decision_id"]),
                {
                    "decision_id": str(item["decision_id"]),
                    "older_decisions": [],
                    "older_node_ids": [],
                    "supersedes_edge_ids": [],
                },
            )["older_decisions"],
        }
        for item in decisions.value
    ]
    return context.finish(
        {
            "module_key": module_key,
            "pr_number": pr_number,
            "decisions": traced_decisions,
        }
    )