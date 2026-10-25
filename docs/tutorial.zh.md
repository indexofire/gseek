# gseek 教程

[English](https://indexofire.github.io/gseek/) | **简体中文**

本教程从第一次比对讲到可复现运行与自定义集成。面向已有基本生信环境、但还没用过
gseek 的 Python 用户。

## 1. gseek 是什么（不是什么）

`gseek` 是一个 Python 库，负责：

- 用 argv 列表规划并运行外部比对/距离工具（绝不经过 shell）；
- 把输出规范化成 `AlignmentRecord` / `DistanceRecord`；
- 管理内容寻址索引；
- 记录可复现 run manifest，并可回放；
- 为消费者提供 `legacy | gseek | shadow` 切换。

它**不**判断 allele 是否有效、物种判定是否正确、基因型/表型是什么；这些是消费者规则。

```text
你的 pipeline
    │  request (query, target, params)
    ▼
 gseek runner
    │  probe → index → plan → execute → parse
    ▼
 records + provenance（可选 manifest）
```

## 2. 安装与检查工具

需要 Python **3.12+**：

```bash
pip install gseek==0.10.0
```

外部工具不是 Python 依赖，按需安装：

```bash
pixi add blast minimap2 mummer4 kma bwa mash sourmash skani
# 或
conda install -c bioconda blast minimap2 mummer4 kma bwa mash sourmash skani
```

检查环境：

```bash
gseek --version
gseek backends
gseek doctor
```

`gseek doctor` 在缺少工具时退出 1。也可以显式指定路径：

```bash
export GSEEK_BLASTN=/opt/bio/bin/blastn
```

## 3. 心智模型

### Request

alignment 请求包含 query、一个或多个 target、适配器与适配器参数。distance 请求
使用 reference 而不是 target。

### Result

`AlignmentResult` 包含：

- 规范化 records（坐标 0-based half-open）；
- inputs/targets 及其哈希；
- 适配器与工具版本；
- 可选 index/manifest 引用；
- 聚合统计。

### Index

索引 key 由适配器版本、语义参数、target 内容哈希和工具版本共同决定。
`index="auto"` 复用匹配索引或构建新索引；`index="none"` 在适配器支持 subject
模式时跳过持久索引。

### Manifest

manifest 记录实际发生了什么：输入、工具、索引、命令、参数、结果哈希以及
allowlist 环境。`verify` 校验记录文件；`replay` 重新规划并可比较结果哈希。

## 4. 第一次比对

假设有样本组装和 target FASTA：

```text
sample.fna
scheme/alleles.fna
```

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

print("records:", len(result.records))
print("backend:", result.backend)
print("index key:", result.index.key if result.index else None)
```

内部流程：

1. gseek 检测 FASTA 输入。
2. 探测 `minimap2` 并选择适配器。
3. `index="auto"` 解析内容寻址 target 索引。
4. 适配器规划命令，执行器运行。
5. 解析器输出规范化 `AlignmentRecord`。
6. record 按稳定顺序排序。
7. 如请求则写 manifest。

## 5. 查看 records 与视图

```python
for hit in result.records[:5]:
    print(
        hit.query_id,
        hit.target_id,
        hit.identity,
        hit.alignment_length,
        hit.target_coverage,
        hit.strand,
    )

best = result.best_per_query_target()
primary = result.primary_only()
summary = result.summarize_by_target()
```

重要约定：

- 坐标是正向序列上的 0-based half-open。
- `strand` 独立（`+` / `-`）。
- `identity` 是百分比；coverage 是 0-1 分数。
- `tags` 保留工具证据，如 `NM`、`identity_method`。

视图是显式的，不删除原始 records。需要其他筛选规则时，请直接过滤
`result.records`。

## 6. FASTQ 与双端 reads

```python
result = gseek.align(
    backend="kma",
    query=(Path("sample_R1.fq"), Path("sample_R2.fq")),
    target=[Path("scheme/alleles.fna")],
    paired=True,
    threads=8,
    index="auto",
)
```

只有支持 FASTQ/paired 的适配器会被接受；否则 gseek 在启动工具前抛出
`UnsupportedFeatureError`。KMA 报告 depth 但没有坐标；不要假设所有 backend 都有
alignment 坐标。

## 7. 索引策略

```python
gseek.align(..., index="auto")     # 复用或构建
gseek.align(..., index="reuse")    # 必须命中缓存
gseek.align(..., index="rebuild")  # 失效并重建
gseek.align(..., index="none")     # 不建持久索引
```

查看缓存：

```bash
gseek index list
gseek index gc            # dry run
gseek index gc --apply    # 删除未引用条目
```

不要用 mtime 或手工拼路径管理索引；让 gseek 从内容与语义参数计算 key。

## 8. 距离与 ANI

```python
distance = gseek.distance(
    backend="mash",
    query=Path("assembly.fna"),
    reference=Path("refs.msh"),
    external_reference=True,
    params={"max_distance": 1.0},
    manifest=Path("runs/mash.manifest.json"),
)

for row in distance.records:
    print(row.query_id, row.reference_id, row.distance, row.pvalue)
```

`external_reference=True` 用于预构建 sketch/数据库（`.msh`、sourmash `.sig`/
rocksdb、skani database）。不传时，gseek 从 FASTA 构建 reference 索引/sketch。

## 9. Manifest、verify 与 replay

写 manifest：

```python
gseek.align(..., manifest=Path("runs/sample.manifest.json"))
```

校验：

```bash
gseek verify runs/sample.manifest.json
```

回放并检查结果哈希：

```bash
gseek replay runs/sample.manifest.json --check --json
```

Replay 不执行保存的 argv，而是根据 manifest 的输入/参数让适配器重新规划，再比较
规范化结果哈希。不一致说明输入、工具版本、环境或适配器行为发生了变化；应排查，
不要忽略。

Python 等价：

```python
issues = gseek.verify("runs/sample.manifest.json")
replayed = gseek.replay("runs/sample.manifest.json", check=True)
```

## 10. 错误

捕获基类并按稳定 code 分支：

```python
from gseek import GseekError

try:
    result = gseek.align(...)
except GseekError as exc:
    print(exc.code, exc.exit_code)
    print(exc.message)
    print(exc.context)
    print(exc.hint)
```

完整表见[错误模型](errors.zh.md)。不要解析异常字符串来决定是否重试或安装工具。

## 11. CLI 等价命令

CLI 适合流水线和排障：

```bash
gseek align \
  --backend minimap2 \
  --query sample.fna \
  --target scheme/alleles.fna \
  --preset asm5 \
  --threads 8 \
  --index auto \
  --manifest runs/sample.manifest.json \
  -o runs/sample.json
```

结果写 stdout（或 `-o`）；错误默认写 stderr 的 `gseek.error/1` JSON。退出码：
`0` 成功、`2` 用法、`3` 工具、`4` 执行/索引、`5` 输入/解析、`6` replay 不一致。
见 [CLI 参考](cli.zh.md)。

## 12. 消费者兼容层（`legacy | gseek | shadow`）

如果维护 legacy 比对路径，用 `gseek.compat.Engine`：

```python
from gseek.compat import Engine

switch = Engine.from_env("MYTOOL_ENGINE")

result = switch.run(
    legacy=lambda: run_legacy(request),
    gseek=lambda: run_with_gseek(request),
    compare=lambda old, new: old == new,
)
```

- `legacy`（默认）：只运行现有实现。
- `gseek`：只运行新路径；异常向上传播。
- `shadow`：两条路径都跑、比较/记录差异、返回 legacy；gseek 或比较器失败时回退
  legacy。

阈值、业务规则与 legacy 实现由消费者自己持有。

## 13. 生产检查清单

1. 固定 gseek 与外部工具版本。
2. 生产运行始终写 manifest。
3. index/cache 根目录放在快速本地盘；定期 `gseek index gc`。
4. 在 CI 或 pipeline 启动时运行 `gseek doctor`。
5. 把 replay mismatch 当作可复现性事故。
6. 在样本层并行；按需设置 `threads=`。
7. 消费者业务规则不要放进适配器。

## 14. 下一步

- [快速开始](quickstart.zh.md) — 最小示例。
- [API 参考](api.zh.md) — 所有公共函数与模型。
- [CLI 参考](cli.zh.md) — 命令与自动化契约。
- [错误模型](errors.zh.md) — 异常/退出码处理。
- [适配器矩阵](https://github.com/indexofire/gseek/blob/main/dev/adapter-matrix.md) —
  backend 参数与坑。
- [兼容性迁移](https://github.com/indexofire/gseek/blob/main/dev/compatibility.md) —
  字段映射与 shadow 迁移。
