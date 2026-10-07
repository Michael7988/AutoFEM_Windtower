# LLM API 自动路由

框架默认使用 `QWEN_PROVIDER_PROFILE=auto`，根据每次请求的实际模型 ID 选择连接：

| 模型 ID | 通道 | API 基址 | 密钥变量 |
|---|---|---|---|
| `qwen*`、`qwq*` | 阿里云百炼官方 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `QWEN_OFFICIAL_API_KEY` |
| `deepseek*` | DeepSeek 官方 | `https://api.deepseek.com` | `DEEPSEEK_OFFICIAL_API_KEY` |
| 其他模型（GPT、Claude、Gemini 等） | N1N 中继 | `https://api.n1n.ai/v1` | `QWEN_N1N_API_KEY`，兼容旧变量 `QWEN_API_KEY` |

当前默认模型为 `qwen3.8-max`，因此默认请求走百炼官方 API。要将两个角色都切换到 DeepSeek 官方，可设置：

```dotenv
QWEN_MODEL=deepseek-v4-pro
QWEN_ENGINEERING_MODEL=deepseek-v4-pro
QWEN_FEM_MODEL=deepseek-v4-pro
```

也可以让两个角色使用不同模型；provider 会在每次请求前按实际 model ID 独立路由。例如：

```dotenv
QWEN_ENGINEERING_MODEL=qwen3.8-max
QWEN_FEM_MODEL=deepseek-v4-pro
```

受控实验可将 `QWEN_PROVIDER_PROFILE` 显式设为 `official`、`deepseek_official` 或 `n1n`，强制所有请求使用指定通道。`legacy` 保留原 `QWEN_API_KEY` + `QWEN_BASE_URL` 行为。

API 密钥只应保存在本地 `.env` 或进程环境变量中，不得写入 YAML、请求记录、实验报告或版本库。
