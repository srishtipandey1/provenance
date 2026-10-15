from __future__ import annotations

import fnmatch
import json
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.config import settings


RAW_ROOT = ROOT / "data" / "raw"
GATE_ROOT = RAW_ROOT / "gate0"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
CORE_URL = "https://github.com/home-assistant/core"
ARCHITECTURE_URL = "https://github.com/home-assistant/architecture"
PAGE_SIZE = 25
PAGES_TO_FETCH = 3
GRAPHQL_POINTS_PER_HOUR = 5000
BREAKING_LABEL_PATTERN = re.compile(r"breaking[\s_-]*change", re.IGNORECASE)

console = Console()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_error(error: Exception, token: str | None) -> str:
    message = str(error) or error.__class__.__name__
    if token:
        message = message.replace(token, "[REDACTED]")
    return message


def clone_if_missing(url: str, destination: Path, token: str | None) -> str:
    if destination.exists():
        if (destination / ".git").exists():
            return "already present"
        raise RuntimeError(f"{destination} exists but is not a Git repository")

    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        if token:
            details = details.replace(token, "[REDACTED]")
        raise RuntimeError(f"git clone failed ({result.returncode}): {details}")
    return "cloned with depth 1"


def normalize_owner(value: str) -> str:
    return value.strip().removeprefix("@").casefold()


def manifest_owners(path: Path) -> tuple[bool, bool, set[str]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, False, set()

    raw_owners = manifest.get("codeowners") if isinstance(manifest, dict) else None
    if not isinstance(raw_owners, list):
        return False, False, set()
    parseable = all(isinstance(owner, str) for owner in raw_owners)
    owners = {normalize_owner(owner) for owner in raw_owners if isinstance(owner, str) and owner.strip()}
    return parseable, True, owners


def parse_codeowners(path: Path) -> list[tuple[str, list[str]]]:
    rules: list[tuple[str, list[str]]] = []
    if not path.is_file():
        return rules
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        fields = stripped.split()
        if len(fields) < 2 or fields[0].startswith("!"):
            continue
        owners = [normalize_owner(item) for item in fields[1:] if item.startswith("@")]
        owners = [owner for owner in owners if owner]
        if owners:
            rules.append((fields[0], owners))
    return rules


@lru_cache(maxsize=8192)
def codeowners_pattern_matches(pattern: str, path: str) -> bool:
    pattern = pattern.lstrip("/")
    directory_rule = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if not pattern:
        return False

    path_parts = tuple(part for part in path.strip("/").split("/") if part)
    pattern_parts = tuple(part for part in pattern.split("/") if part)
    if not pattern_parts:
        return False

    if len(pattern_parts) == 1:
        return any(fnmatch.fnmatchcase(part, pattern_parts[0]) for part in path_parts)

    @lru_cache(maxsize=None)
    def matches(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return True
        current = pattern_parts[pattern_index]
        if current == "**":
            if matches(pattern_index + 1, path_index):
                return True
            return path_index < len(path_parts) and matches(pattern_index, path_index + 1)
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], current)
            and matches(pattern_index + 1, path_index + 1)
        )

    if matches(0, 0):
        return True
    if directory_rule:
        return codeowners_pattern_matches(pattern + "/**", path)
    return False


def owners_for_path(path: str, rules: list[tuple[str, list[str]]]) -> set[str]:
    owners: list[str] = []
    for pattern, rule_owners in rules:
        if codeowners_pattern_matches(pattern, path):
            owners = rule_owners
    return set(owners)


