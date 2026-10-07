# AutoFEM WindTower Desktop

React、TypeScript、Vite 与 Electron 桌面端，与 GitHub v1 四阶段方法一致，并展开为五个操作步骤：

1. 工程文件上传与工程参数候选映射；
2. 可选参考 INP、FEM 规范候选和工程师确认；
3. 已确认 `ParameterSet v2` 审查与自然语言修改；
4. 服务器端 S4R 网格和 INP 确定性生成；
5. 显式 Datacheck、求解、状态轮询与结果读取。

前端不提交任意 IR、Abaqus 关键词或 INP patch，也不保存 LLM 密钥。

桌面端采用单页三栏工程工作台：

- 左侧展示工程顾问、FEM 顾问、双知识库状态，并支持已验证模型、临时参考 INP 或能力配置三种 FEM 来源；
- 中间显示 PyVista 生成的真实 S4R 网格或 ODB 云图，支持三维、正视、侧视、俯视以及整体/门洞局部切换；
- 右上在项目创建前分别确认工程/FEM 参数，创建后统一处理自然语言修改和直接参数编辑；右下执行构建、模型检查、求解、模型库发布、结果与核心文件操作；
- 页面本身不产生长滚动；低于 1280 px 时左右栏切换为抽屉。
- 批次和求解状态使用统一防重入轮询；终态、项目切换或页面卸载时自动停止旧任务更新。

```powershell
npm ci
npm run dev
npm test
npm run build
```

默认通过 Vite 将 `/api` 代理到 `http://127.0.0.1:8000`。桌面打包说明见 `DESKTOP.md`。
