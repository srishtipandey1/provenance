from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from provenance.config import settings


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "data" / "raw" / "prs"
GRAPHQL_URL = "https://api.github.com/graphql"
PAGE_SIZE = 50
FILE_PAGE_SIZE = 100
REVIEW_PAGE_SIZE = 100
LOW_REMAINING_POINTS = 100
MAX_RETRIES = 5
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
GITHUB_TOKEN_PATTERN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")

PULL_REQUESTS_QUERY = """
query MergedPullRequests($first: Int!, $after: String) {
  repository(owner: "home-assistant", name: "core") {
    pullRequests(
      first: $first
      after: $after
      states: MERGED
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        author { login __typename }
        mergedBy { login __typename }
        createdAt
        updatedAt
        mergedAt
        additions
        deletions
        changedFiles
        labels(first: 20) { nodes { name } }
        files(first: 100) {
          totalCount
          nodes { path additions deletions changeType }
        }
        reviews(first: 100) {
          totalCount
          nodes { author { login __typename } state submittedAt }
        }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""


class GitHubFetchError(RuntimeError):
    """Raised when GitHub GraphQL cannot provide a page."""


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def redact_body_text(value: str) -> str:
    without_emails = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
    return GITHUB_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", without_emails)[:6000]


def normalize_pull_request(node: dict[str, Any]) -> dict[str, Any]:
    author = node.get("author") or {}
    merged_by = node.get("mergedBy") or {}
    labels = node.get("labels") or {}
    files = node.get("files") or {}
    reviews = node.get("reviews") or {}
    file_nodes = files.get("nodes") or []
    review_nodes = reviews.get("nodes") or []
    file_total = int(files.get("totalCount") or 0)
    review_total = int(reviews.get("totalCount") or 0)

    return {
        "number": int(node["number"]),
        "title": node.get("title") or "",
        "body": redact_body_text(node.get("body") or ""),
        "author": {
            "login": author.get("login"),
            "__typename": author.get("__typename"),
        },
        "mergedBy": (
            {"login": merged_by.get("login"), "__typename": merged_by.get("__typename")}
            if merged_by
            else None
        ),
        "createdAt": node.get("createdAt"),
        "updatedAt": node.get("updatedAt"),
        "mergedAt": node.get("mergedAt"),
        "additions": int(node.get("additions") or 0),
        "deletions": int(node.get("deletions") or 0),
        "changedFiles": int(node.get("changedFiles") or 0),
        "labels": [label["name"] for label in (labels.get("nodes") or []) if label.get("name")],
        "files": {
            "totalCount": file_total,
            "nodes": [
                {
                    "path": item.get("path"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "changeType": item.get("changeType"),
                }
                for item in file_nodes
            ],
        },
        "files_truncated": file_total > len(file_nodes),
        "reviews": {
            "totalCount": review_total,
            "nodes": [
                {
                    "author": {
                        "login": (item.get("author") or {}).get("login"),
                        "__typename": (item.get("author") or {}).get("__typename"),
                    },
                    "state": item.get("state"),
                    "submittedAt": item.get("submittedAt"),
                }
                for item in review_nodes
            ],
        },
    }


def filter_page_nodes(
    nodes: list[dict[str, Any]], cutoff: datetime
) -> tuple[list[dict[str, Any]], bool]:
    """Return merged PRs in-window and whether updatedAt reached the cutoff."""
    cutoff = cutoff.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    selected: list[dict[str, Any]] = []
    for node in nodes:
        updated_at = node.get("updatedAt")
        if updated_at and parse_datetime(updated_at) < cutoff:
            return selected, True
        merged_at = node.get("mergedAt")
        if not merged_at:
            continue
        merged_time = parse_datetime(merged_at)
        if cutoff <= merged_time <= now:
            selected.append(normalize_pull_request(node))
    return selected, False


def cutoff_for_run(cache_dir: Path, days: int) -> datetime:
    state_path = cache_dir / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if int(state.get("days", days)) != days:
            raise ValueError(
                f"Existing cache uses --days {state.get('days')}; use that value to resume."
            )
        return parse_datetime(state["cutoff"])
    return datetime.now(timezone.utc) - timedelta(days=days)


class RawPageStore:
    def __init__(self, directory: Path, days: int, cutoff: datetime):
        self.directory = directory
        self.pages_path = directory / "pages.jsonl"
        self.state_path = directory / "state.json"
        directory.mkdir(parents=True, exist_ok=True)
        cutoff_text = cutoff.astimezone(timezone.utc).isoformat()
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if int(self.state.get("days", days)) != days:
                raise ValueError(
                    f"Existing cache uses --days {self.state.get('days')}; use that value to resume."
                )
            if self.state.get("cutoff") != cutoff_text:
                raise ValueError("Existing cache cutoff does not match; refusing to mix fetch windows.")
        else:
            self.state = {
                "days": days,
                "cutoff": cutoff_text,
                "cursor": None,
                "page_size": PAGE_SIZE,
                "pages_completed": 0,
                "prs_seen": 0,
                "complete": False,
                "stop_reason": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_state()

        self.records = self._read_records()
        self.state["prs_seen"] = len(self.records)
        self.state["pages_completed"] = max(
            int(self.state.get("pages_completed", 0)), self._count_page_lines()
        )
        self._save_state()

    @property
    def seen_numbers(self) -> set[int]:
        return set(self.records)

    def _read_records(self) -> dict[int, dict[str, Any]]:
        records: dict[int, dict[str, Any]] = {}
        if not self.pages_path.exists():
            return records
        with self.pages_path.open("r", encoding="utf-8") as page_file:
            for line_number, line in enumerate(page_file, start=1):
                if not line.strip():
                    continue
                try:
                    page = json.loads(line)
                    for record in page.get("pull_requests", []):
                        records.setdefault(int(record["number"]), record)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"Invalid PR cache JSONL at line {line_number}.") from error
        return records

    def _count_page_lines(self) -> int:
        if not self.pages_path.exists():
            return 0
        with self.pages_path.open("r", encoding="utf-8") as page_file:
            return sum(1 for line in page_file if line.strip())

    def _save_state(self) -> None:
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary_path = self.state_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, self.state_path)

    def record_page(
        self,
        *,
        after: str | None,
        page_info: dict[str, Any],
        rate_limit: dict[str, Any],
        pull_requests: list[dict[str, Any]],
        page_size: int,
        complete: bool,
        stop_reason: str | None,
    ) -> tuple[int, int]:
        new_records: list[dict[str, Any]] = []
        for record in pull_requests:
            number = int(record["number"])
            if number not in self.records:
                self.records[number] = record
                new_records.append(record)

        page_entry = {
            "requested_after": after,
            "end_cursor": page_info.get("endCursor"),
            "has_next_page": bool(page_info.get("hasNextPage")),
            "page_size": page_size,
            "rate_limit": rate_limit,
            "pull_requests": new_records,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.pages_path.open("a", encoding="utf-8", newline="\n") as page_file:
            page_file.write(json.dumps(page_entry, ensure_ascii=False) + "\n")
            page_file.flush()
            os.fsync(page_file.fileno())

        self.state.update(
            {
                "cursor": page_info.get("endCursor"),
                "page_size": page_size,
                "pages_completed": int(self.state.get("pages_completed", 0)) + 1,
                "prs_seen": len(self.records),
                "complete": complete,
                "stop_reason": stop_reason,
                "last_rate_limit": rate_limit,
            }
        )
        self._save_state()
        return len(new_records), len(self.records)

    def set_trial_limit_reached(self) -> None:
        if not self.state.get("complete"):
            self.state["stop_reason"] = "trial_limit"
            self.state["prs_seen"] = len(self.records)
            self._save_state()

    def cached_records(self) -> list[dict[str, Any]]:
        return [self.records[number] for number in sorted(self.records)]


class GitHubGraphQLClient:
    def __init__(self, token: str, timeout_seconds: float = 90.0):
        self.token = token
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self.client.close()

    def _backoff(self, retry_number: int, minimum_seconds: float = 0) -> None:
        delay = max(minimum_seconds, min(2**retry_number, 60))
        print(f"Retrying GitHub GraphQL after {delay:.0f}s.", flush=True)
        time.sleep(delay)

    def fetch_page(self, after: str | None, page_size: int) -> tuple[dict[str, Any], int, float]:
        current_size = page_size
        retries = 0
        while True:
            try:
                response = self.client.post(
                    GRAPHQL_URL,
                    json={
                        "query": PULL_REQUESTS_QUERY,
                        "variables": {"first": current_size, "after": after},
                    },
                )
            except httpx.TimeoutException as error:
                if current_size > 1:
                    current_size = max(1, current_size // 2)
                    retries = 0
                    print(f"Request timed out; reducing page size to {current_size}.", flush=True)
                    continue
                if retries >= MAX_RETRIES:
                    raise GitHubFetchError("GitHub GraphQL timed out after retries.") from error
                retries += 1
                self._backoff(retries)
                continue
            except httpx.HTTPError as error:
                if retries >= MAX_RETRIES:
                    raise GitHubFetchError("GitHub GraphQL transport failed after retries.") from error
                retries += 1
                self._backoff(retries)
                continue

            if response.status_code in {499, 502, 503, 504}:
                if retries >= MAX_RETRIES:
                    raise GitHubFetchError(
                        f"GitHub GraphQL returned HTTP {response.status_code} after retries."
                    )
                retries += 1
                self._backoff(retries)
                continue

            try:
                payload = response.json()
            except ValueError as error:
                raise GitHubFetchError(
                    f"GitHub GraphQL returned non-JSON HTTP {response.status_code}."
                ) from error

            errors = payload.get("errors") or []
            messages = [str(item.get("message", "GraphQL error")) for item in errors]
            if payload.get("message"):
                messages.append(str(payload["message"]))
            messages = [message.replace(self.token, "[REDACTED]") for message in messages]
            combined_message = " ".join(messages).casefold()
            node_limit = any(
                phrase in combined_message
                for phrase in ("node limit", "too many nodes", "maximum number of nodes")
            )
            if node_limit and current_size > 1:
                current_size = max(1, current_size // 2)
                retries = 0
                print(f"GraphQL node limit; reducing page size to {current_size}.", flush=True)
                continue

            secondary_limit = any(
                phrase in combined_message
                for phrase in ("secondary rate limit", "abuse detection", "temporarily blocked")
            ) or (response.status_code == 403 and "rate limit" in combined_message)
            if secondary_limit:
                if retries >= MAX_RETRIES:
                    raise GitHubFetchError("GitHub secondary rate limit persisted after retries.")
                retries += 1
                retry_after = response.headers.get("Retry-After")
                try:
                    minimum = float(retry_after) if retry_after else 0
                except ValueError:
                    minimum = 0
                self._backoff(retries, minimum)
                continue

            rate_limit = payload.get("data", {}).get("rateLimit") or {}
            if errors:
                if "rate limit" in combined_message:
                    reset_at_value = rate_limit.get("resetAt")
                    if reset_at_value:
                        reset_at = parse_datetime(reset_at_value)
                    else:
                        reset_epoch = response.headers.get("X-RateLimit-Reset")
                        reset_at = (
                            datetime.fromtimestamp(float(reset_epoch), timezone.utc)
                            if reset_epoch
                            else None
                        )
                    if reset_at:
                        delay = max(0, (reset_at - datetime.now(timezone.utc)).total_seconds()) + 1
                        print(f"Primary rate limit reached; waiting {delay:.0f}s for reset.", flush=True)
                        time.sleep(delay)
                        continue
                    if retries < MAX_RETRIES:
                        retries += 1
                        self._backoff(retries)
                        continue
                if errors:
                    raise GitHubFetchError("GitHub GraphQL error: " + "; ".join(messages))
            if response.status_code != 200:
                raise GitHubFetchError(f"GitHub GraphQL returned HTTP {response.status_code}.")
            if not payload.get("data", {}).get("repository"):
                raise GitHubFetchError("GitHub GraphQL response did not include the repository.")
            return payload["data"], current_size, response.elapsed.total_seconds()


def sleep_for_rate_limit(rate_limit: dict[str, Any]) -> None:
    remaining = rate_limit.get("remaining")
    reset_at = rate_limit.get("resetAt")
    if remaining is None or int(remaining) >= LOW_REMAINING_POINTS or not reset_at:
        return
    delay = max(0, (parse_datetime(reset_at) - datetime.now(timezone.utc)).total_seconds()) + 1
    if delay > 0:
        print(f"Only {remaining} points remain; waiting {delay:.0f}s for rate-limit reset.", flush=True)
        time.sleep(delay)


def fetch_merged_prs(days: int = 180, limit: int | None = None) -> dict[str, Any]:
    if days < 1:
        raise ValueError("--days must be a positive integer")
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer")
    token = settings.GITHUB_TOKEN
    if not token:
        raise GitHubFetchError("GITHUB_TOKEN is not configured; no GitHub requests were made.")

    cutoff = cutoff_for_run(RAW_ROOT, days)
    store = RawPageStore(RAW_ROOT, days, cutoff)
    initial_count = len(store.records)
    page_costs: list[dict[str, Any]] = []
    if store.state.get("complete"):
        print("The requested fetch window is already complete; using the cached records.", flush=True)
        return {
            "store": store,
            "records_added": 0,
            "page_costs": [],
            "cutoff": cutoff,
        }

    client = GitHubGraphQLClient(token)
    try:
        while True:
            current_count = len(store.records)
            if limit is not None and current_count - initial_count >= limit:
                store.set_trial_limit_reached()
                break

            page_size = int(store.state.get("page_size") or PAGE_SIZE)
            if limit is not None:
                page_size = min(page_size, limit - (current_count - initial_count))
            after = store.state.get("cursor")
            data, actual_page_size, elapsed = client.fetch_page(after, page_size)
            connection = data["repository"]["pullRequests"]
            page_info = connection["pageInfo"]
            selected, reached_cutoff = filter_page_nodes(connection["nodes"], cutoff)
            rate_limit = data.get("rateLimit") or {}
            complete = reached_cutoff or not page_info.get("hasNextPage")
            stop_reason = "cutoff" if reached_cutoff else ("end_of_connection" if complete else None)
            added, total = store.record_page(
                after=after,
                page_info=page_info,
                rate_limit=rate_limit,
                pull_requests=selected,
                page_size=actual_page_size,
                complete=complete,
                stop_reason=stop_reason,
            )
            page_cost = {
                "page": int(store.state["pages_completed"]),
                "cost": rate_limit.get("cost"),
                "remaining": rate_limit.get("remaining"),
                "resetAt": rate_limit.get("resetAt"),
                "seconds": round(elapsed, 3),
                "page_size": actual_page_size,
            }
            page_costs.append(page_cost)
            print(
                f"Page {page_cost['page']}: {added} new merged PRs; total {total}; "
                f"page size {actual_page_size}; rateLimit cost {page_cost['cost']}; "
                f"remaining {page_cost['remaining']}; {page_cost['seconds']}s.",
                flush=True,
            )

            if complete:
                break
            if limit is not None and len(store.records) - initial_count >= limit:
                store.set_trial_limit_reached()
                break
            sleep_for_rate_limit(rate_limit)
    finally:
        client.close()

    return {
        "store": store,
        "records_added": len(store.records) - initial_count,
        "page_costs": page_costs,
        "cutoff": cutoff,
    }


def sample_record(record: dict[str, Any]) -> dict[str, Any]:
    sample = dict(record)
    sample["body"] = record.get("body", "")[:200]
    sample["files"] = {
        "totalCount": record["files"]["totalCount"],
        "nodes_preview": record["files"]["nodes"][:3],
    }
    sample["reviews"] = {
        "totalCount": record["reviews"]["totalCount"],
        "nodes_preview": record["reviews"]["nodes"][:3],
    }
    return sample


def type_of_change_excerpt(record: dict[str, Any], limit: int = 400) -> str | None:
    body = record.get("body", "")
    section = re.search(r"(?im)^#{1,6}\s+Type of change\s*$", body)
    if section is None:
        return None
    return body[section.start() : section.start() + limit]


def print_trial_summary(result: dict[str, Any]) -> None:
    store: RawPageStore = result["store"]
    all_records = store.cached_records()
    section_record = next((record for record in all_records if type_of_change_excerpt(record)), None)
    records = all_records[:2]
    if section_record and all(record["number"] != section_record["number"] for record in records):
        records[-1] = section_record
    print("\nStage A sample PR records (body shortened to 200 characters):", flush=True)
    for record in records:
        print(json.dumps(sample_record(record), ensure_ascii=False, indent=2), flush=True)
    if section_record:
        print(
            f"\nRaw body Type of change excerpt for PR #{section_record['number']} "
            "(up to 400 characters from section heading):",
            flush=True,
        )
        print(type_of_change_excerpt(section_record), flush=True)
    else:
        print("\nNo Type of change heading found in the 100 trial PR bodies.", flush=True)
    print("\nRate-limit cost per page:", flush=True)
    for page in result["page_costs"]:
        print(
            f"page {page['page']}: cost={page['cost']}, remaining={page['remaining']}",
            flush=True,
        )
    print(
        f"Trial added {result['records_added']} PRs; cache has {len(store.records)} unique PRs. "
        f"Cursor and counters are in {store.state_path}.",
        flush=True,
    )