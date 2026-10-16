# Provenance API

Start the API in the foreground from PowerShell:

```powershell
.\scripts\serve.ps1
```

The default address is `http://127.0.0.1:8000`. The API selects the graph named by the existing application default and reads it through FalkorDB's read-only query interface. CORS allows `http://localhost:5173`; override that single origin with `PROVENANCE_CORS_ORIGIN`.

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /api/health` | Graph reachability, node/edge counts, latest merged-PR timestamp, and provider availability probes. Provider checks use model-list requests, not chat completions. |
| `GET /api/tools` | Registered tool schemas. |
| `POST /api/ask` | Verified answer, claims/reasons, timings, provider metadata, removed claims, and per-claim proof/subgraph. |
| `POST /api/tool/{name}` | Run one registered tool after JSON Schema validation. Body: `{"args":{"module_key":"integration:sensor"}}`. |
| `GET /api/risk` | Current `results/risk_report.json`. |
| `GET /api/scoreboard` | Scoreboard and RAG artifacts, labeled `per-target as-of, pooled Folds B+C`. |
| `GET /api/search/modules?q=...` | Up to ten matching modules. |
| `GET /api/search/people?q=...` | Up to ten matching public GitHub logins. |
| `GET /api/graph/neighborhood?module=...&depth=1` | Module, declared owners, top eight behavioral people, up to thirty direct dependents, and their bounded subgraph. |

Example ask request:

```json
{"question":"what is the risk of changing sensor?","mode":"deterministic"}
```

The question is limited to 300 characters. `mode` is `deterministic`, `llm`, or `auto`; `tamper` is an optional positive integer test hook. LLM/auto requests are rate-limited to six per client per minute. The global daily request budget defaults to 100 and can be changed with `PROVENANCE_LLM_DAILY_REQUEST_LIMIT`; usage is persisted as a count and date in gitignored `data/cache/api/llm_daily.json`. When exhausted, LLM/auto transparently return deterministic output with mode `deterministic-fallback (llm budget exhausted)`. Identical untampered deterministic questions are cached in memory for 30 seconds.

Proof subgraphs use internal FalkorDB IDs, which can change when a graph is rebuilt. Results report total and returned node/edge counts, a `truncated` flag, and a relevance note. Selection prioritizes the claim subject, directly cited entities, high-impact dependents, and then weighted evidence. Node and edge output is capped at 150 and 300 respectively; only `login`, `key`, `name`, `number`, `title`, `status`, `kind`, and `ts` properties are returned. Request logs contain a request ID, method, path, status, and elapsed time, never request bodies or credentials. Error responses do not include stack traces.