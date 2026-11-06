"use strict";

/**
 * 大肥鱼桌宠 · 机器人状态中转
 *
 * QQ / 微信机器人（AstrBot 插件）把状态 POST 到本服务；本服务交给 brain（23402）
 * 生成一句中文旁白（brain 再调 model 层），然后把结果推给桌宠窗口（23335）/state，
 * 由桌宠显示气泡和动作。brain 不可用时才退回本机 Ollama。
 * 本服务独占 23334，桌宠窗口只做显示（23335），不会抢同一个端口。
 *
 * 桌宠只做“状态面板”，不共享 QQ / 微信机器人的人格与记忆。
 */

const fs = require("fs");
const http = require("http");
const path = require("path");

const ROOT = __dirname;
const CONFIG_PATH = path.join(ROOT, "config.json");
// 插件把协议端的本地图片复制到这里；relay 用 /pet-image/<name> 转成 http 地址给桌宠
const PET_IMAGE_DIR = path.join(ROOT, "pet-images");

const DEFAULTS = {
  port: 23334,
  pet: "http://127.0.0.1:23335",
  petPath: "/state",
  petKind: "auto",
  ollama: "http://127.0.0.1:11434",
  model: "qwen3.5:9b",
  narrate: true,
  timeoutMs: 15000,
  // 不再定时补播旧消息：开机/断线重连后只认新到的，历史消息一律不翻出来
  pendingTtlMs: 20 * 1000,
  pendingMaxAgeMs: 15 * 1000,
  maxPending: 1,
  // 刚开机/重连后的这段时间只记录不播，避免把适配器补推的旧消息念出来
  bootGraceMs: 20 * 1000,
  // 桌宠消息缓存：相同内容短时间只播一次，且默认不再写进 store 长期记忆库
  rememberMessages: false,
  contentCacheTtlMs: 10 * 60 * 1000,
  contentCacheMax: 400,
  minSpeakChars: 2,
  recentCacheMax: 100,
};

function loadConfig() {
  try {
    // 配置文件可能是 PowerShell 写出的（带 UTF-8 BOM），JSON.parse 前先去掉
    const raw = fs.readFileSync(CONFIG_PATH, "utf8").replace(/^\uFEFF/, "");
    return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch (err) {
    return DEFAULTS;
  }
}

const CONFIG = loadConfig();
const PORT = Number(process.env.PET_RELAY_PORT || CONFIG.port);

// 桌宠播报节流：dsh-pet 的语音队列只有 3 个名额，且气泡与语音都用同一个 text，
// 所以这里先按会话合并、再留全局最小间隔，只播「最新一条」，不排队堆积。
const PET_TEXT_MAX = 400; // 气泡显示全文；语音侧自己截到 60 字（见 shared-core speechSlice）
const PET_IMAGE_MAX = 3; // 气泡只显示第一张图，多出来的仅记录
const PET_CHAT_INTERVAL_MS = Math.max(0, Number(CONFIG.petChatIntervalMs ?? 15000));
const PET_MIN_GAP_MS = Math.max(0, Number(CONFIG.petMinGapMs ?? 10000));
// 队列只留最新：超过 TTL 的待播直接丢；真正开播前再按 maxAge 兜一次，避免念旧消息
const PET_PENDING_TTL_MS = Math.max(2000, Number(CONFIG.pendingTtlMs ?? 20 * 1000));
const PET_PENDING_MAX_AGE_MS = Math.max(1000, Number(CONFIG.pendingMaxAgeMs ?? 15 * 1000));
const PET_MAX_PENDING = Math.max(1, Number(CONFIG.maxPending ?? 2));
const BOOT_GRACE_MS = Math.max(0, Number(CONFIG.bootGraceMs ?? 20 * 1000));
const RELAY_STARTED_AT = Date.now();

// ---- 消息缓存（只在内存，进程重启即清空，不写盘、不进 store 长期记忆库）----
// 目的：同一句话/同一个「1-9」短消息不反复播、不反复合成语音，也不再反复叫 9B 生成旁白。
const CONTENT_CACHE_TTL_MS = Math.max(0, Number(CONFIG.contentCacheTtlMs ?? 10 * 60 * 1000));
const CONTENT_CACHE_MAX = Math.max(10, Number(CONFIG.contentCacheMax ?? 400));
const NARRATE_CACHE_TTL_MS = 30 * 60 * 1000;
const contentCache = new Map(); // 归一化正文 -> { at, message }
const narrateCache = new Map(); // 状态/事件/内容 -> { at, text }
const recentMessages = []; // 仅内存保留最近 N 条，便于排查

/** 归一化正文：去掉空白、标点、表情，只比「实际说了什么」。 */
function contentKeyOf(raw) {
  return String(raw || "")
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, "")
    .slice(0, 64);
}

