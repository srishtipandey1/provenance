from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable
from uuid import uuid4

from provenance.tools.analysis import (
    bus_factor,
    criticality,
    maintainer_loss,
    ownership_drift,
    suggest_reviewers,
)
from provenance.tools.base import ToolResult
from provenance.tools.queries import QUERY_REGISTRY


def ensure_coordination_schema(graph: Any) -> None:
    indexes = graph.ro_query(
        QUERY_REGISTRY["coordination.schema.indexes@1"], {}
    ).result_set
    existing = {
        (str(row[0]), str(properties[0]))
        for row in indexes
        for properties in [row[1]]
        if properties
    }
    for label, property_name, query_id in (
        ("Task", "id", "coordination.tenant.index.task@1"),
        ("Episode", "id", "coordination.tenant.index.episode@1"),
    ):
        if (label, property_name) not in existing:
            graph.query(QUERY_REGISTRY[query_id], {})


@dataclass(frozen=True)
class CoordinationRun:
    task_id: str
    episode_id: str
    agent_name: str
    question: str
    answer_summary: str
    tool_names: list[str]
    prior_episode_id: str | None = None


def _persist(
    graph: Any,
    *,
    agent_name: str,
    kind: str,
    subject: str,
    question: str,
    answer_summary: str,
    tool_results: Iterable[ToolResult],
    depends_on_task_id: str | None = None,
) -> CoordinationRun:
    task_id = uuid4().hex
    episode_id = uuid4().hex
    now = int(time.time())
    results = list(tool_results)
    if depends_on_task_id is not None and not graph.ro_query(
        QUERY_REGISTRY["coordination.task.exists@1"],
        {"task_id": depends_on_task_id},
    ).result_set:
        raise ValueError(f"Dependency task {depends_on_task_id!r} was not found.")
    node_ids = sorted(
        {int(node_id) for result in results for node_id in result.evidence["node_ids"]}
    )
    edge_ids = sorted(
        {int(edge_id) for result in results for edge_id in result.evidence["edge_ids"]}
    )
    facts = [
        {
            "tool": result.tool,
            "fact_id": fact.fact_id,
            "query_id": fact.query_id,
            "params": fact.params,
            "extractor": fact.extractor,
        }
        for result in results
        for fact in result.facts
    ]
    task_rows = graph.query(
        QUERY_REGISTRY["coordination.task.write@1"],
        {
            "task_id": task_id,
            "kind": kind,
            "status": "running",
            "created_ts": now,
            "updated_ts": now,
            "agent_name": agent_name,
        },
    ).result_set
    if not task_rows:
        raise RuntimeError(f"Failed to persist {agent_name} task {task_id}.")
    if depends_on_task_id is not None:
        dependency_rows = graph.query(
            QUERY_REGISTRY["coordination.task.depends_on@1"],
            {"task_id": task_id, "depends_on_task_id": depends_on_task_id},
        ).result_set
        if not dependency_rows:
            raise ValueError(f"Dependency task {depends_on_task_id!r} was not found.")

    episode_rows = graph.query(
        QUERY_REGISTRY["coordination.episode.write@1"],
        {
            "task_id": task_id,
            "episode_id": episode_id,
            "agent_name": agent_name,
            "subject": subject,
            "question": question,
            "answer_summary": answer_summary,
            "ts": time.time_ns(),
            "node_ids": node_ids,
            "edge_ids": edge_ids,
            "facts_json": json.dumps(facts, sort_keys=True, separators=(",", ":")),
        },
    ).result_set
    if not episode_rows:
        raise RuntimeError(f"Failed to persist {agent_name} episode {episode_id}.")
    if node_ids:
        linked_nodes = graph.query(
            QUERY_REGISTRY["coordination.episode.used_fact@1"],
            {"episode_id": episode_id, "node_ids": node_ids},
        ).result_set
        if len(linked_nodes) != len(node_ids):
            raise RuntimeError(
                f"Episode {episode_id} linked {len(linked_nodes)} of "
                f"{len(node_ids)} evidence nodes."
            )
    completed = graph.query(
        QUERY_REGISTRY["coordination.task.complete@1"],
        {"task_id": task_id, "updated_ts": int(time.time())},
    ).result_set
    if not completed:
        raise RuntimeError(f"Failed to complete {agent_name} task {task_id}.")
    return CoordinationRun(
        task_id=task_id,
        episode_id=episode_id,
        agent_name=agent_name,
        question=question,
        answer_summary=answer_summary,
        tool_names=[result.tool for result in results],
    )


def _reviewer_summary(result: ToolResult, pr_number: int) -> str:
    candidates = result.result.get("reviewers", [])
    reviewers = [
        {
            key: candidate[key]
            for key in ("login", "g3_score", "g1_score", "b3_score", "score")
            if key in candidate
        }
        for candidate in candidates[:5]
    ]
    summary = {
        "pr_number": pr_number,
        "modules": result.result.get("modules", []),
        "top_reviewers": reviewers,
    }
    if "error" in result.result:
        summary["error"] = result.result["error"]
    return json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str)


class ReviewerAgent:
    name = "ReviewerAgent"

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    def run(self, pr_number: int) -> CoordinationRun:
        ensure_coordination_schema(self.graph)
        result = suggest_reviewers(self.graph, pr_number=pr_number)
        return _persist(
            self.graph,
            agent_name=self.name,
            kind="reviewer_recommendation",
            subject=f"PR:{pr_number}",
            question=f"Suggest reviewers for PR {pr_number}.",
            answer_summary=_reviewer_summary(result, pr_number),
            tool_results=[result],
        )


