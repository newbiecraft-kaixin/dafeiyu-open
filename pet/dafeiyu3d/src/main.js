"use strict";

const path = require("path");
const fs = require("fs");
const http = require("http");
const os = require("os");
const { app, BrowserWindow, ipcMain, protocol, net, screen } = require("electron");

const ROOT = path.join(__dirname, "..");
const CONFIG_PATH = path.join(ROOT, "config.json");

function loadConfig() {
  const defaults = {
    port: 23335,
    model: "qwen3.5:9b",
    ollama: "http://127.0.0.1:11434",
    width: 420,
    height: 620,
    marginRight: 18,
    marginBottom: 10,
    petName: "大肥鱼",
    narrationTimeoutMs: 9000,
    narrationCacheMs: 90000,
    narrate: true,
  };
  try {
    return { ...defaults, ...JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")) };
  } catch (err) {
    return defaults;
  }
}

const CONFIG = loadConfig();
const PORT = Number(process.env.PET_PORT || CONFIG.port || 23334);
const OLLAMA = CONFIG.ollama || "http://127.0.0.1:11434";
const MODEL = process.env.PET_MODEL || CONFIG.model || "qwen3.5:9b";
const NARRATION_TIMEOUT = Math.max(3000, Number(CONFIG.narrationTimeoutMs) || 9000);
const NARRATION_CACHE_TTL = Math.max(10000, Number(CONFIG.narrationCacheMs) || 90000);
const NARRATION_CACHE_MAX = 80;
const NARRATE = CONFIG.narrate !== false;

let win = null;

/* ------------------------------------------------------------------ */
/* 桌宠状态                                                            */
/* ------------------------------------------------------------------ */

let petState = {
  state: "idle",
  source: "local",
  event: "boot",
  pet_text: "",
  detail: "",
  action: "",
  agent_id: "",
  session_id: "",
  at: Date.now(),
};

let lastPushedSignature = "";

function stateSignature() {
  const { at, ...rest } = petState;
  return JSON.stringify(rest);
}

function pushState(force = false) {
  const signature = stateSignature();
  if (!force && signature === lastPushedSignature) return false;
  lastPushedSignature = signature;
  if (win && !win.isDestroyed()) {
    win.webContents.send("pet:state", petState);
  }
  return true;
}

const SOURCE_LABEL = {
  qq: "QQ机器人",
  wechat: "微信机器人",
  local: "本机",
  pet: "桌宠",
  dsh: "DeepSeek 工作台",
};

const STATE_LABEL = {
  idle: "空闲",
  working: "忙碌",
  thinking: "思考中",
  sleeping: "休息",
  happy: "开心",
  attention: "注意",
  error: "异常",
};

function fallbackNarration(s) {
  const src = SOURCE_LABEL[s.source] || "";
  switch (s.state) {
    case "working":
      return src ? `${src} 那边我在忙着回话呢～` : "我在忙着干活呢，主人～";
    case "thinking":
      return "让我想想……在泡泡里转一圈再回答～";
    case "sleeping":
      return "呼……主人，我先眯一会儿。";
    case "happy":
      return "嘿嘿，收到主人的消息啦～";
    case "attention":
      return "主人主人，有新的动静！";
    case "error":
      return "哎呀，出了点小状况，主人看一眼～";
    default:
      return "我在这儿陪着主人，泡泡里安安静静的～";
  }
}

function cleanNarration(raw) {
  if (!raw) return "";
  let text = String(raw);
  text = text.replace(/<think[\s\S]*?<\/think>/gi, "");
  text = text.replace(/```[\s\S]*?```/g, "");
  text = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).pop() || "";
  text = text.replace(/^["'“「『]+|["'”」』]+$/g, "");
  text = text.replace(/^[（(]\s*/g, "").replace(/\s*[）)]$/g, "");
  text = text.replace(/^大肥鱼[:：]\s*/g, "");
  text = text.replace(/\s+/g, " ").trim();
  if (text.length > 42) text = text.slice(0, 42) + "…";
  return text;
}

/* 旁白网络层：keep-alive 复用连接 + 同状态复用请求 + 结果缓存 + 只保留最新请求 */
const ollamaAgent = new http.Agent({ keepAlive: true, maxSockets: 2, keepAliveMsecs: 30000 });
const narrationCache = new Map();
const narrationStats = { hit: 0, miss: 0, aborted: 0, failed: 0, generated: 0 };
let activeNarration = null;

function narrationKey(s) {
  return `${s.source}|${s.state}|${s.event}`;
}

function cacheGet(key) {
  const row = narrationCache.get(key);
  if (!row) return "";
  if (Date.now() - row.at > NARRATION_CACHE_TTL) {
    narrationCache.delete(key);
    return "";
  }
  narrationCache.delete(key);
  narrationCache.set(key, row);
  return row.text;
}

function cacheSet(key, text) {
  narrationCache.set(key, { text, at: Date.now() });
  while (narrationCache.size > NARRATION_CACHE_MAX) {
    narrationCache.delete(narrationCache.keys().next().value);
  }
}

function abortActiveNarration() {
  if (!activeNarration) return;
  const current = activeNarration;
  activeNarration = null;
  narrationStats.aborted += 1;
  try {
    current.req.destroy();
  } catch (err) {
    /* ignore */
  }
}

function askOllama(s) {
  const key = narrationKey(s);
  if (activeNarration && activeNarration.key === key) {
    return activeNarration.promise;
  }
  abortActiveNarration();

  const cached = cacheGet(key);
  if (cached) {
    narrationStats.hit += 1;
    return Promise.resolve(cached);
  }
  narrationStats.miss += 1;

  const system =
    "你是「大肥鱼」，一只住在主人桌面上的蓝色鲸鱼女仆桌宠。你的唯一工作：" +
    "把机器人的实时状态用一句中文旁白说出来。要求：不超过26个汉字；" +
    "语气元气、亲昵，称对方为主人；可以带一点鲸鱼、泡泡、海洋的意象；" +
    "不解释规则，不加引号，不用表情符号，不编造输入里没有的细节，只输出这一句旁白。";
  const user =
    `来源：${SOURCE_LABEL[s.source] || "本机"}\n` +
    `状态：${STATE_LABEL[s.state] || s.state || "空闲"}\n` +
    `事件：${s.event || "无"}\n` +
    `细节：${s.detail || "无"}\n` +
    "请播报一句。";

  const body = JSON.stringify({
    model: MODEL,
    system,
    prompt: user,
    stream: false,
    think: false,
    keep_alive: "30m",
    options: { temperature: 0.8, top_p: 0.9, num_predict: 64 },
  });

  const entry = { key, req: null, done: false, promise: null };
  entry.promise = new Promise((resolve) => {
    const finish = (text) => {
      if (entry.done) return;
      entry.done = true;
      if (activeNarration === entry) activeNarration = null;
      if (text) {
        narrationStats.generated += 1;
        cacheSet(key, text);
      }
      resolve(text);
    };

    const url = new URL("/api/generate", OLLAMA);
    const req = http.request(
      {
        hostname: url.hostname,
        port: url.port || 11434,
        path: url.pathname,
        method: "POST",
        agent: ollamaAgent,
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
        timeout: NARRATION_TIMEOUT,
      },
      (res) => {
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          try {
            finish(cleanNarration(JSON.parse(data).response));
          } catch (err) {
            finish("");
          }
        });
      }
    );
    entry.req = req;
    req.on("timeout", () => {
      narrationStats.failed += 1;
      req.destroy();
      finish("");
    });
    req.on("error", () => finish(""));
    req.write(body);
    req.end();
  });

  activeNarration = entry;
  return entry.promise;
}

