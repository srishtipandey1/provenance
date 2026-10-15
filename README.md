# Provenance

Provenance is an engineering-organization brain whose answers carry machine-checked proof. It compares declared structure (owners, dependencies, architecture decisions) with behavioral structure (authorship, reviews, and code changes). FalkorDB computes deterministic graph evidence; an LLM, when added, may plan typed tool calls and narrate verified results, but does not write live Cypher.

## Setup

Requirements: Windows, Python 3.11+, Docker Desktop with Compose, and public Home Assistant data caches/clones under `data/raw`.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
docker compose up -d falkordb
.venv\Scripts\python.exe scripts\load_graph.py
```

The loader rebuilds only `provenance_ha`; `--graph` selects another graph name. To run the reviewer evaluation, use `.venv\Scripts\python.exe scripts\run_eval.py`. Tests use `.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-temp` when the user-profile temp directory is not accessible.

## Graph Data Model

Nodes: `Person(login,is_bot)`, `Team(handle)`, `Module(key,kind,name)`, `PR(number,merged_ts,...)`, and `Decision(id,...)`. Relationships: `AUTHORED`, `REVIEWED`, `TOUCHES`, `DECLARED_OWNER`, `DEPENDS_ON`, `SUPERSEDES`, `REFERENCES`, and `DECIDED_AS`. Timestamps are integer Unix seconds. See [docs/graph_model.md](docs/graph_model.md) for mapping, properties, and ingestion details.

The current graph contains 10,285 merged PRs from the last 180 days, 1,532 integration manifests, and 22 local ADRs. It has 1,497 people, 8 teams, 1,629 modules, 455 decisions, and 5,152 `DEPENDS_ON` edges.

## Cypher Queries and Algorithms

All reads use the official `falkordb` Python client and `GRAPH.RO_QUERY`; all query values are parameters. The loader writes idempotent `UNWIND` batches after matching relationship endpoints separately. Reviewer evaluation uses strict `PR.merged_ts < $as_of` predicates, graph-side `count`/`sum`/`collect` aggregates, and exponentially decayed history with `exp`. G1 downweights broad PRs by the count of touched modules; G2 adds validation-tuned expertise from dependency neighbors for cold integrations. FalkorDB implements a Cypher subset; see its [support matrix](https://docs.falkordb.com/cypher/cypher-support) and [known limitations](https://docs.falkordb.com/cypher/known-limitations) before extending queries.

## Evaluation

Reviewer prediction uses the last 30 days as test and the preceding 30 days for validation, within the 180-day graph window. Validation alone selects recent half-life, graph half-life, author weight, and neighbor factor; the held-out test was scored once at `2026-10-15T12:35:27Z`. For primary reviewers (n=2,683), hit@3 was B1 0.1439, B2 0.3224, B3 0.4342, G1 0.4532, and G2 0.4338. G1's paired hit@3 difference from B3 was +0.0190 (95% CI -0.0045 to +0.0447); G2's was -0.0004 (95% CI -0.0227 to +0.0239). Neither graph method significantly beats B3 at hit@3. B3 leads in hit@5 and recall@5; G1 leads in hit@1 and MRR. The oracle ceiling was 1.0 within the available graph history. Mean query latency was 140.78 ms per scored PR. Full reviewer/approver metrics and slices are in [results/reviewer_eval.json](results/reviewer_eval.json). `DECLARED_OWNER` and `DEPENDS_ON` are current, non-temporal snapshots, so B1 and G2 cannot rule out changes made after some target PRs.

## Tracks Entered

- Agents That Act on Connected Data
- Agent Memory and Coordination
- Company Brain

## AI Use Disclosure

GitHub Copilot assisted with implementation, tests, documentation, query validation, and the reviewer evaluation. No LLM is used to compute reviewer predictions. Full task-level disclosure is in [AI_USE.md](AI_USE.md).