from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
from falkordb import FalkorDB
from rich.console import Console
from rich.table import Table
from sentence_transformers import SentenceTransformer

from provenance.config import settings
from provenance.eval.reviewer_eval import (
    DEFAULT_GRAPH,
    _per_pr_metrics,
    fetch_history,
    load_ground_truth,
    load_static_graph_data,
    load_targets,
    method_rankings,
    prepare_split,
)
from provenance.eval.reviewer_eval_round2 import (
    _fold_windows,
    _metrics,
    _per_pr_rankings,
)


ROOT = Path(__file__).resolve().parents[2]
RAW_PRS_PATH = ROOT / "data" / "raw" / "prs" / "pages.jsonl"
ROUND1_RESULTS_PATH = ROOT / "results" / "reviewer_eval.json"
ROUND2_RESULTS_PATH = ROOT / "results" / "reviewer_eval_round2.json"
PER_PR_RESULTS_PATH = ROOT / "results" / "reviewer_eval_round2_per_pr.json"
RAG_RESULTS_PATH = ROOT / "results" / "rag_baseline.json"
EMBEDDING_CACHE_DIR = ROOT / "data" / "cache" / "rag"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
K_VALUES = (5, 10, 20, 40)
SIMILARITY_EXPONENTS = (1.0, 2.0)
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20261016
METRIC_NAMES = ("hit@1", "hit@3", "hit@5", "recall@5", "MRR")
FROZEN_PARAMETERS = {
    "recent_half_life_days": 14,
    "half_life_days": 90,
    "author_weight": 0.5,
    "neighbor_factor": 0.3,
}


@dataclass(frozen=True)
class PRDocument:
    number: int
    merged_ts: int
    author: str
    text: str
    reviewers: tuple[str, ...]


