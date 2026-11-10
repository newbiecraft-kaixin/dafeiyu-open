const { app, BrowserWindow, ipcMain, screen, Tray, Menu, nativeImage } = require("electron");
const path = require("path");

let win = null;
let tray = null;

if (!app.requestSingleInstanceLock()) {
  app.quit();
}

function createWindow() {
  const area = screen.getPrimaryDisplay().workArea;
  win = new BrowserWindow({
    x: area.x,
    y: area.y,
    width: area.width,
    height: area.height,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    title: "大肥鱼",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  win.setAlwaysOnTop(true, "screen-saver");
  win.setIgnoreMouseEvents(true, { forward: true });
  win.loadFile(path.join(__dirname, "index.html"));

  win.on("closed", () => {
    win = null;
  });
}

function createTray() {
  try {
    const icon = nativeImage
      .createFromPath(path.join(__dirname, "assets", "memes", "cute.png"))
      .resize({ width: 20, height: 20 });
    tray = new Tray(icon);
    tray.setToolTip("大肥鱼");
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: "显示 / 隐藏", click: () => {
            if (!win) return;
            if (win.isVisible()) win.hide();
            else win.show();
          } },
        { type: "separator" },
        { label: "退出大肥鱼", click: () => app.quit() },
      ])
    );
  } catch (e) {
    console.error("tray failed", e);
  }
}

ipcMain.on("set-ignore", (_event, flag) => {
  if (!win) return;
  win.setIgnoreMouseEvents(!!flag, { forward: true });
});

ipcMain.on("pet-quit", () => app.quit());

app.whenReady().then(() => {
  createWindow();
  createTray();
});

app.on("window-all-closed", () => app.quit());
