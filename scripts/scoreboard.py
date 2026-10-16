from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "results"
METHOD_ORDER = ("B1", "B2", "B3", "RAG", "G1", "G3")
METRICS = ("hit@1", "hit@3", "hit@5", "MRR")


def _weighted_artifact_metric(
    per_pr: dict[str, Any], method: str, folds: tuple[str, ...] = ("B", "C")
) -> dict[str, float | None]:
    counts = {
        fold: int(per_pr["folds"][fold]["test_metrics"]["n"]) for fold in folds
    }
    total = sum(counts.values())
    return {
        metric: sum(
            counts[fold]
            * float(per_pr["folds"][fold]["test_metrics"]["methods"][method][metric] or 0.0)
            for fold in folds
        )
        / total
        if total
        else None
        for metric in METRICS
    }


def build_scoreboard(rag: dict[str, Any], per_pr: dict[str, Any]) -> dict[str, Any]:
    expected_n = sum(
        int(per_pr["folds"][fold]["test_metrics"]["n"]) for fold in ("B", "C")
    )
    if int(rag["pooled_BC"]["n"]) != expected_n:
        raise ValueError(
            f"RAG pooled population {rag['pooled_BC']['n']} does not match evaluator B+C n={expected_n}."
        )
    pooled_methods: dict[str, Any] = {}
    for method in METHOD_ORDER:
        if method in {"B3", "G1", "G3"}:
            pooled_methods[method] = _weighted_artifact_metric(per_pr, method)
        else:
            pooled_methods[method] = rag["pooled_BC"]["methods"][method]
    slices = {}
    for label in ("cold", "ownerless_integration"):
        source = rag["slice_metrics_pooled_BC"][label]
        slices[label] = {
            "n": int(source["n"]),
            "methods": {
                method: source["methods"][method]
                for method in METHOD_ORDER
            },
        }
    return {
        "population": "pooled per-target as-of Folds B and C",
        "n": expected_n,
        "methods": list(METHOD_ORDER),
        "metrics": list(METRICS),
        "pooled_BC": {"n": expected_n, "methods": pooled_methods},
        "slices": slices,
        "slice_definitions": {
            "cold": "fewer than 5 distinct earlier merged PRs touching the target PR's integrations",
            "ownerless_integration": "target integrations have zero currently declared human owners",
        },
        "source_artifacts": [
            "results/reviewer_eval_round2_per_pr.json",
            "results/rag_baseline.json",
        ],
    }


def _print_table(title: str, result: dict[str, Any]) -> None:
    table = Table(title=title)
    table.add_column("Method")
    table.add_column("N", justify="right")
    for metric in METRICS:
        table.add_column(metric, justify="right")
    for method in METHOD_ORDER:
        values = result["methods"][method]
        table.add_row(
            method,
            str(result["n"]),
            *["n/a" if values[metric] is None else f"{values[metric]:.3f}" for metric in METRICS],
        )
    Console().print(table)


def main() -> int:
    rag = json.loads((RESULTS / "rag_baseline.json").read_text(encoding="utf-8"))
    per_pr = json.loads((RESULTS / "reviewer_eval_round2_per_pr.json").read_text(encoding="utf-8"))
    scoreboard = build_scoreboard(rag, per_pr)
    output_path = RESULTS / "scoreboard.json"
    output_path.write_text(json.dumps(scoreboard, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_table("Pooled B+C reviewer scoreboard", scoreboard["pooled_BC"])
    for label, result in scoreboard["slices"].items():
        _print_table(f"Pooled B+C slice: {label}", result)
    Console().print(f"Wrote {output_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())