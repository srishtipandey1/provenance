import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from provenance.ingest.github_prs import (
    GitHubGraphQLClient,
    RawPageStore,
    filter_page_nodes,
    normalize_pull_request,
    type_of_change_excerpt,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "github_prs_cutoff.json"


def test_cutoff_filters_merged_at_and_stops_on_old_updated_at() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)

    selected, reached_cutoff = filter_page_nodes(fixture["nodes"], cutoff)

    assert [record["number"] for record in selected] == [101]
    assert reached_cutoff is True
    assert selected[0]["body"].startswith("## Type of change")
    assert "[x] Breaking change" in selected[0]["body"]
    assert "public@example.com" not in selected[0]["body"]
    assert "bodyText" not in selected[0]
    assert type_of_change_excerpt(selected[0]).startswith("## Type of change")
    assert selected[0]["mergedBy"] == {"login": "fixture-bot", "__typename": "Bot"}
    assert selected[0]["reviews"]["nodes"][0]["author"] == {
        "login": "review-bot",
        "__typename": "Bot",
    }


def test_body_is_capped_and_token_shaped_text_is_redacted() -> None:
    token = "ghp_" + "x" * 36
    record = normalize_pull_request(
        {
            "number": 105,
            "body": f"## Type of change\n- [x] Breaking change\n{token}\n" + ("x" * 6100),
            "mergedBy": {"login": "merge-bot", "__typename": "Bot"},
            "reviews": {"nodes": [{"author": {"login": "reviewer", "__typename": "User"}}]},
        }
    )

    assert len(record["body"]) == 6000
    assert token not in record["body"]
    assert "[REDACTED_TOKEN]" in record["body"]
    assert record["mergedBy"]["__typename"] == "Bot"
    assert record["reviews"]["nodes"][0]["author"]["__typename"] == "User"


def test_resume_continues_cursor_and_deduplicates_replayed_prs() -> None:
    cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary_directory:
        cache_dir = Path(temporary_directory)
        first_store = RawPageStore(cache_dir, days=180, cutoff=cutoff)
        first_store.record_page(
            after=None,
            page_info={"endCursor": "cursor-1", "hasNextPage": True},
            rate_limit={"cost": 12, "remaining": 4000, "resetAt": None},
            pull_requests=[{"number": 101}],
            page_size=50,
            complete=False,
            stop_reason=None,
        )

        resumed_store = RawPageStore(cache_dir, days=180, cutoff=cutoff)
        assert resumed_store.state["cursor"] == "cursor-1"
        assert resumed_store.state["prs_seen"] == 1

        resumed_store.record_page(
            after="cursor-1",
            page_info={"endCursor": "cursor-2", "hasNextPage": True},
            rate_limit={"cost": 10, "remaining": 3990, "resetAt": None},
            pull_requests=[{"number": 101}, {"number": 102}],
            page_size=50,
            complete=False,
            stop_reason=None,
        )

        cached = resumed_store.cached_records()
        page_lines = [
            json.loads(line)
            for line in (cache_dir / "pages.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    assert [record["number"] for record in cached] == [101, 102]
    assert [record["number"] for record in page_lines[1]["pull_requests"]] == [102]
    assert resumed_store.state["cursor"] == "cursor-2"
    assert resumed_store.state["prs_seen"] == 2


def test_gateway_499_retries_before_parsing_response() -> None:
    retryable_response = Mock(status_code=499)
    success_payload = {
        "data": {
            "repository": {"pullRequests": {"nodes": [], "pageInfo": {}}},
            "rateLimit": {"cost": 2, "remaining": 1000, "resetAt": None},
        }
    }
    successful_response = Mock(
        status_code=200,
        elapsed=SimpleNamespace(total_seconds=lambda: 0.1),
        headers={},
    )
    successful_response.json.return_value = success_payload
    client = GitHubGraphQLClient("test-token")

    with patch.object(client.client, "post", side_effect=[retryable_response, successful_response]) as post:
        with patch.object(client, "_backoff") as backoff:
            data, page_size, elapsed = client.fetch_page(after=None, page_size=50)
    client.close()

    assert data == success_payload["data"]
    assert page_size == 50
    assert elapsed == 0.1
    assert post.call_count == 2
    backoff.assert_called_once_with(1)