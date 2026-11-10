const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pet", {
  onState(cb) {
    ipcRenderer.on("pet:state", (_e, payload) => cb(payload));
  },
  onSnapshot(cb) {
    ipcRenderer.on("pet:snapshot", () => cb());
  },
  setIgnore(v) {
    ipcRenderer.send("pet:set-ignore", !!v);
  },
  drag(dx, dy) {
    ipcRenderer.send("pet:drag", { dx, dy });
  },
  resetPosition() {
    ipcRenderer.send("pet:reset-position");
  },
  quit() {
    ipcRenderer.send("pet:quit");
  },
  config() {
    return ipcRenderer.sendSync("pet:config");
  },
  testBubble() {
    ipcRenderer.send("pet:test-bubble");
  },
});
