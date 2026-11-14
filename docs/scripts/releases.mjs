import { mkdir, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const repository = "omkality/domofomka";
const directory = fileURLToPath(new URL("../src/generated/", import.meta.url));

function release(value) {
  if (!value || value.draft || value.prerelease || typeof value.tag_name !== "string" || !value.published_at) {
    throw new Error("GitHub returned an invalid stable release");
  }
  const url = new URL(value.html_url);
  if (url.origin !== "https://github.com" || !url.pathname.startsWith(`/${repository}/releases/tag/`)) {
    throw new Error("Unexpected release URL");
  }
  return { tag: value.tag_name, name: value.name || value.tag_name, url: url.href,
    publishedAt: value.published_at, body: value.body || "" };
}

export async function loadReleases(fetcher = fetch, token = process.env.GITHUB_TOKEN) {
  const headers = { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" };
  if (token) headers.Authorization = `Bearer ${token}`;
  async function read(route) {
    const response = await fetcher(`https://api.github.com/repos/${repository}/${route}`, {
      headers, signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error(`GitHub releases unavailable: HTTP ${response.status}`);
    return response.json();
  }
  const latest = release(await read("releases/latest"));
  const history = [];
  for (let page = 1; page <= 10; page++) {
    const values = await read(`releases?per_page=100&page=${page}`);
    if (!Array.isArray(values)) throw new Error("Invalid GitHub releases list");
    history.push(...values.filter((value) => !value.draft && !value.prerelease).map(release));
    if (values.length < 100) break;
    if (page === 10) throw new Error("Release history exceeds the reviewed site budget");
  }
  if (!history.some((item) => item.tag === latest.tag)) throw new Error("Latest release missing from history");
  history.sort((a, b) => b.publishedAt.localeCompare(a.publishedAt));
  return { latest, history };
}

export async function syncReleases() {
  const data = await loadReleases();
  const sourceRevision = execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  const sourceRef = execFileSync("git", ["branch", "--show-current"], { encoding: "utf8" }).trim() || process.env.GITHUB_REF_NAME || "detached";
  const result = { repository, ...data, sourceRevision, sourceRef, generatedAt: new Date().toISOString() };
  await mkdir(directory, { recursive: true });
  await writeFile(`${directory}/releases.json`, JSON.stringify(result, null, 2) + "\n");
  console.log(`Releases: ${data.latest.tag}, ${data.history.length} published versions; docs ${sourceRevision.slice(0, 7)}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await syncReleases();
}
