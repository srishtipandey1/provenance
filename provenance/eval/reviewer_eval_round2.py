from __future__ import annotations

import json
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from falkordb import FalkorDB
from rich.console import Console
from rich.table import Table

from provenance.config import settings
from provenance.eval.reviewer_eval import (
    AUTHOR_WEIGHTS,
    BOOTSTRAP_RESAMPLES,
    DEFAULT_GRAPH,
    DEFAULT_RESULTS_PATH,
    EXPERTISE_QUERY,
    HALF_LIFE_DAYS,
    LN_2,
    expertise_scores,
    load_ground_truth,
    load_static_graph_data,
    load_targets,
    prepare_split,
    _per_pr_metrics,
    rank_candidates,
)


ROOT = Path(__file__).resolve().parents[2]
ROUND1_RESULTS_PATH = DEFAULT_RESULTS_PATH
ROUND2_RESULTS_PATH = DEFAULT_RESULTS_PATH.parent / "reviewer_eval_round2.json"
PER_PR_RESULTS_PATH = DEFAULT_RESULTS_PATH.parent / "reviewer_eval_round2_per_pr.json"
G3_LAMBDAS = (0.0, 0.25, 0.5, 1.0, 2.0)
ROUND2_BOOTSTRAP_SEED = 20261016
FROZEN_B3_HALF_LIFE = 14
FROZEN_G1_HALF_LIFE = 90
FROZEN_G1_AUTHOR_WEIGHT = 0.5
FROZEN_G2_HALF_LIFE = 90
FROZEN_G2_AUTHOR_WEIGHT = 0.0
FROZEN_G2_NEIGHBOR_FACTOR = 0.3

GLOBAL_EVIDENCE_QUERY = f"""
MATCH (person:Person)-[review:REVIEWED]->(pr:PR)
WHERE pr.merged_ts < $as_of AND review.ts < $as_of
RETURN person.login, person.is_bot, count(review),
       sum(exp(-{LN_2} * ($as_of - review.ts) / $half_life_14)),
       sum(exp(-{LN_2} * ($as_of - review.ts) / $half_life_90)),
       collect(DISTINCT pr.number)
ORDER BY person.login
"""

INTEGRATION_PRIOR_PRS_QUERY = """
MATCH (history:PR)-[touch:TOUCHES]->(module:Module)
WHERE history.merged_ts < $as_of AND module.key IN $integration_keys
RETURN module.key, count(touch), collect(DISTINCT history.number)
ORDER BY module.key
"""


@dataclass
class FoldHistory:
    as_of: int
    popularity: dict[str, int] = field(default_factory=dict)
    recency: dict[str, float] = field(default_factory=dict)
    recency_90: dict[str, float] = field(default_factory=dict)
    is_bot: dict[str, bool] = field(default_factory=dict)
    global_review_prs: dict[str, list[int]] = field(default_factory=dict)
    review_by_module: dict[str, dict[str, float]] = field(default_factory=dict)
    author_by_module: dict[str, dict[str, float]] = field(default_factory=dict)
    review_prs_by_module: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    author_prs_by_module: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    integration_pr_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    integration_history_prs: dict[str, set[int]] = field(default_factory=dict)
    integration_pr_count_by_target: dict[int, int] = field(default_factory=dict)
    query_latency_ms: dict[str, float] = field(default_factory=dict)


def _rows(graph: Any, query: str, params: dict[str, Any]) -> list[list[Any]]:
    return graph.ro_query(query, params).result_set


