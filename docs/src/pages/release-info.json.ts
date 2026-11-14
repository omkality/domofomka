import type { APIRoute } from "astro";
import releases from "../generated/releases.json";

export const GET: APIRoute = () => new Response(JSON.stringify({
  tag: releases.latest.tag,
  url: releases.latest.url,
  publishedAt: releases.latest.publishedAt,
  sourceRevision: releases.sourceRevision,
  sourceRef: releases.sourceRef,
  generatedAt: releases.generatedAt,
}), { headers: { "Content-Type": "application/json" } });
