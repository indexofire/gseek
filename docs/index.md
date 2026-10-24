# gseek 文档

`gseek` 是一层稳定、可复现的 DNA 比对 API，封装 BLAST+、minimap2、MUMmer4、
KMA、BWA、mash、sourmash 与 skani。适配器只做无损规范化；业务结论留给调用方。

> 当前版本 **0.2.0（alpha）**。PyPI 发布准备中；在此之前请从 wheel 或源码安装。

## 从这里开始

| 文档 | 适合谁 | 内容 |
| --- | --- | --- |
| [Quickstart](quickstart.md) | 第一次使用 | 安装、第一个 CLI/Python 调用、manifest/replay |
| [CLI](cli.md) | 运维与流水线 | 子命令、JSON 输出、退出码、自动化约定 |
| [错误模型](errors.md) | 库调用方 | `GseekError` 层级、`code`/`exit_code`、错误处理 |
| [API 参考](api.md) | 适配器作者/集成方 | 完整签名、结果模型、索引、manifest、schema |

## 设计原则

1. **统一模型**：不同工具的输出规范化为 `AlignmentRecord` / `DistanceRecord`。
2. **不丢原始信息**：保留 NM、alen、identity/coverage 来源；归并是显式视图。
3. **可复现**：内容寻址索引、`gseek.run/1` manifest、`verify`、`replay --check`。
4. **零运行时依赖**：Python ≥ 3.12，stdlib only；外部工具按需安装。
5. **薄 CLI**：CLI 只调用公共 API，不复制业务逻辑，不提供交互式提示。