def load_fold_history(
    graph: Any,
    *,
    as_of: int,
    module_keys: set[str],
    targets: list[dict[str, Any]],
) -> FoldHistory:
    history = FoldHistory(as_of=as_of)
    started = time.perf_counter()
    global_rows = _rows(
        graph,
        GLOBAL_EVIDENCE_QUERY,
        {
            "as_of": as_of,
            "half_life_14": FROZEN_B3_HALF_LIFE * 86400,
            "half_life_90": FROZEN_G1_HALF_LIFE * 86400,
        },
    )
    history.query_latency_ms["global"] = (time.perf_counter() - started) * 1000
    for login, is_bot, review_count, decay_14, decay_90, pr_numbers in global_rows:
        login = str(login).casefold()
        history.is_bot[login] = bool(is_bot)
        history.popularity[login] = int(review_count or 0)
        history.recency[login] = float(decay_14 or 0.0)
        history.recency_90[login] = float(decay_90 or 0.0)
        history.global_review_prs[login] = [int(number) for number in pr_numbers or []]

    if module_keys:
        half_life_params = {
            f"half_life_{days}": days * 86400 for days in HALF_LIFE_DAYS
        }
        started = time.perf_counter()
        expertise_rows = _rows(
            graph,
            EXPERTISE_QUERY,
            {"as_of": as_of, "module_keys": sorted(module_keys), **half_life_params},
        )
        history.query_latency_ms["expertise"] = (time.perf_counter() - started) * 1000
        for row in expertise_rows:
            role, login, is_bot, module_key, decay_14, _, _, decay_90, _, pr_numbers = row
            login = str(login).casefold()
            module_key = str(module_key)
            history.is_bot[login] = bool(is_bot)
            scores = history.review_by_module if role == "review" else history.author_by_module
            evidence = (
                history.review_prs_by_module
                if role == "review"
                else history.author_prs_by_module
            )
            scores.setdefault(module_key, {})[login] = float(
                decay_14 if role == "review" else decay_90 or 0.0
            )
            if role == "review":
                scores[module_key][login] = float(decay_90 or 0.0)
            evidence.setdefault(module_key, {})[login] = [
                int(number) for number in pr_numbers or []
            ]
    else:
        history.query_latency_ms["expertise"] = 0.0

    integration_keys = {
        key for target in targets for key in target["integration_keys"]
    }
    if integration_keys:
        started = time.perf_counter()
        integration_rows = _rows(
            graph,
            INTEGRATION_PRIOR_PRS_QUERY,
            {"as_of": as_of, "integration_keys": sorted(integration_keys)},
        )
        history.query_latency_ms["integration_counts"] = (
            time.perf_counter() - started
        ) * 1000
        for module_key, _, pr_numbers in integration_rows:
            history.integration_history_prs[str(module_key)] = {
                int(number) for number in pr_numbers or []
            }
        for target in targets:
            number = int(target["number"])
            prior_prs: set[int] = set()
            for module_key in target["integration_keys"]:
                module_prs = history.integration_history_prs.get(module_key, set())
                history.integration_pr_counts.setdefault(number, {})[module_key] = len(
                    module_prs
                )
                prior_prs.update(module_prs)
            history.integration_pr_count_by_target[number] = len(prior_prs)
    else:
        history.query_latency_ms["integration_counts"] = 0.0
    return history


def prepare_window(
    graph: Any,
    *,
    window_start: int,
    window_end: int,
    owners: dict[str, set[str]],
    owner_is_bot: dict[str, bool],
    dependencies: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], FoldHistory]:
    targets = load_targets(graph, window_start, window_end)
    reviewers, approvers = load_ground_truth(graph, window_start, window_end)
    eligible: list[dict[str, Any]] = []
    module_keys: set[str] = set()
    for number, target in sorted(
        targets.items(), key=lambda item: (item[1]["merged_ts"], item[0])
    ):
        integration_keys = {
            key for key, kind in target["modules"].items() if kind == "integration"
        }
        if target["author_is_bot"] or not reviewers.get(number) or not integration_keys:
            continue
        touched_module_keys = set(target["modules"])
        module_keys.update(touched_module_keys)
        for integration_key in integration_keys:
            module_keys.update(dependencies.get(integration_key, set()))
        owner_people = set().union(*(owners.get(key, set()) for key in integration_keys))
        eligible.append(
            {
                **target,
                "module_keys": touched_module_keys,
                "integration_keys": integration_keys,
                "owner_people": owner_people,
                "owner_count": len(owner_people),
                "true_reviewers": reviewers[number],
                "true_approvers": approvers.get(number, set()),
            }
        )

    history = load_fold_history(
        graph,
        as_of=window_start,
        module_keys=module_keys,
        targets=eligible,
    )
    records = []
    for target in eligible:
        number = target["number"]
        records.append(
            {
                "target": target,
                "history": history,
                "owners": owners,
                "owner_is_bot": owner_is_bot,
                "dependencies": dependencies,
                "prior_integration_pr_count": history.integration_pr_count_by_target.get(
                    number, 0
                ),
            }
        )
    return records, history


def _person_bot_map(record: dict[str, Any]) -> dict[str, bool]:
    return {**record["owner_is_bot"], **record["history"].is_bot}


def _g1_scores(record: dict[str, Any]) -> dict[str, float]:
    target = record["target"]
    history = record["history"]
    scores: dict[str, float] = defaultdict(float)
    for module_key, kind in target["modules"].items():
        if kind == "infra":
            continue
        for login, value in history.review_by_module.get(module_key, {}).items():
            scores[login] += value
        for login, value in history.author_by_module.get(module_key, {}).items():
            scores[login] += FROZEN_G1_AUTHOR_WEIGHT * value
    return dict(scores)


def _g2_scores(record: dict[str, Any]) -> dict[str, float]:
    target = record["target"]
    history = record["history"]
    weights: Counter[str] = Counter(
        {
            key: 1.0
            for key, kind in target["modules"].items()
            if kind != "infra"
        }
    )
    counts = history.integration_pr_counts.get(target["number"], {})
    for integration_key in target["integration_keys"]:
        if counts.get(integration_key, 0) < 5:
            for neighbor in record["dependencies"].get(integration_key, set()):
                weights[neighbor] += FROZEN_G2_NEIGHBOR_FACTOR
    scores: dict[str, float] = defaultdict(float)
    for module_key, weight in weights.items():
        for login, value in history.review_by_module.get(module_key, {}).items():
            scores[login] += weight * value
    return dict(scores)