def _timestamp(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _connection_nodes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    nodes = value.get("nodes", [])
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def build_document(raw: dict[str, Any]) -> PRDocument | None:
    merged_ts = _timestamp(raw.get("mergedAt"))
    if merged_ts is None:
        return None
    try:
        number = int(raw["number"])
    except (KeyError, TypeError, ValueError):
        return None

    author_data = raw.get("author")
    author = str(author_data.get("login", "")).casefold() if isinstance(author_data, dict) else ""
    reviewers: set[str] = set()
    for review in _connection_nodes(raw.get("reviews")):
        reviewer_data = review.get("author")
        if not isinstance(reviewer_data, dict) or reviewer_data.get("__typename") == "Bot":
            continue
        login = str(reviewer_data.get("login", "")).casefold()
        review_ts = _timestamp(review.get("submittedAt"))
        if login and login != author and review_ts is not None and review_ts < merged_ts:
            reviewers.add(login)

    labels = raw.get("labels", [])
    label_text = ", ".join(str(label) for label in labels if label) if isinstance(labels, list) else ""
    top_level_paths = {
        PurePosixPath(str(item.get("path", "")).replace("\\", "/")).parts[0]
        for item in _connection_nodes(raw.get("files"))
        if item.get("path") and PurePosixPath(str(item["path"]).replace("\\", "/")).parts
    }
    body = raw.get("body")
    body_text = body[:800] if isinstance(body, str) else ""
    text = "\n".join(
        part
        for part in (
            f"Title: {raw.get('title', '')}",
            f"Labels: {label_text}",
            f"Body: {body_text}",
            f"Changed top-level paths: {', '.join(sorted(top_level_paths))}",
        )
        if part
    )
    return PRDocument(number, merged_ts, author, text, tuple(sorted(reviewers)))


def load_documents(path: Path = RAW_PRS_PATH) -> list[PRDocument]:
    by_number: dict[int, PRDocument] = {}
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            page = json.loads(line)
            pull_requests = page.get("pull_requests", []) if isinstance(page, dict) else []
            if not isinstance(pull_requests, list):
                continue
            for raw in pull_requests:
                if not isinstance(raw, dict):
                    continue
                document = build_document(raw)
                if document is not None:
                    by_number.setdefault(document.number, document)
    return sorted(by_number.values(), key=lambda document: (document.merged_ts, document.number))


def _embedding_digest(documents: list[PRDocument], model_name: str) -> str:
    digest = hashlib.sha256(model_name.encode("utf-8"))
    for document in documents:
        digest.update(str(document.number).encode("ascii"))
        digest.update(b"\0")
        digest.update(document.text.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def load_or_encode(
    documents: list[PRDocument],
    *,
    cache_dir: Path = EMBEDDING_CACHE_DIR,
    model_name: str = EMBEDDING_MODEL,
    show_progress: bool = True,
) -> tuple[np.ndarray, bool]:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{_embedding_digest(documents, model_name)}.npz"
    if cache_path.exists():
        try:
            with np.load(cache_path, allow_pickle=False) as cached:
                vectors = np.asarray(cached["embeddings"], dtype=np.float32)
            if vectors.ndim == 2 and vectors.shape[0] == len(documents):
                return vectors, True
        except (OSError, KeyError, ValueError):
            pass

    model = SentenceTransformer(model_name, device="cpu")
    vectors = np.asarray(
        model.encode(
            [document.text for document in documents],
            batch_size=64,
            show_progress_bar=show_progress,
            convert_to_numpy=True,
            normalize_embeddings=True,
        ),
        dtype=np.float32,
    )
    if vectors.ndim != 2 or vectors.shape[0] != len(documents):
        raise ValueError("Embedding model returned an unexpected matrix shape.")
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=cache_dir, prefix=".rag-", suffix=".npz", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        np.savez_compressed(temporary, embeddings=vectors)
    os.replace(temporary_path, cache_path)
    return vectors, False


def nearest_neighbors(
    target_number: int,
    as_of: int,
    documents: list[PRDocument],
    vectors: np.ndarray,
    *,
    max_k: int = max(K_VALUES),
    index_by_number: dict[int, int] | None = None,
    document_numbers: np.ndarray | None = None,
    document_timestamps: np.ndarray | None = None,
) -> list[tuple[int, float]]:
    index_by_number = index_by_number or {
        document.number: index for index, document in enumerate(documents)
    }
    target_position = index_by_number.get(target_number)
    if target_position is None:
        raise ValueError(f"Target PR {target_number} has no cached text document.")
    numbers = document_numbers if document_numbers is not None else np.fromiter(
        (document.number for document in documents), dtype=np.int64
    )
    timestamps = document_timestamps if document_timestamps is not None else np.fromiter(
        (document.merged_ts for document in documents), dtype=np.int64
    )
    similarities = vectors @ vectors[target_position]
    eligible = np.flatnonzero((timestamps < as_of) & (numbers != target_number))
    if eligible.size == 0:
        return []
    count = min(max_k, int(eligible.size))
    candidate_scores = similarities[eligible]
    if eligible.size > count:
        chosen = np.argpartition(-candidate_scores, count - 1)[:count]
        threshold = float(np.min(candidate_scores[chosen]))
        better = eligible[candidate_scores > threshold]
        tied = eligible[candidate_scores == threshold]
        tie_order = np.argsort(numbers[tied], kind="stable")[: count - len(better)]
        eligible = np.concatenate((better, tied[tie_order]))
    return sorted(
        ((int(index), float(similarities[index])) for index in eligible),
        key=lambda pair: (-pair[1], documents[pair[0]].number),
    )


def rank_reviewers(
    neighbors: list[tuple[int, float]],
    documents: list[PRDocument],
    *,
    target_author: str,
    k: int,
    similarity_exponent: float,
) -> list[str]:
    votes: defaultdict[str, float] = defaultdict(float)
    author = target_author.casefold()
    for document_index, similarity in neighbors[:k]:
        weight = max(0.0, similarity) ** similarity_exponent
        if weight <= 0.0:
            continue
        for reviewer in set(documents[document_index].reviewers):
            normalized = reviewer.casefold()
            if normalized and normalized != author:
                votes[normalized] += weight
    return sorted(votes, key=lambda login: (-votes[login], login))


def _record_target(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("target", record)


def _primary_targets(graph: Any, window_start: int, window_end: int) -> list[dict[str, Any]]:
    targets = load_targets(graph, window_start, window_end)
    reviewers, approvers = load_ground_truth(graph, window_start, window_end)
    records: list[dict[str, Any]] = []
    for number, target in sorted(
        targets.items(), key=lambda item: (item[1]["merged_ts"], item[0])
    ):
        integration_keys = {
            key for key, kind in target["modules"].items() if kind == "integration"
        }
        if target["author_is_bot"] or not reviewers.get(number) or not integration_keys:
            continue
        records.append(
            {
                "target": {
                    **target,
                    "integration_keys": integration_keys,
                    "true_reviewers": reviewers[number],
                    "true_approvers": approvers.get(number, set()),
                }
            }
        )
    return records


def _prediction_metrics(
    records: list[dict[str, Any]], predictions: dict[int, list[str]]
) -> dict[str, Any]:
    per_record = [
        _per_pr_metrics(
            predictions[int(_record_target(record)["number"])],
            _record_target(record)["true_reviewers"],
        )
        for record in records
    ]
    return {
        "n": len(records),
        **{
            metric: float(np.mean([row[metric] for row in per_record]))
            if per_record
            else None
            for metric in METRIC_NAMES
        },
    }


def tune_parameters(
    validation_records: list[dict[str, Any]],
    neighbors: dict[int, list[tuple[int, float]]],
    documents: list[PRDocument],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not validation_records:
        raise ValueError("RAG validation window contains no eligible primary PRs.")
    grid: list[dict[str, Any]] = []
    for k in K_VALUES:
        for exponent in SIMILARITY_EXPONENTS:
            predictions = {
                int(_record_target(record)["number"]): rank_reviewers(
                    neighbors[int(_record_target(record)["number"])],
                    documents,
                    target_author=str(_record_target(record)["author"]),
                    k=k,
                    similarity_exponent=exponent,
                )
                for record in validation_records
            }
            metrics = _prediction_metrics(validation_records, predictions)
            grid.append(
                {
                    "k": k,
                    "similarity_exponent": exponent,
                    **{metric: metrics[metric] for metric in ("hit@3", "MRR", "recall@5")},
                }
            )
    winner = max(
        grid,
        key=lambda row: (row["hit@3"], row["MRR"], row["recall@5"]),
    )
    return {
        "k": int(winner["k"]),
        "similarity_exponent": float(winner["similarity_exponent"]),
    }, grid


def _bootstrap_g3_minus_rag(
    records: list[dict[str, Any]], *, resamples: int = BOOTSTRAP_RESAMPLES
) -> dict[str, Any]:
    if not records:
        return {}
    differences = {
        metric: [
            _per_pr_metrics(record["rankings"]["G3"], record["target"]["true_reviewers"])[metric]
            - _per_pr_metrics(record["rankings"]["RAG"], record["target"]["true_reviewers"])[metric]
            for record in records
        ]
        for metric in ("hit@3", "MRR")
    }
    rng = random.Random(BOOTSTRAP_SEED)
    samples = [
        [rng.randrange(len(records)) for _ in range(len(records))]
        for _ in range(resamples)
    ]
    result: dict[str, Any] = {"n": len(records), "resamples": resamples, "seed": BOOTSTRAP_SEED}
    for metric, values in differences.items():
        means = sorted(
            sum(values[index] for index in sample) / len(sample) for sample in samples
        )
        result[metric] = {
            "mean_difference": float(np.mean(values)),
            "ci95": [means[int(0.025 * (resamples - 1))], means[int(0.975 * (resamples - 1))]],
        }
    return result


def _artifact_metric(per_pr: dict[str, Any], fold: str, method: str) -> dict[str, float | None]:
    return per_pr["folds"][fold]["test_metrics"]["methods"][method]


def _weighted_artifact_metric(
    per_pr: dict[str, Any], method: str, folds: tuple[str, ...]
) -> dict[str, float | None]:
    counts = {
        fold: int(per_pr["folds"][fold]["test_metrics"]["n"]) for fold in folds
    }
    total = sum(counts.values())
    return {
        metric: sum(
            counts[fold] * float(_artifact_metric(per_pr, fold, method)[metric] or 0.0)
            for fold in folds
        )
        / total
        if total
        else None
        for metric in METRIC_NAMES
    }


def _slice_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    methods = ("B1", "B2", "B3", "G1", "G3", "RAG")
    slices = {
        "cold": [record for record in records if record["history"].integration_pr_count < 5],
        "ownerless_integration": [record for record in records if record["target"]["owner_count"] == 0],
    }
    result: dict[str, Any] = {}
    for label, subset in slices.items():
        result[label] = _metrics(subset, methods)
    return result


def _has_activity(history: Any, login: str, module_keys: set[str]) -> bool:
    evidence = history.evidence_prs
    normalized = login.casefold()
    return any(
        evidence.get((role, normalized, module_key))
        for role in ("review", "author")
        for module_key in module_keys
    )


def _failure_examples(
    graph: Any,
    records: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for record in records:
        target = record["target"]
        rag_top = record["rankings"]["RAG"][:3]
        if not rag_top:
            continue
        touched_modules = set(target["modules"])
        expert_modules = {
            module_key
            for module_key, kind in target["modules"].items()
            if kind != "infra"
        }
        possible_failures = [
            login
            for login in rag_top
            if not _has_activity(record["history"], login, expert_modules)
        ]
        if not possible_failures:
            continue
        as_of_history = fetch_history(
            graph,
            as_of=int(target["merged_ts"]),
            module_keys=touched_modules,
            integration_keys=set(target["integration_keys"]),
        )
        unsupported = [
            login
            for login in possible_failures
            if not _has_activity(as_of_history, login, touched_modules)
        ]
        if unsupported:
            examples.append(
                {
                    "pr_number": int(target["number"]),
                    "as_of_merged_ts_exclusive": int(target["merged_ts"]),
                    "rag_top3": rag_top,
                    "g3_top3": record["rankings"]["G3"][:3],
                    "true_reviewers": sorted(target["true_reviewers"]),
                    "module_cold_rag_candidates": unsupported,
                }
            )
            if len(examples) >= limit:
                break
    return examples


def _print_table(
    per_pr: dict[str, Any],
    fold_metrics: dict[str, dict[str, Any]],
    pooled: dict[str, Any],
) -> None:
    table = Table(title="Per-PR reviewer prediction: graph vs text-only RAG")
    for heading in ("Fold", "Method", "N", "Hit@1", "Hit@3", "Hit@5", "Recall@5", "MRR"):
        table.add_column(
            heading,
            justify="right" if heading in {"N", "Hit@1", "Hit@3", "Hit@5", "Recall@5", "MRR"} else "left",
        )
    for fold in ("A", "B", "C"):
        count = int(per_pr["folds"][fold]["test_metrics"]["n"])
        for method in ("B3", "G1", "G3", "RAG"):
            values = (
                _artifact_metric(per_pr, fold, method)
                if method != "RAG"
                else fold_metrics[fold]
            )
            table.add_row(
                fold,
                method,
                str(count),
                *[f"{float(values[key] or 0.0):.3f}" for key in ("hit@1", "hit@3", "hit@5", "recall@5", "MRR")],
            )
    for method in ("B3", "G1", "G3", "RAG"):
        values = pooled["methods"][method]
        table.add_row(
            "B+C",
            method,
            str(pooled["n"]),
            *[f"{float(values[key] or 0.0):.3f}" for key in ("hit@1", "hit@3", "hit@5", "recall@5", "MRR")],
        )
    Console().print(table)


def run_rag_eval(
    *,
    graph_name: str = DEFAULT_GRAPH,
    output_path: Path = RAG_RESULTS_PATH,
) -> dict[str, Any]:
    round1 = json.loads(ROUND1_RESULTS_PATH.read_text(encoding="utf-8"))
    round2 = json.loads(ROUND2_RESULTS_PATH.read_text(encoding="utf-8"))
    per_pr = json.loads(PER_PR_RESULTS_PATH.read_text(encoding="utf-8"))
    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph(graph_name)
    _, _, windows = _fold_windows(round1)
    owners, dependencies, owner_is_bot = load_static_graph_data(graph)

    basic_records: dict[str, list[dict[str, Any]]] = {}
    test_records: dict[str, list[dict[str, Any]]] = {}
    for fold in ("A", "B", "C"):
        window = windows[fold]
        if fold in ("B", "C"):
            records = prepare_split(
                graph,
                window_start=window["test_start"],
                window_end=window["test_end"],
                primary_only=True,
                owners=owners,
                dependencies=dependencies,
                owner_is_bot=owner_is_bot,
            )
        else:
            records = _primary_targets(graph, window["test_start"], window["test_end"])
        expected = int(per_pr["folds"][fold]["test_metrics"]["n"])
        if len(records) != expected:
            raise ValueError(
                f"Fold {fold} target population mismatch: loaded {len(records)}, artifact has {expected}."
            )
        basic_records[fold] = records
        if fold in ("B", "C"):
            test_records[fold] = records
            for record in records:
                _per_pr_rankings(record, float(round2["folds"][fold]["lambda"]))
                baseline = method_rankings(
                    record["target"],
                    record["history"],
                    owners=owners,
                    owner_is_bot=owner_is_bot,
                    dependencies=dependencies,
                    parameters=FROZEN_PARAMETERS,
                )
                record["rankings"]["B1"] = baseline["B1"]
                record["rankings"]["B2"] = baseline["B2"]

    validation_d = _primary_targets(
        graph,
        windows["C"]["validation_start"],
        windows["C"]["validation_end"],
    )
    validation_records = {
        "A": basic_records["B"],
        "B": basic_records["C"],
        "C": validation_d,
    }
    documents = load_documents()
    print(f"Loaded {len(documents)} merged-PR text documents from {RAW_PRS_PATH.relative_to(ROOT)}.")
    embedding_started = time.perf_counter()
    vectors, embedding_cache_hit = load_or_encode(documents)
    embedding_seconds = time.perf_counter() - embedding_started
    print(
        f"CPU embeddings {'reused' if embedding_cache_hit else 'created'} in {embedding_seconds:.1f}s "
        f"(model={EMBEDDING_MODEL}, vectors={vectors.shape[0]}x{vectors.shape[1]})."
    )

    all_records = [*basic_records["A"], *test_records["B"], *test_records["C"], *validation_d]
    neighbor_cache: dict[int, list[tuple[int, float]]] = {}
    doc_positions = {document.number: index for index, document in enumerate(documents)}
    doc_numbers = np.fromiter((document.number for document in documents), dtype=np.int64)
    doc_timestamps = np.fromiter((document.merged_ts for document in documents), dtype=np.int64)
    for record in all_records:
        target = _record_target(record)
        number = int(target["number"])
        if number not in neighbor_cache:
            neighbor_cache[number] = nearest_neighbors(
                number,
                int(target["merged_ts"]),
                documents,
                vectors,
                index_by_number=doc_positions,
                document_numbers=doc_numbers,
                document_timestamps=doc_timestamps,
            )
    if any(int(_record_target(record)["number"]) not in doc_positions for record in all_records):
        raise ValueError("At least one evaluator target is absent from the cached PR corpus.")

    fold_metrics: dict[str, dict[str, Any]] = {}
    tuning: dict[str, Any] = {}
    predictions: dict[str, dict[int, list[str]]] = {}
    for fold in ("A", "B", "C"):
        parameters, grid = tune_parameters(
            validation_records[fold], neighbor_cache, documents
        )
        fold_predictions = {
            int(_record_target(record)["number"]): rank_reviewers(
                neighbor_cache[int(_record_target(record)["number"])],
                documents,
                target_author=str(_record_target(record)["author"]),
                k=parameters["k"],
                similarity_exponent=parameters["similarity_exponent"],
            )
            for record in basic_records[fold]
        }
        predictions[fold] = fold_predictions
        fold_metrics[fold] = _prediction_metrics(basic_records[fold], fold_predictions)
        tuning[fold] = {
            "validation_window_start_utc": datetime.fromtimestamp(
                windows[fold]["validation_start"], timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "validation_window_end_utc": datetime.fromtimestamp(
                windows[fold]["validation_end"], timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "validation_n": len(validation_records[fold]),
            "selected": parameters,
            "grid": grid,
        }
        if fold in ("B", "C"):
            for record in test_records[fold]:
                number = int(record["target"]["number"])
                record["rankings"]["RAG"] = fold_predictions[number]

    pooled_records = [*test_records["B"], *test_records["C"]]
    pooled_rag = _metrics(pooled_records, ("RAG",))
    pooled: dict[str, Any] = {
        "n": pooled_rag["n"],
        "methods": {"RAG": pooled_rag["methods"]["RAG"]},
    }
    for method in ("B3", "G1", "G3"):
        pooled["methods"][method] = _weighted_artifact_metric(per_pr, method, ("B", "C"))
    for method in ("B1", "B2"):
        pooled["methods"][method] = _metrics(pooled_records, (method,))["methods"][method]

    paired_bootstrap = _bootstrap_g3_minus_rag(pooled_records)
    slices = _slice_metrics(pooled_records)
    failure_examples = _failure_examples(graph, pooled_records)
    conclusion = (
        "RAG beats G3 on pooled B/C hit@3."
        if pooled["methods"]["RAG"]["hit@3"] > pooled["methods"]["G3"]["hit@3"]
        else "RAG does not beat G3 on pooled B/C hit@3."
    )
    output = {
        "graph": graph_name,
        "source": "public PR text and review records in data/raw/prs/pages.jsonl; graph used only for evaluator splits and as-of support audit",
        "embedding_model": EMBEDDING_MODEL,
        "embedding_device": "cpu",
        "embedding_cache_hit": embedding_cache_hit,
        "embedding_seconds": round(embedding_seconds, 3),
        "document_count": len(documents),
        "history_rule": "Retrieved PR merged_ts and each review submittedAt must be strictly earlier than the target; target PR is excluded.",
        "reviewer_rule": "One similarity-weighted vote per human reviewer per retrieved PR; exclude retrieved authors and target author.",
        "round2_per_pr_artifact_used_for_graph_metrics": "results/reviewer_eval_round2_per_pr.json",
        "folds": {
            fold: {
                "n": fold_metrics[fold]["n"],
                "parameters": tuning[fold]["selected"],
                "validation_n": tuning[fold]["validation_n"],
                "metrics": {
                    metric: fold_metrics[fold][metric] for metric in METRIC_NAMES
                },
            }
            for fold in ("A", "B", "C")
        },
        "tuning": tuning,
        "pooled_BC": pooled,
        "paired_bootstrap_G3_minus_RAG": paired_bootstrap,
        "slice_metrics_pooled_BC": slices,
        "failure_mode_examples": failure_examples,
        "failure_mode_examples_found": len(failure_examples),
        "conclusion": conclusion,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_table(per_pr, fold_metrics, pooled)
    Console().print("\nRAG failure-mode examples with no prior activity in any touched module:")
    if failure_examples:
        for example in failure_examples:
            Console().print(
                f"PR {example['pr_number']}: RAG {example['rag_top3']}; "
                f"G3 {example['g3_top3']}; true {example['true_reviewers']}"
            )
    else:
        Console().print("No qualifying held-out PRs found.")
    Console().print(conclusion)
    Console().print(f"Wrote {output_path.relative_to(ROOT)}")
    return output


def main() -> int:
    run_rag_eval()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())