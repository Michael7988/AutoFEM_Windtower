# L2 依赖感知受控有限元自动化

## 已实现边界

平台保留 `WT-SHELL-DOOR-STATIC-MODAL-V1` 的 L1 行为，并新增
`WT-FLANGE-BOLT-CONTACT-PRELOAD-V1`。L2 仅覆盖上下局部塔节、单个法兰、
S4R 壳、B31 梁螺栓、法兰面接触以及 preload → hold → service 三步非线性静力分析；
不扩展为整塔多法兰或实体螺栓。

L2 使用 `fe_semantic_ir_v1` 分离工程域、FEM 域和 Abaqus 映射。稳定对象 ID 及依赖边
在编译前完成拓扑检查，LLM 只能提出 Canonical 参数或语义对象补丁，不得生成节点、
单元、关键字、INP 或求解命令。确定性编译器支持 `native_pretension` 和
`equivalent_end_force` 两个共享几何/接触定义的预紧分支。

主要接口：

- `GET /api/capabilities`
- `GET /api/projects/{id}/fe-semantic-ir`
- `GET /api/projects/{id}/dependency-report`
- `POST /api/projects/{id}/repairs/preview`
- `GET /api/projects/{id}/workflow-state`

## 门禁与论文真值

统一门禁顺序为：参数确认 → 依赖图 → 几何/网格 → 集合/表面/耦合 → 接触/预紧语义
→ INP → Datacheck → Solve → 数值验证。结果报告会检查预紧误差、力/矩平衡、接触量、
cutback、严重不连续和负特征值。任何未解释的负特征值、目标预紧误差超过 2% 或平衡
误差超过 1% 都会令 `formal_reference_eligible=false`。

`core/example/ALFF02L2V3.inp` 及其历史求解产物只作为带哈希的专家探索基线。历史记录中
三步求解完成但含 192 个负特征值警告，因此不能作为“无警告真值”。当前确定性原生预紧
模型可以通过 Abaqus Datacheck；真实求解仍会因接触/负特征值和增量收敛阻断。该状态必须
作为待完成的数值校准任务报告，不能被 HTTP 成功或 INP 生成成功掩盖。

## 冻结实验入口

`windtower_backend.benchmark.controlled_fe_l2` 固化 B0/B1/B2/B3、8 类依赖语义任务（每类
3 个改写）、12 类故障注入、六级成功漏斗及运行哈希。正式实验前还需冻结 Provider、模型、
温度、提示词、注册表、知识库、Abaqus 环境和参考结果；历史试跑与工程师改代码后的结果
不得混入正式统计。

## 当前验证基线

- 后端：292 passed，1 skipped。
- 前端：19 passed；TypeScript 与 Vite production build 通过。
- Ruff：全后端与测试通过。
- Mypy：本轮新增 capability、双域 IR、L2 compiler/extractor/postprocess 和实验协议通过；
  历史代码库仍有独立的类型债务，不在本轮做大范围重构。
