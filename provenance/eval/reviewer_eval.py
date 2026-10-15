from __future__ import annotations

import json
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from falkordb import FalkorDB
from rich.console import Console
from rich.table import Table

from provenance.config import settings


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRAPH = "provenance_ha"
DEFAULT_RESULTS_PATH = ROOT / "results" / "reviewer_eval.json"
HALF_LIFE_DAYS = (14, 30, 60, 90)
AUTHOR_WEIGHTS = (0.0, 0.5, 1.0)
NEIGHBOR_FACTORS = (0.0, 0.3)
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20261015
LN_2 = 0.6931471805599453

TARGET_QUERY = """
MATCH (pr:PR)-[touch:TOUCHES]->(module:Module)
MATCH (author:Person)-[authored:AUTHORED]->(pr)
WHERE pr.merged_ts >= $window_start AND pr.merged_ts < $window_end
RETURN pr.number, pr.merged_ts, author.login, author.is_bot,
       module.key, module.kind, touch.files, authored.ts
ORDER BY pr.merged_ts, pr.number, module.key
"""

GROUND_TRUTH_QUERY = """
MATCH (person:Person)-[review:REVIEWED]->(pr:PR)
WHERE pr.merged_ts >= $window_start AND pr.merged_ts < $window_end
RETURN pr.number, person.login, person.is_bot, review.state, review.ts
ORDER BY pr.number, person.login, review.ts
"""

OWNER_QUERY = """
MATCH (person:Person)-[owner:DECLARED_OWNER]->(module:Module)
WHERE module.kind = $module_kind
RETURN module.key, person.login, person.is_bot, count(owner)
ORDER BY module.key, person.login
"""

DEPENDENCY_QUERY = """
MATCH (source:Module)-[edge:DEPENDS_ON]->(target:Module)
RETURN source.key, target.key, target.kind, edge.kind, count(edge)
ORDER BY source.key, target.key, edge.kind
"""

GLOBAL_HISTORY_QUERY = f"""
MATCH (person:Person)-[review:REVIEWED]->(pr:PR)
WHERE pr.merged_ts < $as_of AND review.ts < $as_of
RETURN person.login, person.is_bot, count(review),
       sum(exp(-{LN_2} * ($as_of - review.ts) / $half_life_14)),
       sum(exp(-{LN_2} * ($as_of - review.ts) / $half_life_30)),
       sum(exp(-{LN_2} * ($as_of - review.ts) / $half_life_60)),
       sum(exp(-{LN_2} * ($as_of - review.ts) / $half_life_90))
ORDER BY person.login
"""

EXPERTISE_QUERY = f"""
MATCH (history:PR)-[all_touch:TOUCHES]->(:Module)
WHERE history.merged_ts < $as_of
WITH history, count(all_touch) AS touched_module_count
MATCH (history)-[target_touch:TOUCHES]->(module:Module)
WHERE module.key IN $module_keys
MATCH (person:Person)-[review:REVIEWED]->(history)
WHERE review.ts < $as_of
WITH DISTINCT person, module, history, target_touch, touched_module_count
RETURN 'review' AS role, person.login, person.is_bot, module.key,
       sum(exp(-{LN_2} * ($as_of - history.merged_ts) / $half_life_14) / touched_module_count),
       sum(exp(-{LN_2} * ($as_of - history.merged_ts) / $half_life_30) / touched_module_count),
       sum(exp(-{LN_2} * ($as_of - history.merged_ts) / $half_life_60) / touched_module_count),
       sum(exp(-{LN_2} * ($as_of - history.merged_ts) / $half_life_90) / touched_module_count),
       count(target_touch), collect(DISTINCT history.number)
UNION ALL
MATCH (history:PR)-[all_touch:TOUCHES]->(:Module)
WHERE history.merged_ts < $as_of
WITH history, count(all_touch) AS touched_module_count
MATCH (history)-[target_touch:TOUCHES]->(module:Module)
WHERE module.key IN $module_keys
MATCH (person:Person)-[authored:AUTHORED]->(history)
WHERE authored.ts < $as_of
WITH DISTINCT person, module, history, target_touch, touched_module_count
RETURN 'author' AS role, person.login, person.is_bot, module.key,
       sum(exp(-{LN_2} * ($as_of - history.merged_ts) / $half_life_14) / touched_module_count),
       sum(exp(-{LN_2} * ($as_of - history.merged_ts) / $half_life_30) / touched_module_count),
       sum(exp(-{LN_2} * ($as_of - history.merged_ts) / $half_life_60) / touched_module_count),
       sum(exp(-{LN_2} * ($as_of - history.merged_ts) / $half_life_90) / touched_module_count),
       count(target_touch), collect(DISTINCT history.number)
"""

