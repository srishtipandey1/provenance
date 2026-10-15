from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from provenance.config import settings


RAW_ROOT = ROOT / "data" / "raw" / "prs"
CORE_ROOT = ROOT / "data" / "raw" / "core"
TEMPLATE_PATH = CORE_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
GRAPHQL_URL = "https://api.github.com/graphql"
BREAKING_CHECKBOX_PATTERN = re.compile(
    r"(?im)^[ \t]*-[ \t]*\[[xX]\][ \t]*Breaking change\b"
)
BREAKING_HEADING_PATTERN = re.compile(r"(?im)^##[ \t]+Breaking change[ \t]*$")
NEXT_HEADING_PATTERN = re.compile(r"(?m)^#{1,6}[ \t]+")
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
ARCHITECTURE_LINK_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/home-assistant/architecture(?:[/?#][^\s)\]>]*)?",
    re.IGNORECASE,
)
CORE_REFERENCE_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/home-assistant/core/(?:pull|issues)/(\d+)"
    r"|(?<![\w/])#(\d+)\b",
    re.IGNORECASE,
)
COMPONENT_PATH_PATTERN = re.compile(r"^homeassistant/components/([^/]+)/")
COUNT_QUERY = """
query CountMergedPullRequests($searchQuery: String!) {
  search(query: $searchQuery, type: ISSUE, first: 1) { issueCount }
  rateLimit { cost remaining resetAt }
}
"""

console = Console()


