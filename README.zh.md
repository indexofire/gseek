# gseek

[![PyPI](https://img.shields.io/pypi/v/gseek.svg)](https://pypi.org/project/gseek/)
[![CI](https://github.com/indexofire/gseek/actions/workflows/ci.yml/badge.svg)](https://github.com/indexofire/gseek/actions)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

[English](README.md) | **简体中文**

`gseek` 是一个**零运行时依赖**的 Python 库，为 DNA 比对与距离工具提供稳定、可复现的统一接口。它规范化 BLAST+、minimap2、MUMmer4、KMA、BWA、mash、sourmash 与 skani 的请求和结果，并提供内容寻址索引与可回放 manifest。

它是 [gside](https://github.com/indexofire/gside)、[gapit](https://github.com/indexofire/gapit)、[gmlst](https://github.com/indexofire/gmlst) 共用的比对层。适配器只负责规划命令与无损规范化；生物学解释留在消费者。

```text
consumer (gside / gapit / gmlst)
              │
              ▼
      gseek.api: align / distance / backends
              │
              ▼
 runner: probe → index → plan → execute → parse → manifest
              │
              ▼
 adapters: blast · minimap2 · mummer · kma · bwa · mash · sourmash · skani
```

## 为什么需要 gseek

生信工具解决相似问题，但约定互不兼容：

| 关注点 | 消费者常见做法 | gseek |
| --- | --- | --- |
| BLAST 列 | 每个项目不同的 `outfmt` | 统一 `AlignmentRecord` |
| 坐标 | 1-based inclusive / 0-based half-open 混用 | 0-based half-open + 独立 strand |
| identity / coverage | 工具特定公式 | 归一化值 + 来源 |
| 索引复用 | mtime / 文件存在性 | 内容寻址 key + 锁 |
| 可复现 | 部分版本记录 | `gseek.run/1` manifest + replay |

库不判断一个 hit 是否“好”；它保留证据，让消费者应用阈值和业务规则。

## 状态

`0.9.2`；Python `>=3.12`；运行时依赖：**无**。

| 后端 | 输入 | 操作 | 索引 |
| --- | --- | --- | --- |
| `blast` | FASTA（DNA/蛋白任务） | alignment | target |
| `minimap2` | FASTA / FASTQ / paired | alignment | target |
| `kma` | FASTA / FASTQ / paired | alignment | target |
| `mummer` | FASTA | alignment（MUMmer 4） | 无 |
| `bwa` | FASTA / FASTQ / paired | alignment | target |
| `mash` | FASTA / FASTQ | distance / screen | sketch |
| `sourmash` | FASTA / FASTQ | distance / search / gather | sketch |
| `skani` | FASTA | ANI distance | sketch |

外部工具需另行安装（conda、Bioconda、pixi 或系统包）；`gseek` 运行时发现工具，缺失或版本过旧时给出可操作错误。

## 安装

```bash
pip install gseek==0.9.2
```

源码开发：

```bash
pip install -e ".[dev]"
pixi install
```

外部工具：

```bash
pixi add blast minimap2 mummer4 kma bwa mash sourmash skani
# 或
conda install -c bioconda blast minimap2 mummer4 kma bwa mash sourmash skani
```

Python 包本身没有第三方运行时依赖。

## Python 快速开始

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

print(result.stats.record_count)
for hit in result.records:
    print(hit.query_id, hit.target_id, hit.identity, hit.target_coverage)

best = result.best_per_query_target()
```

查看能力与工具：

```python
gseek.backends()
gseek.get_backend("blast").probe()
gseek.align(...)
gseek.distance(...)
gseek.replay(Path("run.manifest.json"), check=True)
gseek.verify(Path("run.manifest.json"))
```

### 距离与 ANI

```python
result = gseek.distance(
    backend="mash",
    query=Path("assembly.fna"),
    reference=Path("refs.msh"),
    external_reference=True,
    params={"max_distance": 1.0},
    manifest=Path("runs/mash.manifest.json"),
)
```

`distance` 支持 mash、sourmash、skani。`external_reference=True` 表示 reference 已是构建好的 sketch/数据库，而不是待建索引的 FASTA。

## 结果模型

`gseek.align()` 返回 `AlignmentResult`，包含规范化 `AlignmentRecord`、所用索引/工具引用以及可选 manifest 数据。

关键 provenance 字段：

- `query_id`、`target_id`
- `query_start` / `query_end`、`target_start` / `target_end`（0-based half-open）
- `strand`
- `identity`、`aligned_length`、`matches`、`gap_count`、`gap_openings`
- `target_coverage`、`query_coverage`
- `evalue`、`bit_score`、`mapq`、`depth`
- `tags`（工具特有值，如 `NM`、`cs`）

`AlignmentResult` 还提供显式视图：`best_per_query_target()`、`summarize_by_target()`、`iter_records()`。视图不会丢弃原始记录。

`gseek.distance()` 返回 `DistanceResult`；记录保留 query、reference、distance 以及对应后端适用的 p-value/ANI 字段。

## CLI

CLI 是公共 API 的薄封装：解析 argv、输出 JSON、映射退出码；绝不执行 shell 字符串。

```bash
gseek doctor
gseek backends --json

gseek align \
  --backend minimap2 \
  --query sample.fna \
  --target scheme/alleles.fna \
  --preset asm5 \
  --threads 8 \
  --manifest runs/sample.manifest.json \
  -o runs/sample.json

gseek verify runs/sample.manifest.json
gseek replay runs/sample.manifest.json --check --json
```

结果写 stdout（或 `-o`），错误默认写 stderr 的 `gseek.error/1` JSON；人类可读格式用 `--error-format text`。退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `2` | 用法/配置错误 |
| `3` | 工具缺失或版本不支持 |
| `4` | 执行/索引/manifest 错误 |
| `5` | 输入/解析错误 |
| `6` | replay 哈希不一致 |

## 可复现

- 索引内容寻址：适配器版本、语义参数、target 内容哈希和工具版本都参与 key。
- `gseek.run/1` manifest 记录输入、工具版本、规划命令以及 canonical recipe/result hash。
- `gseek verify` 校验记录文件与哈希。
- `gseek replay` 从 manifest 重新规划，并可比较新的 `result_hash`；**不会**重新执行 JSON 中保存的任意 argv。

## 消费者兼容层

`gseek.compat` 统一 `legacy | gseek | shadow` 切换：

```python
from gseek.compat import Engine

switch = Engine.from_env("GSIDE_ENGINE")
result = switch.run(legacy_call, gseek_call, compare=compare)
```

- `legacy`（默认）：只运行消费者已有实现。
- `gseek`：只运行 gseek 路径；失败向上传播。
- `shadow`：同时运行、比较/记录，并返回 legacy 结果。

消费者保留自己的业务规则和 legacy 代码；`gseek.compat` 只负责模式解析、shadow 执行和回退。

## 文档

- [文档站点](https://indexofire.github.io/gseek/zh/)
- [文档首页](docs/index.zh.md)
- [教程](docs/tutorial.zh.md)
- [快速开始](docs/quickstart.zh.md)
- [CLI 参考](docs/cli.zh.md)
- [错误模型](docs/errors.zh.md)
- [API 参考](docs/api.zh.md)
- [English README](README.md)
- [English docs](docs/index.md)

开发文档：

- [开发计划](dev/development-plan.md)
- [适配器矩阵](dev/adapter-matrix.md)
- [兼容性与迁移](dev/compatibility.md)
- [测试与贡献规则](AGENTS.md)
- [变更记录](CHANGELOG.md)

## 开发

项目强制 TDD。

```bash
pixi install
pixi run test          # 单元与契约测试
pixi run test-cov      # 覆盖率门槛
pixi run lint          # ruff check + format --check
pixi run typecheck     # pyright
pixi run check         # lint + typecheck + test
```

集成测试需要外部工具；缺少二进制时跳过。当前 **350 个测试通过**（无工具时 36 个集成测试跳过），覆盖适配器规划/解析、执行、索引、manifest、replay 与消费者 parity。

## 设计原则

1. 适配器是纯规划器/解析器；只有 `execution.py` 启动进程。
2. 不丢信息；归并是显式视图。
3. 坐标 0-based half-open；strand 独立字段。
4. identity/coverage 带来源和公式。
5. 索引内容寻址；不用 mtime 决策。
6. 默认确定性：稳定排序、绝对路径、`LC_ALL=C`、无 shell。
7. 零运行时依赖；类型化错误与稳定 code。
8. 文档与 schema 是契约，随代码演进。

## 许可证

MIT。见 [LICENSE](LICENSE)。
