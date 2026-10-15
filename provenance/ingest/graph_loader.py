from __future__ import annotations

import ast
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from falkordb import FalkorDB
from rich.console import Console
from rich.table import Table

from provenance.config import settings


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "data" / "raw"
DEFAULT_GRAPH = "provenance_ha"
DEFAULT_BATCH_SIZE = 750
ADR_ID_PATTERN = re.compile(r"\bADR[-_ ]*0*(\d{1,5})\b", re.IGNORECASE)
ADR_PATH_PATTERN = re.compile(
    r"github\.com/home-assistant/architecture/blob/[^/]+/adr/0*(\d{1,5})-[^\s)#?]+",
    re.IGNORECASE,
)
ARCHITECTURE_LINK_PATTERN = re.compile(
    r"https?://github\.com/home-assistant/architecture(?:/[^\s<>\])}]*)?",
    re.IGNORECASE,
)
ARCHITECTURE_REFERENCE_PATTERN = re.compile(
    r"/([a-z]+)/([0-9]+)(?:[/?#]|$)", re.IGNORECASE
)
BREAKING_CHECKBOX_PATTERN = re.compile(
    r"(?im)^[ \t]*-[ \t]*\[[xX]\][ \t]*Breaking change\b"
)
BREAKING_HEADING_PATTERN = re.compile(r"(?im)^##[ \t]+Breaking change[ \t]*$")
NEXT_HEADING_PATTERN = re.compile(r"(?m)^#{1,6}[ \t]+")
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)

NODE_LABELS = ("Person", "Team", "Module", "PR", "Decision")
EDGE_TYPES = (
    "AUTHORED",
    "REVIEWED",
    "TOUCHES",
    "DECLARED_OWNER",
    "DEPENDS_ON",
    "SUPERSEDES",
    "REFERENCES",
    "DECIDED_AS",
)
NODE_KEY_PROPERTIES = {
    "Person": "login",
    "Team": "handle",
    "Module": "key",
    "PR": "number",
    "Decision": "id",
}
console = Console()

INDEX_QUERIES = (
    "CREATE INDEX FOR (n:Person) ON (n.login)",
    "CREATE INDEX FOR (n:Team) ON (n.handle)",
    "CREATE INDEX FOR (n:Module) ON (n.key)",
    "CREATE INDEX FOR (n:PR) ON (n.number)",
    "CREATE INDEX FOR (n:Decision) ON (n.id)",
)

NODE_LOAD_QUERIES = {
    "Person": """
        UNWIND $rows AS row
        MERGE (n:Person {login: row.login})
        SET n.is_bot = row.is_bot
    """,
    "Team": """
        UNWIND $rows AS row
        MERGE (n:Team {handle: row.handle})
    """,
    "Module": """
        UNWIND $rows AS row
        MERGE (n:Module {key: row.key})
        SET n.kind = row.kind, n.name = row.name
    """,
    "PR": """
        UNWIND $rows AS row
        MERGE (n:PR {number: row.number})
        SET n.title = row.title,
            n.merged_ts = row.merged_ts,
            n.additions = row.additions,
            n.deletions = row.deletions,
            n.breaking_box = row.breaking_box,
            n.breaking_text = row.breaking_text,
            n.labels = row.labels
    """,
    "Decision": """
        UNWIND $rows AS row
        MERGE (n:Decision {id: row.id})
        SET n.title = row.title, n.status = row.status, n.source = row.source
    """,
}