def load_cached_prs(cache_dir: Path = RAW_ROOT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state = json.loads((cache_dir / "state.json").read_text(encoding="utf-8"))
    records: dict[int, dict[str, Any]] = {}
    pages_path = cache_dir / "pages.jsonl"
    with pages_path.open("r", encoding="utf-8") as pages_file:
        for line_number, line in enumerate(pages_file, start=1):
            if not line.strip():
                continue
            try:
                page = json.loads(line)
                for record in page.get("pull_requests", []):
                    records.setdefault(int(record["number"]), record)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid PR JSONL data at line {line_number}.") from error
    return [records[number] for number in sorted(records)], state


def human_reviewers(pull_request: dict[str, Any]) -> dict[str, str]:
    author_login = ((pull_request.get("author") or {}).get("login") or "").casefold()
    reviewers: dict[str, str] = {}
    for review in (pull_request.get("reviews") or {}).get("nodes", []):
        actor = review.get("author") or {}
        login = actor.get("login")
        if not login or actor.get("__typename") == "Bot" or login.casefold() == author_login:
            continue
        if not review.get("submittedAt") or review.get("state") == "PENDING":
            continue
        reviewers.setdefault(login.casefold(), login)
    return reviewers


def approved_human_reviewers(pull_request: dict[str, Any]) -> set[str]:
    author_login = ((pull_request.get("author") or {}).get("login") or "").casefold()
    approved: set[str] = set()
    for review in (pull_request.get("reviews") or {}).get("nodes", []):
        actor = review.get("author") or {}
        login = actor.get("login")
        if (
            login
            and actor.get("__typename") != "Bot"
            and login.casefold() != author_login
            and review.get("state") == "APPROVED"
        ):
            approved.add(login.casefold())
    return approved


def has_nonempty_breaking_section(body: str) -> bool:
    heading = BREAKING_HEADING_PATTERN.search(body)
    if heading is None:
        return False
    section = body[heading.end() :]
    next_heading = NEXT_HEADING_PATTERN.search(section)
    if next_heading:
        section = section[: next_heading.start()]
    section = HTML_COMMENT_PATTERN.sub("", section)
    return bool(section.strip())


def parse_manifest_owners(components_root: Path) -> dict[str, set[str]]:
    owners_by_integration: dict[str, set[str]] = {}
    if not components_root.is_dir():
        return owners_by_integration
    for manifest_path in components_root.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_owners = manifest.get("codeowners", []) if isinstance(manifest, dict) else []
        if not isinstance(raw_owners, list):
            raw_owners = []
        owners_by_integration[manifest_path.parent.name] = {
            owner.strip().removeprefix("@").casefold()
            for owner in raw_owners
            if isinstance(owner, str) and owner.strip()
        }
    return owners_by_integration


def touched_integrations(pull_request: dict[str, Any]) -> set[str]:
    integrations: set[str] = set()
    for changed_file in (pull_request.get("files") or {}).get("nodes", []):
        path = changed_file.get("path") or ""
        match = COMPONENT_PATH_PATTERN.match(path)
        if match:
            integrations.add(match.group(1))
    return integrations


def baseline_strength(
    pull_requests: list[dict[str, Any]], owners_by_integration: dict[str, set[str]]
) -> dict[str, Any]:
    pair_totals: Counter[str] = Counter()
    pair_matches: Counter[str] = Counter()
    prs_by_bucket: dict[str, set[int]] = defaultdict(set)
    matched_prs_by_bucket: dict[str, set[int]] = defaultdict(set)
    touched_prs: set[int] = set()
    matched_prs: set[int] = set()

    for pull_request in pull_requests:
        number = int(pull_request["number"])
        integrations = touched_integrations(pull_request)
        if not integrations:
            continue
        touched_prs.add(number)
        approved = approved_human_reviewers(pull_request)
        for integration in integrations:
            owners = owners_by_integration.get(integration, set())
            bucket = "0" if not owners else ("1" if len(owners) == 1 else "2+")
            pair_totals[bucket] += 1
            prs_by_bucket[bucket].add(number)
            if owners & approved:
                pair_matches[bucket] += 1
                matched_prs_by_bucket[bucket].add(number)
                matched_prs.add(number)

    def fraction(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    buckets: dict[str, Any] = {}
    for bucket in ("0", "1", "2+"):
        buckets[bucket] = {
            "pr_integration_pairs": pair_totals[bucket],
            "pairs_with_matching_approved_reviewer": pair_matches[bucket],
            "pair_fraction": fraction(pair_matches[bucket], pair_totals[bucket]),
            "unique_prs_touching_bucket": len(prs_by_bucket[bucket]),
            "unique_prs_with_match_in_bucket": len(matched_prs_by_bucket[bucket]),
        }
    return {
        "unit_of_analysis": "unique PR-integration pairs for the split; unique PRs overall",
        "overall_unique_prs_touching_integrations": len(touched_prs),
        "overall_unique_prs_with_matching_approved_reviewer": len(matched_prs),
        "overall_pr_fraction": fraction(len(matched_prs), len(touched_prs)),
        "by_integration_codeowner_count": buckets,
    }


def fetch_search_count(cutoff: str) -> dict[str, Any]:
    token = settings.GITHUB_TOKEN
    if not token:
        return {"error": "GITHUB_TOKEN is not configured; search count was not queried."}
    search_query = f"repo:home-assistant/core is:pr is:merged merged:>={cutoff}"
    try:
        with httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=60.0,
        ) as client:
            response = client.post(
                GRAPHQL_URL,
                json={"query": COUNT_QUERY, "variables": {"searchQuery": search_query}},
            )
        if response.status_code != 200:
            return {"error": f"GitHub GraphQL returned HTTP {response.status_code}."}
        payload = response.json()
        if payload.get("errors"):
            messages = [str(error.get("message", "GraphQL error")) for error in payload["errors"]]
            return {
                "error": "; ".join(message.replace(token, "[REDACTED]") for message in messages)
            }
        data = payload["data"]
        return {
            "query": search_query,
            "search_count": data["search"]["issueCount"],
            "rate_limit": data.get("rateLimit"),
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        return {"error": str(error).replace(token, "[REDACTED]")}


def calculate_survey(
    pull_requests: list[dict[str, Any]],
    state: dict[str, Any],
    template_text: str | None,
    owners_by_integration: dict[str, set[str]],
    search_count_result: dict[str, Any],
) -> dict[str, Any]:
    label_counts: Counter[str] = Counter()
    reviewers_by_pr: dict[int, dict[str, str]] = {}
    reviewer_prs: dict[str, set[int]] = defaultdict(set)
    bot_authored = 0
    files_truncated = 0
    zero_human_reviews = 0
    review_lists_truncated = 0
    changed_file_counts: list[int] = []
    human_reviewer_counts: list[int] = []
    checkbox_prs: list[int] = []
    nonempty_breaking_section_prs: list[int] = []
    architecture_link_prs: list[int] = []
    issue_or_pr_reference_prs: list[int] = []

    for pull_request in pull_requests:
        number = int(pull_request["number"])
        author = pull_request.get("author") or {}
        body = pull_request.get("body") or ""
        if author.get("__typename") == "Bot":
            bot_authored += 1
        if pull_request.get("files_truncated"):
            files_truncated += 1
        if int((pull_request.get("reviews") or {}).get("totalCount") or 0) > len(
            (pull_request.get("reviews") or {}).get("nodes", [])
        ):
            review_lists_truncated += 1
        changed_file_counts.append(int(pull_request.get("changedFiles") or 0))

        reviewers = human_reviewers(pull_request)
        reviewers_by_pr[number] = reviewers
        human_reviewer_counts.append(len(reviewers))
        if not reviewers:
            zero_human_reviews += 1
        for reviewer_key in reviewers:
            reviewer_prs[reviewer_key].add(number)

        label_counts.update(
            label for label in pull_request.get("labels", []) if isinstance(label, str) and label
        )
        if BREAKING_CHECKBOX_PATTERN.search(body):
            checkbox_prs.append(number)
        if has_nonempty_breaking_section(body):
            nonempty_breaking_section_prs.append(number)
        if ARCHITECTURE_LINK_PATTERN.search(body):
            architecture_link_prs.append(number)
        references = {
            first or second
            for first, second in CORE_REFERENCE_PATTERN.findall(body)
            if (first or second) != str(number)
        }
        if references:
            issue_or_pr_reference_prs.append(number)

    label_top_30 = [
        {"name": name, "count": count}
        for name, count in sorted(label_counts.items(), key=lambda item: (-item[1], item[0].casefold()))[:30]
    ]
    top_reviewers = [
        {"login": login, "prs_reviewed": len(numbers)}
        for login, numbers in sorted(
            reviewer_prs.items(), key=lambda item: (-len(item[1]), item[0])
        )[:15]
    ]
    cutoff = datetime.fromisoformat(state["cutoff"].replace("Z", "+00:00"))
    search_count = search_count_result.get("search_count")
    cache_count = len(pull_requests)
    reconciliation = {
        "cutoff_utc": cutoff.isoformat(),
        "cache_prs_in_window": cache_count,
        "github_search_count": search_count,
        "gap_cache_minus_search": cache_count - search_count if search_count is not None else None,
        "missing_by_count": max(0, search_count - cache_count) if search_count is not None else None,
        "method_note": "The search qualifier uses the saved UTC cutoff; GitHub Search indexing may lag, so its total can differ from the cursor fetch count.",
    }
    if search_count_result.get("error"):
        reconciliation["error"] = search_count_result["error"]

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": {"days": state["days"], "cutoff_utc": cutoff.isoformat()},
        "reconciliation": reconciliation,
        "overview": {
            "total_prs": len(pull_requests),
            "bot_authored_prs": bot_authored,
            "files_truncated_prs": files_truncated,
            "zero_human_review_prs": zero_human_reviews,
            "median_changed_files": median(changed_file_counts) if changed_file_counts else None,
            "median_distinct_human_reviewers_per_pr": median(human_reviewer_counts)
            if human_reviewer_counts
            else None,
            "distinct_human_reviewers_total": len(reviewer_prs),
            "top_15_reviewers_by_pr_count": top_reviewers,
            "prs_with_review_lists_truncated_at_100": review_lists_truncated,
        },
        "labels": {"top_30": label_top_30},
        "breaking_change_signals": {
            "checked_checkbox_pattern": BREAKING_CHECKBOX_PATTERN.pattern,
            "checked_breaking_change_checkbox_prs": len(checkbox_prs),
            "checked_breaking_change_checkbox_numbers": checkbox_prs,
            "nonempty_heading_pattern": BREAKING_HEADING_PATTERN.pattern,
            "nonempty_breaking_heading_prs": len(nonempty_breaking_section_prs),
            "nonempty_breaking_heading_numbers": nonempty_breaking_section_prs,
        },
        "references": {
            "architecture_repo_url_pattern": ARCHITECTURE_LINK_PATTERN.pattern,
            "prs_linking_home_assistant_architecture": len(architecture_link_prs),
            "architecture_link_pr_numbers": architecture_link_prs,
            "other_pr_or_issue_number_pattern": CORE_REFERENCE_PATTERN.pattern,
            "prs_referencing_another_pr_or_issue": len(issue_or_pr_reference_prs),
            "referencing_pr_numbers": issue_or_pr_reference_prs,
        },
        "baseline_strength": baseline_strength(pull_requests, owners_by_integration),
        "analysis_limits": [
            "The fetch stores at most the first 100 review nodes per PR; reviewer totals, medians, and baseline matches may undercount PRs with more reviews.",
            "The fetch stores at most the first 100 changed-file nodes per PR; integration touch and owner-baseline analysis may omit files on PRs marked files_truncated.",
        ],
        "pull_request_template_path": TEMPLATE_PATH.relative_to(ROOT).as_posix(),
        "pull_request_template_text": template_text,
    }


def print_report(report: dict[str, Any], output_path: Path) -> None:
    overview = report["overview"]
    table = Table(title="Home Assistant PR Survey")
    table.add_column("Signal")
    table.add_column("Result", justify="right")
    rows = [
        ("Total PRs", overview["total_prs"]),
        ("Bot-authored PRs", overview["bot_authored_prs"]),
        ("Files truncated", overview["files_truncated_prs"]),
        ("Zero human reviewers", overview["zero_human_review_prs"]),
        ("Median changed files", overview["median_changed_files"]),
        ("Median distinct human reviewers", overview["median_distinct_human_reviewers_per_pr"]),
        ("Distinct human reviewers", overview["distinct_human_reviewers_total"]),
        ("Checked breaking-change box", report["breaking_change_signals"]["checked_breaking_change_checkbox_prs"]),
        ("Non-empty breaking section", report["breaking_change_signals"]["nonempty_breaking_heading_prs"]),
        ("Architecture links", report["references"]["prs_linking_home_assistant_architecture"]),
        ("PR/issue references", report["references"]["prs_referencing_another_pr_or_issue"]),
    ]
    baseline = report["baseline_strength"]
    if baseline["overall_pr_fraction"] is not None:
        rows.append(("Reviewer-owner baseline", f"{baseline['overall_pr_fraction']:.1%}"))
    reconciliation = report["reconciliation"]
    if reconciliation.get("github_search_count") is not None:
        rows.append(("Search count / cache gap", f"{reconciliation['github_search_count']} / {reconciliation['gap_cache_minus_search']:+}"))
    for label, value in rows:
        table.add_row(label, str(value))
    console.print(table)

    labels_table = Table(title="30 Most Common Labels")
    labels_table.add_column("Label")
    labels_table.add_column("PRs", justify="right")
    for label in report["labels"]["top_30"]:
        labels_table.add_row(label["name"], str(label["count"]))
    console.print(labels_table)

    template_text = report.get("pull_request_template_text")
    if template_text is not None:
        console.print(Panel(Text(template_text), title=".github/PULL_REQUEST_TEMPLATE.md"))
    console.print(f"Survey saved to {output_path}")


def main() -> int:
    pull_requests, state = load_cached_prs()
    template_text = (
        TEMPLATE_PATH.read_text(encoding="utf-8") if TEMPLATE_PATH.is_file() else None
    )
    owners_by_integration = parse_manifest_owners(CORE_ROOT / "homeassistant" / "components")
    search_count_result = fetch_search_count(state["cutoff"].replace("+00:00", "Z"))
    report = calculate_survey(
        pull_requests,
        state,
        template_text,
        owners_by_integration,
        search_count_result,
    )
    output_path = RAW_ROOT / "survey.json"
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print_report(report, output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())