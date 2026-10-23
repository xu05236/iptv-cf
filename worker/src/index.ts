/**
 * iptv-cf Worker
 * 单一通道设计：
 *   - 公开出口：订阅文件（KV + Cache API，60s TTL）
 *   - 内部 API（Bearer ADMIN_TOKEN）：D1 读写代理 / 上传 / purge / 触发 workflow
 *   - 管理 API（Bearer ADMIN_TOKEN）：源 CRUD
 *   - /admin：静态资产（iOS 风格前端，Phase 6）
 */
export interface Env {
  DB: D1Database;
  BUCKET: KVNamespace;
  KV: KVNamespace;
  ASSETS: Fetcher;
  ADMIN_TOKEN: string;
  GH_TOKEN: string;
  GITHUB_REPO: string;
  CACHE_TTL_PLAYLIST: string;
  CACHE_TTL_EPG: string;
}

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

function isAdmin(request: Request, env: Env): boolean {
  const auth = request.headers.get("Authorization") || "";
  return auth === "Bearer " + env.ADMIN_TOKEN && env.ADMIN_TOKEN !== "";
}

async function serveR2WithCache(
  env: Env, ctx: ExecutionContext, request: Request, key: string, ttl: number,
): Promise<Response> {
  const url = new URL(request.url);
  const cacheKey = new Request(url.toString(), request);
  const cached = await caches.default.match(cacheKey);
  if (cached) return cached;

  const buf = await env.BUCKET.get(key, "arrayBuffer");
  if (!buf) {
    return json({ error: "not found", key }, 404);
  }
  const headers = new Headers();
  headers.set("Content-Type", contentTypeFor(key));
  headers.set("Cache-Control", "public, max-age=" + ttl);
  headers.set(...CORSHeader());
  const resp = new Response(buf, { headers });
  ctx.waitUntil(caches.default.put(cacheKey, resp.clone()));
  return resp;
}

