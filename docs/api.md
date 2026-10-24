# gseek API 参考

> 版本：0.3.1（alpha）  
> 适用范围：Python ≥ 3.12  
> 源码基准：`src/gseek/`  
> 新用户请先读 [Quickstart](quickstart.md) 与 [CLI](cli.md)。  
> 相关文档：[错误模型](errors.md) · [开发计划](../dev/development-plan.md) · [适配器矩阵](../dev/adapter-matrix.md) · [兼容性迁移](../dev/compatibility.md) · [AGENTS.md](../AGENTS.md)

## 0. 当前状态

**M0 基座 + M1–M4 适配器已实现。**

| 后端 | 输入 | 索引 | 状态 |
| --- | --- | --- | --- |
| `blast` | FASTA | target（makeblastdb） | 可用 |
| `minimap2` | FASTA / FASTQ（含 paired） | target（.mmi） | 可用 |
| `kma` | FASTA / FASTQ（含 paired） | target（kma_db） | 可用 |
| `mummer` | FASTA | 无（nucmer） | 可用（MUMmer 4.x） |
| `bwa` | FASTA / FASTQ（含 paired） | target（BWA prefix） | 可用 |
| `mash` | FASTA / FASTQ | sketch（.msh） | 可用（dist/screen） |
| `sourmash` | FASTA / FASTQ | sketch（.sig） | 可用（search/gather） |
| `skani` | FASTA | sketch 目录 | 可用（ANI） |

- `gseek.backends()` 当前返回 8 个后端：`blast`、`minimap2`、`kma`、`mummer`、`bwa`、`mash`、`sourmash`、`skani`。
- `gseek.align(...)` 支持 5 个 alignment 后端；`gseek.distance(...)` 支持 `mash`/`sourmash`/`skani`。
- `gseek.replay(...)` 可按 manifest 安全重放并校验 `result_hash`。
- 本文档描述的是调用方与适配器作者都应遵守的**稳定契约**；后续增加适配器时，
  调用方不需要改代码。

对于尚未内置的后端，可以按 §6 实现并注册自定义适配器（测试通过后再接入生产）。

---

## 目录

