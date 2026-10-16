from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from falkordb import FalkorDB
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from provenance.agents.agent import TOOL_FUNCTIONS, run_agent, validate_tool_arguments
from provenance.agents.llm import llm_available
from provenance.agents.router import resolve_module, run_question
from provenance.agents.verifier import VERIFIED, VerifiedAnswer, render_claim_proof
from provenance.api.subgraph import export_claim_subgraph, export_subgraph
from provenance.config import settings
from provenance.ingest.graph_loader import DEFAULT_GRAPH
from provenance.tools.config import DEFAULT_TOOL_CONFIG
from provenance.tools.queries import QUERY_REGISTRY
from provenance.tools.registry import mcp_tool_definitions


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"
DEFAULT_DAILY_LIMIT = 100
try:
    DEFAULT_DAILY_LIMIT = max(0, int(os.getenv("PROVENANCE_LLM_DAILY_REQUEST_LIMIT", "100")))
except ValueError:
    pass

logger = logging.getLogger("provenance.api")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubgraphNode(StrictModel):
    id: int
    label: str
    key: str | None = None
    display: str
    props: dict[str, Any]


class SubgraphEdge(StrictModel):
    id: int
    type: str
    source: int
    target: int
    props: dict[str, Any]


class SubgraphResponse(StrictModel):
    nodes: list[SubgraphNode]
    edges: list[SubgraphEdge]
    node_count_total: int
    edge_count_total: int
    node_count_returned: int
    edge_count_returned: int
    truncated: bool
    note: str


class ProviderStatus(StrictModel):
    provider: str
    model: str
    available: bool


class HealthResponse(StrictModel):
    graph: str
    graph_reachable: bool
    counts: dict[str, dict[str, int]]
    as_of_ts: int | None
    as_of_utc: str | None
    llm_providers: list[ProviderStatus]


class ToolsResponse(StrictModel):
    tools: list[dict[str, Any]]


class AskRequest(StrictModel):
    question: str = Field(min_length=1, max_length=300)
    mode: Literal["deterministic", "llm", "auto"] = "deterministic"
    tamper: int | None = Field(default=None, ge=1)

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Question cannot be blank.")
        return normalized


class ToolRequest(StrictModel):
    args: dict[str, Any] = Field(default_factory=dict)


class ProofResponse(StrictModel):
    query_ids: list[str]
    params: list[dict[str, Any]]
    rendered_cypher: str
    caveats: list[str]
    subgraph: SubgraphResponse


class ClaimResponse(StrictModel):
    claim_id: str
    text: str
    status: str
    reasons: list[str]
    caveats: list[str]
    proof: ProofResponse


class AskResponse(StrictModel):
    answer: str
    claims: list[ClaimResponse]
    tool_timings: list[dict[str, Any]]
    mode: str
    provider: str | None
    model: str | None
    calls: int
    removed_claims: list[dict[str, Any]]
    ignored_fact_ids: list[str]
    tampered_fact_id: str | None = None


class ToolFactResponse(StrictModel):
    fact_id: str
    text: str
    value: Any
    unit: str
    query_id: str
    params: dict[str, Any]
    extractor: str


class ToolResultResponse(StrictModel):
    tool: str
    args: dict[str, Any]
    as_of_ts: int
    result: dict[str, Any]
    facts: list[ToolFactResponse]
    evidence: dict[str, Any]
    warnings: list[str]


class ModuleHit(StrictModel):
    key: str
    kind: str
    name: str
    id: int


class PersonHit(StrictModel):
    login: str
    id: int


class ModulesSearchResponse(StrictModel):
    results: list[ModuleHit]


class PeopleSearchResponse(StrictModel):
    results: list[PersonHit]


class OwnerHit(StrictModel):
    login: str | None = None
    handle: str | None = None
    is_bot: bool
    id: int


class BehavioralPersonHit(StrictModel):
    login: str
    authored_prs: int
    reviewed_prs: int
    id: int


class DependentHit(StrictModel):
    key: str
    kind: str
    id: int


class NeighborhoodResponse(StrictModel):
    module: ModuleHit
    declared_owners: list[OwnerHit]
    behavioral_people: list[BehavioralPersonHit]
    direct_dependents: list[DependentHit]
    as_of_ts: int
    subgraph: SubgraphResponse


