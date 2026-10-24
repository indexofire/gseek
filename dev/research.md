# gside / gapit / gmlst 比对实现调研

> 调研日期：2026-10-22  
> 调研对象：本地仓库 `gside`、`gapit`、`gmlst` 的当前工作树  
> 目的：找出三套工具在 DNA 比对上的重复实现、语义分歧和可复用资产，为 `gseek` 的抽象与迁移计划提供依据。

## 1. 三个工具的定位

| 工具 | 主要用途 | 当前比对后端 | 输入 |
| --- | --- | --- | --- |
| **gside** | 物种鉴定（marker / ANI / sketch / 共识） | BLAST+、minimap2、mash、sourmash、skani | FASTA、FASTQ |
| **gapit** | contig/reads 基因筛查（abricate 重实现） | BLAST+、minimap2 | FASTA、FASTQ |
| **gmlst** | MLST / cgMLST / wgMLST 分型 | BLAST+、KMA、minimap2、MUMmer4 | FASTA、FASTQ |

三者的共同点：

- 都需要“把一组 query 序列比到一组 reference/template 序列，再把结果归并成领域结论”。
- 都直接调用外部二进制，不依赖 Python 比对库。
- 都各自实现了一套“发现二进制 → 拼命令 → 跑子进程 → 解析文本 → 归一化字段”的流水线。
- 都因为 cgMLST/筛查/鉴定场景，需要缓存参考索引或参考序列，避免每次运行重建。
- 都面向 CLI + JSON/TSV 输出，且越来越强调 agent 可调用、可复现、稳定退出码。

## 2. 代码地图

### 2.1 进程与工具封装

| 关注点 | gside | gapit | gmlst |
| --- | --- | --- | --- |
| 子进程 | `src/gside/engine/_env.py:run_checked` | `src/gapit/proctools.py:run_tool`、`src/gapit/blast.py:_pipeline`、`src/gapit/minimap2_run.py:_iter_minimap2` | `gmlst/utils.py:run_cmd` |
| 二进制发现 | `src/gside/config.py:which`（注入 pixi bin） | 直接 `subprocess`，靠 `FileNotFoundError` | `gmlst/utils.py:require_tool`（`shutil.which`） |
| 版本检查 | 无统一检查 | `src/gapit/blast.py:ensure_blast`（blastn ≥ 2.2.30） | 无版本检查 |
| 超时 | `run_checked(timeout=...)` | 无（依赖外部超时） | 无 |
| 错误类型 | `RuntimeError` + hint | `GapitError` 子类 + `code` + exit code | `RuntimeError` |
| 并行 | 调用方自行处理 | `--jobs` 等 | `ThreadPoolExecutor` 按样本并行，`typing_runner.py` |

### 2.2 比对后端

| 工具 | gside | gapit | gmlst |
| --- | --- | --- | --- |
| BLAST+ | `engine/backends/blast.py` | `blast.py` | `aligners/blastn.py` |
| minimap2 | `analysis/read_screening.py` | `minimap2_run.py`、`paf.py` | `aligners/minimap2.py` |
| KMA | — | — | `aligners/kma.py` |
| MUMmer | — | — | `aligners/nucmer.py` |
| mash | `engine/backends/kmer.py:MashBackend` | — | — |
| sourmash | `engine/backends/kmer.py:SourmashBackend`、`analysis/sourmash_identifier.py` | — | — |
| skani | `engine/backends/skani.py` | — | — |

### 2.3 结果模型

| 工具 | 模型 | 位置 |
| --- | --- | --- |
| gside | `Hit`（BLAST 行）、`KmerDistance`、`AniHit` | `engine/hits.py`、`engine/backends/kmer.py`、`engine/backends/skani.py` |
| gapit | `BlastRow`、`PafRecord`、`Hit` | `blast.py`、`paf.py`、`hits.py` |
| gmlst | `AlleleMatch`、`AlignmentResult` | `aligners/base.py` |

### 2.4 索引与缓存

| 工具 | 索引方式 | 失效判断 |
| --- | --- | --- |
| gside | BLAST `ensure_index`；mash/sourmash/skani 数据库自带索引 | 文件存在性为主 |
| gapit | `makeblastdb`；数据库构建写 `gapit-manifest.json` | 构建时记录 `sha256` + 工具版本；minimap2 直接读 FASTA |
| gmlst | `makeblastdb`、`minimap2 -d`、`kma index`、nucmer 合并 FASTA | 文件存在 + mtime + 合并 FASTA 大小；代表序列另有 fingerprint |

