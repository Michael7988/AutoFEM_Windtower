const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  ipcMain,
  nativeImage,
  net,
  safeStorage,
  screen,
  session,
  shell,
} = require("electron");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const nodeNet = require("node:net");

const isDevelopment = process.env.WINDTOWER_DESKTOP_DEV === "1";
const applicationName = "WindTowerCAE";
const localRoot = path.join(process.env.LOCALAPPDATA || app.getPath("userData"), applicationName);
const roamingRoot = path.join(process.env.APPDATA || app.getPath("userData"), applicationName);
const runtimePaths = {
  data: path.join(localRoot, "data"),
  work: path.join(localRoot, "work", "abaqus"),
  logs: path.join(localRoot, "logs"),
  cache: path.join(localRoot, "cache"),
  backups: path.join(localRoot, "backups"),
  config: path.join(roamingRoot, "config"),
};

let mainWindow;
let tray;
let backendProcess;
let backendPort;
let desktopToken;
let quitting = false;
let latestUpdate;

app.setName(applicationName);
app.setPath("userData", path.join(roamingRoot, "electron"));

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    showMainWindow();
  });
  app.whenReady().then(startApplication).catch(showFatalStartupError);
}

app.on("before-quit", () => {
  quitting = true;
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin" && quitting) app.quit();
});

app.on("will-quit", () => {
  stopBackend();
});

async function startApplication() {
  for (const value of Object.values(runtimePaths)) fs.mkdirSync(value, { recursive: true });
  backendPort = await findAvailablePort();
  desktopToken = crypto.randomBytes(32).toString("hex");
  installRequestTokenHeader();
  backendProcess = startBackend();
  await waitForBackend();
  await restoreLlmKey();
  createWindow();
  installIpcHandlers();
  createTray();
  setTimeout(checkForUpdatesAndNotify, 8000).unref();
}

function startBackend() {
  const logName = new Date().toISOString().replaceAll(":", "-").replaceAll(".", "-");
  const stdout = fs.openSync(path.join(runtimePaths.logs, `backend-${logName}.out.log`), "a");
  const stderr = fs.openSync(path.join(runtimePaths.logs, `backend-${logName}.err.log`), "a");
  const environment = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    WINDTOWER_DESKTOP_MODE: "1",
    WINDTOWER_DESKTOP_TOKEN: desktopToken,
    WINDTOWER_DATA_ROOT: runtimePaths.data,
    WINDTOWER_WORK_ROOT: runtimePaths.work,
    WINDTOWER_LOG_ROOT: runtimePaths.logs,
    WINDTOWER_CACHE_ROOT: runtimePaths.cache,
    WINDTOWER_CONFIG_ROOT: runtimePaths.config,
  };

  let executable;
  let args;
  let cwd;
  if (isDevelopment) {
    executable = process.env.PYTHON_EXECUTABLE || "python";
    args = [
      "-m",
      "uvicorn",
      "windtower_backend.app:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(backendPort),
      "--no-access-log",
    ];
    cwd = path.resolve(__dirname, "..", "..");
  } else {
    executable = path.join(process.resourcesPath, "backend-dist", "windtower-backend", "windtower-backend.exe");
    args = ["--host", "127.0.0.1", "--port", String(backendPort)];
    cwd = path.dirname(executable);
  }
  const child = spawn(executable, args, {
    cwd,
    env: environment,
    windowsHide: true,
    stdio: ["ignore", stdout, stderr],
  });
  fs.closeSync(stdout);
  fs.closeSync(stderr);
  child.once("exit", (code) => {
    if (!quitting && mainWindow) {
      dialog.showMessageBox(mainWindow, {
        type: "error",
        title: "后台服务已停止",
        message: "本地建模服务意外停止。",
        detail: `退出代码：${String(code)}\n请在日志目录中查看详细信息。`,
        buttons: ["打开日志目录", "关闭"],
      }).then(({ response }) => {
        if (response === 0) shell.openPath(runtimePaths.logs);
      });
    }
  });
  return child;
}

