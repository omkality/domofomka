import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const project = path.resolve(fileURLToPath(new URL("../../", import.meta.url)));
const content = path.join(project, "docs/src/content/docs");

// Исходники читаются и в GitHub: относительные ссылки остаются обычными Markdown-ссылками.
// Только при сборке сайта переводим ссылки на страницы в маршруты Starlight.
export function documentationLinks({ base = "/" } = {}) {
  return (tree, file) => {
    function visit(node) {
      if ((node.type === "link" || node.type === "definition") && node.url && !/^(?:[a-z]+:|\/|#)/i.test(node.url)) {
        const [filename, fragment] = node.url.split("#", 2);
        const resolved = path.resolve(path.dirname(file.path), decodeURIComponent(filename));
        if (/\.mdx?$/.test(filename) && resolved.startsWith(content + path.sep) && existsSync(resolved)) {
          const slug = path.relative(content, resolved).replaceAll(path.sep, "/").replace(/\.mdx?$/, "");
          node.url = `${base.replace(/\/$/, "")}/${slug === "index" ? "" : slug + "/"}${fragment ? "#" + fragment : ""}`;
        } else if (resolved.startsWith(project + path.sep) && existsSync(resolved)) {
          const relative = path.relative(project, resolved).replaceAll(path.sep, "/");
          node.url = `https://github.com/omkality/domofomka/blob/dev/${relative}${fragment ? "#" + fragment : ""}`;
        }
      }
      for (const child of node.children || []) visit(child);
    }
    visit(tree);
  };
}