def collect_ownership(repo: Path) -> dict[str, Any]:
    components = repo / "homeassistant" / "components"
    integration_dirs = sorted(
        item.parent for item in components.glob("*/manifest.json") if item.is_file()
    )
    codeowners_path = repo / "CODEOWNERS"
    rules = parse_codeowners(codeowners_path)
    manifest_counts: dict[str, int] = {}
    top_level_counts: dict[str, int] = {}
    integrations: list[dict[str, Any]] = []

    for integration_dir in integration_dirs:
        domain = integration_dir.name
        relative_root = integration_dir.relative_to(repo).as_posix()
        parseable, has_list, declared = manifest_owners(integration_dir / "manifest.json")
        manifest_path = f"{relative_root}/manifest.json"
        effective_codeowners = owners_for_path(manifest_path, rules)

        for owner in declared:
            manifest_counts[owner] = manifest_counts.get(owner, 0) + 1
        for owner in effective_codeowners:
            top_level_counts[owner] = top_level_counts.get(owner, 0) + 1

        integrations.append(
            {
                "domain": domain,
                "manifest_codeowners_parseable": parseable,
                "manifest_codeowners_list_present": has_list,
                "manifest_owners": sorted(declared),
                "codeowners_file_owners": sorted(effective_codeowners),
            }
        )

    agreements = sum(item["manifest_owners"] == item["codeowners_file_owners"] for item in integrations)
    parseable_count = sum(item["manifest_codeowners_parseable"] for item in integrations)
    with_codeowners_list = sum(item["manifest_codeowners_list_present"] for item in integrations)
    with_manifest_owner = sum(bool(item["manifest_owners"]) for item in integrations)
    with_file_owner = sum(bool(item["codeowners_file_owners"]) for item in integrations)
    exactly_one = sum(len(item["manifest_owners"]) == 1 for item in integrations)
    none = sum(not item["manifest_owners"] for item in integrations)

    return {
        "integration_count": len(integrations),
        "manifest_codeowners_lists": with_codeowners_list,
        "parseable_manifest_codeowners": parseable_count,
        "integrations_with_manifest_owner": with_manifest_owner,
        "integrations_with_top_level_codeowners_owner": with_file_owner,
        "ownership_comparison_scope": "all integrations; empty owner sets agree when both sources have no owner",
        "codeowners_matching_scope": "effective last-match top-level CODEOWNERS owners for each integration manifest.json path",
        "ownership_agree": agreements,
        "ownership_disagree": len(integrations) - agreements,
        "integrations_with_exactly_one_manifest_owner": exactly_one,
        "integrations_with_no_manifest_owner": none,
        "top_10_manifest_owners_by_integrations": [
            {"owner": owner, "integrations": count}
            for owner, count in sorted(manifest_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
        "top_10_codeowners_file_owners_by_integrations": [
            {"owner": owner, "integrations": count}
            for owner, count in sorted(top_level_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
        "integrations": integrations,
    }


def locate_adr_folder(repo: Path) -> Path | None:
    candidates = [repo / "adr", repo / "docs" / "adr"]
    candidates.extend(path for path in repo.rglob("adr") if path.is_dir())
    return next((path for path in candidates if path.is_dir()), None)


def adr_references(text: str, field_pattern: re.Pattern[str]) -> list[str]:
    values = field_pattern.findall(text)
    references: set[str] = set()
    for value in values:
        for match in re.finditer(r"(?i)\bADR[-_ ]*(\d{1,5})\b", value):
            references.add(match.group(1).lstrip("0") or "0")
    return sorted(references, key=lambda item: int(item))


def parse_adr(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    id_match = re.search(r"(?im)^\s*(?:id\s*:\s*|ADR[-_ ]*)(\d{1,5})\b", text)
    if not id_match:
        id_match = re.search(r"(?i)(?:adr[-_ ]?)?(\d{1,5})", path.stem)
    title_match = re.search(r"(?im)^\s*title\s*:\s*[\"']?(.+?)[\"']?\s*$", text)
    if not title_match:
        title_match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    status_match = re.search(r"(?im)^\s*(?:[-*]\s*)?status\s*:\s*([^\r\n]+)", text)
    status_value = status_match.group(1).strip() if status_match else None
    if status_value is None:
        status_heading = re.search(r"(?im)^\s*#{1,6}\s+status\s*$", text)
        if status_heading:
            for line in text[status_heading.end() :].splitlines():
                candidate = line.strip()
                if candidate:
                    if candidate.startswith("#"):
                        break
                    status_value = candidate
                    break

    supersedes_pattern = re.compile(r"(?im)^\s*(?:[-*]\s*)?supersedes\s*:?\s*([^\r\n]+)")
    superseded_by_pattern = re.compile(
        r"(?im)^\s*(?:[-*]\s*)?superseded[- ]by\s*:?\s*([^\r\n]+)"
    )
    return {
        "id": id_match.group(1).lstrip("0") or "0" if id_match else None,
        "title": title_match.group(1).strip() if title_match else None,
        "status": status_value,
        "supersedes": adr_references(text, supersedes_pattern),
        "superseded_by": adr_references(text, superseded_by_pattern),
        "path": path.as_posix(),
    }


def collect_adrs(repo: Path) -> dict[str, Any]:
    adr_folder = locate_adr_folder(repo)
    if adr_folder is None:
        return {"adr_folder": None, "file_count": 0, "parsed_adrs": [], "status_count": 0, "linked_adr_count": 0}

    files = sorted(path for path in adr_folder.rglob("*") if path.is_file())
    parseable_files = [path for path in files if path.suffix.lower() in {".md", ".rst", ".txt", ".adoc"}]
    records = [parse_adr(path) for path in parseable_files]
    status_count = sum(bool(record["status"]) for record in records)
    linked_count = sum(bool(record["supersedes"] or record["superseded_by"]) for record in records)
    return {
        "adr_folder": adr_folder.relative_to(repo).as_posix(),
        "file_count": len(files),
        "parsed_file_count": len(records),
        "status_count": status_count,
        "linked_adr_count": linked_count,
        "examples": records[:3],
        "adrs": records,
    }


class GraphQLClient:
    def __init__(self, token: str):
        self.token = token
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=90.0,
        )

    def execute(self, query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        response = self.client.post(
            GITHUB_GRAPHQL_URL,
            json={"query": query, "variables": variables},
        )
        elapsed = time.perf_counter() - started
        if response.status_code != 200:
            raise RuntimeError(f"GitHub GraphQL returned HTTP {response.status_code}")
        payload = response.json()
        if payload.get("errors"):
            messages = [str(error.get("message", "GraphQL error")) for error in payload["errors"]]
            safe_messages = [message.replace(self.token, "[REDACTED]") for message in messages]
            raise RuntimeError("GitHub GraphQL error: " + "; ".join(safe_messages))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("GitHub GraphQL response did not contain data")
        return data, elapsed

    def close(self) -> None:
        self.client.close()


COUNT_QUERY = """
query CountMergedPullRequests($searchQuery: String!) {
  search(query: $searchQuery, type: ISSUE, first: 1) {
    issueCount
  }
  rateLimit { cost remaining resetAt }
}
"""

LABELS_QUERY = """
query RecentMergedPullRequestLabels {
  repository(owner: "home-assistant", name: "core") {
    pullRequests(first: 100, states: MERGED, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes {
        number
        mergedAt
        labels(first: 100) { nodes { name } }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

PULL_REQUESTS_QUERY = """
query MergedPullRequestPage($searchQuery: String!, $after: String) {
  search(query: $searchQuery, type: ISSUE, first: 25, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        author { login }
        mergedAt
        labels(first: 100) { nodes { name } }
        files(first: 100) {
          totalCount
          nodes { path additions deletions }
        }
        reviews(first: 100) {
          totalCount
          nodes { author { login } state submittedAt }
        }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""


def count_and_label_data(client: GraphQLClient, cutoff: str, samples: dict[str, Any], report: dict[str, Any]) -> None:
    search_query = f"repo:home-assistant/core is:pr is:merged merged:>={cutoff}"
    try:
        data, elapsed = client.execute(COUNT_QUERY, {"searchQuery": search_query})
        samples["merged_pr_count"] = data
        report["pull_requests"]["merged_in_last_180_days"] = data["search"]["issueCount"]
        report["pull_requests"]["count_query_elapsed_seconds"] = round(elapsed, 3)
    except Exception as error:
        report["errors"].append(f"180-day merged PR count: {safe_error(error, client.token)}")

    try:
        data, elapsed = client.execute(LABELS_QUERY, {})
        samples["recent_merged_pr_labels"] = data
        connection = data["repository"]["pullRequests"]
        label_names: set[str] = set()
        breaking_pr_count = 0
        for pull_request in connection["nodes"]:
            names = {label["name"] for label in pull_request["labels"]["nodes"]}
            label_names.update(names)
            if any(BREAKING_LABEL_PATTERN.search(name) for name in names):
                breaking_pr_count += 1
        report["pull_requests"]["label_sample"] = {
            "pr_count": len(connection["nodes"]),
            "ordering": "merged PRs ordered by updatedAt descending (closest available connection ordering)",
            "label_names": sorted(label_names, key=str.casefold),
            "breaking_change_label_names": sorted(
                (name for name in label_names if BREAKING_LABEL_PATTERN.search(name)),
                key=str.casefold,
            ),
            "pull_requests_with_breaking_change_label": breaking_pr_count,
            "query_elapsed_seconds": round(elapsed, 3),
        }
    except Exception as error:
        report["errors"].append(f"recent merged PR labels: {safe_error(error, client.token)}")

    try:
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        for page_number in range(1, PAGES_TO_FETCH + 1):
            data, elapsed = client.execute(
                PULL_REQUESTS_QUERY,
                {"searchQuery": search_query + " sort:updated-desc", "after": cursor},
            )
            connection = data["search"]
            page = {
                "page": page_number,
                "elapsed_seconds": round(elapsed, 3),
                "data": data,
            }
            pages.append(page)
            cursor = connection["pageInfo"]["endCursor"]
            if not connection["pageInfo"]["hasNextPage"]:
                break

        samples["merged_pr_pages"] = pages
        pull_requests = [
            pull_request
            for page in pages
            for pull_request in page["data"]["search"]["nodes"]
        ]
        truncated = sum(pull_request["files"]["totalCount"] > 100 for pull_request in pull_requests)
        zero_reviews = sum(pull_request["reviews"]["totalCount"] == 0 for pull_request in pull_requests)
        costs = [page["data"]["rateLimit"]["cost"] for page in pages]
        remaining = [page["data"]["rateLimit"]["remaining"] for page in pages]
        elapsed = [page["elapsed_seconds"] for page in pages]
        report["pull_requests"]["paginated_fetch"] = {
            "query_pages_requested": PAGES_TO_FETCH,
            "pages_fetched": len(pages),
            "page_size": PAGE_SIZE,
            "pull_requests_fetched": len(pull_requests),
            "ordering": "merged-date filtered, updatedAt descending; GitHub GraphQL does not offer mergedAt ordering",
            "cost_per_page": costs,
            "remaining_points_per_page": remaining,
            "average_cost_per_page": sum(costs) / len(costs) if costs else None,
            "pull_requests_with_more_than_100_files_truncated": truncated,
            "pull_requests_with_zero_reviews": zero_reviews,
            "elapsed_seconds_per_page": elapsed,
            "sample_pull_requests": pull_requests,
        }
    except Exception as error:
        report["errors"].append(f"paginated merged PR query: {safe_error(error, client.token)}")


def estimate_fetch(report: dict[str, Any]) -> dict[str, Any] | None:
    count = report["pull_requests"].get("merged_in_last_180_days")
    page_data = report["pull_requests"].get("paginated_fetch")
    if count is None or not page_data or not page_data["cost_per_page"]:
        return None

    pages = math.ceil(count / PAGE_SIZE)
    average_cost = page_data["average_cost_per_page"]
    estimated_points = math.ceil(pages * average_cost)
    seconds_per_page = page_data["elapsed_seconds_per_page"]
    average_seconds = sum(seconds_per_page) / len(seconds_per_page) if seconds_per_page else 0
    request_seconds = pages * average_seconds
    rate_budget_seconds = estimated_points / GRAPHQL_POINTS_PER_HOUR * 3600
    return {
        "merged_pr_count": count,
        "pages_at_25_prs_each": pages,
        "observed_average_points_per_page": round(average_cost, 2),
        "estimated_total_api_points": estimated_points,
        "graphql_budget_points_per_hour_assumed": GRAPHQL_POINTS_PER_HOUR,
        "estimated_api_budget_hours": round(rate_budget_seconds / 3600, 3),
        "estimated_serial_request_wall_seconds": round(request_seconds, 1),
        "estimated_wall_clock_hours_including_rate_budget": round(
            max(request_seconds, rate_budget_seconds) / 3600, 3
        ),
        "assumptions": [
            "Every PR page uses the same query shape and has the observed mean cost of the three sample pages.",
            "The GitHub GraphQL primary rate limit is 5,000 points per hour for this token.",
            "Serial request wall time scales linearly from the measured sample-page latency; retries and outages are excluded.",
            "GitHub search issueCount is used as the 180-day merged-PR total; the search index may lag briefly.",
        ],
    }


def print_summary(report: dict[str, Any]) -> None:
    ownership = report.get("declared_ownership", {})
    adrs = report.get("decisions", {})
    pull_requests = report.get("pull_requests", {})

    console.print("\n[bold]Declared ownership[/bold]")
    if ownership:
        console.print(
            f"Integrations: {ownership['integration_count']}; parseable manifest codeowners: "
            f"{ownership['parseable_manifest_codeowners']}; top-level CODEOWNERS owners: "
            f"{ownership['integrations_with_top_level_codeowners_owner']}"
        )
        console.print(
            f"Ownership agree/disagree: {ownership['ownership_agree']}/"
            f"{ownership['ownership_disagree']} (all integrations, matching CODEOWNERS on manifest paths); "
            f"exactly one manifest owner: {ownership['integrations_with_exactly_one_manifest_owner']}; "
            f"none: {ownership['integrations_with_no_manifest_owner']}"
        )
        console.print("Top 10 manifest owners:")
        for item in ownership["top_10_manifest_owners_by_integrations"]:
            console.print(f"  @{item['owner']}: {item['integrations']}")
    else:
        console.print("Unavailable")

    console.print("\n[bold]Decisions[/bold]")
    if adrs:
        console.print(
            f"ADR folder: {adrs.get('adr_folder')}; files: {adrs.get('file_count', 0)}; "
            f"with status: {adrs.get('status_count', 0)}; linked to another ADR: "
            f"{adrs.get('linked_adr_count', 0)}"
        )
        console.print("Parsed examples:")
        for example in adrs.get("examples", []):
            console.print(
                f"  {example['id']}: {example['title']} | status={example['status']} | "
                f"supersedes={example['supersedes']} | superseded_by={example['superseded_by']}"
            )
    else:
        console.print("Unavailable")
    labels = pull_requests.get("label_sample")
    if labels:
        console.print(
            f"Recent merged PR sample labels ({labels['pr_count']} PRs; "
            f"{labels['ordering']}): {labels['label_names']}"
        )
        console.print(
            f"Breaking-change labels: {labels['breaking_change_label_names']}; "
            f"PRs carrying one: {labels['pull_requests_with_breaking_change_label']}"
        )
    else:
        console.print("GraphQL label sample unavailable")

    console.print("\n[bold]PR data[/bold]")
    console.print(f"Merged in last 180 days: {pull_requests.get('merged_in_last_180_days', 'unavailable')}")
    page_data = pull_requests.get("paginated_fetch")
    if page_data:
        console.print(
            f"Pages fetched: {page_data['pages_fetched']}; PRs: {page_data['pull_requests_fetched']}; "
            f"rateLimit cost/page: {page_data['cost_per_page']}; remaining/page: "
            f"{page_data['remaining_points_per_page']}"
        )
        console.print(
            f"PRs with >100 files (truncated): "
            f"{page_data['pull_requests_with_more_than_100_files_truncated']}; "
            f"zero reviews: {page_data['pull_requests_with_zero_reviews']}"
        )
    else:
        console.print("Paginated query unavailable")

    estimate = report.get("estimate")
    if estimate:
        console.print(
            f"Estimated full-fetch API points: {estimate['estimated_total_api_points']}; "
            f"serial request wall time: {estimate['estimated_serial_request_wall_seconds']} seconds; "
            f"rate-budget time: {estimate['estimated_api_budget_hours']} hours"
        )
    else:
        console.print("Full-fetch estimate unavailable")


def print_gate_table(report: dict[str, Any]) -> None:
    ownership = report.get("declared_ownership", {})
    decisions = report.get("decisions", {})
    prs = report.get("pull_requests", {})
    estimate = report.get("estimate")
    checks = [
        (
            "(a) >=500 parseable integration owners",
            ownership.get("parseable_manifest_codeowners", 0) >= 500,
        ),
        (
            "(b) ADR status and supersession links",
            decisions.get("status_count", 0) > 0 and decisions.get("linked_adr_count", 0) > 0,
        ),
        (
            "(c) Paginated PR query includes files and reviews",
            prs.get("paginated_fetch", {}).get("pages_fetched") == PAGES_TO_FETCH
            and all(
                "files" in pull_request and "reviews" in pull_request
                for pull_request in prs.get("paginated_fetch", {}).get("sample_pull_requests", [])
            )
            and bool(prs.get("paginated_fetch", {}).get("sample_pull_requests")),
        ),
        (
            "(d) 180-day estimate under 3 API-budget hours",
            bool(estimate and estimate["estimated_api_budget_hours"] < 3),
        ),
    ]
    table = Table(title="Gate 0 Feasibility")
    table.add_column("Criterion")
    table.add_column("Result", justify="center")
    table.add_column("Evidence")
    for label, passed in checks:
        table.add_row(label, "PASS" if passed else "FAIL", "measured result in report.json")
    console.print(table)

    failures = [label for label, passed in checks if not passed]
    if failures:
        console.print("[bold red]Recommendation: SWITCH[/bold red] - " + "; ".join(failures))
    else:
        console.print(
            "[bold green]Recommendation: PROCEED with Home Assistant[/bold green] - "
            "all four feasibility gates passed."
        )


def main() -> int:
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    GATE_ROOT.mkdir(parents=True, exist_ok=True)
    token = settings.GITHUB_TOKEN
    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repositories": {},
        "declared_ownership": {},
        "decisions": {},
        "pull_requests": {},
        "estimate": None,
        "errors": [],
    }
    samples: dict[str, Any] = {}
    core_repo = RAW_ROOT / "core"
    architecture_repo = RAW_ROOT / "architecture"

    for name, url, path in (
        ("core", CORE_URL, core_repo),
        ("architecture", ARCHITECTURE_URL, architecture_repo),
    ):
        try:
            state = clone_if_missing(url, path, token)
            report["repositories"][name] = {"url": url, "path": path.relative_to(ROOT).as_posix(), "state": state}
            console.print(f"{name}: {state}")
        except Exception as error:
            message = safe_error(error, token)
            report["repositories"][name] = {"url": url, "state": "failed", "error": message}
            report["errors"].append(f"{name} clone: {message}")

    if core_repo.is_dir():
        try:
            console.print("Analyzing integration manifests and CODEOWNERS...")
            report["declared_ownership"] = collect_ownership(core_repo)
            samples["declared_ownership"] = report["declared_ownership"]
        except Exception as error:
            report["errors"].append(f"declared ownership: {safe_error(error, token)}")
    if architecture_repo.is_dir():
        try:
            console.print("Parsing architecture ADRs...")
            report["decisions"] = collect_adrs(architecture_repo)
            samples["decisions"] = report["decisions"]
        except Exception as error:
            report["errors"].append(f"ADR parsing: {safe_error(error, token)}")

    if not token:
        report["errors"].append("GitHub GraphQL: GITHUB_TOKEN is not configured; no GraphQL calls were made.")
    else:
        console.print("Querying GitHub GraphQL for merged PR counts, labels, files, and reviews...")
        client = GraphQLClient(token)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).date().isoformat()
        report["pull_requests"]["cutoff_date"] = cutoff
        try:
            count_and_label_data(client, cutoff, samples, report)
        finally:
            client.close()

    report["estimate"] = estimate_fetch(report)
    for name, sample in samples.items():
        write_json(GATE_ROOT / f"{name}.json", sample)
    write_json(GATE_ROOT / "report.json", report)

    print_summary(report)
    print_gate_table(report)
    if report["errors"]:
        console.print("\n[bold yellow]Errors / limitations[/bold yellow]")
        for error in report["errors"]:
            console.print(f"- {error}")
    console.print(f"\nSummary saved to {GATE_ROOT.relative_to(ROOT).as_posix()}/report.json")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())