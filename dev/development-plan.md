# gseek 开发计划

> 版本：0.1（设计稿）  
> 日期：2026-10-22  
> 目标读者：gseek 维护者、gside/gapit/gmlst 维护者  
> 配套文档：[API 参考](../docs/api.md) · [三工具调研](research.md) · [适配器矩阵](adapter-matrix.md) · [兼容性迁移](compatibility.md)

## 1. 摘要

`gseek` 是一个 **Python ≥ 3.12** 的 DNA 比对基础设施库，用一层稳定的抽象把 BLAST+、minimap2、MUMmer、KMA、BWA、mash/sourmash 等外部工具封装成统一接口，并提供：

1. **统一抽象**：能力声明、请求/计划/解析三阶段、适配器注册表。
2. **适配器**：每个工具一个纯规划/纯解析的适配器，不直接操作子进程。
3. **结果模型**：无损、可序列化、坐标与 identity/coverage 语义明确的 `AlignmentRecord` / `DistanceRecord`。
4. **可复现执行层**：内容寻址索引、run manifest、recipe hash、verify/replay、稳定排序。

它不负责 MLST 分型、物种鉴定、抗性基因判定等生物学结论；这些留在 gside/gapit/gmlst。`gseek` 只回答“谁比到了谁、在哪里、有多像、用了什么工具和参数”。

首版范围是 **BLAST+（核酸）与 minimap2** 的完整纵切，随后补 KMA、MUMmer4、BWA、mash/sourmash。计划总工期约 7–10 周（单人，含迁移缓冲）。

### 1.1 当前实现状态（2026-10-22）

| 项目 | 状态 |
| --- | --- |
| 项目名 | **gseek**（原 gcore，ADR-0002） |
| MUMmer 目标 | **MUMmer 4.x**（ADR-0001） |
| TDD 流程 | 完成：`AGENTS.md` + 身份契约测试 red→green |
| 工程骨架（pyproject/pixi/CI/MIT/AGENTS/CONTRIBUTING/CHANGELOG/pre-commit） | 完成 |
| 结果模型（alignment/distance/run） | 完成 |
| 执行层（命令计划/超时/流式/版本探测） | 完成 |
| 内容寻址索引（锁/staging/原子发布/GC） | 完成 |
| Provenance（SHA-256/recipe/manifest verify） | 完成 |
| 适配器协议、注册表、Runner、公共 API、CLI | 完成 |
| M1 适配器：BLAST+ / minimap2 | **适配器级完成**（真实工具集成测试通过） |
| M2 适配器：KMA / MUMmer4 | **适配器级完成**（真实工具集成测试通过） |
| M3 适配器：BWA/mash/sourmash | **适配器级完成**（真实工具集成测试通过） |
| M4 适配器：蛋白 BLAST/sourmash gather/skani/replay | **完成**（真实工具集成测试通过） |
| 消费者 parity 迁移（gmlst/gapit/gside） | 进行中：gside 分支已完成；gmlst/gapit 未开始（P1/P2） |
| 测试 | 322 个单元/契约 + 36 个集成；覆盖率 88%；ruff + pyright 全绿 |

当前包可以被 `import gseek`、安装，并已通过 8 个后端跑通真实工具，已实现 `replay`；
0.4.0 已发布 PyPI（sourmash rocksdb 支持）。下一步是 gmlst/gapit parity。

## 2. 背景与问题

### 2.1 现状

三个工具各自维护比对层：

- **gside**：BLAST+、minimap2、mash、sourmash、skani。
- **gapit**：BLAST+、minimap2。
- **gmlst**：BLAST+、KMA、minimap2、MUMmer4。

重复的代码与分歧包括：

- 三套子进程封装、三套工具发现、三套错误模型。
- BLAST outfmt 字段分别为 12/14/15 列，coverage 公式三种。
- minimap2 identity 公式三种（`nmatch/(nmatch+NM)`、`(alen-NM)/alen`、`nmatch/alen`）。
- 坐标约定混用 1-based inclusive、0-based half-open，MUMmer 反向命中的 strand 甚至被硬编码为 `+`。
- 索引失效依赖 mtime/文件存在性，缺少内容哈希与并发锁。
- 只有 gapit 建库时记录工具版本；没有统一的单次运行 manifest，无法回放。
- 黄金测试分散在三个仓库，无法交叉验证。

详细代码级证据见 [research.md](research.md)。

### 2.2 为什么现在做

- cgMLST scheme 越来越大，重复建索引的成本和并发冲突开始显现。
- gside/gapit/gmlst 都在增加后端；每加一个工具就要重复实现命令、解析、错误、缓存。
- Agent/工作流场景要求可复现：同一输入、同一参数、同一工具版本，必须能解释和重放。
- BWA 与 MinHash 是三个工具尚未覆盖但社区常用的能力，适合在统一层一次补齐。

## 3. 目标与非目标

### 3.1 目标

| 编号 | 目标 | 验收标准 |
| --- | --- | --- |
| G1 | 统一后端抽象 | 新增一个比对工具只需实现 1 个适配器模块，不改核心 |
| G2 | 结果无损且语义明确 | 坐标/identity/coverage/strand 有文档和单元测试；原始 tags 保留 |
| G3 | 可复现 | 每次运行产出 `gseek.run/1` manifest；相同 recipe 可 verify/replay |
| G4 | 索引可复用 | 内容寻址缓存；并发安全；跨进程复用；不依赖 mtime |
| G5 | 三工具可迁移 | gside/gapit/gmlst 的现有黄金输出在迁移后保持 parity |
| G6 | 零强制运行时依赖 | 核心仅用 stdlib；外部工具通过 PATH/显式路径发现 |
| G7 | 可扩展 | 第三方可通过 entry point 注册适配器；API 版本化 |
| G8 | 性能不退化 | 同等输入/线程下，gseek 编排开销 < 5%；索引复用后更快 |

### 3.2 非目标

- 不实现 MLST/cgMLST 的 allele calling、ST 查找、novel allele 判定。
- 不实现物种鉴定规则、ANI 阈值、抗性基因数据库语义。
- 不实现比对算法本身（不做纯 Python BLAST/MinHash 重实现）。
- 不绑定特定数据库/provider（PubMLST、CARD、GTDB 等）。
- 不替代 samtools/bcftools 的 BAM/VCF 处理。
- 第一版不支持 Windows 原生；以 Linux/macOS 为主。

