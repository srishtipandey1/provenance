import { useMemo, useState } from "react";
import { api } from "../api";
import { truncatedLabel } from "../format";
import type { NeighborhoodResponse } from "../types";

// Deterministic radial layout from real returned nodes/edges only.
// Center = subject module; ring = owners, people, dependents.
export function GraphViz({ data }: { data: NeighborhoodResponse }) {
  const W = 560;
  const H = 340;
  const cx = W / 2;
  const cy = H / 2;

  const nodes = useMemo(() => {
    const ring = data.subgraph.nodes.filter((n) => n.id !== data.module.id).slice(0, 24);
    const byId = new Map(data.subgraph.nodes.map((n) => [n.id, n]));
    return { ring, byId };
  }, [data]);

  const edges = useMemo(() => {
    const ids = new Set(data.subgraph.nodes.map((n) => n.id));
    return data.subgraph.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  }, [data]);

  const pos = useMemo(() => {
    const m = new Map<number, { x: number; y: number }>();
    m.set(data.module.id, { x: cx, y: cy });
    nodes.ring.forEach((n, i) => {
      const a = (2 * Math.PI * i) / Math.max(1, nodes.ring.length);
      m.set(n.id, { x: cx + 200 * Math.cos(a), y: cy + 120 * Math.sin(a) });
    });
    return m;
  }, [data.module.id, nodes.ring, cx, cy]);

  if (nodes.ring.length === 0) return <p className="muted">No subgraph nodes returned.</p>;

  return (
    <div className="graph-wrap">
      <svg className="graph" width={W} height={H} role="img" aria-label="Module neighborhood graph">
        {edges.map((e) => {
          const a = pos.get(e.source);
          const b = pos.get(e.target);
          if (!a || !b) return null;
          return <line key={e.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#33465e" strokeWidth={1} />;
        })}
        {[{ id: data.module.id, display: data.module.key } as never, ...nodes.ring].map((n) => {
          const p = pos.get((n as { id: number }).id);
          if (!p) return null;
          const isCenter = (n as { id: number }).id === data.module.id;
          return (
            <g key={(n as { id: number }).id}>
              <circle cx={p.x} cy={p.y} r={isCenter ? 10 : 6} fill={isCenter ? "#5cc8ff" : "#94a3b8"} />
              <text x={p.x + 9} y={p.y + 4} fill="#dbe7f3" fontSize={10}>
                {((n as { display?: string }).display ?? "").slice(0, 26)}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="small muted">
        Showing {data.subgraph.node_count_returned} of {data.subgraph.node_count_total} nodes and{" "}
        {data.subgraph.edge_count_returned} of {data.subgraph.edge_count_total} edges —{" "}
        {truncatedLabel(data.subgraph.truncated, data.subgraph.edge_count_total, data.subgraph.edge_count_returned)}.
        Partial display, not the full evidence set.
      </p>
    </div>
  );
}

export function ImpactPanel() {
  const [module, setModule] = useState("integration:sensor");
  const [data, setData] = useState<NeighborhoodResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    const key = module.trim();
    if (!key || loading) return;
    setLoading(true);
    setError(null);
    try {
      setData(await api.neighborhood(key));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card" aria-label="Change impact exploration">
      <h2>Change-impact explorer</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <div className="row">
          <input
            type="text"
            aria-label="Module key"
            value={module}
            maxLength={160}
            onChange={(e) => setModule(e.target.value)}
            placeholder="integration:sensor"
            style={{ flex: 1, minWidth: 200 }}
          />
          <button type="submit" disabled={loading || !module.trim()}>
            {loading ? "Loading…" : "Explore"}
          </button>
        </div>
      </form>
      {error && (
        <p className="alert error" role="alert">
          Neighborhood failed: {error}
        </p>
      )}
      {!data && !error && <p className="muted small">Enter a module key to see owners, behavioral contributors, and dependents.</p>}
      {data && (
        <div>
          <h3>{data.module.key}</h3>
          <p className="small muted">
            {data.direct_dependents.length} direct dependents shown · {data.behavioral_people.length}{" "}
            behavioral contributors · declared owners {data.declared_owners.length}
          </p>
          <div className="grid-2">
            <div>
              <h3>Direct dependents</h3>
              <ul className="clean">
                {data.direct_dependents.slice(0, 10).map((d) => (
                  <li key={d.id}>{d.key}</li>
                ))}
              </ul>
            </div>
            <div>
              <h3>Behavioral contributors</h3>
              <ul className="clean">
                {data.behavioral_people.map((p) => (
                  <li key={p.id}>
                    {p.login}{" "}
                    <span className="muted small">
                      {p.authored_prs} authored · {p.reviewed_prs} reviewed
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
          <h3>Neighborhood graph</h3>
          <GraphViz data={data} />
        </div>
      )}
    </section>
  );
}