EDGE_LOAD_QUERIES = {
    "AUTHORED": """
        UNWIND $rows AS row
        MATCH (person:Person {login: row.login})
        MATCH (pr:PR {number: row.pr_number})
        MERGE (person)-[edge:AUTHORED]->(pr)
        SET edge.ts = row.ts
    """,
    "REVIEWED": """
        UNWIND $rows AS row
        MATCH (person:Person {login: row.login})
        MATCH (pr:PR {number: row.pr_number})
        MERGE (person)-[edge:REVIEWED {state: row.state, ts: row.ts}]->(pr)
    """,
    "TOUCHES": """
        UNWIND $rows AS row
        MATCH (pr:PR {number: row.pr_number})
        MATCH (module:Module {key: row.module_key})
        MERGE (pr)-[edge:TOUCHES]->(module)
        SET edge.files = row.files,
            edge.additions = row.additions,
            edge.deletions = row.deletions
    """,
    "PERSON_OWNER": """
        UNWIND $rows AS row
        MATCH (person:Person {login: row.login})
        MATCH (module:Module {key: row.module_key})
        MERGE (person)-[:DECLARED_OWNER]->(module)
    """,
    "TEAM_OWNER": """
        UNWIND $rows AS row
        MATCH (team:Team {handle: row.handle})
        MATCH (module:Module {key: row.module_key})
        MERGE (team)-[:DECLARED_OWNER]->(module)
    """,
    "DEPENDS_ON": """
        UNWIND $rows AS row
        MATCH (source:Module {key: row.source_key})
        MATCH (target:Module {key: row.target_key})
        MERGE (source)-[:DEPENDS_ON {kind: row.kind}]->(target)
    """,
    "SUPERSEDES": """
        UNWIND $rows AS row
        MATCH (newer:Decision {id: row.newer_id})
        MATCH (older:Decision {id: row.older_id})
        MERGE (newer)-[:SUPERSEDES]->(older)
    """,
    "REFERENCES": """
        UNWIND $rows AS row
        MATCH (pr:PR {number: row.pr_number})
        MATCH (decision:Decision {id: row.decision_id})
        MERGE (pr)-[:REFERENCES]->(decision)
    """,
    "DECIDED_AS": """
        UNWIND $rows AS row
        MATCH (pr:PR {number: row.pr_number})
        MATCH (decision:Decision {id: row.decision_id})
        MERGE (pr)-[:DECIDED_AS]->(decision)
    """,
}


@dataclass
class GraphDataset:
    nodes: dict[str, dict[Any, dict[str, Any]]] = field(
        default_factory=lambda: {label: {} for label in NODE_LABELS}
    )
    edges: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = field(
        default_factory=lambda: {edge_type: {} for edge_type in EDGE_TYPES}
    )
    dependency_edges_by_source: Counter[str] = field(default_factory=Counter)
    source_stats: Counter[str] = field(default_factory=Counter)
    integration_manifest_keys: set[str] = field(default_factory=set)

    def add_node(self, label: str, properties: dict[str, Any]) -> None:
        key_property = NODE_KEY_PROPERTIES[label]
        identity = properties[key_property]
        existing = self.nodes[label].get(identity)
        if existing is None:
            self.nodes[label][identity] = dict(properties)
            return
        if label == "Person":
            existing["is_bot"] = bool(existing.get("is_bot") or properties.get("is_bot"))
        else:
            existing.update(properties)

    def add_edge(
        self, edge_type: str, identity: tuple[Any, ...], properties: dict[str, Any]
    ) -> bool:
        if identity in self.edges[edge_type]:
            return False
        self.edges[edge_type][identity] = dict(properties)
        return True

    def sorted_node_rows(self, label: str) -> list[dict[str, Any]]:
        return [
            self.nodes[label][identity]
            for identity in sorted(self.nodes[label], key=lambda item: (str(type(item)), item))
        ]

    def sorted_edge_rows(self, edge_type: str) -> list[dict[str, Any]]:
        return [
            self.edges[edge_type][identity]
            for identity in sorted(
                self.edges[edge_type],
                key=lambda item: tuple(str(part) for part in item),
            )
        ]


def normalize_login(value: str) -> str:
    return value.strip().removeprefix("@").casefold()


def module_for_path(path: str) -> dict[str, str]:
    normalized = path.replace("\\", "/").strip("/")
    parts = PurePosixPath(normalized).parts
    if len(parts) >= 3 and parts[:2] == ("homeassistant", "components"):
        name = parts[2]
        return {"key": f"integration:{name}", "kind": "integration", "name": name}
    if len(parts) >= 3 and parts[:2] == ("tests", "components"):
        name = parts[2]
        return {"key": f"integration:{name}", "kind": "integration", "name": name}
    if len(parts) >= 2 and parts[0] == "homeassistant":
        name = parts[1]
        return {"key": f"homeassistant/{name}", "kind": "core", "name": name}
    root_name = parts[0] if parts else "unknown"
    return {"key": f"infra:{root_name}", "kind": "infra", "name": root_name}


def to_epoch_seconds(value: str | None) -> int | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def has_breaking_text(body: str) -> bool:
    heading = BREAKING_HEADING_PATTERN.search(body)
    if heading is None:
        return False
    section = body[heading.end() :]
    next_heading = NEXT_HEADING_PATTERN.search(section)
    if next_heading:
        section = section[: next_heading.start()]
    section = HTML_COMMENT_PATTERN.sub("", section)
    return bool(section.strip())


