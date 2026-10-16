import { useEffect, useState } from "react";
import { api } from "./api";
import { formatDate } from "./format";
import type { HealthResponse } from "./types";
import { AskPanel } from "./components/AskPanel";
import { RiskAtlas } from "./components/Overview";
import { ImpactPanel } from "./components/ImpactPanel";
import { SearchPanel } from "./components/SearchPanel";
import { ScoreboardPanel } from "./components/Scoreboard";

const TRACKS = [
  "Agents That Act on Connected Data",
  "Agent Memory and Coordination",
  "Company Brain",
];

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .health()
      .then((h) => {
        if (alive) setHealth(h);
      })
      .catch(() => {
        if (alive) setHealth(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  const reachable = health?.graph_reachable ?? null;

  return (
    <>
      <header className="mast">
        <div className="mast-top">
          <div className="wordmark">
            PROVENANCE<span className="no">fig. 1 — declared vs. behaved</span>
          </div>
          <div className="mast-meta">
            <div>
              {reachable === null ? (
                <span className="pill">checking graph…</span>
              ) : reachable ? (
                <span className="pill live">● falkordb reachable</span>
              ) : (
                <span className="pill down">○ graph down</span>
              )}
            </div>
            <div>
              {health ? (
                <>
                  {health.graph} · as-of {formatDate(health.as_of_ts)} ·{" "}
                  {(health.llm_providers ?? []).filter((p) => p.available).length}/
                  {(health.llm_providers ?? []).length} llm probes
                </>
              ) : (
                <>home-assistant/core · 180-day merged-PR window</>
              )}
            </div>
          </div>
        </div>
        <div className="hero">
          <div>
            <div className="kicker">
              <span className="rule" />
              FalkorDB computes the gap · the LLM only narrates proof
            </div>
            <h1>
              Declared vs. <em>behaved.</em>
            </h1>
            <p className="lede">
              Every organization has an org chart it claims and a contribution graph it
              earns. Provenance overlays CODEOWNERS, manifests and decision records
              against who actually authors, reviews, and breaks what — and every claim
              below replays a read-only FalkorDB query. Nothing here is vibes.
            </p>
            <div className="tracks">
              {TRACKS.map((t) => (
                <span key={t} className="track">
                  {t}
                </span>
              ))}
            </div>
          </div>
          <LedgerStrip />
        </div>
      </header>

      <main className="page">
        <div className="col">
          <AskPanel />
          <RiskAtlas onSelectModule={selectModule} />
        </div>
        <div className="col">
          <ImpactPanel />
          <SearchPanel />
          <ScoreboardPanel />
        </div>
      </main>

      <footer className="foot">
        <div className="foot-inner">
          <span>same-origin /api → 127.0.0.1:8000 · read-only GRAPH.RO_QUERY</span>
          <span>subgraph displays are partial when truncated · only VERIFIED counts</span>
        </div>
      </footer>
    </>
  );
}

function selectModule(key: string) {
  window.dispatchEvent(new CustomEvent("provenance:select-module", { detail: key }));
}

function LedgerStrip() {
  // Hard ledger figures from the frozen 180-day graph snapshot.
  const cells: [string, string][] = [
    ["10,285", "merged PRs"],
    ["1,629", "modules"],
    ["1,497", "people"],
    ["455", "decisions"],
  ];
  return (
    <div className="ledger" aria-label="Graph census">
      {cells.map(([n, l]) => (
        <div key={l} className="ledger-cell">
          <div className="n">{n}</div>
          <div className="l">{l}</div>
        </div>
      ))}
    </div>
  );
}
