from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.eval.reviewer_eval_round2 import (
    DEFAULT_GRAPH,
    ROUND2_RESULTS_PATH,
    run_round2,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the rolling-origin G3 reviewer evaluation without changing round-1 results."
    )
    parser.add_argument("--graph", default=DEFAULT_GRAPH)
    parser.add_argument("--output", type=Path, default=ROUND2_RESULTS_PATH)
    args = parser.parse_args()
    try:
        run_round2(graph_name=args.graph, output_path=args.output)
    except Exception as error:
        print(f"Reviewer evaluation round 2 failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())