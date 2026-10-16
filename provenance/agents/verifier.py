from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from provenance.tools.base import Fact, ToolResult, recompute_fact
from provenance.tools.queries import QUERY_REGISTRY


VERIFIED = "VERIFIED"
UNSUPPORTED = "UNSUPPORTED"
CONTRADICTED = "CONTRADICTED"
UNORDERED_EXTRACTORS = {"module.decision_list@1"}
STATIC_SNAPSHOT_CAVEAT = (
    "DECLARED_OWNER and DEPENDS_ON are static snapshots without valid-from timestamps."
)
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)(\s*%)?"
)
_PARAMETER_PATTERN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class Claim:
    claim_id: str
    text: str
    fact_ids: list[str]
    numbers: list[int | float] | None = None


@dataclass
class AnswerDraft:
    question: str
    claims: list[Claim]
    tool_results: list[ToolResult]


@dataclass
class VerifiedClaim:
    claim_id: str
    text: str
    status: str
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    proof_bundle: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifiedAnswer:
    question: str
    claims: list[VerifiedClaim]
    proof_bundle: dict[str, dict[str, Any]]
    summary_counts: dict[str, int]
    final_answer: str
    removed_claims: list[dict[str, Any]]


def qualified_fact_id(result_index: int, result: ToolResult, fact: Fact) -> str:
    """Return an unambiguous 1-based tool-result reference for a fact."""
    return f"{result_index + 1}:{result.tool}:{fact.fact_id}"


def _fact_index(
    tool_results: list[ToolResult],
) -> tuple[dict[str, list[tuple[int, ToolResult, Fact]]], dict[str, tuple[int, ToolResult, Fact]]]:
    raw: dict[str, list[tuple[int, ToolResult, Fact]]] = {}
    qualified: dict[str, tuple[int, ToolResult, Fact]] = {}
    for result_index, result in enumerate(tool_results):
        for fact in result.facts:
            entry = (result_index, result, fact)
            raw.setdefault(fact.fact_id, []).append(entry)
            qualified[qualified_fact_id(result_index, result, fact)] = entry
    return raw, qualified


def _resolve_fact(
    fact_id: str,
    raw: dict[str, list[tuple[int, ToolResult, Fact]]],
    qualified: dict[str, tuple[int, ToolResult, Fact]],
) -> tuple[tuple[int, ToolResult, Fact] | None, str | None]:
    if fact_id in qualified:
        return qualified[fact_id], None
    matches = raw.get(fact_id, [])
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"Fact ID {fact_id!r} is ambiguous; use a qualified result_index:tool:fact_id reference."
    return None, f"Fact ID {fact_id!r} does not exist in the draft's tool results."


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _values_equal(stored: Any, recomputed: Any, *, unordered: bool = False) -> bool:
    if isinstance(stored, bool) or isinstance(recomputed, bool):
        return type(stored) is type(recomputed) and stored == recomputed
    if isinstance(stored, float) or isinstance(recomputed, float):
        if not isinstance(stored, (int, float)) or not isinstance(recomputed, (int, float)):
            return False
        return math.isclose(float(stored), float(recomputed), rel_tol=1e-9, abs_tol=0.0)
    if type(stored) is not type(recomputed):
        return False
    if isinstance(stored, dict):
        return stored.keys() == recomputed.keys() and all(
            _values_equal(stored[key], recomputed[key], unordered=unordered)
            for key in stored
        )
    if isinstance(stored, (list, tuple)):
        if len(stored) != len(recomputed):
            return False
        if unordered:
            return sorted(_canonical(item) for item in stored) == sorted(
                _canonical(item) for item in recomputed
            )
        return all(
            _values_equal(left, right, unordered=False)
            for left, right in zip(stored, recomputed)
        )
    if isinstance(stored, set):
        return {_canonical(item) for item in stored} == {_canonical(item) for item in recomputed}
    return stored == recomputed


def _numeric_values(value: Any) -> list[int | float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        return [number for nested in value.values() for number in _numeric_values(nested)]
    if isinstance(value, (list, tuple, set)):
        return [number for nested in value for number in _numeric_values(nested)]
    return []


def _number_is_grounded(number_text: str, percent: str, fact_values: list[Any]) -> bool:
    expected = float(number_text)
    decimal_digits = len(number_text.partition(".")[2].split("e")[0].split("E")[0]) if "." in number_text else 0
    if percent.strip():
        expected /= 100.0
    for value in fact_values:
        for candidate in _numeric_values(value):
            adjusted = float(candidate) * 100.0 if percent.strip() else float(candidate)
            if round(adjusted, decimal_digits) == round(expected * (100.0 if percent.strip() else 1.0), decimal_digits):
                return True
    return False


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple, set)):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{key}: {_literal(nested)}" for key, nested in sorted(value.items())
        ) + "}"
    raise TypeError(f"Cannot render parameter value of type {type(value).__name__}.")


