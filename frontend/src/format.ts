// Pure helpers (unit-tested with vitest, no DOM needed).

export function isVerifiedStatus(status: unknown): boolean {
  return typeof status === "string" && status.toUpperCase() === "VERIFIED";
}

export function statusTone(status: unknown): "ok" | "bad" | "idle" {
  const s = typeof status === "string" ? status.toUpperCase() : "";
  if (s === "VERIFIED") return "ok";
  if (s === "UNSUPPORTED" || s === "CONTRADICTED") return "bad";
  return "idle";
}

export function sumCounts(
  counts: Record<string, number> | undefined | null,
): number {
  if (!counts) return 0;
  return Object.values(counts).reduce(
    (acc, v) => acc + (typeof v === "number" && Number.isFinite(v) ? v : 0),
    0,
  );
}

export function formatTs(ts: number | null | undefined): string {
  if (typeof ts !== "number" || !Number.isFinite(ts)) return "—";
  try {
    return new Date(ts * 1000).toISOString().replace("+00:00", "Z");
  } catch {
    return "—";
  }
}

/** Short UTC date (YYYY-MM-DD) for masthead display. */
export function formatDate(ts: number | null | undefined): string {
  if (typeof ts !== "number" || !Number.isFinite(ts)) return "—";
  try {
    return new Date(ts * 1000).toISOString().slice(0, 10);
  } catch {
    return "—";
  }
}

export function truncatedLabel(
  truncated: boolean | undefined,
  total: number | undefined,
  returned: number | undefined,
): string {
  if (!truncated) return "complete";
  return `partial — showing ${returned ?? "?"} of ${total ?? "?"}`;
}

/** 0..1 → "0–100" percentage string with no decimals. */
export function pct01(x: unknown): string {
  if (typeof x !== "number" || !Number.isFinite(x)) return "—";
  return `${Math.round(x * 100)}%`;
}

/** Clamp a score to 0..1 for bar widths. */
export function clamp01(x: unknown): number {
  if (typeof x !== "number" || !Number.isFinite(x)) return 0;
  return Math.min(1, Math.max(0, x));
}
