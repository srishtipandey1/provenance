import { describe, expect, it } from "vitest";
import { buildModulesUrl, buildNeighborhoodUrl, buildPeopleUrl } from "./api";
import { isVerifiedStatus, statusTone, sumCounts, truncatedLabel } from "./format";

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
});