## 4. 设计原则

1. **适配器是纯函数式的规划器/解析器**：不启动进程、不写全局状态；执行由 `Executor` 统一负责。
2. **无损优先**：适配器不丢弃行、不做业务去重；归并是显式视图。
3. **坐标统一**：0-based half-open、正向序列坐标、strand 单独字段。
4. **语义可解释**：identity/coverage 不是“一个数字”，而是带来源和公式的字段。
5. **内容寻址**：索引 key 由内容哈希 + 语义参数 + 工具版本决定，不用 mtime。
6. **默认确定性**：稳定排序、`LC_ALL=C`、绝对路径、无 shell、显式版本。
7. **零依赖核心**：stdlib only；可选功能用 extras。
8. **错误可诊断**：类型化错误 + code + context + hint + exit code。
9. **小步迁移**：消费者通过 feature flag 切换，黄金测试护航，不做大爆炸重写。
10. **文档即契约**：适配器矩阵、schema、迁移映射与代码同步更新。

## 5. 总体架构

```text
┌────────────────────────────────────────────────────────────────────┐
│                         消费者 / CLI                               │
│   gside            gapit             gmlst          gseek CLI      │
└───────────────┬───────────────┬───────────────┬────────────────────┘
                │               │               │
                ▼               ▼               ▼
┌────────────────────────────────────────────────────────────────────┐
│                         gseek.api                                  │
│  align() / distance() / get_backend() / backends()                 │
│  AlignmentResult / DistanceResult / views                          │
└───────────────────────────────┬────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌───────────────┐     ┌─────────────────┐     ┌─────────────────────┐
│   Registry    │     │     Runner      │     │   Views / Summaries │
│ 能力匹配/别名 │     │ plan→exec→parse │     │ best-per / breadth  │
└───────┬───────┘     └────────┬────────┘     └─────────────────────┘
        │                      │
        ▼                      ▼
┌───────────────┐     ┌─────────────────┐     ┌─────────────────────┐
│   Adapters    │     │   Executor      │     │   IndexStore        │
│ blast         │     │ tool resolver   │     │ content-addressed   │
│ minimap2      │     │ subprocess      │     │ locks / atomic      │
│ mummer        │     │ timeout/stream  │     │ GC                  │
│ kma           │     │ env/cwd/argv    │     └─────────────────────┘
│ bwa           │     └────────┬────────┘
│ mash/sourmash │              │
└───────┬───────┘              ▼
        │              ┌─────────────────┐     ┌─────────────────────┐
        └─────────────▶│  Result model   │◀────│   Provenance        │
                       │ records/results │     │ manifest/recipe     │
                       └─────────────────┘     └─────────────────────┘
```

依赖方向严格向下：

```text
api → runner → adapters → execution / index / io / model
                  │
                  └→ capabilities / errors
```

`model` 不依赖任何运行时模块；`adapters` 不依赖 `runner`；`execution` 不依赖 `adapters`。

### 5.1 模块布局

```text
src/gseek/
├── __init__.py          # 公共 API 导出
├── __main__.py          # python -m gseek
├── api.py               # align() / distance() / get_backend() / backends()
├── capabilities.py      # InputKind/OutputKind/IndexScope/Capabilities/BackendSpec
├── registry.py          # 注册表、别名、entry point、能力查询
├── runner.py            # plan → execute → parse → result/manifest
├── adapter.py           # Adapter 协议、Request/Plan/ParseContext
├── execution.py         # CommandPlan/ProcessResult/ToolResolver/Executor/workspace
├── index.py             # IndexKey/IndexStore/IndexPolicy/锁/原子发布/GC
├── provenance.py        # hash/canonical json/manifest/recipe/verify
├── io.py                # FASTA/FASTQ、合并、格式检测、原子写
├── model/
│   ├── __init__.py
│   ├── common.py        # Interval/InputRef/ToolRef/Issue/schema 常量
│   ├── alignment.py     # AlignmentRecord/AlignmentStats/AlignmentResult/views
│   ├── distance.py      # DistanceRecord/DistanceStats/DistanceResult
│   └── run.py           # CommandRecord/IndexRef/RunManifest
├── adapters/
│   ├── __init__.py      # 内置适配器注册
│   ├── base.py          # BaseAdapter/共享 helper
│   ├── blast.py
│   ├── minimap2.py
│   ├── mummer.py
│   ├── kma.py
│   ├── bwa.py
│   ├── mash.py
│   └── sourmash.py
├── views.py             # best_per/dedupe/union breadth/TSV/JSONL
├── cli.py               # backends/doctor/index/run/verify/replay
└── py.typed
```

单文件超过 ~350 行时按职责拆分（与三个消费者现有 250 LOC 风格一致）。

## 6. 公共 API

### 6.1 函数式入口

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

### 6.2 对象式入口

```python
backend = gseek.get_backend("blast", task="blastn")
probe = backend.probe()          # ToolchainProbe
result = backend.align(
    query=Path("genome.fna"),
    target=[Path("genes.fna")],
    params={"evalue": 1e-20, "max_targets": 10_000},
)
```

### 6.3 距离入口

```python
result = gseek.distance(
    backend="mash",
    query=Path("genome.fna"),
    reference=[Path("sketches/ref.msh")],
    params={"k": 21, "sketch_size": 1000},
)
```

### 6.4 注册表

```python
gseek.backends()                       # tuple[BackendSpec, ...]
gseek.find_backends(input="fastq", output="alignment", paired=True)
gseek.register_adapter(MyAdapter)      # 第三方扩展
```

### 6.5 消费者适配层（不在 gseek 核心）

消费者自己写薄映射，例如 gmlst：

```python
from gseek import align

def run_blastn(sample, allele_fastas, loci, threads):
    result = align(
        backend="blast",
        query=sample,                 # genome 作为 query
        target=allele_fastas,         # alleles 作为 target（可缓存索引）
        params={"evalue": 1e-20, "dust": False},
        threads=threads,
    )
    return [
        AlleleMatch(
            locus=split_id(r.target_id)[0],
            allele_id=split_id(r.target_id)[1],
            identity=r.identity,
            coverage=r.target_coverage,
            strand=r.strand,
            query_contig=r.query_id,
            query_start=r.query_start,
            query_end=r.query_end,
            allele_start=r.target_start,
            allele_end=r.target_end,
            sequence=r.query_aligned,
            alignment_length=r.aligned_length,
        )
        for r in result.records
    ]
```