INTEGRATION_HISTORY_QUERY = """
MATCH (history:PR)-[touch:TOUCHES]->(module:Module)
WHERE history.merged_ts < $as_of AND module.key IN $integration_keys
RETURN 'module' AS row_type, module.key AS module_key,
       count(DISTINCT history) AS pr_count, count(touch) AS edge_count
UNION ALL
MATCH (history:PR)-[touch:TOUCHES]->(module:Module)
WHERE history.merged_ts < $as_of AND module.key IN $integration_keys
RETURN 'total' AS row_type, '' AS module_key,
       count(DISTINCT history) AS pr_count, count(touch) AS edge_count
"""


@dataclass
class HistoryFeatures:
    popularity: dict[str, int] = field(default_factory=dict)
    recency: dict[str, dict[int, float]] = field(default_factory=dict)
    is_bot: dict[str, bool] = field(default_factory=dict)
    review_expertise: dict[tuple[str, str], dict[int, float]] = field(default_factory=dict)
    author_expertise: dict[tuple[str, str], dict[int, float]] = field(default_factory=dict)
    evidence_prs: dict[tuple[str, str, str], list[int]] = field(default_factory=dict)
    integration_pr_counts: dict[str, int] = field(default_factory=dict)
    integration_pr_count: int = 0
    query_latency_ms: float = 0.0


def _query_rows(graph: Any, query: str, params: dict[str, Any]) -> list[list[Any]]:
    return graph.ro_query(query, params).result_set


def load_targets(graph: Any, window_start: int, window_end: int) -> dict[int, dict[str, Any]]:
    targets: dict[int, dict[str, Any]] = {}
    for row in _query_rows(
        graph,
        TARGET_QUERY,
        {"window_start": window_start, "window_end": window_end},
    ):
        number, merged_ts, author, is_bot, module_key, module_kind, _, _ = row
        target = targets.setdefault(
            int(number),
            {
                "number": int(number),
                "merged_ts": int(merged_ts),
                "author": str(author).casefold(),
                "author_is_bot": bool(is_bot),
                "modules": {},
            },
        )
        target["modules"][str(module_key)] = str(module_kind)
    return targets


def load_ground_truth(
    graph: Any, window_start: int, window_end: int
) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    reviewers: dict[int, set[str]] = defaultdict(set)
    approvers: dict[int, set[str]] = defaultdict(set)
    for number, login, is_bot, state, _ in _query_rows(
        graph,
        GROUND_TRUTH_QUERY,
        {"window_start": window_start, "window_end": window_end},
    ):
        if not login or bool(is_bot):
            continue
        normalized = str(login).casefold()
        reviewers[int(number)].add(normalized)
        if str(state).upper() == "APPROVED":
            approvers[int(number)].add(normalized)
    return reviewers, approvers


def load_static_graph_data(
    graph: Any,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, bool]]:
    owners: dict[str, set[str]] = defaultdict(set)
    owner_is_bot: dict[str, bool] = {}
    for module_key, login, is_bot, _ in _query_rows(
        graph, OWNER_QUERY, {"module_kind": "integration"}
    ):
        if login:
            normalized = str(login).casefold()
            owners[str(module_key)].add(normalized)
            owner_is_bot[normalized] = bool(is_bot)

    dependencies: dict[str, set[str]] = defaultdict(set)
    for source_key, target_key, target_kind, _, _ in _query_rows(
        graph, DEPENDENCY_QUERY, {}
    ):
        if target_kind != "infra":
            dependencies[str(source_key)].add(str(target_key))
    return dict(owners), dict(dependencies), owner_is_bot


