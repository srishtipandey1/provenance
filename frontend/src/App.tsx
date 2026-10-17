import { useEffect, useState } from "react";
import { api } from "./api";
import { formatDate } from "./format";
import type { HealthResponse } from "./types";
import { AskPanel } from "./components/AskPanel";
import { RiskAtlas } from "./components/Overview";
import { ImpactPanel } from "./components/ImpactPanel";
import { SearchPanel } from "./components/SearchPanel";
import { ScoreboardPanel } from "./components/Scoreboard";

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [activeSection, setActiveSection] = useState("graph");

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

  useEffect(() => {
    const sections = [...document.querySelectorAll<HTMLElement>(".page section[id]")];
    const updateActiveSection = () => {
      const marker = window.innerHeight * 0.35 + 70;
      const current =
        [...sections].reverse().find((section) => section.getBoundingClientRect().top <= marker)?.id ??
        sections[0]?.id ??
        "graph";
      setActiveSection((previous) => (previous === current ? previous : current));
    };
    updateActiveSection();
    window.addEventListener("scroll", updateActiveSection, { passive: true });
    window.addEventListener("resize", updateActiveSection);
    return () => {
      window.removeEventListener("scroll", updateActiveSection);
      window.removeEventListener("resize", updateActiveSection);
    };
  }, []);

  const reachable = health?.graph_reachable ?? null;

  return (
    <>
      <header className="mast">
        <div className="mast-top">
          <div className="wordmark">
            PROVENANCE<span className="no">GRAPH INTELLIGENCE</span>
          </div>
        </div>
        <div className="hero">
          <div className="hero-copy">
            <div className="kicker">
              LIVE GRAPH · HOME ASSISTANT CORE
            </div>
            <h1>Declared vs. behaved.</h1>
            <p className="lede">
              FalkorDB reveals the gap between code ownership and contribution behavior; every claim replays against graph evidence.
            </p>
          </div>
          <div className="hero-data">
            <div className="mast-meta">
              {reachable === null ? (
                <span className="pill">checking graph…</span>
              ) : reachable ? (
                <span className="pill live">● falkordb reachable</span>
              ) : (
                <span className="pill down">○ graph down</span>
              )}
              <span>
                {health
                  ? `${health.graph} · as-of ${formatDate(health.as_of_ts)}`
                  : "home-assistant/core · 180-day merged-PR window"}
              </span>
            </div>
            <LedgerStrip />
          </div>
        </div>
      </header>

      <nav className="section-nav" aria-label="Dashboard sections">
        {[
          ["graph", "Graph"],
          ["ask", "Ask"],
          ["risk", "Risk"],
          ["search", "Search"],
          ["evidence", "Evidence"],
        ].map(([id, label]) => (
          <a
            key={id}
            href={`#${id}`}
            className={activeSection === id ? "active" : undefined}
            aria-current={activeSection === id ? "location" : undefined}
          >
            {label}
          </a>
        ))}
      </nav>

      <main className="page">
        <ImpactPanel />
        <AskPanel />
        <RiskAtlas onSelectModule={selectModule} />
        <div className="secondary-grid">
          <SearchPanel />
          <ScoreboardPanel />
        </div>
      </main>

      <footer className="foot">
        <div className="foot-inner">
          <span>/api · GRAPH.RO_QUERY</span>
          <span>partial subgraphs when truncated · only VERIFIED counts</span>
        </div>
      </footer>
    </>
  );
}

function selectModule(key: string) {
  window.dispatchEvent(new CustomEvent("provenance:select-module", { detail: key }));
}

function LedgerStrip() {
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