class RiskResponse(RootModel[dict[str, Any]]):
    pass


class ScoreboardResponse(RootModel[dict[str, Any]]):
    pass


class _RateLimiter:
    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, client_key: str, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            timestamps = self._requests[client_key]
            while timestamps and now - timestamps[0] >= window_seconds:
                timestamps.popleft()
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            return True


class _DailyRequestBudget:
    def __init__(self, limit: int, path: Path) -> None:
        self.limit = max(0, int(limit))
        self.path = Path(path)
        self.day = date.today().isoformat()
        self.used = 0
        self.reserved = 0
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and data.get("date") == self.day:
            self.used = max(0, int(data.get("used", 0)))

    def _refresh_day(self) -> None:
        today = date.today().isoformat()
        if today != self.day:
            self.day = today
            self.used = 0
            self.reserved = 0

    def reserve(self, requested: int) -> int:
        with self._lock:
            self._refresh_day()
            available = max(0, self.limit - self.used - self.reserved)
            reserved = min(max(0, requested), available)
            self.reserved += reserved
            return reserved

    def settle(self, reserved: int, actual: int) -> None:
        with self._lock:
            self._refresh_day()
            self.reserved = max(0, self.reserved - reserved)
            self.used += min(max(0, int(actual)), reserved)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".llm-budget-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump({"date": self.day, "used": self.used}, temporary)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)


class _DeterministicCache:
    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        self.ttl_seconds = max(0.0, ttl_seconds)
        self.max_entries = max(1, max_entries)
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._entries.pop(key, None)
                return None
            return json.loads(json.dumps(value, default=str))

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.ttl_seconds:
            return
        with self._lock:
            if len(self._entries) >= self.max_entries:
                oldest = min(self._entries, key=lambda entry: self._entries[entry][0])
                self._entries.pop(oldest, None)
            self._entries[key] = (
                time.monotonic() + self.ttl_seconds,
                json.loads(json.dumps(value, default=str)),
            )


def _graph_counts(graph: Any) -> dict[str, dict[str, int]]:
    nodes = {
        str(labels[0]): int(count)
        for labels, count in graph.ro_query(
            QUERY_REGISTRY["api.health.node_counts@1"], {}
        ).result_set
        if isinstance(labels, (list, tuple)) and labels
    }
    edges = {
        str(edge_type): int(count)
        for edge_type, count in graph.ro_query(
            QUERY_REGISTRY["api.health.edge_counts@1"], {}
        ).result_set
    }
    return {"nodes": nodes, "edges": edges}


def _as_of_bounds(graph: Any) -> tuple[int, int]:
    rows = graph.ro_query(QUERY_REGISTRY["meta.graph_bounds@1"], {}).result_set
    if not rows or rows[0][0] is None or rows[0][1] is None:
        raise ValueError("The selected graph contains no merged PR timestamps.")
    return int(rows[0][0]), int(rows[0][1])


def _proof_response(graph: Any, claim: Any) -> ProofResponse:
    proof = claim.proof_bundle
    subgraph = export_claim_subgraph(graph, claim)
    try:
        rendered = render_claim_proof(claim) if proof.get("facts") else ""
    except (KeyError, TypeError, ValueError):
        rendered = ""
    return ProofResponse(
        query_ids=proof.get("query_ids", []),
        params=proof.get("params", []),
        rendered_cypher=rendered,
        caveats=claim.caveats,
        subgraph=subgraph,
    )