/** 纯表情、单字、1-9 这类数字、斜杠指令：不值得开口。 */
function isTrivialText(raw) {
  const text = String(raw || "").trim();
  if (!text) return true;
  if (/^[/／]/.test(text)) return true;
  const key = contentKeyOf(text);
  if (!key) return true;
  if (key.length < Math.max(1, Number(CONFIG.minSpeakChars ?? 2))) return true;
  if (/^\d{1,2}$/.test(key)) return true;
  return false;
}

function trimCache(map, max) {
  while (map.size > max) map.delete(map.keys().next().value);
}

function rememberRecent(item) {
  recentMessages.push(item);
  const max = Math.max(10, Number(CONFIG.recentCacheMax ?? 100));
  while (recentMessages.length > max) recentMessages.shift();
}

const SOURCE_LABEL = {
  qq: "QQ机器人",
  wechat: "微信机器人",
  local: "本机",
  dsh: "DeepSeek 工作台",
  pet: "桌宠",
};

/**
 * 昵称清洗：QQ 协议层有时把号码当昵称（notice 事件、缺 card/nickname 时），
 * 这种纯数字/带号码的「名字」一律当没有昵称，绝不念出来。
 */
function sanitizeName(raw) {
  let name = String(raw || "").replace(/[\u200b-\u200d\ufeff]/g, "").trim();
  if (!name) return "";
  name = name
    .replace(/[（(【\[][\s@]*\d{5,}[\s）)】\]]/g, "")
    .replace(/^\d{5,}$/g, "")
    .replace(/\d{6,}/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return name;
}

/**
 * 正文里的号码痕迹：括号码、@号码、以及「号码：正文」这种前缀。
 * 注意不要删掉正文里正常的数字（订单号、金额等）——完整显示优先；语音侧的清洗更激进。
 */
function stripIdNoise(raw) {
  return String(raw || "")
    .replace(/[（(【\[][\s@]*\d{5,}[\s）)】\]]/g, "")
    .replace(/@\d{5,}/g, "")
    .replace(/^@?\d{5,}\s*[：:]\s*/, "");
}

/** 统一解析插件带来的发言人/会话身份，旧插件没有这些字段时退回安全默认值。 */
function identityOf(payload) {
  const senderName = sanitizeName(payload.sender_name);
  const senderId = String(payload.sender_id || "").trim();
  const groupId = String(payload.group_id || "").trim();
  const groupName = String(payload.group_name || "").trim();
  const chatType = String(payload.chat_type || (groupId ? "group" : "private")).toLowerCase();
  const chatName =
    String(payload.chat_name || "").trim() ||
    (chatType === "group" ? groupName || `群 ${groupId}` : "私聊");
  return {
    senderName,
    senderId,
    groupId,
    groupName,
    chatType,
    chatName,
    // 不再回退到 senderId：号码一旦进了气泡/语音，就会被念成一串数字
    sender: senderName || "",
  };
}

/** 只保留发言人昵称，群名/会话信息不再拼进气泡。 */
function speakerLabel(payload) {
  return identityOf(payload).sender || "某人";
}

/** 旁白/气泡里给正文加发言人前缀，只显示「昵称：」。 */
function messagePrefix(payload) {
  return `${identityOf(payload).sender || "某人"}：`;
}

function chatKey(source, payload) {
  const id = identityOf(payload);
  return `${source}|${id.chatType}|${id.groupId || "-"}|${id.senderId || id.sender || "-"}`;
}

/** 播报节流的合并单位：同一个人在一个群/私聊里只留最新一条；同群不同人各播各的。 */
function roomKey(source, payload) {
  return chatKey(source, payload);
}

/** 真正的聊天消息（QQ/微信，且带正文）；其余都算桌宠自身状态/动作。 */
function isChatPayload(payload) {
  const source = String(payload.source || "").toLowerCase();
  const text = String(payload.detail || payload.message || payload.pet_text || "").trim();
  return (source === "qq" || source === "wechat") && Boolean(text);
}

/** 语音实际只念前 60 字；气泡发同一段，字幕/语音/动作才对得上。 */
function speechSlice(raw) {
  const text = stripIdNoise(raw).replace(/\s+/g, " ").trim();
  if (text.length <= 60) return text;
  return text.slice(0, 59) + "…";
}

/** 气泡显示完整正文（超长只做兜底截断）；语音侧会自己截到 60 字。 */
function fitPetText(raw) {
  const text = stripIdNoise(raw).replace(/\s+/g, " ").trim();
  if (text.length <= PET_TEXT_MAX) return text;
  return text.slice(0, PET_TEXT_MAX - 1) + "…";
}

/** 只接受 http/https 完整地址：dsh-pet 的 /broadcast url 不接受本地文件或 base64。 */
function httpImage(value) {
  const url = String(value || "").trim();
  return /^https?:\/\//i.test(url) ? url : "";
}

/** 插件托管到 relay 的本地图片（local:<文件名>）→ 本机 http 地址。 */
function localImage(value) {
  const name = String(value || "").trim();
  if (!/^[a-zA-Z0-9._-]+$/.test(name)) return "";
  return `http://127.0.0.1:${PORT}/pet-image/${encodeURIComponent(name)}`;
}

/** 从插件 payload 里取图片地址（兼容 images 数组与单张 image/url）。 */
function imagesOf(payload) {
  const out = [];
  const list = Array.isArray(payload.images) ? payload.images : [];
  for (const item of list) {
    const raw = typeof item === "string" ? item : item && (item.url || item.file || item.name);
    const hit = String(raw || "").startsWith("local:")
      ? localImage(String(raw).slice(6))
      : httpImage(raw);
    if (hit && !out.includes(hit)) out.push(hit);
    if (out.length >= PET_IMAGE_MAX) return out;
  }
  const single = httpImage(payload.image) || httpImage(payload.url) || httpImage(payload.image_url);
  if (single && !out.includes(single)) out.push(single);
  return out;
}

/** 本地图片的 Content-Type：先看扩展名，再看文件头（协议端临时文件常没有后缀）。 */
function imageMime(data, name) {
  const ext = path.extname(String(name || "")).toLowerCase();
  const byExt = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
  };
  if (byExt[ext]) return byExt[ext];
  if (data && data.length >= 4) {
    if (data[0] === 0x89 && data[1] === 0x50) return "image/png";
    if (data[0] === 0xff && data[1] === 0xd8) return "image/jpeg";
    if (data[0] === 0x47 && data[1] === 0x49) return "image/gif";
    if (data.toString("ascii", 0, 4) === "RIFF") return "image/webp";
  }
  return "application/octet-stream";
}

