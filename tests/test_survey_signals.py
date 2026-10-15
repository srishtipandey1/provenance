from scripts.survey_signals import (
    BREAKING_CHECKBOX_PATTERN,
    baseline_strength,
    has_nonempty_breaking_section,
    human_reviewers,
)


def test_breaking_change_signals_distinguish_checkbox_and_section_text() -> None:
    body = (
        "## Breaking change\n<!-- template guidance only -->\n\n"
        "This changes how existing sensor entities are restored.\n\n"
        "## Type of change\n- [x] Breaking change (fix/feature causing existing functionality to break)"
    )

    assert BREAKING_CHECKBOX_PATTERN.search(body)
    assert has_nonempty_breaking_section(body)
    assert not has_nonempty_breaking_section(
        "## Breaking change\n<!-- template instructions -->\n\n## Proposed change\nText"
    )


def test_human_reviewers_excludes_bots_author_and_duplicate_reviews() -> None:
    pull_request = {
        "author": {"login": "submitter", "__typename": "User"},
        "reviews": {
            "nodes": [
                {
                    "author": {"login": "reviewer", "__typename": "User"},
                    "state": "COMMENTED",
                    "submittedAt": "2026-01-01T00:00:00Z",
                },
                {
                    "author": {"login": "reviewer", "__typename": "User"},
                    "state": "APPROVED",
                    "submittedAt": "2026-01-01T01:00:00Z",
                },
                {
                    "author": {"login": "automation", "__typename": "Bot"},
                    "state": "APPROVED",
                    "submittedAt": "2026-01-01T02:00:00Z",
                },
                {
                    "author": {"login": "submitter", "__typename": "User"},
                    "state": "COMMENTED",
                    "submittedAt": "2026-01-01T03:00:00Z",
                },
            ]
        },
    }

    assert human_reviewers(pull_request) == {"reviewer": "reviewer"}


def test_baseline_strength_splits_zero_one_and_multiple_codeowners() -> None:
    def pull_request(number: int, integration: str, reviewer: str) -> dict[str, object]:
        return {
            "number": number,
            "author": {"login": f"author-{number}", "__typename": "User"},
            "files": {"nodes": [{"path": f"homeassistant/components/{integration}/sensor.py"}]},
            "reviews": {
                "nodes": [
                    {
                        "author": {"login": reviewer, "__typename": "User"},
                        "state": "APPROVED",
                        "submittedAt": "2026-01-01T00:00:00Z",
                    }
                ]
            },
        }

    survey = baseline_strength(
        [
            pull_request(1, "one_owner", "owner-one"),
            pull_request(2, "two_owners", "owner-two"),
            pull_request(3, "no_owners", "reviewer-outside-owners"),
        ],
        {
            "one_owner": {"owner-one"},
            "two_owners": {"owner-two", "owner-three"},
            "no_owners": set(),
        },
    )

    assert survey["overall_unique_prs_touching_integrations"] == 3
    assert survey["overall_unique_prs_with_matching_approved_reviewer"] == 2
    assert survey["by_integration_codeowner_count"]["0"]["pair_fraction"] == 0
    assert survey["by_integration_codeowner_count"]["1"]["pair_fraction"] == 1
    assert survey["by_integration_codeowner_count"]["2+"]["pair_fraction"] == 1