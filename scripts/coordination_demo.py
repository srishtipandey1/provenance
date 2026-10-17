from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from falkordb import FalkorDB

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.agents.coordination import run_orchestration, task_graph
from provenance.config import settings
from provenance.tools.queries import QUERY_REGISTRY


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic workflow agents and show their shared graph state."
    )
    parser.add_argument("--graph", default="provenance_ha")
    parser.add_argument("--pr-number", type=int)
    parser.add_argument("--module")
    parser.add_argument("--person")
    args = parser.parse_args()

    database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    graph = database.select_graph(args.graph)
    if args.pr_number is None or args.module is None or args.person is None:
        sample = graph.ro_query(
            QUERY_REGISTRY["coordination.demo.sample@1"], {}
        ).result_set
        if not sample:
            parser.error(
                "No authored integration PR is available for auto-selection; "
                "provide --pr-number, --module, and --person."
            )
        sample_pr, sample_module, sample_person = sample[0]
        args.pr_number = args.pr_number or int(sample_pr)
        args.module = args.module or str(sample_module)
        args.person = args.person or str(sample_person)

    runs = run_orchestration(
        graph,
        pr_number=args.pr_number,
        module_key=args.module,
        person_login=args.person,
    )
    task_ids = {run.task_id for run in runs}
    print(f"graph: {args.graph}")
    print(f"PR: {args.pr_number} | module: {args.module} | person: {args.person}")
    print("Agents: ReviewerAgent -> RiskAgent; HistorianAgent reads its prior Episode.")
    print("Task graph (state is shared through FalkorDB, not process memory):")
    print(json.dumps(task_graph(graph, task_ids), indent=2, sort_keys=True))
    print(
        "Historian second-run prior Episode:",
        runs[-1].prior_episode_id or "none",
    )


if __name__ == "__main__":
    main()