**方向说明**：gmlst 目前是 “alleles 作 query、genome 作 target”，以便 `-subject` 免建库。`gseek` 默认把 **target 作为可缓存索引**，因此迁移后是 “genome 作 query、alleles 作 target”，字段映射到 `target_coverage` / `target_start`。这不会改变命中集合，但会改变记录的方向；parity 测试必须覆盖。

## 7. 核心抽象

### 7.1 能力声明

```python
class InputKind(StrEnum):
    FASTA = "fasta"
    FASTQ = "fastq"

class OutputKind(StrEnum):
    ALIGNMENT = "alignment"
    DISTANCE = "distance"

class IndexScope(StrEnum):
    NONE = "none"
    TARGET = "target"
    QUERY = "query"
    BOTH = "both"

@dataclass(frozen=True, slots=True)
class Capabilities:
    inputs: frozenset[InputKind]
    outputs: frozenset[OutputKind]
    molecule: frozenset[Molecule] = frozenset({Molecule.DNA})
    paired: bool = False
    index_scope: IndexScope = IndexScope.NONE
    streaming: bool = False
    threads: bool = True
    presets: tuple[str, ...] = ()
    deterministic: bool = True

@dataclass(frozen=True, slots=True)
class ToolRequirement:
    binary: str
    role: str                       # aligner/indexer/helper
    version_args: tuple[str, ...]
    version_pattern: str | None
    min_version: tuple[int, ...] | None = None
    optional: bool = False
    hint: str = ""

@dataclass(frozen=True, slots=True)
class BackendSpec:
    name: str
    family: str
    summary: str
    capabilities: Capabilities
    tools: tuple[ToolRequirement, ...]
    aliases: tuple[str, ...] = ()
    api_version: str = "1"
    params_schema: Mapping[str, JsonValue] = field(default_factory=dict)
```

`BackendSpec` 是注册表和 CLI 的唯一事实来源；适配器实例只持有配置。

### 7.2 三阶段适配器

```python
@runtime_checkable
class Adapter(Protocol):
    spec: BackendSpec

    def probe(self, resolver: ToolResolver) -> ToolchainProbe: ...

    def plan_index(
        self, request: IndexRequest, ctx: PlanContext
    ) -> IndexPlan | None: ...

    def plan_align(
        self, request: AlignRequest, index: IndexRef | None, ctx: PlanContext
    ) -> AlignPlan: ...

    def parse_align(
        self, ctx: ParseContext, stream: Iterable[str] | None = None
    ) -> Iterator[AlignmentRecord]: ...
```

**为什么是三阶段：**

- `probe`：只做版本/路径检查，可在 `gseek doctor` 中批量调用。
- `plan_index`：返回要执行的命令和期望产物；Runner 负责执行、校验、发布。
- `plan_align`：返回命令计划；Runner 负责在 workspace 中执行。
- `parse_align`：纯解析，输入是文件或 stdout 行迭代器；便于单测。

适配器**不**直接调用 `subprocess`，也**不**读写全局缓存。这样：

- 单元测试可以用 fake executor 断言 argv；
- 同一份原始输出可以在不装工具的情况下做 golden test；
- 执行层的超时、环境、日志、manifest 统一。

### 7.3 请求与计划

```python
@dataclass(frozen=True, slots=True)
class AlignRequest:
    query: tuple[Path, ...]              # 1 个或一对（paired）
    target: tuple[Path, ...]
    query_kind: InputKind | None = None  # None = 自动检测
    paired: bool = False
    params: Mapping[str, JsonValue] = field(default_factory=dict)
    threads: int = 1
    timeout_s: float | None = None
    index_policy: IndexPolicy = IndexPolicy.AUTO
    target_filter: frozenset[str] | None = None
    work_dir: Path | None = None
    keep_work: bool = False

@dataclass(frozen=True, slots=True)
class IndexRequest:
    target: tuple[Path, ...]
    params: Mapping[str, JsonValue]
    threads: int = 1
    force: bool = False

@dataclass(frozen=True, slots=True)
class IndexPlan:
    commands: tuple[CommandPlan, ...]
    entry: str                    # 相对索引目录的入口（前缀/文件）
    artifacts: tuple[str, ...]    # 相对 glob
    params: Mapping[str, JsonValue] = ...

@dataclass(frozen=True, slots=True)
class AlignPlan:
    commands: tuple[CommandPlan, ...]
    stream: bool = False          # 最后一条命令的 stdout 作为解析输入
    result_files: tuple[Path, ...] = ()
    extra: Mapping[str, JsonValue] = ...
```

`CommandPlan` 在 8.1 定义。`stream=True` 时 Runner 把最后一条命令的 stdout 行迭代器传给 `parse_align`；否则解析器读取 `result_files`。

### 7.4 Runner 流程

```text
align(request, backend):
  1. 选择适配器（显式 name 或能力匹配）
  2. probe 工具链，失败则 ToolNotFound/ToolVersionError
  3. 计算输入/目标 InputRef（大小 + sha256 + 格式）
  4. index:
       - scope=NONE → 无索引，target 直接进命令
       - scope=TARGET → IndexStore.lookup(key)
       - miss → 在 staging 目录执行 IndexPlan → 校验 artifacts
                → 原子发布 → IndexRef(cache_hit=False)
  5. 创建 workspace
  6. plan_align → 执行命令（或流式）
  7. parse_align → 规范化记录
  8. 稳定排序 + 统计
  9. 构建 RunManifest（index/align 两个阶段的 CommandRecord）
 10. 可选写 manifest；返回 AlignmentResult
```

Runner 是唯一知道“索引/执行/manifest 如何组合”的地方；适配器不需要重复这些逻辑。

## 8. 可复现执行层

### 8.1 命令与执行

```python
@dataclass(frozen=True, slots=True)
class CommandPlan:
    argv: tuple[str, ...]
    label: str = ""
    stdin: Path | None = None
    stdout: Path | None = None
    stderr: Path | None = None
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float | None = None
    expected_codes: frozenset[int] = frozenset({0})
```

规则：

- 永远 `shell=False`；argv 逐项传入。
- 所有输入/输出路径在规划时绝对化；工具路径由 Runner 在 probe 后绑定到 Executor，
  执行前替换为解析出的绝对路径。输出路径必须在 workspace/index staging 内。