/* ------------------------------------------------------------------ */
/* HTTP 状态接口（给 AstrBot / DSH / QQ / 微信 用）                     */
/* ------------------------------------------------------------------ */

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 200000) {
        req.destroy();
        reject(new Error("payload too large"));
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

function sendJson(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

async function handleState(payload) {
  const next = {
    state: String(payload.state || petState.state || "idle"),
    source: String(payload.source || petState.source || "local"),
    event: String(payload.event || ""),
    detail: String(payload.detail || "").slice(0, 300),
    action: String(payload.action || "").slice(0, 16),
    agent_id: String(payload.agent_id || ""),
    session_id: String(payload.session_id || ""),
    at: Date.now(),
  };

  if (payload.pet_text) {
    abortActiveNarration();
    petState = { ...next, pet_text: String(payload.pet_text).slice(0, 120), narrated: false };
    pushState(true);
    return petState;
  }

  petState = { ...next, pet_text: fallbackNarration(next), narrated: false };
  pushState();

  if (NARRATE && payload.narrate !== false) {
    const text = await askOllama(next);
    if (text) {
      // 只有状态没被后续请求覆盖时才用模型旁白替换
      if (petState.at === next.at) {
        petState = { ...petState, pet_text: text, narrated: true };
        pushState();
      }
      return { ...petState, pet_text: text };
    }
  }
  return petState;
}

function startStateServer() {
  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === "OPTIONS") {
        res.writeHead(204, {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Headers": "content-type",
          "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        });
        return res.end();
      }
      const url = new URL(req.url, "http://127.0.0.1");

      if (req.method === "GET" && url.pathname === "/health") {
        return sendJson(res, 200, {
          ok: true,
          name: CONFIG.petName || "大肥鱼",
          model: MODEL,
          port: PORT,
          state: petState,
          narration: {
            enabled: NARRATE,
            timeout_ms: NARRATION_TIMEOUT,
            cache_size: narrationCache.size,
            cache_ttl_ms: NARRATION_CACHE_TTL,
            inflight: activeNarration ? activeNarration.key : null,
            stats: narrationStats,
          },
        });
      }

      if (req.method === "GET" && url.pathname === "/state") {
        return sendJson(res, 200, petState);
      }

      if (req.method === "POST" && (url.pathname === "/state" || url.pathname === "/")) {
        const raw = await readBody(req);
        let payload = {};
        try {
          payload = raw ? JSON.parse(raw) : {};
        } catch (err) {
          return sendJson(res, 400, { ok: false, error: "invalid json" });
        }
        const result = await handleState(payload);
        return sendJson(res, 200, { ok: true, state: result });
      }

      sendJson(res, 404, { ok: false, error: "not found" });
    } catch (err) {
      sendJson(res, 500, { ok: false, error: String(err && err.message ? err.message : err) });
    }
  });

  server.keepAliveTimeout = 65000;
  server.headersTimeout = 66000;

  server.on("error", (err) => {
    console.warn("[pet] state server error:", err.message);
  });

  server.listen(PORT, "0.0.0.0", () => {
    console.log(`[pet] state server on http://127.0.0.1:${PORT}  model=${MODEL}`);
    try {
      const dir = path.join(os.homedir(), ".clawd");
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(
        path.join(dir, "pet-runtime.json"),
        JSON.stringify({ port: PORT, pid: process.pid, model: MODEL, updatedAt: Date.now() }, null, 2)
      );
    } catch (err) {
      /* ignore */
    }
  });
}