def _adr_status_and_links(text: str) -> tuple[str | None, list[str], list[str]]:
    lines = text.splitlines()
    status: str | None = None
    supersedes: set[str] = set()
    superseded_by: set[str] = set()
    for index, line in enumerate(lines):
        if re.match(r"^\s*#{1,6}\s+status\s*$", line, re.IGNORECASE):
            for candidate in lines[index + 1 :]:
                value = candidate.strip()
                if not value:
                    continue
                if value.startswith("#"):
                    break
                status = value
                break
        lowered = line.casefold()
        refs = {f"{int(match.group(1)):04d}" for match in ADR_ID_PATTERN.finditer(line)}
        if "superseded by" in lowered:
            superseded_by.update(refs)
        elif "supersedes" in lowered:
            supersedes.update(refs)
    return status, sorted(supersedes), sorted(superseded_by)


def parse_adr(path: Path, architecture_root: Path) -> dict[str, Any] | None:
    match = re.match(r"(\d+)", path.stem)
    if match is None:
        return None
    adr_id = f"{int(match.group(1)):04d}"
    text = path.read_text(encoding="utf-8", errors="replace")
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    title = heading.group(1).strip() if heading else path.stem
    title = re.sub(r"^0*\d+[.:\s]+", "", title).strip()
    status, supersedes, superseded_by = _adr_status_and_links(text)
    return {
        "id": adr_id,
        "title": title,
        "status": status,
        "source": path.relative_to(architecture_root).as_posix(),
        "supersedes": supersedes,
        "superseded_by": superseded_by,
    }


def _architecture_reference_decisions(
    body: str, known_adrs: set[str]
) -> tuple[list[dict[str, Any]], set[str]]:
    decisions: dict[str, dict[str, Any]] = {}
    references: set[str] = set()
    for raw_url in ARCHITECTURE_LINK_PATTERN.findall(body):
        url = raw_url.rstrip(".,;:!?\"'")
        adr_match = ADR_PATH_PATTERN.search(url)
        if adr_match:
            adr_id = f"{int(adr_match.group(1)):04d}"
            if adr_id not in known_adrs:
                decisions.setdefault(
                    adr_id,
                    {
                        "id": adr_id,
                        "title": f"ADR {adr_id} (not present in local clone)",
                        "status": None,
                        "source": "architecture-adr-reference",
                    },
                )
            references.add(adr_id)
            continue
        reference_match = ARCHITECTURE_REFERENCE_PATTERN.search(url)
        if reference_match:
            resource, number = reference_match.groups()
            resource = resource.casefold()
            if resource in {"discussions", "issues", "pull"}:
                decision_id = f"architecture:{resource}:{number}"
                title_resource = (
                    "discussion"
                    if resource == "discussions"
                    else "issue"
                    if resource == "issues"
                    else "pull request"
                )
                decisions.setdefault(
                    decision_id,
                    {
                        "id": decision_id,
                        "title": f"Architecture {title_resource} #{number}",
                        "status": "referenced",
                        "source": "architecture-reference",
                    },
                )
                references.add(decision_id)
                continue
        decision_id = "architecture:repository"
        decisions.setdefault(
            decision_id,
            {
                "id": decision_id,
                "title": "Home Assistant architecture repository",
                "status": "referenced",
                "source": "architecture-reference",
            },
        )
        references.add(decision_id)
    return list(decisions.values()), references


def _add_owner(dataset: GraphDataset, owner_text: str, module_key: str) -> None:
    owner_text = owner_text.strip()
    if not owner_text:
        return
    unprefixed = owner_text.removeprefix("@")
    if "/" in unprefixed:
        handle = unprefixed.casefold()
        dataset.add_node("Team", {"handle": handle})
        dataset.add_edge(
            "DECLARED_OWNER",
            ("Team", handle, module_key),
            {"handle": handle, "module_key": module_key},
        )
        return
    login = normalize_login(owner_text)
    dataset.add_node("Person", {"login": login, "is_bot": False})
    dataset.add_edge(
        "DECLARED_OWNER",
        ("Person", login, module_key),
        {"login": login, "module_key": module_key},
    )


def _add_dependency(
    dataset: GraphDataset, source_key: str, target_name: str, kind: str, count_as: str
) -> None:
    target_name = target_name.strip()
    if not target_name:
        return
    target = {"key": f"integration:{target_name}", "kind": "integration", "name": target_name}
    dataset.add_node("Module", target)
    added = dataset.add_edge(
        "DEPENDS_ON",
        (source_key, target["key"], kind),
        {"source_key": source_key, "target_key": target["key"], "kind": kind},
    )
    if added:
        dataset.dependency_edges_by_source[count_as] += 1


