from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from falkordb import FalkorDB

from provenance.config import settings
from provenance.ingest.graph_loader import DEFAULT_GRAPH
from provenance.tools.analysis import (
    blast_radius,
    bus_factor,
    criticality,
    decision_trace,
    maintainer_loss,
    module_profile,
    ownership_drift,
    suggest_reviewers,
)
from provenance.tools.registry import TOOL_REGISTRY


TOOL_FUNCTIONS = {
    "module_profile": module_profile,
    "ownership_drift": ownership_drift,
    "bus_factor": bus_factor,
    "blast_radius": blast_radius,
    "criticality": criticality,
    "suggest_reviewers": suggest_reviewers,
    "maintainer_loss": maintainer_loss,
    "decision_trace": decision_trace,
}


def _decode_json_object(value: str, *, allow_mangled: bool = False) -> tuple[dict[str, Any], bool]:
    try:
        decoded = json.loads(value)
        mangled = False
    except json.JSONDecodeError:
        if not allow_mangled:
            raise
        normalized = re.sub(
            r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:',
            r'\1"\2":',
            value,
        )

        def quote_bare_value(match: re.Match[str]) -> str:
            token = match.group(2)
            if token.casefold() in {"true", "false", "null"}:
                return match.group(0)
            return f'{match.group(1)}"{token}"{match.group(3)}'

        normalized = re.sub(
            r'(:\s*)([A-Za-z_][A-Za-z0-9_.-]*)(\s*[,}])',
            quote_bare_value,
            normalized,
        )
        decoded = json.loads(normalized)
        mangled = True
    if not isinstance(decoded, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    return decoded, mangled


def parse_tool_arguments(
    *,
    args_text: str | None,
    args_file: str | None,
    arg_values: list[str] | None,
    stdin_text: str | None,
) -> tuple[dict[str, Any], bool]:
    arg_values = arg_values or []
    modes = int(args_text is not None) + int(args_file is not None) + int(bool(arg_values))
    if modes != 1:
        raise ValueError("Choose exactly one of --args, --args-file, or --arg.")
    if args_file is not None:
        return _decode_json_object(Path(args_file).read_text(encoding="utf-8"))
    if arg_values:
        parsed: dict[str, Any] = {}
        for item in arg_values:
            key, separator, value = item.partition("=")
            if not separator or not key:
                raise ValueError(f"Invalid --arg {item!r}; expected key=value.")
            try:
                parsed[key] = json.loads(value)
            except json.JSONDecodeError:
                parsed[key] = value
        return parsed, False
    if args_text == "-":
        if stdin_text is None:
            raise ValueError("No JSON was provided on stdin.")
        return _decode_json_object(stdin_text)
    return _decode_json_object(args_text or "", allow_mangled=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a deterministic Provenance graph tool and print its evidence."
    )
    parser.add_argument("tool", choices=sorted(TOOL_REGISTRY))
    parser.add_argument("--args", help="JSON object of tool arguments, or '-' to read stdin.")
    parser.add_argument("--args-file", help="Path to a UTF-8 JSON argument file.")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        help="One tool argument as key=value; values are parsed as JSON when possible.",
    )
    parser.add_argument("--graph", default=DEFAULT_GRAPH)
    args = parser.parse_args()
    try:
        tool_args, used_fallback = parse_tool_arguments(
            args_text=args.args,
            args_file=args.args_file,
            arg_values=args.arg,
            stdin_text=sys.stdin.read() if args.args == "-" else None,
        )
        if used_fallback:
            print(
                "warning: --args used the PowerShell-mangled object fallback parser",
                file=sys.stderr,
            )
        database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
        graph = database.select_graph(args.graph)
        result = TOOL_FUNCTIONS[args.tool](graph, **tool_args)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "tool": result.tool,
                "args": result.args,
                "as_of_ts": result.as_of_ts,
                "result": result.result,
                "facts": [fact.__dict__ for fact in result.facts],
                "evidence": result.evidence,
                "warnings": result.warnings,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())