/* ------------------------------------------------------------------ */
/* 窗口                                                                */
/* ------------------------------------------------------------------ */

function placeWindow() {
  const width = CONFIG.width || 420;
  const height = CONFIG.height || 620;
  const area = screen.getPrimaryDisplay().workArea;
  const x = area.x + area.width - width - (CONFIG.marginRight || 18);
  const y = area.y + area.height - height - (CONFIG.marginBottom || 10);
  win.setPosition(Math.round(x), Math.round(y));
}

function createWindow() {
  const width = CONFIG.width || 420;
  const height = CONFIG.height || 620;
  const area = screen.getPrimaryDisplay().workArea;

  win = new BrowserWindow({
    width,
    height,
    x: Math.round(area.x + area.width - width - (CONFIG.marginRight || 18)),
    y: Math.round(area.y + area.height - height - (CONFIG.marginBottom || 10)),
    transparent: true,
    backgroundColor: "#00000000",
    frame: false,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: true,
    show: false,
    title: "大肥鱼 3D 桌宠",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
      spellcheck: false,
    },
  });

  win.setAlwaysOnTop(true, "screen-saver");
  win.setMenuBarVisibility(false);
  win.setIgnoreMouseEvents(true, { forward: true });
  win.loadURL("pet://app/src/index.html");

  win.once("ready-to-show", () => {
    win.showInactive();
    pushState();
  });

  win.on("closed", () => {
    win = null;
  });
}

