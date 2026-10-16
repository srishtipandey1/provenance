from __future__ import annotations

import numpy as np
import pytest

from provenance.eval.rag_baseline import (
    PRDocument,
    build_document,
    nearest_neighbors,
    rank_reviewers,
    tune_parameters,
    _bootstrap_g3_minus_rag,
)
from scripts.scoreboard import build_scoreboard


def test_build_document_uses_public_text_and_excludes_bots_author_and_late_reviews() -> None:
    raw = {
        "number": 20,
        "mergedAt": "2026-10-01T00:00:00Z",
        "title": "Sensor update",
        "body": "x" * 900,
        "author": {"login": "Author", "__typename": "User"},
        "labels": ["bug", "integration"],
        "files": {"nodes": [{"path": "homeassistant/components/sensor/__init__.py"}, {"path": "tests/components/sensor/test_init.py"}]},
        "reviews": {
            "nodes": [
                {"author": {"login": "Reviewer", "__typename": "User"}, "submittedAt": "2026-09-30T23:00:00Z"},
                {"author": {"login": "author", "__typename": "User"}, "submittedAt": "2026-09-30T23:00:00Z"},
                {"author": {"login": "helper[bot]", "__typename": "Bot"}, "submittedAt": "2026-09-30T23:00:00Z"},
                {"author": {"login": "late", "__typename": "User"}, "submittedAt": "2026-10-02T00:00:00Z"},
            ]
        },
    }

    document = build_document(raw)

    assert document is not None
    assert document.reviewers == ("reviewer",)
    assert "x" * 800 in document.text
    assert "x" * 801 not in document.text
    assert "Changed top-level paths: homeassistant, tests" in document.text


def test_nearest_neighbors_strictly_excludes_target_and_same_timestamp() -> None:
    documents = [
        PRDocument(1, 100, "target", "", ()),
        PRDocument(2, 100, "same-time", "", ("same-time-reviewer",)),
        PRDocument(3, 99, "prior", "", ("prior-reviewer",)),
    ]
    vectors = np.asarray([[1.0, 0.0], [0.99, 0.01], [0.9, 0.1]], dtype=np.float32)

    neighbors = nearest_neighbors(1, 100, documents, vectors, max_k=3)

    assert [documents[index].number for index, _ in neighbors] == [3]


def test_nearest_neighbors_breaks_cutoff_ties_by_pr_number() -> None:
    documents = [
        PRDocument(10, 100, "target", "", ()),
        PRDocument(4, 90, "later-number", "", ("four",)),
        PRDocument(3, 90, "earlier-number", "", ("three",)),
    ]
    vectors = np.asarray([[1.0, 0.0], [0.8, 0.6], [0.8, -0.6]], dtype=np.float32)

    neighbors = nearest_neighbors(10, 100, documents, vectors, max_k=1)

    assert [documents[index].number for index, _ in neighbors] == [3]


def test_rank_reviewers_counts_each_retrieved_pr_once_and_excludes_target_author() -> None:
    documents = [
        PRDocument(1, 1, "old-author", "", ("reviewer", "reviewer", "target-author")),
        PRDocument(2, 2, "old-author-2", "", ("reviewer", "other")),
    ]

    ranking = rank_reviewers(
        [(0, 0.9), (1, 0.5)],
        documents,
        target_author="TARGET-AUTHOR",
        k=2,
        similarity_exponent=1.0,
    )

    assert ranking == ["reviewer", "other"]


def test_tuning_uses_validation_records_and_fixed_search_grid() -> None:
    documents = [
        PRDocument(1, 1, "old-author", "", ("reviewer",)),
        PRDocument(2, 2, "author", "", ()),
    ]
    records = [
        {"target": {"number": 2, "author": "author", "true_reviewers": {"reviewer"}}}
    ]

    selected, grid = tune_parameters(records, {2: [(0, 0.8)]}, documents)

    assert len(grid) == 8
    assert selected == {"k": 5, "similarity_exponent": 1.0}
    assert all(row["hit@3"] == 1.0 for row in grid)


def test_scoreboard_reads_graph_metrics_from_per_pr_artifact() -> None:
    metrics = ("hit@1", "hit@3", "hit@5", "MRR")
    per_pr = {
        "folds": {
            fold: {
                "test_metrics": {
                    "n": count,
                    "methods": {
                        method: {metric: value for metric in metrics}
                        for method, value in {
                            "B3": 0.1 if fold == "B" else 0.3,
                            "G1": 0.2 if fold == "B" else 0.4,
                            "G3": 0.3 if fold == "B" else 0.5,
                        }.items()
                    },
                }
            }
            for fold, count in (("B", 1), ("C", 3))
        }
    }
    methods = {
        method: {metric: 0.25 for metric in metrics}
        for method in ("B1", "B2", "RAG")
    }
    slices = {
        label: {
            "n": 1,
            "methods": {
                method: {metric: 0.5 for metric in metrics}
                for method in ("B1", "B2", "B3", "RAG", "G1", "G3")
            },
        }
        for label in ("cold", "ownerless_integration")
    }

    result = build_scoreboard({"pooled_BC": {"n": 4, "methods": methods}, "slice_metrics_pooled_BC": slices}, per_pr)

    assert result["pooled_BC"]["methods"]["B3"]["hit@3"] == pytest.approx(0.25)
    assert result["pooled_BC"]["methods"]["G3"]["hit@3"] == pytest.approx(0.45)
    assert result["slices"]["cold"]["methods"]["RAG"]["MRR"] == 0.5


def test_paired_bootstrap_reports_g3_minus_rag_with_requested_metrics() -> None:
    records = [
        {
            "target": {"true_reviewers": {"reviewer"}},
            "rankings": {"G3": ["reviewer"], "RAG": ["other"]},
        }
    ]

    result = _bootstrap_g3_minus_rag(records, resamples=1000)

    assert result["resamples"] == 1000
    assert result["hit@3"] == {"mean_difference": 1.0, "ci95": [1.0, 1.0]}
    assert result["MRR"] == {"mean_difference": 1.0, "ci95": [1.0, 1.0]}