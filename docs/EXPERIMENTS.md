# G1—G6 实验说明

| 组别 | 内容 | 数量/范围 |
|---|---|---|
| G1 | 参考工程案例 | 参考模型与公开合成替代 |
| G2 | 异构 Excel | S01—S10 |
| G3 | 跨格式输入 | 3 个案例 × 9 种格式 |
| G4 | 异常输入 | 24 个故障注入 |
| G5 | 参数修改 | 18 个任务 |
| G6 | 数值验证 | Datacheck、静力、模态、ODB、网格敏感性 |

生成冻结数据集并运行本地阶段：

```powershell
python -m windtower_backend.automation.run_experiment_campaign `
  --stages G1 G2 G3 G4 G5
```

G6 的网格部分可本地运行；真实 Abaqus 必须显式启用：

```powershell
python -m windtower_backend.automation.run_experiment_campaign `
  --stages G6 --run-abaqus
```

真实 LLM 映射同样要求 `--run-llm`。实验报告记录环境、数据集校验和、campaign ID 和结果表字段；隐藏真值不得进入模型提示或输入。文件校验和仅用于数据集完整性与实验复现，不进入建模业务接口。