def _base_rankings(record: dict[str, Any]) -> dict[str, Any]:
    target = record["target"]
    history = record["history"]
    author = target["author"]
    popularity = history.popularity
    is_bot = _person_bot_map(record)
    owner_counts: Counter[str] = Counter()
    for module_key in target["integration_keys"]:
        owner_counts.update(record["owners"].get(module_key, set()))
    g1_scores = _g1_scores(record)
    g1_rank = rank_candidates(
        g1_scores, popularity=popularity, is_bot=is_bot, author=author
    )
    b3_scores = history.recency
    b3_rank = rank_candidates(
        b3_scores, popularity=popularity, is_bot=is_bot, author=author
    )
    b2_rank = rank_candidates(
        {login: float(score) for login, score in popularity.items()},
        popularity=popularity,
        is_bot=is_bot,
        author=author,
    )
    g2_rank = rank_candidates(
        _g2_scores(record), popularity=popularity, is_bot=is_bot, author=author
    )
    b1_rank = rank_candidates(
        dict(owner_counts), popularity=popularity, is_bot=is_bot, author=author
    )
    return {
        "scores": {"G1": g1_scores, "B3": b3_scores},
        "rankings": {
            "B1": b1_rank,
            "B2": b2_rank,
            "B3": b3_rank,
            "G1": g1_rank,
            "G2": g2_rank,
        },
    }


def normalized_g3_scores(
    g1_scores: dict[str, float],
    b3_scores: dict[str, float],
    g1_candidates: list[str],
    b3_candidates: list[str],
    lambda_value: float,
) -> dict[str, float]:
    candidate_order = list(dict.fromkeys([*g1_candidates, *b3_candidates[:20]]))
    if not candidate_order:
        return {}
    maximum_g1 = max((g1_scores.get(login, 0.0) for login in candidate_order), default=0.0)
    maximum_b3 = max((b3_scores.get(login, 0.0) for login in candidate_order), default=0.0)
    return {
        login: (g1_scores.get(login, 0.0) / maximum_g1 if maximum_g1 > 0 else 0.0)
        + lambda_value
        * (b3_scores.get(login, 0.0) / maximum_b3 if maximum_b3 > 0 else 0.0)
        for login in candidate_order
    }


