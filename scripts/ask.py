from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from falkordb import FalkorDB

from provenance.config import settings
from provenance.ingest.graph_loader import DEFAULT_GRAPH
from provenance.agents.agent import AgentAnswer, run_agent
from provenance.agents.router import RoutedAnswer, run_question
from provenance.agents.verifier import VERIFIED, VerifiedAnswer, render_claim_proof


def _proof_for(answer: VerifiedAnswer | None, claim_id: str) -> str | None:
    if answer is None:
        return None
    claim = next(
        (item for item in answer.claims if item.claim_id == claim_id),
        None,
    )
    if claim is None or claim.status != VERIFIED:
        return None
    return render_claim_proof(claim)


def answer_question(
    graph: Any,
    question: str,
    *,
    mode: str = "deterministic",
    tamper_n: int | None = None,
    llm_client: Any | None = None,
) -> tuple[VerifiedAnswer | None, list[dict[str, Any]], str | None, str | None, str | None, int, str | None, str | None]:
    if mode == "deterministic":
        result = run_question(graph, question, tamper_n=tamper_n)
        return (
            result.answer,
            result.tool_timings,
            result.unsupported_message,
            None,
            None,
            0,
            "deterministic",
            result.tampered_fact_id,
        )
    result: AgentAnswer = run_agent(
        graph,
        question,
        client=llm_client,
        tamper_n=tamper_n,
    )
    return (
        result.answer,
        result.tool_timings,
        result.unsupported_message,
        result.provider,
        result.model,
        result.llm_calls,
        result.mode,
        result.tampered_fact_id,
    )


def _json_output(
    question: str,
    answer: VerifiedAnswer | None,
    tool_timings: list[dict[str, Any]],
    unsupported_message: str | None,
    provider: str | None,
    model: str | None,
    llm_calls: int,
    mode: str,
    tampered_fact_id: str | None,
    show_proof: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "question": question,
        "mode": mode,
        "provider": provider,
        "model": model,
        "llm_calls": llm_calls,
        "unsupported_message": unsupported_message,
        "answer": asdict(answer) if answer is not None else None,
        "tool_timings": tool_timings,
        "tampered_fact_id": tampered_fact_id,
    }
    if show_proof:
        payload["shown_proof"] = _proof_for(answer, show_proof)
    return payload


def _print_human(
    answer: VerifiedAnswer | None,
    tool_timings: list[dict[str, Any]],
    unsupported_message: str | None,
    provider: str | None,
    model: str | None,
    llm_calls: int,
    mode: str,
    tampered_fact_id: str | None,
    show_proof: str | None,
) -> None:
    print(f"Mode: {mode}; provider: {provider or 'none'}; model: {model or 'none'}; LLM calls: {llm_calls}")
    if unsupported_message:
        print(unsupported_message)
    elif answer is not None:
        print(answer.final_answer or "No claims were verified.")
        counts = answer.summary_counts
        total = sum(counts.values())
        print(f"{counts[VERIFIED]} of {total} claims verified")
        if answer.removed_claims:
            print("Removed claims:")
            for claim in answer.removed_claims:
                reason = "; ".join(claim["reasons"])
                print(f"- {claim['claim_id']} [{claim['status']}]: {reason}")
    if tampered_fact_id:
        status = "CONTRADICTED" if answer and any(
            claim.status == "CONTRADICTED" for claim in answer.claims
        ) else "NOT CAUGHT"
        print(f"TEST HOOK: tampered {tampered_fact_id}; verifier status: {status}")
    if tool_timings:
        print("Tools:")
        for timing in tool_timings:
            args = json.dumps(timing.get("args", {}), sort_keys=True, separators=(",", ":"))
            print(f"- {timing['tool']} {timing['seconds']:.4f}s {args}")
    if show_proof:
        proof = _proof_for(answer, show_proof)
        if proof is None:
            print(f"No verified proof is available for claim {show_proof}.")
        else:
            print(f"Proof for {show_proof} (display only; read-only query literals):")
            print(proof)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ask Provenance using deterministic, verified graph tools only."
    )
    parser.add_argument("question", help="Question about public Home Assistant graph data.")
    parser.add_argument(
        "--mode",
        choices=("deterministic", "llm", "auto"),
        default="deterministic",
        help="deterministic (default), llm, or auto (LLM if usable, otherwise deterministic fallback).",
    )
    parser.add_argument("--json", action="store_true", help="Print the complete verified result as JSON.")
    parser.add_argument("--show-proof", metavar="CLAIM_ID", help="Render a verified claim's read-only proof snippet.")
    parser.add_argument(
        "--tamper",
        type=int,
        metavar="N",
        help="TEST HOOK: perturb the Nth cited fact before verification to demonstrate detection in either mode.",
    )
    parser.add_argument("--graph", default=DEFAULT_GRAPH)
    args = parser.parse_args()
    try:
        database = FalkorDB(host=settings.FALKORDB_HOST, port=settings.FALKORDB_PORT)
        graph = database.select_graph(args.graph)
        answer, timings, unsupported, provider, model, llm_calls, mode, tampered = answer_question(
            graph,
            args.question,
            mode=args.mode,
            tamper_n=args.tamper,
        )
    except (ValueError, KeyError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(
            _json_output(
                args.question, answer, timings, unsupported, provider, model,
                llm_calls, mode, tampered, args.show_proof,
            ),
            indent=2,
            sort_keys=True,
            default=str,
        ))
    else:
        _print_human(
            answer, timings, unsupported, provider, model, llm_calls,
            mode, tampered, args.show_proof,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
