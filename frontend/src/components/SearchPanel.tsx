import { useState } from "react";
import { api } from "../api";
import type { ModuleHit, PersonHit } from "../types";

export function SearchPanel() {
  const [q, setQ] = useState("sensor");
  const [modules, setModules] = useState<ModuleHit[]>([]);
  const [people, setPeople] = useState<PersonHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  async function run() {
    const query = q.trim();
    if (!query || loading) return;
    setLoading(true);
    setError(null);
    try {
      const [m, p] = await Promise.all([
        api.searchModules(query),
        api.searchPeople(query),
      ]);
      setModules(m);
      setPeople(p);
      setSearched(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  function loadModule(key: string) {
    window.dispatchEvent(new CustomEvent("provenance:select-module", { detail: key }));
  }

  return (
    <section className="section" aria-label="Module and person search">
      <div className="sec-head">
        <h2>
          <span className="sec-no">04</span>Index search
        </h2>
        <span className="hint">/api/search/modules · /api/search/people</span>
      </div>
      <div className="sec-body">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run();
          }}
        >
          <div className="impact-input">
            <input
              type="text"
              className="field"
              aria-label="Search query"
              value={q}
              maxLength={100}
              onChange={(e) => setQ(e.target.value)}
              placeholder="e.g. sensor, epenet"
            />
            <button type="submit" className="btn" disabled={loading || !q.trim()}>
              {loading ? "…" : "Find →"}
            </button>
          </div>
        </form>
        {error && (
          <p className="alert" role="alert">
            Search failed: {error}
          </p>
        )}
        <div className="kv">
          <div>
            <h3>Modules ({modules.length})</h3>
            {(!searched || modules.length === 0) && (
              <p className="muted small">{searched ? "No module matches." : "Not searched yet."}</p>
            )}
            <ul className="plain">
              {modules.map((m) => (
                <li key={m.id}>
                  <button type="button" className="linklike mono small" onClick={() => loadModule(m.key)} title="Trace in impact explorer →">
                    <strong>{m.key}</strong>
                  </button>{" "}
                  <span className="muted small">{m.kind}</span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <h3>People ({people.length})</h3>
            {(!searched || people.length === 0) && (
              <p className="muted small">{searched ? "No person matches." : "Not searched yet."}</p>
            )}
            <ul className="plain">
              {people.map((p) => (
                <li key={p.id} className="mono small">{p.login}</li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </section>
  );
}