def _imported_integration(module_name: str | None) -> str | None:
    prefix = "homeassistant.components."
    if not module_name or not module_name.startswith(prefix):
        return None
    remainder = module_name[len(prefix) :]
    return remainder.split(".", 1)[0] if remainder else None


def _scan_component_imports(
    dataset: GraphDataset,
    core_root: Path,
    component_root: Path,
) -> None:
    for source_path in sorted(component_root.rglob("*.py")):
        relative = source_path.relative_to(core_root).as_posix()
        parts = PurePosixPath(relative).parts
        if len(parts) < 4 or parts[0:2] != ("homeassistant", "components"):
            continue
        if any(part.casefold() == "tests" for part in parts[3:]):
            continue
        source_name = parts[2]
        source_module = module_for_path(relative)
        dataset.add_node("Module", source_module)
        dataset.source_stats["python_files_considered"] += 1
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, UnicodeDecodeError, SyntaxError):
            dataset.source_stats["python_files_skipped_parse_error"] += 1
            continue
        dataset.source_stats["python_files_parsed"] += 1
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target_name = _imported_integration(alias.name)
                    if target_name:
                        targets.add(target_name)
            elif isinstance(node, ast.ImportFrom):
                target_name = _imported_integration(node.module)
                if target_name:
                    targets.add(target_name)
        for target_name in sorted(targets):
            if target_name != source_name:
                _add_dependency(
                    dataset,
                    source_module["key"],
                    target_name,
                    "python_import",
                    "python_imports",
                )


def _load_pr_page_cache(dataset: GraphDataset, pages_path: Path, adr_ids: set[str]) -> None:
    pull_requests: dict[int, dict[str, Any]] = {}
    with pages_path.open("r", encoding="utf-8") as pages_file:
        for line_number, line in enumerate(pages_file, start=1):
            if not line.strip():
                continue
            try:
                page = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid PR cache JSONL at line {line_number}.") from error
            for pull_request in page.get("pull_requests", []):
                pull_requests.setdefault(int(pull_request["number"]), pull_request)

    for number in sorted(pull_requests):
        record = pull_requests[number]
        merged_ts = to_epoch_seconds(record.get("mergedAt"))
        if merged_ts is None:
            dataset.source_stats["pull_requests_missing_mergedAt"] += 1
            continue
        body = record.get("body") or ""
        breaking_box = bool(BREAKING_CHECKBOX_PATTERN.search(body))
        breaking_text = has_breaking_text(body)
        labels = list(dict.fromkeys(label for label in record.get("labels", []) if label))
        dataset.add_node(
            "PR",
            {
                "number": number,
                "title": record.get("title") or "",
                "merged_ts": merged_ts,
                "additions": int(record.get("additions") or 0),
                "deletions": int(record.get("deletions") or 0),
                "breaking_box": breaking_box,
                "breaking_text": breaking_text,
                "labels": labels,
            },
        )
        dataset.source_stats["pull_requests_loaded"] += 1

        author = record.get("author") or {}
        author_login = normalize_login(author.get("login") or "")
        if author_login:
            dataset.add_node(
                "Person",
                {"login": author_login, "is_bot": author.get("__typename") == "Bot"},
            )
            authored_ts = to_epoch_seconds(record.get("createdAt"))
            if authored_ts is not None:
                dataset.add_edge(
                    "AUTHORED",
                    (author_login, number),
                    {"login": author_login, "pr_number": number, "ts": authored_ts},
                )

        for review in (record.get("reviews") or {}).get("nodes", []):
            reviewer = review.get("author") or {}
            reviewer_login = normalize_login(reviewer.get("login") or "")
            if (
                not reviewer_login
                or reviewer.get("__typename") != "User"
                or reviewer_login == author_login
            ):
                continue
            reviewed_ts = to_epoch_seconds(review.get("submittedAt"))
            if reviewed_ts is None:
                continue
            dataset.add_node("Person", {"login": reviewer_login, "is_bot": False})
            state = str(review.get("state") or "UNKNOWN")
            dataset.add_edge(
                "REVIEWED",
                (reviewer_login, number, state, reviewed_ts),
                {
                    "login": reviewer_login,
                    "pr_number": number,
                    "state": state,
                    "ts": reviewed_ts,
                },
            )

        touch_summaries: dict[str, dict[str, Any]] = {}
        for changed_file in (record.get("files") or {}).get("nodes", []):
            path = changed_file.get("path") or ""
            if not path:
                continue
            module = module_for_path(path)
            dataset.add_node("Module", module)
            summary = touch_summaries.setdefault(
                module["key"],
                {
                    "pr_number": number,
                    "module_key": module["key"],
                    "files": 0,
                    "additions": 0,
                    "deletions": 0,
                },
            )
            summary["files"] += 1
            summary["additions"] += int(changed_file.get("additions") or 0)
            summary["deletions"] += int(changed_file.get("deletions") or 0)
        for module_key, summary in touch_summaries.items():
            dataset.add_edge("TOUCHES", (number, module_key), summary)

        reference_decisions, decision_ids = _architecture_reference_decisions(body, adr_ids)
        for decision in reference_decisions:
            dataset.add_node("Decision", decision)
        for decision_id in sorted(decision_ids):
            dataset.add_edge(
                "REFERENCES",
                (number, decision_id),
                {"pr_number": number, "decision_id": decision_id},
            )

        if breaking_box or breaking_text:
            decision_id = f"pr-breaking:{number}"
            dataset.add_node(
                "Decision",
                {
                    "id": decision_id,
                    "title": f"Breaking change: {record.get('title') or f'PR #{number}'}",
                    "status": "reported",
                    "source": "pr-breaking",
                },
            )
            dataset.add_edge(
                "DECIDED_AS",
                (number, decision_id),
                {"pr_number": number, "decision_id": decision_id},
            )


