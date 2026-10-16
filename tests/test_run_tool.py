from __future__ import annotations

from pathlib import Path

from scripts.run_tool import parse_tool_arguments


FIXTURE_ARGS = Path(__file__).parent / "fixtures" / "run_tool" / "criticality.json"


def test_run_tool_argument_sources() -> None:
    assert parse_tool_arguments(
        args_text='{"top_n":2}',
        args_file=None,
        arg_values=None,
        stdin_text=None,
    ) == ({"top_n": 2}, False)
    assert parse_tool_arguments(
        args_text="{top_n:2}",
        args_file=None,
        arg_values=None,
        stdin_text=None,
    ) == ({"top_n": 2}, True)
    assert parse_tool_arguments(
        args_text=None,
        args_file=str(FIXTURE_ARGS),
        arg_values=None,
        stdin_text=None,
    ) == ({"top_n": 2}, False)
    assert parse_tool_arguments(
        args_text=None,
        args_file=None,
        arg_values=["top_n=2", "person_login=epenet", "dry_run=false"],
        stdin_text=None,
    ) == ({"top_n": 2, "person_login": "epenet", "dry_run": False}, False)
    assert parse_tool_arguments(
        args_text="-",
        args_file=None,
        arg_values=None,
        stdin_text='{"top_n":2}',
    ) == ({"top_n": 2}, False)