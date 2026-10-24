# ADR-0001: 目标 MUMmer 4.x，而不是 MUMmer3

- **状态**：Accepted
- **日期**：2026-10-22
- **决策者**：gseek 维护者
- **相关**：`../adapter-matrix.md` §4、`../research.md` §2.2、`../../AGENTS.md` §5

## Context

`gseek` 的 MUMmer 适配器来自 gmlst 的 `nucmer` 后端。需要确认目标版本是 MUMmer3 还是
MUMmer4，因为两者在以下方面不同：

- CLI 参数与版本探测方式；
- `show-coords` 的列数/表头；
- `nucmer` 的 delta 格式与默认过滤行为；
- `mummer4` 与 `mummer` 两个 conda 包名。

三条证据：

1. gmlst 的 pixi 依赖明确写的是 `mummer4 = ">=4.0"`（`gmlst/pixi.toml:30`）。
2. 当前 gmlst 测试环境的 conda 包为：
   `mummer4-4.0.1-pl5321h9948957_0`（`conda-meta/`）。
3. 实际二进制输出：

   ```text
   $ nucmer --version
   4.0.1
   $ mummer --version
   4.0.1
   ```

   而 `show-coords -rcl -T` 的 MUMmer4 输出为 13 列，表头是 `[TAGS]` 后接
   `TAGR`/`TAGQ` 两个数据列。

gmlst 的文档也明确写的是 “MUMmer4 / nucmer”：`gmlst/docs/en/backends.md` 与
`gmlst/docs/en/installation.md` 都使用 `mummer4` 安装方式。

## Decision

`gseek` 的 MUMmer 适配器**只以 MUMmer 4.x 为目标**：

- 版本探测：`nucmer --version`，解析 `(\d+)\.(\d+)\.(\d+)`。
- 最低版本：`(4, 0, 0)`；CI 以 4.0.1 为基线。
- 比对：`nucmer --maxmatch -c 20 -l 20 -p <prefix> <target> <query>`。
- 解析：`show-coords -rcl -T <prefix>.delta`，按 13 列解析。
- 反向命中：`S2 > E2` → `strand="-"`；不得硬编码 `"+"`。
- MUMmer3 不在 1.0 支持范围内。

## Consequences

正面：

- 只维护一套参数/输出解析，减少“按版本猜格式”的分支。
- 与 gmlst 的现有行为一致，便于 parity 迁移。
- `show-coords` 的列语义明确，可正确恢复反向 strand。

负面/代价：

- 使用 MUMmer3 的用户无法直接使用 `mummer` 适配器；需要升级到 MUMmer4，或等待独立的
  `mummer3` 适配器。
- 某些 Linux 发行版的 `mummer` 包仍是 3.x；安装检查必须给出明确提示：
  “需要 MUMmer 4.x（conda-forge/bioconda 包名 mummer4）”。

## Alternatives considered

1. **同时支持 3.x 和 4.x**：需要两套解析器和版本分支；首版没有真实 3.x 测试环境，
   容易写出“看起来兼容但未验证”的代码。拒绝。
2. **只支持 `mummer` 精确匹配**：不满足 gmlst 的 nucmer 语义（gapped/反向/坐标）。
   拒绝。
3. **把 `show-coords` 输出当自由文本解析**：无法稳定恢复列语义与 strand。拒绝。

## Verification

- 单元测试（M2 实现时）：
  - `ToolRequirement` 的 `min_version == (4, 0, 0)`；
  - `nucmer --version` 输出解析；
  - `show-coords` 正向/反向 fixture；
  - MUMmer3 风格输出必须被明确拒绝，而不是静默误解析。
- 集成测试：使用 MUMmer 4.0.1 跑一个含反向命中的小样本。
