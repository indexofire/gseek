# ADR 0003: backend 存在性由记录模型契合度决定

日期：2026-10-26
状态：Accepted

## 背景

消费者（gside）的 meta 分析使用 kraken2(+bracken) 与 sylph。sylph 输出
逐参考基因组的 ANI/coverage（`Adjusted_ANI`/`Eff_cov`），与既有
`DistanceRecord` 语义同构（同 sourmash/skani 家族）；kraken2+bracken 输出
的是样本分类组成表（taxon、reads、fraction），既非比对也非距离。

若只为"每个 gside 工具都有 backend"而包装 kraken2，需要为 gseek 引入
第三个记录家族（如 `gseek.profile/1` + `ProfileAdapter` 协议），核心抽象
为单一工具扩张。

## 决策

1. **规则**：一个工具获得 gseek backend，当且仅当其输出能被既有记录
   家族（`AlignmentRecord` / `DistanceRecord`）忠实承载。契合度决定
   backend 存在性，而非"消费者是否使用该工具"。
2. **sylph**：契合 `DistanceAdapter`（FASTQ 输入、外部预建
   `.syldb/.syl2db` 引用、ANI/coverage 记录）——建立 backend。
   阈值→confidence 标签等业务判定留在消费者。
3. **kraken2/bracken**：暂不建 backend。其输出需要"组成/丰度"记录
   家族，当前只有 1.5 个消费者（kraken2 全量 + sylph 的丰度维度），
   不足以摊销核心扩张成本。
4. **触发条件（重新评估）**：当出现第二个纯组成语义消费者
   （kaiju / centrifuge / metamaps 等）时，重新评估引入
   `ProfileRecord`（`gseek.profile/1`）家族，并按本 ADR 流程修订。
   在此之前，kraken2 的进程卫生需求可在消费者侧复用
   `gseek.execution`（`Executor`/`CommandPlan`）满足，不进记录模型。

## 后果

- `GSIDE_ENGINE=gseek/shadow` 对 meta 模式覆盖 sylph 而不覆盖
  kraken2；该不对称是本 ADR 的显式结论而非遗漏。
- gseek 宪章保持 "alignment/distance 基础设施"；扩张到组成语义
  需要新 ADR。