### 2.5 可复现性

| 工具 | 已记录内容 | 缺口 |
| --- | --- | --- |
| gside | 部分 JSON 输出带软件版本/时间戳 | 无统一 run manifest，无命令回放 |
| gapit | `gapit-manifest.json` 记录 `sha256`、`makeblastdb_version`、`minimap2_version` | 只覆盖建库，不覆盖单次比对运行；无完整 argv/环境 |
| gmlst | 索引目录、representative fingerprint、部分 metadata | 无 run manifest；索引失效依赖 mtime，不是内容哈希 |

## 3. 关键实现差异

### 3.1 BLAST outfmt 字段不同

| 工具 | 字段数 | 字段 |
| --- | --- | --- |
| gmlst | 12 | `qseqid sseqid pident length qlen qstart qend sstart send evalue bitscore sseq` |
| gapit | 15 | `qseqid qstart qend qlen sseqid sstart send slen sstrand evalue length pident gaps gapopen stitle` |
| gside | 14 | `qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qlen slen` |

影响：

- 三者对 coverage 的计算口径不同（见 3.2）。
- gapit 依赖 `sstrand` 和 `stitle`；gside 依赖 `mismatch/gapopen`；gmlst 依赖 `sseq` 提取 novel allele 序列。
- 统一结果模型必须同时保留“列语义”和“原始标签”，不能只保留一个投影。

### 3.2 coverage 计算口径不同

| 工具 | 公式 | 代码 |
| --- | --- | --- |
| gmlst | `aln_len / qlen` | `aligners/blastn.py`、`aligners/minimap2.py` |
| gapit | `(length - gaps) / slen` | `hits.py:process_rows` |
| gside | query: `aln_len / qlen`；subject: `aln_len / slen` | `engine/hits.py:Hit.from_blast_line` |

同一个 BLAST 行在三套工具里可能得到不同的 `coverage`，尤其是含 gap 或 query/target 长度不对称时。`gseek` 必须显式定义：

- `query_coverage`：query 被比对区间覆盖的比例；
- `target_coverage`：target 被比对区间覆盖的比例；
- `ungapped_query_coverage` / `ungapped_target_coverage`：用 `aligned_length - gaps` 计算的“无 gap 覆盖”；
- 所有 coverage 保留原始浮点，展示层再舍入。

### 3.3 minimap2 identity 公式不同

| 工具 | identity | 代码 |
| --- | --- | --- |
| gmlst | `nmatch / (nmatch + NM)` | `aligners/minimap2.py:_parse_paf` |
| gapit | `(alen - NM) / alen` | `paf.py:alignment_identity` |
| gside | `nmatch / alen` | `analysis/read_screening.py:_aggregate` |

这三者在有 gap 时并不等价。更麻烦的是，minimap2 只有在 `-c`/`--cs` 时才会给出 `NM`/`cg`/`cs`；没有这些标签时只能退化为 `nmatch/alen`。

`gseek` 的策略：

- `identity` 字段统一为“比对列上的百分比一致性”，由适配器声明其计算方式；
- 额外保留 `nmatch`、`alen`、`nm`、`identity_method`；
- 提供 `identity_from_nm`、`identity_from_matches`、`identity_nmatch_nm` 只读属性，
  供消费者做 parity 对比；
- minimap2 适配器默认优先 `NM`（`paf_nm`），没有 `NM` 时退化为 `nmatch/alen`
  （`paf_matches`）；不把某一种公式写死进核心模型。

### 3.4 坐标约定不同

| 工具 | 原生坐标 | 归一化注意点 |
| --- | --- | --- |
| BLAST outfmt 6 | 1-based inclusive；`sstart > send` 表示反向 | start 减 1，end 不变 |
| minimap2 PAF | 0-based half-open；strand 单独列 | 直接使用；`tp:A:S` 为 secondary |
| MUMmer `show-coords` | 1-based inclusive；`S2 > E2` 表示反向 | start 减 1；gmlst 目前把 strand 硬编码为 `+`，反向命中会丢信息 |
| SAM/BWA | 1-based POS；CIGAR 决定 reference 跨度；FLAG 0x10 表示反向 | POS 减 1；按 CIGAR 求和 |
| KMA `.res` | 无坐标，只有 template 级 identity/coverage/depth | 坐标为 `None`，不能伪造 |
| mash/sourmash | 无坐标，只有距离/containment | 结果模型走 `DistanceRecord` |

