import { describe, expect, it } from "vitest";
import { buildModulesUrl, buildNeighborhoodUrl, buildPeopleUrl } from "./api";
import { clamp01, formatDate, isVerifiedStatus, pct01, statusTone, sumCounts, truncatedLabel } from "./format";

describe("api url builders", () => {
  it("encodes query params", () => {
    expect(buildModulesUrl("integration:sensor")).toBe(
      "/api/search/modules?q=integration%3Asensor",
    );
    expect(buildPeopleUrl("epenet")).toBe("/api/search/people?q=epenet");
    expect(buildNeighborhoodUrl("integration:sensor")).toBe(
      "/api/graph/neighborhood?module=integration%3Asensor&depth=1",
    );
  });
});

describe("verification helpers", () => {
  it("only VERIFIED counts as verified", () => {
    expect(isVerifiedStatus("VERIFIED")).toBe(true);
    expect(isVerifiedStatus("verified")).toBe(true);
    expect(isVerifiedStatus("UNSUPPORTED")).toBe(false);
    expect(isVerifiedStatus(null)).toBe(false);
  });

  it("maps status to tone", () => {
    expect(statusTone("VERIFIED")).toBe("ok");
    expect(statusTone("CONTRADICTED")).toBe("bad");
    expect(statusTone("whatever")).toBe("idle");
  });

  it("sums counts defensively", () => {
    expect(sumCounts({ Person: 3, Module: 2 })).toBe(5);
    expect(sumCounts(null)).toBe(0);
  });

  it("labels truncation", () => {
    expect(truncatedLabel(false, 10, 10)).toBe("complete");
    expect(truncatedLabel(true, 3810, 224)).toContain("224");
  });

  it("formats ledger helpers", () => {
    expect(formatDate(1728000000)).toBe("2024-10-04");
    expect(formatDate(null)).toBe("—");
    expect(pct01(0.5441)).toBe("54%");
    expect(pct01(null)).toBe("—");
    expect(clamp01(2)).toBe(1);
    expect(clamp01(-1)).toBe(0);
    expect(clamp01(0.5)).toBe(0.5);
  });
});
