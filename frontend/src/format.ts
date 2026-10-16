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

export function truncatedLabel(
  truncated: boolean | undefined,
  total: number | undefined,
  returned: number | undefined,
): string {
  if (!truncated) return "complete";
  return `partial — showing ${returned ?? "?"} of ${total ?? "?"}`;
}
