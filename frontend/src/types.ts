// Response shapes mirror provenance/api/app.py StrictModels.
// All fields the UI reads are optional-tolerant: the backend may
// omit them, so every accessor defends with fallbacks.

export interface ProviderStatus {
  provider: string;
  model: string;
  available: boolean;
}

export interface HealthResponse {
  graph: string;
  graph_reachable: boolean;
  counts: { nodes: Record<string, number>; edges: Record<string, number> };
  as_of_ts: number | null;
  as_of_utc: string | null;
  llm_providers: ProviderStatus[];
}

export interface SubgraphNode {
  id: number;
  label: string;
  key?: string | null;
  display: string;
  props: Record<string, unknown>;
}

export interface SubgraphEdge {
  id: number;
  type: string;
  source: number;
  target: number;
  props: Record<string, unknown>;
}

export interface Subgraph {
  nodes: SubgraphNode[];
  edges: SubgraphEdge[];
  node_count_total: number;
  edge_count_total: number;
  node_count_returned: number;
  edge_count_returned: number;
  truncated: boolean;
  note: string;
}

export interface ClaimProof {
  query_ids: string[];
  params: Record<string, unknown>[];
  rendered_cypher: string;
  caveats: string[];
  subgraph: Subgraph;
}

export interface Claim {
  claim_id: string;
  text: string;
  status: string;
  reasons: string[];
  caveats: string[];
  proof: ClaimProof;
}

export interface AskResponse {
  answer: string;
  claims: Claim[];
  tool_timings: Record<string, unknown>[];
  mode: string;
  provider: string | null;
  model: string | null;
  calls: number;
  removed_claims: Record<string, unknown>[];
  ignored_fact_ids: string[];
  tampered_fact_id?: string | null;
}

export interface ModuleHit {
  key: string;
  kind: string;
  name: string;
  id: number;
}

export interface PersonHit {
  login: string;
  id: number;
}

export interface NeighborhoodResponse {
  module: ModuleHit;
  declared_owners: {
    login?: string | null;
    handle?: string | null;
    is_bot: boolean;
    id: number;
  }[];
  behavioral_people: {
    login: string;
    authored_prs: number;
    reviewed_prs: number;
    id: number;
  }[];
  direct_dependents: { key: string; kind: string; id: number }[];
  as_of_ts: number;
  subgraph: Subgraph;
}

// Risk report rows (results/risk_report.json via GET /api/risk).
export interface RiskModule {
  module_key: string;
  risk_score: number;
  dependent_count: number;
  page_rank: number;
  ownership_drift: number;
  bus_factor_share: number;
  ownerless: boolean;
  declared_owners?: { identity: string; kind: string }[];
  top_behavioral_contributor?: {
    login: string;
    activity_prs: number;
    authored_prs: number;
    reviewed_prs: number;
  } | null;
}

export interface RiskReport {
  graph?: string;
  as_of_ts?: number;
  top_15_modules?: RiskModule[];
  single_person_risks?: {
    person_login: string;
    uncovered_module_count: number;
    distinct_dependents_affected: number;
  }[];
  [key: string]: unknown;
}

export interface Scoreboard {
  regime?: string;
  scoreboard?: {
    methods?: string[];
    n?: number;
    pooled_BC?: {
      n?: number;
      methods?: Record<string, Record<string, number>>;
    };
  };
  [key: string]: unknown;
}
