# AutoFEM WindTower v1

面向参数化工程结构的 LLM 辅助有限元自动建模研究实现。GitHub v1 仅覆盖**带门洞钢制风电塔筒整体 S4R 壳模型、线弹性静力与模态分析**。

> 研究软件，不用于结构设计审批或安全结论。采用 MIT License，详见仓库根目录 `LICENSE`。

## 方法边界

```text
工程信息映射 ──→ 工程参数候选 ──┐
                                  ├→ 工程师确认 → ParameterSet v2
模型库/参考 INP/能力配置 ─→ FEM规范候选 ─┘
                                              ↓
版本化 ParameterSet（唯一正式建模接口）
              ↓
风塔 S4R 适配器确定性生成网格与 INP
              ↓
Abaqus Datacheck、求解、ODB 与参数化迭代
```

- LLM 只能输出参数候选或受限 `ParameterChange`，不能输出正式参数集、节点、单元、Abaqus 关键词或 INP。
- 参考 INP 只用于提取单元、截面、网格、边界和分析步规范；上传文件不会被直接执行，也不会复制其节点和单元。
- 工程信息顾问与 FEM 规范顾问职责分离，所有候选统一由确定性协调器执行能力匹配、冲突检查和人工确认。
- 两个顾问可分别配置 Qwen 模型、温度、Token、超时、提示版本和本地 FTS5 知识库；知识片段只产生带引用建议。
- 只有工程师显式发布且保存验证等级的模型才能进入私有模型库；复用的是参数与 FEM 规范，不是旧拓扑或 INP 文本。
- 候选参数经过单位归一、类型/范围/依赖检查、冲突处理和工程师确认后才能提升。
- 参数修改采用“预览差异 → 确认 → revision 增加 → 完整重编译”，不直接修改既有 INP。
- 保留来源引用、参数门禁、显式求解、任务隔离、审计事件和实验环境清单。

## 目录

```text
core/windtower_backend/       FastAPI、参数层、S4R 生成与 Abaqus 服务
core/windtower-modeling-ui/   React/Electron 四阶段桌面端
tests/fixtures/               完全合成的公开测试数据
docs/ARCHITECTURE.md          v1 架构与公共 API
docs/EXPERIMENTS.md           G1—G6 实验协议
```

## 安装与运行

论文基准环境为 Windows、Python 3.8 和本地 Abaqus：

```powershell
py -3.8 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-lock-py38.txt
python -m pip install -r requirements-quality.txt
python -m pip install -e .
python -m uvicorn windtower_backend.api:app --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd core\windtower-modeling-ui
npm ci
npm test
npm run build
```

## 质量门禁

```powershell
python -m pytest -q
python -m ruff check core tests
python -m mypy core/windtower_backend/api core/windtower_backend/persistence
```

真实 LLM 与 Abaqus 执行默认关闭，必须通过相应显式参数启用。运行数据应通过 `WINDTOWER_DATA_ROOT` 等环境变量放到源码树外；不得提交真实工程输入、密钥、ODB、求解日志或本机绝对路径。

双顾问可使用 `QWEN_ENGINEERING_MODEL`、`QWEN_FEM_MODEL` 及对应的
`*_TEMPERATURE`、`*_MAX_TOKENS`、`*_TIMEOUT`、`*_PROMPT_VERSION` 独立配置。
本地知识库通过 `python -m windtower_backend.automation.index_knowledge_bases engineering <资料目录>`
或将域改为 `fem` 建立；历史模型经工程师审核后可使用
`python -m windtower_backend.automation.import_model_library <parameter_set.json> <reference.inp> --label <名称>` 导入。

## English

AutoFEM WindTower v1 separates engineering-evidence mapping from FEM-specification mapping. An optional project-level reference INP is parsed into advisory modeling metadata only; its topology is never copied or executed. A versioned `ParameterSet` remains the sole formal modeling interface, and the server-side S4R adapter generates the mesh and Abaqus INP deterministically.

The validated scope is limited to linear static and modal analyses. Nonlinear analysis, buckling, contact, autonomous repair, direct LLM-to-INP generation, and cross-family generation are outside v1.

See `docs/ARCHITECTURE.md`, `docs/EXPERIMENTS.md`, `CONTRIBUTING.md`, and `SECURITY.md`.