- 默认环境：继承 + `LC_ALL=C` + `LANG=C` + `TZ=UTC`；可通过 `deterministic_env=False` 关闭。
- 超时：`Popen` + 进程组 kill；`start_new_session=True`。
- stderr：后台线程持续排空，避免 64KB 管道阻塞；保留尾部 N KB。
- stdout：可重定向文件、捕获为文本、或流式逐行。
- 失败：抛 `ExecutionError(returncode, argv, stderr_tail, timed_out)`。
- `CommandRecord` 记录 label、argv、cwd、exit code、耗时、stderr 尾部、timed_out。

### 8.2 ToolResolver

```python
resolver = ToolResolver(
    overrides={"blastn": Path("/opt/blast/bin/blastn")},
    extra_paths=[Path("/opt/conda/bin")],
)
resolver.resolve("blastn")
resolver.probe(ToolRequirement("blastn", "aligner", ("-version",), r"blastn:\s*(\d+)\.(\d+)\.(\d+)"))
```

优先级：

1. 显式 `overrides`；
2. 环境变量 `GSEEK_<BINARY>`（例如 `GSEEK_BLASTN`）；
3. `GSEEK_PATH_PREPEND` + `PATH`；
4. 可选 `extra_paths`（消费者可传 pixi/conda bin）。

版本解析结果缓存（进程内），key = 解析后的二进制路径 + version argv。

### 8.3 内容寻址索引

```text
<GSEEK_CACHE_DIR or ~/.cache/gseek>/
└── index/
    └── <adapter>/
        └── <key>/
            ├── index.json        # gseek.index/1，最后写入
            ├── _READY            # 可选标记
            └── ...               # 工具产物
```

`key = sha256(canonical_json({
  "schema": "gseek.index-key/1",
  "adapter": "blast",
  "adapter_version": "1",
  "format_version": "1",
  "params": {...语义参数...},
  "targets": [{"name": "...", "sha256": "..."}],
  "tools": [{"name": "makeblastdb", "version": "2.17.0+"}]
}))`

规则：

- 只纳入影响索引内容的参数；`threads`、`timeout`、`debug` 不纳入。
- target 先由 `io.merge_fasta` 规范化，再哈希；顺序稳定、ID 校验。
- 构建在 `<key>.staging.<pid>.<uuid>` 中进行；成功后 `os.replace`/`rename` 发布。
- `<key>.lock` 使用 `fcntl.flock`；等待超时抛 `IndexLockTimeout`。
- 发布后再次校验 `index.json` + artifacts + key；损坏则删除并重建。
- `IndexRef` 记录 key、adapter、entry、artifacts、source sha256、params、created_at、cache_hit。
- 不依赖 mtime；升级工具/参数/目标内容都会得到新 key。
- GC：按 LRU + 总大小上限清理；默认不自动删除，提供 `gseek index gc`。

### 8.4 Run manifest

Schema：`gseek.run/1`。示例：

```json
{
  "schema": "gseek.run/1",
  "run_id": "01J...",
  "recipe_hash": "sha256:...",
  "created_at": "2026-10-22T13:00:00Z",
  "gseek_version": "0.4.0",
  "python_version": "3.12.11",
  "platform": "Linux-6.12-x86_64",
  "hostname": "compute-01",
  "backend": "minimap2",
  "backend_version": "1",
  "tools": [
    {"name": "minimap2", "path": "/usr/bin/minimap2", "version": "2.30-r1287", "role": "aligner"}
  ],
  "inputs": [
    {"path": "sample.fna", "sha256": "...", "size": 12345, "kind": "fasta", "records": 12}
  ],
  "targets": [
    {"path": "alleles.fna", "sha256": "...", "size": 67890, "kind": "fasta", "records": 2400}
  ],
  "index": {
    "key": "...",
    "adapter": "minimap2",
    "entry": "targets.mmi",
    "cache_hit": true,
    "artifacts": ["targets.mmi"],
    "source_sha256": "...",
    "params": {"preset": "asm5"}
  },
  "params": {"preset": "asm5", "secondary": "no"},
  "commands": [
    {"phase": "align", "label": "minimap2 align", "argv": ["minimap2", "-x", "asm5", "..."], "cwd": "/tmp/gseek-...", "exit_code": 0, "duration_s": 1.23, "timed_out": false, "stderr_tail": ""}
  ],
  "outputs": [
    {"path": "runs/sample.jsonl", "sha256": "...", "size": 4567}
  ],
  "env": {"LC_ALL": "C", "TZ": "UTC"},
  "status": "ok",
  "duration_s": 2.34,
  "warnings": [],
  "errors": []
}
```

### 8.5 recipe hash

`recipe_hash` 是“可复现身份”，只包含语义输入：

```text
sha256(canonical_json({
  "schema": "gseek.recipe/1",
  "gseek_version": "0.4.0",
  "backend": "minimap2",
  "adapter_version": "1",
  "params": {...},
  "inputs": [{"sha256": "...", "kind": "fasta"}],
  "targets": [{"sha256": "...", "kind": "fasta"}],
  "index_key": "...",
  "tools": [{"name": "minimap2", "version": "2.30-r1287"}]
}))
```

排除：时间戳、hostname、run_id、workspace、线程数、超时、日志路径、输出路径。  
包含：线程数以外的所有影响结果的工具参数；如果某参数在某些工具中影响结果（如 BWA `-t` 不影响），由适配器在 `semantic_params` 中显式声明。

### 8.6 verify / replay

- `gseek verify run.json`：重算输入/目标/输出 sha256，检查工具版本是否仍匹配；不重跑。
- `gseek replay run.json --check`：按 manifest 中的 backend/params/inputs 重建命令并重跑；比较规范化记录的 `result_hash`。
- **安全**：replay **不执行 manifest 里的任意 argv**。它只把 manifest 当作 recipe，重新走 adapter 的 `plan_*`，因此恶意 manifest 不能注入命令。
- `--check` 失败时输出差异摘要（记录数、新增/缺失 key、字段差异），退出码 6。

### 8.7 确定性清单

| 来源 | 处理 |
| --- | --- |
| 线程数 | 记录，不纳入 recipe hash；结果稳定排序 |
| 工具版本 | 纳入 recipe hash + index key |
| 环境变量 | 只记录 allowlist（`PATH`, `LD_LIBRARY_PATH`, `CONDA_PREFIX`, `GSEEK_*`） |
| locale | 强制 `LC_ALL=C` |
| 时间 | UTC ISO8601，仅 manifest |
| 浮点 | 保留原始；JSON 序列化不截断；展示层统一舍入 |
| 输出顺序 | 适配器不承诺；结果按稳定键排序 |
| 临时路径 | 不进入 recipe hash；manifest 记录 cwd 仅用于诊断 |
| 工具内部随机性 | 记录 seed（若工具支持）；在文档中标注不可控项 |

