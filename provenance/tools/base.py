from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from difflib import get_close_matches
from typing import Any, Callable

from provenance.tools.config import DEFAULT_TOOL_CONFIG, ToolConfig
from provenance.tools.queries import QUERY_EVIDENCE_COLUMNS, QUERY_REGISTRY


@dataclass
class Fact:
    fact_id: str
    text: str
    value: Any
    unit: str
    query_id: str
    params: dict[str, Any]
    extractor: str


@dataclass
class ToolResult:
    tool: str
    args: dict[str, Any]
    as_of_ts: int
    result: dict[str, Any]
    facts: list[Fact]
    evidence: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolArgument:
    type: str
    required: bool = False
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments: dict[str, ToolArgument]


Extractor = Callable[[list[list[Any]], dict[str, Any]], Any]
EXTRACTORS: dict[str, Extractor] = {}


def register_extractor(name: str) -> Callable[[Extractor], Extractor]:
    def decorate(function: Extractor) -> Extractor:
        if name in EXTRACTORS:
            raise ValueError(f"Extractor already registered: {name}")
        EXTRACTORS[name] = function
        return function

    return decorate


@register_extractor("scalar.column@1")
def extract_scalar_column(rows: list[list[Any]], params: dict[str, Any]) -> Any:
    if not rows:
        return params.get("empty_value")
    row_index = int(params.get("extract_row", 0))
    column_index = int(params["extract_column"])
    return rows[row_index][column_index]


@register_extractor("pinned_as_of_ts@1")
def extract_pinned_as_of(rows: list[list[Any]], params: dict[str, Any]) -> int:
    row = rows[0]
    requested = params.get("requested_as_of_ts")
    maximum = int(row[1])
    return maximum if requested is None else min(int(requested), maximum)


@register_extractor("rows.count@1")
def extract_row_count(rows: list[list[Any]], params: dict[str, Any]) -> int:
    return len(rows)


@register_extractor("rows.column_values@1")
def extract_column_values(rows: list[list[Any]], params: dict[str, Any]) -> list[Any]:
    index = int(params["extract_column"])
    return [row[index] for row in rows]


