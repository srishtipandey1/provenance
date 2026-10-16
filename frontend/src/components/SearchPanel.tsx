import { useState } from "react";
import { api } from "../api";
import type { ModuleHit, PersonHit } from "../types";

export function SearchPanel() {
  const [q, setQ] = useState("sensor");
  const [modules, setModules] = useState<ModuleHit[]>([]);
  const [people, setPeople] = useState<PersonHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card" aria-label="Module and person search">
      <h2>Search modules &amp; people</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <div className="row">
          <input
            type="text"
            aria-label="Search query"
            value={q}
            maxLength={100}
            onChange={(e) => setQ(e.target.value)}
            placeholder="e.g. sensor, epenet"
            style={{ flex: 1, minWidth: 200 }}
          />
          <button type="submit" disabled={loading || !q.trim()}>
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
      </form>
      {error && (
        <p className="alert error" role="alert">
          Search failed: {error}
        </p>
      )}
      <div className="grid-2">
        <div>
          <h3>Modules ({modules.length})</h3>
          {modules.length === 0 && <p className="muted small">No module results yet.</p>}
          <ul className="clean">
            {modules.map((m) => (
              <li key={m.id}>
                <strong>{m.key}</strong> <span className="muted small">{m.kind}</span>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h3>People ({people.length})</h3>
          {people.length === 0 && <p className="muted small">No person results yet.</p>}
          <ul className="clean">
            {people.map((p) => (
              <li key={p.id}>{p.login}</li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
