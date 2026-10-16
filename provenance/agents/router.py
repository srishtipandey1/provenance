from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

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
from provenance.tools.base import Fact, ToolResult
from provenance.tools.queries import QUERY_REGISTRY
from provenance.agents.verifier import (
    AnswerDraft,
    Claim,
    VerifiedAnswer,
    qualified_fact_id,
    verify,
)


ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_QUESTIONS = (
    "what is the risk of changing <module>?",
    "who should review PR <number>?",
    "who should review changes to <module>?",
    "what happens if <login> leaves or is unavailable?",
    "who owns <module>?",
    "what depends on <module>?",
    "which modules are most at risk?",
    "why was <module> decided?",
)
_PR_PATTERN = re.compile(
    r"(?:\bPR\s*#?\s*(\d+)|(?<![A-Za-z0-9_])#(\d+)\b)",
    re.IGNORECASE,
)
_LOGIN_PATTERN = re.compile(
    r"(?:if|when)\s+@?([A-Za-z0-9][A-Za-z0-9-]*)\s+(?:leaves?|is\s+unavailable)",
    re.IGNORECASE,
)
_GENERIC_MODULE_WORDS = {
    "the", "a", "an", "integration", "component", "module", "please", "home", "assistant"
}


@dataclass
class RoutedAnswer:
    question: str
    answer: VerifiedAnswer | None
    tool_timings: list[dict[str, Any]]
    unsupported_message: str | None = None
    tampered_fact_id: str | None = None


