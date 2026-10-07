const { contextBridge, ipcRenderer } = require("electron");

function argument(name) {
  const prefix = `--${name}=`;
  const match = process.argv.find((item) => item.startsWith(prefix));
  return match ? match.slice(prefix.length) : "";
}

const apiBase = argument("windtower-api-base");
const appVersion = argument("windtower-app-version");

contextBridge.exposeInMainWorld("windTowerDesktop", {
  runtime: Object.freeze({
    desktop: true,
    apiBase,
    appVersion,
    platform: process.platform,
  }),
  getRuntimeInfo: () => ipcRenderer.invoke("desktop:get-runtime-info"),
  chooseFiles: () => ipcRenderer.invoke("desktop:choose-files"),
  openDataFolder: () => ipcRenderer.invoke("desktop:open-data-folder"),
  openLogs: () => ipcRenderer.invoke("desktop:open-logs"),
  checkForUpdates: () => ipcRenderer.invoke("desktop:check-for-updates"),
  downloadUpdate: (update) => ipcRenderer.invoke("desktop:download-update", update),
  saveLlmKey: (apiKey) => ipcRenderer.invoke("desktop:save-llm-key", apiKey),
  quitApplication: () => ipcRenderer.invoke("desktop:quit"),
  onUpdateAvailable: (callback) => {
    const listener = (_event, update) => callback(update);
    ipcRenderer.on("desktop:update-available", listener);
    return () => ipcRenderer.removeListener("desktop:update-available", listener);
  },
});
