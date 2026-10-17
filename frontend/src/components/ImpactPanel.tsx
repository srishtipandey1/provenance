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
  module: "var(--node-module)",
  person: "var(--node-person)",
  team: "var(--node-team)",
  other: "var(--node-other)",
};

const STROKE: Record<NodeKind, string> = {
  module: "var(--node-module)",
  person: "var(--node-person)",
  team: "var(--node-team)",
  other: "var(--node-other)",
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
    <section id="graph" className="section section-graph" aria-label="Change impact explorer">
      <div className="sec-head">
        <h2>
          <span className="sec-no">01</span>Impact explorer
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
  const [compact, setCompact] = useState(() => window.matchMedia("(max-width: 640px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 640px)");
    const update = () => setCompact(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  const W = compact ? 640 : 1600;
  const H = 620;
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
  const visibleNodes = compact ? nodes.rest.slice(0, 6) : nodes.rest;

  const edges = useMemo(() => {
    const ids = new Set(data.subgraph.nodes.map((n) => n.id));
    return data.subgraph.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  }, [data]);

  const pos = useMemo(() => {
    const m = new Map<number, { x: number; y: number; ring: number }>();
    m.set(data.module.id, { x: cx, y: cy, ring: 0 });
    const inner = visibleNodes.slice(0, compact ? 6 : 9);
    const outer = visibleNodes.slice(compact ? 6 : 9);
    inner.forEach((n, i) => {
      const a = (2 * Math.PI * i) / Math.max(1, inner.length) - Math.PI / 2;
      m.set(n.id, {
        x: cx + (compact ? 210 : 240) * Math.cos(a),
        y: cy + (compact ? 185 : 174) * Math.sin(a),
        ring: 1,
      });
    });
    outer.forEach((n, i) => {
      const a = (2 * Math.PI * i) / Math.max(1, outer.length) - Math.PI / 2 + 0.18;
      m.set(n.id, {
        x: cx + (compact ? 258 : 650) * Math.cos(a),
        y: cy + (compact ? 258 : 274) * Math.sin(a),
        ring: 2,
      });
    });
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compact, data.module.id, visibleNodes]);

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
    const fullLabel = ("display" in n ? n.display : full?.display) ?? "";
    const shortLabel = compact && kind === "module"
      ? fullLabel.slice(fullLabel.lastIndexOf(":") + 1)
      : fullLabel;
    const labelLimit = compact ? 6 : 24;
    const label = shortLabel.length > labelLimit
      ? `${shortLabel.slice(0, labelLimit - 1)}…`
      : shortLabel;
    const labelOnLeft = p.x > (compact ? cx : W * 0.68);
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
          <rect x={p.x - 10} y={p.y - 10} width={20} height={20} fill={FILL.module} stroke={isSel ? "var(--graph-ink)" : FILL.module} strokeWidth={isSel ? 3 : 1} />
        ) : kind === "team" ? (
          <path
            d={`M ${p.x} ${p.y - 9} L ${p.x + 9} ${p.y} L ${p.x} ${p.y + 9} L ${p.x - 9} ${p.y} Z`}
            fill={isSel ? "var(--graph-ink)" : FILL.team}
            stroke={STROKE.team}
            strokeWidth={1.5}
          />
        ) : (
          <circle
            cx={p.x}
            cy={p.y}
            r={kind === "person" ? 8 : 6}
            fill={kind === "other" ? FILL.other : FILL[kind]}
            stroke={isSel ? "var(--graph-ink)" : STROKE[kind]}
            strokeWidth={isSel ? 2.5 : 1.5}
          />
        )}
        <text
          x={p.x + (labelOnLeft ? -13 : 13)}
          y={p.y + 5}
          textAnchor={labelOnLeft ? "end" : "start"}
          fontSize={compact ? 28 : 17}
          className="node-label"
          fill="var(--graph-ink)"
        >
          <title>{fullLabel}</title>
          {label}
        </text>
      </g>
    );
  }

  return (
    <div>
      <p className="small" style={{ margin: "12px 0 0" }}>
        <strong className="graph-module-key">{data.module.key}</strong>{" "}
        <span className="muted mono small">
          {data.direct_dependents.length} dependents · {data.behavioral_people.length}{" "}
          behavioral · {data.declared_owners.length} declared owners
        </span>
      </p>
      <div className="graph-frame">
        <div className="graph-legend">
          <span><span className="dot" style={{ background: "var(--node-module)" }} />module (■ subject)</span>
          <span><span className="dot" style={{ background: "var(--node-person)", borderRadius: "50%" }} />person</span>
          <span><span className="dot" style={{ background: "var(--node-team)" }} />team</span>
          <span><span className="dot" style={{ background: "var(--node-other)", borderRadius: "50%" }} />other</span>
          <span style={{ marginLeft: "auto" }}>hover isolates · click pins</span>
        </div>
        <svg className="ledger-graph" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Module neighborhood graph">
          <defs>
            <radialGradient id="subject-glow">
              <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.18" />
              <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
            </radialGradient>
          </defs>
          <circle cx={cx} cy={cy} r={compact ? 220 : 320} fill="url(#subject-glow)" />
          <ellipse cx={cx} cy={cy} rx={compact ? 138 : 240} ry={compact ? 132 : 174} fill="none" stroke="var(--graph-ring)" strokeDasharray="4 7" />
          <ellipse cx={cx} cy={cy} rx={compact ? 258 : 650} ry={compact ? 258 : 274} fill="none" stroke="var(--graph-ring)" strokeDasharray="4 7" />
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
                stroke={active ? "var(--graph-edge)" : "var(--graph-edge-dim)"}
                strokeWidth={active ? 1.8 : 1}
                opacity={active ? 0.9 : 0.58}
              />
            );
          })}
          {renderNode({ id: data.module.id, display: data.module.key }, true)}
          {visibleNodes.filter((n) => pos.has(n.id)).map((n) => renderNode(n, false))}
        </svg>
        <div className="graph-cap">
          {truncatedLabel(data.subgraph.truncated, data.subgraph.edge_count_total, data.subgraph.edge_count_returned)}{" "}
          — nodes {data.subgraph.node_count_returned}/{data.subgraph.node_count_total},
          edges {data.subgraph.edge_count_returned}/{data.subgraph.edge_count_total}.{" "}
          {data.subgraph.note} Partial display, not the full evidence set.
          {compact && (
            <> Mobile labels {visibleNodes.length + 1}/{data.subgraph.nodes.length} · tap a node to pin its full key.</>
          )}
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