def _ask_response(
    graph: Any,
    question: str,
    *,
    mode: str,
    tamper: int | None,
    llm_client: Any | None,
    max_calls: int | None,
    forced_mode: str | None = None,
) -> AskResponse:
    ignored_fact_ids: list[str] = []
    if mode == "deterministic":
        routed = run_question(graph, question, tamper_n=tamper)
        answer: VerifiedAnswer | None = routed.answer
        timings = routed.tool_timings
        unsupported = routed.unsupported_message
        provider = model = None
        calls = 0
        output_mode = "deterministic"
        tampered_fact_id = routed.tampered_fact_id
    else:
        result = run_agent(
            graph,
            question,
            client=llm_client,
            max_calls=max_calls,
            tamper_n=tamper,
        )
        answer = result.answer
        timings = result.tool_timings
        unsupported = result.unsupported_message
        provider = result.provider
        model = result.model
        calls = result.llm_calls
        output_mode = result.mode
        tampered_fact_id = result.tampered_fact_id
        ignored_fact_ids = result.ignored_fact_ids

    claims = [
        ClaimResponse(
            claim_id=claim.claim_id,
            text=claim.text,
            status=claim.status,
            reasons=claim.reasons,
            caveats=claim.caveats,
            proof=_proof_response(graph, claim),
        )
        for claim in (answer.claims if answer is not None else [])
    ]
    return AskResponse(
        answer=answer.final_answer if answer is not None else (unsupported or ""),
        claims=claims,
        tool_timings=timings,
        mode=forced_mode or output_mode,
        provider=provider,
        model=model,
        calls=calls,
        removed_claims=answer.removed_claims if answer is not None else [],
        ignored_fact_ids=ignored_fact_ids,
        tampered_fact_id=tampered_fact_id,
    )


def _safe_request_id(value: str | None) -> str:
    if value:
        normalized = re.sub(r"[^A-Za-z0-9._-]", "", value)[:64]
        if normalized:
            return normalized
    return uuid.uuid4().hex


