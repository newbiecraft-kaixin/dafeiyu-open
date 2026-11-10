"use strict";

/**
 * 大肥鱼桌宠 · 机器人状态中转
 *
 * 改造自 clawd-on-desk（https://github.com/rullerzhou-afk/clawd-on-desk，AGPL-3.0）；
 * 本文件在原项目的窗口 / 气泡 / 动作 / 语音链路上做了修改，修改时间 2026-11。
 * 完整署名与再分发要求见仓库根目录 NOTICE。
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
const https = require("https");
const os = require("os");
const path = require("path");

const ROOT = __dirname;
const CONFIG_PATH = path.join(ROOT, "config.json");
// 插件把协议端的本地图片复制到 <根目录>/bots/pet-images（见插件 main.py），
// relay 用 /pet-image/<name> 转成 http 地址给桌宠；旧目录只做兼容读取。
const PET_IMAGE_DIR = path.join(ROOT, "..", "bots", "pet-images");
const LEGACY_PET_IMAGE_DIR = path.join(ROOT, "pet-images");
// 图片保护：单张最大 20MB、远程拉取 6s 超时，避免超大 GIF 把桌宠气泡卡住。
const PET_IMAGE_MAX_BYTES = 20 * 1024 * 1024;
const PET_IMAGE_FETCH_TIMEOUT_MS = 6000;

const DEFAULTS = {
  port: 23334,
  pet: "http://127.0.0.1:23335",
  petPath: "/state",
  petKind: "auto",
  ollama: "http://127.0.0.1:11434",
  model: "qwen3.5:9b",
  narrate: true,
  timeoutMs: 15000,
  // 待播槽存活 90s：足以撑过一句最长 45s 的播报，又不会把半分钟前的消息一直留着
  pendingTtlMs: 90 * 1000,
  // 真正送桌宠时再卡一次新鲜度：超过 30s 的旧消息直接丢，绝不能念两分钟前的话
  pendingMaxAgeMs: 30 * 1000,
  // 桌宠一句 CosyVoice 合成要 15~60s，但宿主语音队列只有 3 格；
  // 这里只留「1 条在途 + 1 条最新待播」，多出来的直接覆盖，避免越排越多
  maxPending: 2,
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
// pendingMaxAgeMs = 0 表示「不按年龄丢」：桌宠忙/重启时宁可晚一点播，也不吞掉这条字幕
const PET_PENDING_MAX_AGE_MS = (() => {
  const raw = Number(CONFIG.pendingMaxAgeMs ?? 15 * 1000);
  return raw <= 0 ? 0 : Math.max(1000, raw);
})();
const PET_MAX_PENDING = Math.max(1, Number(CONFIG.maxPending ?? 2));
const BOOT_GRACE_MS = Math.max(0, Number(CONFIG.bootGraceMs ?? 20 * 1000));
const RELAY_STARTED_AT = Date.now();

// ---- 字幕必显（forceSubtitle，默认开）----
// 默认行为：不再因为「内容重复 / 纯表情 / 单字」静默丢掉字幕；推给桌宠失败会重试，
// 仍失败就回队等桌宠恢复，宁可晚几秒也不让一条消息没有字幕。
// 唯一仍然跳过的是斜杠指令（/余额 之类），那是命令不是聊天内容，机器人自己的回复照样会显示。
const FORCE_SUBTITLE = CONFIG.forceSubtitle !== false;
const INJECT_RETRY = Math.max(1, Number(CONFIG.injectRetry ?? 3));
const INJECT_RETRY_GAP_MS = Math.max(200, Number(CONFIG.injectRetryGapMs ?? 1200));
const REQUEUE_MAX = Math.max(0, Number(CONFIG.requeueMax ?? 8));
const REQUEUE_DELAY_MS = Math.max(500, Number(CONFIG.requeueDelayMs ?? 3000));
const skipStats = { delivered: 0, injectFail: 0, requeued: 0, giveUp: 0, ttl: 0, stale: 0, trivial: 0, cached: 0, bootGrace: 0, command: 0 };

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
 * 字幕脏字符清洗：协议层偶尔会把控制字符、占位标记（例如「<$ÿĀ\x11\x10>」）
 * 塞进群名/昵称/正文，原样送桌宠就会出现乱码方块，甚至把控制字符念进语音。
 * 统一在入口剥掉，保证气泡和语音只拿到可读文本。
 */
