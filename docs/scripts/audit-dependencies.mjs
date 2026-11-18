import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const deferredAdvisory = "https://github.com/advisories/GHSA-fc5q-39xc-vjpf";

function onlyDeferredAdvisory(name, vulnerabilities) {
  const pending = [name];
  const visited = new Set();
  let foundAdvisory = false;
  while (pending.length) {
    const current = pending.pop();
    if (visited.has(current)) continue;
    visited.add(current);
    const via = vulnerabilities[current]?.via;
    if (!Array.isArray(via) || !via.length) return false;
    for (const cause of via) {
      if (typeof cause === "string") {
        pending.push(cause);
      } else if (cause?.url === deferredAdvisory && cause.name === "picomatch" && cause.severity === "high") {
        foundAdvisory = true;
      } else {
        return false;
      }
    }
  }
  return foundAdvisory;
}

export function auditDecision(report) {
  const vulnerabilities = report.vulnerabilities;
  if (
    report.error || report.auditReportVersion !== 2 ||
    !vulnerabilities || typeof vulnerabilities !== "object" || Array.isArray(vulnerabilities)
  ) {
    throw new Error("npm did not return a valid vulnerability report");
  }
  const blocked = [];
  const deferred = [];
  for (const [name, entry] of Object.entries(vulnerabilities)) {
    if (entry?.severity === "low" || entry?.severity === "info") continue;
    if (entry?.severity === "high" && onlyDeferredAdvisory(name, vulnerabilities)) {
      deferred.push(name);
    } else {
      blocked.push(name);
    }
  }
  return { blocked, deferred };
}

// Documentation builds use repository-controlled glob patterns and ship static
// files. Remove this exception when compatible patched packages are available.
function main() {
  const result = spawnSync(process.execPath, [process.env.npm_execpath, "audit", "--audit-level=moderate", "--json"], {
    encoding: "utf8",
  });
  if (result.error) throw result.error;
  const { blocked, deferred } = auditDecision(JSON.parse(result.stdout));
  if (blocked.length || (result.status !== 0 && (result.status !== 1 || !deferred.length))) {
    console.error(result.stdout);
    console.error(result.stderr);
    process.exitCode = 1;
    return;
  }
  if (deferred.length) {
    console.warn(`Temporarily allowed for the static docs build: ${deferredAdvisory} (${deferred.length} affected entries)`);
  }
  console.log("Documentation dependency audit passed at moderate severity.");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