def fetch_history(
    graph: Any,
    *,
    as_of: int,
    module_keys: set[str],
    integration_keys: set[str],
) -> HistoryFeatures:
    features = HistoryFeatures()
    half_life_params = {
        f"half_life_{days}": days * 86400 for days in HALF_LIFE_DAYS
    }

    started = time.perf_counter()
    for row in _query_rows(
        graph,
        GLOBAL_HISTORY_QUERY,
        {"as_of": as_of, **half_life_params},
    ):
        login, is_bot, review_count, *decays = row
        normalized = str(login).casefold()
        features.is_bot[normalized] = bool(is_bot)
        features.popularity[normalized] = int(review_count or 0)
        features.recency[normalized] = {
            days: float(decay or 0.0)
            for days, decay in zip(HALF_LIFE_DAYS, decays)
        }

    if module_keys:
        for row in _query_rows(
            graph,
            EXPERTISE_QUERY,
            {
                "as_of": as_of,
                "module_keys": sorted(module_keys),
                **half_life_params,
            },
        ):
            role, login, is_bot, module_key, *aggregates = row
            decays = aggregates[:4]
            evidence_numbers = [int(number) for number in aggregates[5] or []]
            normalized = str(login).casefold()
            features.is_bot[normalized] = bool(is_bot)
            identity = (normalized, str(module_key))
            target = (
                features.review_expertise
                if role == "review"
                else features.author_expertise
            )
            target[identity] = {
                days: float(decay or 0.0)
                for days, decay in zip(HALF_LIFE_DAYS, decays)
            }
            features.evidence_prs[(str(role), normalized, str(module_key))] = (
                evidence_numbers
            )

    if integration_keys:
        for row_type, module_key, pr_count, _ in _query_rows(
            graph,
            INTEGRATION_HISTORY_QUERY,
            {"as_of": as_of, "integration_keys": sorted(integration_keys)},
        ):
            if row_type == "module":
                features.integration_pr_counts[str(module_key)] = int(pr_count)
            else:
                features.integration_pr_count = int(pr_count)
    features.query_latency_ms = (time.perf_counter() - started) * 1000
    return features


def rank_candidates(
    scores: dict[str, float],
    *,
    popularity: dict[str, int],
    is_bot: dict[str, bool],
    author: str,
) -> list[str]:
    author = author.casefold()
    candidates = [
        login
        for login in scores
        if login != author and not is_bot.get(login, False)
    ]
    return sorted(
        candidates,
        key=lambda login: (-scores[login], -popularity.get(login, 0), login),
    )


def _module_expertise(
    login: str, module_key: str, features: HistoryFeatures, half_life: int, author_weight: float
) -> float:
    reviewer_score = features.review_expertise.get((login, module_key), {}).get(
        half_life, 0.0
    )
    author_score = features.author_expertise.get((login, module_key), {}).get(
        half_life, 0.0
    )
    return reviewer_score + author_weight * author_score


def expertise_scores(
    target: dict[str, Any],
    features: HistoryFeatures,
    *,
    half_life: int,
    author_weight: float,
    neighbor_factor: float = 0.0,
    dependencies: dict[str, set[str]] | None = None,
) -> dict[str, float]:
    modules = target["modules"]
    touched_non_infra = {key for key, kind in modules.items() if kind != "infra"}
    touched_integrations = {
        key for key, kind in modules.items() if kind == "integration"
    }
    cold_integrations = {
        key
        for key in touched_integrations
        if features.integration_pr_counts.get(key, 0) < 5
    }
    borrowed: Counter[str] = Counter()
    if neighbor_factor and dependencies:
        for integration in cold_integrations:
            borrowed.update(dependencies.get(integration, set()))

    weights: Counter[str] = Counter({key: 1.0 for key in touched_non_infra})
    for module_key, count in borrowed.items():
        weights[module_key] += neighbor_factor * count

    identities = set(features.review_expertise)
    if author_weight > 0:
        identities.update(features.author_expertise)
    scores: dict[str, float] = defaultdict(float)
    for login, module_key in identities:
        if module_key in weights:
            scores[login] += weights[module_key] * _module_expertise(
                login, module_key, features, half_life, author_weight
            )
    return dict(scores)


