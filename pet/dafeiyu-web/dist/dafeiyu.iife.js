"use strict";
var Dafeiyu = (() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, { get: all[name], enumerable: true });
  };
  var __copyProps = (to, from, except, desc) => {
    if (from && typeof from === "object" || typeof from === "function") {
      for (let key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(to, key) && key !== except)
          __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
    }
    return to;
  };
  var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

  // src/index.ts
  var index_exports = {};
  __export(index_exports, {
    default: () => index_default,
    mount: () => mount
  });

  // src/anims.ts
  var ANIMS = [
    "idle-breathing",
    "idle-humming",
    "idle-looking",
    "idle-yawn",
    "sleep-snooze",
    "wake-startled",
    "click-happy",
    "click-wave",
    "click-annoyed",
    "drag-startled",
    "talk-screen",
    "talk-thinking",
    "walk-crab",
    "rare-whale",
    "pet-scratch",
    "eat-rice",
    "eat-caught",
    "eat-icecream",
    "play-gomoku",
    "game-rage"
  ];
  function isAnimName(v) {
    return ANIMS.includes(v);
  }
  var LOOPING = /* @__PURE__ */ new Set([
    "idle-breathing",
    "sleep-snooze",
    "talk-screen",
    "talk-thinking",
    "play-gomoku"
  ]);
  var IDLE_POOL = ["idle-breathing", "idle-humming", "idle-looking", "idle-yawn"];
  var CLICK_POOL = ["click-happy", "click-wave"];
  var FEED_POOL = ["eat-rice", "eat-caught", "eat-icecream"];

  // src/config.ts
  var DEFAULTS = {
    assetBase: "https://cdn.jsdelivr.net/npm/dafeiyu/assets/",
    // 素材是 16:9 画幅、角色只占画面中段，宽度给足角色才有存在感
    size: 240,
    zIndex: 9999,
    whisperIntervalSec: 120,
    enabledOnMobile: false,
    pageContext: true,
    voice: false
  };
  var BUILTIN_LINES = [
    "\u5495\u565C\u5495\u565C\uFF5E",
    "\u8FD9\u7247\u7F51\u9875\u7684\u6C34\u8D28\u4E0D\u9519\u3002",
    "\u4F60\u4ECA\u5929\u559D\u6C34\u4E86\u5417\uFF1F",
    "\u6478\u9C7C\u8981\u9002\u5EA6\u54E6\u3002",
    "\u55EF\uFF1F\u5728\u770B\u6211\uFF1F",
    "\u542C\u8BF4\u65E9\u7761\u7684\u4EBA\u8FD0\u6C14\u4E0D\u4F1A\u592A\u5DEE\u3002",
    "\u4EE3\u7801\u5199\u5B8C\u4E86\u5417\uFF1F\u6CA1\u5199\u5B8C\u4E5F\u6CA1\u4E8B\u3002",
    "\u98CE\u5E73\u6D6A\u9759\uFF0C\u9002\u5408\u53D1\u5446\u3002",
    "\u8981\u4E0D\u8981\u4F11\u606F\u4E00\u4E0B\u773C\u775B\uFF1F",
    "\u6211\u662F\u4E00\u6761\u6709\u68A6\u60F3\u7684\u9C7C\u3002",
    "\u8FD9\u4E2A bug \u80AF\u5B9A\u4E0D\u602A\u4F60\u3002",
    "\u6084\u6084\u5192\u4E2A\u6CE1\u3002",
    "\u6DF1\u6C34\u533A\u7981\u6B62\u55A7\u54D7\u3002",
    "\u4ECA\u5929\u4E5F\u662F\u5143\u6C14\u6EE1\u6EE1\u7684\u4E00\u5929\u3002",
    "\u522B\u5237\u4E86\uFF0C\u53BB\u770B\u70B9\u957F\u6587\u7AE0\u5427\u3002",
    "\u6709\u70B9\u56F0\u2026\u2026\u5148\u772F\u4E00\u4F1A\u513F\u3002",
    "\u9CB8\u9C7C\u662F\u6211\u8FDC\u623F\u4EB2\u621A\uFF0C\u522B\u63D0\u4E86\u3002",
    "\u4F60\u56DE\u6765\u5566\u3002",
    "\u6162\u6162\u6765\uFF0C\u6BD4\u8F83\u5FEB\u3002",
    "\u9875\u9762\u52A0\u8F7D\u5F97\u8FD8\u987A\u6ED1\u5417\uFF1F",
    "\u5468\u4E94\u7684\u4E0B\u5348\u6700\u6F2B\u957F\u3002",
    "\u6DF1\u6D77\u4FE1\u53F7\u4E0D\u597D\uFF0C\u518D\u8BF4\u4E00\u904D\uFF1F",
    "\uFF08\u5410\u4E86\u4E00\u4E32\u6CE1\u6CE1\uFF09",
    "\u503C\u73ED\u4E2D\uFF0C\u6709\u4E8B\u6233\u6211\u3002"
  ];

  // src/affinity.ts
  var KEY = "dafeiyu:affinity";
  var LEVELS = [
    { at: 0, name: "\u964C\u751F" },
    { at: 30, name: "\u719F\u6089" },
    { at: 100, name: "\u8001\u670B\u53CB" },
    { at: 300, name: "\u631A\u53CB" }
  ];
  var FRIEND_LINES = [
    "\u90FD\u8BA4\u8BC6\u8FD9\u4E48\u4E45\u4E86\uFF0C\u8FD8\u662F\u89C9\u5F97\u4F60\u6700\u597D\u3002",
    "\u8001\u670B\u53CB\u6765\u5566\uFF0C\u4ECA\u5929\u4F4D\u7F6E\u7ED9\u4F60\u7559\u7740\u5462\u3002",
    "\u966A\u6211\u8FD9\u4E48\u4E45\uFF0C\u6DF1\u6D77\u91CC\u4E5F\u6570\u4F60\u8138\u719F\u3002"
  ];
  function loadAffinity() {
    try {
      const n = Number(JSON.parse(localStorage.getItem(KEY) || "0"));
      return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
    } catch {
      return 0;
    }
  }
  function saveAffinity(n) {
    try {
      localStorage.setItem(KEY, String(Math.floor(n)));
    } catch {
    }
  }
  function addAffinity(delta) {
    const n = loadAffinity() + delta;
    saveAffinity(n);
    return n;
  }
  function levelOf(n) {
    let cur = LEVELS[0];
    for (const lv of LEVELS) if (n >= lv.at) cur = lv;
    return cur;
  }

  // src/bubble.ts
  var Bubble = class {
    constructor(shadow) {
      this.hideTimer = 0;
      this.visible = false;
      this.el = document.createElement("div");
      const s = this.el.style;
      s.position = "absolute";
      s.left = "50%";
      s.bottom = "calc(100% + 12px)";
      s.transform = "translateX(-50%) translateY(4px)";
      s.maxWidth = "230px";
      s.padding = "8px 12px";
      s.borderRadius = "12px";
      s.background = "rgba(32, 34, 40, 0.92)";
      s.border = "1px solid rgba(255, 255, 255, 0.14)";
      s.color = "#f2f3f5";
      s.font = '13px/1.55 system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
      s.textAlign = "center";
      s.wordBreak = "break-word";
      s.boxShadow = "0 4px 16px rgba(0, 0, 0, 0.18)";
      s.opacity = "0";
      s.transition = "opacity 0.25s, transform 0.25s";
      s.pointerEvents = "none";
      s.whiteSpace = "normal";
      const tail = document.createElement("style");
      tail.textContent = `
      .dfy-bubble::after {
        content: '';
        position: absolute;
        left: 50%;
        top: 100%;
        transform: translateX(-50%);
        border: 6px solid transparent;
        border-top-color: rgba(32, 34, 40, 0.92);
      }
    `;
      shadow.appendChild(tail);
      this.el.className = "dfy-bubble";
      shadow.appendChild(this.el);
    }
    /**
     * 显示一句话；durationMs 后自动隐藏。宠物贴近视口顶部时翻到下方。
     * imageUrl：可选表情包（显示在文字下方，加载失败自动隐藏——素材缺失不破坏体验）
     */
    say(text, durationMs = 1e4, flipBelow = false, imageUrl) {
      var _a;
      this.el.textContent = text;
      (_a = this.el.querySelector("img")) == null ? void 0 : _a.remove();
      if (imageUrl) {
        const img = document.createElement("img");
        img.src = imageUrl;
        img.alt = "";
        img.style.cssText = "display:block;margin:6px auto 0;max-height:110px;max-width:100%;border-radius:8px";
        img.onerror = () => img.remove();
        this.el.appendChild(img);
      }
      this.el.style.bottom = flipBelow ? "auto" : "calc(100% + 12px)";
      this.el.style.top = flipBelow ? "calc(100% + 12px)" : "auto";
      this.el.style.opacity = "1";
      this.el.style.transform = "translateX(-50%) translateY(0)";
      this.visible = true;
      window.clearTimeout(this.hideTimer);
      this.hideTimer = window.setTimeout(() => this.hide(), durationMs);
    }
    hide() {
      if (!this.visible) return;
      this.visible = false;
      window.clearTimeout(this.hideTimer);
      this.el.style.opacity = "0";
      this.el.style.transform = "translateX(-50%) translateY(4px)";
    }
    destroy() {
      window.clearTimeout(this.hideTimer);
      this.el.remove();
    }
  };

  // src/chat.ts
  var PANEL_STYLE = `
  .dfy-chat {
    position: absolute;
    bottom: calc(100% + 12px);
    left: 0;
    width: 288px;
    max-width: calc(100vw - 24px);
    display: flex;
    flex-direction: column;
    border-radius: 14px;
    background: rgba(32, 34, 40, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.14);
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.22);
    color: #f2f3f5;
    font: 13px/1.6 system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    overflow: hidden;
    opacity: 0;
    transform: translateY(4px);
    transition: opacity .2s, transform .2s;
    pointer-events: auto;
  }
  .dfy-chat.dfy-open { opacity: 1; transform: translateY(0); }
  .dfy-chat-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 12px; font-weight: 600; border-bottom: 1px solid rgba(255,255,255,.1);
  }
  .dfy-chat-close {
    border: 0; background: none; color: inherit; font-size: 15px;
    cursor: pointer; padding: 0 4px; line-height: 1; opacity: .7;
  }
  .dfy-chat-close:hover { opacity: 1; }
  .dfy-chat-list {
    max-height: 240px; min-height: 60px; overflow-y: auto;
    padding: 10px 12px; display: flex; flex-direction: column; gap: 8px;
  }
  .dfy-msg { padding: 6px 10px; border-radius: 10px; max-width: 86%; word-break: break-word; white-space: pre-wrap; }
  .dfy-msg-user { align-self: flex-end; background: #3b6ea5; color: #fff; }
  .dfy-msg-bot { align-self: flex-start; background: rgba(255,255,255,.1); }
  .dfy-msg-err { align-self: center; color: #b9bcc4; font-size: 12px; background: none; padding: 2px; }
  .dfy-chat-input { display: flex; gap: 8px; padding: 8px 10px 10px; border-top: 1px solid rgba(255,255,255,.1); }
  .dfy-chat-input input {
    flex: 1; min-width: 0; border-radius: 8px; border: 1px solid rgba(255,255,255,.18);
    background: rgba(255,255,255,.08); color: inherit; font: inherit; padding: 5px 10px; outline: none;
  }
  .dfy-chat-input input:focus { border-color: rgba(255,255,255,.4); }
  .dfy-chat-input button {
    border: 0; border-radius: 8px; background: #3b6ea5; color: #fff;
    font: inherit; padding: 5px 12px; cursor: pointer;
  }
  .dfy-chat-input button:disabled { opacity: .5; cursor: default; }
`;
  var ChatPanel = class {
    constructor(shadow, handlers) {
      this.sending = false;
      this.openState = false;
      this.onSend = handlers.onSend;
      this.onClose = handlers.onClose;
      const style = document.createElement("style");
      style.textContent = PANEL_STYLE;
      shadow.appendChild(style);
      this.root = document.createElement("div");
      this.root.className = "dfy-chat";
      this.root.style.display = "none";
      const head = document.createElement("div");
      head.className = "dfy-chat-head";
      head.textContent = handlers.title ? `\u548C${handlers.title}\u804A\u804A` : "\u548C\u5927\u80A5\u9C7C\u804A\u804A";
      const close = document.createElement("button");
      close.className = "dfy-chat-close";
      close.textContent = "\xD7";
      close.setAttribute("aria-label", "\u5173\u95ED\u804A\u5929");
      close.addEventListener("click", () => this.close());
      head.appendChild(close);
      this.list = document.createElement("div");
      this.list.className = "dfy-chat-list";
      const tip = document.createElement("div");
      tip.className = "dfy-msg dfy-msg-err";
      tip.textContent = "\u53CC\u51FB\u9C7C\u4E5F\u80FD\u6253\u5F00/\u5173\u95ED";
      this.list.appendChild(tip);
      this.input = document.createElement("input");
      this.input.type = "text";
      this.input.placeholder = "\u8BF4\u70B9\u4EC0\u4E48\u2026\uFF08Enter \u53D1\u9001\uFF09";
      this.input.maxLength = 500;
      this.input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") this.submit();
        if (e.key === "Escape") this.close();
        e.stopPropagation();
      });
      this.sendBtn = document.createElement("button");
      this.sendBtn.textContent = "\u53D1\u9001";
      this.sendBtn.addEventListener("click", () => this.submit());
      const row = document.createElement("div");
      row.className = "dfy-chat-input";
      row.append(this.input, this.sendBtn);
      this.root.append(head, this.list, row);
      shadow.appendChild(this.root);
    }
    get isOpen() {
      return this.openState;
    }
    open() {
      this.openState = true;
      this.root.style.display = "flex";
      requestAnimationFrame(() => this.root.classList.add("dfy-open"));
      this.clampIntoViewport();
      this.input.focus();
    }
    close() {
      if (!this.openState) return;
      this.openState = false;
      this.root.classList.remove("dfy-open");
      window.setTimeout(() => {
        if (!this.openState) this.root.style.display = "none";
      }, 220);
      this.onClose();
    }
    /** 面板贴着宠物，宠物贴着视口左/右缘时把面板水平拉回视口内 */
    clampIntoViewport() {
      this.root.style.marginLeft = "0";
      const r = this.root.getBoundingClientRect();
      const vw = window.innerWidth;
      let shift = 0;
      if (r.left < 8) shift = 8 - r.left;
      else if (r.right > vw - 8) shift = vw - 8 - r.right;
      if (shift !== 0) this.root.style.marginLeft = `${shift}px`;
    }
    submit() {
      const text = this.input.value.trim();
      if (!text || this.sending) return;
      this.input.value = "";
      this.onSend(text);
    }
    setSending(v) {
      this.sending = v;
      this.sendBtn.disabled = v;
      this.input.disabled = v;
      if (!v) this.input.focus();
    }
    addUser(text) {
      this.appendMsg("dfy-msg-user", text);
    }
    /** 一次性提示（错误/降级说明），不进对话流样式 */
    addNote(text) {
      this.appendMsg("dfy-msg-err", text);
    }
    startAssistant() {
      const el = this.appendMsg("dfy-msg-bot", "");
      const textSpan = document.createElement("span");
      el.appendChild(textSpan);
      const scroll = () => this.list.scrollTop = this.list.scrollHeight;
      return {
        push: (delta) => {
          textSpan.textContent += delta;
          scroll();
        },
        setImage: (url) => {
          var _a;
          (_a = el.querySelector("img")) == null ? void 0 : _a.remove();
          const img = document.createElement("img");
          img.src = url;
          img.alt = "";
          img.style.cssText = "display:block;margin-bottom:4px;max-height:130px;max-width:100%;border-radius:8px";
          img.onerror = () => img.remove();
          el.insertBefore(img, textSpan);
          scroll();
        },
        done: () => {
          if (!textSpan.textContent) textSpan.textContent = "\u2026";
          scroll();
        },
        error: (msg) => {
          el.className = "dfy-msg dfy-msg-err";
          el.textContent = msg;
          scroll();
        }
      };
    }
    appendMsg(cls, text) {
      const el = document.createElement("div");
      el.className = `dfy-msg ${cls}`;
      el.textContent = text;
      this.list.appendChild(el);
      this.list.scrollTop = this.list.scrollHeight;
      return el;
    }
  };
  function streamChat(url, messages, handlers, timeoutMs = 6e4) {
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
    const fail = (msg) => {
      window.clearTimeout(timer);
      handlers.onError(msg);
    };
    fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ messages }),
      signal: ctrl.signal
    }).then(async (res) => {
      var _a;
      if (!res.ok || !res.body) {
        let msg = `\u8FDE\u63A5\u4E0D\u4E0A\u5927\u80A5\u9C7C\uFF08${res.status}\uFF09`;
        try {
          const data = await res.json();
          if (typeof data.message === "string") msg = data.message;
        } catch {
        }
        fail(msg);
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (; ; ) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of block.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (payload === "[DONE]") {
              window.clearTimeout(timer);
              handlers.onDone();
              return;
            }
            try {
              const evt = JSON.parse(payload);
              if (typeof evt.e === "string") {
                fail(evt.e);
                return;
              }
              if (typeof evt.d === "string") handlers.onDelta(evt.d);
              if (typeof evt.i === "string") (_a = handlers.onImage) == null ? void 0 : _a.call(handlers, evt.i);
            } catch {
            }
          }
        }
      }
      window.clearTimeout(timer);
      handlers.onDone();
    }).catch(() => fail("\u5927\u80A5\u9C7C\u8D70\u795E\u4E86\uFF0C\u7A0D\u540E\u518D\u8BD5"));
    return ctrl;
  }

  // src/game.ts
  var SIZE = 15;
  var CELL = 22;
  var PAD = CELL / 2 + 4;
  var PANEL_STYLE2 = `
  .dfy-game {
    position: absolute;
    bottom: calc(100% + 12px);
    left: 0;
    width: 348px;
    max-width: calc(100vw - 24px);
    border-radius: 14px;
    background: rgba(32, 34, 40, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.14);
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.22);
    color: #f2f3f5;
    font: 13px/1.6 system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    overflow: hidden;
    display: none;
    pointer-events: auto;
  }
  .dfy-game.dfy-open { display: block; }
  .dfy-game-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 12px; font-weight: 600; border-bottom: 1px solid rgba(255,255,255,.1);
  }
  .dfy-game-head button {
    border: 0; background: none; color: inherit; font: inherit;
    cursor: pointer; padding: 0 4px; opacity: .75;
  }
  .dfy-game-head button:hover { opacity: 1; }
  .dfy-game-status {
    padding: 6px 12px 0; color: #b9bcc4; font-size: 12px;
  }
  .dfy-game canvas {
    display: block; margin: 8px auto 12px; border-radius: 8px;
    cursor: pointer; max-width: 100%; height: auto;
  }
`;
  var GomokuPanel = class {
    constructor(shadow, handlers) {
      this.board = new Int8Array(SIZE * SIZE);
      // 0 空 / 1 访客(黑) / 2 鱼(白)
      this.turn = 1;
      this.over = false;
      this.openState = false;
      this.timer = 0;
      this.handlers = handlers;
      const style = document.createElement("style");
      style.textContent = PANEL_STYLE2;
      shadow.appendChild(style);
      this.root = document.createElement("div");
      this.root.className = "dfy-game";
      this.root.style.display = "none";
      const head = document.createElement("div");
      head.className = "dfy-game-head";
      const title = document.createElement("span");
      title.textContent = "\u548C\u5927\u80A5\u9C7C\u4E0B\u4E94\u5B50\u68CB";
      const btns = document.createElement("span");
      const reset = document.createElement("button");
      reset.textContent = "\u91CD\u5F00";
      reset.addEventListener("click", () => this.reset());
      const close = document.createElement("button");
      close.textContent = "\xD7";
      close.setAttribute("aria-label", "\u5173\u95ED\u6E38\u620F");
      close.addEventListener("click", () => this.close());
      btns.append(reset, close);
      head.append(title, btns);
      this.statusEl = document.createElement("div");
      this.statusEl.className = "dfy-game-status";
      this.canvas = document.createElement("canvas");
      this.canvas.width = SIZE * CELL + PAD * 2;
      this.canvas.height = SIZE * CELL + PAD * 2;
      this.canvas.addEventListener("click", (e) => this.onCanvasClick(e));
      this.ctx = this.canvas.getContext("2d");
      this.root.append(head, this.statusEl, this.canvas);
      shadow.appendChild(this.root);
      this.draw();
    }
    get isOpen() {
      return this.openState;
    }
    open() {
      this.openState = true;
      this.root.style.display = "block";
      requestAnimationFrame(() => this.root.classList.add("dfy-open"));
      this.clampIntoViewport();
      this.setStatus();
    }
    close() {
      if (!this.openState) return;
      this.openState = false;
      this.root.classList.remove("dfy-open");
      window.clearTimeout(this.timer);
      window.setTimeout(() => {
        if (!this.openState) this.root.style.display = "none";
      }, 220);
      this.handlers.onClose();
    }
    destroy() {
      window.clearTimeout(this.timer);
      this.root.remove();
    }
    clampIntoViewport() {
      this.root.style.marginLeft = "0";
      const r = this.root.getBoundingClientRect();
      const vw = window.innerWidth;
      let shift = 0;
      if (r.left < 8) shift = 8 - r.left;
      else if (r.right > vw - 8) shift = vw - 8 - r.right;
      if (shift !== 0) this.root.style.marginLeft = `${shift}px`;
    }
    reset() {
      this.board.fill(0);
      this.turn = 1;
      this.over = false;
      window.clearTimeout(this.timer);
      this.root.dataset.moves = "0";
      this.draw();
      this.setStatus();
    }
    moves() {
      let n = 0;
      for (const v of this.board) if (v) n++;
      return n;
    }
    setStatus() {
      if (this.over) return;
      this.statusEl.textContent = this.turn === 1 ? "\u8F6E\u5230\u4F60\u4E86\uFF08\u9ED1\u68CB\uFF09" : "\u5927\u80A5\u9C7C\u601D\u8003\u4E2D\u2026";
      this.root.dataset.moves = String(this.moves());
    }
    onCanvasClick(e) {
      if (this.over || this.turn !== 1) return;
      const rect = this.canvas.getBoundingClientRect();
      const scale = this.canvas.width / rect.width;
      const x = Math.round(((e.clientX - rect.left) * scale - PAD) / CELL);
      const y = Math.round(((e.clientY - rect.top) * scale - PAD) / CELL);
      if (x < 0 || y < 0 || x >= SIZE || y >= SIZE) return;
      if (this.board[y * SIZE + x]) return;
      this.place(x, y, 1);
      if (this.over) return;
      this.turn = 2;
      this.setStatus();
      this.timer = window.setTimeout(() => this.petMove(), 600 + Math.random() * 500);
    }
    petMove() {
      if (this.over || !this.openState) return;
      const idx = this.bestCell();
      if (idx < 0) {
        this.end("draw");
        return;
      }
      this.place(idx % SIZE, Math.floor(idx / SIZE), 2);
      if (!this.over) {
        this.turn = 1;
        this.setStatus();
      }
    }
    place(x, y, who) {
      this.board[y * SIZE + x] = who;
      this.draw();
      if (this.wins(x, y, who)) {
        this.end(who === 1 ? "player" : "pet");
      } else if (this.moves() >= SIZE * SIZE) {
        this.end("draw");
      }
    }
    /** 以 (x,y) 为中心四个方向数连子 */
    wins(x, y, who) {
      for (const [dx, dy] of [[1, 0], [0, 1], [1, 1], [1, -1]]) {
        let n = 1;
        for (const s of [1, -1]) {
          let i = x + dx * s;
          let j = y + dy * s;
          while (i >= 0 && j >= 0 && i < SIZE && j < SIZE && this.board[j * SIZE + i] === who) {
            n++;
            i += dx * s;
            j += dy * s;
          }
        }
        if (n >= 5) return true;
      }
      return false;
    }
    // ── 启发式 AI ──────────────────────────────────────────────
    /** 该点对 who 的方向价值：连子数 × 开放端数 */
    lineScore(x, y, who) {
      let total = 0;
      for (const [dx, dy] of [[1, 0], [0, 1], [1, 1], [1, -1]]) {
        let run = 1;
        let open = 0;
        for (const s of [1, -1]) {
          let i = x + dx * s;
          let j = y + dy * s;
          while (i >= 0 && j >= 0 && i < SIZE && j < SIZE && this.board[j * SIZE + i] === who) {
            run++;
            i += dx * s;
            j += dy * s;
          }
          if (i >= 0 && j >= 0 && i < SIZE && j < SIZE && this.board[j * SIZE + i] === 0) open++;
        }
        if (run >= 5) total += 1e4;
        else if (open === 0) total += 0;
        else total += run * run * open;
      }
      return total;
    }
    bestCell() {
      let best = -1;
      let bestScore = -1;
      const center = (SIZE - 1) / 2;
      for (let i = 0; i < SIZE * SIZE; i++) {
        if (this.board[i]) continue;
        const x = i % SIZE;
        const y = Math.floor(i / SIZE);
        const myWin = this.wins(x, y, 2);
        const yourWin = this.wins(x, y, 1);
        let score;
        if (myWin) score = 1e9;
        else if (yourWin) score = 1e8;
        else {
          score = this.lineScore(x, y, 2) * 1.1 + this.lineScore(x, y, 1);
          score -= (Math.abs(x - center) + Math.abs(y - center)) * 2;
        }
        if (score > bestScore) {
          bestScore = score;
          best = i;
        }
      }
      return best;
    }
    end(result) {
      this.over = true;
      this.statusEl.textContent = result === "player" ? "\u4F60\u8D62\u4E86\uFF01\u70B9\u300C\u91CD\u5F00\u300D\u518D\u6765\u4E00\u5C40" : result === "pet" ? "\u5927\u80A5\u9C7C\u8D62\u4E86\uFF5E\u4E0D\u670D\u5C31\u91CD\u5F00" : "\u5E73\u5C40\uFF0C\u68CB\u9022\u5BF9\u624B";
      this.root.dataset.moves = String(this.moves());
      this.handlers.onEnd(result);
    }
    // ── 绘制 ────────────────────────────────────────────────────
    draw() {
      const c = this.ctx;
      const w = this.canvas.width;
      c.fillStyle = "#e2c08d";
      c.fillRect(0, 0, w, w);
      c.strokeStyle = "#8a6a3a";
      c.lineWidth = 1;
      for (let i = 0; i < SIZE; i++) {
        c.beginPath();
        c.moveTo(PAD + i * CELL, PAD);
        c.lineTo(PAD + i * CELL, w - PAD);
        c.stroke();
        c.beginPath();
        c.moveTo(PAD, PAD + i * CELL);
        c.lineTo(w - PAD, PAD + i * CELL);
        c.stroke();
      }
      c.fillStyle = "#8a6a3a";
      for (const [sx, sy] of [[3, 3], [11, 3], [7, 7], [3, 11], [11, 11]]) {
        c.beginPath();
        c.arc(PAD + sx * CELL, PAD + sy * CELL, 2.5, 0, Math.PI * 2);
        c.fill();
      }
      for (let y = 0; y < SIZE; y++) {
        for (let x = 0; x < SIZE; x++) {
          const v = this.board[y * SIZE + x];
          if (!v) continue;
          c.beginPath();
          c.arc(PAD + x * CELL, PAD + y * CELL, CELL * 0.42, 0, Math.PI * 2);
          if (v === 1) {
            c.fillStyle = "#1c1e22";
            c.fill();
            c.strokeStyle = "#000";
            c.stroke();
          } else {
            c.fillStyle = "#f7f7f5";
            c.fill();
            c.strokeStyle = "#b9b5ad";
            c.stroke();
          }
        }
      }
    }
  };

  // src/menu.ts
  var MENU_STYLE = `
  .dfy-menu {
    position: absolute;
    bottom: calc(100% + 12px);
    left: 0;
    min-width: 148px;
    border-radius: 12px;
    background: rgba(32, 34, 40, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.14);
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.22);
    color: #f2f3f5;
    font: 13px/1.6 system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    overflow: hidden;
    display: none;
    pointer-events: auto;
  }
  .dfy-menu.dfy-open { display: block; }
  .dfy-menu-head {
    padding: 7px 12px 5px;
    font-size: 12px;
    color: #b9bcc4;
    border-bottom: 1px solid rgba(255,255,255,.1);
  }
  .dfy-menu button {
    display: block; width: 100%; text-align: left;
    padding: 7px 14px; border: 0; background: none;
    color: inherit; font: inherit; cursor: pointer;
  }
  .dfy-menu button:hover { background: rgba(255,255,255,.09); }
`;
  var PetMenu = class {
    constructor(shadow, host, handlers) {
      this.openState = false;
      this.handlers = handlers;
      this.host = host;
      const style = document.createElement("style");
      style.textContent = MENU_STYLE;
      shadow.appendChild(style);
      this.root = document.createElement("div");
      this.root.className = "dfy-menu";
      this.head = document.createElement("div");
      this.head.className = "dfy-menu-head";
      this.root.appendChild(this.head);
      const item = (label, fn) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.addEventListener("click", () => {
          this.close();
          fn();
        });
        this.root.appendChild(b);
        return b;
      };
      item("\u{1F4AC} \u8BF4\u53E5\u8BDD", handlers.onSay);
      item("\u{1F35A} \u5582\u98DF", handlers.onFeed);
      this.sleepBtn = item("\u{1F634} \u7761\u89C9", handlers.onToggleSleep);
      this.chatBtn = item("\u{1F4AC} \u804A\u5929", handlers.onChat);
      item("\u265F \u4E94\u5B50\u68CB", handlers.onGame);
      this.voiceBtn = item("\u{1F50A} \u8BED\u97F3\uFF1A\u5F00", handlers.onToggleVoice);
      this.voiceCycleBtn = item("\u{1F3A4} \u97F3\u8272", handlers.onCycleVoice);
      item("\u{1F3AF} \u56DE\u539F\u4F4D", handlers.onResetPos);
      shadow.appendChild(this.root);
      this.onDocDown = (e) => {
        if (!this.openState) return;
        if (!e.composedPath().includes(this.host)) this.close();
      };
      this.onKey = (e) => {
        if (e.key === "Escape" && this.openState) this.close();
      };
      this.onScroll = () => this.close();
      document.addEventListener("pointerdown", this.onDocDown, true);
      document.addEventListener("keydown", this.onKey);
      window.addEventListener("scroll", this.onScroll, { passive: true });
    }
    get isOpen() {
      return this.openState;
    }
    /**
     * @param name        宠物名（头部展示）
     * @param affinity    好感数值
     * @param levelName   档位名
     * @param sleeping    当前是否睡着（决定「睡觉/叫醒」文案）
     * @param canChat     是否有聊天能力（无则隐藏聊天项）
     * @param voiceOn     语音朗读开关状态（决定「语音」项文案）
     * @param voiceLabel  当前音色名（「音色」项展示；空串隐藏该项）
     */
    open(name, affinity, levelName, sleeping, canChat, voiceOn, voiceLabel) {
      this.head.textContent = `${name} \xB7 \u597D\u611F ${affinity}\uFF08${levelName}\uFF09`;
      this.sleepBtn.textContent = sleeping ? "\u23F0 \u53EB\u9192" : "\u{1F634} \u7761\u89C9";
      this.chatBtn.style.display = canChat ? "" : "none";
      this.voiceBtn.textContent = voiceOn ? "\u{1F50A} \u8BED\u97F3\uFF1A\u5F00" : "\u{1F507} \u8BED\u97F3\uFF1A\u5173";
      this.voiceCycleBtn.textContent = `\u{1F3A4} \u97F3\u8272\uFF1A${voiceLabel || "\u65E0"}`;
      this.openState = true;
      this.root.classList.add("dfy-open");
      this.root.style.marginLeft = "0";
      const r = this.root.getBoundingClientRect();
      const vw = window.innerWidth;
      let shift = 0;
      if (r.left < 8) shift = 8 - r.left;
      else if (r.right > vw - 8) shift = vw - 8 - r.right;
      if (shift !== 0) this.root.style.marginLeft = `${shift}px`;
    }
    close() {
      this.openState = false;
      this.root.classList.remove("dfy-open");
    }
    destroy() {
      document.removeEventListener("pointerdown", this.onDocDown, true);
      document.removeEventListener("keydown", this.onKey);
      window.removeEventListener("scroll", this.onScroll);
      this.root.remove();
    }
  };

  // src/physics.ts
  var DRAG_THRESHOLD_PX = 6;
  var SAMPLE_WINDOW_MS = 100;
  var GRAVITY = 22e-4;
  var AIR_DRAG = 0.9995;
  var WALL_BOUNCE = 0.6;
  var FLOOR_BOUNCE = 0.45;
  var FLOOR_FRICTION = 0.85;
  var REST_SPEED = 0.03;
  function attachDrag(el, h) {
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let lastX = 0;
    let lastY = 0;
    let samples = [];
    let pid = -1;
    const onDown = (e) => {
      if (e.button !== void 0 && e.button !== 0) return;
      pid = e.pointerId;
      startX = lastX = e.clientX;
      startY = lastY = e.clientY;
      samples = [{ t: performance.now(), x: startX, y: startY }];
      dragging = false;
      el.setPointerCapture(pid);
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
      el.addEventListener("pointercancel", onUp);
      h.onGrab(startX, startY);
    };
    const onMove = (e) => {
      if (e.pointerId !== pid) return;
      const x = e.clientX;
      const y = e.clientY;
      if (!dragging) {
        if (Math.hypot(x - startX, y - startY) < DRAG_THRESHOLD_PX) return;
        dragging = true;
      }
      lastX = x;
      lastY = y;
      const now = performance.now();
      samples.push({ t: now, x, y });
      while (samples.length > 2 && now - samples[0].t > SAMPLE_WINDOW_MS) samples.shift();
      h.onDragMove(x, y);
    };
    const onUp = (e) => {
      if (e.pointerId !== pid) return;
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onUp);
      el.removeEventListener("pointercancel", onUp);
      try {
        el.releasePointerCapture(pid);
      } catch {
      }
      pid = -1;
      if (!dragging) {
        h.onClick();
        return;
      }
      const now = performance.now();
      samples.push({ t: now, x: lastX, y: lastY });
      const first = samples[0];
      const dt = Math.max(now - first.t, 1);
      h.onRelease((lastX - first.x) / dt, (lastY - first.y) / dt);
    };
    el.addEventListener("pointerdown", onDown);
    return () => {
      el.removeEventListener("pointerdown", onDown);
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onUp);
      el.removeEventListener("pointercancel", onUp);
    };
  }
  function startFlight(o) {
    let x = o.x;
    let y = o.y;
    let vx = o.vx;
    let vy = o.vy;
    let raf = 0;
    let last = performance.now();
    let stillFrames = 0;
    const step = (now) => {
      const dt = Math.min(now - last, 32);
      last = now;
      const { vw, vh, w, h } = o.bounds();
      vy += GRAVITY * dt;
      vx *= Math.pow(AIR_DRAG, dt);
      x += vx * dt;
      y += vy * dt;
      if (x < 0) {
        x = 0;
        vx = -vx * WALL_BOUNCE;
      } else if (x > vw - w) {
        x = vw - w;
        vx = -vx * WALL_BOUNCE;
      }
      if (y < 0) {
        y = 0;
        vy = -vy * WALL_BOUNCE;
      } else if (y > vh - h) {
        y = vh - h;
        vy = -vy * FLOOR_BOUNCE;
        vx *= FLOOR_FRICTION;
        if (Math.abs(vy) < REST_SPEED) vy = 0;
      }
      o.onStep(x, y);
      const speed = Math.hypot(vx, vy);
      if (speed < REST_SPEED && y >= vh - h - 1) {
        if (++stillFrames >= 3) {
          o.onRest(x, y);
          return;
        }
      } else {
        stillFrames = 0;
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }

  // src/player.ts
  function prefersMov() {
    const ua = navigator.userAgent;
    if (/Chrome|Chromium|Edg|OPR|Firefox|FxiOS/i.test(ua)) return false;
    return /Mac/i.test(navigator.platform || ua);
  }
  var LOAD_TIMEOUT_MS = 1e4;
  var Player = class {
    constructor(video, assetBase) {
      /** 加载失败（404 / 解码错误）的动画，调度器应将其从池中剔除 */
      this.unavailable = /* @__PURE__ */ new Set();
      this.token = 0;
      this.currentName = null;
      this.video = video;
      this.assetBase = assetBase.endsWith("/") ? assetBase : assetBase + "/";
      this.ext = prefersMov() ? ".mov" : ".webm";
      video.muted = true;
      video.playsInline = true;
      video.preload = "none";
      video.setAttribute("muted", "");
      video.setAttribute("aria-hidden", "true");
      video.addEventListener("error", () => {
        if (this.currentName) this.unavailable.add(this.currentName);
      });
    }
    url(name) {
      return `${this.assetBase}${name}${this.ext}`;
    }
    get playing() {
      return this.currentName;
    }
    /** 换源播放。resolve false 表示加载失败（已记入黑名单），调用方应换动画 */
    async play(name, loop) {
      const token = ++this.token;
      const url = this.url(name);
      const abs = new URL(url, location.href).href;
      if (this.currentName === name && loop && this.video.loop && !this.video.ended) {
        void this.video.play().catch(() => {
        });
        return true;
      }
      this.currentName = name;
      this.video.loop = loop;
      this.video.src = url;
      this.video.load();
      const ok = await new Promise((resolve) => {
        const timer = window.setTimeout(() => resolve(false), LOAD_TIMEOUT_MS);
        const done = (v) => {
          window.clearTimeout(timer);
          this.video.removeEventListener("loadeddata", onData);
          this.video.removeEventListener("error", onError);
          resolve(v);
        };
        const onData = () => done(true);
        const onError = () => done(false);
        this.video.addEventListener("loadeddata", onData, { once: true });
        this.video.addEventListener("error", onError, { once: true });
      });
      if (token !== this.token) return false;
      if (!ok) {
        this.unavailable.add(name);
        return false;
      }
      try {
        await this.video.play();
      } catch {
      }
      return token === this.token;
    }
    /**
     * 播一遍并在 ended 时 resolve；加载失败立即 resolve false。
     * 不做 token 检查：被更新的播放取代时，这个 promise 可能晚 Resolve 或
     * 被后来者的 ended 提前 Resolve——调用方（pet.ts）有自己的状态代次守卫。
     */
    async playOnce(name) {
      const started = await this.play(name, false);
      if (!started) return false;
      return await new Promise((resolve) => {
        const duration = (this.video.duration || 12) * 1e3 + 2e3;
        const timer = window.setTimeout(() => finish(true), duration);
        const finish = (v) => {
          window.clearTimeout(timer);
          this.video.removeEventListener("ended", onEnd);
          this.video.removeEventListener("error", onErr);
          resolve(v);
        };
        const onEnd = () => finish(true);
        const onErr = () => finish(false);
        this.video.addEventListener("ended", onEnd, { once: true });
        this.video.addEventListener("error", onErr, { once: true });
      });
    }
    pause() {
      this.video.pause();
    }
    /** 朝向翻转（水平镜像），漫游向左走时用 */
    setMirror(faceLeft) {
      this.video.style.transform = faceLeft ? "scaleX(-1)" : "";
    }
    get el() {
      return this.video;
    }
  };

  // src/speech.ts
  var VOICE_KEY = "dafeiyu:voice";
  var VOICE_NAME_KEY = "dafeiyu:voice-name";
  var Speech = class {
    constructor(enabled) {
      var _a, _b;
      this.synth = typeof speechSynthesis !== "undefined" ? speechSynthesis : null;
      let saved = null;
      try {
        const raw = localStorage.getItem(VOICE_KEY);
        if (raw === "1" || raw === "0") saved = raw === "1";
      } catch {
      }
      this.on = saved != null ? saved : enabled;
      (_b = (_a = this.synth) == null ? void 0 : _a.addEventListener) == null ? void 0 : _b.call(_a, "voiceschanged", () => this.currentVoice());
    }
    get enabled() {
      return this.on;
    }
    setEnabled(v) {
      this.on = v;
      if (!v) this.cancel();
      try {
        localStorage.setItem(VOICE_KEY, v ? "1" : "0");
      } catch {
      }
    }
    /** 本机全部中文语音（Windows 一般 2-3 个，macOS 1-2 个，Linux 可能没有） */
    listZhVoices() {
      if (!this.synth) return [];
      return this.synth.getVoices().filter((v) => {
        var _a;
        return (_a = v.lang) == null ? void 0 : _a.toLowerCase().startsWith("zh");
      });
    }
    /** 当前生效的语音：记住的偏好名 → 第一个 zh-CN → 列表第一个 */
    currentVoice() {
      const list = this.listZhVoices();
      if (!list.length) return null;
      let savedName = null;
      try {
        savedName = localStorage.getItem(VOICE_NAME_KEY);
      } catch {
      }
      if (savedName) {
        const hit = list.find((v) => v.name === savedName);
        if (hit) return hit;
      }
      return list.find((v) => v.lang === "zh-CN") || list[0];
    }
    /** 切到下一个中文语音并持久化偏好；无语音可切返回 null */
    cycleVoice() {
      const list = this.listZhVoices();
      if (!list.length) return null;
      const cur = this.currentVoice();
      const idx = cur ? list.indexOf(cur) : -1;
      const next = list[(idx + 1) % list.length];
      try {
        localStorage.setItem(VOICE_NAME_KEY, next.name);
      } catch {
      }
      return next;
    }
    /** 菜单展示用：当前音色名（截断），无中文语音时显示「无」 */
    voiceLabel() {
      const v = this.currentVoice();
      if (!v) return "\u65E0";
      return v.name.length > 12 ? `${v.name.slice(0, 12)}\u2026` : v.name;
    }
    /** 朗读一段文本；关闭状态/无语音环境/文本为空都静默跳过 */
    speak(text) {
      if (!this.on || !this.synth) return;
      const clean = text.replace(/\s+/g, " ").trim().slice(0, 200);
      if (!clean) return;
      try {
        this.synth.cancel();
        const u = new SpeechSynthesisUtterance(clean);
        const v = this.currentVoice();
        if (v) {
          u.voice = v;
          u.lang = v.lang;
        } else {
          u.lang = "zh-CN";
        }
        u.rate = 1.05;
        u.pitch = 1.15;
        this.synth.speak(u);
      } catch {
      }
    }
    cancel() {
      var _a;
      try {
        (_a = this.synth) == null ? void 0 : _a.cancel();
      } catch {
      }
    }
  };

  // src/pet.ts
  var MOBILE_QUERY = "(max-width: 767px)";
  var POS_KEY = "dafeiyu:pos";
  var MARGIN = 16;
  var IDLE_STAY_MS = [8e3, 15e3];
  var SLEEP_AFTER_MS = 5 * 60 * 1e3;
  var ACTIVITY_CHECK_MS = 10 * 1e3;
  var START_DELAY_MS = 30 * 1e3;
  var COMBO_WINDOW_MS = 3e3;
  var COMBO_ANGRY = 5;
  var THROW_SPEED = 0.5;
  var RARE_CHANCE = 0.02;
  var RARE_CHANCE_FRIEND = 0.05;
  var WANDER_CHANCE = 0.08;
  var BUBBLE_MS = 1e4;
  var LONG_PRESS_MS = 800;
  var CHAT_HISTORY_KEY = "dafeiyu:chat-history";
  var CHAT_MAX_ITEMS = 10;
  var ASPECT_H = 9 / 16;
  function rand(min, max) {
    return min + Math.random() * (max - min);
  }
  function trimHistory(list) {
    return list.slice(-CHAT_MAX_ITEMS).map((m) => ({ role: m.role, content: m.content.slice(0, 500) }));
  }
  var Pet = class _Pet {
    constructor(options = {}) {
      this.x = 0;
      this.y = 0;
      this.h = 0;
      this.state = "idle";
      this.stateToken = 0;
      this.lastIdle = null;
      this.lastLine = "";
      this.timers = /* @__PURE__ */ new Set();
      this.wanderRaf = 0;
      this.flightCancel = null;
      this.detachDrag = null;
      this.lastActivity = Date.now();
      this.started = false;
      this.combo = 0;
      this.comboAt = 0;
      this.dead = false;
      this.chatPanel = null;
      this.chatOpen = false;
      this.chatHistory = [];
      this.chatAbort = null;
      this.menu = null;
      this.gamePanel = null;
      this.gameOpen = false;
      this.longPressTimer = 0;
      this.justPetted = false;
      this.onActivity = () => {
        this.lastActivity = Date.now();
        this.started = true;
      };
      this.onVisibility = () => {
        if (document.hidden) this.suspend();
        else this.resume();
      };
      this.onResize = () => {
        this.clampIntoViewport();
        this.applyTransform();
      };
      // ── 拖拽与点击 ────────────────────────────────────────────────
      this.grabOffX = 0;
      this.grabOffY = 0;
      this.dragging = false;
      var _a, _b, _c;
      (_a = _Pet.current) == null ? void 0 : _a.destroy();
      _Pet.current = this;
      this.opts = { ...DEFAULTS, ...options };
      this.extraLines = (_b = options.lines) != null ? _b : [];
      this.endpoints = options.endpoints;
      this.reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (!this.opts.enabledOnMobile && matchMedia(MOBILE_QUERY).matches) {
        this.dead = true;
        return;
      }
      this.buildDom();
      this.restorePosition();
      this.player = new Player(this.video, this.opts.assetBase);
      this.bubble = new Bubble(this.shadow);
      this.speech = new Speech(this.opts.voice);
      this.detachDrag = attachDrag(this.video, {
        onGrab: (px, py) => {
          this.grabOffX = px - this.x;
          this.grabOffY = py - this.y;
        },
        onDragMove: (px, py) => this.handleDragMove(px, py),
        onRelease: (vx, vy) => this.handleRelease(vx, vy),
        onClick: () => this.handleClick()
      });
      if ((_c = this.endpoints) == null ? void 0 : _c.chat) {
        this.chatPanel = new ChatPanel(this.shadow, {
          title: this.petName,
          onSend: (text) => this.sendChat(text),
          onClose: () => this.closeChat()
        });
        this.chatHistory = this.loadHistory();
        this.video.addEventListener(
          "dblclick",
          () => this.chatOpen ? this.closeChat() : this.openChat()
        );
      }
      this.gamePanel = new GomokuPanel(this.shadow, {
        onEnd: (result) => this.onGameEnd(result),
        onClose: () => this.closeGame()
      });
      this.menu = new PetMenu(this.shadow, this.host, {
        onSay: () => this.forceSay(),
        onFeed: () => this.feed(),
        onToggleSleep: () => this.toggleSleep(),
        onResetPos: () => this.resetPos(),
        onChat: () => this.chatOpen ? this.closeChat() : this.openChat(),
        onGame: () => this.gameOpen ? this.closeGame() : this.openGame(),
        onToggleVoice: () => this.setVoice(!this.speech.enabled),
        onCycleVoice: () => this.cycleVoice()
      });
      this.video.addEventListener("contextmenu", (e) => {
        var _a2;
        e.preventDefault();
        if ((_a2 = this.menu) == null ? void 0 : _a2.isOpen) this.menu.close();
        else this.openMenu();
      });
      this.video.addEventListener("pointerdown", () => {
        this.cancelLongPress();
        this.longPressTimer = window.setTimeout(() => this.petting(), LONG_PRESS_MS);
      });
      this.video.addEventListener("pointerup", () => this.cancelLongPress());
      this.video.addEventListener("pointercancel", () => this.cancelLongPress());
      this.video.addEventListener("pointermove", (e) => {
        if (e.buttons && this.longPressTimer) this.cancelLongPress();
      });
      document.addEventListener("pointerdown", this.onActivity, { passive: true, capture: true });
      document.addEventListener("keydown", this.onActivity, { passive: true, capture: true });
      document.addEventListener("visibilitychange", this.onVisibility);
      window.addEventListener("resize", this.onResize);
      if (this.reduced) {
        void this.player.play("idle-breathing", true).then(() => this.player.pause());
        return;
      }
      void this.player.play("idle-breathing", true).then((ok) => {
        if (!ok) {
          console.warn("[dafeiyu] \u4E3B\u5F85\u673A\u52A8\u753B\u52A0\u8F7D\u5931\u8D25\uFF0C\u5BA0\u7269\u9000\u51FA\u3002\u68C0\u67E5 assetBase \u662F\u5426\u6B63\u786E");
          this.destroy();
          return;
        }
        this.h = this.video.getBoundingClientRect().height || this.opts.size * ASPECT_H;
      });
      this.setT(() => this.started = true, START_DELAY_MS);
      this.scheduleIdle(rand(1500, 4e3));
      this.scheduleWhisper();
      this.setT(this.checkSleep.bind(this), ACTIVITY_CHECK_MS);
    }
    // ── DOM 与位置 ────────────────────────────────────────────────
    buildDom() {
      this.host = document.createElement("div");
      this.host.setAttribute("data-dafeiyu", "");
      const hs = this.host.style;
      hs.position = "fixed";
      hs.left = "0";
      hs.top = "0";
      hs.width = `${this.opts.size}px`;
      hs.zIndex = String(this.opts.zIndex);
      hs.pointerEvents = "none";
      hs.contain = "layout style";
      this.shadow = this.host.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = `
      video {
        display: block;
        width: 100%;
        height: auto;
        pointer-events: auto;
        cursor: grab;
        user-select: none;
        -webkit-user-drag: none;
      }
      video:active { cursor: grabbing; }
    `;
      this.shadow.appendChild(style);
      this.video = document.createElement("video");
      this.shadow.appendChild(this.video);
      document.body.appendChild(this.host);
    }
    restorePosition() {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const size = this.opts.size;
      const margin = MARGIN + 4;
      let nx, ny;
      const p = this.opts.position;
      if (p === "bottom-left" || p === "top-left" || p === "bottom-right" || p === "top-right") {
        nx = p.endsWith("right") ? 1 : 0;
        ny = p.startsWith("bottom") ? 1 : 0;
      } else if (p && typeof p.x === "number" && typeof p.y === "number") {
        nx = p.x;
        ny = p.y;
      } else {
        try {
          const saved = JSON.parse(localStorage.getItem(POS_KEY) || "null");
          if (saved && typeof saved.nx === "number" && typeof saved.ny === "number") {
            nx = saved.nx;
            ny = saved.ny;
          } else {
            nx = 1;
            ny = 1;
          }
        } catch {
          nx = 1;
          ny = 1;
        }
      }
      this.x = Math.min(Math.max(nx * vw - size / 2, margin), vw - size - margin);
      this.y = Math.min(Math.max(ny * vh - this.hPlaceholder() / 2, margin), vh - size * ASPECT_H - margin);
      this.applyTransform();
    }
    hPlaceholder() {
      return this.h || this.opts.size * ASPECT_H;
    }
    applyTransform() {
      this.host.style.transform = `translate(${Math.round(this.x)}px, ${Math.round(this.y)}px)`;
    }
    clampIntoViewport() {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      this.x = Math.min(Math.max(this.x, 4), Math.max(vw - this.opts.size - 4, 4));
      this.y = Math.min(Math.max(this.y, 4), Math.max(vh - this.hPlaceholder() - 4, 4));
    }
    savePos() {
      try {
        localStorage.setItem(
          POS_KEY,
          JSON.stringify({
            nx: (this.x + this.opts.size / 2) / window.innerWidth,
            ny: (this.y + this.hPlaceholder() / 2) / window.innerHeight
          })
        );
      } catch {
      }
    }
    // ── 定时器 ────────────────────────────────────────────────────
    setT(fn, ms) {
      const id = window.setTimeout(() => {
        this.timers.delete(id);
        fn();
      }, ms);
      this.timers.add(id);
    }
    clearTimers() {
      for (const id of this.timers) window.clearTimeout(id);
      this.timers.clear();
      if (this.wanderRaf) {
        cancelAnimationFrame(this.wanderRaf);
        this.wanderRaf = 0;
      }
    }
    bump() {
      return ++this.stateToken;
    }
    // ── 待机调度 ──────────────────────────────────────────────────
    scheduleIdle(delayMs) {
      this.setT(() => this.pickNext(), delayMs);
    }
    pickNext() {
      if (this.dead || this.reduced || this.state !== "idle") return;
      if (this.gameOpen) return;
      const rare = loadAffinity() >= 100 ? RARE_CHANCE_FRIEND : RARE_CHANCE;
      const r = Math.random();
      if (r < rare && !this.player.unavailable.has("rare-whale")) {
        this.runOnce("rare-whale", "react");
        return;
      }
      if (r < RARE_CHANCE + WANDER_CHANCE && !this.player.unavailable.has("walk-crab")) {
        this.wander();
        return;
      }
      const name = this.pickIdle();
      if (!name) return;
      this.lastIdle = name;
      if (LOOPING.has(name)) {
        void this.player.play(name, true).then((ok) => {
          if (!ok) this.pickNext();
        });
        this.scheduleIdle(rand(IDLE_STAY_MS[0], IDLE_STAY_MS[1]));
      } else {
        this.runOnce(name, "idle");
      }
    }
    /** 待机池加权随机：呼吸为底，深夜哈欠加权；尽量不与上一段重复 */
    pickIdle() {
      let pool = IDLE_POOL.filter((n) => !this.player.unavailable.has(n));
      if (pool.length > 1 && this.lastIdle) {
        const filtered = pool.filter((n) => n !== this.lastIdle);
        if (filtered.length) pool = filtered;
      }
      if (!pool.length) return null;
      const hour = (/* @__PURE__ */ new Date()).getHours();
      const weight = (n) => {
        if (n === "idle-breathing") return 3;
        if (n === "idle-humming") return 1.2;
        if (n === "idle-looking") return 1;
        return hour >= 22 || hour < 6 ? 2.2 : 0.5;
      };
      const total = pool.reduce((s, n) => s + weight(n), 0);
      let r = Math.random() * total;
      for (const n of pool) {
        r -= weight(n);
        if (r <= 0) return n;
      }
      return pool[pool.length - 1];
    }
    /** 播一遍动画然后回到 idle 链；token 防旧链路复活 */
    async runOnce(name, from) {
      const token = this.bump();
      this.state = from === "idle" ? "idle" : from;
      const ok = await this.player.playOnce(name);
      if (token !== this.stateToken) return;
      if (this.state === "drag" || this.state === "fly" || this.dead) return;
      this.state = "idle";
      this.scheduleIdle(rand(300, 900));
    }
    /** 横向漫游：播走路动画的同时把容器挪一段 */
    wander() {
      this.state = "wander";
      this.bump();
      const vw = window.innerWidth;
      const dir = Math.random() < 0.5 ? -1 : 1;
      const from = this.x;
      const to = Math.min(Math.max(from + dir * rand(120, 360), MARGIN), vw - this.opts.size - MARGIN);
      const dur = rand(1500, 3500);
      const t0 = performance.now();
      this.player.setMirror(dir < 0);
      void this.player.play("walk-crab", false);
      const step = (now) => {
        if (this.state !== "wander") return;
        const k = Math.min((now - t0) / dur, 1);
        const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
        this.x = from + (to - from) * e;
        this.applyTransform();
        if (k < 1) {
          this.wanderRaf = requestAnimationFrame(step);
        } else {
          this.wanderRaf = 0;
          this.player.setMirror(false);
          this.savePos();
          this.state = "idle";
          this.scheduleIdle(rand(300, 900));
        }
      };
      this.wanderRaf = requestAnimationFrame(step);
    }
    // ── 睡眠 ──────────────────────────────────────────────────────
    checkSleep() {
      if (this.dead) return;
      if (this.state === "idle" && !this.gameOpen && Date.now() - this.lastActivity > SLEEP_AFTER_MS) {
        this.state = "sleep";
        this.bump();
        void this.player.play("sleep-snooze", true);
      }
      this.setT(this.checkSleep.bind(this), ACTIVITY_CHECK_MS);
    }
    wake() {
      this.bubble.hide();
      void this.runOnce("wake-startled", "react");
    }
    // ── 碎碎念 ────────────────────────────────────────────────────
    scheduleWhisper() {
      const base = this.opts.whisperIntervalSec * 1e3;
      this.setT(() => {
        this.tryWhisper();
        this.scheduleWhisper();
      }, base * rand(0.7, 1.3));
    }
    async tryWhisper() {
      var _a;
      if (this.dead || this.reduced || !this.started || document.hidden) return;
      if (this.state !== "idle" || this.gameOpen) return;
      let line = this.pickLine();
      let image;
      if ((_a = this.endpoints) == null ? void 0 : _a.whisper) {
        const remote = await this.fetchWhisper();
        if (this.dead || this.state !== "idle") return;
        if (remote) {
          line = remote.line;
          image = remote.image ? this.memeUrl(remote.image) : void 0;
        }
      }
      if (!line) return;
      this.clearTimers();
      this.state = "talk";
      this.bump();
      void this.player.play("talk-screen", true);
      const ny = (this.y + this.hPlaceholder() / 2) / window.innerHeight;
      this.bubble.say(line, BUBBLE_MS, ny < 0.28, image);
      this.speech.speak(line);
      this.setT(() => {
        if (this.state === "talk") {
          this.state = "idle";
          this.scheduleIdle(rand(300, 900));
        }
      }, BUBBLE_MS);
    }
    /** 拉一句后端碎碎念（可带配图键名）；任何失败都返回 null，由本地台词池兜底 */
    async fetchWhisper() {
      var _a;
      const url = (_a = this.endpoints) == null ? void 0 : _a.whisper;
      if (!url) return null;
      try {
        const target = new URL(url, location.href);
        if (this.opts.pageContext !== false) {
          target.searchParams.set("title", document.title.slice(0, 60));
          target.searchParams.set("path", location.pathname.slice(0, 120));
        }
        const ctrl = new AbortController();
        const timer = window.setTimeout(() => ctrl.abort(), 8e3);
        const res = await fetch(target.href, { signal: ctrl.signal });
        window.clearTimeout(timer);
        if (!res.ok) return null;
        const data = await res.json();
        if (typeof (data == null ? void 0 : data.line) === "string") {
          const t = data.line.trim();
          if (t && t.length <= 120) {
            return {
              line: t,
              image: typeof data.image === "string" ? data.image.slice(0, 40) : void 0
            };
          }
        }
        return null;
      } catch {
        return null;
      }
    }
    pickLine() {
      let pool = BUILTIN_LINES.concat(this.extraLines);
      if (loadAffinity() >= 300) pool = pool.concat(FRIEND_LINES);
      if (!pool.length) return null;
      let line = pool[Math.floor(Math.random() * pool.length)];
      if (pool.length > 1) {
        for (let i = 0; i < 3 && line === this.lastLine; i++) {
          line = pool[Math.floor(Math.random() * pool.length)];
        }
      }
      this.lastLine = line;
      return line;
    }
    handleDragMove(px, py) {
      var _a;
      if (!this.dragging) {
        this.dragging = true;
        this.clearTimers();
        (_a = this.flightCancel) == null ? void 0 : _a.call(this);
        this.flightCancel = null;
        this.state = "drag";
        this.bump();
        this.bubble.hide();
        void this.player.play("drag-startled", false);
      }
      this.lastActivity = Date.now();
      this.x = Math.min(Math.max(px - this.grabOffX, 0), window.innerWidth - this.opts.size);
      this.y = Math.min(Math.max(py - this.grabOffY, 0), window.innerHeight - this.hPlaceholder());
      this.applyTransform();
    }
    handleRelease(vx, vy) {
      var _a;
      this.dragging = false;
      this.lastActivity = Date.now();
      const speed = Math.hypot(vx, vy);
      if (speed > THROW_SPEED) {
        this.state = "fly";
        this.bump();
        (_a = this.flightCancel) == null ? void 0 : _a.call(this);
        this.flightCancel = startFlight({
          x: this.x,
          y: this.y,
          vx,
          vy,
          bounds: () => ({
            vw: window.innerWidth,
            vh: window.innerHeight,
            w: this.opts.size,
            h: this.hPlaceholder()
          }),
          onStep: (x, y) => {
            this.x = x;
            this.y = y;
            this.applyTransform();
          },
          onRest: (x, y) => {
            this.flightCancel = null;
            this.x = x;
            this.y = y;
            this.savePos();
            if (this.state === "fly") {
              this.state = "idle";
              this.scheduleIdle(rand(300, 900));
            }
          }
        });
      } else {
        this.savePos();
        this.state = "idle";
        this.scheduleIdle(rand(200, 600));
      }
    }
    handleClick() {
      var _a, _b;
      this.lastActivity = Date.now();
      this.started = true;
      if (this.dead) return;
      if (this.justPetted) {
        this.justPetted = false;
        return;
      }
      if (this.chatOpen) return;
      if (this.gameOpen) return;
      if (this.state === "sleep") {
        this.wake();
        return;
      }
      this.bubble.hide();
      const now = Date.now();
      this.combo = now - this.comboAt < COMBO_WINDOW_MS ? this.combo + 1 : 1;
      this.comboAt = now;
      const angry = this.combo >= COMBO_ANGRY;
      const pool = CLICK_POOL.filter((n) => !this.player.unavailable.has(n));
      const name = angry ? this.player.unavailable.has("click-annoyed") ? (_a = pool[0]) != null ? _a : null : "click-annoyed" : (_b = pool[Math.floor(Math.random() * pool.length)]) != null ? _b : null;
      if (name) void this.runOnce(name, "react");
      if (angry) {
        this.bubble.say("\u6233\u591F\u4E86\u6CA1\u6709\uFF01", 3e3, false);
        this.combo = 0;
      }
    }
    // ── 聊天（endpoints.chat 配置后启用）─────────────────────────
    openChat() {
      if (this.dead || !this.chatPanel) return;
      if (this.gameOpen) this.closeGame();
      if (!this.chatOpen) {
        this.chatOpen = true;
        this.clearTimers();
        this.state = "talk";
        this.bump();
        this.bubble.hide();
        void this.player.play("talk-screen", true);
      }
      this.chatPanel.open();
    }
    closeChat() {
      var _a, _b;
      if (!this.chatOpen) return;
      this.chatOpen = false;
      (_a = this.chatAbort) == null ? void 0 : _a.abort();
      this.chatAbort = null;
      (_b = this.chatPanel) == null ? void 0 : _b.close();
      if (!this.dead && !this.reduced) {
        this.state = "idle";
        this.scheduleIdle(rand(300, 900));
      }
    }
    sendChat(text) {
      var _a;
      const panel = this.chatPanel;
      const url = (_a = this.endpoints) == null ? void 0 : _a.chat;
      if (!panel || !url) return;
      this.lastActivity = Date.now();
      panel.addUser(text);
      panel.setSending(true);
      void this.player.play("talk-thinking", true);
      const outgoing = [...this.chatHistory, { role: "user", content: text }];
      const handle = panel.startAssistant();
      let acc = "";
      let firstDelta = true;
      this.chatAbort = streamChat(url, outgoing, {
        onDelta: (d) => {
          if (firstDelta) {
            firstDelta = false;
            void this.player.play("talk-screen", true);
          }
          acc += d;
          handle.push(d);
        },
        onImage: (key) => {
          const u = this.memeUrl(key);
          if (u) handle.setImage(u);
        },
        onDone: () => {
          handle.done();
          this.chatHistory = trimHistory([...outgoing, { role: "assistant", content: acc || "\u2026" }]);
          this.saveHistory();
          panel.setSending(false);
          this.chatAbort = null;
          this.speech.speak(acc);
        },
        onError: (msg) => {
          handle.error(msg);
          panel.setSending(false);
          this.chatAbort = null;
        }
      });
    }
    loadHistory() {
      try {
        const raw = JSON.parse(localStorage.getItem(CHAT_HISTORY_KEY) || "[]");
        if (!Array.isArray(raw)) return [];
        return trimHistory(
          raw.filter(
            (m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string"
          )
        );
      } catch {
        return [];
      }
    }
    saveHistory() {
      try {
        localStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(this.chatHistory));
      } catch {
      }
    }
    // ── 互动（右键菜单 / 手势入口）──────────────────────────────
    openMenu() {
      var _a;
      if (this.dead || !this.menu) return;
      const n = loadAffinity();
      this.menu.open(
        this.petName,
        n,
        levelOf(n).name,
        this.state === "sleep",
        !!((_a = this.endpoints) == null ? void 0 : _a.chat),
        this.speech.enabled,
        this.speech.voiceLabel()
      );
    }
    /** 语音朗读开关（右键菜单入口）；关闭时立刻停掉正在读的 */
    setVoice(on) {
      this.speech.setEnabled(on);
    }
    /** 切换到下一个本机中文音色并试听一句；无语音可切时静默 */
    cycleVoice() {
      const v = this.speech.cycleVoice();
      if (v) this.speech.speak("\u5C31\u8FD9\u4E2A\u58F0\u97F3\u5566\uFF5E");
    }
    get petName() {
      var _a;
      return (_a = this.opts.name) != null ? _a : "\u5927\u80A5\u9C7C";
    }
    /** 表情包键名 → 素材 URL；键名只放行 ASCII，防注入奇怪路径 */
    memeUrl(key) {
      return /^[a-z0-9-]{1,40}$/.test(key) ? `${this.opts.assetBase}memes/${key}.png` : void 0;
    }
    cancelLongPress() {
      if (this.longPressTimer) {
        window.clearTimeout(this.longPressTimer);
        this.longPressTimer = 0;
      }
    }
    /** 摸头：长按触发，好感 +1；好感 ≥30（熟悉）后有概率冒泡回应 */
    petting() {
      this.cancelLongPress();
      if (this.dead || this.chatOpen) return;
      this.justPetted = true;
      this.lastActivity = Date.now();
      const n = addAffinity(1);
      this.bubble.hide();
      if (n >= 30 && Math.random() < 0.34) {
        const pets = ["\u5495\u565C\u5495\u565C\u2026\u597D\u8212\u670D", "\u518D\u6478\u4F1A\u513F\u4E5F\u884C", "\u6478\u5934\u6740\uFF01"];
        this.bubble.say(pets[Math.floor(Math.random() * pets.length)], 5e3);
      }
      void this.runOnce("pet-scratch", "react");
    }
    /** 喂食：好感 +2，随机吃一段；抽到「偷吃被抓住」配一句嘴硬台词 */
    feed() {
      if (this.dead) return;
      this.lastActivity = Date.now();
      addAffinity(2);
      this.bubble.hide();
      const pool = FEED_POOL.filter((a) => !this.player.unavailable.has(a));
      if (!pool.length) return;
      const name = pool[Math.floor(Math.random() * pool.length)];
      if (name === "eat-caught") this.bubble.say("\u4E0D\u3001\u4E0D\u662F\u6211\u5077\u5403\u7684\uFF01", 5e3);
      this.state = "react";
      void this.runOnce(name, "react");
    }
    /** 菜单里的睡觉/叫醒开关 */
    toggleSleep() {
      if (this.dead) return;
      this.lastActivity = Date.now();
      if (this.state === "sleep") {
        this.wake();
      } else {
        this.clearTimers();
        this.state = "sleep";
        this.bump();
        this.bubble.hide();
        void this.player.play("sleep-snooze", true);
      }
    }
    /** 回到默认角落（右下），并清掉记住的位置；进行中的抛掷也要停 */
    resetPos() {
      var _a;
      if (this.dead) return;
      (_a = this.flightCancel) == null ? void 0 : _a.call(this);
      this.flightCancel = null;
      if (this.state === "fly") {
        this.state = "idle";
        this.scheduleIdle(rand(300, 900));
      }
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      this.x = vw - this.opts.size - MARGIN;
      this.y = vh - this.hPlaceholder() - MARGIN;
      this.applyTransform();
      this.savePos();
    }
    // ── 五子棋（本地小游戏）─────────────────────────────────────
    openGame() {
      if (this.dead || !this.gamePanel) return;
      if (this.chatOpen) this.closeChat();
      if (!this.gameOpen) {
        this.gameOpen = true;
        this.clearTimers();
        this.bubble.hide();
        void this.player.play("play-gomoku", true);
      }
      this.gamePanel.open();
    }
    closeGame() {
      var _a;
      if (!this.gameOpen) return;
      this.gameOpen = false;
      (_a = this.gamePanel) == null ? void 0 : _a.close();
      if (!this.dead && !this.reduced) {
        this.state = "idle";
        void this.player.play("idle-breathing", true);
        this.scheduleIdle(rand(300, 900));
      }
    }
    /** 终局反应：访客赢 → 气急败坏；鱼赢 → 开心跃动；平局 → 哼歌。完事回到对局循环 */
    onGameEnd(result) {
      this.lastActivity = Date.now();
      addAffinity(result === "player" ? 5 : 3);
      const react = result === "player" ? "game-rage" : result === "pet" ? "click-happy" : "idle-humming";
      const line = result === "player" ? "\u53EF\u6076\uFF01\u8FD9\u5C40\u4E0D\u7B97\uFF0C\u518D\u6765\uFF01" : result === "pet" ? "\u563F\u563F\uFF0C\u627F\u8BA9\u627F\u8BA9\uFF5E" : "\u5E73\u5C40\u4E5F\u633A\u597D\uFF0C\u68CB\u9022\u5BF9\u624B\u3002";
      this.bubble.say(line, 6e3);
      this.speech.speak(line);
      void this.player.playOnce(react).then(() => {
        if (!this.dead && this.gameOpen) void this.player.play("play-gomoku", true);
      });
    }
    // ── 可见性 ────────────────────────────────────────────────────
    suspend() {
      var _a;
      this.clearTimers();
      (_a = this.flightCancel) == null ? void 0 : _a.call(this);
      this.flightCancel = null;
      this.player.pause();
    }
    resume() {
      if (this.dead || this.reduced) return;
      if (this.chatOpen) {
        void this.player.play("talk-screen", true);
        return;
      }
      if (this.gameOpen) {
        void this.player.play("play-gomoku", true);
        return;
      }
      if (this.state === "sleep") {
        void this.player.play("sleep-snooze", true);
        this.setT(this.checkSleep.bind(this), ACTIVITY_CHECK_MS);
        this.scheduleWhisper();
        return;
      }
      if (this.state === "drag") return;
      this.state = "idle";
      this.scheduleIdle(200);
      this.scheduleWhisper();
      this.setT(this.checkSleep.bind(this), ACTIVITY_CHECK_MS);
    }
    // ── 对外（调试/站点调用）与销毁 ───────────────────────────────
    forceSay(text) {
      if (this.dead) return;
      const line = text != null ? text : this.pickLine();
      if (!line) return;
      this.clearTimers();
      this.state = "talk";
      this.bump();
      void this.player.play("talk-screen", true);
      const ny = (this.y + this.hPlaceholder() / 2) / window.innerHeight;
      this.bubble.say(line, BUBBLE_MS, ny < 0.28);
      this.speech.speak(line);
      this.setT(() => {
        if (this.state === "talk") {
          this.state = "idle";
          this.scheduleIdle(rand(300, 900));
        }
      }, BUBBLE_MS);
    }
    forcePlay(name) {
      if (this.dead || !isAnimName(name) || this.player.unavailable.has(name)) return;
      this.clearTimers();
      this.state = "react";
      void this.runOnce(name, "react");
    }
    destroy() {
      var _a, _b, _c, _d, _e, _f, _g, _h;
      if (this.dead && !this.host) {
        if (_Pet.current === this) _Pet.current = void 0;
        return;
      }
      this.dead = true;
      this.clearTimers();
      this.cancelLongPress();
      (_a = this.flightCancel) == null ? void 0 : _a.call(this);
      (_b = this.chatAbort) == null ? void 0 : _b.abort();
      this.chatAbort = null;
      (_c = this.menu) == null ? void 0 : _c.destroy();
      (_d = this.gamePanel) == null ? void 0 : _d.destroy();
      this.speech.cancel();
      (_e = this.detachDrag) == null ? void 0 : _e.call(this);
      document.removeEventListener("pointerdown", this.onActivity, { capture: true });
      document.removeEventListener("keydown", this.onActivity, { capture: true });
      document.removeEventListener("visibilitychange", this.onVisibility);
      window.removeEventListener("resize", this.onResize);
      (_f = this.bubble) == null ? void 0 : _f.destroy();
      (_g = this.player) == null ? void 0 : _g.pause();
      (_h = this.host) == null ? void 0 : _h.remove();
      if (_Pet.current === this) _Pet.current = void 0;
    }
  };

  // src/index.ts
  function mount(options = {}) {
    if (typeof window === "undefined" || typeof document === "undefined") {
      throw new Error("[dafeiyu] mount() \u53EA\u80FD\u5728\u6D4F\u89C8\u5668\u91CC\u8C03\u7528");
    }
    let pet = null;
    const ensure = () => {
      if (!pet) pet = new Pet(options);
      return pet;
    };
    if (document.body) {
      pet = new Pet(options);
    } else {
      document.addEventListener("DOMContentLoaded", () => ensure(), { once: true });
    }
    return {
      destroy: () => {
        pet == null ? void 0 : pet.destroy();
        pet = null;
      },
      say: (text) => ensure().forceSay(text),
      play: (name) => ensure().forcePlay(name),
      feed: () => ensure().feed(),
      setVoice: (on) => ensure().setVoice(on),
      openChat: () => ensure().openChat(),
      closeChat: () => ensure().closeChat(),
      openGame: () => ensure().openGame(),
      closeGame: () => ensure().closeGame()
    };
  }
  var index_default = { mount };
  return __toCommonJS(index_exports);
})();
