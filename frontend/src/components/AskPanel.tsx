import { useState } from "react";
import { api } from "../api";
import { statusTone, truncatedLabel } from "../format";
import type { AskResponse } from "../types";

const EXAMPLES = [
  "what is the risk of changing sensor?",
  "what happens if epenet leaves?",
  "who should review PR 179264?",
];

export function AskPanel() {
  const [question, setQuestion] = useState(EXAMPLES[0]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resp, setResp] = useState<AskResponse | null>(null);

  async function submit(q: string) {
    const trimmed = q.trim();
    if (!trimmed || loading) return;
    setLoading(true);
    setError(null);
    try {
      setResp(await api.ask(trimmed.slice(0, 300)));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResp(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card" aria-label="Natural language query">
      <h2>Ask the graph</h2>
      <p className="muted small">
        Deterministic mode only. Claims are machine-checked; only claims with status VERIFIED
        count as verified.
      </p>
      <div className="row" aria-label="Example questions">
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            className="ghost example-btn"
            type="button"
            onClick={() => {
              setQuestion(ex);
              void submit(ex);
            }}
          >
            {ex}
          </button>
        ))}
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit(question);
        }}
      >
        <label htmlFor="ask-q" className="small muted">
          Question (max 300 chars)
        </label>
        <textarea
          id="ask-q"
          rows={2}
          maxLength={300}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <div className="row" style={{ marginTop: 8 }}>
          <button type="submit" disabled={loading || !question.trim()}>
            {loading ? "Asking…" : "Ask"}
          </button>
          {resp && (
            <span className="muted small">
              mode {resp.mode} · {resp.claims.length} claim(s) · {resp.removed_claims.length}{" "}
              removed
            </span>
          )}
        </div>
      </form>
      {loading && <p className="spin">Running verified tools against FalkorDB…</p>}
      {error && (
        <p className="alert error" role="alert">
          Ask failed: {error}
        </p>
      )}
      {resp && (
        <div style={{ marginTop: 8 }}>
          <h3>Answer</h3>
          <p>{resp.answer || <span className="muted">No verified answer returned.</span>}</p>
          {resp.removed_claims.length > 0 && (
            <p className="alert warn">
              {resp.removed_claims.length} claim(s) were removed by the verifier and are not
              shown as verified.
            </p>
          )}
          <h3>Claims ({resp.claims.length})</h3>
          {resp.claims.length === 0 && <p className="muted">No claims.</p>}
          {resp.claims.map((c) => {
            const tone = statusTone(c.status);
            const sg = c.proof?.subgraph;
            return (
              <article key={c.claim_id} className="claim">
                <span className={`badge ${tone}`}>{c.status}</span>{" "}
                <strong className="small">{c.claim_id}</strong>
                <p>{c.text}</p>
                {c.caveats?.length > 0 && (
                  <p className="small muted">Caveats: {c.caveats.join("; ")}</p>
                )}
                {c.reasons?.length > 0 && (
                  <p className="small muted">Checks: {c.reasons.join("; ")}</p>
                )}
                <details>
                  <summary className="small">Proof ({c.proof?.query_ids?.join(", ") || "no query"})</summary>
                  {c.proof?.caveats?.length > 0 && (
                    <p className="small muted">Proof caveats: {c.proof.caveats.join("; ")}</p>
                  )}
                  {sg && (
                    <p className="small muted">
                      Subgraph: {truncatedLabel(sg.truncated, sg.edge_count_total, sg.edge_count_returned)}{" "}
                      (nodes {sg.node_count_returned}/{sg.node_count_total}, edges{" "}
                      {sg.edge_count_returned}/{sg.edge_count_total}). {sg.note}
                    </p>
                  )}
                  {c.proof?.rendered_cypher && (
                    <pre className="cypher">{c.proof.rendered_cypher}</pre>
                  )}
                </details>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
