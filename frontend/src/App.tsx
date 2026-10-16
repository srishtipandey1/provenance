import { Overview } from "./components/Overview";
import { AskPanel } from "./components/AskPanel";
import { SearchPanel } from "./components/SearchPanel";
import { ImpactPanel } from "./components/ImpactPanel";

export default function App() {
  return (
    <>
      <header className="app-header">
        <h1>Provenance — engineering impact dashboard</h1>
        <p>
          Declared ownership vs. behavioral contribution, computed by FalkorDB and verified
          claim-by-claim. Only claims with status VERIFIED count as verified.
        </p>
      </header>
      <main className="layout">
        <div>
          <Overview />
          <SearchPanel />
        </div>
        <div>
          <AskPanel />
          <ImpactPanel />
        </div>
      </main>
      <footer className="foot">
        Backend: same-origin /api (dev proxy → http://127.0.0.1:8000). No secrets in frontend
        code. Subgraph displays are partial when truncated.
      </footer>
    </>
  );
}