def collect_graph_dataset(
    *,
    prs_jsonl: Path,
    core_root: Path,
    architecture_root: Path,
) -> GraphDataset:
    dataset = GraphDataset()
    components_root = core_root / "homeassistant" / "components"
    manifest_paths = sorted(components_root.glob("*/manifest.json"))
    for manifest_path in manifest_paths:
        integration = manifest_path.parent.name
        module = {
            "key": f"integration:{integration}",
            "kind": "integration",
            "name": integration,
        }
        dataset.add_node("Module", module)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            dataset.source_stats["manifests_skipped_parse_error"] += 1
            continue
        dataset.source_stats["manifests_loaded"] += 1
        dataset.integration_manifest_keys.add(module["key"])
        owners = manifest.get("codeowners", []) if isinstance(manifest, dict) else []
        if isinstance(owners, list):
            for owner in owners:
                if isinstance(owner, str):
                    _add_owner(dataset, owner, module["key"])
        dependencies = manifest.get("dependencies", []) if isinstance(manifest, dict) else []
        after_dependencies = (
            manifest.get("after_dependencies", []) if isinstance(manifest, dict) else []
        )
        if isinstance(dependencies, list):
            for target in dependencies:
                if isinstance(target, str):
                    _add_dependency(
                        dataset,
                        module["key"],
                        target,
                        "dependency",
                        "manifest_dependencies",
                    )
        if isinstance(after_dependencies, list):
            for target in after_dependencies:
                if isinstance(target, str):
                    _add_dependency(
                        dataset,
                        module["key"],
                        target,
                        "after_dependency",
                        "manifest_after_dependencies",
                    )

    _scan_component_imports(dataset, core_root, components_root)

    adr_files = sorted((architecture_root / "adr").glob("*.md"))
    adrs: dict[str, dict[str, Any]] = {}
    for adr_path in adr_files:
        adr = parse_adr(adr_path, architecture_root)
        if adr is None:
            continue
        adrs[adr["id"]] = adr
        properties = {
            key: value
            for key, value in adr.items()
            if key not in {"supersedes", "superseded_by"}
        }
        dataset.add_node("Decision", properties)
    dataset.source_stats["adrs_loaded"] = len(adrs)

    for adr in adrs.values():
        for older_id in adr["supersedes"]:
            if older_id in adrs:
                dataset.add_edge(
                    "SUPERSEDES",
                    (adr["id"], older_id),
                    {"newer_id": adr["id"], "older_id": older_id},
                )
        for newer_id in adr["superseded_by"]:
            if newer_id in adrs:
                dataset.add_edge(
                    "SUPERSEDES",
                    (newer_id, adr["id"]),
                    {"newer_id": newer_id, "older_id": adr["id"]},
                )

    _load_pr_page_cache(dataset, prs_jsonl, set(adrs))
    return dataset


