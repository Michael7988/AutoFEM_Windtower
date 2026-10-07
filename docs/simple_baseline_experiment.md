# 四塔筒最简基线实验

入口：

```powershell
python -m core.windtower_backend.automation.run_simple_baseline prepare
python -m core.windtower_backend.automation.run_simple_baseline pilot --case 2 --campaign-root <campaign>
python -m core.windtower_backend.automation.run_simple_baseline freeze --campaign-root <campaign>
python -m core.windtower_backend.automation.run_simple_baseline formal --cases 2,1,3,4 --repeats 2 --campaign-root <campaign>
python -m core.windtower_backend.automation.run_simple_baseline resume --campaign-root <campaign>
python -m core.windtower_backend.automation.run_simple_baseline status --campaign-root <campaign>
python -m core.windtower_backend.automation.run_simple_baseline report --campaign-root <campaign>
```

`prepare` 默认读取 `core0809/example/*_FEM参数.xls`，创建盲化副本、非空单元格证据投影、知识片段、暂定参数真值和人工审核清单。所有真实调用按阶段、案例、方法、重复、角色和尝试号保存请求哈希与原始响应；相同调用不会重复发送，改变已记录请求会被拒绝。

`pilot` 仅接受案例 2。严格 INP 只作为案例 2 的输出可行性试验；四案例正式主比较为 B1-Script 与 Structured。三种路径均先过静态安全门和 Abaqus Datacheck，通过后才求解。失败、空输出、截断、无 INP、Datacheck 失败、求解失败及 ODB 缺失均保留在分母中。

`freeze` 必须在 `truth/ENGINEER_REVIEW_CHECKLIST.csv` 的 8 行中填写 `engineer_decision=accept` 和非空 `reviewer`。审核只允许接受或拒绝，不允许修改数值。冻结封印记录提示词、实现、能力配置和四个输入文件的 SHA-256；封印后若任一哈希改变，`formal` 会拒绝运行。

正式输出位于 `reports/`：

- `comparison_report.json`：方法汇总与逐次运行指标；
- `run_metrics.csv`：可直接用于统计和制图的长表；
- `comparison_report.html`：浏览器报告；
- 各运行目录：INP、Datacheck、求解、ODB、后处理、参数签名及诊断。

案例 2 开发试跑发现的通用修正包括：实际请求体显式设置 `enable_thinking=false`；FEM `boundary.dofs` 按 JSON Schema 确定性归一化；门洞符号来源与具体单元格定位建立确定性对应；直接脚本必须同时写出固定名称的静力和模态 INP。