## 9. 结果模型

### 9.1 AlignmentRecord

```python
@dataclass(frozen=True, slots=True)
class AlignmentRecord:
    query_id: str
    target_id: str
    query_length: int | None = None
    target_length: int | None = None
    query_start: int | None = None      # 0-based half-open
    query_end: int | None = None
    target_start: int | None = None
    target_end: int | None = None
    strand: Strand = Strand.PLUS
    identity: float | None = None       # 0..100
    aligned_length: int | None = None
    matches: int | None = None
    mismatches: int | None = None
    gap_count: int | None = None
    gap_openings: int | None = None
    evalue: float | None = None
    bit_score: float | None = None
    mapq: int | None = None
    depth: float | None = None
    score: float | None = None
    query_aligned: str | None = None
    target_aligned: str | None = None
    target_description: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)
```

派生属性：

- `query_interval` / `target_interval`（`Interval | None`）
- `query_coverage` / `target_coverage`（区间跨度 / 长度）
- `ungapped_query_coverage` / `ungapped_target_coverage`
- `identity_fraction`
- `is_primary`（从 tags 或显式字段）
- `is_reverse`

不变式：

- `identity` 在 `0..100`；`None` 表示工具未提供。
- 区间要么同时为 `None`，要么 `0 <= start <= end`。
- 长度非负。
- 不强制区间必须落在长度内（某些工具会报告超出/裁剪坐标），但会在 `tags` 中保留原始值。

### 9.2 结果与视图

```python
@dataclass(frozen=True, slots=True)
class AlignmentResult:
    backend: str
    records: tuple[AlignmentRecord, ...]
    inputs: tuple[InputRef, ...]
    targets: tuple[InputRef, ...]
    params: Mapping[str, JsonValue]
    stats: AlignmentStats
    index: IndexRef | None = None
    manifest: RunManifest | None = None
    warnings: tuple[Issue, ...] = ()
    errors: tuple[Issue, ...] = ()
```

视图（`gseek.views`）：

| 视图 | 用途 | 对应消费者 |
| --- | --- | --- |
| `best_per_query_target()` | 每个 `(query,target)` 最佳行 | gmlst `select_best_matches` |
| `primary_only()` | 过滤 secondary/supplementary | gapit/gside |
| `summarize_by_target()` | 区间并集 breadth、加权 identity、depth | gside reads、gapit coverage |
| `summarize_by_query()` | 每个 query 的命中目标 | 诊断 |
| `deduplicate(key=...)` | 显式去重 | gapit `(qseqid,qstart,qend)` |
| `to_tsv()` / `to_jsonl()` / `to_blast6()` / `to_paf()` | 导出 | CLI/测试 |

`summarize_by_target()` 的 breadth 使用**半开区间并集**，与 gside/gapit 的 union 算法一致；identity 使用 `aligned_length` 加权平均；`depth` 使用记录中 depth 的加权平均。

### 9.3 距离模型

```python
@dataclass(frozen=True, slots=True)
class DistanceRecord:
    query_id: str
    reference_id: str
    distance: float | None = None
    pvalue: float | None = None
    shared_hashes: int | None = None
    total_hashes: int | None = None
    containment: float | None = None
    jaccard: float | None = None
    ani: float | None = None
    aligned_fraction: float | None = None
    tags: Mapping[str, str] = field(default_factory=dict)
```

`DistanceResult` 与 `AlignmentResult` 同构，但 stats 只统计 query/reference 数量和距离分布。

### 9.4 序列化

- `to_dict()`：返回纯 JSON 可序列化对象；键顺序稳定。
- `to_jsonl()`：第一行 header（schema/backend/params/stats），随后每条记录一行，最后 footer（可选）。
- `to_tsv()`：列由调用方选择，默认稳定列集。
- schema 版本独立于包版本：`gseek.alignment/1`、`gseek.distance/1`、`gseek.run/1`、`gseek.index/1`、`gseek.error/1`。
- 破坏性字段变更必须升 schema 版本，并提供 `gseek.migrate` 或兼容读取。

## 10. 适配器路线图

详细命令/字段见 [adapter-matrix.md](adapter-matrix.md)。摘要：

| 阶段 | 适配器 | 关键能力 | 主要消费者 |
| --- | --- | --- | --- |
| M1 | BLAST+（blastn） | target 索引、tabular 解析、流式、蛋白模式预留 | 三者 |
| M1 | minimap2 | PAF、FASTA/FASTQ、paired、presets、target 索引 | 三者 |
| M2 | KMA | `.res`、FASTA/FASTQ、paired、无坐标 | gmlst |
| M2 | MUMmer4/nucmer | 多步命令、coords、无索引、最低 4.0.0 | gmlst |
| M3 | BWA | SAM、paired、target 索引 | 新增 |
| M3 | mash | sketch/dist/screen、距离模型 | gside |
| M3 | sourmash | sketch/search、CSV、距离模型 | gside |
| M4 | BLAST 蛋白模式、sourmash gather、skani | 补齐 gside 路径 | gside |
| M5 | bwa-mem2、minimap2 SAM、其他 | 社区需求 | 可选 |

### 10.1 适配器实现模板

```python
class BlastAdapter:
    spec = BackendSpec(
        name="blast",
        family="blast",
        summary="NCBI BLAST+ nucleotide/protein alignment",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
            index_scope=IndexScope.TARGET,
            streaming=True,
            threads=True,
        ),
        tools=(
            ToolRequirement("blastn", "aligner", ("-version",), r"blastn:\s*(\d+)\.(\d+)\.(\d+)", (2, 2, 30)),
            ToolRequirement("makeblastdb", "indexer", ("-version",), r"makeblastdb:\s*(\d+)\.(\d+)\.(\d+)", (2, 2, 30)),
        ),
        aliases=("blastn", "blast+"),
    )

    def plan_index(self, request, ctx):
        merged = io.merge_fasta(request.target, ctx.workspace / "targets.fasta")
        cmd = CommandPlan(
            argv=("makeblastdb", "-in", str(merged), "-dbtype", "nucl", "-out", str(ctx.workspace / "targets")),
            label="makeblastdb",
        )
        return IndexPlan((cmd,), entry="targets", artifacts=("targets.n*",))

    def plan_align(self, request, index, ctx):
        out = ctx.workspace / "hits.tsv"
        argv = ("blastn", "-query", str(request.query[0]), "-db", str(ctx.index_dir / index.entry), ...)
        return AlignPlan((CommandPlan(argv=argv, stdout=out),), result_files=(out,))

    def parse_align(self, ctx, stream=None):
        yield from parse_blast6(ctx.plan.result_files[0], ...)
```