`gseek` 统一采用 **0-based half-open、始终在正向参考序列上表示区间、strand 单独字段**。这样与 PAF/BED 生态一致，也便于区间并集和 breadth 计算。

### 3.5 命中归并策略不同

| 工具 | 归并策略 | 代码 |
| --- | --- | --- |
| gmlst | 每个 `(locus, allele)` 保留最佳行；另存 fragment HSP 列表，最多 100 条/locus | `aligners/base.py:select_best_matches`、`cap_fragments` |
| gapit | 按 `(qseqid, qstart, qend)` 去重；再按 subject coverage 过滤；可选 fragment merge | `hits.py`、`fragments.py` |
| gside | 按 target 聚合：区间并集求 breadth，alignment-length 加权平均 identity | `analysis/read_screening.py:_aggregate` |

这些是**应用策略**，不应固化在适配器里。`gseek` 的原则：

- 适配器只做“工具输出 → 规范化记录”的无损转换；
- `AlignmentResult` 保留全部记录；
- 通过 `best_per()`、`summarize_by_target()`、`deduplicate()` 等显式视图实现归并；
- 每个视图在文档中写清 tie-break 规则。

### 3.6 secondary / primary 处理不同

- gmlst 给 minimap2 传 `--secondary=no`。
- gapit 解析 `tp:A:`，默认只保留 primary；cluster 引擎另行处理。
- gside 在 `_parse_paf_line` 中过滤 `tp:A:S`，并用 `--secondary=no`。

`gseek` 的默认值应与三者一致（primary only），但保留 `include_secondary=True` 的能力。过滤属于视图层，不丢弃原始记录时更利于诊断。

### 3.7 版本与命令探测

- BLAST：`blastn -version`，gapit 只要求 ≥ 2.2.30；当前环境为 2.17.0+。
- minimap2：`minimap2 --version`，当前环境有 2.30-r1287、2.31-r1302 两个版本。
- MUMmer：确认为 **MUMmer4**。gmlst 的 pixi 依赖是 `mummer4 >=4.0`，当前环境为
  conda 包 `mummer4-4.0.1`；`nucmer --version` 与 `mummer --version` 均输出 `4.0.1`；
  `show-coords` 没有可用的 `--version`。决策见 [ADR-0001](decisions/0001-mummer4.md)。
- KMA：`kma -v` 输出 `KMA-1.6.8`；`kma -version` 是非法参数。
- BWA：无参数运行，stderr 输出 `Version: 0.7.19-r1273`。
- mash：`mash --version` → `2.3`。
- sourmash：`sourmash --version` → `sourmash 4.9.4`。

`gseek` 需要为每个工具声明 `ToolRequirement`（binary、version argv、解析正则、最低版本、是否可选），并缓存 probe 结果。

### 3.8 索引生命周期不同

- gmlst：`core/indexing.py` 用 artifact 存在性、合并 FASTA 大小、mtime 判断 stale；按 backend 清理；没有跨进程锁。
- gapit：建库时写 manifest，记录 sha256 和工具版本；但没有统一的“索引 key”，也没有并发保护。
- gside：以文件存在性为主，缺少内容指纹。

`gseek` 采用**内容寻址索引**：

- index key = hash(adapter + adapter version + 语义参数 + target 内容哈希 + 工具版本 + 索引格式版本)；
- 索引目录以 key 命名，构建完成后原子发布；
- 用 `fcntl.flock` 防止并发重复构建；
- 失效判断只看内容哈希，不看 mtime；
- manifest 记录索引 key、artifact 哈希、构建命令和工具版本。

### 3.9 错误模型不同

- gside：`RuntimeError` + 面向用户的 hint。
- gapit：`GapitError` 家族，带 `code`、`exit_code`、`context`，输出 `gapit.error/1` JSON envelope。
- gmlst：`RuntimeError` / `ValueError`，CLI 自己转成 Rich 输出。