class RiskAgent:
    name = "RiskAgent"

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    def run(
        self, module_key: str, *, depends_on_task_id: str | None = None
    ) -> CoordinationRun:
        ensure_coordination_schema(self.graph)
        if depends_on_task_id is not None and not self.graph.ro_query(
            QUERY_REGISTRY["coordination.task.exists@1"],
            {"task_id": depends_on_task_id},
        ).result_set:
            raise ValueError(f"Dependency task {depends_on_task_id!r} was not found.")
        results = [
            ownership_drift(self.graph, module_key=module_key),
            bus_factor(self.graph, module_key=module_key),
            criticality(self.graph),
        ]
        bus_rows = results[1].result.get("modules", [])
        criticality_rows = results[2].result.get("modules", [])
        risk_summary = {
            "module_key": module_key,
            "ownership_drift": results[0].result.get("drift_score"),
            "bus_factor": next(
                (
                    {
                        "module_key": row[0],
                        "pr_count": row[1],
                        "top_person": row[3],
                        "top1_share": row[5],
                        "bus_factor_risk": row[6],
                    }
                    for row in bus_rows
                    if row[0] == module_key
                ),
                None,
            ),
            "criticality_rank": next(
                (
                    index + 1
                    for index, row in enumerate(criticality_rows)
                    if row.get("module_key") == module_key
                ),
                None,
            ),
            "criticality_top": [
                row.get("module_key") for row in criticality_rows[:5]
            ],
        }
        summary = json.dumps(
            risk_summary, sort_keys=True, separators=(",", ":"), default=str
        )
        return _persist(
            self.graph,
            agent_name=self.name,
            kind="module_risk_assessment",
            subject=module_key,
            question=f"Assess ownership drift, bus factor, and criticality for {module_key}.",
            answer_summary=summary,
            tool_results=results,
            depends_on_task_id=depends_on_task_id,
        )


class HistorianAgent:
    name = "HistorianAgent"

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    def run(self, person_login: str) -> CoordinationRun:
        ensure_coordination_schema(self.graph)
        person_login = person_login.casefold()
        prior = self.graph.ro_query(
            QUERY_REGISTRY["coordination.episode.latest_subject@1"],
            {"agent_name": self.name, "subject": person_login},
        ).result_set
        prior_episode_id = str(prior[0][0]) if prior else None
        result = maintainer_loss(self.graph, person_login=person_login)
        history_summary = {
            "person_login": person_login,
            "module_count": result.result.get("module_count", 0),
            "distinct_dependents_affected": result.result.get(
                "distinct_dependents_affected", 0
            ),
            "modules": [
                {
                    key: module[key]
                    for key in ("module_key", "became_uncovered", "dependent_count")
                    if key in module
                }
                for module in result.result.get("modules", [])[:10]
            ],
        }
        if "error" in result.result:
            history_summary["error"] = result.result["error"]
        summary = json.dumps(
            history_summary, sort_keys=True, separators=(",", ":"), default=str
        )
        if prior:
            prior_summary = str(prior[0][1])[:500]
            summary = (
                f"Prior episode {prior_episode_id} reported: {prior_summary}. "
                f"Current run: {summary}"
            )
        run = _persist(
            self.graph,
            agent_name=self.name,
            kind="maintainer_history",
            subject=person_login,
            question=f"Summarize maintainer-loss exposure for {person_login}.",
            answer_summary=summary,
            tool_results=[result],
        )
        return CoordinationRun(**{**asdict(run), "prior_episode_id": prior_episode_id})


def task_graph(graph: Any, task_ids: set[str] | None = None) -> list[dict[str, Any]]:
    rows = graph.ro_query(QUERY_REGISTRY["coordination.graph.tasks@1"], {}).result_set
    output = []
    for row in rows:
        if task_ids is not None and str(row[0]) not in task_ids:
            continue
        claimed_by = str(row[3]) if row[3] is not None else None
        episode_id = str(row[4]) if row[4] is not None else None
        dependency_id = str(row[6]) if row[6] is not None else None
        edges = []
        if claimed_by:
            edges.append({"type": "CLAIMED_BY", "target": claimed_by})
        if episode_id:
            edges.append({"type": "PRODUCED", "target": episode_id})
            edges.extend(
                {
                    "type": "USED_FACT",
                    "target_node_id": int(node_id),
                    "evidence_edge_ids": [int(edge_id) for edge_id in row[8] or []],
                }
                for node_id in row[7] or []
            )
        if dependency_id:
            edges.append({"type": "DEPENDS_ON", "target": dependency_id})
        output.append({
            "task_id": str(row[0]),
            "kind": str(row[1]),
            "status": str(row[2]),
            "claimed_by": claimed_by,
            "episode_id": episode_id,
            "summary": str(row[5])[:500] if row[5] is not None else None,
            "depends_on": dependency_id,
            "edges": edges,
        })
    return output


def run_orchestration(
    graph: Any, *, pr_number: int, module_key: str, person_login: str
) -> list[CoordinationRun]:
    reviewer = ReviewerAgent(graph).run(pr_number)
    risk = RiskAgent(graph).run(module_key, depends_on_task_id=reviewer.task_id)
    historian = HistorianAgent(graph).run(person_login)
    # The second graph lookup demonstrates memory retrieved from persisted Episodes.
    historian_repeat = HistorianAgent(graph).run(person_login)
    return [reviewer, risk, historian, historian_repeat]