def create_app(
    graph: Any | None = None,
    *,
    graph_name: str = DEFAULT_GRAPH,
    llm_client: Any | None = None,
    provider_probe: Any = llm_available,
    llm_rate_limit: int = 6,
    llm_rate_window_seconds: float = 60.0,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
    daily_budget_path: Path | None = None,
    deterministic_cache_ttl: float = 30.0,
) -> FastAPI:
    app = FastAPI(title="Provenance API", version="1.0.0")
    app.state.graph = graph
    app.state.database = None
    app.state.llm_client = llm_client
    app.state.graph_name = graph_name
    app.state.rate_limiter = _RateLimiter()
    budget_path = daily_budget_path or (ROOT / "data" / "cache" / "api" / "llm_daily.json")
    app.state.daily_budget = _DailyRequestBudget(daily_limit, budget_path)
    app.state.deterministic_cache = _DeterministicCache(deterministic_cache_ttl)
    app.state.provider_probe = provider_probe
    app.state.llm_rate_limit = max(1, llm_rate_limit)
    app.state.llm_rate_window_seconds = max(1.0, llm_rate_window_seconds)

    cors_origin = os.getenv("PROVENANCE_CORS_ORIGIN", "http://localhost:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[cors_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Request-ID"],
    )

    def get_graph() -> Any:
        if app.state.graph is None:
            if app.state.database is None:
                app.state.database = FalkorDB(
                    host=settings.FALKORDB_HOST,
                    port=settings.FALKORDB_PORT,
                )
            app.state.graph = app.state.database.select_graph(app.state.graph_name)
        return app.state.graph

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        request_id = _safe_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_id=%s method=%s path=%s status=%s elapsed_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        request_id = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid request.", "request_id": request_id},
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception):
        request_id = getattr(request.state, "request_id", "")
        logger.error("request_id=%s error_type=%s", request_id, type(error).__name__)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error.", "request_id": request_id},
        )

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        reachable = False
        counts: dict[str, dict[str, int]] = {"nodes": {}, "edges": {}}
        as_of_ts = None
        as_of_utc = None
        try:
            selected_graph = get_graph()
            counts = _graph_counts(selected_graph)
            _, as_of_ts = _as_of_bounds(selected_graph)
            as_of_utc = datetime.fromtimestamp(as_of_ts, timezone.utc).isoformat().replace("+00:00", "Z")
            reachable = True
        except Exception:
            pass
        try:
            statuses = [
                ProviderStatus(
                    provider=str(item.get("provider", "unknown")),
                    model=str(item.get("model", "unknown")),
                    available=bool(item.get("available", False)),
                )
                for item in app.state.provider_probe()
            ]
        except Exception:
            statuses = []
        return HealthResponse(
            graph=app.state.graph_name,
            graph_reachable=reachable,
            counts=counts,
            as_of_ts=as_of_ts,
            as_of_utc=as_of_utc,
            llm_providers=statuses,
        )

    @app.get("/api/tools", response_model=ToolsResponse)
    def tools() -> ToolsResponse:
        return ToolsResponse(tools=mcp_tool_definitions())

    @app.post("/api/ask", response_model=AskResponse)
    def ask(body: AskRequest, request: Request) -> AskResponse:
        selected_graph = get_graph()
        if body.mode == "deterministic":
            cache_key = body.question
            if body.tamper is None:
                cached = app.state.deterministic_cache.get(cache_key)
                if cached is not None:
                    return AskResponse.model_validate(cached)
            response = _ask_response(
                selected_graph,
                body.question,
                mode=body.mode,
                tamper=body.tamper,
                llm_client=None,
                max_calls=None,
            )
            if body.tamper is None:
                app.state.deterministic_cache.put(cache_key, response.model_dump(mode="json"))
            return response

        client_key = request.client.host if request.client else "unknown"
        if not app.state.rate_limiter.allow(
            client_key,
            app.state.llm_rate_limit,
            app.state.llm_rate_window_seconds,
        ):
            raise HTTPException(status_code=429, detail="LLM request rate limit exceeded.")
        reservation = app.state.daily_budget.reserve(3)
        if reservation == 0:
            return _ask_response(
                selected_graph,
                body.question,
                mode="deterministic",
                tamper=body.tamper,
                llm_client=None,
                max_calls=None,
                forced_mode="deterministic-fallback (llm budget exhausted)",
            )
        response: AskResponse | None = None
        try:
            response = _ask_response(
                selected_graph,
                body.question,
                mode=body.mode,
                tamper=body.tamper,
                llm_client=app.state.llm_client,
                max_calls=reservation,
            )
            return response
        finally:
            app.state.daily_budget.settle(
                reservation,
                response.calls if response is not None else 0,
            )

    @app.post("/api/tool/{name}", response_model=ToolResultResponse)
    def run_tool(name: str, body: ToolRequest) -> ToolResultResponse:
        if name not in TOOL_FUNCTIONS:
            raise HTTPException(status_code=404, detail="Unknown tool.")
        selected_graph = get_graph()
        try:
            args = validate_tool_arguments(name, body.args, selected_graph)
            result = TOOL_FUNCTIONS[name](selected_graph, **args)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid tool arguments.") from None
        return ToolResultResponse.model_validate(result.to_dict())

    @app.get("/api/risk", response_model=RiskResponse)
    def risk() -> RiskResponse:
        path = RESULTS / "risk_report.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Risk report is unavailable.")
        return RiskResponse.model_validate_json(path.read_text(encoding="utf-8"))

    @app.get("/api/scoreboard", response_model=ScoreboardResponse)
    def scoreboard() -> ScoreboardResponse:
        scoreboard_path = RESULTS / "scoreboard.json"
        rag_path = RESULTS / "rag_baseline.json"
        if not scoreboard_path.exists() or not rag_path.exists():
            raise HTTPException(status_code=404, detail="Evaluation reports are unavailable.")
        data = {
            "regime": "per-target as-of, pooled Folds B+C",
            "scoreboard": json.loads(scoreboard_path.read_text(encoding="utf-8")),
            "rag_baseline": json.loads(rag_path.read_text(encoding="utf-8")),
        }
        return ScoreboardResponse.model_validate(data)

    @app.get("/api/search/modules", response_model=ModulesSearchResponse)
    def search_modules(q: str = Query(min_length=1, max_length=100)) -> ModulesSearchResponse:
        rows = get_graph().ro_query(QUERY_REGISTRY["modules.list@1"], {}).result_set
        wanted = q.casefold()
        results = [
            ModuleHit(key=str(key), kind=str(kind), name=str(name), id=int(node_id))
            for key, kind, name, node_id in rows
            if wanted in str(key).casefold() or wanted in str(name).casefold()
        ][:10]
        return ModulesSearchResponse(results=results)

    @app.get("/api/search/people", response_model=PeopleSearchResponse)
    def search_people(q: str = Query(min_length=1, max_length=100)) -> PeopleSearchResponse:
        rows = get_graph().ro_query(
            QUERY_REGISTRY["api.search.people@1"], {"q": q}
        ).result_set
        return PeopleSearchResponse(
            results=[PersonHit(login=str(login), id=int(node_id)) for login, node_id in rows[:10]]
        )

    @app.get("/api/graph/neighborhood", response_model=NeighborhoodResponse)
    def neighborhood(
        module: str = Query(min_length=1, max_length=160),
        depth: int = Query(default=1, ge=1, le=1),
    ) -> NeighborhoodResponse:
        selected_graph = get_graph()
        module_key, suggestions = resolve_module(selected_graph, module)
        if module_key is None:
            raise HTTPException(status_code=404, detail="Module not found.")
        module_rows = selected_graph.ro_query(
            QUERY_REGISTRY["module.metadata@1"], {"module_key": module_key}
        ).result_set
        if not module_rows:
            raise HTTPException(status_code=404, detail="Module not found.")
        module_row = module_rows[0]
        module_hit = ModuleHit(
            key=str(module_row[0]),
            kind=str(module_row[1]),
            name=str(module_row[2]),
            id=int(module_row[3]),
        )
        owners_rows = selected_graph.ro_query(
            QUERY_REGISTRY["module.owners@1"], {"module_key": module_key}
        ).result_set
        owners = [
            OwnerHit(
                login=str(row[1]) if row[1] else None,
                handle=str(row[2]) if row[2] else None,
                is_bot=bool(row[3]),
                id=int(row[5]),
            )
            for row in owners_rows
        ]
        minimum_ts, as_of_ts = _as_of_bounds(selected_graph)
        activity_start = max(
            minimum_ts,
            as_of_ts - DEFAULT_TOOL_CONFIG.activity_window_days * 86400,
        )
        activity_rows = selected_graph.ro_query(
            QUERY_REGISTRY["ownership.drift.inputs@1"],
            {
                "module_key": module_key,
                "activity_start_ts": activity_start,
                "as_of_ts": as_of_ts,
            },
        ).result_set
        people: dict[str, dict[str, Any]] = {}
        for row in activity_rows:
            if row[0] != "activity" or not row[1] or row[4] not in {"author", "review"}:
                continue
            login = str(row[1]).casefold()
            person = people.setdefault(
                login,
                {
                    "id": int(row[6]),
                    "prs": set(),
                    "authored": set(),
                    "reviewed": set(),
                    "rows": [],
                },
            )
            pr_id = int(row[7])
            person["prs"].add(pr_id)
            person["rows"].append(row)
            person["reviewed" if row[4] == "review" else "authored"].add(pr_id)
        ranked_people = sorted(
            people.items(),
            key=lambda item: (
                -len(item[1]["prs"]),
                -len(item[1]["reviewed"]),
                item[0],
            ),
        )[:8]
        behavioral = [
            BehavioralPersonHit(
                login=login,
                authored_prs=len(record["authored"]),
                reviewed_prs=len(record["reviewed"]),
                id=record["id"],
            )
            for login, record in ranked_people
        ]

        dependent_rows = selected_graph.ro_query(
            QUERY_REGISTRY["api.neighborhood.dependents@1"],
            {"module_key": module_key},
        ).result_set
        dependents = dependent_rows[:30]
        dependent_hits = [
            DependentHit(key=str(row[0]), kind=str(row[1]), id=int(row[2]))
            for row in dependents
        ]

        node_ids = [module_hit.id, *(owner.id for owner in owners)]
        edge_ids = [int(row[6]) for row in owners_rows]
        for _, record in ranked_people:
            node_ids.append(record["id"])
            preferred = next(
                (row for row in record["rows"] if row[4] == "review"),
                record["rows"][0],
            )
            edge_ids.extend((int(preferred[8]), int(preferred[9])))
        for row in dependents:
            node_ids.extend((int(row[2]), int(row[4])))
            dependency_ids = row[3] if isinstance(row[3], (list, tuple)) else [row[3]]
            edge_ids.extend(int(edge_id) for edge_id in dependency_ids if edge_id is not None)
        subgraph = export_subgraph(
            selected_graph,
            {"node_ids": node_ids, "edge_ids": edge_ids},
        )
        if len(dependent_rows) > 30:
            subgraph["truncated"] = True
        return NeighborhoodResponse(
            module=module_hit,
            declared_owners=owners,
            behavioral_people=behavioral,
            direct_dependents=dependent_hits,
            as_of_ts=as_of_ts,
            subgraph=subgraph,
        )

    return app


app = create_app()