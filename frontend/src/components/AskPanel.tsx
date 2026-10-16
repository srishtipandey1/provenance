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
  const [copied, setCopied] = useState<string | null>(null);

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

  const verified = (resp?.claims ?? []).filter(
    (c) => c.status.toUpperCase() === "VERIFIED",
  ).length;

  return (
    <section className="section" aria-label="Ask the graph">
      <div className="sec-head">
        <h2>
          <span className="sec-no">01</span>Ask the graph
        </h2>
        <span className="hint">deterministic · GRAPH.RO_QUERY only</span>
      </div>
      <div className="sec-body">
        <ol className="ex-list">
          {EXAMPLES.map((ex, i) => (
            <li
              key={ex}
              onClick={() => {
                setQuestion(ex);
                void submit(ex);
              }}
            >
              <span className="idx">{String(i + 1).padStart(2, "0")}</span>
              <button
                type="button"
                className="linklike q"
                onClick={(e) => {
                  e.stopPropagation();
                  setQuestion(ex);
                  void submit(ex);
                }}
              >
                {ex}
              </button>
              <span className="go">run →</span>
            </li>
          ))}
        </ol>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit(question);
          }}
        >
          <textarea
            className="ask-box"
            aria-label="Question"
            rows={2}
            maxLength={300}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. what is the risk of changing sensor?"
          />
          <div className="ask-row">
            <span className="count">{question.trim().length}/300 · mode deterministic</span>
            <button type="submit" className="btn" disabled={loading || !question.trim()}>
              {loading ? "Querying FalkorDB…" : "Ask →"}
            </button>
          </div>
        </form>

        {loading && <p className="spin">replaying registered tool queries…</p>}
        {error && (
          <p className="alert" role="alert">
            Ask failed: {error}. Is the API running at /api?
          </p>
        )}

        {resp && (
          <div>
            <div className="answer">
              {resp.answer || <span className="muted">No verified answer returned.</span>}
              <div className="small mono muted" style={{ marginTop: 6 }}>
                {verified}/{resp.claims.length} verified · {resp.removed_claims.length}{" "}
                removed by verifier · mode {resp.mode}
              </div>
            </div>
            {resp.removed_claims.length > 0 && (
              <p className="alert warn">
                {resp.removed_claims.length} claim(s) failed replay and are withheld —
                only VERIFIED claims count.
              </p>
            )}
            {resp.claims.map((c) => {
              const tone = statusTone(c.status);
              const sg = c.proof?.subgraph;
              return (
                <article key={c.claim_id} className="claim">
                  <div className="claim-top">
                    <span className={`stamp ${tone}`}>{c.status}</span>
                    <span className="claim-id">{c.claim_id}</span>
                    <span className="claim-id" style={{ marginLeft: "auto" }}>
                      {(c.proof?.query_ids ?? []).join(" · ") || "no query"}
                    </span>
                  </div>
                  <p className="text">{c.text}</p>
                  {(c.caveats?.length > 0 || c.reasons?.length > 0) && (
                    <p className="sub">
                      {c.caveats?.length > 0 && <>Caveats: {c.caveats.join("; ")}. </>}
                      {c.reasons?.length > 0 && <>Checks: {c.reasons.join("; ")}.</>}
                    </p>
                  )}
                  <details className="proof">
                    <summary>
                      proof · {(c.proof?.query_ids ?? []).join(", ") || "no query"}
                      {sg
                        ? ` · ${truncatedLabel(sg.truncated, sg.edge_count_total, sg.edge_count_returned)}`
                        : ""}
                    </summary>
                    <div className="proof-inner">
                      {c.proof?.caveats?.length > 0 && (
                        <p className="proof-note">
                          Snapshot caveat: {c.proof.caveats.join("; ")}
                        </p>
                      )}
                      {sg && (
                        <p className="proof-note">
                          Subgraph: nodes {sg.node_count_returned}/{sg.node_count_total},
                          edges {sg.edge_count_returned}/{sg.edge_count_total}.{" "}
                          {sg.note}
                        </p>
                      )}
                      {c.proof?.rendered_cypher && (
                        <>
                          <pre className="cypher">{c.proof.rendered_cypher}</pre>
                          <button
                            type="button"
                            className="copy-btn"
                            onClick={() => {
                              void navigator.clipboard
                                ?.writeText(c.proof.rendered_cypher)
                                .then(() => {
                                  setCopied(c.claim_id);
                                  window.setTimeout(() => setCopied(null), 1200);
                                })
                                .catch(() => {});
                            }}
                          >
                            {copied === c.claim_id ? "copied ✓" : "copy cypher"}
                          </button>
                        </>
                      )}
                    </div>
                  </details>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
