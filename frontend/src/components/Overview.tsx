import { useEffect, useState } from "react";
import { api } from "../api";
import { clamp01 } from "../format";
import type { RiskReport } from "../types";

export function RiskAtlas({ onSelectModule }: { onSelectModule: (key: string) => void }) {
  const [risk, setRisk] = useState<RiskReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    api
      .risk()
      .then((r) => alive && setRisk(r))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const rows = Array.isArray(risk?.top_15_modules)
    ? [...risk.top_15_modules]
        .sort((a, b) => (b.risk_score ?? 0) - (a.risk_score ?? 0))
    : [];
  const exposure = Array.isArray(risk?.single_person_risks) ? risk.single_person_risks[0] : undefined;
  const maxScore = Math.max(0.01, ...rows.map((r) => r.risk_score ?? 0));

  return (
    <section className="section" aria-label="Risk atlas">
      <div className="sec-head">
        <h2>
          <span className="sec-no">02</span>Risk atlas
        </h2>
        <span className="hint">GET /api/risk · bus 0.35 / drift 0.30 / rank 0.20 / deps 0.15</span>
      </div>
      <div className="sec-body">
        {loading && <p className="spin">loading risk report…</p>}
        {error && (
          <p className="alert" role="alert">
            Risk report failed: {error}
          </p>
        )}
        {!loading && !error && rows.length === 0 && (
          <p className="muted">No risk rows returned.</p>
        )}
        {rows.length > 0 && (
          <table className="risk-table">
            <thead>
              <tr>
                <th>#</th>
                <th>module</th>
                <th>risk</th>
                <th>score</th>
                <th>deps</th>
                <th>drift</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m, i) => (
                <tr
                  key={m.module_key}
                  className="clickable"
                  title="Load in change-impact explorer →"
                  onClick={() => onSelectModule(m.module_key)}
                >
                  <td className="rank">{String(i + 1).padStart(2, "0")}</td>
                  <td>
                    <span className="modkey">{m.module_key}</span>
                    {m.ownerless && <span className="tag-ownerless">ownerless</span>}
                  </td>
                  <td>
                    <div className="bar">
                      <span style={{ width: `${(clamp01(m.risk_score / maxScore) * 100).toFixed(0)}%` }} />
                    </div>
                  </td>
                  <td className="num">{(m.risk_score ?? 0).toFixed(3)}</td>
                  <td className="num">{m.dependent_count ?? "—"}</td>
                  <td className="num">{(m.ownership_drift ?? 0).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="small muted" style={{ marginBottom: 0 }}>
          {exposure ? (
            <>
              Largest single-maintainer exposure: <strong>{exposure.person_login}</strong>{" "}
              — {exposure.uncovered_module_count} modules uncovered,{" "}
              {exposure.distinct_dependents_affected} distinct dependents.{" "}
            </>
          ) : null}
          Click a row to inspect it in the impact explorer →
        </p>
      </div>
    </section>
  );
}