def method_rankings(
    target: dict[str, Any],
    features: HistoryFeatures,
    *,
    owners: dict[str, set[str]],
    owner_is_bot: dict[str, bool],
    dependencies: dict[str, set[str]],
    parameters: dict[str, Any],
) -> dict[str, list[str]]:
    author = target["author"]
    is_bot = {**owner_is_bot, **features.is_bot}
    popularity = features.popularity
    touched_integrations = [
        key for key, kind in target["modules"].items() if kind == "integration"
    ]
    owned_counts: Counter[str] = Counter()
    for module_key in touched_integrations:
        owned_counts.update(owners.get(module_key, set()))

    half_life = int(parameters["half_life_days"])
    author_weight = float(parameters["author_weight"])
    g1_scores = expertise_scores(
        target, features, half_life=half_life, author_weight=author_weight
    )
    g2_scores = expertise_scores(
        target,
        features,
        half_life=half_life,
        author_weight=author_weight,
        neighbor_factor=float(parameters["neighbor_factor"]),
        dependencies=dependencies,
    )
    half_life_b3 = int(parameters["recent_half_life_days"])

    return {
        "B1": rank_candidates(
            dict(owned_counts), popularity=popularity, is_bot=is_bot, author=author
        ),
        "B2": rank_candidates(
            {login: float(count) for login, count in popularity.items()},
            popularity=popularity,
            is_bot=is_bot,
            author=author,
        ),
        "B3": rank_candidates(
            {
                login: values.get(half_life_b3, 0.0)
                for login, values in features.recency.items()
            },
            popularity=popularity,
            is_bot=is_bot,
            author=author,
        ),
        "G1": rank_candidates(
            g1_scores, popularity=popularity, is_bot=is_bot, author=author
        ),
        "G2": rank_candidates(
            g2_scores, popularity=popularity, is_bot=is_bot, author=author
        ),
    }


def _eligible(
    targets: dict[int, dict[str, Any]],
    reviewers: dict[int, set[str]],
    approvers: dict[int, set[str]],
    *,
    primary_only: bool,
    owners: dict[str, set[str]],
    dependencies: dict[str, set[str]],
    owner_is_bot: dict[str, bool],
    graph: Any,
) -> list[dict[str, Any]]:
    eligible_targets = []
    for number, target in sorted(
        targets.items(), key=lambda item: (item[1]["merged_ts"], item[0])
    ):
        integration_keys = {
            key for key, kind in target["modules"].items() if kind == "integration"
        }
        if target["author_is_bot"] or not reviewers.get(number):
            continue
        if primary_only and not integration_keys:
            continue
        owner_people = (
            set().union(*(owners.get(key, set()) for key in integration_keys))
            if integration_keys
            else set()
        )
        expert_module_keys = {
            key for key, kind in target["modules"].items() if kind != "infra"
        }
        for integration in integration_keys:
            expert_module_keys.update(dependencies.get(integration, set()))
        history = fetch_history(
            graph,
            as_of=target["merged_ts"],
            module_keys=expert_module_keys,
            integration_keys=integration_keys,
        )
        target_copy = {
            **target,
            "integration_keys": integration_keys,
            "owner_people": owner_people,
            "owner_count": len(owner_people),
            "true_reviewers": reviewers[number],
            "true_approvers": approvers.get(number, set()),
        }
        eligible_targets.append(
            {
                "target": target_copy,
                "history": history,
                "owners": owners,
                "owner_is_bot": owner_is_bot,
                "dependencies": dependencies,
            }
        )
    return eligible_targets