1. [安装与导入](#1-安装与导入)
2. [快速开始](#2-快速开始)
3. [核心概念](#3-核心概念)
4. [公共 API](#4-公共-api)
5. [结果模型](#5-结果模型)
6. [适配器 API](#6-适配器-api)
7. [执行层](#7-执行层)
8. [索引层](#8-索引层)
9. [可复现与 manifest](#9-可复现与-manifest)
10. [I/O 与视图](#10-io-与视图)
11. [错误模型](#11-错误模型)
12. [CLI](#12-cli)
13. [JSON schema 与示例](#13-json-schema-与示例)
14. [稳定性、版本与限制](#14-稳定性版本与限制)

---

## 1. 安装与导入

```bash
pip install gseek
```

开发环境：

```bash
pixi install
pixi run check
```

```python
import gseek

print(gseek.__version__)          # "0.3.1"
print(gseek.ALIGNMENT_SCHEMA)     # "gseek.alignment/1"
```

核心运行时**零第三方依赖**，只使用 Python 标准库。外部比对工具（BLAST+、minimap2、
MUMmer4、KMA、BWA、mash、sourmash）不随包安装，由 `ToolResolver` 在运行时发现。

---

## 2. 快速开始

### 2.1 当前可运行

```python
import gseek

print(gseek.__version__)
print(gseek.backends())        # seven built-in backends

# 查看缓存目录与已有索引
print(gseek.default_cache_root())
store = gseek.IndexStore()
for entry in store.list_indexes():
    print(entry.adapter, entry.key, entry.size_bytes)
```

### 2.2 真实比对（M1 起）

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

best = result.best_per_query_target()
summary = result.summarize_by_target()
```

### 2.3 便捷函数 vs 对象 API

| 场景 | 推荐入口 |
| --- | --- |
| 一次性比对 | `gseek.align(...)` |
| 一次性距离比较 | `gseek.distance(...)` |
| 复用同一个适配器/探测结果 | `gseek.get_backend(...)` + `runner.run_alignment` |
| 自定义适配器 | §6 + `gseek.register_adapter(...)` |
| 检查运行记录 | `gseek.verify(...)` / `gseek.manifest verify` |

---

## 3. 核心概念

### 3.1 一次运行的阶段

```text
AlignRequest
  → 选择适配器（backend 或 adapter）
  → probe 工具（路径 + 版本）
  → 输入/目标哈希（InputRef）
  → 索引（IndexRef：lookup / build / publish）
  → plan_index / plan_align（纯命令计划）
  → Executor 执行（无 shell；超时；流式）
  → parse_align（纯解析）
  → AlignmentResult（稳定排序 + 统计）
  → RunManifest（recipe hash + 命令 + 环境 + 版本）
```

适配器不直接启动子进程；只有 `gseek.execution` 负责执行。这样命令规划、解析和
执行可以分别测试。

### 3.2 坐标约定

- 所有区间是 **0-based half-open**：`[start, end)`。
- 区间始终表示**正向序列**上的位置。
- `strand` 单独表示 query 相对 target 的方向：`+` 同向，`-` 反向。
- 无法提供坐标的后端（KMA、mash/sourmash）将区间字段设为 `None`，不伪造。
- `Interval` 对象可用于区间并集、覆盖率和合法性校验。

### 3.3 identity 与 coverage

| 字段/属性 | 含义 |
| --- | --- |
| `identity` | 百分比，`0..100`；来源写入 `tags["identity_method"]` |
| `query_coverage` | query 被比对区间覆盖的比例，通常 `0..1` |
| `target_coverage` | target 被比对区间覆盖的比例，通常 `0..1` |
| `ungapped_query_coverage` | `(aligned_length - gap_count) / query_length` |
| `ungapped_target_coverage` | `(aligned_length - gap_count) / target_length` |
| `identity_fraction` | `identity / 100` |

注意：

- 核心模型**不 clamp** coverage。KMA 等工具可能报告大于 100% 的模板覆盖，原始值
  会保留；消费者需要知道其语义。
- `identity` 的来源可能是 `blast_pident`、`paf_nm`、`paf_matches`、`sam_nm`、
  `kma_template`、`mummer_coords` 等，见 [适配器矩阵](../dev/adapter-matrix.md)。
- `aligned_length`、`gap_count` 缺失时，`ungapped_*` 返回 `None`，不猜测。

### 3.4 无损与显式归并

适配器只做“工具输出 → `AlignmentRecord`”的无损转换：

- 不自动去重；
- 不自动只保留 best hit；
- 不把应用策略（locus 解析、阈值、ST 判定）写进核心。

归并通过显式视图完成：

```python
best = result.best_per_query_target()
primary = result.primary_only()
summary = result.summarize_by_target()
```

### 3.5 稳定排序

`AlignmentResult` 构造时按以下键排序，保证不同线程数/输入顺序下规范输出一致：

```text
(query_id, target_id, query_start or -1, target_start or -1, strand, -aligned_length)
```

`DistanceResult` 按 `(query_id, reference_id, distance)` 排序。

---

## 4. 公共 API

以下符号均可从顶层 `import gseek` 获得。

### 4.1 `gseek.align`

```python
gseek.align(
    *,
    query: Path | str | Sequence[Path | str],
    target: Path | str | Sequence[Path | str],
    backend: str | None = None,
    adapter: AlignmentAdapter | None = None,
    query_kind: InputKind | str | None = None,
    paired: bool = False,
    params: Mapping[str, JsonValue] | None = None,
    preset: str | None = None,
    threads: int = 1,
    timeout_s: float | None = None,
    index: IndexPolicy | str = IndexPolicy.AUTO,
    target_filter: Iterable[str] | None = None,
    work_dir: Path | str | None = None,
    keep_work: bool = False,
    manifest: Path | str | None = None,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
) -> AlignmentResult
```

参数：

| 参数 | 说明 |
| --- | --- |
| `query` | 单端一个文件；paired 时两个文件（R1、R2） |
| `target` | 一个或多个 FASTA 文件，或包含 FASTA 的目录 |
| `backend` | 已注册适配器名/别名；与 `adapter` 二选一 |
| `adapter` | 直接传入适配器实例；与 `backend` 二选一 |
| `query_kind` | `"fasta"` / `"fastq"`；默认按后缀/内容检测 |
| `paired` | `True` 时 `query` 必须恰好两个文件 |
| `params` | 适配器参数；不同后端不同，见适配器矩阵 |
| `preset` | 便捷参数，会写入 `params["preset"]` |
| `threads` | 传给工具；只写入 manifest，不进入 recipe hash |
| `timeout_s` | 单条命令超时；默认无限 |
| `index` | `IndexPolicy` 或字符串：`auto`/`reuse`/`rebuild`/`none` |
| `target_filter` | 可选的目标 ID 集合；由适配器解释，核心不强制过滤 |
| `work_dir` | workspace 的父目录 |
| `keep_work` | 保留 workspace 以便调试 |
| `manifest` | 输出 `gseek.run/1` JSON 的路径 |
| `resolver` | 自定义工具发现/版本探测 |
| `executor` | 自定义执行器 |
| `store` | 自定义索引缓存 |

自动选择：

- 未传 `backend`/`adapter` 时，按输入格式、输出类型、paired 能力过滤已注册适配器。
- 恰好一个候选才自动选择；0 个或多个都会报错，避免“猜最快后端”导致结果不可解释。

错误：

- `ConfigError`：未知/歧义后端、同时传 `backend` 和 `adapter`；
- `UnsupportedFeatureError`：输入类型/paired/preset 不支持；
- `ToolNotFoundError` / `ToolVersionError`；
- `ExecutionError` / `ParseError` / `IndexBuildError`。

当前内置后端注意事项：

| 后端 | 注意 |
| --- | --- |
| `blast` | 核酸与蛋白任务；FASTA；不接受 `preset`；`index_policy=none` 时使用 `-subject` |
| `minimap2` | 必须显式 `preset`；FASTA/FASTQ、pair；默认 primary-only |
| `kma` | 必须有 target 索引；坐标为空；FASTQ 才有 depth；paired 仅 FASTQ |
| `mummer` | 仅 FASTA；MUMmer **4.x**；无持久索引；反向命中由 `S2 > E2` 恢复 |
| `bwa` | 必须有 target 索引；SAM/CIGAR；默认跳过 secondary；supplementary 保留标记 |
| `mash` | 默认 `dist`；`params={"mode":"screen"}` 走 reads 筛查；ID 来自 sketch 标签；`max_distance` 默认 0.1，传 1.0 可保留全部配对 |
| `sourmash` | 每次只接受一个 query 文件；`containment` 切换 containment 语义；`mode="gather"` 走 gather CSV |
| `skani` | 仅 FASTA；默认 sketch 数据库；ANI 不是 Jaccard/containment；输出 ANI + aligned fraction |

### 4.2 `gseek.distance`

```python
gseek.distance(
    *,
    query: Path | str | Sequence[Path | str],
    reference: Path | str | Sequence[Path | str],
    backend: str | None = None,
    adapter: DistanceAdapter | None = None,
    query_kind: InputKind | str | None = None,
    params: Mapping[str, JsonValue] | None = None,
    threads: int = 1,
    timeout_s: float | None = None,
    index: IndexPolicy | str = IndexPolicy.AUTO,
    external_reference: bool = False,
    work_dir: Path | str | None = None,
    keep_work: bool = False,
    manifest: Path | str | None = None,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
) -> DistanceResult
```

用于 mash/sourmash 等 MinHash/距离后端。语义与 `align` 类似，但参数是 `reference`，
结果是 `DistanceResult`，没有坐标。

`external_reference=True` 表示 `reference` 是**预构建距离产物**，不是 FASTA。
支持两种形式：

- 单文件：mash `.msh`、sourmash `.sig`/`.zip`；
- 目录：sourmash rocksdb 数据库（`sourmash index -F rocksdb` 生成）。

此时 gseek 跳过索引构建与 reference sketch，直接把路径交给 `mash dist` /
`sourmash gather`；manifest 中的 `InputRef.kind` 为 `sketch`。目录使用
`sha256_tree` 做确定性哈希（排序后的相对路径、类型、文件内容；树内 symlink
被拒绝），因此 `verify`/`replay` 同样适用。skani database 目录仍不支持
external 模式。

> 当前 distance 后端为 `mash`、`sourmash` 与 `skani`。

### 4.3 注册表查询

```python
gseek.backends() -> tuple[BackendSpec, ...]
gseek.find(
    *,
    input: InputKind | str | None = None,
    output: OutputKind | str | None = None,
    paired: bool | None = None,
    molecule: Molecule | str | None = None,
) -> tuple[BackendSpec, ...]
gseek.find_backends(...)  # find 的别名
```

示例：

```python
alignment_backends = gseek.find(input="fastq", output="alignment", paired=True)
for spec in alignment_backends:
    print(spec.name, spec.family, spec.capabilities.index_scope)
```

`BackendSpec` 的能力字段：

| 字段 | 含义 |
| --- | --- |
| `inputs` | 支持的 `InputKind` 集合 |
| `outputs` | 支持的 `OutputKind` 集合 |
| `molecule` | DNA/RNA/蛋白质 |
| `paired` | 是否支持双端 |
| `index_scope` | `none`/`target`/`query`/`both` |
| `streaming` | 是否支持 stdout 流式解析 |
| `threads` | 是否接受线程参数 |
| `presets` | 允许的 preset 名称 |
| `deterministic` | 后端是否声明内容确定性 |

### 4.4 获取/注册适配器

```python
adapter = gseek.get_backend("minimap2")
result = gseek.align(
    adapter=adapter,
    query="sample.fna",
    target="alleles.fna",
    preset="asm5",
)

gseek.register_adapter(MyAdapter)
gseek.register_adapter(MyAdapter, replace=True)
```

- `get_backend(name, **kwargs)` 通过注册表实例化；`kwargs` 传给适配器构造函数。
  内置适配器（`blast`/`minimap2`/`kma`/`mummer`/`bwa`/`mash`/`sourmash`/`skani`）不需要构造参数；运行参数（如 `preset`）通过
  `align`/`distance` 的 `params` 传入。
- `register_adapter` 注册类或实例；默认重名抛 `ConfigError`，`replace=True` 覆盖。
- 插件也可通过 entry point 组 `gseek.adapters` 注册（见 §6.7）。

### 4.5 校验运行记录

```python
issues = gseek.verify("runs/sample.manifest.json")
if issues:
    for issue in issues:
        print(issue.severity, issue.code, issue.message)
```

`verify` 接受路径或 `RunManifest`，返回 `tuple[Issue, ...]`；只校验记录中的文件
大小/SHA-256，不会重新探测工具版本或重跑。

### 4.6 版本与 schema 常量

```python
gseek.__version__        # "0.3.1"
gseek.ALIGNMENT_SCHEMA   # "gseek.alignment/1"
gseek.DISTANCE_SCHEMA    # "gseek.distance/1"
gseek.RUN_SCHEMA         # "gseek.run/1"
gseek.INDEX_SCHEMA       # "gseek.index/1"
```

---

## 5. 结果模型

所有模型都是 frozen dataclass（`RunManifest`/`IndexRef`/`CommandRecord` 为 keyword-only），
可安全跨线程传递。模型只保存事实，不做生物学判定。

### 5.1 `AlignmentRecord`

```python
AlignmentRecord(
    query_id: str,
    target_id: str,
    query_length: int | None = None,
    target_length: int | None = None,
    query_start: int | None = None,
    query_end: int | None = None,
    target_start: int | None = None,
    target_end: int | None = None,
    strand: Strand = Strand.PLUS,
    identity: float | None = None,
    aligned_length: int | None = None,
    matches: int | None = None,
    mismatches: int | None = None,
    gap_count: int | None = None,
    gap_openings: int | None = None,
    evalue: float | None = None,
    bit_score: float | None = None,
    mapq: int | None = None,
    depth: float | None = None,
    score: float | None = None,
    query_coverage: float | None = None,
    target_coverage: float | None = None,
    query_aligned: str | None = None,
    target_aligned: str | None = None,
    target_description: str | None = None,
    is_primary: bool = True,
    is_supplementary: bool = False,
    tags: Mapping[str, str] = {},
)
```

字段分组：

| 组 | 字段 |
| --- | --- |
| ID/长度 | `query_id`, `target_id`, `query_length`, `target_length` |
| 区间/方向 | `query_start/end`, `target_start/end`, `strand` |
| 比对质量 | `identity`, `aligned_length`, `matches`, `mismatches`, `gap_count`, `gap_openings` |
| 统计量 | `evalue`, `bit_score`, `mapq`, `depth`, `score` |
| 覆盖 | `query_coverage`, `target_coverage` |
| 序列 | `query_aligned`, `target_aligned`, `target_description` |
| 状态/扩展 | `is_primary`, `is_supplementary`, `tags` |

构造校验：

- `query_id` / `target_id` 非空；
- `identity` 在 `0..100`；
- 长度/计数非负；
- 区间必须成对出现，`0 <= start <= end`；
- 若未显式给 coverage 且区间/长度齐全，自动由区间跨度推导。

派生属性：

| 属性 | 返回 |
| --- | --- |
| `query_interval` / `target_interval` | `Interval | None` |
| `span_query_coverage` / `span_target_coverage` | 区间跨度 / 长度 |
| `ungapped_query_coverage` / `ungapped_target_coverage` | `(aligned_length-gap_count)/length` |
| `identity_fraction` | `identity/100` |
| `identity_method` | `tags["identity_method"]` |
| `identity_from_nm` | `(aligned_length-NM)/aligned_length`，无 NM 时为 `None` |
| `identity_from_matches` | `matches/aligned_length`，缺失时为 `None` |
| `identity_nmatch_nm` | gmlst 兼容的 `nmatch/(nmatch+NM)` |
| `is_reverse` | `strand == "-"` |
| `aligned_bases` | `aligned_length`，缺失时用 query 区间长度 |
| `sort_key()` | 主排序键 |
| `stable_key()` | 全序排序键（主排序键 + canonical JSON tie-break） |
| `quality_key()` | best-hit 视图使用的质量键 |

示例：

```python
from gseek import AlignmentRecord

record = AlignmentRecord(
    query_id="contig_1",
    target_id="abcZ_12",
    query_length=5000,
    target_length=480,
    query_start=1000,
    query_end=1480,
    target_start=0,
    target_end=480,
    identity=100.0,
    aligned_length=480,
    gap_count=0,
    tags={"identity_method": "blast_pident"},
)
assert record.target_coverage == 1.0
assert record.query_coverage == 0.096
```

### 5.2 `AlignmentResult`

```python
AlignmentResult(
    backend: str,
    records: tuple[AlignmentRecord, ...],
    inputs: tuple[InputRef, ...],
    targets: tuple[InputRef, ...],
    params: dict[str, JsonValue],
    stats: AlignmentStats,
    index: IndexRef | None = None,
    manifest: RunManifest | None = None,
    warnings: tuple[Issue, ...] = (),
    errors: tuple[Issue, ...] = (),
    schema: str = "gseek.alignment/1",
)
```

- 可迭代，`len(result)` 是记录数。
- 构造时自动稳定排序。
- `to_dict(include_manifest=False)` 返回 JSON 可序列化对象。
- `to_dict(include_manifest=True)` 会嵌入完整 manifest（可能较大）。

方法：

| 方法 | 说明 |
| --- | --- |
| `primary_only()` | 过滤 `is_primary=False` 或 `is_supplementary=True` |
| `best_per_query_target()` | 每个 `(query,target)` 最佳行；质量键 identity→length→coverage |
| `summarize_by_target()` | 每个 target 的区间并集 breadth、加权 identity、depth |
| `summarize_by_query()` | 每个 query 的命中 target 数与最佳 identity |
| `to_dict()` | 序列化为 dict |

`TargetSummary` 字段：

```python
target_id, target_length, record_count, query_count,
covered_bases, breadth, mean_identity, weighted_identity,
mean_depth, best_record
```

- `breadth = union(target intervals) / target_length`；
- `weighted_identity` 以 `aligned_length` 加权（缺失按 1）；
- `mean_depth` 以 `aligned_length` 加权。

`AlignmentStats`：

```python
record_count, query_count, target_count,
queries_with_hits, targets_with_hits,
aligned_bases, runtime_s, truncated
```

- `query_count` / `target_count` 是**输入文件数**，与 `inputs` / `targets` 元组长度一致，
  不是 FASTA record 数（record 数记录在 `InputRef.records`）。
- `queries_with_hits` / `targets_with_hits` 是命中记录中不同 ID 的数量。
- `aligned_bases` 是各记录 `aligned_bases` 之和（优先 `aligned_length`）。

### 5.3 `DistanceRecord` / `DistanceResult`

```python
DistanceRecord(
    query_id: str,
    reference_id: str,
    distance: float | None = None,
    pvalue: float | None = None,
    shared_hashes: int | None = None,
    total_hashes: int | None = None,
    containment: float | None = None,
    jaccard: float | None = None,
    ani: float | None = None,
    aligned_fraction: float | None = None,
    tags: Mapping[str, str] = {},
)
```

```python
DistanceResult(
    backend: str,
    records: tuple[DistanceRecord, ...],
    inputs: tuple[InputRef, ...],
    references: tuple[InputRef, ...],
    params: dict[str, JsonValue],
    stats: DistanceStats,
    index: IndexRef | None = None,
    manifest: RunManifest | None = None,
    warnings: tuple[Issue, ...] = (),
    errors: tuple[Issue, ...] = (),
    schema: str = "gseek.distance/1",
)
```

- `DistanceResult.best_per_query()`：每个 query 的最低 distance；平局看 shared_hashes。
- `DistanceStats`：`record_count`, `query_count`, `reference_count`, `runtime_s`；
  这里的 `query_count`/`reference_count` 是**有命中的不同 ID 数**，与
  `AlignmentStats.query_count`（输入文件数）语义不同。
- `tags["metric"]` 应记录指标类型（mash distance、containment、Jaccard 等）。

### 5.4 文件与工具引用

```python
FileRef(path: str, size: int, sha256: str)

InputRef(
    path: str, size: int, sha256: str,
    kind: InputKind = InputKind.FASTA,  # fasta | fastq | sketch
    records: int | None = None,
    compressed: bool = False,
)

ToolRef(
    name: str, version: str,
    path: str | None = None,
    version_raw: str = "",
    role: str = "aligner",
    sha256: str | None = None,
)

Issue(
    code: str, message: str,
    severity: Literal["warning", "error"] = "warning",
    context: dict[str, str] = {},
)
```

`InputRef`/`FileRef` 的 `path` 在 Runner 写入 manifest 时是绝对路径（相对输入会在
`input_ref` 阶段 `resolve()`），因此 manifest 可脱离运行 cwd 校验。

### 5.5 `IndexRef`

```python
IndexRef(
    key: str,
    adapter: str,
    adapter_version: str,
    entry: str,
    source_sha256: str,
    artifacts: tuple[str, ...] = (),
    params: dict[str, JsonValue] = {},
    created_at: str | None = None,
    cache_hit: bool = False,
    tool_versions: dict[str, str] = {},
)
```

- `entry` 是相对索引目录的入口（文件或前缀）。
- `artifacts` 是相对 glob，用于发布前校验。
- `cache_hit=True` 表示本次没有重建。
- `source_sha256` 是 target 内容集合的规范哈希。

### 5.6 `CommandRecord`

```python
CommandRecord(
    phase: Literal["index", "align", "distance"],
    label: str,
    argv: tuple[str, ...],
    cwd: str | None,
    exit_code: int,
    duration_s: float,
    timed_out: bool = False,
    stderr_tail: str = "",
    stdout_path: str | None = None,
)
```

### 5.7 `RunManifest`

```python
RunManifest(
    run_id: str,
    recipe_hash: str,              # 64 位 sha256 hex
    created_at: str,               # UTC ISO8601
    gseek_version: str,
    python_version: str,
    platform: str,
    backend: str,
    backend_version: str,
    tools: tuple[ToolRef, ...],
    inputs: tuple[InputRef, ...],
    targets: tuple[InputRef, ...],
    params: dict[str, JsonValue],
    commands: tuple[CommandRecord, ...],
    result_hash: str | None = None,   # 规范化结果的 sha256
    outputs: tuple[FileRef, ...] = (),
    index: IndexRef | None = None,
    hostname: str | None = None,
    env: dict[str, str] = {},
    status: RunStatus = RunStatus.OK,
    duration_s: float = 0.0,
    warnings: tuple[Issue, ...] = (),
    errors: tuple[Issue, ...] = (),
    extra: dict[str, JsonValue] = {},
    schema: str = "gseek.run/1",
)
```

- `recipe_hash` 是确定性身份；不包含时间、hostname、workspace、线程数。
- `params` 已做语义过滤（见 §9.4）。
- `result_hash` 是规范化记录的 sha256（哈希前按稳定全序排序，因此与解析器输出顺序
  无关），用于 `replay --check`。
- `outputs` 目前由调用方按需填写；Runner 默认不写结果文件。
- `RunManifest.from_dict()` 会校验 schema 与 recipe hash 长度；普通读取建议用
  `gseek.read_manifest(path)`。

---

## 6. 适配器 API

### 6.1 协议

```python
class AlignmentAdapter(Protocol):
    spec: BackendSpec
    def probe(self, resolver: ToolResolver, *, strict: bool = False) -> ToolchainProbe: ...
    def semantic_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]: ...
    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan | None: ...
    def plan_align(
        self, request: AlignRequest, index: IndexRef | None, ctx: PlanContext
    ) -> AlignPlan: ...
    def parse_align(
        self, ctx: ParseContext, stream: Iterable[str] | None = None
    ) -> Iterator[AlignmentRecord]: ...
```

距离后端实现 `DistanceAdapter`：`plan_distance` / `parse_distance`，返回
`DistanceRecord`。

### 6.2 请求对象

```python
AlignRequest(
    query: tuple[Path, ...],
    target: tuple[Path, ...],
    query_kind: InputKind | None = None,
    paired: bool = False,
    params: Mapping[str, JsonValue] = {},
    threads: int = 1,
    timeout_s: float | None = None,
    index_policy: IndexPolicy = IndexPolicy.AUTO,
    target_filter: frozenset[str] | None = None,
    work_dir: Path | None = None,
    keep_work: bool = False,
)
```

约束：

- `query` 必须非空；`paired=True` 时必须恰好 2 个；
- `target` 非空；
- `threads >= 1`；`timeout_s > 0`；
- `query_kind` 为 `None` 时按第一个 query 检测。

`DistanceRequest` 用 `reference` 替代 `target`，其余类似。

### 6.3 计划与上下文

```python
PlanContext(
    workspace: Path,
    index_dir: Path | None = None,
    threads: int = 1,
    query_refs: tuple[InputRef, ...] = (),
    target_refs: tuple[InputRef, ...] = (),
)

IndexPlan(
    commands: tuple[CommandPlan, ...],
    entry: str,
    artifacts: tuple[str, ...] = (),
    params: Mapping[str, JsonValue] = {},
)

AlignPlan(
    commands: tuple[CommandPlan, ...],
    stream: bool = False,
    result_files: tuple[Path, ...] = (),
    extra: Mapping[str, JsonValue] = {},
)

ParseContext(
    request: AlignRequest,
    plan: AlignPlan,
    workspace: Path,
    index: IndexRef | None = None,
    tools: tuple[ToolRef, ...] = (),
)
```

- `stream=True`：Runner 把最后一条命令的 stdout 行迭代器传给 `parse_align`。
- `stream=False`：Runner 执行全部命令，解析器读取 `result_files`。
- `ctx.path(...)` 会创建父目录，方便适配器把输出放进 workspace。
- 输出路径必须位于 workspace/index staging 内。

### 6.4 能力声明

```python
ToolRequirement(
    binary: str,
    role: str = "aligner",
    version_args: tuple[str, ...] = ("--version",),
    version_pattern: str | None = None,
    min_version: tuple[int, ...] | None = None,
    optional: bool = False,
    hint: str = "",
)

Capabilities(
    inputs: frozenset[InputKind],
    outputs: frozenset[OutputKind],
    molecule: frozenset[Molecule] = {DNA},
    paired: bool = False,
    index_scope: IndexScope = IndexScope.NONE,
    streaming: bool = False,
    threads: bool = True,
    presets: tuple[str, ...] = (),
    deterministic: bool = True,
)

BackendSpec(
    name: str,
    family: str,
    summary: str,
    capabilities: Capabilities,
    tools: tuple[ToolRequirement, ...],
    aliases: tuple[str, ...] = (),
    api_version: str = "1",
    params_schema: dict[str, JsonValue] = {},
)
```

`BackendSpec` 是能力矩阵、`gseek.backends()`、`gseek.find()` 和 `gseek doctor`
的事实来源。

### 6.5 `BaseAdapter`

`BaseAdapter` 提供：

| 方法 | 行为 |
| --- | --- |
| `probe(resolver, strict=False)` | 逐个探测 `spec.tools`，汇总 `ok/error`；`strict=True` 时失败即抛 |
| `semantic_params(params)` | 过滤执行期参数（threads/debug 等） |
| `plan_index(request, ctx)` | 无索引后端返回 `None`；声明索引但未实现时抛 `NotImplementedError` |
| `check_align_request(request)` | 校验 input/paired/preset 能力 |
| `describe()` | 把 spec 转为 JSON 友好 dict |

### 6.6 最小适配器骨架

以下示例展示**契约**，不是可运行的比对工具：

```python
from collections.abc import Iterable, Iterator

from gseek import (
    AlignmentRecord, AlignPlan, AlignRequest, BackendSpec, BaseAdapter,
    Capabilities, CommandPlan, IndexPlan, IndexRequest, IndexScope,
    InputKind, OutputKind, ParseContext, PlanContext, ToolRequirement,
    ToolchainProbe, ToolResolver,
)


class MyAdapter(BaseAdapter):
    spec = BackendSpec(
        name="myalign",
        family="myalign",
        summary="example adapter",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
            index_scope=IndexScope.TARGET,
            streaming=True,
            threads=True,
        ),
        tools=(
            ToolRequirement(
                binary="myalign",
                version_args=("--version",),
                version_pattern=r"(\d+)\.(\d+)\.(\d+)",
                min_version=(1, 0, 0),
            ),
        ),
    )

    def probe(self, resolver: ToolResolver, *, strict: bool = False) -> ToolchainProbe:
        return super().probe(resolver, strict=strict)

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        out = ctx.path("myalign.idx")
        return IndexPlan(
            commands=(CommandPlan(argv=("myalign", "index", "-o", str(out), *map(str, request.target))),),
            entry=out.name,
            artifacts=(out.name,),
        )

    def plan_align(self, request: AlignRequest, index, ctx: PlanContext) -> AlignPlan:
        command = CommandPlan(
            argv=("myalign", "map", str(ctx.index_dir / index.entry), *map(str, request.query)),
            label="myalign map",
        )
        return AlignPlan(commands=(command,), stream=True)

    def parse_align(
        self, ctx: ParseContext, stream: Iterable[str] | None = None
    ) -> Iterator[AlignmentRecord]:
        assert stream is not None
        for line in stream:
            # TODO: 解析 myalign 输出，转换为 0-based half-open 坐标
            ...
```

注册：

```python
import gseek
gseek.register_adapter(MyAdapter)
result = gseek.align(backend="myalign", query="q.fa", target="t.fa")
```

内置适配器还应把模块加入 `gseek.registry.BUILTIN_MODULES`。

### 6.7 插件入口点

第三方包可在 `pyproject.toml` 声明：

```toml
[project.entry-points."gseek.adapters"]
myalign = "mypkg.myalign:ADAPTER"
```

`ADAPTER` 可以是适配器类或实例。注册表在首次使用时加载，坏插件只记录 warning，
不会拖垮核心导入。

### 6.8 适配器测试要求

按 [AGENTS.md](../AGENTS.md) §3.3，至少覆盖：

1. `BackendSpec` 能力/工具/版本约束；
2. 命令规划（argv、cwd、env、输出路径、超时）；
3. 真实输出 golden（正向/反向/gapped/空结果/坏行/CRLF）；
4. 坐标/identity/coverage 归一化；
5. 索引 key/artifacts；
6. 集成测试（`@pytest.mark.integration`）；
7. gmlst/gapit/gside parity（迁移阶段）。

---

## 7. 执行层

模块：`gseek.execution`。这是**唯一**允许启动子进程的地方。

### 7.1 `CommandPlan`

```python
CommandPlan(
    argv: tuple[str, ...],
    label: str = "",
    stdin: Path | None = None,
    stdin_text: str | None = None,
    stdout: Path | None = None,
    stderr: Path | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] = {},
    timeout_s: float | None = None,
    expected_codes: frozenset[int] = frozenset({0}),
)
```

- `argv` 必须非空且全为字符串；
- `stdin` 与 `stdin_text` 互斥；
- `stdout`/`stderr` 指定时重定向到文件；
- `expected_codes` 之外的退出码抛 `ExecutionError`；
- `render()` 返回空格拼接的命令（日志用，不用于执行）。

### 7.2 `Executor`

```python
executor = Executor(
    base_env=None,
    cwd=None,
    deterministic_env=True,   # LC_ALL=C, LANG=C, TZ=UTC
    stderr_limit=64_000,
)

result = executor.run(plan)     # -> ProcessResult

with executor.stream(plan) as process:
    for line in process.iter_lines():
        ...
    result = process.wait()
```

- 永远 `shell=False`；
- `start_new_session=True`，超时/取消时杀整个进程组；
- stderr 由后台线程持续排空，避免管道写满；
- `stream()` 适合 PAF/SAM/tabular 大输出；
- `ProcessResult` 提供 `stdout`、`stderr`、`returncode`、`duration_s`、
  `timed_out`、`stdout_path`、`stderr_path`、`stderr_tail`。

### 7.3 `ToolResolver`

```python
resolver = ToolResolver(
    overrides={"blastn": "/opt/blast/bin/blastn"},
    extra_paths=["/opt/conda/bin"],
    env=os.environ,
    executor=executor,
)

path = resolver.resolve("blastn")
probe = resolver.probe(
    ToolRequirement(
        binary="blastn",
        version_args=("-version",),
        version_pattern=r"blastn:\s*(\d+)\.(\d+)\.(\d+)",
        min_version=(2, 2, 30),
    )
)
```

查找顺序：

1. `overrides[binary]`；
2. 环境变量 `GSEEK_<BINARY>`（如 `GSEEK_BLASTN`）；
3. `extra_paths` + `GSEEK_PATH_PREPEND` + `PATH`（`shutil.which`）。

`probe` 结果缓存于进程内，key 为解析后的二进制路径、version argv、version pattern、
min_version 与 role。`strict=False` 时缺失/版本错误返回 `ToolProbe(ok=False)`；
`strict=True` 时缺失抛 `ToolNotFoundError`、版本不满足抛 `ToolVersionError`，并带上
`ToolRequirement.hint`。`optional=True` 的工具即使版本过旧也不会因 `strict` 抛错。

Runner 会把 probe 解析出的绝对路径绑定到 `Executor`；适配器 argv 中的裸二进制名在
执行前会被替换为该路径。因此 `overrides`、`GSEEK_<BINARY>`、`extra_paths`、
`GSEEK_PATH_PREPEND` 同时影响版本探测和实际执行，manifest 记录的 `argv[0]` 也是
绝对路径。

### 7.4 环境变量

| 变量 | 作用 |
| --- | --- |
| `GSEEK_CACHE_DIR` | 索引缓存根目录 |
| `GSEEK_TMPDIR` | run workspace 的临时根目录 |
| `GSEEK_PATH_PREPEND` | 工具搜索路径前缀 |
| `GSEEK_<BINARY>` | 指定某工具路径（如 `GSEEK_MINIMAP2`） |
| `PATH` / `LD_LIBRARY_PATH` / `CONDA_PREFIX` | 正常进程环境；manifest 只记录 allowlist |

### 7.5 Workspace

```python
workspace = create_workspace(prefix="gseek-", parent=None, keep=False)
path = workspace.path("hits.tsv")   # 自动创建父目录
workspace.cleanup()                 # keep=True 时保留
```

Runner 默认在结束时清理；`keep_work=True` 可保留。`GSEEK_TMPDIR` 只改变 workspace
的父目录，不改变清理行为。

---

## 8. 索引层

模块：`gseek.index`。

### 8.1 `IndexPolicy`

| 值 | 行为 |
| --- | --- |
| `auto` | 命中缓存则复用，否则构建 |
| `reuse` | 只复用；未命中报 `IndexBuildError` |
| `rebuild` | 强制重建（替换现有索引） |
| `none` | 不建/不用索引，适配器直接读 target |

### 8.2 缓存布局

```text
<GSEEK_CACHE_DIR 或 ~/.cache/gseek>/
└── index/
    └── <adapter>/
        └── <key>/
            ├── index.json
            └── ...              # 工具产物
```

`key = sha256(canonical_json({...}))`，包含：

- adapter 名称与实现版本（`api_version+gseek.<package version>`）；
- 影响索引的语义参数；
- target 内容哈希（不是 mtime）；
- 工具版本；
- 索引格式版本。

构建在 `.staging-<key>-...` 中进行，`index.json` 最后写；成功后目录 rename 发布。
`<key>.lock` 使用 `fcntl.flock`，防止并发重复构建。

### 8.3 `IndexStore`

```python
store = IndexStore(root=None, enabled=True)

store.lookup(adapter="blast", key="...")          # IndexRef | None
store.new_staging(adapter="blast", key="...")     # Path
store.publish(
    staging, adapter="blast", key="...",
    adapter_version="1", entry="targets", artifacts=("targets.n*",),
    source_sha256="...", params={}, tool_versions={"makeblastdb": "2.17.0+"},
    replace=False,
)                                                  # IndexRef
store.lock("blast", "...", timeout_s=300)          # context manager
store.list_indexes()                               # tuple[IndexGcEntry, ...]
store.gc(max_bytes=None, max_age_days=None, dry_run=True)
```

`replace=True` 由 `IndexPolicy.REBUILD` 使用：即使已有有效索引，也用新构建的 staging
替换。`enabled=False` 时 `lookup/list_indexes/gc` 返回空，`new_staging/publish/lock`
抛 `CacheError`；Runner 会直接跳过索引。

`IndexGcEntry`：`key`, `adapter`, `path`, `size_bytes`, `created_at`, `removed`。

### 8.4 Key 计算

```python
key = compute_index_key(
    adapter="minimap2",
    adapter_version="1+gseek.0.3.1",
    targets=(target_ref,),
    params={"preset": "asm5"},
    tool_versions={"minimap2": "2.30-r1287"},
    format_version="1",
)
```

注意：

- `threads`、`timeout`、`debug`、输出路径等不进入 key；
- target 使用输入文件内容哈希；适配器在 staging 中调用 `merge_fasta` 生成规范化
  target，但 key 不依赖合并后的临时路径；
- Runner 的 `adapter_version` 是 `api_version+gseek.<package version>`，gseek 升级或
  适配器实现变化会使缓存失效；
- 工具升级/参数变化/目标内容变化都会得到新 key。

---

## 9. 可复现与 manifest

模块：`gseek.provenance`。

### 9.1 Hashing 与 canonical JSON

```python
gseek.sha256_file(path)             # 流式 SHA-256
gseek.canonical_json(value)         # sort_keys + compact + ASCII
gseek.recipe_hash(...)              # 语义 recipe 哈希
gseek.result_hash(records)          # 规范化记录哈希
```

`canonical_json` 对 dict 排序、对 tuple/list 统一、枚举取 value、Path 取 str；
用于稳定哈希。

### 9.2 `recipe_hash`

```python
digest = gseek.recipe_hash(
    backend="minimap2",
    adapter_version="1",
    inputs=(query_ref,),
    targets=(target_ref,),
    params={"preset": "asm5", "threads": 8},
    tools=(tool_ref,),
    index_key=index.key,
)
```

包含：gseek 版本、backend、adapter 版本、语义参数、输入/目标哈希、index key、工具版本。

排除：时间、hostname、workspace、输出路径、线程数、超时、调试开关、manifest 路径。

### 9.3 Manifest 读写

```python
from gseek import read_manifest, write_manifest

manifest = read_manifest(Path("run.json"))
write_manifest(Path("copy.json"), manifest)
```

`write_manifest` 使用同目录临时文件 + `os.replace` 原子发布。

### 9.4 语义参数过滤

`semantic_params(params)` 会去掉：

```text
threads, num_threads, timeout_s, timeout,
keep_work, work_dir, cache_dir,
verbose, quiet, debug,
manifest, manifest_path, output, output_path
```

这些参数会从 manifest 的 `params` 中移除，不参与 recipe hash；`threads`/`paired`
仍记录在 `extra` 中。`timeout_s`、`work_dir`、`output` 等执行参数不再写入 `params`。

### 9.5 `verify_manifest`

```python
issues = gseek.verify_manifest(manifest, base_dir=None)
```

检查 `inputs`/`targets`/`outputs` 是否存在、大小和 SHA-256 是否匹配。**不**重新探测
工具版本，也**不**重跑。新 manifest 记录绝对路径；读取旧 manifest 时，如果 `verify`
入参是路径且未指定 `base_dir`，相对路径按 manifest 所在目录解析（与 `replay` 一致）。
CLI：

```bash
gseek verify runs/sample.manifest.json
gseek manifest verify runs/sample.manifest.json --json
```

### 9.6 回放

```python
outcome = gseek.replay("runs/sample.manifest.json", check=True)
outcome.result_hash   # 本次规范化记录哈希
outcome.expected_hash # manifest 中的结果哈希
outcome.matches       # bool | None
```

`replay` 只把 manifest 当作 recipe **重新规划**命令，绝不执行 manifest 中记录的任意
argv。使用 backend/params/inputs/threads/paired/target_filter 重新调用 API；`--check`
不一致时抛 `ReplayMismatchError`（退出码 6）。

---

## 10. I/O 与视图

### 10.1 `gseek.io`

```python
SequenceRecord(sequence_id, sequence, description="")

detect_input_kind(path, probe_content=True) -> InputKind
iter_fasta(path) -> Iterator[SequenceRecord]
read_fasta(path) -> tuple[SequenceRecord, ...]
iter_fastq(path) -> Iterator[SequenceRecord]     # 严格四行 FASTQ
count_records(path, kind=None) -> int
open_text(path)                                  # gzip/bz2/xz 透明读取
```

`merge_fasta` 是 target 规范化的核心：

```python
merge_fasta(
    paths: Sequence[Path],
    dest: Path,
    *,
    width: int = 60,
    uppercase: bool = False,
    on_duplicate: "error" | "first" | "prefix" = "error",
    sort_files: bool = True,
) -> Path
```

- 目录会展开为其中的 FASTA 文件；
- 文件按路径排序，输出确定；
- 重复 ID 默认报 `DuplicateIdError`；`first` 保留首个；`prefix` 用 `<stem>|<id>`；
- 先写临时文件，再 `os.replace`，不会留下半成品。

### 10.2 `gseek.views`

```python
to_jsonl(result) -> str
to_tsv(result, columns=DEFAULT_TSV_COLUMNS) -> str
deduplicate_intervals(records) -> tuple[AlignmentRecord, ...]
count_distinct_occurrences(records, *, query_id, target_id) -> int
```

- `to_jsonl`：header 行 + 每条记录一行 + footer 行；footer 的 `status` 为
  `ok`/`warning`/`error`，反映结果上的 `warnings`/`errors`。
- `to_tsv`：默认稳定列集；未知列抛 `ValueError`。
- `deduplicate_intervals`：按 `(query_id, query_start, query_end)` 去重，忽略 strand，
  首个保留（gapit/abricate 兼容）；无坐标记录（如 KMA）原样保留。
- `count_distinct_occurrences`：统计一个 target 在 query 上的不同放置数（gmlst
  `count_same_copy` 语义）。

---

## 11. 错误模型

完整的异常/退出码表与处理建议见 [错误模型](errors.md)。


所有错误继承 `gseek.GseekError`，带 `code`、`exit_code`、`context`、`hint`。

### 11.1 错误表

| 异常 | `code` | CLI exit | 场景 |
| --- | --- | --- | --- |
| `UsageError` | `USAGE_ERROR` | 2 | API/CLI 用法错误 |
| `ConfigError` | `CONFIG_ERROR` | 2 | 未知/歧义后端、配置错误 |
| `UnsupportedFeatureError` | `UNSUPPORTED_FEATURE` | 2 | 输入/preset/paired 不支持 |
| `ToolNotFoundError` | `TOOL_NOT_FOUND` | 3 | 二进制缺失 |
| `ToolVersionError` | `TOOL_VERSION_ERROR` | 3 | 版本不可解析/过旧 |
| `ExecutionError` | `EXECUTION_FAILED` | 4 | 非零退出/超时 |
| `IndexBuildError` | `INDEX_BUILD_FAILED` | 4 | 索引构建/产物校验失败 |
| `IndexLockError` | `INDEX_LOCK_TIMEOUT` | 4 | 等待索引锁超时 |
| `CacheError` | `CACHE_ERROR` | 4 | 缓存目录不可用/损坏 |
| `ManifestError` | `MANIFEST_ERROR` | 4 | manifest 缺失/格式错误 |
| `ParseError` | `PARSE_ERROR` | 5 | 输出行无法解析 |
| `InputError` | `INPUT_ERROR` | 5 | 输入缺失/格式错误 |
| `DuplicateIdError` | `DUPLICATE_SEQUENCE_ID` | 5 | 目标 FASTA ID 重复 |
| `ReplayMismatchError` | `REPLAY_MISMATCH` | 6 | replay 结果不一致 |
| `GseekError` | `GSEEK_ERROR` | 1 | 其他库错误/未分类失败 |

### 11.2 JSON envelope

```python
from gseek import InputError, render_error

error = InputError("reads file not found", context={"file": "r1.fq"})
print(render_error(error))
```

```json
{
  "schema": "gseek.error/1",
  "code": "INPUT_ERROR",
  "message": "reads file not found",
  "context": {"file": "r1.fq"},
  "hint": ""
}
```

CLI 捕获 `GseekError` 后输出一行 envelope 到 stderr，并以 `exit_code` 退出。

---

## 12. CLI

面向用户的命令示例、JSON 约定与自动化注意事项见 [CLI](cli.md)。

```bash
gseek --version
gseek --error-format {json,text} <command> ...
gseek backends [--json]
gseek doctor [--json]
gseek index list [--json]
gseek index gc [--apply] [--max-bytes N] [--max-age-days D] [--json]
gseek manifest verify PATH [--base-dir DIR] [--json]
gseek verify PATH [--base-dir DIR] [--json]
gseek replay PATH [--base-dir DIR] [--check] [--json]
gseek align --backend NAME --query Q --target T [options]
gseek distance --backend NAME --query Q --reference R [options]
```

### 12.1 `align` / `distance` 选项

| 选项 | 说明 |
| --- | --- |
| `--backend` | 必填；已注册适配器 |
| `--query` | 可重复；paired 时两个 |
| `--target` / `--reference` | 可重复 |
| `--threads` | 默认 1 |
| `--preset` | 仅 `align`；写入 params |
| `--param KEY=VALUE` | 可重复；值先按 JSON 解析，失败按字符串 |
| `--index` | `auto`/`reuse`/`rebuild`/`none` |
| `--manifest` | 输出 run manifest |
| `--output` / `-o` | 结果 JSON 文件；缺省 stdout |
| `--paired` | 仅 `align`；双端 |
| `--query-kind` | `fasta`/`fastq` |
| `--external-reference` | 仅 `distance`；`--reference` 是预构建 `.msh`/`.sig`/`.zip` |
| `--keep-work` | 保留临时目录 |

示例：

```bash
gseek align \
  --backend minimap2 \
  --query sample.fna \
  --target scheme/alleles.fna \
  --preset asm5 \
  --threads 8 \
  --manifest runs/sample.manifest.json \
  -o runs/sample.json
```

### 12.2 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 未预期错误 |
| 2 | 用法/配置/不支持 |
| 3 | 工具缺失/版本错误 |
| 4 | 执行/索引/缓存/manifest 错误 |
| 5 | 输入/解析错误 |
| 6 | replay 不一致 |
| 130 | 用户中断 |

### 12.3 `doctor`

```bash
gseek doctor --json
```

对每个已注册后端调用 `probe(strict=False)`，输出工具路径/版本/错误；任一必需工具
不可用时返回 1。当前内置 8 个后端（`blast`、`minimap2`、`kma`、`mummer`、
`bwa`、`mash`、`sourmash`、`skani`）；没有安装对应工具时 doctor 会报告缺失并返回 1。

### 12.4 `replay`

```bash
gseek replay runs/sample.manifest.json --check --json
```

- 按 manifest 的 backend/params/inputs 重新规划，不执行 manifest 中的 argv。
- `--check` 时比较 `result_hash`，不一致返回 6（`REPLAY_MISMATCH`）。
- `--base-dir` 用于解析 manifest 中的相对输入路径。

---

## 13. JSON schema 与示例

### 13.1 `gseek.alignment/1`（节选）

```json
{
  "schema": "gseek.alignment/1",
  "backend": "minimap2",
  "params": {"preset": "asm5"},
  "stats": {
    "record_count": 1,
    "query_count": 1,
    "target_count": 1,
    "queries_with_hits": 1,
    "targets_with_hits": 1,
    "aligned_bases": 480,
    "runtime_s": 0.42,
    "truncated": false
  },
  "inputs": [
    {
      "path": "sample.fna",
      "size": 52341,
      "sha256": "…",
      "kind": "fasta",
      "records": 12,
      "compressed": false
    }
  ],
  "targets": [
    {
      "path": "alleles.fna",
      "size": 780123,
      "sha256": "…",
      "kind": "fasta",
      "records": 2400,
      "compressed": false
    }
  ],
  "index": {
    "key": "…",
    "adapter": "minimap2",
    "adapter_version": "1",
    "entry": "targets.mmi",
    "source_sha256": "…",
    "artifacts": ["targets.mmi"],
    "params": {"preset": "asm5"},
    "created_at": "2026-10-22T12:00:00Z",
    "cache_hit": true,
    "tool_versions": {"minimap2": "2.30-r1287"}
  },
  "records": [
    {
      "query_id": "contig_1",
      "target_id": "abcZ_12",
      "query_length": 5000,
      "target_length": 480,
      "query_start": 1000,
      "query_end": 1480,
      "target_start": 0,
      "target_end": 480,
      "strand": "+",
      "identity": 100.0,
      "aligned_length": 480,
      "matches": 480,
      "mismatches": 0,
      "gap_count": 0,
      "gap_openings": 0,
      "evalue": 0.0,
      "bit_score": 890.0,
      "mapq": 60,
      "depth": null,
      "score": 100.0,
      "query_coverage": 0.096,
      "target_coverage": 1.0,
      "ungapped_query_coverage": 0.096,
      "ungapped_target_coverage": 1.0,
      "query_aligned": null,
      "target_aligned": null,
      "target_description": null,
      "is_primary": true,
      "is_supplementary": false,
      "tags": {"identity_method": "paf_nm"}
    }
  ],
  "warnings": [],
  "errors": []
}
```

> 示例中的 `…` 表示省略的 64 位十六进制哈希；实际输出始终是完整 SHA-256。

### 13.2 `gseek.distance/1`（节选）

```json
{
  "schema": "gseek.distance/1",
  "backend": "mash",
  "params": {"k": 21, "sketch_size": 1000},
  "stats": {"record_count": 1, "query_count": 1, "reference_count": 1, "runtime_s": 0.1},
  "inputs": [],
  "references": [],
  "index": null,
  "records": [
    {
      "query_id": "sample.fna",
      "reference_id": "GCF_000006945.2",
      "distance": 0.0021,
      "pvalue": 1e-12,
      "shared_hashes": 990,
      "total_hashes": 1000,
      "containment": null,
      "jaccard": null,
      "ani": null,
      "aligned_fraction": null,
      "tags": {"metric": "mash_distance"}
    }
  ],
  "warnings": [],
  "errors": []
}
```

### 13.3 `gseek.run/1`（节选）

```json
{
  "schema": "gseek.run/1",
  "run_id": "8f0c…",
  "recipe_hash": "4a7d…",
  "created_at": "2026-10-22T12:00:00Z",
  "gseek_version": "0.3.1",
  "python_version": "3.12.15",
  "platform": "Linux-6.12-x86_64",
  "hostname": "compute-01",
  "backend": "minimap2",
  "backend_version": "1",
  "tools": [
    {"name": "minimap2", "version": "2.30-r1287", "path": "/usr/bin/minimap2"}
  ],
  "inputs": [],
  "targets": [],
  "index": null,
  "params": {"preset": "asm5"},
  "commands": [
    {
      "phase": "align",
      "label": "minimap2 align",
      "argv": ["minimap2", "-x", "asm5", "…"],
      "cwd": "/tmp/gseek-…",
      "exit_code": 0,
      "duration_s": 0.42,
      "timed_out": false,
      "stderr_tail": "",
      "stdout_path": null
    }
  ],
  "outputs": [],
  "env": {"PATH": "…", "LC_ALL": "C"},
  "status": "ok",
  "duration_s": 0.5,
  "warnings": [],
  "errors": []
}
```

### 13.4 `gseek.error/1`

```json
{
  "schema": "gseek.error/1",
  "code": "CONFIG_ERROR",
  "message": "unknown backend 'blast'; available: <none>",
  "context": {"backend": "blast"},
  "hint": ""
}
```

---

## 14. 稳定性、版本与限制

### 14.1 版本策略

- 包版本遵循 SemVer（当前 `0.3.1`，alpha）。
- schema 版本独立：`gseek.alignment/1` 等；破坏性字段变更必须升 schema 版本。
- 1.0 前公共 API 仍可能调整，但适配器协议与结果模型会尽量保持稳定。

### 14.2 当前限制

- 内置适配器目前有 **BLAST+（核酸/蛋白）、minimap2、KMA、MUMmer4、BWA、mash、
  sourmash、skani**。
- BLAST 支持 `blastn`/`megablast`/`dc-megablast`/`blastp`/`blastx`/`tblastn`/`tblastx`；
  蛋白索引需要 `task=blastp|blastx` 或显式 `dbtype=prot`。
- minimap2 必须显式提供 `preset`（如 `asm5`、`sr`、`map-ont`），不会静默选择。
- KMA 必须有 target 索引，`index_policy=none` 不支持；paired 只支持 FASTQ。
- MUMmer 仅支持 **MUMmer 4.x**（最低 4.0.0）；无持久索引，使用 nucmer + show-coords。
- BWA 必须有 target 索引；SAM 解析保留 secondary/supplementary 标记，默认跳过 secondary。
- mash 的 ID 使用输入文件名；sourmash 优先签名 `name`，其次是像基因组文件的
  `filename`，最后用唯一 `md5`（合并 `.sig` 中 `filename` 相同，不能作为唯一 ID）。
  原始路径或签名标签保留在 `tags`。
  距离/containment 语义记录在 `tags["metric"]`；mash 支持 `params={"mode":"screen"}`。
- 没有 `run_many`/内置批量调度；多后端编排由调用方负责。
- 集成测试需要外部工具；默认单元测试不依赖网络/二进制。
- 暂不支持 Windows 原生；目标 Linux/macOS。

### 14.3 相关文档

- [开发计划](../dev/development-plan.md) — 里程碑、架构、风险、开放决策
- [适配器矩阵](../dev/adapter-matrix.md) — 每个工具的命令/字段/版本/坑
- [兼容性迁移](../dev/compatibility.md) — gside/gapit/gmlst 字段映射与 parity
- [研究文档](../dev/research.md) — 三工具现有实现对比
- [ADR-0001](../dev/decisions/0001-mummer4.md) — MUMmer4 选型
- [ADR-0002](../dev/decisions/0002-project-rename.md) — gcore → gseek 更名
- [AGENTS.md](../AGENTS.md) — TDD 与架构红线