### 10.2 能力匹配

`registry.find_backends(input=..., output=..., paired=...)` 按能力过滤。`gseek.align(backend=None)` 的自动选择规则：

1. 如果只有一个候选，选它。
2. 如果多个候选，按 `BackendSpec.priority`（新增字段）或显式配置；**不做隐式“最快”猜测**，避免结果不可解释。
3. 无候选 → `UnsupportedFeature`，错误信息列出可用的输入/输出组合。

## 11. 包与工程配置

### 11.1 元数据

```toml
[project]
name = "gseek"          # 见 15.1：原 gcore 名称冲突，2026-10-22 更名
version = "0.4.0"
requires-python = ">=3.12"
dependencies = []            # 核心零依赖
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.6", "pyright>=1.1", "hypothesis>=6", "pre-commit>=3.7"]
docs = ["mkdocs>=1.6", "mkdocs-material>=9"]
```

- 构建后端：hatchling。
- `src/` 布局；`py.typed`。
- 入口：`gseek = "gseek.cli:main"`；`python -m gseek`。
- 插件入口组：`gseek.adapters`。
- 许可证：MIT（与三者一致）。

### 11.2 质量门禁

| 门禁 | 工具 | 阈值 |
| --- | --- | --- |
| 格式/静态检查 | ruff | `E,F,I,UP,B,SIM,C4,RET,RUF`；line-length 100 |
| 类型 | pyright standard（M1 起对 `src` 收紧到 strict） | 0 error |
| 单测 | pytest | 通过；M0 基线 ≥ 85%（当前 88%），M1 起目标 ≥ 90%（待补测试） |
| 集成 | pytest -m integration | 有工具的环境全过；无工具自动 skip |
| 文档 | mkdocs build | 0 warning |
| 打包 | `python -m build` + `twine check` | 通过 |

### 11.3 CI

- 单元测试矩阵：Python 3.12 / 3.13 / 3.14，Ubuntu + macOS。
- 静态检查：ruff + pyright。
- 集成测试：单独 job，使用 pixi 环境安装固定版本 BLAST+/minimap2/MUMmer/KMA/BWA/mash/sourmash。
- 可选 nightly：最新 bioconda 版本，提前发现版本漂移。
- Windows：暂不纳入；如需要，先支持 WSL。

## 12. 兼容与迁移

详细字段映射见 [compatibility.md](compatibility.md)。核心策略：

1. **先冻结行为**：gseek 已沉淀真实工具输出到 `tests/fixtures/`，并在 `tests/parity/`
   对照三个消费者的公式；消费者仓库自身的 golden 迁移仍待完成。
2. **feature flag**：消费者通过环境变量/构造函数切换到 gseek 适配器，保留旧路径。
3. **影子模式**：同一输入同时跑旧路径和 gseek，比较规范化结果；差异记录为 warning，不改变最终输出。
4. **逐后端切换**：BLAST → minimap2 → KMA → MUMmer → mash/sourmash。
5. **删除旧代码**：只有在 parity 连续两个 release 通过、且消费者集成测试全绿后才删除。
6. **Python 版本**：gseek 要求 ≥3.12，gapit 当前是 ≥3.11；迁移时 gapit 需提升到 3.12（或把 gseek 作为可选 extra，保持 3.11 兼容）。推荐统一到 3.12。

### 12.1 迁移顺序

| 阶段 | 消费者 | 后端 | 验收 |
| --- | --- | --- | --- |
| P1 | gmlst | blastn | `test_aligner_golden` 全过；真实样本 typing 结果一致 |
| P1 | gmlst | minimap2 | 同上；fragment/join 语义一致 |
| P2 | gapit | blastn | `tests/golden/*` 全过；abricate parity 不退化 |
| P2 | gapit | minimap2 | reads/cluster 黄金输出一致 |
| P2 | gside | blastn + minimap2 | marker/reads 黄金输出一致 |
| P3 | gmlst | kma + nucmer | 黄金输出一致；修复 nucmer strand bug |
| P3 | gside | mash + sourmash | ANI/sketch 黄金输出一致 |
| P4 | 三者 | 删除旧适配器 | 代码净减少；文档更新 |

### 12.2 兼容层

`gseek` 不提供 `AlleleMatch`/`Hit` 的历史类；消费者用一个薄 `compat.py` 做映射。这样可以避免把业务字段（locus、gene、database）塞进核心模型。

如果迁移成本过高，可提供 `gseek.compat.gmlst` 等**可选子模块**，但第一版不做。

## 13. 测试策略

### 13.1 TDD 工作流（强制）

所有生产代码变更遵循 Red-Green-Refactor：

1. **Red**：先写最小失败测试，运行并确认失败原因是“行为缺失”。
2. **Green**：写最少实现让目标测试通过。
3. **Refactor**：在绿的前提下重构。
4. **全量门禁**：`pixi run check`（ruff + pyright + pytest）。

具体规则、例外与适配器测试顺序见 [AGENTS.md](../AGENTS.md) §3。重命名/重构也必须先写新契约测试——本项目从 `gcore` 更名为 `gseek` 时，先写 `tests/test_project_identity.py` 并确认 6 个测试全红，再改包名与 schema。

### 13.2 测试金字塔

| 层级 | 内容 | 是否需要外部工具 |
| --- | --- | --- |
| 单元 | Interval/坐标、coverage/identity、模型校验、canonical JSON、hash、index key、lock | 否 |
| 解析 golden | 每个适配器一份真实输出 + 期望记录 | 否 |
| 计划 golden | 断言 argv、cwd、env、输出路径 | 否 |
| 集成 | 小样本真实运行，比较记录/统计 | 是（可 skip） |
| 消费者 parity | 同一原始输出/同一输入，与旧模型逐字段比较 | 部分 |
| 可复现 | 两次运行 recipe_hash/result_hash 一致；缓存命中；verify/replay | 完成 |
| 属性测试 | 坐标转换、区间并集、CIGAR 解析 | 否 |
| 性能 | 大 PAF/大 scheme 的内存/耗时基线 | 是 |

