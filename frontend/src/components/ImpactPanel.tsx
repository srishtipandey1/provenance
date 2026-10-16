import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { truncatedLabel } from "../format";
import type { NeighborhoodResponse, SubgraphEdge, SubgraphNode } from "../types";

type NodeKind = "module" | "person" | "team" | "other";

function kindOf(n: SubgraphNode): NodeKind {
  const l = (n.label ?? "").toLowerCase();
  if (l.includes("person")) return "person";
  if (l.includes("team")) return "team";
  if (l.includes("module")) return "module";
  return "other";
}

const FILL: Record<NodeKind, string> = {
  module: "#1b1710",
  person: "#1e5b4a",
  team: "#7a5f14",
  other: "#ffffff",
};

const STROKE: Record<NodeKind, string> = {
  module: "#1b1710",
  person: "#1e5b4a",
  team: "#7a5f14",
  other: "#8a8171",
};

export function ImpactPanel() {
  const [module, setModule] = useState("integration:sensor");
  const [data, setData] = useState<NeighborhoodResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  async function run(key: string) {
    const k = key.trim();
    if (!k || loading) return;
    setLoading(true);
    setError(null);
    setSelected(null);
    try {
      setData(await api.neighborhood(k));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const handler = (ev: Event) => {
      const key = (ev as CustomEvent<string>).detail;
      if (typeof key === "string" && key) {
        setModule(key);
        void run(key);
      }
    };
    window.addEventListener("provenance:select-module", handler);
    return () => window.removeEventListener("provenance:select-module", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void run("integration:sensor");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section className="section" aria-label="Change impact explorer">
      <div className="sec-head">
        <h2>
          <span className="sec-no">03</span>Impact explorer
        </h2>
        <span className="hint">owners · behavior · dependents</span>
      </div>
      <div className="sec-body">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run(module);
          }}
        >
          <div className="impact-input">
            <input
              type="text"
              className="field"
              aria-label="Module key"
              value={module}
              maxLength={160}
              onChange={(e) => setModule(e.target.value)}
              placeholder="integration:sensor"
            />
            <button type="submit" className="btn" disabled={loading || !module.trim()}>
              {loading ? "…" : "Trace →"}
            </button>
          </div>
        </form>
        {error && (
          <p className="alert" role="alert">
            Neighborhood failed: {error}
          </p>
        )}
        {!data && !error && (
          <p className="muted small">Tracing declared owners, behavioral contributors, dependents.</p>
        )}
        {data && (
          <ImpactGraph
            key={data.module.key}
            data={data}
            selected={selected}
            onSelect={setSelected}
          />
        )}
      </div>
    </section>
  );
}

