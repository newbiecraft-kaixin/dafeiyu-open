const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("petHost", {
  setIgnore: (flag) => ipcRenderer.send("set-ignore", !!flag),
  quit: () => ipcRenderer.send("pet-quit"),
});
