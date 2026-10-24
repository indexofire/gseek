# gseek

[![PyPI](https://img.shields.io/pypi/v/gseek.svg)](https://pypi.org/project/gseek/)

**Unified, reproducible DNA alignment infrastructure for Python ≥ 3.12.**

`gseek` 把 BLAST+、minimap2、MUMmer、KMA、BWA、mash/sourmash 等外部 DNA 比对工具封装成一层稳定 API：统一请求、统一结果模型、内容寻址索引、可复现的 run manifest。它面向 [gside](https://github.com/indexofire/gside)、[gapit](https://github.com/indexofire/gapit)、[gmlst](https://github.com/indexofire/gmlst) 这类“需要反复做 DNA 比对”的基因组工具，避免每个项目重复实现命令拼装、输出解析、缓存和版本记录。

> **状态：M0 基座 + M1–M4 适配器（0.5.0 alpha，已发布 PyPI）。** BLAST+（核酸/蛋白）、minimap2、KMA、MUMmer4、BWA、mash、sourmash、skani 已可用；replay 已落地。真实输出 golden fixtures、8 后端 replay 矩阵与 gside 迁移分支已验证；剩余工作是 gmlst/gapit 迁移。

### 当前进度

- [x] 工程骨架：`pyproject.toml`、`src/` 布局、pixi、CI、MIT。
- [x] `gseek.model`：`AlignmentRecord` / `AlignmentResult` / `DistanceResult` / `RunManifest`。
- [x] `gseek.execution`：无 shell、绝对路径、超时、流式、stderr 排空、版本探测。
- [x] `gseek.index`：内容寻址、文件锁、staging + 原子发布、GC。
- [x] `gseek.provenance`：SHA-256、canonical JSON、recipe hash、manifest verify。
- [x] `gseek.adapter` + `gseek.registry` + `gseek.runner`：三阶段协议与端到端编排。
- [x] `gseek.api` 与 `gseek` CLI：align/distance/backends/doctor/index/manifest。
- [x] 项目更名 **gseek**（原 gcore 名称冲突）；身份契约测试先红后绿。
- [x] 工程规范：`AGENTS.md`（强制 TDD）、`CONTRIBUTING.md`、`.editorconfig`、pre-commit、ADR。
- [x] MUMmer 版本确认：**MUMmer 4.x**（测试基线 4.0.1，ADR-0001）。
- [x] M1：BLAST+（blastn）与 minimap2 适配器；真实工具集成测试通过。
- [x] M2：KMA（FASTA/FASTQ/paired）与 MUMmer4（nucmer/show-coords、反向 strand）；真实工具集成测试通过。
- [x] M3：BWA（SAM）、mash（sketch/dist/screen）、sourmash（sketch/search）；真实工具集成测试通过。
- [x] M4：BLAST 蛋白任务（blastp/blastx/tblastn/tblastx）、sourmash gather、skani ANI、manifest replay。
- [x] 真实工具 golden fixtures、gside/gapit/gmlst 字段 parity、8 后端 replay 矩阵。
- [x] 324 个单元/契约测试 + 36 个集成测试（无工具时 skip）；覆盖率 88%；ruff + pyright 全绿。

## 为什么需要 gseek

三个现有工具各自维护了一套比对层，且语义并不一致：

| 问题 | 现状 |
| --- | --- |
| BLAST outfmt | gmlst 12 列、gside 14 列、gapit 15 列 |
| coverage | 三种公式 |
| minimap2 identity | 三种公式 |
| 坐标 | 1-based inclusive / 0-based half-open 混用 |
| 索引 | mtime/文件存在性判断，缺少并发锁 |
| 可复现 | 只有 gapit 建库时记录版本，没有单次运行 manifest |

`gseek` 的设计目标是：**适配器只负责“无损规范化”，执行层负责“可复现”，业务结论留在消费者。**

## 安装

```bash
# PyPI
pip install gseek==0.5.0

# 本地 wheel（发布物验证）
uv pip install dist/gseek-0.5.0-py3-none-any.whl

# 源码开发模式
pip install -e ".[dev]"

# 仓库环境
pixi install
```

> **命名说明**：项目原名 `gcore`，因 PyPI 上的 `gcore` 已被 [Gcore Python API](https://pypi.org/project/gcore/) 占用，于 2026-10-22 更名为 **gseek**。PyPI 分发名、导入名和 CLI 均为 `gseek`；0.5.0 已发布。决策记录见 [ADR-0002](dev/decisions/0002-project-rename.md)。
>
> **稳定性**：0.5.0 仍是 alpha；1.0 前公共 API 可能调整。生产集成请钉住
> `gseek==0.5.0`，并通过 `GSIDE_ENGINE`/feature flag 保留旧路径。

外部工具不随包安装，按需通过 conda/pixi/系统包管理器安装：

```bash
pixi add blast minimap2 mummer4 kma bwa mash sourmash skani
# 或
conda install -c bioconda blast minimap2 mummer4 kma bwa mash sourmash skani
```

## 快速预览（M1–M4 可用）

```python
from pathlib import Path
import gseek

result = gseek.align(
    backend="minimap2",
    query=Path("sample.fna"),
    target=[Path("scheme/alleles.fna")],
    preset="asm5",
    threads=8,
    index="auto",
    manifest=Path("runs/sample.manifest.json"),
)

for hit in result.records:
    print(hit.query_id, hit.target_id, hit.identity, hit.target_coverage)

summary = result.summarize_by_target()
best = result.best_per_query_target()
```

更多 API：

```python
gseek.backends()                       # 能力矩阵
gseek.get_backend("blast").probe()     # 工具/版本探测
gseek.distance(...)                    # mash/sourmash/skani 距离/ANI
gseek.replay(Path("run.manifest.json"), check=True)
gseek.verify(Path("run.manifest.json"))
```

## CLI 快速开始

```bash
# 先确认工具是否齐全；缺失时返回 1
gseek doctor

# 比对，并写出可 verify/replay 的 manifest
gseek align \
  --backend minimap2 \
  --query sample.fna \
  --target scheme/alleles.fna \
  --preset asm5 \
  --threads 8 \
  --manifest runs/sample.manifest.json \
  -o runs/sample.json

# 校验输入/工具/结果哈希
gseek verify runs/sample.manifest.json

# 重新规划并校验 result_hash；不会执行 manifest 里的旧 argv
gseek replay runs/sample.manifest.json --check --json
```

CLI 约定：结果写 stdout，错误写 stderr，默认错误是 `gseek.error/1` JSON；
`--error-format text` 可切换人类可读格式。详见 [CLI](docs/cli.md)。

## 架构

```text
┌────────────────────────────────────────────────────────────┐
│ gside / gapit / gmlst / CLI                                │
└──────────────────────────┬─────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────┐
│ gseek.api: align / distance / backends                     │
└──────────────────────────┬─────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────┐
│ runner: probe → index → plan → execute → parse → manifest  │
└───────┬──────────────────────┬──────────────────────┬──────┘
        ▼                      ▼                      ▼
┌──────────────┐      ┌────────────────┐     ┌────────────────┐
│  adapters    │      │   execution    │     │   index store  │
│ blast        │      │ command plan   │     │ content hash   │
│ minimap2     │      │ subprocess     │     │ lock + atomic  │
│ mummer4/kma  │      │ timeouts       │     │ GC             │
│ bwa/minhash  │      │ env/streaming  │     └────────────────┘
└───────┬──────┘      └───────┬────────┘
        └─────────────────────┴──────────────┐
                                             ▼
                              ┌────────────────────────────┐
                              │ result model + provenance  │
                              │ records / manifest / views │
                              └────────────────────────────┘
```

## 文档

- [文档首页](docs/index.md)：按角色导航。
- [Quickstart](docs/quickstart.md)：安装、第一个 CLI/Python 调用、manifest/replay。
- [CLI](docs/cli.md)：子命令、JSON 输出、退出码、自动化约定。
- [错误模型](docs/errors.md)：异常层级、`code`/`exit_code`、处理建议。
- [API 参考](docs/api.md)：公共 API、模型、适配器协议、执行/索引/可复现层、schema。
- [开发计划](dev/development-plan.md)：目标、架构、接口、里程碑、风险、开放决策。
- [三工具调研](dev/research.md)：gside/gapit/gmlst 的代码级对比与重复点。
- [适配器矩阵](dev/adapter-matrix.md)：六类工具的命令、索引、解析、版本和坑。
- [兼容性迁移](dev/compatibility.md)：字段映射、影子模式、parity 验收。
- [ADR-0001：MUMmer4](dev/decisions/0001-mummer4.md)：确认目标为 MUMmer 4.x。
- [ADR-0002：更名 gseek](dev/decisions/0002-project-rename.md)：名称冲突与迁移清单。
- [AGENTS.md](AGENTS.md)：TDD、架构边界、验证命令（开发者/agent 必读）。
- [CONTRIBUTING.md](CONTRIBUTING.md)：贡献流程与测试分层。
- [CHANGELOG.md](CHANGELOG.md)：版本变更记录。
- [发布清单](dev/releasing.md)：版本、构建、PyPI 上传与验证。

## 开发

**TDD 是强制的**：先写失败测试，确认失败原因，再写最少实现，最后跑全量门禁。详见
[AGENTS.md](AGENTS.md)。

```bash
pixi run test          # 全量单测
pixi run test-cov      # 覆盖率（fail_under=85）
pixi run lint          # ruff check + format --check
pixi run typecheck     # pyright
pixi run check         # lint + typecheck + test
pixi run pre-commit    # 全部 pre-commit hook
```

或手动：

```bash
PYTHONPATH=src pytest
ruff check . && ruff format --check src tests
PYTHONPATH=src pyright
```

## 设计原则

1. 适配器是纯规划器/解析器，不直接启动子进程。
2. 结果无损；归并是显式视图，不是隐式行为。
3. 坐标统一为 0-based half-open，strand 单独字段。
4. identity/coverage 带来源和公式，不只是一个数字。
5. 索引内容寻址；不依赖 mtime。
6. 默认确定性：稳定排序、`LC_ALL=C`、绝对路径、无 shell。
7. 核心零运行时依赖；外部工具通过 PATH/显式路径发现。
8. 类型化错误 + code + context + hint。
9. 文档即契约：适配器矩阵与 schema 和代码同步。
10. 先迁移、后删除；影子模式护航。

## 许可证

MIT。详见 [LICENSE](LICENSE)。
