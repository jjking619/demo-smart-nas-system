#!/usr/bin/env node
/**
 * NAS Knowledge Base MCP bridge
 *
 * - 文档/文件查询  → KB API  (http://knowledge_base:8084/search)
 * - 照片语义查询  → Immich smart search (http://immich-server:2283)
 *
 * 协议：换行分隔 JSON（openclaw SDK 1.29.0 stdio 格式）
 * 同时兼容 Content-Length 帧协议（旧客户端兜底）
 */

"use strict";

const fs = require("fs");
const path = require("path");

const KB_API_URL      = process.env.KB_API_URL      || "http://knowledge_base:8084";
const IMMICH_BASE_URL = process.env.IMMICH_BASE_URL  || "http://immich-server:2283";
const IMMICH_API_KEY  = process.env.IMMICH_API_KEY   || "";
const TIMEOUT_MS      = 15_000;
const MCP_LOG_FILE    = (process.env.MCP_LOG_FILE || "/logs/mcp_kb_search.log").trim();
const LOG_MAX_BYTES   = Number(process.env.LOG_MAX_BYTES || 5 * 1024 * 1024);
const LOG_BACKUPS     = Number(process.env.LOG_BACKUPS || 3);

// 照片意图关键词 → 走 Immich CLIP，其余走 KB FTS5
const PHOTO_KW = ["照片", "图片", "相册", "风景", "动物", "人物", "美食", "植物", "旅行", "photo", "image"];

function isPhotoQuery(q) {
  const lower = q.toLowerCase();
  return PHOTO_KW.some(k => lower.includes(k));
}

function rotateLogFile(filePath) {
  try {
    if (!filePath || !fs.existsSync(filePath)) return;
    const maxBytes = Number.isFinite(LOG_MAX_BYTES) && LOG_MAX_BYTES > 0 ? LOG_MAX_BYTES : 5 * 1024 * 1024;
    const backups = Number.isFinite(LOG_BACKUPS) && LOG_BACKUPS > 0 ? Math.floor(LOG_BACKUPS) : 3;
    if (fs.statSync(filePath).size < maxBytes) return;
    for (let i = backups - 1; i >= 1; i--) {
      const src = `${filePath}.${i}`;
      const dst = `${filePath}.${i + 1}`;
      if (fs.existsSync(src)) fs.renameSync(src, dst);
    }
    fs.renameSync(filePath, `${filePath}.1`);
  } catch {
    // Keep MCP stdio clean even if file logging fails.
  }
}

function logEvent(level, event, fields = {}) {
  if (!MCP_LOG_FILE) return;
  try {
    fs.mkdirSync(path.dirname(MCP_LOG_FILE), { recursive: true });
    rotateLogFile(MCP_LOG_FILE);
    const line = JSON.stringify({
      ts: new Date().toISOString(),
      level,
      event,
      ...fields,
    });
    fs.appendFileSync(MCP_LOG_FILE, `${line}\n`, { encoding: "utf8" });
    rotateLogFile(MCP_LOG_FILE);
  } catch {
    // Keep MCP stdio clean even if file logging fails.
  }
}

// ── MCP tool 定义 ───────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: "kb_search",
    description:
      "在NAS知识库中搜索文件、文档和照片。\n" +
      "• 文件/文档定位：'合同在哪'、'季度报告'、'预算表'\n" +
      "• 文档内容：'押金多少'、'方案内容'\n" +
      "• 照片语义：'海边风景'、'猫的照片'（走 Immich CLIP）",
    inputSchema: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description: "搜索词，支持中文，例如：合同、季度报告、猫的照片",
        },
        type: {
          type: "string",
          enum: ["auto", "file", "photo"],
          description: "搜索类型：auto=自动判断（默认），file=文件/文档，photo=照片",
        },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
];

// ── I/O helpers ─────────────────────────────────────────────────────────────

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}
const ok   = (id, result)           => send({ jsonrpc: "2.0", id, result });
const fail = (id, code, message)    => send({ jsonrpc: "2.0", id, error: { code, message } });

async function fetchJSON(url, opts) {
  const ctl   = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const resp = await fetch(url, { ...opts, signal: ctl.signal });
    return await resp.json();
  } finally {
    clearTimeout(timer);
  }
}

// ── 搜索后端 ─────────────────────────────────────────────────────────────────

async function searchKB(query) {
  const data = await fetchJSON(`${KB_API_URL}/search`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ query }),
  });
  return Array.isArray(data.results) ? data.results : [];
}

async function searchImmich(query) {
  if (!IMMICH_API_KEY) return [];
  const data = await fetchJSON(`${IMMICH_BASE_URL}/api/search/smart`, {
    method:  "POST",
    headers: { "Content-Type": "application/json", "x-api-key": IMMICH_API_KEY },
    body:    JSON.stringify({ query, size: 5 }),
  });
  const items = data?.assets?.items || [];
  return items.map(a => ({
    path:     a.originalPath || a.deviceAssetId || "",
    name:     a.originalFileName || "",
    ext:      ".jpg",
    category: "照片",
    snippet:  [a.localDateTime?.slice(0, 10), a.city, a.country].filter(Boolean).join(" "),
  }));
}

