# Project State

## Goal
- Provenance compares declared engineering structure with public contribution behavior using deterministic FalkorDB tools and replayable facts.
- The LLM may select typed tools and narrate evidence; it never writes live Cypher.
- Tracks: Agents That Act on Connected Data; Agent Memory and Coordination; Company Brain.

## Data
- Public Home Assistant `core` and `architecture` sources; graph `provenance_ha` contains the latest 180-day merged-PR cache.
- Nodes: Person 1,497; Team 8; Module 1,629; PR 10,285; Decision 455. Relationships: AUTHORED 10,285; REVIEWED 18,597; TOUCHES 25,312; DECLARED_OWNER 1,477; DEPENDS_ON 5,152; SUPERSEDES 1; REFERENCES 139; DECIDED_AS 384.
- Event times are integer Unix seconds. Reads use parameterized `GRAPH.RO_QUERY`; reviewer history is strict as-of. Owner/dependency relationships are current snapshots.
- Import scan: AST 8,794 files; regex fallback 1,540; unreadable 0; Python import edges 4,467.

## Evaluation
- Round 1 held-out B3 hit@3 is 0.4342; round 2 G3 pooled unseen B/C hit@3 is 0.5441 versus B3 0.4257. G3 improves top-K metrics, not hit@1 universally.
- Per-PR Fold A reproduces round-1 B3 exactly (0.434215); fixed-window B3 is 0.297428. See [results/reviewer_eval_round2.json](results/reviewer_eval_round2.json) and [results/reviewer_eval_round2_per_pr.json](results/reviewer_eval_round2_per_pr.json).

## Tools And Reports
- Eight deterministic, read-only, fact-replaying tools: module_profile, ownership_drift, bus_factor, blast_radius, criticality, suggest_reviewers, maintainer_loss, decision_trace.
- Registry exports JSON schemas for OpenAI function calling and MCP. CLI: `scripts/run_tool.py`; risk report: `scripts/risk_report.py` and [results/risk_report.json](results/risk_report.json).
- Real graph: `integration:sensor` has 797 direct dependency rows; removing `epenet` newly uncovers 21 modules, with 2,297 total dependents affected and 456 for `integration:binary_sensor`.
- Risk score weights: bus factor 0.35, ownership drift 0.30, normalized PageRank 0.20, normalized reverse-dependency count 0.15; full formula is in `DECISIONS.md`.
- A temporary-graph audit confirmed FalkorDB's variable-length path quirk for `UNWIND` aliases in pattern maps; the registered query now filters `target.key IN $module_keys`.

## Verification
- Full suite: `.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-temp`.
- Real CLI forms verified: `--arg`, `--args-file`, `--args -` (stdin), and the warned PowerShell-mangled `--args` fallback.
- AI assistance and the G3 review-event deduplication fix are recorded in `AI_USE.md`; evaluator code and published metrics were unchanged.