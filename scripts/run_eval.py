from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.eval.reviewer_eval import DEFAULT_GRAPH, DEFAULT_RESULTS_PATH, run_evaluation


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tune reviewer predictors on validation and evaluate once on the held-out test window."
    )
    parser.add_argument("--graph", default=DEFAULT_GRAPH)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--as-of", type=int, help="Unix timestamp defining the exclusive end of the evaluation windows.")
    args = parser.parse_args()
    try:
        run_evaluation(graph_name=args.graph, window_end=args.as_of, output_path=args.output)
    except Exception as error:
        print(f"Reviewer evaluation failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())