function ImpactGraph({
  data,
  selected,
  onSelect,
}: {
  data: NeighborhoodResponse;
  selected: number | null;
  onSelect: (id: number | null) => void;
}) {
  const W = 640;
  const H = 400;
  const cx = W / 2;
  const cy = H / 2;

  const nodes = useMemo(() => {
    // Stable deterministic order: subject first, then by node id.
    const rest = [...data.subgraph.nodes]
      .filter((n) => n.id !== data.module.id)
      .sort((a, b) => a.id - b.id)
      .slice(0, 26);
    const byId = new Map<number, SubgraphNode>(
      data.subgraph.nodes.map((n) => [n.id, n]),
    );
    return { rest, byId };
  }, [data]);

  const edges = useMemo(() => {
    const ids = new Set(data.subgraph.nodes.map((n) => n.id));
    return data.subgraph.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  }, [data]);

  const pos = useMemo(() => {
    const m = new Map<number, { x: number; y: number; ring: number }>();
    m.set(data.module.id, { x: cx, y: cy, ring: 0 });
    const inner = nodes.rest.slice(0, 9);
    const outer = nodes.rest.slice(9);
    inner.forEach((n, i) => {
      const a = (2 * Math.PI * i) / Math.max(1, inner.length) - Math.PI / 2;
      m.set(n.id, { x: cx + 118 * Math.cos(a), y: cy + 92 * Math.sin(a), ring: 1 });
    });
    outer.forEach((n, i) => {
      const a = (2 * Math.PI * i) / Math.max(1, outer.length) - Math.PI / 2 + 0.18;
      m.set(n.id, { x: cx + 238 * Math.cos(a), y: cy + 158 * Math.sin(a), ring: 2 });
    });
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.module.id, nodes.rest]);

  const [hover, setHover] = useState<number | null>(null);
  const focus = hover ?? selected;
  const linked = useMemo(() => {
    if (focus == null) return null;
    const s = new Set<number>([focus]);
    for (const e of edges) {
      if (e.source === focus) s.add(e.target);
      if (e.target === focus) s.add(e.source);
    }
    return s;
  }, [edges, focus]);

  const selectedNode = selected != null ? nodes.byId.get(selected) : undefined;

  function renderNode(n: SubgraphNode | { id: number; display: string }, center: boolean) {
    const p = pos.get(n.id);
    if (!p) return null;
    const full = nodes.byId.get(n.id);
    const kind: NodeKind = full ? kindOf(full) : "module";
    const dim = linked && !linked.has(n.id);
    const isSel = selected === n.id;
    const label = (("display" in n ? n.display : full?.display) ?? "").slice(0, 24);
    return (
      <g
        key={n.id}
        opacity={dim ? 0.28 : 1}
        onMouseEnter={() => setHover(n.id)}
        onMouseLeave={() => setHover(null)}
        onClick={() => onSelect(isSel ? null : n.id)}
        style={{ cursor: "pointer" }}
      >
        {center ? (
          <rect x={p.x - 9} y={p.y - 9} width={18} height={18} fill={FILL.module} stroke={isSel ? "#b3400f" : FILL.module} strokeWidth={isSel ? 3 : 1} />
        ) : kind === "team" ? (
          <path
            d={`M ${p.x} ${p.y - 8} L ${p.x + 8} ${p.y} L ${p.x} ${p.y + 8} L ${p.x - 8} ${p.y} Z`}
            fill={isSel ? "#b3400f" : FILL.team}
            stroke={isSel ? "#b3400f" : STROKE.team}
            strokeWidth={1.5}
          />
        ) : (
          <circle
            cx={p.x}
            cy={p.y}
            r={kind === "person" ? 7 : 5}
            fill={kind === "other" ? FILL.other : FILL[kind]}
            stroke={isSel ? "#b3400f" : STROKE[kind]}
            strokeWidth={isSel ? 2.5 : 1.5}
          />
        )}
        <text x={p.x + 11} y={p.y + 4} fontSize={9.5} className="node-label" fill="#1b1710">
          {label}
        </text>
      </g>
    );
  }

  return (
    <div>
      <p className="small" style={{ margin: "12px 0 0" }}>
        <strong style={{ fontFamily: "Georgia, serif", fontSize: "1.05rem" }}>{data.module.key}</strong>{" "}
        <span className="muted mono small">
          {data.direct_dependents.length} dependents · {data.behavioral_people.length}{" "}
          behavioral · {data.declared_owners.length} declared owners
        </span>
      </p>
      <div className="graph-frame">
        <div className="graph-legend">
          <span><span className="dot" style={{ background: "#1b1710" }} />module (■ subject)</span>
          <span><span className="dot" style={{ background: "#1e5b4a", borderRadius: "50%" }} />person</span>
          <span><span className="dot" style={{ background: "#7a5f14" }} />team</span>
          <span><span className="dot" style={{ background: "#fff", border: "1px solid #8a8171", borderRadius: "50%" }} />other</span>
          <span style={{ marginLeft: "auto" }}>hover isolates · click pins</span>
        </div>
        <svg className="ledger-graph" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Module neighborhood graph">
          <ellipse cx={cx} cy={cy} rx={118} ry={92} fill="none" stroke="#d9cfb4" strokeDasharray="3 4" />
          <ellipse cx={cx} cy={cy} rx={238} ry={158} fill="none" stroke="#d9cfb4" strokeDasharray="3 4" />
          {edges.map((e: SubgraphEdge) => {
            const a = pos.get(e.source);
            const b = pos.get(e.target);
            if (!a || !b) return null;
            const active = focus == null || e.source === focus || e.target === focus;
            return (
              <line
                key={e.id}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke={active ? "#1b1710" : "#cfc4a6"}
                strokeWidth={active ? 1.2 : 0.7}
                opacity={active ? 0.75 : 0.5}
              />
            );
          })}
          {renderNode({ id: data.module.id, display: data.module.key }, true)}
          {nodes.rest.filter((n) => pos.has(n.id)).map((n) => renderNode(n, false))}
        </svg>
        <div className="graph-cap">
          {truncatedLabel(data.subgraph.truncated, data.subgraph.edge_count_total, data.subgraph.edge_count_returned)}{" "}
          — nodes {data.subgraph.node_count_returned}/{data.subgraph.node_count_total},
          edges {data.subgraph.edge_count_returned}/{data.subgraph.edge_count_total}.{" "}
          {data.subgraph.note} Partial display, not the full evidence set.
          {selectedNode && (
            <> Pinned: {selectedNode.display} ({selectedNode.label}).</>
          )}
        </div>
      </div>
      <div className="kv">
        <div>
          <h3>Direct dependents ≤10</h3>
          <ul className="plain">
            {data.direct_dependents.slice(0, 10).map((d) => (
              <li key={d.id} className="mono small">{d.key}</li>
            ))}
            {data.direct_dependents.length === 0 && <li className="muted small">None returned.</li>}
          </ul>
        </div>
        <div>
          <h3>Behavioral contributors</h3>
          <ul className="plain">
            {data.behavioral_people.map((p) => (
              <li key={p.id}>
                <span className="mono small">{p.login}</span>{" "}
                <span className="muted small">{p.authored_prs}a · {p.reviewed_prs}r</span>
              </li>
            ))}
            {data.behavioral_people.length === 0 && <li className="muted small">None returned.</li>}
          </ul>
        </div>
      </div>
    </div>
  );
}