def _chunks(rows: list[dict[str, Any]], batch_size: int):
    for start in range(0, len(rows), batch_size):
        yield rows[start : start + batch_size]


def _write_batches(graph: Any, query: str, rows: list[dict[str, Any]], batch_size: int) -> int:
    written = 0
    for batch in _chunks(rows, batch_size):
        graph.query(query, {"rows": batch})
        written += len(batch)
    return written


def _scalar_result(graph: Any, query: str) -> int:
    rows = graph.ro_query(query).result_set
    return int(rows[0][0]) if rows else 0


def delete_graph_if_exists(database: FalkorDB, graph_name: str) -> bool:
    existing_graphs = {
        graph.decode("utf-8") if isinstance(graph, bytes) else str(graph)
        for graph in database.list_graphs()
    }
    if graph_name not in existing_graphs:
        return False
    database.select_graph(graph_name).delete()
    return True


def load_dataset(
    dataset: GraphDataset,
    *,
    graph_name: str = DEFAULT_GRAPH,
    batch_size: int = DEFAULT_BATCH_SIZE,
    database: FalkorDB | None = None,
) -> tuple[Any, dict[str, Any]]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if database is None:
        database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
    started = time.perf_counter()
    delete_graph_if_exists(database, graph_name)
    graph = database.select_graph(graph_name)

    for index_query in INDEX_QUERIES:
        graph.query(index_query)

    for label in NODE_LABELS:
        _write_batches(
            graph,
            NODE_LOAD_QUERIES[label],
            dataset.sorted_node_rows(label),
            batch_size,
        )

    edge_write_order = (
        "AUTHORED",
        "REVIEWED",
        "TOUCHES",
        "PERSON_OWNER",
        "TEAM_OWNER",
        "DEPENDS_ON",
        "SUPERSEDES",
        "REFERENCES",
        "DECIDED_AS",
    )
    edge_to_dataset_type = {"PERSON_OWNER": "DECLARED_OWNER", "TEAM_OWNER": "DECLARED_OWNER"}
    for write_type in edge_write_order:
        dataset_type = edge_to_dataset_type.get(write_type, write_type)
        rows = dataset.sorted_edge_rows(dataset_type)
        if write_type == "PERSON_OWNER":
            rows = [row for row in rows if "login" in row]
        elif write_type == "TEAM_OWNER":
            rows = [row for row in rows if "handle" in row]
        if rows:
            _write_batches(graph, EDGE_LOAD_QUERIES[write_type], rows, batch_size)

    node_counts = {
        label: _scalar_result(graph, f"MATCH (n:{label}) RETURN count(n)")
        for label in NODE_LABELS
    }
    edge_counts = {
        edge_type: _scalar_result(
            graph, f"MATCH ()-[edge:{edge_type}]->() RETURN count(edge)"
        )
        for edge_type in EDGE_TYPES
    }
    summary = {
        "graph": graph_name,
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "dependency_edges_by_source": dict(sorted(dataset.dependency_edges_by_source.items())),
        "source_stats": dict(sorted(dataset.source_stats.items())),
        "load_seconds": time.perf_counter() - started,
        "batch_size": batch_size,
    }
    return graph, summary