/** 定期清掉插件复制过来的旧图，别让 pet-images 无限增长。 */
function cleanupPetImages(maxAgeMs = 6 * 60 * 60 * 1000) {
  fs.readdir(PET_IMAGE_DIR, (err, names) => {
    if (err) return;
    const now = Date.now();
    for (const name of names) {
      fs.stat(path.join(PET_IMAGE_DIR, name), (e, st) => {
        if (!e && st.isFile() && now - st.mtimeMs > maxAgeMs) {
          fs.unlink(path.join(PET_IMAGE_DIR, name), () => {});
        }
      });
    }
  });
}

/** 内部状态 -> 官方桌宠状态 */
const STATE_MAP = {
  idle: "IDLE",
  working: "WORKING",
  thinking: "THINKING",
  sleeping: "WAITING",
  happy: "SUCCESS",
  success: "SUCCESS",
  attention: "WAITING",
  waiting: "WAITING",
  error: "ERROR",
  disconnected: "DISCONNECTED",
};

/** 桌宠窗口使用的内部状态名（小写） */
const PET_STATE = {
  IDLE: "idle",
  WORKING: "working",
  THINKING: "thinking",
  WAITING: "sleeping",
  SUCCESS: "happy",
  ERROR: "error",
  DISCONNECTED: "error",
};

const FALLBACK = {
  WORKING: (src) => (src ? `${src} 那边我在忙着回话呢～` : "我在忙着干活呢，主人～"),
  THINKING: () => "让我想想……在泡泡里转一圈再回答～",
  WAITING: () => "我在等下一句，泡泡里安安静静的～",
  SUCCESS: () => "嘿嘿，搞定啦，主人夸夸我嘛～",
  ERROR: () => "哎呀，出了点小状况，主人看一眼～",
  IDLE: () => "我在这儿陪着主人，鲸鱼尾巴都放松啦～",
  DISCONNECTED: () => "咦，信号断啦，主人快接上我～",
};