### 13.3 黄金 fixture 目录

```text
tests/
├── fixtures/
│   ├── blast/          # 12/14/15 列、反向、gapped、空结果、CRLF
│   ├── paf/            # primary/secondary、+/-、NM/cs/cg、paired
│   ├── kma/            # .res 各版本、空/缺文件
│   ├── mummer/         # coords 正/反向、MUMmer4 header
│   ├── sam/            # CIGAR 各种 op、FLAG、paired
│   └── minhash/        # mash dist/screen、sourmash search CSV
├── parity/
│   ├── gmlst/          # 从 test_aligner_golden 复制
│   ├── gapit/          # 从 tests/golden 复制
│   └── gside/          # 从 integration golden 复制
└── integration/
```

### 13.4 关键测试用例

- BLAST 反向命中：`sstart > send`，query 坐标不交换，target 坐标为正向。
- PAF `tp:A:S` 过滤；无 `NM` 时的 identity 回退。
- MUMmer `S2 > E2` 的 strand。
- KMA `.res` 的 `Query_Coverage > 100` 不被 clamp。
- SAM CIGAR `10M2I3D5M` 的 reference/query 跨度。
- 区间并集：重叠、相邻、包含、空集。
- 索引 key：同内容不同 mtime → 同 key；不同参数/工具版本 → 不同 key。
- 并发构建：两个进程同时请求同一 key，只构建一次。
- manifest：篡改输入/输出后 verify 失败。
- replay：恶意 argv 不被执行。
- 排序：不同线程数/不同输入顺序下结果规范一致。

## 14. 里程碑

> 估算按单人全职、熟悉代码库的前提；含自测和文档，不含跨团队等待。

### M0：设计与基座（3–4 人日）

- 完成 `research.md`、`development-plan.md`、`adapter-matrix.md`、`compatibility.md`。
- 初始化 `pyproject.toml`、`src/gseek`、`tests`、CI、pixi 任务。
- 实现 `model/`、`errors.py`、`io.py`、`execution.py`、`provenance.py`、`index.py` 的骨架与单测。
- 产出公共 API 的 import 稳定性检查。
- **验收**：`pytest`、`ruff`、`pyright` 全绿；包可 `pip install -e .`。

### M1：BLAST + minimap2 纵切（8–10 人日）— 适配器级完成

**状态：适配器级完成（2026-10-22）。** 消费者 parity 属于迁移阶段 P1，尚未开始。

- [x] `BlastAdapter`（blastn）：makeblastdb 索引、outfmt 解析、版本检查、流式。
- [x] `Minimap2Adapter`：PAF 解析、preset、FASTA/FASTQ、paired、索引。
- [x] `Runner` 端到端：索引缓存、manifest、稳定排序、错误映射。
- [x] `views`：best-per、primary-only、summarize_by_target（基座已有）。
- [x] `gseek align` CLI 可用。
- [x] 索引前缀 entry（BLAST `targets.nhr`）支持。
- **验收**：
  - [x] 真实小样本 BLAST/minimap2 集成测试通过；
  - [x] 第二次运行索引 cache_hit=true；
  - [x] manifest verify 通过；
  - [x] 不同线程数结果规范一致；
  - [ ] 与 gmlst/gapit/gside 的消费者 parity（P1 迁移阶段）。

### M2：KMA + MUMmer4（8–10 人日）— 适配器级完成

**状态：适配器级完成（2026-10-22）。** 消费者 parity 属于迁移阶段 P1，尚未开始。

- [x] `KmaAdapter`：index、`.res`、FASTA/FASTQ、paired、depth。
- [x] `MummerAdapter`：**MUMmer 4.x**（最低 4.0.0，测试基线 4.0.1）；nucmer + show-coords
  多步、strand、无索引。MUMmer3 明确拒绝并提示安装 `mummer4`（见
  [ADR-0001](decisions/0001-mummer4.md)）。
- [x] 补齐无坐标/多步/非流式场景的测试。
- [x] 真实工具集成：KMA 组装/FASTQ/paired、索引复用；MUMmer 反向 strand。
- **验收**：
  - [x] KMA `.res` 解析（含 `Query_Coverage > 100` 原样保留）；
  - [x] nucmer 反向命中不再丢 strand；
  - [x] MUMmer3/版本过旧会拒绝，不会误解析；
  - [ ] gmlst 的四个后端消费者 parity（P1 迁移阶段）。

### M3：BWA + MinHash（8–10 人日）— 适配器级完成

**状态：适配器级完成（2026-10-23）。** 消费者 parity 属迁移阶段；samtools 不是必需
依赖（SAM 解析在 gseek 内完成），距离视图使用 `DistanceResult.best_per_query()`。

- [x] `BwaAdapter`：index、mem、SAM/CIGAR 解析、paired。
- [x] `MashAdapter`：sketch/dist/screen。
- [x] `SourmashAdapter`：sketch/search（gather 在 M4 补齐）。
- [x] `DistanceResult` 模型与 `gseek.distance` 端到端。
- [x] 真实工具集成：BWA 组装/paired、mash dist/screen、sourmash search/containment。
- [x] 不引入 samtools 依赖；纯 SAM 解析（设计选择）。
- **验收**：
  - [x] SAM CIGAR/FLAG 单测覆盖主要 op（M/I/D/S/H、反向、secondary/supplementary）；
  - [x] BWA 端到端小样本测试；
  - [ ] mash/sourmash 结果与 gside 黄金输出一致（P1 迁移阶段）；
  - [x] 距离视图：`DistanceResult.best_per_query()`。

### M4：迁移与打磨（5–7 人日）— 代码特性完成，迁移待开始

**状态：M4 代码特性完成（2026-10-23）。** 消费者影子模式、性能基准与 1.0-rc1
属于跨仓库迁移，尚未在本仓库完成。

- [x] BLAST 蛋白任务（blastp/blastx/tblastn/tblastx）。
- [x] sourmash gather。
- [x] skani ANI 适配器。
- [x] `gseek replay` + manifest `result_hash`。
- [ ] gside/gapit/gmlst 的 feature flag + 影子模式。
- [ ] 性能基准：编排开销、索引复用、内存。
- [ ] 1.0.0-rc1。
- **验收**：
  - [ ] 三个消费者在 CI 中同时跑旧路径和 gseek 路径；
  - [ ] 黄金输出零差异（允许显式记录的浮点舍入）；
  - [ ] 性能不退化（G8）。

