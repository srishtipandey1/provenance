# Provenance Graph Model

This graph is rebuilt from the public Home Assistant PR JSONL cache, the local `home-assistant/core` clone, and the local `home-assistant/architecture` clone. It contains no File nodes and performs no live GitHub fetches.

## Nodes

| Label | Key | Properties |
| --- | --- | --- |
| `Person` | `login` | `is_bot` |
| `Team` | `handle` | GitHub organization/team handle without its leading `@` |
| `Module` | `key` | `kind` (`integration`, `core`, or `infra`), `name` |
| `PR` | `number` | `title`, integer `merged_ts`, `additions`, `deletions`, `breaking_box`, `breaking_text`, `labels` |
| `Decision` | `id` | `title`, `status`, `source` |

All event timestamps are integer Unix epoch seconds. `PR.merged_ts`, `AUTHORED.ts`, and `REVIEWED.ts` are never stored as ISO strings. `Person.login` and team handles are case-folded for stable GitHub identity matching; person logins have no leading `@`. Bot authors are retained with `is_bot=true`; bot reviewers and reviews by the PR author are omitted.

## Relationships

| Pattern | Properties / identity |
| --- | --- |
| `(Person)-[:AUTHORED]->(PR)` | `ts` is the PR creation time |
| `(Person)-[:REVIEWED]->(PR)` | `state`, `ts`; one edge per distinct reviewer state/timestamp event |
| `(PR)-[:TOUCHES]->(Module)` | `files`, `additions`, `deletions`, aggregated per PR/module from returned file nodes |
| `(Person|Team)-[:DECLARED_OWNER]->(Module)` | From integration manifest `codeowners` |
| `(Module)-[:DEPENDS_ON]->(Module)` | `kind` is `dependency`, `after_dependency`, or `python_import` |
| `(Decision)-[:SUPERSEDES]->(Decision)` | Direction is newer decision to older decision |
| `(PR)-[:REFERENCES]->(Decision)` | Resolved ADR links and reference-only architecture discussion/issue links |
| `(PR)-[:DECIDED_AS]->(Decision)` | One `pr-breaking` Decision for each PR with either breaking signal |

ADR Decisions use the padded ADR id as `id`, the local ADR title/status, and the path relative to `data/raw/architecture` as `source`. Architecture discussion/issue links become explicit reference-only Decisions with generated titles and `source="architecture-reference"`; no remote discussion text or status is claimed. PR breaking Decisions use `id="pr-breaking:<number>"`, `status="reported"`, and `source="pr-breaking"`.

## Module Mapping

- `homeassistant/components/<name>/...` maps to `Module {kind: "integration", key: "integration:<name>", name: "<name>"}`.
- `tests/components/<name>/...` maps to the same integration key.
- Other `homeassistant/<second-level>/...` paths map to a `core` module keyed by the first two path levels, such as `homeassistant/helpers`. A root-level `homeassistant/<file>` maps to `homeassistant/<file>`.
- Every other path maps to `infra`. Its module key is `infra:<first path component>`; a root-level file uses its filename as that component. For example, `.github/workflows/ci.yaml` maps to `infra:.github`, while `requirements.txt` maps to `infra:requirements.txt`.
- Multiple changed files in one PR that map to the same module are aggregated into one `TOUCHES` edge. File nodes are not created. If the source PR has `files_truncated=true`, its `TOUCHES` edges represent only the first 100 returned files.

## Dependencies and Ownership

Manifest `dependencies` generate `DEPENDS_ON {kind: "dependency"}` edges; `after_dependencies` generate `kind: "after_dependency"`. A single sorted traversal of Python files under `homeassistant/components` attempts to parse each non-test source file once with `ast`. Imports of `homeassistant.components.<name>` produce `kind: "python_import"` edges. If parsing raises `SyntaxError`, a line-anchored regex fallback scans `from homeassistant.components.<name> ...` and `import homeassistant.components.<name>` statements; unreadable files are counted separately. The loader reports AST-parsed, regex-recovered, and unreadable file counts plus the three most common syntax error messages. Repeated imports of the same target within one source integration produce one edge per source/target/kind.

Manifest owners beginning `@org/team` create `Team {handle: "org/team"}`; other owners create `Person {login: "owner"}`. The loader reports dependency-edge counts separately by manifest dependencies, after-dependencies, and AST imports.

## Loading and Queries

The loader deletes only its selected graph name, recreates it, creates range indexes on `Person.login`, `Team.handle`, `Module.key`, `PR.number`, and `Decision.id`, then writes sorted `UNWIND $rows` batches of 750 by default. Node endpoints are matched separately before relationship `MERGE`; this avoids the full-path `MERGE` behavior that can duplicate partially matched endpoints. Every data value is passed as a query parameter. Reads use the official Python client's `ro_query` (`GRAPH.RO_QUERY`).

Rebuild is intentionally destructive and deterministic: rerunning the loader deletes and recreates only the chosen graph from the current local caches. It is not an incremental loader; retain or back up a graph under a different graph name before rebuilding if needed. Use `--graph` to select a tenant graph.

The current public-data rebuild parsed 8,794 source files with AST, recovered imports from 1,540 syntax-invalid files, and found 0 unreadable files. It produced 4,467 Python-import edges and 5,152 `DEPENDS_ON` edges total (542 manifest dependencies, 143 after-dependencies, 4,467 Python imports). The most common parser messages were `invalid syntax` (1,095), `multiple exception types must be parenthesized` (315), and `expected '('` (127). Representative files use Python 3.12 type aliases and PEP 695 generic functions; other files contain unparenthesized multi-exception handlers that are invalid Python syntax. Therefore the failures are not all explained by a Python 3.11 ceiling.

The loader prints node/edge counts, dependency-source counts, source scan counts, the three most common syntax errors, and elapsed load time, then runs its six read-only verification query groups. The zero-owner query is restricted to integration keys from parsed manifests, excluding test-only/path-inferred modules. It explicitly aliases and references relationships in aggregates because FalkorDB optimizes relationship matching when an edge variable is unused.