def _display_literal(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 8:
        preview = ", ".join(_literal(item) for item in value[:3])
        return f"[{preview}, ... {len(value) - 3} more]"
    return _literal(value)


def _compact_display_query(query_id: str, query: str) -> str:
    if query_id == "reviewer.g3.bundle@1":
        query = re.sub(r"\s+AS\s+[A-Za-z_][A-Za-z0-9_]*", "", query)
        query = query.replace("0.6931471805599453", "log(2)")
        aliases = {
            "touched_module_count": "tc",
            "target_touch": "tt",
            "all_touch": "at",
            "authored": "a",
            "history": "h",
            "person": "p",
            "module": "m",
            "review": "r",
            "review_ids": "ri",
        }
        for original, shortened in aliases.items():
            query = re.sub(rf"\b{original}\b", shortened, query)
    elif query_id == "reviewer.support_edges@1":
        for original, shortened in {
            "person": "p",
            "review": "r",
            "touch": "t",
            "module": "m",
        }.items():
            query = re.sub(rf"\b{original}\b", shortened, query)
    return " ".join(query.split())


def _display_query_override(query_id: str) -> tuple[str | None, str]:
    if query_id == "reviewer.support_edges@1":
        return (
            "MATCH (p:Person)-[r:REVIEWED]->(pr:PR)-[t:TOUCHES]->(m:Module) "
            "WHERE p.login IN $candidate_logins AND m.key IN $module_keys "
            "AND pr.merged_ts >= $expertise_start_ts AND pr.merged_ts <= $as_of_ts "
            "AND r.ts <= $as_of_ts "
            "RETURN id(p), id(pr), id(m), id(r), id(t) "
            "ORDER BY pr.merged_ts DESC LIMIT 120",
            " (display view)",
        )
    return None, ""


def _render_fact_query(query_id: str, facts: list[Fact]) -> str:
    query = QUERY_REGISTRY.get(query_id)
    if query is None:
        raise KeyError(f"Unknown query ID {query_id!r}.")
    display_query, display_note = _display_query_override(query_id)
    if display_query is not None:
        query = display_query

    parameter_names = list(dict.fromkeys(_PARAMETER_PATTERN.findall(query)))
    bindings = [
        {name: fact.params[name] for name in parameter_names if name in fact.params}
        for fact in facts
    ]
    placeholders: set[str] = set()
    header_lines: list[str] = []
    inline_values: dict[str, Any] = {}
    for name in parameter_names:
        values = [binding[name] for binding in bindings if name in binding]
        if len(values) != len(bindings) or not values:
            raise KeyError(f"Query parameter ${name} missing from fact {query_id!r}.")
        unique_values: list[Any] = []
        seen_values: set[str] = set()
        for value in values:
            key = _canonical(value)
            if key not in seen_values:
                seen_values.add(key)
                unique_values.append(value)
        has_long_list = any(
            isinstance(value, (list, tuple)) and len(value) > 8
            for value in unique_values
        )
        if len(unique_values) == 1 and not has_long_list:
            inline_values[name] = unique_values[0]
            continue
        placeholders.add(name)
        if len(unique_values) == 1:
            display_values = _display_literal(unique_values[0])
        else:
            display_values = "; ".join(
                f"binding {index}: {_display_literal(value)}"
                for index, value in enumerate(unique_values, start=1)
            )
        header_lines.append(f"// ${name} = {display_values}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in placeholders:
            return f"${name}"
        return _literal(inline_values[name])

    query = _PARAMETER_PATTERN.sub(replace, query)
    query = _compact_display_query(query_id, query)
    prefix = [f"// {query_id}{display_note}", *header_lines]
    return "\n".join([*prefix, query]).strip()


def render_claim_proof(claim: VerifiedClaim) -> str:
    """Render registry queries with literal parameters for display only; never execute this text."""
    fact_records = claim.proof_bundle.get("facts", [])
    facts_by_query: dict[str, list[Fact]] = {}
    for item in fact_records:
        facts_by_query.setdefault(item["query_id"], []).append(item["fact"])
    return "\n\n".join(
        _render_fact_query(query_id, facts)
        for query_id, facts in facts_by_query.items()
    )


def _verify_claim(
    claim: Claim,
    raw_facts: dict[str, list[tuple[int, ToolResult, Fact]]],
    qualified_facts: dict[str, tuple[int, ToolResult, Fact]],
    graph: Any,
) -> VerifiedClaim:
    reasons: list[str] = []
    citations: list[tuple[int, ToolResult, Fact]] = []
    if not claim.fact_ids:
        reasons.append("Claim has no cited facts.")
    for fact_id in claim.fact_ids:
        entry, error = _resolve_fact(fact_id, raw_facts, qualified_facts)
        if error:
            reasons.append(error)
        elif entry is not None:
            citations.append(entry)

    replay_errors: list[str] = []
    contradictory = False
    as_of_values: set[int] = set()
    query_ids: list[str] = []
    params_list: list[dict[str, Any]] = []
    node_ids: set[int] = set()
    edge_ids: set[int] = set()
    caveats: list[str] = []
    proof_facts: list[dict[str, Any]] = []

    for _, result, fact in citations:
        as_of_values.add(int(result.as_of_ts))
        parameter_as_of = fact.params.get("as_of_ts")
        if parameter_as_of is not None and int(parameter_as_of) != int(result.as_of_ts):
            reasons.append(
                f"Fact {fact.fact_id} params as_of_ts={parameter_as_of} disagrees with tool result pin {result.as_of_ts}."
            )
        try:
            recomputed = recompute_fact(graph, fact)
        except Exception as error:
            replay_errors.append(f"Fact {fact.fact_id} could not be replayed: {error}")
            continue
        if not _values_equal(
            fact.value,
            recomputed,
            unordered=fact.extractor in UNORDERED_EXTRACTORS,
        ):
            contradictory = True
            replay_errors.append(
                f"Fact {fact.fact_id} mismatch: stored={_canonical(fact.value)}, recomputed={_canonical(recomputed)}."
            )
        query_ids.append(fact.query_id)
        params_list.append({"query_id": fact.query_id, "params": fact.params})
        node_ids.update(int(item) for item in result.evidence.get("node_ids", []))
        edge_ids.update(int(item) for item in result.evidence.get("edge_ids", []))
        query_text = QUERY_REGISTRY.get(fact.query_id, "")
        if "DECLARED_OWNER" in query_text or "DEPENDS_ON" in query_text:
            if STATIC_SNAPSHOT_CAVEAT not in caveats:
                caveats.append(STATIC_SNAPSHOT_CAVEAT)
        proof_facts.append(
            {
                "fact_id": qualified_fact_id(_, result, fact),
                "query_id": fact.query_id,
                "params": fact.params,
                "fact": fact,
            }
        )

    if len(as_of_values) > 1:
        reasons.append(
            "Cited facts disagree on as_of_ts: "
            + ", ".join(str(value) for value in sorted(as_of_values))
            + "."
        )
    reasons.extend(replay_errors)

    values_for_grounding = [fact.value for _, _, fact in citations]
    for number_match in _NUMBER_PATTERN.finditer(claim.text):
        number_text, percent = number_match.group(1), number_match.group(2) or ""
        if not _number_is_grounded(number_text, percent, values_for_grounding):
            reasons.append(f"Number {number_match.group(0).strip()!r} in claim text is not grounded by cited facts.")
    for number in claim.numbers or []:
        if not _number_is_grounded(str(number), "", values_for_grounding):
            reasons.append(f"Declared number {number!r} is not grounded by cited facts.")

    if contradictory:
        status = CONTRADICTED
    elif reasons:
        status = UNSUPPORTED
    else:
        status = VERIFIED

    proof_bundle = {
        "query_ids": list(dict.fromkeys(query_ids)),
        "params": params_list,
        "node_ids": sorted(node_ids),
        "edge_ids": sorted(edge_ids),
        "as_of_ts": next(iter(as_of_values)) if len(as_of_values) == 1 else None,
        "caveats": caveats,
        "facts": proof_facts,
    }
    return VerifiedClaim(
        claim_id=claim.claim_id,
        text=claim.text,
        status=status,
        reasons=reasons,
        caveats=caveats,
        proof_bundle=proof_bundle,
    )


def verify(draft: AnswerDraft, graph: Any) -> VerifiedAnswer:
    raw_facts, qualified_facts = _fact_index(draft.tool_results)
    verified_claims = [
        _verify_claim(claim, raw_facts, qualified_facts, graph)
        for claim in draft.claims
    ]
    summary_counts = {
        status: sum(claim.status == status for claim in verified_claims)
        for status in (VERIFIED, UNSUPPORTED, CONTRADICTED)
    }
    proof_bundle = {
        claim.claim_id: claim.proof_bundle
        for claim in verified_claims
        if claim.status == VERIFIED
    }
    final_lines = []
    removed_claims = []
    for claim in verified_claims:
        if claim.status == VERIFIED:
            line = claim.text
            if claim.caveats:
                line += " Caveat: " + " ".join(claim.caveats)
            final_lines.append(line)
        else:
            removed_claims.append(
                {
                    "claim_id": claim.claim_id,
                    "text": claim.text,
                    "status": claim.status,
                    "reasons": claim.reasons,
                }
            )
    return VerifiedAnswer(
        question=draft.question,
        claims=verified_claims,
        proof_bundle=proof_bundle,
        summary_counts=summary_counts,
        final_answer="\n".join(final_lines),
        removed_claims=removed_claims,
    )