### M5：1.0 与发布（3–5 人日）

- 删除消费者旧适配器（或标记 deprecated）。
- 发布 `gseek` 0.1/1.0（PyPI/conda-forge）。
- 发布公告、迁移博客、示例项目。
- **验收**：
  - 三个消费者主分支使用 gseek；
  - 安装文档在干净环境可复现；
  - 安全/许可证审查通过。

**总计：约 35–46 人日（7–10 周）。** 如果只能投入半天/天，按阶段拆分，M1 是最小可用价值点。

## 15. 风险与缓解

| 风险 | 影响 | 概率 | 缓解 |
| --- | --- | --- | --- |
| 工具语义差异导致 parity 失败 | 高 | 高 | 先冻结黄金用例；适配器不归并；逐字段映射；影子模式 |
| identity/coverage 公式争议 | 高 | 高 | 显式字段 + `identity_method` + `ungapped_*` 视图；文档写清 |
| 坐标转换 bug | 高 | 中 | 统一 0-based half-open；属性测试；逐工具黄金用例 |
| 索引 key 漏参数导致错误复用 | 高 | 中 | 适配器声明 index 相关参数；key 包含工具版本；发布前校验 artifacts；禁用 mtime |
| 并发构建冲突 | 中 | 中 | 文件锁 + staging + 原子 rename；超时；损坏自愈 |
| 工具版本漂移 | 中 | 高 | CI 固定版本矩阵 + nightly 最新版；版本约束；manifest 记录 |
| 线程数影响结果顺序 | 中 | 高 | 稳定排序；recipe hash 排除线程；文档标注工具内部不确定性 |
| 大 PAF/SAM 内存 | 高 | 中 | 流式解析；可选 collector；记录上限；不默认物化全部 |
| 迁移导致消费者行为变化 | 高 | 中 | feature flag；影子模式；两个 release 的 parity；回滚开关 |
| PyPI 名称冲突 | 中 | 确定 | 分发名 `gseek`，导入名 `gseek`；见 15.1 |
| 零依赖承诺被破坏 | 中 | 低 | CI 检查 `dependencies == []`；可选功能放 extras |
| 安全（manifest 注入） | 高 | 低 | replay 只走 adapter 重新规划，不执行 manifest argv；路径校验 |
| Windows 不支持 | 低 | 高 | 明确声明 Linux/macOS；WSL 作为替代 |

### 15.1 项目更名（重要）

项目最初命名为 `gcore`。截至 2026-10-22，PyPI 的 `gcore` 已被 **Gcore Python API library**（Apache-2.0，`gcore.com`）占用，无法发布同名包。因此项目于同日更名为 **gseek**：

- **PyPI 分发名**：`gseek`（2026-10-22 检查为 404，可用）。
- **导入包名**：`gseek`。
- **CLI**：`gseek`。
- **环境变量**：`GSEEK_*`（例如 `GSEEK_CACHE_DIR`、`GSEEK_TMPDIR`）。
- **schema**：`gseek.alignment/1`、`gseek.run/1`、`gseek.error/1` 等。
- **插件入口组**：`gseek.adapters`。

更名决策与迁移清单见 [ADR-0002](decisions/0002-project-rename.md)。仓库目录仍可能叫 `gcore`（历史路径），以包名和文档为准。

## 16. 开放决策

| 编号 | 决策 | 选项 | 建议 |
| --- | --- | --- | --- |
| D1 | 结果模型实现 | stdlib dataclass vs pydantic | **dataclass**：零依赖、与消费者 pydantic 模型解耦 |
| D2 | 项目/分发名 | `gseek` | **gseek**：`gcore` 已被 PyPI 占用，2026-10-22 更名 |
| D3 | Python 下限 | 3.12 vs 3.11 | **3.12**（用户要求；gapit 迁移时提升） |
| D4 | 是否内置纯 Python MinHash | 是/否 | **否**：只封装 mash/sourmash，避免算法正确性负担 |
| D5 | 是否支持蛋白 BLAST 首发 | 是/否 | **M4**：先核酸，蛋白模式后补 |
| D6 | 是否提供 `gseek.compat.gmlst` | 是/否 | **先不提供**：消费者薄映射更清晰；迁移痛点多时再加 |
| D7 | CLI 依赖 | stdlib argparse vs click/rich | **argparse**：核心零依赖；富文本输出作为可选 extra |
| D8 | 是否捆绑工具二进制 | 是/否 | **否**：外部工具由 conda/pixi/用户安装；只做探测和提示 |
| D9 | Windows 支持 | 是/否 | **暂不**：外部工具生态以 Linux/macOS 为主 |
| D10 | 插件 API 稳定性 | 1.0 前可变 vs 1.0 冻结 | **1.0 冻结**；之前标 `api_version="0"` |

## 17. 完成定义（Definition of Done）

一个适配器/里程碑只有同时满足以下条件才算完成：

- [ ] 代码、类型注解、文档齐全；`ruff`/`pyright`/`pytest` 全绿。
- [ ] 坐标、identity、coverage、strand 的单元测试和黄金测试。
- [ ] 命令规划测试：argv、cwd、env、路径、超时。
- [ ] 真实工具集成测试（可 skip）和至少一份真实输出 fixture。
- [ ] 与目标消费者的 parity 测试通过。
- [ ] 运行 manifest 完整；`verify` 通过；第二次运行索引命中。
- [ ] 错误路径：工具缺失、版本过旧、空输出、解析失败、超时、取消。
- [ ] 文档更新：`adapter-matrix.md`、`research.md`、API reference。
- [ ] 性能基线：编排开销 < 5%，索引复用有可量化收益。
- [ ] 变更日志和迁移说明。

## 18. 下一步

1. 评审本计划，确认第 16 节的开放决策（尤其 D1/D2/D3）。
2. 启动 M0：把本仓库的基座代码补全到可测试状态。
3. 启动 M1：先实现 BLAST 适配器，再实现 minimap2；用 gmlst 的黄金用例做第一轮 parity。
4. 在 gmlst 上做影子模式试点；确认字段映射和方向翻转无行为差异。
5. 根据试点结果调整抽象，再推进消费者迁移。