function CORSHeader(): [string, string] {
  return ["Access-Control-Allow-Origin", "*"];
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS });
    }

    // ---------- 公开订阅出口 ----------
    const publishFiles: Record<string, { key: string; ttl: number }> = {
      "/iptv.m3u": { key: "publish/iptv.m3u", ttl: Number(env.CACHE_TTL_PLAYLIST) || 60 },
      "/iptv.txt": { key: "publish/iptv.txt", ttl: Number(env.CACHE_TTL_PLAYLIST) || 60 },
      "/iptv.json": { key: "publish/iptv.json", ttl: Number(env.CACHE_TTL_PLAYLIST) || 60 },
      "/epg.xml": { key: "publish/epg.xml", ttl: Number(env.CACHE_TTL_EPG) || 3600 },
    };
    if (publishFiles[path]) {
      const f = publishFiles[path];
      return serveR2WithCache(env, ctx, request, f.key, f.ttl);
    }
    // 台标
    if (path.startsWith("/logos/")) {
      return serveR2WithCache(env, ctx, request, path.slice(1), 86400);
    }
    // 快照回滚出口（Phase 2 增强）
    if (path.startsWith("/snapshots/")) {
      return serveR2WithCache(env, ctx, request, path.slice(1), 60);
    }

    // ---------- 公开状态 ----------
    if (path === "/api/v1/status" && request.method === "GET") {
      const runs = await env.DB.prepare(
        "SELECT id, kind, ts, status, summary_json FROM runs ORDER BY ts DESC LIMIT 5",
      ).all();
      const counts = await env.DB.prepare(
        "SELECT (SELECT COUNT(*) FROM entries WHERE active=1) AS entries_active," +
        " (SELECT COUNT(*) FROM entries WHERE active=1 AND status='ok') AS entries_ok," +

        " (SELECT COUNT(*) FROM channels) AS channels",
      ).first();
      return json({ runs: runs.results, counts });
    }

    // ---------- 内部 API：D1 代理 / 上传 / 触发 ----------
    if (path === "/api/internal/d1-query" && isAdmin(request, env)) {
      const body = await request.json<{ sql: string; params?: unknown[] }>();
      const stmt = env.DB.prepare(body.sql);
      const result = body.params?.length
        ? await stmt.bind(...body.params).all()
        : await stmt.all();
      return json({ rows: result.results });
    }

    if (path === "/api/internal/d1" && isAdmin(request, env)) {
      const body = await request.json<
        { sql?: string; params?: unknown[]; statements?: { sql: string; params?: unknown[] }[] }
      >();
      if (body.statements?.length) {
        const prepared = body.statements.map((s) =>
          s.params?.length
            ? env.DB.prepare(s.sql).bind(...s.params)
            : env.DB.prepare(s.sql),
        );
        const results = await env.DB.batch(prepared);
        return json({ ok: true, count: results.length });
      }
      if (body.sql) {
        const stmt = body.params?.length
          ? env.DB.prepare(body.sql).bind(...body.params)
          : env.DB.prepare(body.sql);
        const result = await stmt.run();
        return json({ ok: true, meta: result.meta });
      }
      return json({ error: "bad request" }, 400);
    }

    if (path === "/api/internal/upload" && isAdmin(request, env)) {
      const key = url.searchParams.get("key") || "";
      if (!key || key.includes("..")) {
        return json({ error: "bad key" }, 400);
      }
      const data = await request.arrayBuffer();
      await env.BUCKET.put(key, data);
      // purge 公开出口的缓存
      const purgeUrls: Record<string, string> = {
        "publish/iptv.m3u": "/iptv.m3u",
        "publish/iptv.txt": "/iptv.txt",
        "publish/iptv.json": "/iptv.json",
        "publish/epg.xml": "/epg.xml",
      };
      const purgePath = purgeUrls[key];
      if (purgePath) {
        ctx.waitUntil(caches.default.delete(new Request(new URL(purgePath, url).toString())));
      }
      return json({ ok: true, key, size: data.byteLength });
    }

    if (path === "/api/trigger" && isAdmin(request, env)) {
      const body = await request.json<{ workflow: string }>();
      const allowed = ["fetch", "speed", "aggregate", "epg"];
      if (!allowed.includes(body.workflow)) {
        return json({ error: "bad workflow" }, 400);
      }
      const resp = await fetch(
        "https://api.github.com/repos/" + env.GITHUB_REPO + "/actions/workflows/" + body.workflow + ".yml/dispatches",
        {
          method: "POST",
          headers: {
            Authorization: "Bearer " + env.GH_TOKEN,
            Accept: "application/vnd.github+json",
            "User-Agent": "iptv-cf-worker",
          },
          body: JSON.stringify({ ref: "main" }),
        },
      );
      if (resp.status >= 300) {
        return json({ error: "github " + resp.status, detail: await resp.text() }, 502);
      }
      return json({ ok: true, workflow: body.workflow });
    }

    // ---------- 管理 API：源 CRUD（Phase 5 扩展入口） ----------
    if (path === "/api/v1/sources" && request.method === "GET" && isAdmin(request, env)) {
      const rows = await env.DB.prepare(
        "SELECT id, type, url, name, enabled, status, etag, last_fetched_at," +
        " entry_count, reputation FROM sources ORDER BY name",
      ).all();
      return json({ sources: rows.results });
    }

    if (path === "/api/v1/sources" && request.method === "POST" && isAdmin(request, env)) {
      const body = await request.json<{
        name: string; url: string; type?: string; headers_json?: string;
      }>();
      if (!body.name || !body.url) {
        return json({ error: "name/url required" }, 400);
      }
      const slug = "custom:" + Date.now().toString(36);
      await env.DB.prepare(
        "INSERT INTO sources (id, type, url, name, headers_json, enabled) VALUES (?, ?, ?, ?, ?, 1)",
      ).bind(slug, body.type || "auto", body.url, body.name, body.headers_json || "{}").run();
      return json({ ok: true, id: slug });
    }

    // ---------- 静态资产（/admin 前端，Phase 6） ----------
    if (path === "/" || path.startsWith("/admin") || path.startsWith("/assets/")) {
      const assetResp = await env.ASSETS.fetch(request);
      // /admin 及子路径回退到 SPA 入口
      if (assetResp.status === 404 && (path === "/" || path.startsWith("/admin"))) {
        return env.ASSETS.fetch(new URL("/", request.url));
      }
      return assetResp;
    }

    return json({ error: "not found" }, 404);
  },
};

function contentTypeFor(key: string): string {
  if (key.endsWith(".m3u") || key.endsWith(".m3u8")) return "application/vnd.apple.mpegurl";
  if (key.endsWith(".txt")) return "text/plain; charset=utf-8";
  if (key.endsWith(".json")) return "application/json";
  if (key.endsWith(".xml")) return "application/xml";
  if (key.endsWith(".png")) return "image/png";
  if (key.endsWith(".jpg") || key.endsWith(".jpeg")) return "image/jpeg";
  if (key.endsWith(".svg")) return "image/svg+xml";
  if (key.endsWith(".gz")) return "application/gzip";
  return "application/octet-stream";
}