function cleanNarration(raw) {
  if (!raw) return "";
  let text = String(raw);
  text = text.replace(/<think[\s\S]*?<\/think>/gi, "");
  text = text.replace(/```[\s\S]*?```/g, "");
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  text = lines.length ? lines[lines.length - 1] : "";
  text = text.replace(/^["'“「『]+|["'”」』]+$/g, "");
  text = text.replace(/^大肥鱼[:：]\s*/g, "");
  text = text.replace(/\s+/g, " ").trim();
  if (text.length > 42) text = text.slice(0, 42) + "…";
  return text;
}

function narrationText(payload, petState, srcLabel) {
  const id = identityOf(payload);
  const who = id.sender || "某人";
  return (
    `来源：${srcLabel || "本机"}\n` +
    `发言人：${who}\n` +
    `状态：${petState}\n` +
    `事件：${payload.event || "无"}\n` +
    `内容：${stripIdNoise(payload.detail || "无").slice(0, 240)}\n` +
    "请用一句中文旁白播报，点明是谁说了什么，不要提群名或会话信息。"
  );
}

const BRAIN_URL = CONFIG.brain || "http://127.0.0.1:23402";
const BRAIN_TIMEOUT_MS = Number(CONFIG.brainTimeoutMs || 10000);
// brain 断路器：低配机可能没装 Ollama，brain 又恰好离线时，
// 别让每条消息都白等满 BRAIN_TIMEOUT_MS。连续失败 2 次后进入 60s 冷却，
// 冷却期内直接跳过 brain 走 FALLBACK 文案；冷却期满放行一次探测，成功即复位。
const BRAIN_BREAK_THRESHOLD = 2;
const BRAIN_BREAK_COOLDOWN_MS = 60 * 1000;
let brainFailCount = 0;
let brainBlockedUntil = 0;

function brainDown() {
  brainFailCount += 1;
  if (brainFailCount >= BRAIN_BREAK_THRESHOLD) {
    brainBlockedUntil = Date.now() + BRAIN_BREAK_COOLDOWN_MS;
  }
}

function brainUp() {
  brainFailCount = 0;
  brainBlockedUntil = 0;
}

/** 先走 brain（它会串到 model），失败或超时再退回本机 Ollama。 */
function askBrain(user, payload, srcLabel) {
  return new Promise((resolve) => {
    if (Date.now() < brainBlockedUntil) return resolve("");
    let target;
    try {
      target = new URL("/chat", BRAIN_URL);
    } catch (err) {
      return resolve("");
    }
    const id = identityOf(payload);
    const source = String(payload.source || "local").toLowerCase();
    const body = JSON.stringify({
      text: user,
      role: "桌宠",
      actor: {
        platform: source,
        user_id: id.senderId || id.sender || "unknown",
        name: id.sender || srcLabel || "桌宠",
      },
      chat: {
        id: id.groupId || source,
        type: id.chatType,
        name: id.chatName || srcLabel || "桌宠",
      },
    });
    const req = http.request(
      {
        hostname: target.hostname,
        port: target.port || 80,
        path: target.pathname,
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
        timeout: BRAIN_TIMEOUT_MS,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          try {
            const json = JSON.parse(data);
            const reply = json && json.ok ? cleanNarration(json.data && json.data.reply) : "";
            if (reply) brainUp();
            else brainDown();
            resolve(reply);
          } catch (err) {
            brainDown();
            resolve("");
          }
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      brainDown();
      resolve("");
    });
    req.on("error", () => {
      brainDown();
      resolve("");
    });
    req.write(body);
    req.end();
  });
}

async function askOllama(payload, petState, srcLabel) {
  const viaBrain = await askBrain(narrationText(payload, petState, srcLabel), payload, srcLabel);
  if (viaBrain) return viaBrain;
  return askOllamaDirect(payload, petState, srcLabel);
}

// Ollama 回退同样加断路器：没装 Ollama 的机器上别让每次旁白都白等满超时。
const OLLAMA_BREAK_THRESHOLD = 2;
const OLLAMA_BREAK_COOLDOWN_MS = 10 * 60 * 1000; // 本机大模型基本不会自己复活，冷却给长一点
let ollamaFailCount = 0;
let ollamaBlockedUntil = 0;

function askOllamaDirect(payload, petState, srcLabel) {
  return new Promise((resolve) => {
    if (Date.now() < ollamaBlockedUntil) return resolve("");
    const system =
      "你是「大肥鱼」，一只住在主人桌面上的蓝色鲸鱼女仆桌宠。你的唯一工作：" +
      "把机器人收到的消息用一句中文旁白说出来。要求：不超过36个汉字；旁白里必须点明" +
      "是谁说了什么，但不要提群名、群号或会话信息；" +
      "语气元气、亲昵，称对方为主人；可以带一点鲸鱼、泡泡、海洋的意象；" +
      "不解释规则，不加引号，不用表情符号，不编造输入里没有的细节，只输出这一句旁白。";
    const user = narrationText(payload, petState, srcLabel);

    const body = JSON.stringify({
      model: CONFIG.model,
      system,
      prompt: user,
      stream: false,
      think: false,
      keep_alive: "30m",
      options: { temperature: 0.8, top_p: 0.9, num_predict: 64 },
    });

    const target = new URL("/api/generate", CONFIG.ollama);
    const req = http.request(
      {
        hostname: target.hostname,
        port: target.port || 11434,
        path: target.pathname,
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
        timeout: CONFIG.timeoutMs,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          try {
            const json = JSON.parse(data);
            const reply = cleanNarration(json.response);
            ollamaFailCount = reply ? 0 : ollamaFailCount + 1;
            if (ollamaFailCount >= OLLAMA_BREAK_THRESHOLD) {
              ollamaBlockedUntil = Date.now() + OLLAMA_BREAK_COOLDOWN_MS;
            }
            resolve(reply);
          } catch (err) {
            ollamaFailCount += 1;
            if (ollamaFailCount >= OLLAMA_BREAK_THRESHOLD) {
              ollamaBlockedUntil = Date.now() + OLLAMA_BREAK_COOLDOWN_MS;
            }
            resolve("");
          }
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      ollamaFailCount += 1;
      if (ollamaFailCount >= OLLAMA_BREAK_THRESHOLD) {
        ollamaBlockedUntil = Date.now() + OLLAMA_BREAK_COOLDOWN_MS;
      }
      resolve("");
    });
    req.on("error", () => {
      ollamaFailCount += 1;
      if (ollamaFailCount >= OLLAMA_BREAK_THRESHOLD) {
        ollamaBlockedUntil = Date.now() + OLLAMA_BREAK_COOLDOWN_MS;
      }
      resolve("");
    });
    req.write(body);
    req.end();
  });
}

const STORE_URL = process.env.DAFEIYU_STORE_URL || "http://127.0.0.1:23407";

// 桌宠收到的 QQ/微信消息也写进统一记忆库（services/store），和其它角色/平台同一套方案。
function recordMemory(scope, kind, text, extra = {}) {
  try {
    const target = new URL("/messages", STORE_URL);
    const body = JSON.stringify({ scope, kind, text, role: "大肥鱼", ...extra });
    const req = http.request(
      {
        hostname: target.hostname,
        port: target.port || 80,
        path: target.pathname,
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
        timeout: 2000,
      },
      (res) => res.resume()
    );
    req.on("timeout", () => req.destroy());
    req.on("error", () => {});
    req.write(body);
    req.end();
  } catch (_) {
    /* 记忆库不可用时不影响桌宠播报 */
  }
}

/** 把最终旁白推给桌宠：默认 dafeiyu3d，也支持当前使用的 dsh-pet（/broadcast）。 */
function injectToPet(message) {
  return new Promise((resolve) => {
    let target;
    try {
      target = new URL(CONFIG.petPath || "/state", (CONFIG.pet || "http://127.0.0.1:23335") + "/");
    } catch (err) {
      return resolve({ ok: false, error: "bad pet url" });
    }
    const kind = CONFIG.petKind === "dsh-pet" || target.pathname.includes("/broadcast") ? "dsh-pet" : "dafeiyu3d";
    let body;
    if (kind === "dsh-pet") {
      // 气泡、语音、时长对齐在同一段文本上：
      // - 语音只念前 60 字，气泡也只发这 60 字，避免「字幕还没念完，动作先换下一个」；
      // - anim 不发：桌宠自己选说话动作，发非法值反而会乱插一个跟内容无关的动作。
      const raw = String(message.message || "").trim() || cleanLatest(message.detail || "");
      const text = fitPetText(speechSlice(raw));
      const url = httpImage(message.url);
      const duration = Math.min(60000, Math.max(10000, text.length * 320 + (url ? 4000 : 0)));
      body = JSON.stringify({ text, ...(url ? { url } : {}), duration });
    } else {
      body = JSON.stringify({
        state: PET_STATE[message.state] || "idle",
        source: message.source || lastStatus.source,
        event: message.activity || "",
        detail: message.detail || "",
        pet_text: message.message || "",
        session_id: message.sessionId || "",
      });
    }
    const req = http.request(
      {
        hostname: target.hostname,
        port: target.port || 80,
        path: target.pathname + (target.search || ""),
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
        timeout: 4000,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => resolve({ ok: res.statusCode === 200, status: res.statusCode, body: data }));
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, error: "pet timeout" });
    });
    req.on("error", (err) => resolve({ ok: false, error: err.message }));
    req.write(body);
    req.end();
  });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 128 * 1024) {
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

let lastStatus = {
  at: 0,
  source: "local",
  state: "idle",
  message: "",
  sender: "",
  chat: "",
};

function cleanLatest(raw) {
  return stripIdNoise(raw)
    .replace(/^(用户说|回复内容|用户|AI)：?\s*/, "")
    .replace(/\s+/g, " ")
    .trim();
}

// ---- 节流队列：同一会话只保留最新一条待播，按会话/全局最小间隔串行送桌宠 ----
const pendingByChat = new Map(); // key -> { payload, at, ready, timer }
const lastChatAt = new Map(); // key -> 上次真正播报的时间
let lastInjectAt = 0;
let pumpRunning = false;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function dropPending(key) {
  const entry = pendingByChat.get(key);
  if (!entry) return;
  clearTimeout(entry.timer);
  pendingByChat.delete(key);
}

function oldestPendingKey(exceptKey) {
  let key = null;
  let at = Infinity;
  for (const [k, entry] of pendingByChat) {
    if (k === exceptKey) continue;
    if (entry.at < at) {
      at = entry.at;
      key = k;
    }
  }
  return key;
}

function enqueuePet(payload) {
  const source = String(payload.source || "local").toLowerCase();
  const key = roomKey(source, payload);
  const now = Date.now();
  const chat = isChatPayload(payload);

  // 只保留最新一条：任何新消息进来，排队中的旧消息（聊天/自身动作）全部作废。
  // 桌宠只反映「此刻最新」，绝不补播历史队列。
  for (const k of [...pendingByChat.keys()]) dropPending(k);

  const last = lastChatAt.get(key) || 0;
  const wait = Math.max(0, last + PET_CHAT_INTERVAL_MS - now);
  const entry = {
    payload: { ...payload, source, __chat: chat, __at: now, can_push: true },
    at: now,
    ready: false,
    timer: null,
  };
  if (wait > 0) {
    entry.timer = setTimeout(() => {
      entry.ready = true;
      pumpPet();
    }, wait);
    entry.timer.unref?.();
  } else {
    entry.ready = true;
  }
  pendingByChat.set(key, entry);

  // 兜底：队列最多留 maxPending 条，超了从最旧的开始丢
  while (pendingByChat.size > PET_MAX_PENDING) {
    const oldKey = oldestPendingKey(key);
    if (!oldKey) break;
    dropPending(oldKey);
  }

  pumpPet();
  return { queued: true, key };
}

async function pumpPet() {
  if (pumpRunning) return;
  pumpRunning = true;
  try {
    while (true) {
      const now = Date.now();
      for (const [key, entry] of pendingByChat) {
        if (now - entry.at > PET_PENDING_TTL_MS) {
          dropPending(key);
        }
      }
      const ready = [...pendingByChat.entries()].filter(([, entry]) => entry.ready);
      if (!ready.length) break;
      // 只要还有聊天消息在队列里，自身动作就一直往后排
      const hasPendingChat = [...pendingByChat.values()].some((entry) => entry.payload.__chat);
      const usable = ready.filter(([, entry]) => entry.payload.__chat || !hasPendingChat);
      if (!usable.length) break;
      const gap = lastInjectAt + PET_MIN_GAP_MS - now;
      if (gap > 0) {
        await sleep(gap);
        continue;
      }
      usable.sort(
        (a, b) =>
          Number(b[1].payload.__chat) - Number(a[1].payload.__chat) || a[1].at - b[1].at
      );
      const [key, entry] = usable[0];
      // 等待插话的这段时间里可能已被更新的消息顶掉；不是同一条就重新挑
      if (pendingByChat.get(key) !== entry) continue;
      // can_push 是每条消息的一次性闸门：true 才允许推，推完立刻置 false，防止重试/重复播报
      if (entry.payload.can_push === false) {
        dropPending(key);
        continue;
      }
      // 已经放旧了的直接丢，不念历史
      if (Date.now() - entry.at > PET_PENDING_MAX_AGE_MS) {
        dropPending(key);
        continue;
      }
      dropPending(key);
      lastInjectAt = Date.now();
      lastChatAt.set(key, lastInjectAt);
      const delivered = await deliverPet(entry.payload, key);
      // 推发后立即闸门关闭，绝不再推第二次
      entry.payload.can_push = false;
      entry.payload.pushed_at = Date.now();
      entry.payload.pushed = Boolean(delivered && delivered.ok);
    }
  } finally {
    pumpRunning = false;
    if ([...pendingByChat.values()].some((entry) => entry.ready)) setTimeout(pumpPet, 0);
  }
}

/** 真正播报一条：生成一句旁白，再作为唯一 text 送桌宠（字幕与语音同源）。 */
async function deliverPet(payload, key) {
  // 一次性推发闸门：false 的消息永不再推
  if (payload && payload.can_push === false) return { ok: true, skipped: "can_push=false" };
  const source = String(payload.source || "local").toLowerCase();
  const srcLabel = SOURCE_LABEL[source] || source;
  const internalState = String(payload.state || "working").toLowerCase();
  const petState = STATE_MAP[internalState] || "WORKING";
  const ident = identityOf(payload);

  const rawDetail = stripIdNoise(String(payload.detail || ""));
  const detail = rawDetail ? `${messagePrefix(payload)}${cleanLatest(rawDetail)}` : "";
  const images = imagesOf(payload);

  // 气泡/语音只显示「昵称：消息」；没有正文时才退回模型旁白。
  let message = stripIdNoise(String(payload.message || payload.pet_text || "")).trim();
  if (!message && detail) message = detail;
  if (!message && CONFIG.narrate) {
    // 同一状态/事件/内容的旁白直接复用，不再反复叫 9B 生成。
    const cacheKey = ["narrate", internalState, source, payload.event || "", contentKeyOf(payload.detail || "")].join("|");
    const hit = narrateCache.get(cacheKey);
    if (hit && Date.now() - hit.at < NARRATE_CACHE_TTL_MS) {
      message = hit.text;
    } else {
      message = await askOllama(payload, internalState, srcLabel);
      if (message) {
        narrateCache.set(cacheKey, { at: Date.now(), text: message });
        trimCache(narrateCache, 200);
      }
    }
  }
  if (!message && images.length) message = "发来一张图片";
  if (!message) {
    const build = FALLBACK[petState] || FALLBACK.WORKING;
    message = build(srcLabel);
  }
  // 同上：纯表情/单字/1-9 不播；相同内容在缓存窗口内只播一次，省一次语音合成。
  // 缓存键用「原始正文」而不是加了「昵称：」前缀的气泡文本，才能和 handleStatus 里的预检命中同一个键。
  const dedupeSource = String(payload.detail || payload.message || payload.pet_text || "").trim();
  const contentKey = contentKeyOf(dedupeSource) || contentKeyOf(message);
  if (!images.length && contentKey) {
    if (isTrivialText(dedupeSource)) return { ok: true, skipped: "trivial" };
    const cached = contentCache.get(contentKey);
    if (cached && Date.now() - cached.at < CONTENT_CACHE_TTL_MS) {
      console.log(`[relay] skip cached ${key} content=${contentKey.slice(0, 16)}`);
      return { ok: true, skipped: "cached" };
    }
  }
  const result = await injectToPet({
    state: petState,
    source,
    message,
    detail,
    url: images[0],
    activity: String(payload.event || "").slice(0, 60) || undefined,
    sessionId: `bot-${key}`,
  });

  lastStatus = {
    at: Date.now(),
    source,
    state: petState,
    message,
    sender: ident.sender,
    chat: ident.chatName,
    images,
    inject: result,
  };
  // 已经播过的消息不会留在任何“定时补播”池里（补播已移除）
  if (result.ok) {
    if (contentKey) {
      contentCache.set(contentKey, { at: Date.now(), message });
      trimCache(contentCache, CONTENT_CACHE_MAX);
    }
  }
  console.log(
    `[relay] deliver ${key} ${petState} -> ${result.ok ? "ok" : result.status || result.error}`
  );
  return result;
}

async function handleStatus(payload) {
  const source = String(payload.source || "local").toLowerCase();
  const srcLabel = SOURCE_LABEL[source] || source;
  const internalState = String(payload.state || "working").toLowerCase();
  const petState = STATE_MAP[internalState] || "WORKING";
  const ident = identityOf(payload);
  const key = chatKey(source, payload);

  const latestText = String(payload.detail || payload.message || "").trim();
  if ((source === "qq" || source === "wechat") && latestText) {
    rememberRecent({
      at: Date.now(),
      source,
      chat: ident.chatName,
      sender: ident.sender || srcLabel,
      text: latestText.slice(0, 200),
    });
    // 默认不再把消息写进 store 长期记忆库（避免以后被当作训练语料）；
    // 想恢复就在 config.json 里把 rememberMessages 改成 true。
    if (CONFIG.rememberMessages) {
      recordMemory(`大肥鱼|${source}|notice`, "notice", latestText, {
        platform: source,
        chat_type: ident.chatType,
        chat_id: ident.groupId || source,
        chat_name: ident.chatName,
        sender_name: ident.sender || srcLabel,
        sender_id: ident.senderId,
      });
    }
  }

  // 刚启动的一小段时间只记录、不播，避免把适配器补推的旧消息念出来。
  if (latestText && (source === "qq" || source === "wechat") &&
      Date.now() - RELAY_STARTED_AT < BOOT_GRACE_MS) {
    return { ok: true, queued: false, skipped: "boot-grace", state: petState, key };
  }
  // 只播报「有内容」的消息：纯表情、单字、1-9、斜杠指令直接放过，不打扰桌宠。
  if (latestText && !imagesOf(payload).length && isTrivialText(latestText)) {
    return { ok: true, queued: false, skipped: "trivial", state: petState, key };
  }
  // 相同内容在缓存窗口内只播一次，省掉重复的旁白与语音合成。
  if (latestText && !imagesOf(payload).length) {
    const cached = contentCache.get(contentKeyOf(latestText));
    if (cached && Date.now() - cached.at < CONTENT_CACHE_TTL_MS) {
      return { ok: true, queued: false, skipped: "cached", state: petState, key };
    }
  }

  // 立刻返回，旁白生成和送桌宠都交给节流队列，避免插件端等待和队列堆积
  const queued = enqueuePet({ ...payload, source, state: internalState });

  return {
    ok: true,
    queued: queued.queued,
    merged: Boolean(queued.merged),
    state: petState,
    sender: speakerLabel(payload),
    chat: ident.chatName,
    key,
  };
}

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

    if (req.method === "GET" && url.pathname.startsWith("/pet-image/")) {
      const name = decodeURIComponent(url.pathname.slice("/pet-image/".length));
      if (!/^[a-zA-Z0-9._-]+$/.test(name)) {
        return sendJson(res, 400, { ok: false, error: "bad image name" });
      }
      return fs.readFile(path.join(PET_IMAGE_DIR, name), (err, data) => {
        if (err) return sendJson(res, 404, { ok: false, error: "image not found" });
        res.writeHead(200, {
          "Content-Type": imageMime(data, name),
          "Content-Length": data.length,
          "Cache-Control": "no-store",
          "Access-Control-Allow-Origin": "*",
        });
        res.end(data);
      });
    }

    if (req.method === "GET" && url.pathname === "/health") {
      return sendJson(res, 200, {
        ok: true,
        service: "dafeiyu-pet-relay",
        port: PORT,
        model: CONFIG.model,
        brain: BRAIN_URL,
        pet: CONFIG.pet,
        periodic: { enabled: false },
        pending: {
          count: pendingByChat.size,
          ttlMs: PET_PENDING_TTL_MS,
          maxAgeMs: PET_PENDING_MAX_AGE_MS,
          max: PET_MAX_PENDING,
          bootGraceLeftMs: Math.max(0, BOOT_GRACE_MS - (Date.now() - RELAY_STARTED_AT)),
          items: [...pendingByChat.entries()].map(([k, entry]) => ({
            key: k,
            at: entry.at,
            ageMs: Date.now() - entry.at,
            can_push: entry.payload.can_push !== false,
            pushed: Boolean(entry.payload.pushed),
            text: String(entry.payload.detail || entry.payload.message || "").slice(0, 80),
          })),
        },
        cache: {
          rememberMessages: Boolean(CONFIG.rememberMessages),
          contentTtlMs: CONTENT_CACHE_TTL_MS,
          content: contentCache.size,
          narrate: narrateCache.size,
          recent: recentMessages.length,
        },
        breakers: {
          brain: { blocked: Date.now() < brainBlockedUntil, fails: brainFailCount, retryInMs: Math.max(0, brainBlockedUntil - Date.now()) },
          ollama: { blocked: Date.now() < ollamaBlockedUntil, fails: ollamaFailCount, retryInMs: Math.max(0, ollamaBlockedUntil - Date.now()) },
        },
        last: lastStatus,
      });
    }

    if (req.method === "GET" && url.pathname === "/last") {
      return sendJson(res, 200, lastStatus);
    }

    // 只读的内存缓存视图：看最近收到的消息，便于排查，不涉及 store 长期记忆库。
    if (req.method === "GET" && url.pathname === "/recent") {
      return sendJson(res, 200, { ok: true, items: recentMessages });
    }

    if (req.method === "POST" && (url.pathname === "/state" || url.pathname === "/")) {
      const raw = await readBody(req);
      let payload;
      try {
        payload = raw ? JSON.parse(raw) : {};
      } catch (err) {
        return sendJson(res, 400, { ok: false, error: "invalid json" });
      }
      const result = await handleStatus(payload);
      return sendJson(res, result.ok ? 200 : 502, result);
    }

    sendJson(res, 404, { ok: false, error: "not found" });
  } catch (err) {
    sendJson(res, 500, { ok: false, error: String(err && err.message ? err.message : err) });
  }
});

server.on("error", (err) => {
  console.error("[relay] server error:", err.message);
  process.exit(1);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[relay] listening on http://127.0.0.1:${PORT}`);
  console.log(`[relay] narrate brain=${BRAIN_URL} (fallback ollama=${CONFIG.ollama})`);
  console.log(`[relay] pet display -> ${CONFIG.pet}${CONFIG.petPath || "/state"}`);
  console.log(
    `[relay] pending max=${PET_MAX_PENDING} ttl=${PET_PENDING_TTL_MS}ms maxAge=${PET_PENDING_MAX_AGE_MS}ms (no historical replay)`
  );
  console.log(
    `[relay] cache ttl=${CONTENT_CACHE_TTL_MS}ms max=${CONTENT_CACHE_MAX} rememberStore=${CONFIG.rememberMessages}`
  );
  cleanupPetImages();
  setInterval(cleanupPetImages, 30 * 60 * 1000).unref?.();
});