def _g3_ranking(
    record: dict[str, Any],
    base: dict[str, Any],
    lambda_value: float,
    *,
    include_evidence: bool,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    g1_scores = base["scores"]["G1"]
    b3_scores = base["scores"]["B3"]
    g1_rank = base["rankings"]["G1"]
    b3_rank = base["rankings"]["B3"]
    candidate_order = list(dict.fromkeys([*g1_rank, *b3_rank[:20]]))
    scores = normalized_g3_scores(
        g1_scores, b3_scores, g1_rank, b3_rank, lambda_value
    )
    if not candidate_order:
        return [], {}
    target = record["target"]
    ranking = rank_candidates(
        scores,
        popularity=record["history"].popularity,
        is_bot=_person_bot_map(record),
        author=target["author"],
    )
    evidence: dict[str, dict[str, Any]] = {}
    if include_evidence:
        for login in candidate_order:
            module_support = []
            for module_key, kind in target["modules"].items():
                if kind == "infra":
                    continue
                reviewed = record["history"].review_prs_by_module.get(module_key, {}).get(login, [])
                authored = record["history"].author_prs_by_module.get(module_key, {}).get(login, [])
                if reviewed or authored:
                    module_support.append(
                        {"module_key": module_key, "review_prs": reviewed, "authored_prs": authored}
                    )
            evidence[login] = {
                "score": scores[login],
                "g1_score": g1_scores.get(login, 0.0),
                "b3_score": b3_scores.get(login, 0.0),
                "review_prs_global": record["history"].global_review_prs.get(login, []),
                "module_support": module_support,
            }
    return ranking, evidence


def _per_pr_rankings(record: dict[str, Any], lambda_value: float) -> dict[str, list[str]]:
    target, history = record["target"], record["history"]
    author = target["author"]
    popularity = history.popularity
    is_bot = {**record["owner_is_bot"], **history.is_bot}
    g1_scores = expertise_scores(
        target,
        history,
        half_life=FROZEN_G1_HALF_LIFE,
        author_weight=FROZEN_G1_AUTHOR_WEIGHT,
    )
    b3_scores = {
        login: values.get(FROZEN_B3_HALF_LIFE, 0.0)
        for login, values in history.recency.items()
    }
    g1_rank = rank_candidates(
        g1_scores, popularity=popularity, is_bot=is_bot, author=author
    )
    b3_rank = rank_candidates(
        b3_scores, popularity=popularity, is_bot=is_bot, author=author
    )
    g3_scores = normalized_g3_scores(
        g1_scores, b3_scores, g1_rank, b3_rank, lambda_value
    )
    g3_rank = rank_candidates(
        g3_scores, popularity=popularity, is_bot=is_bot, author=author
    )
    record["rankings"] = {"B3": b3_rank, "G1": g1_rank, "G3": g3_rank}
    return record["rankings"]


def run_per_pr_comparison(
    *,
    graph_name: str = DEFAULT_GRAPH,
    output_path: Path = PER_PR_RESULTS_PATH,
) -> dict[str, Any]:
    round1 = json.loads(ROUND1_RESULTS_PATH.read_text(encoding="utf-8"))
    round2 = json.loads(ROUND2_RESULTS_PATH.read_text(encoding="utf-8"))
    frozen = round1["tuning"]["frozen"]
    if frozen != {
        "recent_half_life_days": FROZEN_B3_HALF_LIFE,
        "half_life_days": FROZEN_G1_HALF_LIFE,
        "author_weight": FROZEN_G1_AUTHOR_WEIGHT,
        "g2_half_life_days": FROZEN_G2_HALF_LIFE,
        "g2_author_weight": FROZEN_G2_AUTHOR_WEIGHT,
        "neighbor_factor": FROZEN_G2_NEIGHBOR_FACTOR,
    }:
        raise ValueError("Round-1 frozen parameters differ from the expected immutable settings.")

    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph(graph_name)
    _, _, windows = _fold_windows(round1)
    owners, dependencies, owner_is_bot = load_static_graph_data(graph)
    folds: dict[str, Any] = {}
    for fold_name in ("A", "B", "C"):
        window = windows[fold_name]
        records = prepare_split(
            graph,
            window_start=window["test_start"],
            window_end=window["test_end"],
            primary_only=True,
            owners=owners,
            dependencies=dependencies,
            owner_is_bot=owner_is_bot,
        )
        lambda_value = float(round2["folds"][fold_name]["lambda"])
        for record in records:
            _per_pr_rankings(record, lambda_value)
        metrics = _metrics(records, ("B3", "G1", "G3"))
        folds[fold_name] = {
            "label": round2["folds"][fold_name]["label"],
            "test_start_utc": round2["folds"][fold_name]["test_start_utc"],
            "test_end_utc": round2["folds"][fold_name]["test_end_utc"],
            "history_regime": "per-target as-of: history PR merged_ts < target PR merged_ts",
            "lambda_reused_from_round2": lambda_value,
            "tuning_performed": False,
            "test_metrics": metrics,
        }

    round1_b3_hit3 = round1["test"]["reviewers"]["primary"]["methods"]["B3"]["hit@3"]
    round2_window_b3_hit3 = round2["folds"]["A"]["test_metrics"]["methods"]["B3"]["hit@3"]
    per_pr_b3_hit3 = folds["A"]["test_metrics"]["methods"]["B3"]["hit@3"]
    output = {
        "graph": graph_name,
        "round1_artifact_modified": False,
        "round1_as_of_utc": round1["evaluation_as_of_utc"],
        "round2_lambda_source": str(ROUND2_RESULTS_PATH.relative_to(ROOT)).replace("\\", "/"),
        "base_parameters_frozen_from_round1": frozen,
        "history_regime": "per-target as-of history, matching reviewer_eval.py; no test-window lambda tuning",
        "methods": ["B3", "G1", "G3"],
        "folds": folds,
        "regime_comparison": {
            "fold_A_n": folds["A"]["test_metrics"]["n"],
            "round1_per_pr_B3_hit@3": round1_b3_hit3,
            "round2_fixed_window_B3_hit@3": round2_window_b3_hit3,
            "round2_per_pr_B3_hit@3": per_pr_b3_hit3,
            "per_pr_minus_round1": per_pr_b3_hit3 - round1_b3_hit3,
            "per_pr_minus_fixed_window": per_pr_b3_hit3 - round2_window_b3_hit3,
            "matches_round1_within_1e-9": abs(per_pr_b3_hit3 - round1_b3_hit3) <= 1e-9,
        },
        "regime_comparison_note": "Fold A B3 compares identical PRs and frozen parameters; per-PR history admits earlier merged PRs from within the test window, while fixed-window history does not.",
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    table = Table(title="Per-PR as-of regime (round 2 frozen lambdas)")
    for heading in ("Fold", "Method", "N", "Hit@1", "Hit@3", "Hit@5", "Recall@5", "MRR"):
        table.add_column(heading, justify="right" if heading not in {"Fold", "Method"} else "left")
    for fold_name, fold in folds.items():
        for method, values in fold["test_metrics"]["methods"].items():
            table.add_row(
                fold_name + (" POST-HOC" if fold_name == "A" else ""),
                method,
                str(fold["test_metrics"]["n"]),
                *[f"{values[key]:.3f}" for key in ("hit@1", "hit@3", "hit@5", "recall@5", "MRR")],
            )
    Console().print(table)
    return output


def rank_record(
    record: dict[str, Any],
    lambda_value: float,
    *,
    diagnostic: bool,
    include_evidence: bool = True,
) -> dict[str, list[str]]:
    base = record.get("_base_rankings")
    if base is None:
        base = _base_rankings(record)
        record["_base_rankings"] = base
    g3_rank, g3_evidence = _g3_ranking(
        record, base, lambda_value, include_evidence=include_evidence
    )
    g1_rank = base["rankings"]["G1"]
    g3b_rank = list(g1_rank)
    for login in base["rankings"]["B3"]:
        if login not in g3b_rank:
            g3b_rank.append(login)
        if len(g3b_rank) >= 10:
            break
    if include_evidence:
        record["g3_evidence"] = g3_evidence
    record["rankings"] = {
        **base["rankings"],
        "G3": g3_rank,
        "G3b": g3b_rank[:10],
    }
    if not diagnostic:
        record["rankings"] = {
            method: record["rankings"][method]
            for method in ("B2", "B3", "G1", "G3", "G3b")
        }
    return record["rankings"]


def _metrics(records: list[dict[str, Any]], methods: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {"n": len(records), "methods": {}}
    for method in methods:
        values = []
        for record in records:
            values.append(
                _per_pr_metrics(
                    record["rankings"][method], record["target"]["true_reviewers"]
                )
            )
        result["methods"][method] = {
            metric: statistics.fmean(row[metric] for row in values) if values else None
            for metric in ("hit@1", "hit@3", "hit@5", "recall@5", "MRR")
        }
    return result


def _slice_records(records: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {
        "integration_declared_owner_count": {"0": [], "1": [], "2+": []},
        "cold_warm": {"cold": [], "warm": []},
        "integrations_touched": {"1": [], "several": []},
        "true_reviewers_include_declared_owner": {"yes": [], "no": []},
    }
    for record in records:
        target = record["target"]
        owner_count = target["owner_count"]
        owner_bucket = "0" if owner_count == 0 else "1" if owner_count == 1 else "2+"
        groups["integration_declared_owner_count"][owner_bucket].append(record)
        cold = record["prior_integration_pr_count"] < 5
        groups["cold_warm"]["cold" if cold else "warm"].append(record)
        integration_bucket = "1" if len(target["integration_keys"]) == 1 else "several"
        groups["integrations_touched"][integration_bucket].append(record)
        has_owner = bool(target["true_reviewers"] & target["owner_people"])
        groups["true_reviewers_include_declared_owner"]["yes" if has_owner else "no"].append(record)
    return groups


def _report(records: list[dict[str, Any]], methods: tuple[str, ...]) -> dict[str, Any]:
    return {
        **_metrics(records, methods),
        "slices": {
            dimension: {
                label: _metrics(group, methods)
                for label, group in labels.items()
            }
            for dimension, labels in _slice_records(records).items()
        },
    }


def tune_lambda(records: list[dict[str, Any]]) -> tuple[float, list[dict[str, float]]]:
    if not records:
        raise ValueError("Lambda validation window contains no eligible primary PRs.")
    grid = []
    for lambda_value in G3_LAMBDAS:
        for record in records:
            rank_record(record, lambda_value, diagnostic=False, include_evidence=False)
        metrics = _metrics(records, ("G3",))
        g3 = metrics["methods"]["G3"]
        grid.append(
            {
                "lambda": lambda_value,
                "n": metrics["n"],
                "hit@3": g3["hit@3"],
                "recall@5": g3["recall@5"],
                "MRR": g3["MRR"],
            }
        )
    winner = max(grid, key=lambda row: (row["hit@3"], row["MRR"], row["recall@5"]))
    return float(winner["lambda"]), grid


def _confidence_intervals(
    records: list[dict[str, Any]],
    *,
    methods: tuple[str, ...] = ("B3", "G1", "G3"),
) -> dict[str, Any]:
    comparisons = (("G1", "B3"), ("G3", "B3"), ("G3", "G1"))
    metrics = ("hit@1", "hit@3", "hit@5", "MRR")
    result: dict[str, Any] = {"n": len(records), "resamples": BOOTSTRAP_RESAMPLES, "comparisons": {}}
    if not records:
        return result
    rng = random.Random(ROUND2_BOOTSTRAP_SEED)
    indices = [
        [rng.randrange(len(records)) for _ in range(len(records))]
        for _ in range(BOOTSTRAP_RESAMPLES)
    ]
    for method_a, method_b in comparisons:
        result["comparisons"][f"{method_a}-vs-{method_b}"] = {}
        for metric in metrics:
            paired = [
                _per_pr_metrics(record["rankings"][method_a], record["target"]["true_reviewers"])[metric]
                - _per_pr_metrics(record["rankings"][method_b], record["target"]["true_reviewers"])[metric]
                for record in records
            ]
            means = sorted(statistics.fmean(paired[index] for index in sample) for sample in indices)
            result["comparisons"][f"{method_a}-vs-{method_b}"][metric] = {
                "mean_difference": statistics.fmean(paired),
                "ci95": [
                    means[int(0.025 * (BOOTSTRAP_RESAMPLES - 1))],
                    means[int(0.975 * (BOOTSTRAP_RESAMPLES - 1))],
                ],
            }
    return result


def _oracle_diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    global_hits = 0
    module_hits = 0
    for record in records:
        target = record["target"]
        history = record["history"]
        truth = target["true_reviewers"]
        global_hits += bool(truth & set(history.popularity))
        module_candidates = set()
        for module_key in target["module_keys"]:
            module_candidates.update(history.review_by_module.get(module_key, {}))
            module_candidates.update(history.author_by_module.get(module_key, {}))
        module_hits += bool(truth & module_candidates)
    count = len(records)
    return {
        "n": count,
        "global": {"n": count, "count": global_hits, "share": global_hits / count if count else None},
        "module_local": {"n": count, "count": module_hits, "share": module_hits / count if count else None},
        "module_local_definition": "At least one true reviewer previously reviewed or authored a prior PR touching any module touched by the target; all module kinds are included.",
    }


def _candidate_length_diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    lengths = [len(record["rankings"]["G1"]) for record in records]
    distribution = Counter(lengths)
    buckets = Counter()
    for length in lengths:
        bucket = "0" if length == 0 else "1-4" if length < 5 else "5-9" if length < 10 else "10-19" if length < 20 else "20+"
        buckets[bucket] += 1
    enough = [record for record in records if len(record["rankings"]["G1"]) >= 5]
    hit5 = [
        _per_pr_metrics(record["rankings"]["G1"], record["target"]["true_reviewers"])["hit@5"]
        for record in enough
    ]
    under_five = sum(length < 5 for length in lengths)
    return {
        "n": len(records),
        "exact_length_distribution": dict(sorted(distribution.items())),
        "length_buckets": dict(buckets),
        "fewer_than_5": {
            "n": under_five,
            "share": under_five / len(lengths) if lengths else None,
        },
        "g1_hit@5_when_candidate_count_at_least_5": {
            "n": len(enough),
            "hit@5": statistics.fmean(hit5) if hit5 else None,
        },
    }


def _fold_windows(round1: dict[str, Any]) -> tuple[int, int, dict[str, dict[str, int]]]:
    day = 86400
    end_a = int(datetime.fromisoformat(round1["window"]["end_utc"].replace("Z", "+00:00")).timestamp())
    start_a = int(datetime.fromisoformat(round1["window"]["test_start_utc"].replace("Z", "+00:00")).timestamp())
    graph_start = int(datetime.fromisoformat(round1["window"]["start_utc"].replace("Z", "+00:00")).timestamp())
    windows = {
        "A": {"test_start": start_a, "test_end": end_a, "validation_start": start_a - day * 30, "validation_end": start_a},
        "B": {"test_start": start_a - day * 30, "test_end": start_a, "validation_start": start_a - day * 60, "validation_end": start_a - day * 30},
        "C": {"test_start": start_a - day * 60, "test_end": start_a - day * 30, "validation_start": start_a - day * 90, "validation_end": start_a - day * 60},
    }
    return graph_start, end_a, windows


def _print_metrics_table(title: str, records: list[dict[str, Any]], methods: tuple[str, ...]) -> None:
    table = Table(title=title)
    for heading in ("Slice", "Group", "Method", "N", "Hit@1", "Hit@3", "Hit@5", "Recall@5", "MRR"):
        table.add_column(heading, justify="right" if heading in {"N", "Hit@1", "Hit@3", "Hit@5", "Recall@5", "MRR"} else "left")
    groups: list[tuple[str, str, list[dict[str, Any]]]] = [("all", "all", records)]
    for dimension, labels in _slice_records(records).items():
        groups.extend((dimension, label, group) for label, group in labels.items())
    for dimension, label, group in groups:
        metrics = _metrics(group, methods)
        for method in methods:
            values = metrics["methods"][method]
            table.add_row(
                dimension,
                label,
                method,
                str(metrics["n"]),
                *["n/a" if values[key] is None else f"{values[key]:.3f}" for key in ("hit@1", "hit@3", "hit@5", "recall@5", "MRR")],
            )
    Console().print(table)


def _print_summary_table(title: str, datasets: list[tuple[str, list[dict[str, Any]]]], methods: tuple[str, ...]) -> None:
    table = Table(title=title)
    for heading in ("Fold", "Method", "N", "Hit@1", "Hit@3", "Hit@5", "Recall@5", "MRR"):
        table.add_column(heading, justify="right" if heading in {"N", "Hit@1", "Hit@3", "Hit@5", "Recall@5", "MRR"} else "left")
    for fold, records in datasets:
        metrics = _metrics(records, methods)
        for method in methods:
            values = metrics["methods"][method]
            table.add_row(fold, method, str(metrics["n"]), *["n/a" if values[key] is None else f"{values[key]:.3f}" for key in ("hit@1", "hit@3", "hit@5", "recall@5", "MRR")])
    Console().print(table)


def run_round2(
    *,
    graph_name: str = DEFAULT_GRAPH,
    output_path: Path = ROUND2_RESULTS_PATH,
) -> dict[str, Any]:
    round1 = json.loads(ROUND1_RESULTS_PATH.read_text(encoding="utf-8"))
    frozen = round1["tuning"]["frozen"]
    expected_frozen = {
        "recent_half_life_days": FROZEN_B3_HALF_LIFE,
        "half_life_days": FROZEN_G1_HALF_LIFE,
        "author_weight": FROZEN_G1_AUTHOR_WEIGHT,
        "g2_half_life_days": FROZEN_G2_HALF_LIFE,
        "g2_author_weight": FROZEN_G2_AUTHOR_WEIGHT,
        "neighbor_factor": FROZEN_G2_NEIGHBOR_FACTOR,
    }
    if frozen != expected_frozen:
        raise ValueError("Round-1 frozen parameters differ from the expected immutable settings.")

    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph(graph_name)
    graph_start, end_a, windows = _fold_windows(round1)
    owners, dependencies, owner_is_bot = load_static_graph_data(graph)
    fold_records: dict[str, list[dict[str, Any]]] = {}
    fold_meta: dict[str, dict[str, Any]] = {}
    lambda_tuning: dict[str, Any] = {}
    query_evidence: dict[str, Any] = {}

    day = 86400
    first_start = windows["A"]["test_start"]
    interval_windows = {
        "A": (windows["A"]["test_start"], windows["A"]["test_end"]),
        "B": (first_start - day * 30, first_start),
        "C": (first_start - day * 60, first_start - day * 30),
        "D": (first_start - day * 90, first_start - day * 60),
    }
    window_records: dict[str, list[dict[str, Any]]] = {}
    window_histories: dict[str, FoldHistory] = {}
    for interval_name, (interval_start, interval_end) in interval_windows.items():
        interval_records, interval_history = prepare_window(
            graph,
            window_start=interval_start,
            window_end=interval_end,
            owners=owners,
            owner_is_bot=owner_is_bot,
            dependencies=dependencies,
        )
        window_records[interval_name] = interval_records
        window_histories[interval_name] = interval_history

    for fold_name in ("A", "B", "C"):
        window = windows[fold_name]
        validation_window = chr(ord(fold_name) + 1)
        validation_records = window_records[validation_window]
        validation_history = window_histories[validation_window]
        lambda_value, grid = tune_lambda(validation_records)
        test_records = window_records[fold_name]
        test_history = window_histories[fold_name]
        for record in test_records:
            rank_record(
                record,
                lambda_value,
                diagnostic=(fold_name == "A"),
                include_evidence=True,
            )
        fold_records[fold_name] = test_records
        history_days = (window["test_start"] - graph_start) // 86400
        fold_meta[fold_name] = {
            "label": "POST-HOC: G3 was designed after observing this test window." if fold_name == "A" else "Rolling-origin held-out fold.",
            "test_start_utc": datetime.fromtimestamp(window["test_start"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "test_end_utc": datetime.fromtimestamp(window["test_end"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "validation_start_utc": datetime.fromtimestamp(window["validation_start"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "validation_end_utc": datetime.fromtimestamp(window["validation_end"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "history_start_utc": datetime.fromtimestamp(graph_start, timezone.utc).isoformat().replace("+00:00", "Z"),
            "history_cutoff_utc": datetime.fromtimestamp(window["test_start"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "available_history_days": history_days,
            "validation_primary_n": len(validation_records),
            "test_primary_n": len(test_records),
            "lambda": lambda_value,
            "test_metrics": _report(test_records, ("B2", "B3", "G1", "G3", "G3b")),
        }
        lambda_tuning[fold_name] = {
            "validation_history_cutoff_utc": datetime.fromtimestamp(window["validation_start"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "validation_primary_n": len(validation_records),
            "objective": "hit@3, ties by MRR then recall@5",
            "grid": grid,
        }
        query_evidence[fold_name] = {
            "validation": _query_evidence(validation_history),
            "test": _query_evidence(test_history),
        }

    diagnostic_records = fold_records["A"]
    diagnostics = {
        "fold_label": "POST-HOC",
        "test_primary_n": len(diagnostic_records),
        "g1_candidate_lengths": _candidate_length_diagnostics(diagnostic_records),
        "oracle": _oracle_diagnostics(diagnostic_records),
        "all_methods_by_slice": _report(
            diagnostic_records,
            ("B1", "B2", "B3", "G1", "G2", "G3", "G3b"),
        ),
    }

    confirmatory_records = [*fold_records["B"], *fold_records["C"]]
    pooled_records_all = [*fold_records["A"], *confirmatory_records]
    methods = ("B2", "B3", "G1", "G3", "G3b")
    pooled_bc = _report(confirmatory_records, methods)
    pooled_abc = _report(pooled_records_all, methods)
    confidence_intervals = {
        "pooled_BC": _confidence_intervals(confirmatory_records),
        "fold_A_POST_HOC": _confidence_intervals(fold_records["A"]),
    }
    g3_metrics = pooled_bc["methods"]["G3"]
    g1_metrics = pooled_bc["methods"]["G1"]
    b3_metrics = pooled_bc["methods"]["B3"]
    g3_beats_both = (
        g3_metrics["hit@3"] > g1_metrics["hit@3"]
        and g3_metrics["hit@3"] > b3_metrics["hit@3"]
    )
    conclusion = (
        "On pooled Folds B and C, G3 beats both parents at hit@3."
        if g3_beats_both
        else "On pooled Folds B and C, G3 does not beat both parents at hit@3; no overall hybrid win is claimed."
    )
    output = {
        "graph": graph_name,
        "round1_artifact": str(ROUND1_RESULTS_PATH.relative_to(Path(__file__).resolve().parents[2])).replace("\\", "/"),
        "round1_artifact_modified": False,
        "base_parameters_frozen_from_round1": frozen,
        "folds": fold_meta,
        "lambda_tuning": lambda_tuning,
        "diagnostics_fold_A": diagnostics,
        "pooled_BC": pooled_bc,
        "pooled_ABC_descriptive": pooled_abc,
        "confidence_intervals": confidence_intervals,
        "confidence_interval_metrics": ["hit@1", "hit@3", "hit@5", "MRR"],
        "conclusion": conclusion,
        "fold_validation_note": "Lambda is tuned on the immediately preceding 30-day interval for each origin. That interval may be a test interval for a later-origin fold, but it is strictly before the test start of the fold whose lambda is being tuned; no fold uses its own test labels.",
        "history_rule": "Each window uses only PR history with merged_ts strictly less than that window's start; no PR from the validation or test window is used as history for that window.",
        "available_history_caveat": "The graph contains only the cached 180-day window; history lengths are truncated to that availability.",
        "static_relationship_caveat": "DECLARED_OWNER and DEPENDS_ON are current snapshots without valid-from timestamps; diagnostics using B1/G2 inherit that caveat.",
        "query_evidence": query_evidence,
        "score_traceability": "Global reviewer scores retain prior REVIEWED PR numbers; module expertise scores retain role, person, module, and contributing PR numbers from the as-of Cypher aggregates. G3 returns per-candidate component scores and evidence references during scoring.",
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_round2(output, fold_records, confirmatory_records, pooled_records_all)
    return output


def _query_evidence(history: FoldHistory) -> dict[str, Any]:
    return {
        "as_of_merged_ts_exclusive": history.as_of,
        "global": {
            "query_id": "reviewer_round2_global_evidence_v1",
            "params": {"as_of": history.as_of, "half_life_days": [14, 90]},
            "person_aggregates": len(history.popularity),
            "review_pr_evidence_count": sum(len(numbers) for numbers in history.global_review_prs.values()),
            "latency_ms": history.query_latency_ms.get("global", 0.0),
        },
        "expertise": {
            "query_id": "reviewer_round2_module_expertise_v1",
            "params": {"as_of": history.as_of, "module_keys_scored": len(set(history.review_by_module) | set(history.author_by_module))},
            "person_module_role_aggregates": sum(map(len, history.review_by_module.values())) + sum(map(len, history.author_by_module.values())),
            "supporting_pr_evidence_count": sum(len(prs) for rows in history.review_prs_by_module.values() for prs in rows.values()) + sum(len(prs) for rows in history.author_prs_by_module.values() for prs in rows.values()),
            "latency_ms": history.query_latency_ms.get("expertise", 0.0),
        },
        "integration_history": {
            "query_id": "reviewer_round2_window_coldness_v1",
            "target_count": len(history.integration_pr_count_by_target),
            "module_count": len(history.integration_history_prs),
            "supporting_pr_evidence_count": sum(
                len(pr_numbers) for pr_numbers in history.integration_history_prs.values()
            ),
            "latency_ms": history.query_latency_ms.get("integration_counts", 0.0),
        },
    }


def _print_round2(
    output: dict[str, Any],
    fold_records: dict[str, list[dict[str, Any]]],
    pooled_bc_records: list[dict[str, Any]],
    pooled_all_records: list[dict[str, Any]],
) -> None:
    console = Console()
    console.print(f"Frozen round-1 parameters: {output['base_parameters_frozen_from_round1']}")
    console.print("G3 lambda values, tuned only on each immediately preceding validation window:")
    for fold, details in output["lambda_tuning"].items():
        console.print(f"  Fold {fold}: lambda={output['folds'][fold]['lambda']} n={details['validation_primary_n']}")
    _print_summary_table(
        "Rolling-origin reviewer prediction: per fold and pooled",
        [(f"Fold {fold}{' POST-HOC' if fold == 'A' else ''}", fold_records[fold]) for fold in ("A", "B", "C")]
        + [("Pooled B+C", pooled_bc_records), ("Pooled A+B+C (descriptive)", pooled_all_records)],
        ("B2", "B3", "G1", "G3", "G3b"),
    )
    for fold in ("A", "B", "C"):
        methods = ("B1", "B2", "B3", "G1", "G2", "G3", "G3b") if fold == "A" else ("B2", "B3", "G1", "G3", "G3b")
        _print_metrics_table(
            f"Fold {fold} slices{' (POST-HOC)' if fold == 'A' else ''}",
            fold_records[fold],
            methods,
        )
    diagnostics = output["diagnostics_fold_A"]
    console.print(f"Fold A G1 candidate length distribution: {diagnostics['g1_candidate_lengths']}")
    console.print(f"Fold A global and module-local oracles: {diagnostics['oracle']}")
    console.print("Paired 95% bootstrap intervals for G1-B3, G3-B3, and G3-G1:")
    for population, details in output["confidence_intervals"].items():
        console.print(f"  {population}: {details}")
    console.print(output["conclusion"])
