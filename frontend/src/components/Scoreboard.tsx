import { useEffect, useState } from "react";
import { api } from "../api";
import type { Scoreboard } from "../types";

const ORDER = ["B3", "G1", "G3", "G3b", "RAG", "B1", "B2"];

export function ScoreboardPanel() {
  const [score, setScore] = useState<Scoreboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    api
      .scoreboard()
      .then((s) => alive && setScore(s))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const methods = score?.scoreboard?.pooled_BC?.methods;
  const names = methods ? ORDER.filter((n) => n in methods) : [];
  const n = score?.scoreboard?.pooled_BC?.n;

  return (
    <section className="section" aria-label="Reviewer scoreboard">
      <div className="sec-head">
        <h2>
          <span className="sec-no">05</span>Reviewer scoreboard
        </h2>
        <span className="hint">pooled B+C{n ? ` · n=${n}` : ""}</span>
      </div>
      <div className="sec-body">
        {loading && <p className="spin">loading scoreboard…</p>}
        {error && (
          <p className="alert" role="alert">
            Scoreboard failed: {error}
          </p>
        )}
        {!loading && !error && names.length === 0 && (
          <p className="muted">No scoreboard methods returned.</p>
        )}
        {names.length > 0 && (
          <table className="score">
            <thead>
              <tr>
                <th>method</th>
                <th>hit@3</th>
                <th>MRR</th>
              </tr>
            </thead>
            <tbody>
              {names.map((name) => (
                <tr key={name} style={name === "G3" ? { background: "#eef3ea" } : undefined}>
                  <td className="m">{name}{name === "G3" && " ★"}</td>
                  <td>{methods?.[name]?.["hit@3"]?.toFixed(3) ?? "—"}</td>
                  <td>{methods?.[name]?.["MRR"]?.toFixed(3) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="small muted" style={{ marginBottom: 0 }}>
          Frozen per-PR as-of regime. G3 ★ improves top-K over B3/G1; B3 keeps a narrow
          hit@1 edge. Full CIs in results/reviewer_eval_round2.json.
        </p>
      </div>
    </section>
  );
}
