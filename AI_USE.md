# AI Assistance Disclosure
Tools used and what each was used for (updated as the project progresses).

- GitHub Copilot: bootstrapped the repository skeleton, configuration, Docker Compose setup, and FalkorDB smoke test; checked the official FalkorDB documentation for deployment and client usage.
- GitHub Copilot: wrote `scripts/gate0.py` to analyze public Home Assistant repository data and run read-only GitHub GraphQL feasibility queries; no data was loaded into FalkorDB.
- GitHub Copilot: implemented a resumable public GitHub PR fetcher, runner, and fixture-based offline tests; used the supplied local PR template to ground the later survey's checkbox matching rule.
- GitHub Copilot: updated the PR cache schema to retain raw Markdown bodies and actor typenames, extended fixture tests, cleared the obsolete trial cache, and reran the limited Stage A trial.
- GitHub Copilot: monitored the full fetch, diagnosed a transient HTTP 499 termination, and added bounded retry handling with an offline regression test before resuming from the saved cursor.
- GitHub Copilot: reconciled the completed cache against GitHub search and implemented the local read-only signal survey with tests for breaking-change parsing and human-reviewer filtering.