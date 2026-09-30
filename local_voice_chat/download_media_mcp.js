#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const DEFAULT_API_URL = process.env.DOWNLOAD_API_URL || "http://media_downloader:8081/download";
const DOWNLOAD_ROOT_LABEL = process.env.DOWNLOAD_ROOT_LABEL || "/home/pi/nas_share/downloads";
const DEFAULT_NOTIFY_TEXT = process.env.DOWNLOAD_NOTIFY_TEXT || "下载已完成";
const REQUEST_TIMEOUT_MS = Number(process.env.DOWNLOAD_API_TIMEOUT_MS || 15 * 60 * 1000);
const MCP_LOG_FILE = (process.env.MCP_LOG_FILE || "/logs/mcp_download_media.log").trim();
const LOG_MAX_BYTES = Number(process.env.LOG_MAX_BYTES || 5 * 1024 * 1024);
const LOG_BACKUPS = Number(process.env.LOG_BACKUPS || 3);

// 视频关键词库：用户可说关键词而不是URL
const MEDIA_LIBRARY = {
  // 环境纪录片
  "海洋": {
    url: "https://vjs.zencdn.net/v/oceans.mp4",
    label: "海洋纪录片",
    default_folder: "Movies",
  },
  "大海": { url: "https://vjs.zencdn.net/v/oceans.mp4", label: "海洋纪录片", default_folder: "Movies" },
  // 电影预告片
  "预告片": {
    url: "https://media.w3.org/2010/05/sintel/trailer.mp4",
    label: "Sintel电影预告片",
    default_folder: "Movies",
  },
  "sintel": {
    url: "https://media.w3.org/2010/05/sintel/trailer.mp4",
    label: "Sintel电影预告片",
    default_folder: "Movies",
  },
  // 通用样本
  "样本": {
    url: "https://vjs.zencdn.net/v/oceans.mp4",
    label: "通用视频样本",
    default_folder: "Movies",
  },
  "测试": {
    url: "https://www.learningcontainer.com/wp-content/uploads/2020/05/sample-mp4-file.mp4",
    label: "通用视频样本",
    default_folder: "Movies",
  },
};

const TOOL = {
  name: "download_media",
  description:
    "下载媒体到NAS目录。支持关键词快速下载（如'海洋'、'预告片'）或自定义URL/搜索。" +
    "预定义关键词：" + Object.keys(MEDIA_LIBRARY).join("、"),
  inputSchema: {
    type: "object",
    properties: {
      url: {
        type: "string",
        description: "媒体链接（可选，与query/keyword二选一）",
      },
      query: {
        type: "string",
        description: "搜索关键词（可选，例如：流浪地球3 预告片；或预定义词：海洋、预告片、兔子、样本）",
      },
      keyword: {
        type: "string",
        description: "快速关键词（可选，会从库中查找对应URL。支持：" + Object.keys(MEDIA_LIBRARY).join("、") + "）",
      },
      target_folder: {
        type: "string",
        description: "目标文件夹描述，例如：Movies、TV Shows、电影、剧集。不指定时使用关键词默认值",
      },
      notify_tts: {
        type: "boolean",
        description: "下载完成后是否触发TTS通知，默认 true",
      },
      tts_message: {
        type: "string",
        description: "自定义TTS通知文案，默认 下载已完成",
      },
    },
    additionalProperties: false,
  },
};

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

function safeHostFromUrl(value) {
  try {
    return new URL(value).host;
  } catch {
    return "";
  }
}

const shortMap = [
  ["tvshows", "TV Shows"],
  ["tvshow", "TV Shows"],
  ["series", "TV Shows"],
  ["剧集", "TV Shows"],
  ["电视剧", "TV Shows"],
  ["连续剧", "TV Shows"],
  ["movies", "Movies"],
  ["movie", "Movies"],
  ["电影", "Movies"],
  ["影片", "Movies"],
  ["家庭影院", "Movies"],
  ["视频", "Movies"],
  ["video", "Movies"],
  ["音乐", "音乐"],
  ["music", "音乐"],
];