`gseek` 应采用 gapit 风格的类型化错误，因为它是三者中最适合库边界的模型：

- `GseekError(code, message, context, hint, exit_code)`；
- 细分 `ToolNotFound`、`ToolVersionError`、`ExecutionError`、`ParseError`、`InputError`、`IndexBuildError`、`ManifestError`、`UnsupportedFeature`；
- 统一 `gseek.error/1` envelope；
- 适配器抛出的错误必须带 backend、binary、phase、文件/行号。

### 3.10 并行与缓存

- gmlst 已经实现了“样本级并行 + 每样本线程数降为 1 + 先 warm-up 建索引 + 有序回吐结果”的模式（`commands/typing_runner.py`）。
- gapit 的 `--jobs` 等是命令行级并行。
- gside 目前没有统一的批量调度层。

`gseek` 可以提供可选的 `run_many()`：线程池执行外部进程（I/O bound），结果按输入顺序返回，索引构建只做一次。但第一版不强制内置调度器，避免把应用策略带进库。

## 4. 可复用资产

### 4.1 测试数据与黄金输出

- `gmlst/test/test_aligner_golden.py`：BLAST/PAF 解析的黄金断言，含 best/fragment 语义、sequence 提取、copy_count。
- `gapit/tests/golden/*.json|tsv`：筛查/分型/reads 的端到端黄金输出。
- `gapit/tests/parity/`：与 abricate 的 parity 脚本。
- `gside/tests/integration/test_marker_golden.py`、`test_ani_golden.py`、`test_sourmash_golden.py`：真实工具集成测试。
- `gmlst/test/test_kma_aligner.py`、`test_minimap2_aligner.py`：命令拼装与 FASTA/FASTQ 分支。

这些应作为 `gseek` 的 **consumer parity fixtures**：适配器解析同一份原始输出，断言与消费者现有模型一致。

### 4.2 可借鉴的设计

| 资产 | 出处 | 在 gseek 中的用法 |
| --- | --- | --- |
| `sha256_file` / `tool_version_line` | `gapit/dbbuild.py` | 扩展为 `InputRef` / `ToolRef` / run manifest |
| `gapit-manifest.json` | `gapit/records.py` | 演化为 `gseek.run/1` + `gseek.index/1` |
| representative fingerprint | `gmlst/core/indexing.py` | 演化为内容寻址 index key |
| fragment merge / union breadth | `gapit/fragments.py`、`gside/read_screening.py` | 作为 `gseek.views` 的聚合算法 |
| typed error envelope | `gapit/errors.py` | 作为 `gseek.error/1` |
| sample-level parallel + ordered flush | `gmlst/commands/typing_runner.py` | 作为 `gseek.run_many()` 的参考 |
| `gmlst` backend comparison docs | `gmlst/docs/en/backends.md` | 作为 `gseek` 适配器能力矩阵的输入 |

## 5. 结论：gseek 应该拥有什么

### 应该上收到 gseek

- 外部工具的发现、版本探测、命令规划、进程执行、超时/取消、stderr 捕获。
- 参考序列/查询序列的格式检测、合并、校验、内容哈希。
- 各工具输出的**无损规范化**：坐标、strand、identity、coverage、tags。
- 统一结果模型：`AlignmentRecord`、`AlignmentResult`、`DistanceRecord`、`DistanceResult`。
- 索引的内容寻址、并发锁、原子发布、GC。
- run manifest、recipe hash、环境/命令/版本记录、verify/replay。
- 错误类型与 JSON envelope。
- 通用视图：best-per、per-target 聚合、区间并集、primary/secondary 过滤。

### 应该留在消费者

- locus/allele 的命名解析（`abcZ_1` 的生物学含义）。
- 等位基因判定阈值、exact/closest/novel/partial 分类。
- ST profile 查找、物种规则、抗性基因数据库语义。
- 输出格式（gmlst 的 TSV、gapit 的 abricate 兼容表、gside 的物种 JSON）。
- CLI 参数策略（例如 FASTQ 自动切 KMA、cgMLST 模式）。
- 样本配对命名规则（`_R1/_R2`）——可以提供 helper，但不作为核心模型。

这条边界是 `gseek` 能否被三个项目同时采用的关键：**库只负责“比对事实”，不负责“生物学结论”**。
