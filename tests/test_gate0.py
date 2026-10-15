from pathlib import Path
from unittest.mock import patch

from scripts.gate0 import codeowners_pattern_matches, parse_adr


def test_codeowners_directory_rule_matches_descendants() -> None:
    assert codeowners_pattern_matches(
        "homeassistant/components/demo",
        "homeassistant/components/demo/manifest.json",
    )
    assert not codeowners_pattern_matches(
        "homeassistant/components/demo",
        "homeassistant/components/demo2/manifest.json",
    )


def test_parse_markdown_adr_status_and_supersession() -> None:
    adr_path = Path("0002-example.md")
    contents = (
        "# 2. Example decision\n\n## Status\n\n"
        "Superseded by [ADR-0020](./0020-example.md)\n"
    )
    with patch.object(Path, "read_text", return_value=contents):
        parsed = parse_adr(adr_path)

    assert parsed["id"] == "2"
    assert parsed["title"] == "2. Example decision"
    assert parsed["status"] == "Superseded by [ADR-0020](./0020-example.md)"
    assert parsed["superseded_by"] == ["20"]