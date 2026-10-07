# WindTower CAE 桌面版

桌面版复用现有 React UI 和 FastAPI 后端。Electron 只负责本地进程、
安全存储、文件选择、托盘和更新，不承载工程建模规则。

## 开发运行

环境要求：

- Node.js 22
- 当前后端可用的 Python 环境
- Abaqus 可选；未安装时仍可解析、预览和生成 INP

```powershell
cd core\windtower-modeling-ui
npm install
npm run desktop:dev
```

Electron 会自动启动后端并选择空闲端口。不要另外启动 8000 端口服务。
项目数据写入 `%LOCALAPPDATA%\WindTowerCAE`，不会写入安装目录。

## 生成安装包

安装桌面后端依赖：

```powershell
python -m pip install -r ..\windtower_backend\requirements-desktop.txt
```

生成可执行目录或 Windows 安装程序：

```powershell
npm run desktop:package
npm run desktop:make
```

产物位于 `out`。发布前应在未安装 Python/Node.js 的干净 Windows x64
虚拟机中验证启动、资料解析、INP生成和 Abaqus 检测。

## 团队更新

设置环境变量 `WINDTOWER_UPDATE_MANIFEST_URL` 指向 HTTPS JSON：

```json
{
  "version": "0.2.1",
  "notes": "更新说明",
  "download_url": "https://intranet.example/WindTowerCAE-Setup.exe",
  "sha256": "安装包SHA256"
}
```

软件只提示并下载更新，不会在 Abaqus 运行期间自动安装。团队发布版本应使用
Windows 代码签名证书签名；证书和更新服务器凭据不得提交到代码库。

## 数据和安全

- Qwen API Key 由 Electron `safeStorage` 加密，Windows 下使用 DPAPI。
- 后端仅监听 `127.0.0.1`，并要求每次启动生成的临时令牌。
- 升级只替换程序文件；项目、ODB、提交记录和配置保留在用户目录。
- 关闭窗口时如有活动求解，程序进入托盘；明确退出才会中断任务。
