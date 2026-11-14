import assert from "node:assert/strict";
import { test } from "node:test";
import { loadReleases } from "../releases.mjs";

const stable = { tag_name: "v2.0.2", name: "Release", html_url: "https://github.com/omkality/domofomka/releases/tag/v2.0.2",
  published_at: "2026-11-14T15:01:27Z", body: "Release notes", draft: false, prerelease: false };

test("read the canonical latest release and omit drafts and prereleases", async () => {
  const calls = [];
  const result = await loadReleases(async (url, options) => {
    calls.push(url);
    assert.equal(options.headers.Authorization, "Bearer test-only");
    return { ok: true, json: async () => url.endsWith("latest") ? stable : [stable, { ...stable, draft: true }, { ...stable, prerelease: true }] };
  }, "test-only");
  assert.equal(result.latest.tag, "v2.0.2");
  assert.equal(result.history.length, 1);
  assert.equal(calls.length, 2);
});

test("do not silently publish old data when GitHub is unavailable", async () => {
  await assert.rejects(loadReleases(async () => ({ ok: false, status: 503 }), ""), /HTTP 503/);
});

test("validate release links before placing them in the public site", async () => {
  await assert.rejects(loadReleases(async () => ({ ok: true, json: async () => ({ ...stable, html_url: "https://example.com/" }) }), ""), /Unexpected release URL/);
});

test("follow pagination instead of truncating published history", async () => {
  const result = await loadReleases(async (url) => ({ ok: true, json: async () => {
    if (url.endsWith("latest")) return stable;
    return url.endsWith("page=1") ? Array.from({ length: 100 }, (_, i) => ({ ...stable, tag_name: `v1.0.${i}` })) : [stable];
  } }), "");
  assert.equal(result.history.length, 101);
});
