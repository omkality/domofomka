import assert from "node:assert/strict";
import test from "node:test";
import { auditDecision } from "../audit-dependencies.mjs";

const deferred = {
  name: "picomatch",
  severity: "high",
  url: "https://github.com/advisories/GHSA-fc5q-39xc-vjpf",
};
const report = (vulnerabilities) => ({ auditReportVersion: 2, vulnerabilities });

test("allows only the affected package and its dependency chains", () => {
  const result = auditDecision(report({
    picomatch: { severity: "high", via: [deferred] },
    tool: { severity: "high", via: ["picomatch"] },
  }));
  assert.deepEqual(result, { blocked: [], deferred: ["picomatch", "tool"] });
});

test("blocks a second advisory even through the same dependency chain", () => {
  const result = auditDecision(report({
    picomatch: { severity: "high", via: [deferred, { ...deferred, url: "different-advisory", severity: "moderate" }] },
    tool: { severity: "high", via: ["picomatch"] },
    separate: { severity: "moderate", via: [{ url: "another-advisory" }] },
    noise: { severity: "low", via: [{ url: "minor-advisory" }] },
  }));
  assert.deepEqual(result, { blocked: ["picomatch", "tool", "separate"], deferred: [] });
});

test("blocks unknown causes and rejects failed audit responses", () => {
  assert.deepEqual(auditDecision(report({ tool: { severity: "high", via: ["missing"] } })),
    { blocked: ["tool"], deferred: [] });
  assert.throws(() => auditDecision({ error: { code: "E500" } }), /valid vulnerability report/);
});
