// Small typed API client. Same-origin relative URLs so the Vite
// dev proxy (and any same-origin deployment) forwards /api to FastAPI.
import type {
  AskResponse,
  HealthResponse,
  ModuleHit,
  NeighborhoodResponse,
  PersonHit,
  RiskReport,
  Scoreboard,
} from "./types";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    throw new Error(`GET ${path} failed (${res.status})`);
  }
  return (await res.json()) as T;
}

export function buildModulesUrl(q: string): string {
  return `/api/search/modules?q=${encodeURIComponent(q)}`;
}

export function buildPeopleUrl(q: string): string {
  return `/api/search/people?q=${encodeURIComponent(q)}`;
}

export function buildNeighborhoodUrl(module: string): string {
  return `/api/graph/neighborhood?module=${encodeURIComponent(module)}&depth=1`;
}

export const api = {
  health: () => getJson<HealthResponse>("/api/health"),
  tools: () => getJson<{ tools: unknown[] }>("/api/tools"),
  risk: () => getJson<RiskReport>("/api/risk"),
  scoreboard: () => getJson<Scoreboard>("/api/scoreboard"),
  searchModules: async (q: string): Promise<ModuleHit[]> => {
    const data = await getJson<{ results: ModuleHit[] }>(buildModulesUrl(q));
    return Array.isArray(data.results) ? data.results : [];
  },
  searchPeople: async (q: string): Promise<PersonHit[]> => {
    const data = await getJson<{ results: PersonHit[] }>(buildPeopleUrl(q));
    return Array.isArray(data.results) ? data.results : [];
  },
  neighborhood: (module: string) =>
    getJson<NeighborhoodResponse>(buildNeighborhoodUrl(module)),
  ask: async (question: string): Promise<AskResponse> => {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ question, mode: "deterministic" }),
    });
    if (!res.ok) {
      throw new Error(`POST /api/ask failed (${res.status})`);
    }
    const data = (await res.json()) as AskResponse;
    // Tolerate malformed payloads: panels assume arrays, never undefined.
    if (!Array.isArray(data.claims)) data.claims = [];
    if (!Array.isArray(data.removed_claims)) data.removed_claims = [];
    if (!Array.isArray(data.tool_timings)) data.tool_timings = [];
    if (!Array.isArray(data.ignored_fact_ids)) data.ignored_fact_ids = [];
    return data;
  },
};