function sanitizeJunk(raw) {
  return String(raw || "")
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, " ")       // C0/C1 控制字符
    .replace(/[\u200b-\u200f\u2028-\u202f\ufeff]/g, "")  // 零宽 / 行分隔
    .replace(/<[^<>\u4e00-\u9fff]{0,40}>/g, "")          // 「<...>」纯符号占位
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * 昵称清洗：QQ 协议层有时把号码当昵称（notice 事件、缺 card/nickname 时），
 * 这种纯数字/带号码的「名字」一律当没有昵称，绝不念出来。
 */
function sanitizeName(raw) {
  let name = sanitizeJunk(raw);
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
  return sanitizeJunk(raw)
    .replace(/[（(【\[][\s@]*\d{5,}[\s）)】\]]/g, "")
    .replace(/@\d{5,}/g, "")
    .replace(/^@?\d{5,}\s*[：:]\s*/, "");
}

/** 统一解析插件带来的发言人/会话身份，旧插件没有这些字段时退回安全默认值。 */
function identityOf(payload) {
  const senderName = sanitizeName(payload.sender_name);
  const senderId = String(payload.sender_id || "").trim();
  const groupId = String(payload.group_id || "").trim();
  const groupName = sanitizeJunk(payload.group_name);
  const chatType = String(payload.chat_type || (groupId ? "group" : "private")).toLowerCase();
  const chatName =
    sanitizeJunk(payload.chat_name) ||
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

/**
 * 长消息按句切段：桌宠语音只念 60 字，以前超出的部分直接被「…」截掉，后半句永远没声音。
 * 这里按句末标点切成多段、每段不超过 60 字，交给 deliverPet 逐段投递，每段各配一段声音。
 */
function splitSpeech(raw, max = 60) {
  const text = stripIdNoise(raw).replace(/\s+/g, " ").trim();
  if (!text) return [];
  if (text.length <= max) return [text];
  const sentences = text.match(/[^。！？；…!?;]+[。！？；…!?;]?/g) || [text];
  const out = [];
  let buf = "";
  for (const sentence of sentences) {
    if (buf && (buf + sentence).length > max) {
      out.push(buf);
      buf = "";
    }
    if (sentence.length > max) {
      if (buf) {
        out.push(buf);
        buf = "";
      }
      for (let i = 0; i < sentence.length; i += max) out.push(sentence.slice(i, i + max));
    } else {
      buf += sentence;
    }
  }
  if (buf) out.push(buf);
  return out.map((item) => item.trim()).filter(Boolean);
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

/** 发言人头像：插件带来的 http 地址；QQ 没带时按号码现拼公开头像地址。 */
function avatarOf(payload, senderId) {
  const fromPayload = httpImage(payload.sender_avatar);
  if (fromPayload) return fromPayload;
  const id = String(senderId || "").trim();
  if (/^\d{5,11}$/.test(id)) return `https://q1.qlogo.cn/g?b=qq&nk=${id}&s=100`;
  return "";
}

/** 插件托管到 relay 的本地图片（local:<文件名>）→ 本机 http 地址。 */
function localImage(value) {
  const name = String(value || "").trim();
  if (!/^[a-zA-Z0-9._-]+$/.test(name)) return "";
  return `http://127.0.0.1:${PORT}/pet-image/${encodeURIComponent(name)}`;
}

/** 远程图片统一走 relay 代理：6s 超时 + 20MB 上限，按原始字节透传（GIF 仍能动）。 */
function proxyImage(value) {
  const remote = httpImage(value);
  if (!remote) return "";
  return `http://127.0.0.1:${PORT}/pet-image-proxy?url=${encodeURIComponent(remote)}`;
}

/** 从插件 payload 里取图片地址（兼容 images 数组与单张 image/url）。 */
function imagesOf(payload) {
  const out = [];
  const list = Array.isArray(payload.images) ? payload.images : [];
  for (const item of list) {
    const raw = typeof item === "string" ? item : item && (item.url || item.file || item.name);
    const hit = String(raw || "").startsWith("local:")
      ? localImage(String(raw).slice(6))
      : proxyImage(raw);
    if (hit && !out.includes(hit)) out.push(hit);
    if (out.length >= PET_IMAGE_MAX) return out;
  }
  const single = proxyImage(payload.image || payload.url || payload.image_url);
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

/** 读本地图并回给桌宠：超过 20MB 回 413，宁可不显示也不把气泡拖死。 */
function sendImageFile(file, res) {
  fs.stat(file, (err, st) => {
    if (err || !st.isFile()) return sendJson(res, 404, { ok: false, error: "image not found" });
    if (st.size > PET_IMAGE_MAX_BYTES) {
      return sendJson(res, 413, { ok: false, error: "image too large" });
    }
    fs.readFile(file, (readErr, data) => {
      if (readErr) return sendJson(res, 404, { ok: false, error: "image not found" });
      res.writeHead(200, {
        "Content-Type": imageMime(data, file),
        "Content-Length": data.length,
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
      });
      res.end(data);
    });
  });
}

/** 新目录优先，旧 relay/pet-images 只做兼容兜底。 */
function servePetImage(name, res) {
  const primary = path.join(PET_IMAGE_DIR, name);
  fs.stat(primary, (err, st) => {
    if (!err && st.isFile()) return sendImageFile(primary, res);
    sendImageFile(path.join(LEGACY_PET_IMAGE_DIR, name), res);
  });
}

/** 远程图片代理：跟随最多 3 次跳转，6s 超时，20MB 上限，按原始字节透传。 */
function proxyImageTo(res, target, redirects = 0) {
  let parsed;
  try {
    parsed = new URL(String(target || ""));
  } catch (_) {
    return sendJson(res, 400, { ok: false, error: "bad image url" });
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return sendJson(res, 400, { ok: false, error: "bad image protocol" });
  }
  const client = parsed.protocol === "https:" ? https : http;
  const req = client.get(
    parsed,
    { headers: { "User-Agent": "Mozilla/5.0", Accept: "image/*,*/*;q=0.8" } },
    (up) => {
      const code = Number(up.statusCode || 0);
      if ([301, 302, 303, 307, 308].includes(code) && up.headers.location && redirects < 3) {
        up.resume();
        let next;
        try {
          next = new URL(up.headers.location, parsed).toString();
        } catch (_) {
          return sendJson(res, 502, { ok: false, error: "bad redirect" });
        }
        return proxyImageTo(res, next, redirects + 1);
      }
      if (code !== 200) {
        up.resume();
        return sendJson(res, 502, { ok: false, error: `upstream ${code}` });
      }
      const type = String(up.headers["content-type"] || "").toLowerCase().split(";")[0];
      if (!type.startsWith("image/")) {
        up.resume();
        return sendJson(res, 415, { ok: false, error: "not an image" });
      }
      const declared = Number(up.headers["content-length"] || 0);
      if (declared > PET_IMAGE_MAX_BYTES) {
        up.destroy();
        return sendJson(res, 413, { ok: false, error: "image too large" });
      }
      const chunks = [];
      let total = 0;
      let done = false;
      up.on("data", (chunk) => {
        if (done) return;
        total += chunk.length;
        if (total > PET_IMAGE_MAX_BYTES) {
          done = true;
          up.destroy();
          return sendJson(res, 413, { ok: false, error: "image too large" });
        }
        chunks.push(chunk);
      });
      up.on("end", () => {
        if (done) return;
        done = true;
        const data = Buffer.concat(chunks);
        const sniffed = imageMime(data, "");
        res.writeHead(200, {
          "Content-Type": sniffed.startsWith("image/") ? sniffed : type,
          "Content-Length": data.length,
          "Cache-Control": "no-store",
          "Access-Control-Allow-Origin": "*",
        });
        res.end(data);
      });
      up.on("error", () => {
        if (done) return;
        done = true;
        if (!res.headersSent) sendJson(res, 502, { ok: false, error: "image fetch failed" });
      });
    }
  );
  req.setTimeout(PET_IMAGE_FETCH_TIMEOUT_MS, () => req.destroy(new Error("image timeout")));
  req.on("error", () => {
    if (!res.headersSent) sendJson(res, 502, { ok: false, error: "image fetch failed" });
  });
}

/** 定期清掉插件复制过来的旧图，别让 pet-images 无限增长。 */
function cleanupPetImages(maxAgeMs = 6 * 60 * 60 * 1000) {
  const now = Date.now();
  for (const dir of [PET_IMAGE_DIR, LEGACY_PET_IMAGE_DIR]) {
    fs.readdir(dir, (err, names) => {
      if (err) return;
      for (const name of names) {
        fs.stat(path.join(dir, name), (e, st) => {
          if (!e && st.isFile() && now - st.mtimeMs > maxAgeMs) {
            fs.unlink(path.join(dir, name), () => {});
          }
        });
      }
    });
  }
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
      "你是「大肥鱼」，一只住在主人桌面上的蓝色鲸鱼娘女仆桌宠。你的唯一工作：" +
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
      keep_alive: "5m", // 8GB 显存扛不住多模型长驻留：旁白模型用完 5 分钟就卸，给语音/视觉让路
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

// ---- DSH 网页 token（3080）----
// DSH 启动时把带 token 的地址打印到 ~/.dsh/web-server.out.log；中继是后台进程，
// 没有浏览器的登录 cookie，向 3080 推送插件动作时必须自己补 token，否则会被 401/403 拦掉。
const DSH_LOG = path.join(os.homedir(), ".dsh", "web-server.out.log");
const DSH_TOKEN_TTL_MS = 5000;
let dshTokenCache = { at: 0, token: "" };

function dshToken() {
  const now = Date.now();
  if (now - dshTokenCache.at < DSH_TOKEN_TTL_MS) return dshTokenCache.token;
  let token = "";
  try {
    const text = fs.readFileSync(DSH_LOG, "utf8");
    const matches = text.match(/[?&]token=([A-Za-z0-9_-]+)/g) || [];
    if (matches.length) token = matches[matches.length - 1].replace(/^[?&]token=/, "");
  } catch (_) {
    token = "";
  }
  dshTokenCache = { at: now, token };
  return token;
}

function resetDshToken() {
  dshTokenCache = { at: 0, token: "" };
}

/** 只给 3080 的请求补 token；显式带了 token 或不是 DSH 地址就不动。 */
function applyDshToken(target) {
  try {
    if (Number(target.port || 80) !== 3080 || target.searchParams.has("token")) return;
    const token = dshToken();
    if (token) target.searchParams.set("token", token);
  } catch (_) {
    /* URL 异常时保持原样 */
  }
}

// ---- 桌宠显示面（surface）----
// dsh-pet 的 /broadcast 要求 surface 与 pets[].display 一致（desktop / web）。
// 不一致时它仍回 200，但 body 是 {ok:false, reason:"pet-not-displayed"}——看着成功，其实一个字没播。
const PET_SURFACE_TTL_MS = 60000;
let petSurfaceCache = { at: 0, surface: "" };

function resetPetSurface() {
  petSurfaceCache = { at: 0, surface: "" };
}

/** 读 /config 里 main 的 display（web/desktop），60 秒缓存；读不到就返回空串走原配置。 */
function petSurface() {
  const now = Date.now();
  if (now - petSurfaceCache.at < PET_SURFACE_TTL_MS) return Promise.resolve(petSurfaceCache.surface);
  let target;
  try {
    const base = new URL(CONFIG.petPath || "/state", (CONFIG.pet || "http://127.0.0.1:23335") + "/");
    target = new URL(base.toString());
    target.pathname = target.pathname.replace(/\/[^/]*$/, "/config");
  } catch (_) {
    return Promise.resolve("");
  }
  applyDshToken(target);
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: target.hostname,
        port: target.port || 80,
        path: target.pathname + (target.search || ""),
        method: "GET",
        timeout: 2000,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          let surface = "";
          try {
            const cfg = JSON.parse(data);
            const pets = [];
            const walk = (node) => {
              if (!node || typeof node !== "object") return;
              if (Array.isArray(node)) return node.forEach(walk);
              if (Array.isArray(node.pets)) pets.push(...node.pets);
              Object.values(node).forEach(walk);
            };
            walk(cfg);
            const main = pets.find((p) => p && p.id === "main") || pets[0];
            if (main && typeof main.display === "string") surface = main.display;
          } catch (_) {
            surface = "";
          }
          petSurfaceCache = { at: Date.now(), surface };
          resolve(surface);
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve("");
    });
    req.on("error", () => resolve(""));
    req.end();
  });
}