function mapTargetFolder(raw) {
  const v = String(raw || "").trim();
  if (!v) return "Movies";

  const compact = v.replace(/\s+/g, "").toLowerCase();
  for (const [k, val] of shortMap) {
    if (compact.includes(k)) return val;
  }

  const safe = v
    .replace(/\\/g, "/")
    .split("/")
    .map((part) =>
      part
        .replace(/\.\./g, "")
        .replace(/[^\w\u4e00-\u9fff\-()（）\[\]【】 .]/g, "_")
        .replace(/_+/g, "_")
        .replace(/^[._ ]+|[._ ]+$/g, "")
        .slice(0, 80)
    )
    .filter(Boolean)
    .join("/");

  return safe || "Movies";
}

function send(msg) {
  const json = JSON.stringify(msg);
  // MCP SDK stdio 协议：换行分隔 JSON（`JSON.stringify(message) + '\n'`）
  process.stdout.write(`${json}\n`);
}

function ok(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function fail(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

// 根据关键词查找URL
function matchMediaKeyword(keyword) {
  if (!keyword) return null;
  const key = String(keyword).toLowerCase().trim();
  for (const [k, v] of Object.entries(MEDIA_LIBRARY)) {
    if (k.toLowerCase() === key) {
      return v;
    }
  }
  return null;
}

async function callDownloadApi(args) {
  let url = String(args.url || "").trim();
  let query = String(args.query || "").trim();
  let keyword = String(args.keyword || "").trim();
  let targetFolderOverride = String(args.target_folder || "").trim();

  // 优先级：keyword > url > query
  if (keyword && !url) {
    const matched = matchMediaKeyword(keyword);
    if (matched) {
      url = matched.url;
      // 如果没有显式指定目标文件夹，使用关键词的默认文件夹
      if (!targetFolderOverride) {
        targetFolderOverride = matched.default_folder;
      }
    } else {
      // 关键词未找到，将其作为搜索词处理
      query = keyword;
    }
  }

  if (!url && !query) {
    return {
      ok: false,
      error: "url、query 或 keyword 至少提供一个",
      status: 400,
    };
  }

  const payload = {
    url,
    query,
    target_subdir: mapTargetFolder(targetFolderOverride),
    notify_tts: args.notify_tts !== false,
    tts_message: String(args.tts_message || DEFAULT_NOTIFY_TEXT).trim() || DEFAULT_NOTIFY_TEXT,
  };

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const resp = await fetch(DEFAULT_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: ctl.signal,
    });
    const data = await resp.json().catch(() => ({}));
    return {
      ok: resp.ok && !!data.ok,
      status: resp.status,
      data,
    };
  } catch (err) {
    return {
      ok: false,
      status: 500,
      error: err && err.name === "AbortError" ? "下载请求超时" : String(err),
    };
  } finally {
    clearTimeout(timer);
  }
}

function renderSuccess(data) {
  const safeSubdir = data.safe_subdir || "Movies";
  const files = Array.isArray(data.files) ? data.files : [];
  const first = files.length > 0 ? files[0] : "(文件名未返回)";
  const ttsText = data.notify_tts ? `；通知：${data.tts_text || DEFAULT_NOTIFY_TEXT}` : "";
  const summary = `下载已完成，目录：${safeSubdir}，文件数：${files.length}${ttsText}`;

  return {
    content: [
      {
        type: "text",
        text: `${summary}\n首个文件：${first}`,
      },
    ],
    structuredContent: {
      ok: true,
      summary,
      safe_subdir: safeSubdir,
      files,
      notify_tts: !!data.notify_tts,
      notify_status: data.notify_status || "unknown",
      download_root: DOWNLOAD_ROOT_LABEL,
    },
  };
}

