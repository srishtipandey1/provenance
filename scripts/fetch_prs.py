from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.ingest.github_prs import fetch_merged_prs, print_trial_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch merged Home Assistant PRs into a resumable local cache.")
    parser.add_argument("--days", type=int, default=180, help="Merged PR lookback window (default: 180 days).")
    parser.add_argument("--limit", type=int, help="Stop after this many new PRs (for a trial run).")
    args = parser.parse_args()

    try:
        result = fetch_merged_prs(days=args.days, limit=args.limit)
    except Exception as error:
        print(f"Fetch failed: {error}", file=sys.stderr, flush=True)
        return 1

    if args.limit is not None:
        print_trial_summary(result)
    else:
        store = result["store"]
        print(
            f"Fetch state: {store.state['prs_seen']} unique PRs across "
            f"{store.state['pages_completed']} pages; complete={store.state['complete']}; "
            f"stop_reason={store.state['stop_reason']}.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())