from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.ingest.graph_loader import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_GRAPH,
    load_graph,
    print_summary,
    print_verification,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild a FalkorDB graph from the local Home Assistant caches."
    )
    parser.add_argument(
        "--graph",
        default=DEFAULT_GRAPH,
        help=f"Graph name to delete and rebuild (default: {DEFAULT_GRAPH}).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"UNWIND rows per write query (default: {DEFAULT_BATCH_SIZE}).",
    )
    args = parser.parse_args()
    try:
        summary, verification = load_graph(
            graph_name=args.graph,
            batch_size=args.batch_size,
        )
    except Exception as error:
        print(f"Graph load failed: {error}", file=sys.stderr, flush=True)
        return 1
    print_summary(summary)
    if verification is not None:
        print_verification(verification)
    return 0


if __name__ == "__main__":
    sys.exit(main())