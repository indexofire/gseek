# Architecture Decision Records

本目录记录难以回滚、影响多个模块或消费者的决策。格式参考
[Michael Nygard 的 ADR](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)。

| 编号 | 标题 | 状态 | 日期 |
| --- | --- | --- | --- |
| [0001](0001-mummer4.md) | 目标 MUMmer 4.x，而不是 MUMmer3 | Accepted | 2026-10-22 |
| [0002](0002-project-rename.md) | 项目更名为 gseek | Accepted | 2026-10-22 |
| [0003](0003-backend-scope-rule.md) | backend 存在性由记录模型契合度决定（sylph 入、kraken2 缓） | Accepted | 2026-10-26 |

约定：

- 每个 ADR 包含 Context / Decision / Consequences / Alternatives。
- 决策变更不删除旧 ADR；新增 ADR 并将旧 ADR 标记为 Superseded。
- 影响 schema、公共 API、工具版本下限、命名、许可证的变更必须有 ADR。
