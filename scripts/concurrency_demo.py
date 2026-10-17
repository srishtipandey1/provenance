from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from falkordb import FalkorDB

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.agents.coordination import _persist, ensure_coordination_schema
from provenance.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write independent Task/Episode pairs concurrently to FalkorDB."
    )
    parser.add_argument("--graph", default="provenance_ha")
    parser.add_argument("--calls", type=int, default=20)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()
    if args.calls < 1 or args.workers < 1:
        parser.error("--calls and --workers must be positive.")

    graph = FalkorDB(
        host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT
    ).select_graph(args.graph)
    ensure_coordination_schema(graph)

    def write_one(index: int) -> str:
        run = _persist(
            graph,
            agent_name="ConcurrencyDemoAgent",
            kind="concurrent_write_probe",
            subject=f"probe:{index}",
            question=f"Persist concurrent probe {index}.",
            answer_summary=f"Probe {index} persisted.",
            tool_results=[],
        )
        return run.task_id

    started = time.perf_counter()
    successes = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(write_one, index) for index in range(args.calls)]
        for future in as_completed(futures):
            try:
                future.result()
                successes += 1
            except Exception as error:
                failures.append(f"{type(error).__name__}: {error}")
    elapsed = time.perf_counter() - started
    print(f"graph: {args.graph}")
    print(f"submitted: {args.calls} | workers: {args.workers}")
    print(f"successes: {successes} | failures: {len(failures)}")
    print(f"elapsed_seconds: {elapsed:.3f}")
    for failure in failures:
        print(f"failure: {failure}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
