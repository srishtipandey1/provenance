# Project State

## Goal
- Provenance is an engineering-organization brain whose graph answers carry machine-checked evidence.
- Thesis: deterministic FalkorDB analysis compares declared structure with behavior; LLMs may plan typed tools and narrate, never write live Cypher.
- Tracks: Agents That Act on Connected Data; Agent Memory and Coordination; Company Brain.

## Data And Graph
- Public data only: Home Assistant `core` and `architecture`; merged PRs in the latest 180-day cache window.
- Graph: `provenance_ha`; nodes Person 1,497, Team 8, Module 1,629, PR 10,285, Decision 455.
- Edges: AUTHORED 10,285; REVIEWED 18,597; TOUCHES 25,312; DECLARED_OWNER 1,477; DEPENDS_ON 5,152; SUPERSEDES 1; REFERENCES 139; DECIDED_AS 384.
- Times are integer Unix seconds. Reads use parameterized `GRAPH.RO_QUERY`; history filters strictly `PR.merged_ts < target merged_ts`.
- Import scan: AST 8,794 files; regex fallback 1,540; unreadable 0; Python import edges 4,467. Top failures: invalid syntax 1,095, unparenthesized multiple exceptions 315, expected `(` 127.

## Scripts
- Fetch public PR cache: `scripts/fetch_prs.py`; survey: `scripts/survey_signals.py`; graph rebuild: `.venv\Scripts\python.exe scripts\load_graph.py`.
- Reviewer evaluation: `.venv\Scripts\python.exe scripts\run_eval.py`; tests: `.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-temp`.
- Evaluator and results: [provenance/eval/reviewer_eval.py](provenance/eval/reviewer_eval.py), [results/reviewer_eval.json](results/reviewer_eval.json).

## Reviewer Evaluation
- As-of run: 2026-10-15T12:35:27Z. Validation `[t-60d,t-30d)`, test `[t-30d,t)`; validation primary n=1,740; held-out primary reviewers n=2,683, oracle 1.0 within available graph history.
- Frozen: B3 half-life 14d; G1 half-life 90d, author weight 0.5; G2 half-life 90d, author weight 0, neighbor factor 0.3.
- Primary reviewer test metrics:

| Method | Hit@1 | Hit@3 | Hit@5 | Recall@5 | MRR |
| --- | ---: | ---: | ---: | ---: | ---: |
| B1 owners | 0.1163 | 0.1439 | 0.1510 | 0.1165 | 0.1306 |
| B2 global | 0.1211 | 0.3224 | 0.5218 | 0.4788 | 0.3117 |
| B3 recent | 0.1379 | 0.4342 | 0.6653 | 0.6176 | 0.3494 |
| G1 expertise | 0.2408 | 0.4532 | 0.5837 | 0.5381 | 0.3881 |
| G2 dependencies | 0.2024 | 0.4338 | 0.5703 | 0.5273 | 0.3604 |

- G1-B3 hit@3 difference +0.0190, 95% CI [-0.0045, 0.0447]; G2-B3 -0.0004, CI [-0.0227, 0.0239]. Neither significantly beats B3; B3 leads hit@5/recall@5, G1 leads hit@1/MRR. Mean query latency 140.78 ms/PR (median 129.15, p95 213.79).
- Regime check: per-PR Fold A B3=0.434215, exactly round 1; fixed-window B3=0.297428. The +0.136787 gap is fully explained by excluding earlier PRs from inside the test window. See [results/reviewer_eval_round2_per_pr.json](results/reviewer_eval_round2_per_pr.json).
- Caveat: DECLARED_OWNER and DEPENDS_ON are current snapshots, not time-versioned; possible post-target relationship changes remain.

## Reviewer Round 2
- Fixed window history: A/B/C have 150/120/90 available days; lambda validation n=1,740/1,072/1,324 and selected lambda=1/1/2. A is POST-HOC. Each lambda uses its origin's pre-test window, though that interval can be another later-origin fold's test.
| Method | Hit@1 | Hit@3 | Hit@5 | Recall@5 | MRR |
| --- | ---: | ---: | ---: | ---: | ---: |
| B2 | 0.3144 | 0.4225 | 0.5950 | 0.5160 | 0.4307 |
| B3 | 0.3137 | 0.4257 | 0.5334 | 0.4551 | 0.4250 |
| G1 | 0.3019 | 0.4772 | 0.5605 | 0.4858 | 0.4148 |
| G3 | 0.3076 | 0.5441 | 0.6472 | 0.5674 | 0.4617 |
| G3b | 0.3243 | 0.5135 | 0.6159 | 0.5361 | 0.4494 |
- Fold A diagnostics: G1 returned <5 candidates on 421/2,683 PRs (15.7%); conditional G1 hit@5=0.5097. Global oracle=1.0; module-local oracle=0.7059.
- Conclusion: On unseen pooled B/C, G3 improves hit@3, hit@5, recall@5 and MRR over B3 and G1, but not hit@1; G3b has the highest hit@1. This supports a top-K hybrid gain, not universal superiority. Fold A is post-hoc; owners/dependencies are current snapshots and history is limited to 180 days. Full results: [results/reviewer_eval_round2.json](results/reviewer_eval_round2.json).

## Status And Next
- Done: graph loader recovery and rebuild, one held-out evaluation run, strict-as-of leak tests, metrics artifact, docs and AI-use records; full suite passes (15 tests).
- Next: typed tools with evidence objects, verifier, agent layer, multi-agent coordination, frontend, deployment, and README/video polish.
- Keep tasks small; never commit or push. Author commits manually. Suggested commit: `feat: evaluate graph reviewer predictions`.