def prepare_split(
    graph: Any,
    *,
    window_start: int,
    window_end: int,
    primary_only: bool,
    owners: dict[str, set[str]],
    dependencies: dict[str, set[str]],
    owner_is_bot: dict[str, bool],
) -> list[dict[str, Any]]:
    targets = load_targets(graph, window_start, window_end)
    reviewers, approvers = load_ground_truth(graph, window_start, window_end)
    return _eligible(
        targets,
        reviewers,
        approvers,
        primary_only=primary_only,
        owners=owners,
        dependencies=dependencies,
        owner_is_bot=owner_is_bot,
        graph=graph,
    )


def _hit_at(prediction: list[str], truth: set[str], k: int) -> int:
    return int(bool(set(prediction[:k]) & truth))


def _per_pr_metrics(prediction: list[str], truth: set[str]) -> dict[str, float]:
    ranks = [index for index, login in enumerate(prediction, start=1) if login in truth]
    return {
        "hit@1": _hit_at(prediction, truth, 1),
        "hit@3": _hit_at(prediction, truth, 3),
        "hit@5": _hit_at(prediction, truth, 5),
        "recall@5": len(set(prediction[:5]) & truth) / len(truth) if truth else 0.0,
        "MRR": 1.0 / ranks[0] if ranks else 0.0,
    }


def _aggregate_metrics(records: list[dict[str, Any]], truth_key: str) -> dict[str, Any]:
    methods: dict[str, dict[str, float | None]] = {}
    for method in ("B1", "B2", "B3", "G1", "G2"):
        values = [
            _per_pr_metrics(item["rankings"][method], item["target"][truth_key])
            for item in records
        ]
        methods[method] = {
            metric: (statistics.fmean(value[metric] for value in values) if values else None)
            for metric in ("hit@1", "hit@3", "hit@5", "recall@5", "MRR")
        }
    oracle = [
        bool(item["target"][truth_key] & item["history"].popularity.keys())
        for item in records
    ]
    return {
        "n": len(records),
        "oracle_ceiling": statistics.fmean(oracle) if oracle else None,
        "methods": methods,
    }


def _bootstrap_differences(
    records: list[dict[str, Any]], truth_key: str
) -> dict[str, dict[str, Any]]:
    if not records:
        return {}
    differences: dict[str, dict[str, Any]] = {}
    rng = random.Random(BOOTSTRAP_SEED)
    samples = [
        [rng.randrange(len(records)) for _ in range(len(records))]
        for _ in range(BOOTSTRAP_RESAMPLES)
    ]
    for graph_method in ("G1", "G2"):
        for baseline in ("B1", "B2", "B3"):
            paired = [
                _hit_at(record["rankings"][graph_method], record["target"][truth_key], 3)
                - _hit_at(record["rankings"][baseline], record["target"][truth_key], 3)
                for record in records
            ]
            bootstrap_means = sorted(
                statistics.fmean(paired[index] for index in sample)
                for sample in samples
            )
            differences[f"{graph_method}-vs-{baseline}"] = {
                "mean_difference": statistics.fmean(paired),
                "ci95": [
                    bootstrap_means[int(0.025 * (BOOTSTRAP_RESAMPLES - 1))],
                    bootstrap_means[int(0.975 * (BOOTSTRAP_RESAMPLES - 1))],
                ],
                "resamples": BOOTSTRAP_RESAMPLES,
            }
    return differences


