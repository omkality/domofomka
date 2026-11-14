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
for (const slug of ["index", "architecture", "processes", "database", "architecture-audit", "cicd"]) {
  const file = path.join(output, slug === "index" ? "index.html" : `${slug}/index.html`);
  if (!(await readFile(file, "utf8")).includes("domofomka")) throw new Error(`Нет страницы ${slug}`);
}
let checkedLinks = 0;
const base = (process.env.DOCS_BASE_PATH || "/").replace(/\/$/, "");
for (const file of built.filter((item) => item.endsWith(".html"))) {
  const html = await readFile(file, "utf8");
  for (const match of html.matchAll(/<a\b[^>]*\bhref="([^"]+)"/g)) {
    const href = match[1];
    if (/^(?:[a-z]+:|\/\/|#)/i.test(href)) continue;
    let pathname = decodeURIComponent(href.split(/[?#]/, 1)[0]);
    if (!pathname) continue;
    if (base && pathname.startsWith(base + "/")) pathname = pathname.slice(base.length);
    const target = pathname.startsWith("/") ? path.join(output, pathname) : path.resolve(path.dirname(file), pathname);
    try {
      const info = await stat(target);
      if (info.isDirectory()) await stat(path.join(target, "index.html"));
    } catch {
      throw new Error(`Неработающая ссылка ${path.relative(output, file)} → ${href}`);
    }
    checkedLinks++;
  }
}
console.log(JSON.stringify({ static_files: built.length, checked_links: checkedLinks, bytes, budget_bytes: limit, server_node_required: false }));