function createWindow() {
  const rendererLogPath = path.join(runtimePaths.logs, "renderer.log");
  const appendRendererLog = (message) => {
    fs.appendFileSync(rendererLogPath, `${new Date().toISOString()} ${message}\n`, "utf8");
  };
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 960,
    minWidth: 1100,
    minHeight: 720,
    show: false,
    backgroundColor: "#f4f7fb",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      additionalArguments: [
        `--windtower-api-base=http://127.0.0.1:${backendPort}`,
        `--windtower-app-version=${app.getVersion()}`,
      ],
    },
  });
  mainWindow.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    appendRendererLog(`did-fail-load code=${errorCode} url=${validatedURL} detail=${errorDescription}`);
  });
  mainWindow.webContents.on("render-process-gone", (_event, details) => {
    appendRendererLog(`render-process-gone reason=${details.reason} exitCode=${details.exitCode}`);
  });
  mainWindow.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    if (level >= 2) appendRendererLog(`console level=${level} source=${sourceId}:${line} message=${message}`);
  });
  mainWindow.once("ready-to-show", showMainWindow);
  mainWindow.on("close", (event) => {
    if (quitting) return;
    event.preventDefault();
    backendJson("/api/desktop/shutdown-readiness")
      .catch(() => ({ safe_to_exit: true }))
      .then((readiness) => {
        if (!readiness.safe_to_exit) {
          mainWindow.hide();
          tray?.displayBalloon({
            title: "计算继续运行",
            content: "Abaqus任务仍在后台执行，双击托盘图标可返回软件。",
          });
          return;
        }
        quitting = true;
        mainWindow.close();
      });
  });
  if (isDevelopment) {
    mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL || "http://127.0.0.1:5173");
  } else {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  const bounds = mainWindow.getBounds();
  const intersectsDisplay = screen.getAllDisplays().some(({ workArea }) => (
    bounds.x < workArea.x + workArea.width
    && bounds.x + bounds.width > workArea.x
    && bounds.y < workArea.y + workArea.height
    && bounds.y + bounds.height > workArea.y
  ));
  if (!intersectsDisplay) {
    const { workArea } = screen.getPrimaryDisplay();
    const width = Math.min(bounds.width, workArea.width);
    const height = Math.min(bounds.height, workArea.height);
    mainWindow.setBounds({
      x: workArea.x + Math.floor((workArea.width - width) / 2),
      y: workArea.y + Math.floor((workArea.height - height) / 2),
      width,
      height,
    });
  }

  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.setAlwaysOnTop(true);
  mainWindow.focus();
  mainWindow.setAlwaysOnTop(false);
}

function createTray() {
  const icon = nativeImage.createFromDataURL(
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAKElEQVR42mNkYPj/n4GBgYGJAQkwMjAwMDAwEP0nGGoAMjAwMDAwAAA9WQYf64z5JAAAAABJRU5ErkJggg==",
  );
  tray = new Tray(icon);
  tray.setToolTip("风电塔筒 CAE 建模工作台");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "打开工作台", click: () => mainWindow?.show() },
    { label: "打开数据目录", click: () => shell.openPath(runtimePaths.data) },
    { type: "separator" },
    { label: "退出", click: requestQuit },
  ]));
  tray.on("double-click", showMainWindow);
}

function installRequestTokenHeader() {
  const filter = { urls: [`http://127.0.0.1:${backendPort}/*`] };
  session.defaultSession.webRequest.onBeforeSendHeaders(filter, (details, callback) => {
    details.requestHeaders["X-WindTower-Desktop-Token"] = desktopToken;
    callback({ requestHeaders: details.requestHeaders });
  });
}

function installIpcHandlers() {
  ipcMain.handle("desktop:get-runtime-info", () => backendJson("/api/desktop/health"));
  ipcMain.handle("desktop:choose-files", async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "选择工程资料",
      properties: ["openFile", "multiSelections"],
      filters: [
        { name: "工程资料", extensions: ["xls", "xlsx", "xlsm", "csv", "tsv", "json", "yaml", "yml", "txt", "md", "inp", "pdf", "docx", "pptx", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"] },
        { name: "所有文件", extensions: ["*"] },
      ],
    });
    return result.canceled ? [] : result.filePaths;
  });
  ipcMain.handle("desktop:open-data-folder", () => shell.openPath(runtimePaths.data));
  ipcMain.handle("desktop:open-logs", () => shell.openPath(runtimePaths.logs));
  ipcMain.handle("desktop:check-for-updates", checkForUpdates);
  ipcMain.handle("desktop:download-update", (_event, update) => downloadUpdate(update));
  ipcMain.handle("desktop:save-llm-key", (_event, apiKey) => saveLlmKey(apiKey));
  ipcMain.handle("desktop:quit", requestQuit);
}

async function requestQuit() {
  const readiness = await backendJson("/api/desktop/shutdown-readiness").catch(() => ({ safe_to_exit: true, active_count: 0 }));
  if (!readiness.safe_to_exit) {
    const result = await dialog.showMessageBox(mainWindow, {
      type: "warning",
      title: "仍有计算任务",
      message: `当前还有 ${readiness.active_count} 个计算任务。`,
      detail: "强制退出会中断本机Abaqus任务，下次启动后可从中断状态重试。",
      buttons: ["返回并继续计算", "强制退出"],
      defaultId: 0,
      cancelId: 0,
    });
    if (result.response === 0) return { quitting: false, activeCount: readiness.active_count };
  }
  quitting = true;
  app.quit();
  return { quitting: true, activeCount: readiness.active_count || 0 };
}

async function waitForBackend() {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    if (backendProcess?.exitCode !== null) throw new Error("内置后端进程启动失败。");
    try {
      const response = await net.fetch(`http://127.0.0.1:${backendPort}/api/desktop/health`, {
        headers: { "X-WindTower-Desktop-Token": desktopToken },
      });
      if (response.ok) return;
    } catch {
      // Backend native libraries can take several seconds to load.
    }
    await new Promise((resolve) => setTimeout(resolve, 350));
  }
  throw new Error("等待内置后端启动超时，请检查桌面日志。");
}