/* ------------------------------------------------------------------ */
/* IPC                                                                 */
/* ------------------------------------------------------------------ */

function wireIpc() {
  ipcMain.on("pet:set-ignore", (_e, v) => {
    if (win && !win.isDestroyed()) win.setIgnoreMouseEvents(!!v, { forward: true });
  });

  ipcMain.on("pet:drag", (_e, { dx, dy }) => {
    if (!win || win.isDestroyed()) return;
    const [x, y] = win.getPosition();
    win.setPosition(Math.round(x + dx), Math.round(y + dy));
  });

  ipcMain.on("pet:reset-position", () => {
    if (win && !win.isDestroyed()) placeWindow();
  });

  ipcMain.on("pet:quit", () => app.quit());

  ipcMain.on("pet:config", (e) => {
    e.returnValue = { petName: CONFIG.petName || "大肥鱼", port: PORT, model: MODEL };
  });

  ipcMain.on("pet:test-bubble", () => {
    handleState({
      source: "qq",
      state: "working",
      event: "测试：收到一条群消息并开始回复",
      detail: "群「摸鱼小分队」有人说：今天吃什么？",
    });
  });
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */

protocol.registerSchemesAsPrivileged([
  { scheme: "pet", privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } },
]);

const singleInstance = app.requestSingleInstanceLock();
if (!singleInstance) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win && !win.isDestroyed()) {
      win.showInactive();
      win.setAlwaysOnTop(true, "screen-saver");
    }
  });

  app.whenReady().then(() => {
    protocol.handle("pet", (request) => {
      try {
        const url = new URL(request.url);
        const rel = decodeURIComponent(url.pathname).replace(/^\/+/, "");
        const filePath = path.join(ROOT, rel);
        if (!filePath.startsWith(ROOT)) {
          return new Response("forbidden", { status: 403 });
        }
        return net.fetch("file://" + filePath.replace(/\\/g, "/"));
      } catch (err) {
        return new Response("not found", { status: 404 });
      }
    });

    wireIpc();
    startStateServer();
    createWindow();

    const snapArg = process.argv.find((arg) => arg.startsWith("--snapshot="));
    if (snapArg) {
      const target = snapArg.slice("--snapshot=".length);
      win.webContents.once("did-finish-load", () => {
        setTimeout(async () => {
          try {
            win.webContents.send("pet:snapshot");
            await new Promise((r) => setTimeout(r, 900));
            const image = await win.webContents.capturePage();
            fs.mkdirSync(path.dirname(target), { recursive: true });
            fs.writeFileSync(target, image.toPNG());
            console.log("[pet] snapshot saved:", target);
          } catch (err) {
            console.error("[pet] snapshot failed:", err);
          }
          app.quit();
        }, 4500);
      });
    }

    app.on("activate", () => {
      if (!win) createWindow();
    });
  });

  app.on("window-all-closed", () => {
    app.quit();
  });
}
