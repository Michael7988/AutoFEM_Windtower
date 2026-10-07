# AutoFEM Wind Tower Backend v1

面向带门洞钢制风电塔筒 S4R 壳模型的参数化有限元自动建模后端。

## v1 边界

系统采用四个宏观阶段，在桌面端展开为以下五个操作步骤：

1. 工程资料解析与受限工程参数候选映射；
2. 模型库、可选参考 INP 与 FEM 规范候选映射；
3. 工程师双域独立确认与确定性合并；
4. 版本化 `ParameterSet v2` 驱动服务器端确定性网格和 INP 生成；
5. Datacheck、显式求解、ODB 检查和参数化迭代。

LLM 不生成正式参数集、节点、单元、Abaqus 关键词或 INP。参数修改必须经过“预览差异 → 工程师确认 → revision 增加 → 完整重新编译”。

## 参考 INP 复用规则

- 可复用：S4R、Shell Section、线弹性材料、网格尺寸统计、边界语义、受支持的载荷传递、静力/模态和输出规范。
- 需确认：荷载实现、语义集合重建、局部网格意图及求解控制；项目荷载数值不会从旧模型自动迁移。
- 仅记录：原节点与单元拓扑、任意装配、接触、预紧、复杂连接、非线性材料和扩展分析。
- `NLGEOM=YES` 等超出当前线弹性能力的设置会进入 `deferred_features`，不会写入正式 `ParameterSet` 或新 INP。

公开输入支持 Excel、CSV、JSON、YAML、TXT、DOCX 和文字层 PDF。图片、PPTX、扫描 PDF 与 OCR 不属于当前研究输入范围。只填写建模目标也可创建准备批次，但所有非观测建议值均需工程师确认。

## 模块

```text
windtower_backend/
├── api/                 # v1 HTTP 接口与输入批次
├── services/            # 参数、生成和验证服务
├── parameter_ir.py      # 唯一正式参数契约
├── reference_models.py  # 参考 INP 语义摘要与 FEM 规范候选
├── model_library.py     # 工程师发布的不可变验证模型版本
├── knowledge.py         # 工程/FEM 双本地知识索引
├── input_batches/       # 异构输入提取、冲突和确认
├── modeling/            # 项目状态、能力配置与风塔结构族实现
├── l0/                  # S4R 网格、INP、Abaqus 和 ODB
├── qwen_client/         # 受限候选映射与审查
└── benchmark/           # G1—G6 实验
```

## 启动

```powershell
python -m pip install -r core\windtower_backend\requirements.txt
python -m uvicorn windtower_backend.app:app --app-dir core --host 127.0.0.1 --port 8000
```

健康检查：`GET http://127.0.0.1:8000/api/health`

## 质量门禁

```powershell
python -m pytest -q
python -m ruff check core tests
python -m mypy
cd core\windtower-modeling-ui
npm test
npm run build
```

真实 Qwen 与 Abaqus 执行默认关闭，必须通过配置和显式请求开启。运行数据写入本地数据目录，不应提交到 Git。