async function backendJson(endpoint, init = {}) {
  const response = await net.fetch(`http://127.0.0.1:${backendPort}${endpoint}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-WindTower-Desktop-Token": desktopToken,
      ...(init.headers || {}),
    },
  });
  if (!response.ok) throw new Error(`Desktop backend HTTP ${response.status}`);
  return response.json();
}

async function findAvailablePort() {
  return new Promise((resolve, reject) => {
    const server = nodeNet.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

function stopBackend() {
  if (!backendProcess || backendProcess.exitCode !== null) return;
  backendProcess.kill();
  backendProcess = undefined;
}

function secretsPath() {
  return path.join(runtimePaths.config, "desktop-secrets.json");
}

async function saveLlmKey(apiKey) {
  const value = String(apiKey || "").trim();
  if (!value) throw new Error("API Key不能为空。");
  if (!safeStorage.isEncryptionAvailable()) throw new Error("Windows安全存储当前不可用。");
  const encrypted = safeStorage.encryptString(value).toString("base64");
  fs.writeFileSync(secretsPath(), JSON.stringify({ qwen_api_key: encrypted }, null, 2), { encoding: "utf8", mode: 0o600 });
  await backendJson("/api/desktop/settings/llm-key", {
    method: "POST",
    body: JSON.stringify({ api_key: value }),
  });
  return { configured: true };
}

async function restoreLlmKey() {
  if (!fs.existsSync(secretsPath()) || !safeStorage.isEncryptionAvailable()) return;
  try {
    const payload = JSON.parse(fs.readFileSync(secretsPath(), "utf8"));
    const value = safeStorage.decryptString(Buffer.from(payload.qwen_api_key, "base64"));
    await backendJson("/api/desktop/settings/llm-key", {
      method: "POST",
      body: JSON.stringify({ api_key: value }),
    });
  } catch {
    // A moved or damaged DPAPI blob must not prevent the modeling app starting.
  }
}

async function checkForUpdates() {
  const manifestUrl = process.env.WINDTOWER_UPDATE_MANIFEST_URL;
  if (!manifestUrl) return { available: false, reason: "update_manifest_not_configured" };
  const response = await net.fetch(manifestUrl, { cache: "no-store" });
  if (!response.ok) return { available: false, reason: `http_${response.status}` };
  const update = await response.json();
  if (!update.version || !update.download_url) return { available: false, reason: "invalid_manifest" };
  latestUpdate = update;
  return { available: compareVersions(update.version, app.getVersion()) > 0, update };
}

async function checkForUpdatesAndNotify() {
  const statePath = path.join(runtimePaths.config, "update-state.json");
  try {
    if (fs.existsSync(statePath)) {
      const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
      if (Date.now() - Number(state.last_checked_at || 0) < 24 * 60 * 60 * 1000) return;
    }
    const result = await checkForUpdates();
    fs.writeFileSync(statePath, JSON.stringify({ last_checked_at: Date.now() }, null, 2), "utf8");
    if (result.available && mainWindow) mainWindow.webContents.send("desktop:update-available", result.update);
  } catch {
    // Update availability never blocks local engineering work.
  }
}

async function downloadUpdate(update) {
  const candidate = update?.download_url ? update : latestUpdate;
  if (!candidate?.download_url) throw new Error("没有可下载的更新。");
  const parsed = new URL(candidate.download_url);
  if (parsed.protocol !== "https:" && parsed.hostname !== "127.0.0.1" && parsed.hostname !== "localhost") {
    throw new Error("更新地址必须使用HTTPS。");
  }
  const response = await net.fetch(candidate.download_url);
  if (!response.ok) throw new Error(`更新下载失败：HTTP ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  const hash = crypto.createHash("sha256").update(bytes).digest("hex");
  if (candidate.sha256 && hash.toLowerCase() !== String(candidate.sha256).toLowerCase()) {
    throw new Error("更新安装包校验失败。");
  }
  const filename = path.basename(parsed.pathname) || `WindTowerCAE-${candidate.version}.exe`;
  const target = path.join(app.getPath("downloads"), filename);
  fs.writeFileSync(target, bytes);
  await shell.showItemInFolder(target);
  return { downloaded: true, path: target, sha256: hash };
}

function compareVersions(left, right) {
  const a = String(left).split(".").map((item) => Number.parseInt(item, 10) || 0);
  const b = String(right).split(".").map((item) => Number.parseInt(item, 10) || 0);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    if ((a[index] || 0) !== (b[index] || 0)) return (a[index] || 0) > (b[index] || 0) ? 1 : -1;
  }
  return 0;
}

function showFatalStartupError(error) {
  dialog.showErrorBox("风电塔筒CAE启动失败", `${error?.message || String(error)}\n\n日志目录：${runtimePaths.logs}`);
  quitting = true;
  app.quit();
}