def _normalize_module(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", value.casefold().replace("_", " "))
    return " ".join(word for word in words if word not in _GENERIC_MODULE_WORDS)


def resolve_module(graph: Any, hint: str) -> tuple[str | None, list[str]]:
    rows = graph.ro_query(QUERY_REGISTRY["modules.list@1"], {}).result_set
    wanted = _normalize_module(hint)
    if not wanted:
        return None, []
    candidates: list[tuple[float, str, str]] = []
    for row in rows:
        key, kind, name = str(row[0]), str(row[1]), str(row[2])
        tail = key.split(":", 1)[-1]
        aliases = {
            _normalize_module(key),
            _normalize_module(tail),
            _normalize_module(name),
        }
        aliases.discard("")
        if not aliases:
            continue
        if wanted in aliases:
            score = 1.0
        else:
            score = max(
                SequenceMatcher(None, wanted, alias).ratio()
                for alias in aliases
            )
            if any(wanted in alias or alias in wanted for alias in aliases if alias):
                score = max(score, 0.84)
        if score >= 0.58:
            candidates.append((score, key, kind))
    candidates.sort(
        key=lambda item: (
            -item[0],
            0 if item[2] == "integration" else 1,
            item[1],
        )
    )
    if not candidates:
        return None, []
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.025:
        exact = [item for item in candidates if item[0] == 1.0]
        if len(exact) != 1:
            return None, [item[1] for item in candidates[:5]]
    return candidates[0][1], [item[1] for item in candidates[:5]]


def resolve_person(graph: Any, login_hint: str) -> str | None:
    login = login_hint.strip().lstrip("@").casefold()
    if not login:
        return None
    rows = graph.ro_query(
        QUERY_REGISTRY["person.by_login@1"],
        {"person_login": login},
    ).result_set
    return str(rows[0][0]).casefold() if rows else None


def _module_hint(question: str, pattern: str) -> str | None:
    match = re.search(pattern, question, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip(" \t\r\n?.!,;:'\"")


def _fact(result: ToolResult, extractor: str) -> Fact:
    return next(item for item in result.facts if item.extractor == extractor)


def _reference(result_index: int, result: ToolResult, fact: Fact) -> str:
    return qualified_fact_id(result_index, result, fact)


def _execute(
    tool_name: str,
    args: dict[str, Any],
    call: Callable[[], ToolResult],
    results: list[ToolResult],
    timings: list[dict[str, Any]],
) -> ToolResult:
    started = time.perf_counter()
    result = call()
    timings.append(
        {"tool": tool_name, "args": args, "seconds": round(time.perf_counter() - started, 4)}
    )
    results.append(result)
    return result


def _claim(claims: list[Claim], text: str, references: list[str]) -> None:
    claims.append(Claim(f"C{len(claims) + 1}", text, references))


def _module_profile_claims(
    module_key: str,
    profile: ToolResult,
    result_index: int,
    claims: list[Claim],
) -> None:
    count_fact = _fact(profile, "module.pr_count_value@1")
    metadata_fact = _fact(profile, "module.metadata_value@1")
    _claim(
        claims,
        f"{module_key} has {count_fact.value} PRs in the configured activity window.",
        [
            _reference(result_index, profile, metadata_fact),
            _reference(result_index, profile, count_fact),
        ],
    )


def _risk_module(graph: Any, question: str, results: list[ToolResult], timings: list[dict[str, Any]], claims: list[Claim]) -> str | None:
    hint = _module_hint(
        question,
        r"(?:risk\s+of\s+(?:changing|change\s+to)|changing|change\s+to)\s+(.+?)(?:\?|$)",
    )
    module_key, suggestions = resolve_module(graph, hint or "")
    if module_key is None:
        return f"I couldn't resolve a module from {hint!r}. Similar modules: {', '.join(suggestions) or 'none found'}."
    profile = _execute(
        "module_profile", {"module_key": module_key},
        lambda: module_profile(graph, module_key=module_key), results, timings,
    )
    _module_profile_claims(module_key, profile, len(results) - 1, claims)
    drift = _execute(
        "ownership_drift", {"module_key": module_key},
        lambda: ownership_drift(graph, module_key=module_key), results, timings,
    )
    drift_fact = _fact(drift, "ownership.drift_score@1")
    _claim(
        claims,
        f"The ownership drift score is {float(drift_fact.value):.3f}.",
        [_reference(len(results) - 1, drift, drift_fact)],
    )
    bus = _execute(
        "bus_factor", {"module_key": module_key},
        lambda: bus_factor(graph, module_key=module_key), results, timings,
    )
    bus_fact = _fact(bus, "bus.ranking@1")
    bus_record = next((row for row in bus_fact.value if row[0] == module_key), None)
    if bus_record:
        _claim(
            claims,
            f"The top human reviewer's share for {module_key} is {float(bus_record[5]):.3f}.",
            [_reference(len(results) - 1, bus, bus_fact)],
        )
    blast = _execute(
        "blast_radius", {"module_key": module_key},
        lambda: blast_radius(graph, module_key=module_key), results, timings,
    )
    blast_fact = _fact(blast, "blast.dependent_count@1")
    _claim(
        claims,
            f"The requested target has {blast_fact.value} distinct reverse dependents within the configured depth.",
        [_reference(len(results) - 1, blast, blast_fact)],
    )
    critical = _execute(
        "criticality", {"top_n": 2000},
        lambda: criticality(graph, top_n=2000), results, timings,
    )
    rank_fact = _fact(critical, "criticality.top_scores@1")
    rank_record = next((row for row in rank_fact.value if row[0] == module_key), None)
    if rank_record:
        _claim(
            claims,
            f"The PageRank score for {module_key} is {float(rank_record[2]):.6f}.",
            [_reference(len(results) - 1, critical, rank_fact)],
        )
    return None


def _reviewer_claim(
    graph: Any,
    question: str,
    results: list[ToolResult],
    timings: list[dict[str, Any]],
    claims: list[Claim],
) -> str | None:
    pr_match = _PR_PATTERN.search(question)
    if pr_match:
        pr_number = int(pr_match.group(1) or pr_match.group(2))
        result = _execute(
            "suggest_reviewers", {"pr_number": pr_number, "top_n": 3},
            lambda: suggest_reviewers(graph, pr_number=pr_number, top_n=3), results, timings,
        )
    else:
        hint = _module_hint(
            question,
            r"(?:review\s+(?:changes\s+)?to|review\s+for)\s+(.+?)(?:\?|$)",
        )
        module_key, suggestions = resolve_module(graph, hint or "")
        if module_key is None:
            return f"I couldn't resolve a module from {hint!r}. Similar modules: {', '.join(suggestions) or 'none found'}."
        result = _execute(
            "suggest_reviewers", {"modules": [module_key], "top_n": 3},
            lambda: suggest_reviewers(graph, modules=[module_key], top_n=3), results, timings,
        )
    if result.result.get("error"):
        return str(result.result["error"])
    candidate_fact = _fact(result, "reviewer.g3_candidates@1")
    candidates = candidate_fact.value[:3]
    if not candidates:
        _claim(
            claims,
            "No eligible human reviewer candidates were found in the configured history.",
            [_reference(len(results) - 1, result, candidate_fact)],
        )
        return None
    names = ", ".join(item["login"] for item in candidates)
    target_fact = next(
        (item for item in result.facts if item.extractor == "reviewer.target@1"),
        None,
    )
    prefix = f"For PR {pr_number}, " if pr_match else f"For {result.result['modules'][0]}, "
    reference_facts = [_reference(len(results) - 1, result, candidate_fact)]
    if pr_match and target_fact is not None:
        reference_facts.append(_reference(len(results) - 1, result, target_fact))
    _claim(claims, prefix + f"the top reviewer suggestions are {names}.", reference_facts)
    return None


def _maintainer_claim(
    graph: Any,
    question: str,
    results: list[ToolResult],
    timings: list[dict[str, Any]],
    claims: list[Claim],
) -> str | None:
    match = _LOGIN_PATTERN.search(question)
    login = resolve_person(graph, match.group(1)) if match else None
    if login is None:
        return "I couldn't resolve a known GitHub login. Use a public GitHub login in 'what happens if <login> leaves?'"
    result = _execute(
        "maintainer_loss", {"person_login": login},
        lambda: maintainer_loss(graph, person_login=login), results, timings,
    )
    loss_fact = _fact(result, "maintainer.loss_modules@1")
    impact_fact = _fact(result, "maintainer.dependent_impact@1")
    distinct = impact_fact.value["distinct_dependents_affected"]
    summed = impact_fact.value["summed_dependent_edges"]
    if result.result["modules"]:
        text = (
            f"If {login} leaves, affected modules include "
            + ", ".join(item["module_key"] for item in result.result["modules"][:3])
            + f"; {distinct} distinct dependent modules are affected across uncovered modules, "
            + f"with {summed} summed per-module dependent counts."
        )
    else:
        text = f"If {login} leaves, no module crosses the configured coverage or review-share thresholds."
    _claim(
        claims,
        text,
        [
            _reference(len(results) - 1, result, loss_fact),
            _reference(len(results) - 1, result, impact_fact),
        ],
    )
    return None


def _owner_claim(
    graph: Any,
    question: str,
    results: list[ToolResult],
    timings: list[dict[str, Any]],
    claims: list[Claim],
) -> str | None:
    hint = _module_hint(question, r"who\s+owns\s+(.+?)(?:\?|$)")
    module_key, suggestions = resolve_module(graph, hint or "")
    if module_key is None:
        return f"I couldn't resolve a module from {hint!r}. Similar modules: {', '.join(suggestions) or 'none found'}."
    profile = _execute(
        "module_profile", {"module_key": module_key},
        lambda: module_profile(graph, module_key=module_key), results, timings,
    )
    owners_fact = _fact(profile, "module.owner_list@1")
    drift = _execute(
        "ownership_drift", {"module_key": module_key},
        lambda: ownership_drift(graph, module_key=module_key), results, timings,
    )
    behavior_fact = _fact(drift, "ownership.behavioral_top@1")
    owners = [
        item.get("handle") if item.get("kind") == "team" else item.get("login")
        for item in owners_fact.value
    ]
    contributors = [item["login"] for item in behavior_fact.value[:5]]
    owner_text = ", ".join(item for item in owners if item) or "no declared owner"
    contributor_text = ", ".join(contributors) or "no recent human contributors"
    _claim(
        claims,
        f"Declared owner(s): {owner_text}; top behavioral contributors include {contributor_text}.",
        [
            _reference(len(results) - 2, profile, owners_fact),
            _reference(len(results) - 1, drift, behavior_fact),
        ],
    )
    return None


def _blast_claim(
    graph: Any,
    question: str,
    results: list[ToolResult],
    timings: list[dict[str, Any]],
    claims: list[Claim],
) -> str | None:
    hint = _module_hint(question, r"what\s+depends\s+on\s+(.+?)(?:\?|$)")
    module_key, suggestions = resolve_module(graph, hint or "")
    if module_key is None:
        return f"I couldn't resolve a module from {hint!r}. Similar modules: {', '.join(suggestions) or 'none found'}."
    result = _execute(
        "blast_radius", {"module_key": module_key},
        lambda: blast_radius(graph, module_key=module_key), results, timings,
    )
    fact = _fact(result, "blast.dependent_count@1")
    _claim(
        claims,
        f"{module_key} has {fact.value} distinct reverse-dependent modules within the configured depth.",
        [_reference(len(results) - 1, result, fact)],
    )
    return None


def _top_risk_claims(
    graph: Any,
    results: list[ToolResult],
    timings: list[dict[str, Any]],
    claims: list[Claim],
) -> None:
    bus = _execute(
        "bus_factor", {"top_n": 5},
        lambda: bus_factor(graph, top_n=5), results, timings,
    )
    bus_fact = _fact(bus, "bus.ranking@1")
    if bus_fact.value:
        item = bus_fact.value[0]
        _claim(
            claims,
            f"The leading review-concentration module is {item[0]} with top-person review share {float(item[5]):.3f}.",
            [_reference(len(results) - 1, bus, bus_fact)],
        )
    critical = _execute(
        "criticality", {"top_n": 5},
        lambda: criticality(graph, top_n=5), results, timings,
    )
    critical_fact = _fact(critical, "criticality.top_scores@1")
    if critical_fact.value:
        item = critical_fact.value[0]
        _claim(
            claims,
            f"The leading dependency-PageRank module is {item[0]} with score {float(item[2]):.6f}.",
            [_reference(len(results) - 1, critical, critical_fact)],
        )
    weights = {
        "risk_bus_factor_weight": "bus-factor concentration",
        "risk_ownership_drift_weight": "ownership drift",
        "risk_criticality_weight": "criticality",
        "risk_blast_radius_weight": "blast radius",
    }
    config_facts = {
        fact.text.removeprefix("Pinned tool default for ").removesuffix("."): fact
        for fact in bus.facts
        if fact.text.startswith("Pinned tool default for risk_")
    }
    if all(name in config_facts for name in weights):
        detail = ", ".join(
            f"{label} {float(config_facts[name].value):.2f}"
            for name, label in weights.items()
        )
        _claim(
            claims,
            f"The stored risk-report formula weights are {detail}.",
            [
                _reference(len(results) - 2, bus, config_facts[name])
                for name in weights
            ],
        )


def _decision_claim(
    graph: Any,
    question: str,
    results: list[ToolResult],
    timings: list[dict[str, Any]],
    claims: list[Claim],
) -> str | None:
    hint = _module_hint(
        question,
        r"why\s+(?:was|were|is|are)\s+(.+?)\s+(?:decided|chosen|selected)(?:\?|$)",
    ) or _module_hint(question, r"why\s+(?:was\s+the\s+decision\s+for)\s+(.+?)(?:\?|$)")
    module_key, suggestions = resolve_module(graph, hint or "")
    if module_key is None:
        return f"I couldn't resolve a module from {hint!r}. Similar modules: {', '.join(suggestions) or 'none found'}."
    result = _execute(
        "decision_trace", {"module_key": module_key},
        lambda: decision_trace(graph, module_key=module_key), results, timings,
    )
    decisions_fact = _fact(result, "module.decision_list@1")
    chain_fact = _fact(result, "decision.supersedes_chain@1")
    if not decisions_fact.value:
        text = f"No linked decision records were found for {module_key}."
    else:
        first = decisions_fact.value[0]
        title = re.sub(r"\d+", "", str(first["title"])).strip()
        title = " ".join(title.split()) or "decision record"
        text = (
            f"The linked decision '{title}' is connected through {first['relation']}; "
            "source metadata is recorded."
        )
    _claim(
        claims,
        text,
        [
            _reference(len(results) - 1, result, decisions_fact),
            _reference(len(results) - 1, result, chain_fact),
        ],
    )
    return None


def _classify(question: str) -> str | None:
    normalized = question.casefold()
    if "who should review" in normalized:
        return "reviewer"
    if (_LOGIN_PATTERN.search(question) and re.search(r"what\s+happens|what\s+if", normalized)):
        return "maintainer"
    if re.search(r"risk\s+of\s+(?:changing|change\s+to)|\bchanging\b|\bchange\s+to\b", normalized):
        return "risk"
    if re.search(r"who\s+owns\b", normalized):
        return "owner"
    if re.search(r"what\s+depends\s+on\b", normalized):
        return "blast"
    if re.search(r"which\s+modules?.*\brisk\b|modules?.*most\s+at\s+risk", normalized):
        return "top_risk"
    if re.search(r"why\s+(?:was|were|is|are)\b.*\b(?:decided|chosen|selected)\b|why\s+was\s+the\s+decision\s+for", normalized):
        return "decision"
    return None


def run_question(graph: Any, question: str, *, tamper_n: int | None = None) -> RoutedAnswer:
    route = _classify(question)
    timings: list[dict[str, Any]] = []
    results: list[ToolResult] = []
    claims: list[Claim] = []
    if route is None:
        supported = "Supported questions:\n- " + "\n- ".join(SUPPORTED_QUESTIONS)
        return RoutedAnswer(question, None, timings, unsupported_message=supported)

    if route == "risk":
        message = _risk_module(graph, question, results, timings, claims)
    elif route == "reviewer":
        message = _reviewer_claim(graph, question, results, timings, claims)
    elif route == "maintainer":
        message = _maintainer_claim(graph, question, results, timings, claims)
    elif route == "owner":
        message = _owner_claim(graph, question, results, timings, claims)
    elif route == "blast":
        message = _blast_claim(graph, question, results, timings, claims)
    elif route == "top_risk":
        _top_risk_claims(graph, results, timings, claims)
        message = None
    else:
        message = _decision_claim(graph, question, results, timings, claims)

    if message:
        return RoutedAnswer(question, None, timings, unsupported_message=message)
    draft = AnswerDraft(question=question, claims=claims, tool_results=results)
    tampered_fact_id = _tamper_draft(draft, tamper_n) if tamper_n is not None else None
    started = time.perf_counter()
    answer = verify(draft, graph)
    timings.append({"tool": "verify", "seconds": round(time.perf_counter() - started, 4)})
    return RoutedAnswer(question, answer, timings, tampered_fact_id=tampered_fact_id)


def _tamper_draft(draft: AnswerDraft, number: int) -> str:
    if number < 1:
        raise ValueError("--tamper N uses a one-based positive fact index.")
    seen: set[str] = set()
    refs = [fact_id for claim in draft.claims for fact_id in claim.fact_ids if not (fact_id in seen or seen.add(fact_id))]
    if number > len(refs):
        raise ValueError(f"--tamper {number} exceeds the {len(refs)} unique cited facts.")
    target_id = refs[number - 1]
    for result_index, result in enumerate(draft.tool_results):
        for fact in result.facts:
            if qualified_fact_id(result_index, result, fact) == target_id or fact.fact_id == target_id:
                fact.value = _perturb(fact.value)
                return target_id
    raise ValueError(f"Cited fact {target_id!r} could not be found for tampering.")


def _perturb(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return value + " [tampered]"
    if isinstance(value, list):
        if value:
            copied = list(value)
            copied[0] = _perturb(copied[0])
            return copied
        return ["tampered"]
    if isinstance(value, dict):
        if value:
            copied = dict(value)
            key = next(iter(copied))
            copied[key] = _perturb(copied[key])
            return copied
        return {"tampered": True}
    return "tampered"