def _rows(graph: Any, query: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
    return graph.ro_query(query, params or {}).result_set


def _top_module_dependents(graph: Any, module_key: str) -> list[dict[str, Any]]:
    rows = _rows(
        graph,
        """
        MATCH (dependent:Module)-[edge:DEPENDS_ON]->(target:Module {key: $module_key})
        RETURN dependent.key, edge.kind
        ORDER BY dependent.key, edge.kind
        """,
        {"module_key": module_key},
    )
    counts: Counter[str] = Counter()
    kinds: dict[str, set[str]] = defaultdict(set)
    for dependent_key, kind in rows:
        counts[str(dependent_key)] += 1
        kinds[str(dependent_key)].add(str(kind))
    return [
        {"module": key, "edges": counts[key], "kinds": sorted(kinds[key])}
        for key in sorted(counts, key=lambda item: (-counts[item], item))[:5]
    ]


def run_read_only_verification(
    graph: Any, integration_manifest_keys: set[str] | None = None
) -> dict[str, Any]:
    results: dict[str, Any] = {"graph_counts": {}, "queries": {}}
    for label in NODE_LABELS:
        results["graph_counts"].setdefault("nodes", {})[label] = _scalar_result(
            graph, f"MATCH (node:{label}) RETURN count(node)"
        )
    for edge_type in EDGE_TYPES:
        results["graph_counts"].setdefault("edges", {})[edge_type] = _scalar_result(
            graph, f"MATCH ()-[edge:{edge_type}]->() RETURN count(edge)"
        )

    results["queries"]["top_reviewers"] = _rows(
        graph,
        """
        MATCH (person:Person)-[review:REVIEWED]->(pr:PR)
        WHERE person.is_bot = false AND review.ts >= 0
        RETURN person.login, count(DISTINCT pr.number) AS pr_count
        ORDER BY pr_count DESC, person.login ASC
        LIMIT 10
        """,
    )

    in_degree_rows = _rows(
        graph,
        """
        MATCH (dependent:Module)-[edge:DEPENDS_ON]->(target:Module {kind: 'integration'})
        RETURN target.key, count(edge) AS dependent_count
        ORDER BY dependent_count DESC, target.key ASC
        LIMIT 1
        """,
    )
    most_depended_on = str(in_degree_rows[0][0]) if in_degree_rows else None
    results["queries"]["most_depended_on_integration"] = {
        "module": most_depended_on,
        "in_degree": int(in_degree_rows[0][1]) if in_degree_rows else 0,
        "top_dependents": _top_module_dependents(graph, most_depended_on)
        if most_depended_on
        else [],
        "top_reviewers": _rows(
            graph,
            """
            MATCH (person:Person)-[review:REVIEWED]->(pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
            WHERE person.is_bot = false AND review.ts >= 0 AND touch.files > 0
            RETURN person.login, count(DISTINCT pr.number) AS pr_count
            ORDER BY pr_count DESC, person.login ASC
            LIMIT 5
            """,
            {"module_key": most_depended_on},
        )
        if most_depended_on
        else [],
    }

    exactly_one_owner_rows = _rows(
        graph,
        """
        MATCH (owner)-[declared:DECLARED_OWNER]->(module:Module {kind: 'integration'})
        RETURN module.key, count(declared) AS owner_count
        ORDER BY module.key ASC
        """,
    )
    one_owner_module = next(
        (str(row[0]) for row in exactly_one_owner_rows if int(row[1]) == 1),
        None,
    )
    if one_owner_module:
        owner_rows = _rows(
            graph,
            """
            MATCH (owner)-[declared:DECLARED_OWNER]->(module:Module {key: $module_key})
            RETURN owner.login, owner.handle
            """,
            {"module_key": one_owner_module},
        )
        reviewer_rows = _rows(
            graph,
            """
            MATCH (person:Person)-[review:REVIEWED]->(pr:PR)-[touch:TOUCHES]->(module:Module {key: $module_key})
            WHERE person.is_bot = false AND review.ts >= 0 AND touch.files > 0
            RETURN person.login, count(DISTINCT pr.number) AS pr_count
            ORDER BY pr_count DESC, person.login ASC
            LIMIT 5
            """,
            {"module_key": one_owner_module},
        )
        owner_record = owner_rows[0] if owner_rows else [None, None]
        declared_owner = owner_record[0] if owner_record[0] is not None else owner_record[1]
        results["queries"]["single_owner_vs_reviewers"] = {
            "module": one_owner_module,
            "declared_owner": declared_owner,
            "top_reviewers": reviewer_rows,
        }
    else:
        results["queries"]["single_owner_vs_reviewers"] = None

    cochange_started = time.perf_counter()
    cochange_rows = _rows(
        graph,
        """
        MATCH (source:Module {key: $module_key})<-[source_touch:TOUCHES]-(pr:PR)-[other_touch:TOUCHES]->(other:Module)
        WHERE other.key <> source.key
          AND source_touch.files > 0
          AND other_touch.files > 0
        WITH other, count(DISTINCT pr.number) AS shared_count
        WHERE shared_count >= $minimum_shared
        RETURN other.key, shared_count
        ORDER BY shared_count DESC, other.key ASC
        LIMIT 10
        """,
        {"module_key": "integration:zha", "minimum_shared": 5},
    )
    results["queries"]["zha_cochange"] = {
        "module": "integration:zha",
        "minimum_shared_prs": 5,
        "rows": cochange_rows,
        "latency_ms": round((time.perf_counter() - cochange_started) * 1000, 3),
    }

    manifest_keys = sorted(integration_manifest_keys or set())
    zero_owner_rows = (
        _rows(
            graph,
            """
            UNWIND $integration_keys AS module_key
            MATCH (module:Module {key: module_key})
            OPTIONAL MATCH (owner)-[declared:DECLARED_OWNER]->(module)
            WITH module, count(declared) AS owner_count
            WHERE owner_count = 0
            OPTIONAL MATCH (pr:PR)-[touch:TOUCHES]->(module)
            RETURN count(DISTINCT module.key), count(DISTINCT pr.number), count(DISTINCT touch)
            """,
            {"integration_keys": manifest_keys},
        )
        if manifest_keys
        else []
    )
    results["queries"]["zero_owner_integrations"] = zero_owner_rows or [[0, 0, 0]]
    return results


def print_summary(summary: dict[str, Any]) -> None:
    table = Table(title=f"FalkorDB graph {summary['graph']} load")
    table.add_column("Kind")
    table.add_column("Label / type")
    table.add_column("Count", justify="right")
    for label, count in summary["node_counts"].items():
        table.add_row("Node", label, str(count))
    for edge_type, count in summary["edge_counts"].items():
        table.add_row("Edge", edge_type, str(count))
    console.print(table)
    console.print(f"Load time: {summary['load_seconds']:.2f}s; batch size: {summary['batch_size']}")
    console.print("Dependency edges by source:")
    for source, count in summary["dependency_edges_by_source"].items():
        console.print(f"  {source}: {count}")
    console.print("Source scan:")
    for name, count in summary["source_stats"].items():
        console.print(f"  {name}: {count}")


def print_verification(results: dict[str, Any]) -> None:
    console.print("\n[bold]Read-only verification[/bold]")
    counts = Table(title="1. Node and edge counts")
    counts.add_column("Nodes")
    counts.add_column("Count", justify="right")
    counts.add_column("Edges")
    counts.add_column("Count", justify="right")
    labels = list(results["graph_counts"]["nodes"].items())
    edges = list(results["graph_counts"]["edges"].items())
    for index in range(max(len(labels), len(edges))):
        label, label_count = labels[index] if index < len(labels) else ("", "")
        edge, edge_count = edges[index] if index < len(edges) else ("", "")
        counts.add_row(label, str(label_count), edge, str(edge_count))
    console.print(counts)

    reviewers = Table(title="2. Top 10 human reviewers by reviewed PRs")
    reviewers.add_column("Login")
    reviewers.add_column("PRs", justify="right")
    for login, count in results["queries"]["top_reviewers"]:
        reviewers.add_row(str(login), str(count))
    console.print(reviewers)

    dependency = results["queries"]["most_depended_on_integration"]
    console.print(
        f"\n3. Most depended-on integration: {dependency['module']} "
        f"({dependency['in_degree']} incoming DEPENDS_ON edges)"
    )
    console.print(f"   Top dependents: {dependency['top_dependents']}")
    console.print(f"   Top reviewers: {dependency['top_reviewers']}")

    single_owner = results["queries"]["single_owner_vs_reviewers"]
    if single_owner:
        console.print(
            f"\n4. One-owner integration {single_owner['module']}: "
            f"declared owner={single_owner['declared_owner']}; "
            f"top actual reviewers={single_owner['top_reviewers']}"
        )
    else:
        console.print("\n4. No one-owner integration was present.")

    cochange = results["queries"]["zha_cochange"]
    console.print(
        f"\n5. Modules sharing >=5 PRs with integration:zha "
        f"(latency {cochange['latency_ms']} ms): {cochange['rows']}"
    )
    zero_owner = results["queries"]["zero_owner_integrations"]
    console.print(
        "\n6. Integrations with zero declared owners / PRs touching them: "
        f"{zero_owner[0][0]} / {zero_owner[0][1]}"
    )


def load_graph(
    *,
    graph_name: str = DEFAULT_GRAPH,
    batch_size: int = DEFAULT_BATCH_SIZE,
    prs_jsonl: Path = RAW_ROOT / "prs" / "pages.jsonl",
    core_root: Path = RAW_ROOT / "core",
    architecture_root: Path = RAW_ROOT / "architecture",
    database: FalkorDB | None = None,
    run_verification: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    dataset = collect_graph_dataset(
        prs_jsonl=Path(prs_jsonl),
        core_root=Path(core_root),
        architecture_root=Path(architecture_root),
    )
    graph, summary = load_dataset(
        dataset,
        graph_name=graph_name,
        batch_size=batch_size,
        database=database,
    )
    verification = (
        run_read_only_verification(graph, dataset.integration_manifest_keys)
        if run_verification
        else None
    )
    return summary, verification