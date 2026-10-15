W 'AGENTS.md' @'
# Project: Provenance
An engineering-organization brain whose answers carry machine-checked proof.
Hackathon: FalkorDB Graph Hacks. Tracks entered: Agents That Act on Connected Data, Agent Memory and Coordination, Company Brain.
Dataset: public GitHub data from home-assistant/core and its public architecture repo.

## Core thesis
Every organization has a declared graph (CODEOWNERS, manifests, decision records) and a behavioral graph (who authors, who reviews, what changes together). The gap between them is where risk hides. FalkorDB computes that gap deterministically. The LLM only plans tool calls and narrates results. Every claim in an answer must be backed by a subgraph that a verifier re-executes.

## Non-negotiables
- FalkorDB is the primary datastore and the reasoning engine. If the product still works with FalkorDB removed, we failed.
- Before using any Cypher clause or algorithm, read https://docs.falkordb.com/llms.txt and the relevant page. FalkorDB implements a SUBSET of OpenCypher. Never assume Neo4j-only procedures exist.
- Use the official `falkordb` Python client. Use read-only queries (GRAPH.RO_QUERY) for reads. Parameterize every query. Use UNWIND batches for ingestion. Create indexes on lookup keys.
- Data: only public GitHub data. Store GitHub logins only. Never store emails, tokens, or any private data.
- Ingestion is idempotent (MERGE), cached raw to data/raw as JSONL, and resumable. Respect GitHub rate limits and log remaining quota.
- LLM calls go through one provider-agnostic OpenAI-compatible client with fallback order Groq, then Gemini, then Ollama. Cache by prompt hash. Maximum 4 LLM calls per user question. Every feature must also work in deterministic mode with no LLM.
- The LLM never writes free-form Cypher against the live graph. It selects from typed tools.
- Every tool returns {result, evidence: {query_id, params, node_ids, edge_ids}}. A verifier re-executes evidence queries and flags unsupported claims.
- Per-tenant graphs use separate FalkorDB graph names.
- Never print or log secrets. Read them only from environment variables loaded from .env.

## Environment
- OS is Windows with PowerShell. Use pathlib, not hard-coded separators. Python 3.11+ in a virtual environment at .venv. Node 20+ for the frontend.
- FalkorDB runs through docker-compose.yml.

## Working rules
- Keep each task small and focused so it can be committed separately.
- After each task, print: what changed, how to verify it, and what you are unsure about.
- Append every schema or query decision to DECISIONS.md in plain language.
- Record AI assistance in AI_USE.md.
- Write tests for every tool and for the verifier.
- Never fabricate data or results. If something fails, say so plainly.
- Do not add dependencies without stating why in DECISIONS.md.
- Never run git commit or git push. The author commits manually in small Conventional Commits (feat:, fix:, test:, docs:, chore:, refactor:). Suggest a commit message at the end of each task instead.
- Terminal commands run on Windows PowerShell. Call .venv\Scripts\python.exe directly.

## README must eventually contain
Setup instructions, graph data model, the Cypher queries and algorithms the product depends on, evaluation numbers, tracks entered, and AI-use disclosure.
