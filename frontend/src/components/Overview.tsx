import { useEffect, useState } from "react";
import { api } from "../api";
import { formatTs, sumCounts } from "../format";
import type { HealthResponse, RiskReport, Scoreboard } from "../types";

function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    fn()
      .then((d) => alive && setData(d))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading };
}

export function Overview() {
  const health = useAsync<HealthResponse>(api.health);
  const risk = useAsync<RiskReport>(api.risk);
  const score = useAsync<Scoreboard>(api.scoreboard);

  const topModules = Array.isArray(risk.data?.top_15_modules)
    ? risk.data.top_15_modules.slice(0, 8)
    : [];
  const topPersonLoss = Array.isArray(risk.data?.single_person_risks)
    ? risk.data.single_person_risks[0]
    : undefined;
  const methods = score.data?.scoreboard?.pooled_BC?.methods;
  const methodNames = methods ? Object.keys(methods) : [];

  return (
    <section className="card" aria-label="Overview dashboard">
      <h2>Overview</h2>
      {health.loading && <p className="spin">Loading graph health…</p>}
      {health.error && (
        <p className="alert error" role="alert">
          Health check failed: {health.error}. Is the backend running at /api?
        </p>
      )}
      {health.data && (
        <div className="grid-2">
          <div className="metric">
            <div className="value">{health.data.graph_reachable ? "Reachable" : "Down"}</div>
            <div className="label">FalkorDB graph “{health.data.graph}”</div>
          </div>
          <div className="metric">
            <div className="value">{sumCounts(health.data.counts?.nodes).toLocaleString()}</div>
            <div className="label">nodes · {sumCounts(health.data.counts?.edges).toLocaleString()} edges</div>
          </div>
          <div className="metric">
            <div className="value small">{formatTs(health.data.as_of_ts)}</div>
            <div className="label">latest merged-PR timestamp</div>
          </div>
          <div className="metric">
            <div className="value small">
              {(health.data.llm_providers ?? []).filter((p) => p.available).length}/
              {(health.data.llm_providers ?? []).length} providers
            </div>
            <div className="label">LLM availability (probes only)</div>
          </div>
        </div>
      )}

      <h3>Top-risk modules (GET /api/risk)</h3>
      {risk.loading && <p className="spin">Loading risk report…</p>}
      {risk.error && (
        <p className="alert error" role="alert">Risk report failed: {risk.error}</p>
      )}
      {!risk.loading && !risk.error && topModules.length === 0 && (
        <p className="muted">No risk rows returned.</p>
      )}
      {topModules.length > 0 && (
        <ul className="clean">
          {topModules.map((m) => (
            <li key={m.module_key}>
              <strong>{m.module_key}</strong>{" "}
              <span className="muted small">
                score {m.risk_score.toFixed(3)} · {m.dependent_count} dependents · drift{" "}
                {m.ownership_drift.toFixed(2)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <h3>Reviewer scoreboard, pooled B+C (GET /api/scoreboard)</h3>
      {topPersonLoss && (
        <p className="small muted">
          Largest single-maintainer exposure: {topPersonLoss.person_login} —{" "}
          {topPersonLoss.uncovered_module_count} modules uncovered,{" "}
          {topPersonLoss.distinct_dependents_affected} distinct dependents affected.
        </p>
      )}
      {score.loading && <p className="spin">Loading scoreboard…</p>}
      {score.error && (
        <p className="alert error" role="alert">Scoreboard failed: {score.error}</p>
      )}
      {!score.loading && !score.error && methodNames.length === 0 && (
        <p className="muted">No scoreboard methods returned.</p>
      )}
      {methodNames.length > 0 && (
        <table className="score">
          <thead>
            <tr>
              <th>method</th>
              <th>hit@3</th>
              <th>MRR</th>
            </tr>
          </thead>
          <tbody>
            {methodNames.map((name) => (
              <tr key={name}>
                <td>{name}</td>
                <td>{methods?.[name]?.["hit@3"]?.toFixed(3) ?? "—"}</td>
                <td>{methods?.[name]?.["MRR"]?.toFixed(3) ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