@register_extractor("contributors.module@1")
def extract_module_contributors(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    module_key = params.get("extract_module_key")
    top_n = int(params.get("extract_top_n", 15))
    counts: dict[str, dict[str, set[int]]] = defaultdict(
        lambda: {"author": set(), "review": set()}
    )
    for row in rows:
        if module_key is not None and row[0] != module_key:
            continue
        login, role = row[2], row[3]
        if login and role in {"author", "review"}:
            counts[str(login)][str(role)].add(int(row[4]))
    total_human_reviews = sum(len(values["review"]) for values in counts.values())
    contributors = []
    for login, values in counts.items():
        reviewed = len(values["review"])
        authored = len(values["author"])
        contributors.append(
            {
                "login": login,
                "authored_prs": authored,
                "reviewed_prs": reviewed,
                "activity_prs": len(values["author"] | values["review"]),
                "review_share": reviewed / total_human_reviews if total_human_reviews else 0.0,
            }
        )
    contributors.sort(key=lambda item: (-item["activity_prs"], -item["reviewed_prs"], item["login"]))
    return contributors[:top_n]


@register_extractor("ownership.drift_summary@1")
def extract_ownership_drift(rows: list[list[Any]], params: dict[str, Any]) -> dict[str, Any]:
    module_key = str(params["module_key"])
    top_n = int(params.get("extract_top_n", 5))
    owners: list[dict[str, Any]] = []
    activity: dict[str, dict[str, set[int]]] = defaultdict(
        lambda: {"author": set(), "review": set()}
    )
    for row in rows:
        if row[0] == "owner":
            if row[1] or row[2]:
                owners.append(
                    {
                        "kind": "team" if row[2] else "person",
                        "login": row[1],
                        "handle": row[2],
                        "is_bot": bool(row[3]),
                    }
                )
        elif row[0] == "activity" and row[1] and row[4] in {"author", "review"}:
            activity[str(row[1])][str(row[4])].add(int(row[7]))
    owners.sort(key=lambda item: (item["kind"], item["login"] or item["handle"] or ""))
    people = []
    for login, roles in activity.items():
        people.append(
            {
                "login": login,
                "authored_prs": len(roles["author"]),
                "reviewed_prs": len(roles["review"]),
                "activity_prs": len(roles["author"] | roles["review"]),
            }
        )
    people.sort(key=lambda item: (-item["activity_prs"], -item["reviewed_prs"], item["login"]))
    top_people = people[:top_n]
    declared_people = [owner["login"] for owner in owners if owner["kind"] == "person"]
    stale = []
    for login in declared_people:
        roles = activity.get(login, {"author": set(), "review": set()})
        if not (roles["author"] | roles["review"]):
            stale.append(login)
    top_logins = {person["login"] for person in top_people}
    outside = [login for login in declared_people if login not in top_logins]
    ownerless = len(owners) == 0
    stale_share = len(stale) / len(declared_people) if declared_people else 0.0
    outside_share = len(outside) / len(declared_people) if declared_people else 0.0
    drift_score = (
        float(params.get("ownership_drift_ownerless_weight", 0.4)) * int(ownerless)
        + float(params.get("ownership_drift_stale_weight", 0.3)) * stale_share
        + float(params.get("ownership_drift_outside_top5_weight", 0.3)) * outside_share
    )
    return {
        "module_key": module_key,
        "owners": owners,
        "declared_person_owners": declared_people,
        "behavioral_top": top_people,
        "ownerless": ownerless,
        "stale_declared_owners": stale,
        "declared_owners_outside_top5": outside,
        "drift_score": drift_score,
    }


@register_extractor("bus.module_summary@1")
def extract_bus_module_summary(rows: list[list[Any]], params: dict[str, Any]) -> dict[str, Any]:
    module_key = str(params["module_key"])
    pr_numbers: set[int] = set()
    reviews: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        if row[0] != module_key:
            continue
        if row[2] == "pr":
            pr_numbers.add(int(row[4]))
        elif row[2] == "review" and row[3]:
            reviews[str(row[3])].add(int(row[4]))
    denominator = sum(len(prs) for prs in reviews.values())
    reviewer_counts = sorted(
        ((login, len(prs)) for login, prs in reviews.items()),
        key=lambda item: (-item[1], item[0]),
    )
    top_login, top_reviews = reviewer_counts[0] if reviewer_counts else (None, 0)
    top_share = top_reviews / denominator if denominator else 0.0
    minimum_prs = int(params["bus_factor_min_prs"])
    threshold = float(params["bus_factor_share_threshold"])
    return {
        "module_key": module_key,
        "pr_count": len(pr_numbers),
        "human_review_pr_pairs": denominator,
        "top_person": top_login,
        "top_person_reviews": top_reviews,
        "top1_share": top_share,
        "bus_factor_risk": len(pr_numbers) >= minimum_prs and top_share >= threshold,
    }


@register_extractor("module.suggestions@1")
def extract_module_suggestions(rows: list[list[Any]], params: dict[str, Any]) -> list[str]:
    wanted = str(params["unknown_module_key"])
    keys = [str(row[0]) for row in rows]
    return get_close_matches(wanted, keys, n=5, cutoff=0.35)


def extract_module_activity(rows: list[list[Any]], params: dict[str, Any]) -> dict[str, Any]:
    module_key = str(params.get("extract_module_key") or params["module_key"])
    prs: set[int] = set()
    people: set[str] = set()
    for row in rows:
        if row[0] != module_key:
            continue
        if row[3] in {"author", "review"}:
            prs.add(int(row[4]))
            if row[2]:
                people.add(str(row[2]))
    depth = int(params["depth"])
    weight = (1.0 / math.sqrt(depth)) * (1.0 + math.log1p(len(prs)))
    return {"recent_pr_count": len(prs), "recent_human_count": len(people), "weight": weight}


@register_extractor("reviewer.g3_candidates@1")
def extract_g3_candidates(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    from provenance.eval.reviewer_eval import rank_candidates
    from provenance.eval.reviewer_eval_round2 import normalized_g3_scores

    global_scores: dict[str, float] = {}
    popularity: dict[str, int] = {}
    module_scores: dict[str, dict[str, float]] = defaultdict(dict)
    support: dict[str, dict[str, dict[str, list[int]]]] = defaultdict(
        lambda: defaultdict(lambda: {"review_prs": [], "authored_prs": []})
    )
    for row in rows:
        row_type, login, module_key = str(row[0]), str(row[1]), str(row[2])
        if row_type == "global":
            global_scores[login] = float(row[4] or 0.0)
            popularity[login] = int(row[3] or 0)
        elif row_type == "module_review":
            module_scores[login][module_key] = (
                module_scores[login].get(module_key, 0.0) + float(row[5] or 0.0)
            )
            support[login][module_key]["review_prs"] = [int(number) for number in row[6] or []]
        elif row_type == "module_author":
            module_scores[login][module_key] = (
                module_scores[login].get(module_key, 0.0)
                + float(params["author_weight"]) * float(row[5] or 0.0)
            )
            support[login][module_key]["authored_prs"] = [int(number) for number in row[6] or []]

    excluded_author = str(params.get("excluded_author") or "")
    g1_scores = {
        login: sum(module_scores[login].values())
        for login in set(global_scores) | set(module_scores)
    }
    is_bot = {login: False for login in set(global_scores) | set(g1_scores)}
    g1_rank = rank_candidates(
        g1_scores,
        popularity=popularity,
        is_bot=is_bot,
        author=excluded_author,
    )
    b3_rank = rank_candidates(
        global_scores,
        popularity=popularity,
        is_bot=is_bot,
        author=excluded_author,
    )
    scores = normalized_g3_scores(
        g1_scores,
        global_scores,
        g1_rank,
        b3_rank,
        float(params["g3_lambda"]),
    )
    ranked = rank_candidates(
        scores,
        popularity=popularity,
        is_bot=is_bot,
        author=excluded_author,
    )[: int(params.get("top_n", 15))]
    return [
        {
            "login": login,
            "g1_score": g1_scores.get(login, 0.0),
            "b3_score": global_scores.get(login, 0.0),
            "g3_score": scores[login],
            "global_review_prs": [
                int(number)
                for row in rows
                if row[0] == "global" and row[1] == login
                for number in row[6] or []
            ],
            "module_support": [
                {
                    "module_key": module_key,
                    "review_prs": values["review_prs"],
                    "authored_prs": values["authored_prs"],
                }
                for module_key, values in sorted(support.get(login, {}).items())
            ],
        }
        for login in ranked
    ]


@register_extractor("reviewer.target@1")
def extract_reviewer_target(rows: list[list[Any]], params: dict[str, Any]) -> dict[str, Any]:
    if not rows:
        return {}
    first = rows[0]
    return {
        "pr_number": int(first[0]),
        "merged_ts": int(first[1]),
        "author": str(first[2]).casefold(),
        "author_is_bot": bool(first[3]),
        "modules": [
            {"module_key": str(row[4]), "kind": str(row[5])}
            for row in rows
        ],
    }


@register_extractor("module.metadata_value@1")
def extract_module_metadata(rows: list[list[Any]], params: dict[str, Any]) -> list[Any]:
    return list(rows[0][:3]) if rows else []


@register_extractor("ownership.owner_list@1")
def extract_ownership_owner_list(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    return extract_ownership_drift(rows, params)["owners"]


@register_extractor("ownership.drift_score@1")
def extract_drift_score(rows: list[list[Any]], params: dict[str, Any]) -> float:
    return float(extract_ownership_drift(rows, params)["drift_score"])


@register_extractor("ownership.drift_flags@1")
def extract_drift_flags(rows: list[list[Any]], params: dict[str, Any]) -> list[Any]:
    summary = extract_ownership_drift(rows, params)
    return [
        summary["ownerless"],
        summary["stale_declared_owners"],
        summary["declared_owners_outside_top5"],
    ]


@register_extractor("ownership.behavioral_top@1")
def extract_drift_top(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    return extract_ownership_drift(rows, params)["behavioral_top"]


@register_extractor("module.owner_list@1")
def extract_module_owners(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    owners = []
    for row in rows:
        if row[1] or row[2]:
            owners.append(
                {
                    "kind": "team" if row[2] else "bot" if row[3] else "person",
                    "login": row[1],
                    "handle": row[2],
                }
            )
    return owners


@register_extractor("module.pr_count_value@1")
def extract_module_pr_count(rows: list[list[Any]], params: dict[str, Any]) -> int:
    wanted = str(params["extract_module_key"])
    for row in rows:
        if row[0] == wanted:
            return int(row[1])
    return 0


@register_extractor("module.pr_count_map@1")
def extract_module_pr_count_map(rows: list[list[Any]], params: dict[str, Any]) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in rows}


@register_extractor("maintainer.affected_modules@1")
def extract_maintainer_affected_modules(rows: list[list[Any]], params: dict[str, Any]) -> list[str]:
    person_login = str(params["person_login"])
    return sorted(
        {
            str(row[0])
            for row in rows
            if row[2] == person_login and row[3] in {"author", "review"}
        }
    )


@register_extractor("module.decision_list@1")
def extract_module_decisions(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        identity = (row[0], row[1], row[5])
        unique[identity] = {
            "pr_number": int(row[0]),
            "decision_id": row[1],
            "title": row[2],
            "status": row[3],
            "source": row[4],
            "relation": row[5],
        }
    return list(unique.values())[: int(params.get("extract_top_n", 25))]


@register_extractor("decision.supersedes_chain@1")
def extract_decision_supersedes_chain(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    chains = []
    for row in rows:
        older_decisions = [
            {
                "decision_id": str(item["decision_id"]),
                "title": item["title"],
                "status": item["status"],
                "source": item["source"],
            }
            for item in row[1] or []
            if isinstance(item, dict) and item.get("decision_id") is not None
        ]
        older_decisions.sort(key=lambda item: item["decision_id"])
        older_node_ids = sorted(
            int(node_id) for node_id in row[3] or [] if node_id is not None
        )
        supersedes_edge_ids = sorted(
            int(edge_id)
            for edge_group in row[4] or []
            for edge_id in edge_group or []
            if edge_id is not None
        )
        chains.append(
            {
                "decision_id": str(row[0]),
                "older_decisions": older_decisions,
                "older_node_ids": older_node_ids,
                "supersedes_edge_ids": supersedes_edge_ids,
            }
        )
    return chains


@register_extractor("bus.module_value@1")
def extract_bus_value(rows: list[list[Any]], params: dict[str, Any]) -> list[Any]:
    value = extract_bus_module_summary(rows, params)
    return [
        value["pr_count"],
        value["human_review_pr_pairs"],
        value["top_person"],
        value["top_person_reviews"],
        value["top1_share"],
        value["bus_factor_risk"],
    ]


@register_extractor("bus.ranking@1")
def extract_bus_ranking(rows: list[list[Any]], params: dict[str, Any]) -> list[list[Any]]:
    module_keys = sorted({str(row[0]) for row in rows})
    modules = []
    for module_key in module_keys:
        value = extract_bus_module_summary(rows, {**params, "module_key": module_key})
        modules.append(
            [
                module_key,
                value["pr_count"],
                value["human_review_pr_pairs"],
                value["top_person"],
                value["top_person_reviews"],
                value["top1_share"],
                value["bus_factor_risk"],
            ]
        )
    modules.sort(key=lambda item: (-item[5], -item[1], item[0]))
    if params.get("module_key") is not None:
        modules = [item for item in modules if item[0] == params["module_key"]]
    return modules[: int(params.get("extract_top_n", params.get("top_n", 15)))]


@register_extractor("blast.module_activity@1")
def extract_blast_activity_value(rows: list[list[Any]], params: dict[str, Any]) -> list[Any]:
    value = extract_module_activity(rows, params)
    return [value["recent_pr_count"], value["recent_human_count"], value["weight"]]


@register_extractor("blast.module_activities@1")
def extract_blast_module_activities(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    depths = {
        str(module_key): int(depth)
        for module_key, depth in params["extract_depth_by_module"].items()
    }
    activity: dict[str, dict[str, set[Any]]] = defaultdict(
        lambda: {"prs": set(), "people": set()}
    )
    for row in rows:
        module_key, login, role, pr_number = row[0], row[2], row[3], row[4]
        if module_key in depths and role in {"author", "review"}:
            activity[str(module_key)]["prs"].add(int(pr_number))
            if login:
                activity[str(module_key)]["people"].add(str(login))
    results = []
    for module_key, depth in sorted(depths.items()):
        pr_count = len(activity[module_key]["prs"])
        results.append(
            {
                "module_key": module_key,
                "recent_pr_count": pr_count,
                "recent_human_count": len(activity[module_key]["people"]),
                "weight": (1.0 / math.sqrt(depth)) * (1.0 + math.log1p(pr_count)),
            }
        )
    return results


@register_extractor("criticality.top_scores@1")
def extract_criticality_top_scores(rows: list[list[Any]], params: dict[str, Any]) -> list[list[Any]]:
    top_n = int(params.get("extract_top_n", 15))
    return [[row[0], row[1], float(row[2])] for row in rows[:top_n]]


@register_extractor("criticality.top_degrees@1")
def extract_criticality_top_degrees(rows: list[list[Any]], params: dict[str, Any]) -> list[list[Any]]:
    top_keys = set(params.get("extract_module_keys", []))
    values = [[row[0], int(row[1])] for row in rows if not top_keys or row[0] in top_keys]
    return values[: int(params.get("extract_top_n", 15))]


@register_extractor("blast.dependent_count@1")
def extract_blast_dependent_count(rows: list[list[Any]], params: dict[str, Any]) -> int:
    target_key = str(params.get("extract_target_key", ""))
    return len({str(row[0]) for row in rows if str(row[0]) != target_key})


@register_extractor("blast.dependent_list@1")
def extract_blast_dependent_list(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    target_key = str(params.get("extract_target_key", ""))
    dependents: dict[str, dict[str, Any]] = {}
    for row in rows:
        module_key = str(row[0])
        if module_key == target_key:
            continue
        depth = int(row[2])
        raw_edge_ids = row[4] if isinstance(row[4], list) else [row[4]]
        edge_ids = {int(edge_id) for edge_id in raw_edge_ids if edge_id is not None}
        current = dependents.get(module_key)
        if current is None or depth < current["depth"]:
            dependents[module_key] = {
                "module_key": module_key,
                "kind": row[1],
                "depth": depth,
                "dependency_edge_ids": edge_ids,
            }
        elif depth == current["depth"]:
            current["dependency_edge_ids"].update(edge_ids)
    return [
        {
            **dependent,
            "dependency_edge_ids": sorted(dependent["dependency_edge_ids"]),
        }
        for dependent in sorted(dependents.values(), key=lambda item: item["module_key"])
    ]


@register_extractor("blast.dependent_depth@1")
def extract_blast_dependent_depth(rows: list[list[Any]], params: dict[str, Any]) -> int | None:
    dependent_key = str(params["extract_dependent_key"])
    values = [int(row[2]) for row in rows if str(row[0]) == dependent_key]
    return min(values) if values else None


@register_extractor("criticality.score@1")
def extract_criticality_score(rows: list[list[Any]], params: dict[str, Any]) -> float:
    module_key = str(params["extract_module_key"])
    return next(float(row[2]) for row in rows if row[0] == module_key)


@register_extractor("criticality.in_degree@1")
def extract_criticality_degree(rows: list[list[Any]], params: dict[str, Any]) -> int:
    module_key = str(params["extract_module_key"])
    return next(int(row[1]) for row in rows if row[0] == module_key)


@register_extractor("maintainer.module_loss@1")
def extract_maintainer_module_loss(rows: list[list[Any]], params: dict[str, Any]) -> dict[str, Any]:
    module_key = str(params["extract_module_key"])
    person_login = str(params["removed_login"])
    min_activity = int(params["uncovered_person_activity_prs"])
    min_prs = int(params["bus_factor_min_prs"])
    threshold = float(params["bus_factor_share_threshold"])
    pr_count = int(params["extract_pr_counts"].get(module_key, 0))
    prs: set[int] = set()
    activity: dict[str, dict[str, set[int]]] = defaultdict(
        lambda: {"author": set(), "review": set()}
    )
    for row in rows:
        if str(row[0]) != module_key:
            continue
        if row[3] in {"author", "review"} and row[2]:
            pr_number = int(row[4])
            prs.add(pr_number)
            activity[str(row[2])][str(row[3])].add(pr_number)
    remaining = {
        login: roles
        for login, roles in activity.items()
        if login != person_login
    }
    originally_covered = any(
        max(len(roles["author"]), len(roles["review"])) >= min_activity
        for roles in activity.values()
    )
    remains_covered = any(
        max(len(roles["author"]), len(roles["review"])) >= min_activity
        for roles in remaining.values()
    )
    original_reviews = {login: len(roles["review"]) for login, roles in activity.items()}
    remaining_reviews = {login: len(roles["review"]) for login, roles in remaining.items()}
    original_total = sum(original_reviews.values())
    remaining_total = sum(remaining_reviews.values())
    original_top = max(original_reviews.values(), default=0)
    remaining_top = max(remaining_reviews.values(), default=0)
    original_share = original_top / original_total if original_total else 0.0
    remaining_share = remaining_top / remaining_total if remaining_total else 0.0
    reviewer_counts = sorted(
        remaining_reviews.items(),
        key=lambda item: (-item[1], item[0]),
    )
    return {
        "module_key": module_key,
        "pr_count": pr_count,
        "became_uncovered": originally_covered and not remains_covered,
        "review_share_before": original_share,
        "review_share_after": remaining_share,
        "review_share_crossed_threshold": (
            pr_count >= min_prs
            and original_share < threshold <= remaining_share
        ),
        "top_reviewer_after": reviewer_counts[0][0] if reviewer_counts else None,
    }


@register_extractor("maintainer.loss_modules@1")
def extract_maintainer_loss_modules(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    module_keys = [str(module_key) for module_key in params["extract_module_keys"]]
    wanted_modules = set(module_keys)
    rows_by_module: dict[str, list[list[Any]]] = defaultdict(list)
    for row in rows:
        module_key = str(row[0])
        if module_key in wanted_modules:
            rows_by_module[module_key].append(row)
    losses = []
    for module_key in module_keys:
        value = extract_maintainer_module_loss(
            rows_by_module[module_key],
            {**params, "extract_module_key": module_key},
        )
        if value["became_uncovered"] or value["review_share_crossed_threshold"]:
            losses.append(value)
    return losses


@register_extractor("maintainer.dependent_count@1")
def extract_maintainer_dependent_count(rows: list[list[Any]], params: dict[str, Any]) -> int:
    root_key = str(params["extract_root_key"])
    return len({str(row[1]) for row in rows if str(row[0]) == root_key})


@register_extractor("maintainer.dependent_counts@1")
def extract_maintainer_dependent_counts(rows: list[list[Any]], params: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        root_key, dependent_key = str(row[0]), str(row[1])
        counts[root_key].add(dependent_key)
    return {key: len(values) for key, values in sorted(counts.items())}


@register_extractor("maintainer.top_reviewers@1")
def extract_maintainer_top_reviewers(rows: list[list[Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    top_n = int(params.get("extract_top_n", 10))
    return [
        {
            "login": str(row[0]),
            "reviewed_pr_count": int(row[1]),
        }
        for row in rows[:top_n]
    ]


def _flatten_ints(value: Any) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for nested in value for item in _flatten_ints(nested)]
    return []


class ToolContext:
    def __init__(
        self,
        graph: Any,
        *,
        tool_name: str,
        args: dict[str, Any],
        requested_as_of_ts: int | None = None,
        config: ToolConfig = DEFAULT_TOOL_CONFIG,
    ) -> None:
        self.graph = graph
        self.tool_name = tool_name
        self.args = dict(args)
        self.config = config
        self.requested_as_of_ts = requested_as_of_ts
        self.facts: list[Fact] = []
        self.query_ids: list[str] = []
        self.node_ids: set[int] = set()
        self.edge_ids: set[int] = set()
        self.warnings: list[str] = [
            "DECLARED_OWNER and DEPENDS_ON are static snapshots without valid-from timestamps."
        ]
        self._query_cache: dict[tuple[str, str], list[list[Any]]] = {}
        meta_params = {
            "requested_as_of_ts": requested_as_of_ts,
            **asdict(config),
        }
        bounds = self.query("meta.graph_bounds@1", meta_params)
        if not bounds or bounds[0][1] is None:
            raise ValueError("The selected graph contains no merged PR timestamps.")
        self.graph_min_ts = int(bounds[0][0])
        self.graph_max_ts = int(bounds[0][1])
        self.as_of_ts = self.graph_max_ts if requested_as_of_ts is None else min(
            int(requested_as_of_ts), self.graph_max_ts
        )
        if requested_as_of_ts is not None and requested_as_of_ts > self.graph_max_ts:
            self.warnings.append("Requested as_of_ts exceeded graph maximum and was capped.")
        if self.as_of_ts < self.graph_min_ts:
            self.warnings.append("as_of_ts predates retained graph history; activity results may be empty.")
        self.args["as_of_ts"] = self.as_of_ts
        config_params = asdict(config)
        config_rows = self.query("meta.tool_config@1", config_params)
        config_names = list(config_params)
        for index, name in enumerate(config_names):
            params = {**config_params, "extract_column": index}
            self._append_fact(
                text=f"Pinned tool default for {name}.",
                value=extract_scalar_column(config_rows, params),
                unit="configuration_value",
                query_id="meta.tool_config@1",
                params=params,
                extractor="scalar.column@1",
            )
        self._add_fact(
            "Pinned as-of timestamp for this read-only tool call.",
            self.as_of_ts,
            "unix_epoch_seconds",
            "meta.graph_bounds@1",
            meta_params,
            "pinned_as_of_ts@1",
        )

    @property
    def activity_start_ts(self) -> int:
        return max(
            self.graph_min_ts,
            self.as_of_ts - self.config.activity_window_days * 86400,
        )

    @property
    def expertise_start_ts(self) -> int:
        return max(
            self.graph_min_ts,
            self.as_of_ts - self.config.expertise_window_days * 86400,
        )

    def query(self, query_id: str, params: dict[str, Any]) -> list[list[Any]]:
        query = QUERY_REGISTRY.get(query_id)
        if query is None:
            raise KeyError(f"Unregistered Cypher query id: {query_id}")
        cache_key = (query_id, json.dumps(params, sort_keys=True, default=str))
        rows = self._query_cache.get(cache_key)
        if rows is None:
            rows = self.graph.ro_query(query, params).result_set
            self._query_cache[cache_key] = rows
        if query_id not in self.query_ids:
            self.query_ids.append(query_id)
        columns = QUERY_EVIDENCE_COLUMNS.get(query_id, {})
        for row in rows:
            for column in columns.get("nodes", ()):
                self.node_ids.update(_flatten_ints(row[column]))
            for column in columns.get("edges", ()):
                self.edge_ids.update(_flatten_ints(row[column]))
        return rows

    def fact(
        self,
        *,
        text: str,
        unit: str,
        query_id: str,
        params: dict[str, Any],
        extractor: str,
    ) -> Fact:
        rows = self.query(query_id, params)
        function = EXTRACTORS.get(extractor)
        if function is None:
            raise KeyError(f"Unregistered fact extractor: {extractor}")
        return self._append_fact(
            text=text,
            value=function(rows, params),
            unit=unit,
            query_id=query_id,
            params=params,
            extractor=extractor,
        )

    def _add_fact(
        self,
        text: str,
        value: Any,
        unit: str,
        query_id: str,
        params: dict[str, Any],
        extractor: str,
    ) -> Fact:
        return self._append_fact(text, value, unit, query_id, params, extractor)

    def _append_fact(
        self,
        text: str,
        value: Any,
        unit: str,
        query_id: str,
        params: dict[str, Any],
        extractor: str,
    ) -> Fact:
        fact = Fact(
            fact_id=f"F{len(self.facts) + 1}",
            text=text,
            value=value,
            unit=unit,
            query_id=query_id,
            params=dict(params),
            extractor=extractor,
        )
        self.facts.append(fact)
        return fact

    def finish(self, result: dict[str, Any]) -> ToolResult:
        return ToolResult(
            tool=self.tool_name,
            args=self.args,
            as_of_ts=self.as_of_ts,
            result=result,
            facts=self.facts,
            evidence={
                "query_ids": self.query_ids,
                "node_ids": sorted(self.node_ids),
                "edge_ids": sorted(self.edge_ids),
            },
            warnings=list(dict.fromkeys(self.warnings)),
        )


def recompute_fact(graph: Any, fact: Fact) -> Any:
    query = QUERY_REGISTRY.get(fact.query_id)
    extractor = EXTRACTORS.get(fact.extractor)
    if query is None or extractor is None:
        raise KeyError(f"Fact references unknown query/extractor: {fact.query_id}/{fact.extractor}")
    rows = graph.ro_query(query, fact.params).result_set
    return extractor(rows, fact.params)