/** 把最终旁白推给桌宠：默认 dafeiyu3d，也支持当前使用的 dsh-pet（/broadcast）。 */
function injectToPet(message) {
  return (async () => {
    let last = { ok: false, error: "pet unreachable", durationMs: 0 };
    for (let attempt = 1; attempt <= INJECT_RETRY; attempt += 1) {
      last = await injectOnce(message);
      if (last.ok) return last;
      // 4xx 是参数问题（pet id 不存在 / text 为空），重试没有意义；408/429 除外
      // 401/403 多半是 DSH 换了网页 token：清缓存后重取再试
      const code = Number(last.status || 0);
      if (code === 401 || code === 403) resetDshToken();
      else if (code >= 400 && code < 500 && code !== 408 && code !== 429) break;
      // 显示面刚被切换时缓存可能过期：清掉缓存重读 display 再试一次
      else if (last.body && last.body.includes("pet-not-displayed")) resetPetSurface();
      else if (last.reason) break;
      if (attempt < INJECT_RETRY) await sleep(INJECT_RETRY_GAP_MS);
    }
    return last;
  })();
}

async function injectOnce(message) {
  let target;
  try {
    target = new URL(CONFIG.petPath || "/state", (CONFIG.pet || "http://127.0.0.1:23335") + "/");
  } catch (err) {
    return { ok: false, error: "bad pet url", durationMs: 0 };
  }
  applyDshToken(target);
  const kind = CONFIG.petKind === "dsh-pet" || target.pathname.includes("/broadcast") ? "dsh-pet" : "dafeiyu3d";
  if (kind === "dsh-pet" && target.searchParams.has("surface")) {
    const surface = await petSurface();
    // display=both 时不能把 "both" 原样塞进 surface：宿主只认 desktop / web，
    // 认不出就当作非桌面端，于是桌面桌宠只显示气泡不出声。这种情况保留原配置的 desktop。
    if (surface === "desktop" || surface === "web") target.searchParams.set("surface", surface);
  }
  let body;
  let durationMs = 0;
  if (kind === "dsh-pet") {
    // 气泡、语音、时长对齐在同一段文本上：
    // - 语音只念前 60 字，气泡也只发这 60 字，避免「字幕还没念完，动作先换下一个」；
    // - anim 不发：桌宠自己选说话动作，发非法值反而会乱插一个跟内容无关的动作。
    const raw = String(message.message || "").trim() || cleanLatest(message.detail || "");
    // 清洗后可能一个字符都不剩（纯 ID/占位符）；这里兜一个省略号，绝不给桌宠发空 text
    const text = fitPetText(speechSlice(raw)) || "……";
    const url = httpImage(message.url);
    const avatar = httpImage(message.avatar);
    // 字数估时与客户端静音兜底同口径（260ms/字）；真实音频时长由客户端的
    // playing/ended 事件接管气泡收起，不再依赖这个估算值。
    const duration = Math.min(60000, Math.max(10000, text.length * 260 + (url ? 4000 : 0)));
    durationMs = duration;
    body = JSON.stringify({ text, ...(url ? { url } : {}), ...(avatar ? { avatar } : {}), duration });
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
  return new Promise((resolve) => {
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
        res.on("end", () => {
          // 桌宠拒绝时仍回 200（如 pet-not-displayed），不能只看状态码，否则日志里的 ok 是假的
          let ok = res.statusCode === 200;
          let reason = "";
          try {
            const parsed = JSON.parse(data);
            if (parsed && parsed.ok === false) {
              ok = false;
              reason = String(parsed.reason || "pet refused");
            }
          } catch (_) {
            /* 非 JSON（dafeiyu3d）按状态码判断 */
          }
          resolve({ ok, status: res.statusCode, body: data, reason, durationMs });
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, error: "pet timeout", durationMs: 0 });
    });
    req.on("error", (err) => resolve({ ok: false, error: err.message, durationMs: 0 }));
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
// 泵互斥：同一时刻只允许一个 pump 循环在跑（保证桌宠一次只收一条）
let pumpRunning = false;

// 「正在说话」布尔锁：推给桌宠成功后按估算时长上锁，锁住期间新消息只覆盖本会话的
// 待播槽，不再往下追加；锁在成功（到时释放）、失败、超时三种情况下都会释放。
const MAX_SPEAK_LOCK_MS = 45 * 1000;
let petSpeaking = false;
let petSpeakingUntil = 0;
let speakTimer = null;

function lockPetSpeaking(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value <= 0) return;
  const hold = Math.max(1000, Math.min(MAX_SPEAK_LOCK_MS, value));
  petSpeaking = true;
  petSpeakingUntil = Date.now() + hold;
  if (speakTimer) clearTimeout(speakTimer);
  speakTimer = setTimeout(() => {
    petSpeaking = false;
    petSpeakingUntil = 0;
    speakTimer = null;
    pumpPetSafely();
  }, hold);
  speakTimer.unref?.();
}

function unlockPetSpeaking() {
  petSpeaking = false;
  petSpeakingUntil = 0;
  if (speakTimer) {
    clearTimeout(speakTimer);
    speakTimer = null;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function dropPending(key) {
  const entry = pendingByChat.get(key);
  if (!entry) return;
  clearTimeout(entry.timer);
  pendingByChat.delete(key);
}

function oldestPendingKey(exceptKey) {
  // 溢出丢弃时优先牺牲「别人的待播」：肥鱼自己的发言字幕最后才动，
  // 否则群里一热闹，回复还没开播就被顶掉——用户看到的就是「发言没内容」。
  let key = null;
  let at = Infinity;
  for (const [k, entry] of pendingByChat) {
    if (k === exceptKey) continue;
    if (entry.payload && entry.payload.is_self) continue;
    if (entry.at < at) {
      at = entry.at;
      key = k;
    }
  }
  if (key === null) {
    at = Infinity;
    for (const [k, entry] of pendingByChat) {
      if (k === exceptKey) continue;
      if (entry.at < at) {
        at = entry.at;
        key = k;
      }
    }
  }
  return key;
}

function enqueuePet(payload) {
  const source = String(payload.source || "local").toLowerCase();
  const key = roomKey(source, payload);
  const now = Date.now();
  const chat = isChatPayload(payload);

  // 同一会话只保留最新一条：新消息只作废本会话排队中的旧消息，
  // 别的会话还没送出的任务不能被一起清掉（否则一提交新任务就打断别人没做完的）。
  // 「在途」期间新消息也只会顶掉本会话的那一条待播槽，不会无限追加。
  dropPending(key);

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
      pumpPetSafely();
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

  pumpPetSafely();
  return { queued: true, key };
}

/** pumpPet 是异步的：任何一次投递异常都不能变成未处理的 Promise rejection 把 relay 进程带走。 */
function pumpPetSafely() {
  pumpPet().catch((err) => {
    console.error("[relay] pump failed:", err && err.message ? err.message : err);
  });
}

async function pumpPet() {
  if (pumpRunning) return;
  pumpRunning = true;
  try {
    while (true) {
      // 布尔锁：桌宠正在说这一条时，先不取新消息；到点自动放行（超时兜底）
      if (petSpeaking) {
        setTimeout(pumpPetSafely, Math.max(50, petSpeakingUntil - Date.now())).unref?.();
        break;
      }
      const now = Date.now();
      for (const [key, entry] of pendingByChat) {
        // 肥鱼自己的发言给 3 倍时限：桌宠忙（语音长句/说话锁）时宁可晚播也别吞掉
        const ttl = entry.payload && entry.payload.is_self ? PET_PENDING_TTL_MS * 3 : PET_PENDING_TTL_MS;
        if (now - entry.at > ttl) {
          skipStats.ttl += 1;
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
      // 已经放旧了的直接丢，不念历史（pendingMaxAgeMs=0 时不按年龄丢，字幕必显）；
      // 自身发言同样放宽 3 倍，晚出现的回复也比没有回复好
      const selfFlag = entry.payload && entry.payload.is_self;
      const ageLimit = PET_PENDING_MAX_AGE_MS > 0 ? PET_PENDING_MAX_AGE_MS * (selfFlag ? 3 : 1) : 0;
      if (ageLimit > 0 && Date.now() - entry.at > ageLimit) {
        skipStats.stale += 1;
        dropPending(key);
        continue;
      }
      dropPending(key);
      lastInjectAt = Date.now();
      lastChatAt.set(key, lastInjectAt);
      let delivered;
      try {
        delivered = await deliverPet(entry.payload, key);
      } finally {
        // 成功按估算时长上锁；失败 / 超时（injectToPet 会 resolve 失败）立即放行
        if (delivered && delivered.ok) lockPetSpeaking(delivered.durationMs);
        else unlockPetSpeaking();
      }
      if (delivered && delivered.ok) {
        skipStats.delivered += 1;
        // 推发后立即闸门关闭，绝不再推第二次
        entry.payload.can_push = false;
        entry.payload.pushed_at = Date.now();
        entry.payload.pushed = true;
        continue;
      }
      // 失败（桌宠没起 / 请求超时 / 401）不吞掉：回队等它恢复，最多重投 REQUEUE_MAX 次
      if (delivered && delivered.skipped) {
        entry.payload.can_push = false;
        continue;
      }
      skipStats.injectFail += 1;
      const retries = Number(entry.payload.__retries || 0);
      if (retries < REQUEUE_MAX) {
        skipStats.requeued += 1;
        entry.payload.__retries = retries + 1;
        entry.payload.can_push = true;
        entry.at = Date.now();
        entry.ready = false;
        pendingByChat.set(key, entry);
        entry.timer = setTimeout(() => {
          entry.ready = true;
          pumpPetSafely();
        }, REQUEUE_DELAY_MS);
        entry.timer.unref?.();
        console.log(
          `[relay] 字幕未送达 ${key} attempts=${retries + 1}/${REQUEUE_MAX}，${REQUEUE_DELAY_MS}ms 后重投`
        );
        continue;
      }
      skipStats.giveUp += 1;
      console.log(`[relay] 字幕重投用尽 ${key}，放弃：${delivered && (delivered.status || delivered.error)}`);
    }
  } finally {
    pumpRunning = false;
    if (petSpeaking) {
      setTimeout(pumpPetSafely, Math.max(50, petSpeakingUntil - Date.now())).unref?.();
    } else if ([...pendingByChat.values()].some((entry) => entry.ready)) {
      setTimeout(pumpPetSafely, 0);
    }
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
  // 聊天消息只念原文：让 9B 改写会额外占一块显存，把 CosyVoice 合成拖到几十秒。
  if (!message && CONFIG.narrate && !isChatPayload(payload)) {
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
  // forceSubtitle 打开时这两条静默丢弃全部失效——字幕必显，宁可重复也不吞。
  // 缓存键用「原始正文」而不是加了「昵称：」前缀的气泡文本，才能和 handleStatus 里的预检命中同一个键。
  const dedupeSource = String(payload.detail || payload.message || payload.pet_text || "").trim();
  const contentKey = contentKeyOf(dedupeSource) || contentKeyOf(message);
  if (!FORCE_SUBTITLE && !images.length && contentKey) {
    if (isTrivialText(dedupeSource)) {
      skipStats.trivial += 1;
      return { ok: true, skipped: "trivial" };
    }
    const cached = contentCache.get(contentKey);
    if (cached && Date.now() - cached.at < CONTENT_CACHE_TTL_MS) {
      skipStats.cached += 1;
      console.log(`[relay] skip cached ${key} content=${contentKey.slice(0, 16)}`);
      return { ok: true, skipped: "cached" };
    }
  }
  // 聊天消息按句切段：每段各配一段声音，长消息不再被 60 字截断掉后半截。
  const segments = isChatPayload(payload) ? splitSpeech(message) : [];
  const parts = segments.length ? segments : [message];
  const baseMessage = {
    state: petState,
    source,
    detail,
    url: images[0],
    avatar: avatarOf(payload, ident.senderId),
    activity: String(payload.event || "").slice(0, 60) || undefined,
    sessionId: `bot-${key}`,
  };
  let result = { ok: true };
  for (let i = 0; i < parts.length; i += 1) {
    if (i > 0) await sleep(600);
    result = await injectToPet({ ...baseMessage, message: parts[i] });
    if (!result.ok) break;
  }

  lastStatus = {
    at: Date.now(),
    source,
    state: petState,
    message,
    segments: parts.length,
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
    skipStats.bootGrace += 1;
    return { ok: true, queued: false, skipped: "boot-grace", state: petState, key };
  }
  // 斜杠指令是命令不是聊天内容，机器人自己的回复会另外播；其余一律进队列（字幕必显）。
  if (FORCE_SUBTITLE && latestText && !imagesOf(payload).length && /^[/／]/.test(latestText.trim())) {
    skipStats.command += 1;
    return { ok: true, queued: false, skipped: "command", state: petState, key };
  }
  // 只播报「有内容」的消息：纯表情、单字、1-9、斜杠指令直接放过，不打扰桌宠。
  // forceSubtitle 打开时取消这些静默丢弃（斜杠指令仍跳过，见上）。
  if (!FORCE_SUBTITLE && latestText && !imagesOf(payload).length && isTrivialText(latestText)) {
    skipStats.trivial += 1;
    return { ok: true, queued: false, skipped: "trivial", state: petState, key };
  }
  // 相同内容在缓存窗口内只播一次，省掉重复的旁白与语音合成。
  if (!FORCE_SUBTITLE && latestText && !imagesOf(payload).length) {
    const cached = contentCache.get(contentKeyOf(latestText));
    if (cached && Date.now() - cached.at < CONTENT_CACHE_TTL_MS) {
      skipStats.cached += 1;
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

    if (req.method === "GET" && url.pathname === "/pet-image-proxy") {
      return proxyImageTo(res, url.searchParams.get("url") || "");
    }

    if (req.method === "GET" && url.pathname.startsWith("/pet-image/")) {
      const name = decodeURIComponent(url.pathname.slice("/pet-image/".length));
      if (!/^[a-zA-Z0-9._-]+$/.test(name)) {
        return sendJson(res, 400, { ok: false, error: "bad image name" });
      }
      return servePetImage(name, res);
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
        speaking: {
          active: petSpeaking,
          leftMs: Math.max(0, petSpeakingUntil - Date.now()),
        },
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
        subtitle: {
          force: FORCE_SUBTITLE,
          injectRetry: INJECT_RETRY,
          requeueMax: REQUEUE_MAX,
          ...skipStats,
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
    `[relay] pending max=${PET_MAX_PENDING} ttl=${PET_PENDING_TTL_MS}ms maxAge=${PET_PENDING_MAX_AGE_MS}ms (1 speaking + latest only)`
  );
  console.log(
    `[relay] cache ttl=${CONTENT_CACHE_TTL_MS}ms max=${CONTENT_CACHE_MAX} rememberStore=${CONFIG.rememberMessages}`
  );
  cleanupPetImages();
  setInterval(cleanupPetImages, 30 * 60 * 1000).unref?.();
});
