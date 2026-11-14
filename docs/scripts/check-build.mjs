import { readdir, stat, readFile } from "node:fs/promises";
import path from "node:path";

const output = path.resolve("dist");
async function files(dir) {
  const result = [];
  for (const item of await readdir(dir, { withFileTypes: true })) {
    const file = path.join(dir, item.name);
    result.push(...(item.isDirectory() ? await files(file) : [file]));
  }
  return result;
}
const built = await files(output);
const bytes = (await Promise.all(built.map(async (file) => (await stat(file)).size))).reduce((a, b) => a + b, 0);
const limit = 32 * 1024 * 1024;
if (bytes > limit) throw new Error(`Документация превышает бюджет 32 МиБ: ${bytes} байт`);
for (const slug of ["index", "architecture", "processes", "database", "releases", "research/architecture-audit", "cicd"]) {
  const file = path.join(output, slug === "index" ? "index.html" : `${slug}/index.html`);
  if (!(await readFile(file, "utf8")).includes("domofomka")) throw new Error(`Нет страницы ${slug}`);
}
const releaseInfo = JSON.parse(await readFile(path.join(output, "release-info.json"), "utf8"));
const generated = JSON.parse(await readFile("src/generated/releases.json", "utf8"));
if (releaseInfo.tag !== generated.latest.tag || releaseInfo.sourceRevision !== generated.sourceRevision) {
  throw new Error("Release metadata differs from the checked build");
}
for (const slug of ["index", "database", "releases"]) {
  const html = await readFile(path.join(output, slug === "index" ? "index.html" : `${slug}/index.html`), "utf8");
  if (!html.includes(`data-release-tag="${releaseInfo.tag}"`) || !html.includes(`data-docs-revision="${releaseInfo.sourceRevision}"`)) {
    throw new Error(`Missing current release or source revision on ${slug}`);
  }
}
let checkedLinks = 0;
let checkedAnchors = 0;
let checkedAssets = 0;
const errors = [];
const ids = new Map();
const base = (process.env.DOCS_BASE_PATH || "/").replace(/\/$/, "");
for (const file of built.filter((item) => item.endsWith(".html"))) {
  const html = await readFile(file, "utf8");
  for (const tag of html.matchAll(/<(?:script|link|img|astro-island)\b[^>]*>/g)) {
    for (const match of tag[0].matchAll(/\b(?:href|src|component-url|renderer-url|before-hydration-url)="([^"]+)"/g)) {
      const href = match[1];
      if (/^(?:[a-z]+:|\/\/|#)/i.test(href)) continue;
      let pathname = decodeURIComponent(href.split(/[?#]/, 1)[0]);
      if (base && pathname.startsWith("/") && !pathname.startsWith(base + "/")) {
        errors.push(`Ресурс вне базового пути ${path.relative(output, file)} → ${href}`);
        continue;
      }
      if (base && pathname.startsWith(base + "/")) pathname = pathname.slice(base.length);
      const target = pathname.startsWith("/") ? path.join(output, pathname) : path.resolve(path.dirname(file), pathname);
      try {
        if (!(await stat(target)).isFile()) throw new Error("Ресурс не является файлом");
      } catch {
        errors.push(`Отсутствует ресурс ${path.relative(output, file)} → ${href}`);
      }
      checkedAssets++;
    }
  }
  for (const match of html.matchAll(/<a\b[^>]*\bhref="([^"]+)"/g)) {
    const href = match[1];
    if (/^(?:[a-z]+:|\/\/)/i.test(href)) continue;
    let pathname = decodeURIComponent(href.split(/[?#]/, 1)[0]);
    if (base && pathname.startsWith(base + "/")) pathname = pathname.slice(base.length);
    let target = !pathname ? file : pathname.startsWith("/") ? path.join(output, pathname) : path.resolve(path.dirname(file), pathname);
    try {
      const info = await stat(target);
      if (info.isDirectory()) {
        target = path.join(target, "index.html");
        await stat(target);
      }
      const fragment = href.includes("#") ? decodeURIComponent(href.split("#", 2)[1]) : "";
      if (fragment && target.endsWith(".html")) {
        if (!ids.has(target)) ids.set(target, new Set([...(await readFile(target, "utf8")).matchAll(/\bid="([^"]+)"/g)].map((item) => item[1])));
        if (!ids.get(target).has(fragment)) errors.push(`Нет раздела ${path.relative(output, file)} → ${href}`);
        checkedAnchors++;
      }
    } catch {
      errors.push(`Неработающая ссылка ${path.relative(output, file)} → ${href}`);
    }
    checkedLinks++;
  }
}
if (errors.length) throw new Error(errors.join("\n"));
console.log(JSON.stringify({ static_files: built.length, checked_links: checkedLinks, checked_anchors: checkedAnchors, checked_assets: checkedAssets, bytes, budget_bytes: limit, server_node_required: false }));
