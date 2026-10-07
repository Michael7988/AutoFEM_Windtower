# 贡献说明

## v1 边界

- 公共建模接口只有版本化 `ParameterSet`；禁止增加客户端任意 IR/INP patch。
- LLM 输出必须停留在参数候选或受限 `ParameterChange`。
- 正式生成仅支持带门洞风塔 S4R 壳、线弹性静力与模态。
- 新增真实 LLM 或 Abaqus 执行必须默认关闭并要求显式开关。
- 不恢复 trusted compiler、shadow compiler、自治修复、多级哈希或重复 workflow。

## 提交要求

- 不提交真实工程数据、密钥、企业信息、本机绝对路径、ODB、求解日志、缓存或安装包。
- fixtures 必须完全合成、可重复生成，并隔离真值与模型输入。
- API、参数契约、生成器或实验协议变更必须同步测试和文档。

```powershell
python -m pytest -q
python -m ruff check core tests
python -m mypy
cd core\windtower-modeling-ui
npm ci
npm test
npm run build
```

涉及 Abaqus 的改动还应执行最小 Datacheck、静力、模态与 ODB 冒烟测试。
