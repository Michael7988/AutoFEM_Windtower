# Synthetic fixtures

此目录只包含可公开、完全合成且确定性生成的测试输入和期望结果，不包含真实工程数据。

- `synthetic_standard_tower.xlsx`：10 段标准工作簿。
- `synthetic_mechanical_interface_FEM.xls`：机械接口兼容样例。
- `synthetic_l0_ir.json`：固定项目标识和清理后元数据的 Canonical L0 IR。
- `synthetic_l0_static.inp`：由同一 IR 编译的语义回归 INP。
- `llm_benchmark/`：S01–S10 合成 benchmark。
- `fixture_manifest.json`：生成参数、随机种子和 SHA256。

重新生成后必须确认两次运行得到相同哈希，并同步更新依赖这些 fixtures 的契约测试。