async function onToolCall(id, params) {
  const name = params && params.name;
  const args = (params && params.arguments) || {};

  if (name !== TOOL.name) {
    logEvent("warn", "tool_unknown", { id, name: String(name || "") });
    ok(id, {
      content: [{ type: "text", text: `未知工具: ${name}` }],
      isError: true,
    });
    return;
  }

  logEvent("info", "tool_call_start", {
    id,
    name,
    has_url: !!String(args.url || "").trim(),
    url_host: safeHostFromUrl(String(args.url || "").trim()),
    query: String(args.query || "").trim().slice(0, 80),
    keyword: String(args.keyword || "").trim().slice(0, 40),
    target_folder: String(args.target_folder || "").trim().slice(0, 80),
    notify_tts: args.notify_tts !== false,
  });

  const ret = await callDownloadApi(args);
  if (!ret.ok) {
    const msg = ret.data && ret.data.error ? ret.data.error : ret.error || "下载失败";
    logEvent("error", "tool_call_failed", {
      id,
      status: ret.status,
      error: String(msg).slice(0, 240),
    });
    ok(id, {
      content: [
        {
          type: "text",
          text: `下载失败：${msg}`,
        },
      ],
      isError: true,
      structuredContent: {
        ok: false,
        status: ret.status,
        error: msg,
        download_root: DOWNLOAD_ROOT_LABEL,
      },
    });
    return;
  }

  const successData = ret.data || {};
  logEvent("info", "tool_call_done", {
    id,
    status: ret.status,
    ok: true,
    safe_subdir: String(successData.safe_subdir || "").slice(0, 120),
    files_count: Array.isArray(successData.files) ? successData.files.length : 0,
    notify_tts: !!successData.notify_tts,
    notify_status: String(successData.notify_status || ""),
  });

  ok(id, renderSuccess(successData));
}

async function handle(msg) {
  if (!msg || msg.jsonrpc !== "2.0") return;

  const id = Object.prototype.hasOwnProperty.call(msg, "id") ? msg.id : null;
  const method = msg.method;

  if (!method) return;

  if (method === "initialize") {
    ok(id, {
      protocolVersion: (msg.params && msg.params.protocolVersion) || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "download-media-mcp", version: "0.1.0" },
    });
    return;
  }

  if (method === "initialized") {
    return;
  }

  if (method === "ping") {
    if (id !== null) ok(id, {});
    return;
  }

  if (method === "tools/list") {
    ok(id, { tools: [TOOL] });
    return;
  }

  if (method === "tools/call") {
    await onToolCall(id, msg.params || {});
    return;
  }

  if (id !== null) {
    fail(id, -32601, `Method not found: ${method}`);
  }
}

let buf = Buffer.alloc(0);
process.stdin.on("data", async (chunk) => {
  buf = Buffer.concat([buf, chunk]);

  while (true) {
    // 协议1：MCP SDK 换行分隔 JSON（优先）
    const nl = buf.indexOf("\n");
    if (nl !== -1) {
      const line = buf.slice(0, nl).toString("utf8").replace(/\r$/, "");
      buf = buf.slice(nl + 1);
      const trimmed = line.trim();
      if (trimmed) {
        let msg;
        try {
          msg = JSON.parse(trimmed);
        } catch {
          continue;
        }
        try {
          await handle(msg);
        } catch (err) {
          const id = Object.prototype.hasOwnProperty.call(msg, "id") ? msg.id : null;
          if (id !== null) {
            fail(id, -32000, String(err));
          }
        }
      }
      continue;
    }

    // 协议2：旧式 Content-Length 帧（兼容）
    const idx = buf.indexOf("\r\n\r\n");
    if (idx === -1) break;

    const header = buf.slice(0, idx).toString("utf8");
    const m = /Content-Length:\s*(\d+)/i.exec(header);
    if (!m) {
      buf = buf.slice(idx + 4);
      continue;
    }

    const len = Number(m[1]);
    const total = idx + 4 + len;
    if (buf.length < total) break;

    const raw = buf.slice(idx + 4, total).toString("utf8");
    buf = buf.slice(total);

    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      continue;
    }

    try {
      await handle(msg);
    } catch (err) {
      const id = Object.prototype.hasOwnProperty.call(msg, "id") ? msg.id : null;
      if (id !== null) {
        fail(id, -32000, String(err));
      }
    }
  }
});

process.stdin.resume();