def _slice_groups(
    records: list[dict[str, Any]], truth_key: str
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    dimensions: dict[str, dict[str, list[dict[str, Any]]]] = {
        "integration_declared_owner_count": {"0": [], "1": [], "2+": []},
        "cold_warm": {"cold": [], "warm": []},
        "integrations_touched": {"0": [], "1": [], "several": []},
        "true_reviewers_include_declared_owner": {"yes": [], "no": []},
    }
    for record in records:
        target = record["target"]
        owner_count = int(target["owner_count"])
        owner_bucket = "0" if owner_count == 0 else "1" if owner_count == 1 else "2+"
        dimensions["integration_declared_owner_count"][owner_bucket].append(record)
        cold = record["history"].integration_pr_count < 5
        dimensions["cold_warm"]["cold" if cold else "warm"].append(record)
        integration_count = len(target["integration_keys"])
        integration_bucket = (
            "0" if integration_count == 0 else "1" if integration_count == 1 else "several"
        )
        dimensions["integrations_touched"][integration_bucket].append(record)
        includes_owner = bool(target[truth_key] & target["owner_people"])
        dimensions["true_reviewers_include_declared_owner"][
            "yes" if includes_owner else "no"
        ].append(record)
    return dimensions


def _population_report(
    records: list[dict[str, Any]], truth_key: str, *, include_bootstrap: bool
) -> dict[str, Any]:
    result = _aggregate_metrics(records, truth_key)
    result["slices"] = {
        dimension: {
            label: _aggregate_metrics(group, truth_key)
            for label, group in groups.items()
        }
        for dimension, groups in _slice_groups(records, truth_key).items()
    }
    if include_bootstrap:
        result["bootstrap_hit3_differences"] = _bootstrap_differences(records, truth_key)
    return result


def build_test_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for variant, truth_key in (
        ("reviewers", "true_reviewers"),
        ("approvers", "true_approvers"),
    ):
        variant_records = [record for record in records if record["target"][truth_key]]
        report[variant] = {
            "primary": _population_report(
                [record for record in variant_records if record["target"]["integration_keys"]],
                truth_key,
                include_bootstrap=True,
            ),
            "secondary": _population_report(
                variant_records, truth_key, include_bootstrap=True
            ),
        }
    return report


def _tuning_objective(
    records: list[dict[str, Any]],
    truth_key: str,
    ranking_function: Callable[[dict[str, Any]], list[str]],
) -> dict[str, float]:
    per_pr = [
        _per_pr_metrics(ranking_function(item), item["target"][truth_key])
        for item in records
    ]
    return {
        metric: statistics.fmean(item[metric] for item in per_pr) if per_pr else 0.0
        for metric in ("hit@3", "recall@5", "MRR")
    }


def tune_parameters(records: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not records:
        raise ValueError("Validation set contains no eligible integration PRs.")
    grid: dict[str, list[dict[str, Any]]] = {"B3": [], "G1": [], "G2": []}

    for half_life in HALF_LIFE_DAYS:
        metric = _tuning_objective(
            records,
            "true_reviewers",
            lambda item, days=half_life: rank_candidates(
                {login: value.get(days, 0.0) for login, value in item["history"].recency.items()},
                popularity=item["history"].popularity,
                is_bot=item["history"].is_bot,
                author=item["target"]["author"],
            ),
        )
        grid["B3"].append({"recent_half_life_days": half_life, **metric})

    for half_life in HALF_LIFE_DAYS:
        for author_weight in AUTHOR_WEIGHTS:
            metric = _tuning_objective(
                records,
                "true_reviewers",
                lambda item, days=half_life, weight=author_weight: rank_candidates(
                    expertise_scores(
                        item["target"], item["history"], half_life=days, author_weight=weight
                    ),
                    popularity=item["history"].popularity,
                    is_bot={**item["owner_is_bot"], **item["history"].is_bot},
                    author=item["target"]["author"],
                ),
            )
            grid["G1"].append(
                {"half_life_days": half_life, "author_weight": author_weight, **metric}
            )

    for half_life in HALF_LIFE_DAYS:
        for author_weight in AUTHOR_WEIGHTS:
            for neighbor_factor in NEIGHBOR_FACTORS:
                metric = _tuning_objective(
                    records,
                    "true_reviewers",
                    lambda item, days=half_life, weight=author_weight, neighbor=neighbor_factor: rank_candidates(
                        expertise_scores(
                            item["target"],
                            item["history"],
                            half_life=days,
                            author_weight=weight,
                            neighbor_factor=neighbor,
                            dependencies=item["dependencies"],
                        ),
                        popularity=item["history"].popularity,
                        is_bot={**item["owner_is_bot"], **item["history"].is_bot},
                        author=item["target"]["author"],
                    ),
                )
                grid["G2"].append(
                    {
                        "half_life_days": half_life,
                        "author_weight": author_weight,
                        "neighbor_factor": neighbor_factor,
                        **metric,
                    }
                )

    def winner(entries: list[dict[str, Any]]) -> dict[str, Any]:
        return max(entries, key=lambda entry: (entry["hit@3"], entry["MRR"], entry["recall@5"]))

    b3, g1, g2 = winner(grid["B3"]), winner(grid["G1"]), winner(grid["G2"])
    frozen = {
        "recent_half_life_days": b3["recent_half_life_days"],
        "half_life_days": g1["half_life_days"],
        "author_weight": g1["author_weight"],
        "g2_half_life_days": g2["half_life_days"],
        "g2_author_weight": g2["author_weight"],
        "neighbor_factor": g2["neighbor_factor"],
    }
    return frozen, {
        "objective": "validation primary hit@3, ties by MRR then recall@5",
        "grid": grid,
        "winners": {"B3": b3, "G1": g1, "G2": g2},
    }


def score_records(records: list[dict[str, Any]], frozen: dict[str, Any]) -> None:
    for record in records:
        target, history = record["target"], record["history"]
        parameters = {
            "recent_half_life_days": frozen["recent_half_life_days"],
            "half_life_days": frozen["half_life_days"],
            "author_weight": frozen["author_weight"],
            "neighbor_factor": 0.0,
        }
        rankings = method_rankings(
            target,
            history,
            owners=record["owners"],
            owner_is_bot=record["owner_is_bot"],
            dependencies=record["dependencies"],
            parameters=parameters,
        )
        g2_rankings = method_rankings(
            target,
            history,
            owners=record["owners"],
            owner_is_bot=record["owner_is_bot"],
            dependencies=record["dependencies"],
            parameters={
                **parameters,
                "half_life_days": frozen["g2_half_life_days"],
                "author_weight": frozen["g2_author_weight"],
                "neighbor_factor": frozen["neighbor_factor"],
            },
        )
        rankings["G2"] = g2_rankings["G2"]
        record["rankings"] = rankings


def latency_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(record["history"].query_latency_ms for record in records)
    if not values:
        return {"scored_prs": 0, "mean_ms": None, "median_ms": None, "p95_ms": None}
    return {
        "scored_prs": len(values),
        "mean_ms": statistics.fmean(values),
        "median_ms": statistics.median(values),
        "p95_ms": values[min(len(values) - 1, int(0.95 * len(values)))],
    }


def _iso_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def run_evaluation(
    *,
    graph_name: str = DEFAULT_GRAPH,
    window_end: int | None = None,
    output_path: Path = DEFAULT_RESULTS_PATH,
) -> dict[str, Any]:
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph(graph_name)
    now = int(datetime.now(timezone.utc).timestamp()) if window_end is None else int(window_end)
    day = 86400
    validation_start, validation_end = now - 60 * day, now - 30 * day
    test_start, test_end = validation_end, now
    window_start = now - 180 * day
    owners, dependencies, owner_is_bot = load_static_graph_data(graph)

    validation_records = prepare_split(
        graph,
        window_start=validation_start,
        window_end=validation_end,
        primary_only=True,
        owners=owners,
        dependencies=dependencies,
        owner_is_bot=owner_is_bot,
    )
    frozen, tuning = tune_parameters(validation_records)

    # Test labels and histories are first loaded after validation parameters are frozen.
    test_records = prepare_split(
        graph,
        window_start=test_start,
        window_end=test_end,
        primary_only=False,
        owners=owners,
        dependencies=dependencies,
        owner_is_bot=owner_is_bot,
    )
    score_records(test_records, frozen)
    test_report = build_test_report(test_records)
    output = {
        "graph": graph_name,
        "evaluation_as_of_utc": _iso_utc(now),
        "window": {
            "start_utc": _iso_utc(window_start),
            "validation_start_utc": _iso_utc(validation_start),
            "validation_end_utc": _iso_utc(validation_end),
            "test_start_utc": _iso_utc(test_start),
            "end_utc": _iso_utc(test_end),
            "rule": "All history queries filter merged_ts < the target PR merge time.",
        },
        "eligibility": {
            "reviewer_primary": "non-bot author, at least one distinct human reviewer, and at least one integration module",
            "secondary": "non-bot author, at least one distinct human reviewer, any touched module type",
            "approver_variant": "same eligibility, restricted to PRs with at least one distinct APPROVED human reviewer",
        },
        "methods": {
            "B1": "current declared Person owners of touched integrations; rank by owned integration count, then pre-merge global review count",
            "B2": "pre-merge count of REVIEWED edges",
            "B3": "pre-merge exponentially decayed REVIEWED edge count",
            "G1": "sum over touched non-infra modules of unit module weight times role-weighted, merge-time-decayed prior PR expertise divided by each prior PR's touched-module count",
            "G2": "G1 plus neighbor_factor times DEPENDS_ON neighbor expertise for touched integrations with fewer than five prior PRs",
            "module_weight": 1.0,
            "broad_pr_downweight": "Each prior PR contribution is divided by its number of TOUCHES edges across all module kinds.",
            "tie_break": "global popularity before login order; B1 first ranks by owner count.",
        },
        "static_relationship_caveat": "DECLARED_OWNER and DEPENDS_ON have no valid-from history in this graph; B1 and G2 use current manifest/source snapshots, so post-target relationship changes cannot be ruled out.",
        "oracle_definition": "Fraction with at least one true reviewer who had a REVIEWED edge on a prior PR before the target merge time.",
        "tuning": {
            "validation_primary_n": len(validation_records),
            "frozen": frozen,
            **tuning,
        },
        "test": test_report,
        "cypher_latency_per_scored_pr": latency_summary(test_records),
        "query_evidence": {
            "global_history_query_id": "reviewer_global_history_v1",
            "expertise_query_id": "reviewer_module_expertise_v1",
            "integration_history_query_id": "reviewer_integration_coldness_v1",
            "evidence_pr_numbers_are_returned_for_each_person_module_role_aggregate": True,
        },
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print_report(output)
    return output


def _number(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def print_report(output: dict[str, Any]) -> None:
    console = Console()
    console.print(f"Validation-frozen parameters: {output['tuning']['frozen']}")
    table = Table(title="Held-out reviewer prediction")
    for heading in (
        "Variant", "Population", "Slice", "Group", "Method", "N", "Hit@1",
        "Hit@3", "Hit@5", "Recall@5", "MRR",
    ):
        table.add_column(
            heading,
            justify="right" if heading in {"N", "Hit@1", "Hit@3", "Hit@5", "Recall@5", "MRR"} else "left",
        )
    for variant, populations in output["test"].items():
        for population_name, population in populations.items():
            groups: list[tuple[str, str, dict[str, Any]]] = [("all", "all", population)]
            for dimension, labels in population["slices"].items():
                groups.extend((dimension, label, details) for label, details in labels.items())
            for dimension, label, details in groups:
                for method, metrics in details["methods"].items():
                    table.add_row(
                        variant, population_name, dimension, label, method,
                        str(details["n"]), _number(metrics["hit@1"]),
                        _number(metrics["hit@3"]), _number(metrics["hit@5"]),
                        _number(metrics["recall@5"]), _number(metrics["MRR"]),
                    )
    console.print(table)
    console.print("Paired 95% bootstrap intervals for hit@3 differences (G1/G2 minus each baseline):")
    for variant, populations in output["test"].items():
        for population_name, population in populations.items():
            console.print(f"  {variant}/{population_name}: {population['bootstrap_hit3_differences']}")
    console.print(f"Cypher latency per scored PR: {output['cypher_latency_per_scored_pr']}")