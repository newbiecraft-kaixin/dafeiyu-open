// 大肥鱼桌宠气泡：把 /state 里的 pet_text 渲染成宠物头顶的对话气泡。
// 这是一个独立的小窗口，不依赖权限气泡的排队/审批逻辑。
const { BrowserWindow, ipcMain } = require("electron");

const MIN_WIDTH = 300;
const MAX_WIDTH = 440;
const DEFAULT_DURATION_MS = 9000;
const GAP = 10;

const HTML = `<!doctype html>
<meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: transparent; overflow: hidden;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased; }
  #card { box-sizing: border-box; padding: 8px 12px 10px; border-radius: 14px;
    background: rgba(255, 253, 250, 0.97);
    border: 1px solid rgba(120, 150, 200, 0.38);
    box-shadow: 0 6px 20px rgba(40, 60, 110, 0.18);
    color: #26324b; }
  #name { font-size: 11px; color: #6b86b8; margin-bottom: 3px; letter-spacing: 0.5px; }
  #text { font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
</style>
<div id="card"><div id="name">大肥鱼</div><div id="text"></div></div>
<script>
  const { ipcRenderer } = require("electron");
  ipcRenderer.on("pet-speech-render", (_event, payload) => {
    document.getElementById("name").textContent = payload.name || "大肥鱼";
    document.getElementById("text").textContent = payload.text || "";
    requestAnimationFrame(() => {
      const height = document.getElementById("card").getBoundingClientRect().height;
      ipcRenderer.send("pet-speech-height", Math.ceil(height));
    });
  });
</script>`;

function initPetSpeech(ctx) {
  let win = null;
  let ready = false;
  let pending = null;
  let hideTimer = null;
  let cardHeight = 0;

  function within(area) {
    try {
      if (area && typeof area.x === "number") return area;
    } catch {}
    return null;
  }

  function anchor() {
    const pet = typeof ctx.getPetWindowBounds === "function" ? ctx.getPetWindowBounds() : null;
    const area = within(typeof ctx.getBubbleWorkArea === "function" ? ctx.getBubbleWorkArea() : null);
    if (!pet || typeof pet.x !== "number") return null;
    const width = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round((pet.width || 160) * 1.25)));
    const height = Math.max(60, cardHeight || 96);
    let x = Math.round(pet.x + pet.width / 2 - width / 2);
    let y = Math.round(pet.y - height + GAP);
    if (area) {
      const minX = Math.round(area.x + 6);
      const maxX = Math.round(area.x + area.width - width - 6);
      const minY = Math.round(area.y + 6);
      const maxY = Math.round(area.y + area.height - height - 6);
      x = Math.min(Math.max(x, minX), Math.max(minX, maxX));
      if (y < minY) y = Math.round(pet.y + pet.height * 0.92 - height * 0.18);
      y = Math.min(Math.max(y, minY), Math.max(minY, maxY));
    }
    return { x, y, width, height };
  }

  function place() {
    if (!win || win.isDestroyed()) return;
    const bounds = anchor();
    if (!bounds) return;
    win.setBounds(bounds);
  }

  function applyHeight(height) {
    if (!Number.isFinite(height) || height <= 0) return;
    cardHeight = Math.min(260, Math.round(height) + 2);
    place();
  }

  function ensureWindow() {
    if (win && !win.isDestroyed()) return win;
    ready = false;
    cardHeight = 0;
    win = new BrowserWindow({
      width: MIN_WIDTH,
      height: 96,
      frame: false,
      transparent: true,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      focusable: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      hasShadow: false,
      show: false,
      webPreferences: {
        nodeIntegration: true,
        contextIsolation: false,
        sandbox: false,
        backgroundThrottling: false,
      },
    });
    win.setIgnoreMouseEvents(true, { forward: true });
    win.setAlwaysOnTop(true, "screen-saver");
    win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(HTML));
    win.webContents.on("did-finish-load", () => {
      ready = true;
      if (pending) {
        win.webContents.send("pet-speech-render", pending);
        pending = null;
      }
    });
    win.on("closed", () => {
      win = null;
      ready = false;
    });
    return win;
  }

  ipcMain.on("pet-speech-height", (event, height) => {
    if (!win || win.isDestroyed()) return;
    if (event.sender !== win.webContents) return;
    applyHeight(height);
  });

  function clearTimer() {
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  function hide() {
    clearTimer();
    if (win && !win.isDestroyed()) win.hide();
  }

  function show(payload) {
    if (!payload || typeof payload.text !== "string" || !payload.text.trim()) return;
    if (payload.dnd) return;
    if (ctx.petHidden === true) return;
    const w = ensureWindow();
    clearTimer();
    const message = { name: payload.name || "大肥鱼", text: payload.text.trim().slice(0, 400) };
    if (ready && !w.isDestroyed()) w.webContents.send("pet-speech-render", message);
    else pending = message;
    place();
    if (!w.isVisible()) w.showInactive();
    const duration = Number.isFinite(payload.durationMs) ? payload.durationMs : DEFAULT_DURATION_MS;
    hideTimer = setTimeout(() => {
      hideTimer = null;
      if (win && !win.isDestroyed()) win.hide();
    }, Math.max(1500, duration));
  }

  return { show, hide, reposition: place };
}

module.exports = { initPetSpeech };