// ── 工具调用 ─────────────────────────────────────────────────────────────────

async function onToolCall(id, params) {
  const { name, arguments: args = {} } = params || {};
  if (name !== "kb_search") {
    logEvent("warn", "tool_unknown", { id, name: String(name || "") });
    ok(id, { content: [{ type: "text", text: `未知工具: ${name}` }], isError: true });
    return;
  }

  const query = String(args.query || "").trim();
  if (!query) {
    logEvent("warn", "tool_bad_request", { id, reason: "empty_query" });
    ok(id, { content: [{ type: "text", text: "请提供搜索词" }], isError: true });
    return;
  }

  const type = args.type || "auto";
  let results = [];
  const usePhotoIntent = type === "photo" || (type === "auto" && isPhotoQuery(query));
  logEvent("info", "tool_call_start", {
    id,
    name,
    type,
    use_photo_intent: usePhotoIntent,
    query: query.slice(0, 120),
  });

  try {
    if (usePhotoIntent) {
      results = await searchImmich(query).catch(() => []);
      if (results.length === 0) {
        results = await searchKB(query);   // fallback to FTS5
      }
    } else {
      results = await searchKB(query);
    }
  } catch (err) {
    logEvent("error", "tool_call_failed", {
      id,
      type,
      error: String(err && (err.message || err)).slice(0, 240),
    });
    ok(id, { content: [{ type: "text", text: `搜索失败: ${err.message || err}` }], isError: true });
    return;
  }

  if (results.length === 0) {
    logEvent("info", "tool_call_done", {
      id,
      type,
      result_count: 0,
      fallback_to_kb: usePhotoIntent,
    });
    ok(id, { content: [{ type: "text", text: `未找到与"${query}"相关的内容` }] });
    return;
  }

  const lines = results.map((r, i) => {
    const snip = r.snippet ? ` — ${r.snippet}` : "";
    return `${i + 1}. ${r.path}${snip}`;
  });

  ok(id, {
    content: [{ type: "text", text: `找到 ${results.length} 条结果：\n${lines.join("\n")}` }],
    structuredContent: { ok: true, query, results },
  });
  logEvent("info", "tool_call_done", {
    id,
    type,
    result_count: results.length,
    fallback_to_kb: usePhotoIntent,
  });
}

// ── MCP 协议分发 ─────────────────────────────────────────────────────────────

async function handle(msg) {
  if (!msg || msg.jsonrpc !== "2.0") return;
  const id     = Object.prototype.hasOwnProperty.call(msg, "id") ? msg.id : null;
  const method = msg.method;
  if (!method) return;

  switch (method) {
    case "initialize":
      ok(id, {
        protocolVersion: msg.params?.protocolVersion || "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "kb-mcp", version: "0.1.0" },
      });
      break;
    case "initialized": break;
    case "ping":
      if (id !== null) ok(id, {});
      break;
    case "tools/list":
      ok(id, { tools: TOOLS });
      break;
    case "tools/call":
      await onToolCall(id, msg.params || {});
      break;
    default:
      if (id !== null) fail(id, -32601, `Method not found: ${method}`);
  }
}

// ── stdin 解析：优先换行分隔 JSON，兜底 Content-Length 帧 ────────────────────

let buf = Buffer.alloc(0);
process.stdin.on("data", async chunk => {
  buf = Buffer.concat([buf, chunk]);

  while (true) {
    // 协议1：换行分隔 JSON（openclaw SDK 1.29.0）
    const nl = buf.indexOf("\n");
    if (nl !== -1) {
      const line = buf.slice(0, nl).toString("utf8").replace(/\r$/, "");
      buf = buf.slice(nl + 1);
      const trimmed = line.trim();
      if (!trimmed) continue;
      let msg;
      try { msg = JSON.parse(trimmed); } catch { continue; }
      try { await handle(msg); } catch (err) {
        const id = Object.prototype.hasOwnProperty.call(msg, "id") ? msg.id : null;
        if (id !== null) fail(id, -32000, String(err));
      }
      continue;
    }

    // 协议2：Content-Length 帧（兼容旧客户端）
    const sep = buf.indexOf("\r\n\r\n");
    if (sep === -1) break;
    const header = buf.slice(0, sep).toString("utf8");
    const m = header.match(/Content-Length:\s*(\d+)/i);
    if (!m) { buf = buf.slice(sep + 4); continue; }
    const bodyLen = parseInt(m[1], 10);
    if (buf.length < sep + 4 + bodyLen) break;
    const body = buf.slice(sep + 4, sep + 4 + bodyLen).toString("utf8");
    buf = buf.slice(sep + 4 + bodyLen);
    let msg;
    try { msg = JSON.parse(body); } catch { continue; }
    try { await handle(msg); } catch (err) {
      const id = Object.prototype.hasOwnProperty.call(msg, "id") ? msg.id : null;
      if (id !== null) fail(id, -32000, String(err));
    }
  }
});

process.stdin.on("end", () => process.exit(0));
