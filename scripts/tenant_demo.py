from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from falkordb import FalkorDB

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.agents.coordination import _persist
from provenance.agents.router import run_question
from provenance.config import settings
from provenance.ingest.graph_loader import delete_graph_if_exists
from provenance.tools.queries import QUERY_REGISTRY


def copy_module_slice(source: object, target: object, module_keys: list[str]) -> int:
    dependencies = source.ro_query(
        QUERY_REGISTRY["coordination.tenant.dependencies@1"],
        {"module_keys": module_keys},
    ).result_set
    module_rows = source.ro_query(
        QUERY_REGISTRY["coordination.tenant.modules@1"],
        {"module_keys": module_keys},
    ).result_set
    modules = {
        str(row[0]): {"key": str(row[0]), "kind": str(row[1]), "name": str(row[2])}
        for row in module_rows
    }
    edges: list[dict[str, str]] = []
    for row in dependencies:
        source_key, source_kind, source_name, target_key, target_kind, target_name, edge_kind = row
        for key, kind, name in (
            (str(source_key), str(source_kind), str(source_name)),
            (str(target_key), str(target_kind), str(target_name)),
        ):
            modules[key] = {"key": key, "kind": kind, "name": name}
        edges.append(
            {
                "source_key": str(source_key),
                "target_key": str(target_key),
                "kind": str(edge_kind),
            }
        )
    if not modules:
        raise RuntimeError(
            f"Source graph has no modules matching {', '.join(module_keys)}."
        )
    activity_rows = source.ro_query(
        QUERY_REGISTRY["coordination.tenant.activity@1"],
        {"module_keys": module_keys},
    ).result_set
    people = {
        str(row[0]): {"login": str(row[0]), "is_bot": bool(row[1])}
        for row in activity_rows
    }
    person_owners = source.ro_query(
        QUERY_REGISTRY["coordination.tenant.person_owners@1"],
        {"module_keys": module_keys},
    ).result_set
    team_owners = source.ro_query(
        QUERY_REGISTRY["coordination.tenant.team_owners@1"],
        {"module_keys": module_keys},
    ).result_set
    for login, is_bot, _ in person_owners:
        people[str(login)] = {"login": str(login), "is_bot": bool(is_bot)}
    teams = {str(row[0]) for row in team_owners}
    prs = {
        int(row[2]): {
            "number": int(row[2]),
            "title": str(row[3] or ""),
            "merged_ts": int(row[4]),
            "additions": int(row[5] or 0),
            "deletions": int(row[6] or 0),
            "body": str(row[7] or ""),
        }
        for row in activity_rows
    }
    touches = {
        (int(row[2]), str(row[8]))
        for row in activity_rows
    }
    authored = [
        {
            "login": str(row[0]),
            "pr_number": int(row[2]),
            "ts": int(row[10] or 0),
            "role": str(row[11] or ""),
            "state": str(row[12] or ""),
        }
        for row in activity_rows
        if row[9] == "AUTHORED"
    ]
    reviewed = [
        {
            "login": str(row[0]),
            "pr_number": int(row[2]),
            "ts": int(row[10] or 0),
            "role": str(row[11] or ""),
            "state": str(row[12] or ""),
        }
        for row in activity_rows
        if row[9] == "REVIEWED"
    ]
    for query_id in (
        "coordination.tenant.index.module@1",
        "coordination.tenant.index.person@1",
        "coordination.tenant.index.team@1",
        "coordination.tenant.index.pr@1",
        "coordination.tenant.index.task@1",
        "coordination.tenant.index.episode@1",
    ):
        target.query(QUERY_REGISTRY[query_id], {})
    target.query(
        QUERY_REGISTRY["coordination.tenant.copy.modules@1"],
        {"rows": list(modules.values())},
    )
    if edges:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.dependencies@1"],
            {"rows": edges},
        )
    if people:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.people@1"],
            {"rows": list(people.values())},
        )
    if teams:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.teams@1"],
            {"rows": [{"handle": handle} for handle in sorted(teams)]},
        )
    if prs:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.prs@1"],
            {"rows": list(prs.values())},
        )
    touch_rows = [
        {"pr_number": pr_number, "module_key": module_key}
        for pr_number, module_key in sorted(touches)
    ]
    if touch_rows:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.touches@1"],
            {"rows": touch_rows},
        )
    if authored:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.authored@1"],
            {"rows": authored},
        )
    if reviewed:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.reviewed@1"],
            {"rows": reviewed},
        )
    if person_owners:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.person_owners@1"],
            {
                "rows": [
                    {"login": str(login), "module_key": str(module_key)}
                    for login, _, module_key in person_owners
                ]
            },
        )
    if team_owners:
        target.query(
            QUERY_REGISTRY["coordination.tenant.copy.team_owners@1"],
            {
                "rows": [
                    {"handle": str(handle), "module_key": str(module_key)}
                    for handle, module_key in team_owners
                ]
            },
        )
    return len(modules)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy two module slices into isolated FalkorDB graphs and compare."
    )
    parser.add_argument("--source-graph", default="provenance_ha")
    parser.add_argument("--module", default="integration:zha")
    parser.add_argument("--second-module", default="integration:sensor")
    args = parser.parse_args()

    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    source = database.select_graph(args.source_graph)
    tenant_names = ["provenance_tenant_a", "provenance_tenant_b"]
    results: dict[str, dict[str, object]] = {}
    try:
        for tenant_name in tenant_names:
            delete_graph_if_exists(database, tenant_name)
            tenant = database.select_graph(tenant_name)
            module_count = copy_module_slice(
                source, tenant, [args.module, args.second_module]
            )
            question = f"What is the risk of changing {args.module}?"
            routed = run_question(tenant, question)
            if routed.answer is None:
                raise RuntimeError(
                    f"Router failed for {tenant_name}: {routed.unsupported_message}"
                )
            result = _persist(
                tenant,
                agent_name="TenantDemoAgent",
                kind="tenant_isolation_check",
                subject=args.module,
                question=question,
                answer_summary=routed.answer.final_answer,
                tool_results=routed.tool_results,
            )
            task_count = int(
                tenant.ro_query(
                    QUERY_REGISTRY["coordination.graph.task_count@1"], {}
                ).result_set[0][0]
            )
            results[tenant_name] = {
                "module_nodes_copied": module_count,
                "router_answer": routed.answer.final_answer,
                "task_id": result.task_id,
                "episode_id": result.episode_id,
                "task_count": task_count,
            }
        task_ids = {str(result["task_id"]) for result in results.values()}
        if len(task_ids) != 2:
            raise RuntimeError("Tenant Task IDs were not isolated.")
        if any(result["task_count"] != 1 for result in results.values()):
            raise RuntimeError("Tenant Task/Episode state leaked between graphs.")
        print(
            json.dumps(
                {
                    "source_graph": args.source_graph,
                    "question": f"What is the risk of changing {args.module}?",
                    "isolated_results": results,
                    "proof": "each graph has one independent Task and Episode",
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        for tenant_name in tenant_names:
            delete_graph_if_exists(database, tenant_name)
        print("Temporary tenant graphs deleted.")


if __name__ == "__main__":
    main()
