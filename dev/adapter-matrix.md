# gseek 适配器矩阵

> 本文是 `gseek` 适配器实现的“事实清单”。每个适配器都必须按这里的命令、字段和语义实现，并在 `development-plan.md` 的里程碑中逐项验收。

## 0. 统一约定

### 0.1 坐标

- 所有区间：**0-based half-open**，`[start, end)`。
- 所有区间始终表示**正向序列**上的位置；方向放在 `strand` 字段。
- `strand` 表示 query 相对于 target 的方向：`+` 同向，`-` 反向。
- 无法获得坐标的后端（KMA、mash、sourmash）将区间字段设为 `None`，不伪造。

### 0.2 identity

- `identity`：百分比，`0..100`。
- 适配器必须在 `record.tags["identity_method"]` 中记录来源：
  - `blast_pident`
  - `paf_nm`（`(alen - NM) / alen`）
  - `paf_matches`（`nmatch / alen`）
  - `paf_nmatch_nm`（`nmatch / (nmatch + NM)`）
  - `sam_nm`（`(aligned_columns - NM) / aligned_columns`）
  - `kma_template`
  - `mummer_coords`
- 同时保留原始字段：`nmatch`、`alen`、`nm`、`matches`、`mismatches`、`gap_count`。
- 展示层可四舍五入；核心模型保留原始浮点。

### 0.3 coverage

| 字段 | 定义 |
| --- | --- |
| `query_coverage` | query 被比对区间覆盖的比例，`0..1`；无坐标时为 `None` |
| `target_coverage` | target 被比对区间覆盖的比例，`0..1`；无坐标时为 `None` |
| `ungapped_query_coverage` | `(aligned_length - gap_count) / query_length` |
| `ungapped_target_coverage` | `(aligned_length - gap_count) / target_length` |

KMA 的 `Template_Coverage` 直接填入 `target_coverage`（除以 100），不反推坐标。

### 0.4 primary / secondary

- 适配器默认请求或过滤为 **primary only**，但过滤发生在解析之后、结果构建之前。
- 原始记录可以通过 `include_secondary=True` 保留。
- minimap2 的 secondary 标记：`tp:A:S`；supplementary：`tp:A:P` + `SA` 或 `tp:A:S` 需按版本确认，统一解析 `tp`。
- BWA 的 secondary：FLAG `0x100`；supplementary：FLAG `0x800`。

### 0.5 排序

适配器不承诺输出顺序。`gseek` 在 `AlignmentResult` 构建时按以下稳定键排序：

```text
(query_id, target_id, query_start or -1, target_start or -1, strand, aligned_length desc)
```

这样同一批记录在不同线程数下也能得到稳定的规范输出。

## 1. 能力矩阵

| 家族 | 实现 | 输入 | 输出 | 索引 | 配对 | 流式 | 线程 | 坐标 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `blast` | `blastn` / `blastp` / `blastx` / `tblastn` / `tblastx` | FASTA | tabular | target | 否 | 是 | 是 | 有 | 蛋白模式需单独参数 |
| `minimap2` | `minimap2` | FASTA / FASTQ | PAF / SAM | target | 是 | 是 | 是 | 有 | 预设决定灵敏度 |
| `mummer` | `nucmer` + `show-coords` | FASTA | coords | 无 | 否 | 是 | 有限 | 有 | 需处理 delta |
| `kma` | `kma` | FASTA / FASTQ | `.res` | target | 是 | 否（文件） | 是 | 无 | 模板级结果 |
| `bwa` | `bwa mem` | FASTA / FASTQ | SAM | target | 是 | 是 | 是 | 有 | 需要 SAM 解析器 |
| `minhash` | `mash` / `sourmash` | FASTA / FASTQ | 距离表 / CSV | sketch | 否 | 是 | 是 | 无 | 两组距离语义 |

## 2. BLAST+

### 2.1 工具与版本

| 角色 | 二进制 | 探测命令 | 解析 | 最低版本 |
| --- | --- | --- | --- | --- |
| aligner | `blastn`（默认） | `blastn -version` | `blastn:\s*(\d+)\.(\d+)\.(\d+)` | 2.2.30 |
| indexer | `makeblastdb` | `makeblastdb -version` | 同上 | 2.2.30 |
| 蛋白模式 | `blastp` / `blastx` / `tblastn` / `tblastx` | `<tool> -version` | 同上 | 2.2.30 |

### 2.2 索引

```bash
makeblastdb -in targets.fasta -dbtype nucl|prot -out <index>/targets -title <title>
```

- 产物：`targets.nhr/.nin/.nsq`（nucl）或 `.phr/.pin/.psq`（prot）。
- 索引 key 必须包含：`dbtype`、`target` 内容哈希、`makeblastdb` 版本、`-parse_seqids` 等参数。
- 目标 FASTA 必须由 `gseek.io.merge_fasta` 生成，保证 ID 无空白、顺序稳定、可哈希。

### 2.3 比对命令（核酸）

```bash
blastn \
  -query query.fasta \
  -db <index>/targets \
  -outfmt "6 qseqid sseqid pident length mismatch gapopen gaps qstart qend sstart send evalue bitscore qlen slen stitle" \
  -evalue 1e-5 \
  -max_target_seqs 500 \
  -num_threads 4 \
  -dust no \
  -perc_identity 0
```

- 用 `-db` 而不是 `-subject`，这样索引可复用。
- 需要 `stitle` 时追加；需要 `sseq` 时追加（gmlst 的 novel allele 提取）。
- 需要 `sstrand` 时追加；不要假设所有 BLAST 版本都支持相同字段（2.2.30+ 支持）。
- `-max_target_seqs` 不是严格的 top-N，BLAST 文档明确说明；结果模型不能把它当排序保证。
- `-culling_limit 1` 可用于筛查场景，但会改变结果；作为参数暴露。

### 2.4 解析

16 个基础列（按上面 outfmt 顺序）：

| 字段 | 归一化 |
| --- | --- |
| `qseqid` | `query_id` |
| `sseqid` | `target_id` |
| `pident` | `identity`，`identity_method=blast_pident` |
| `length` | `aligned_length` |
| `mismatch` | `mismatches` |
| `gapopen` | `gap_openings` |
| `gaps` | `gap_count` |
| `qstart/qend` | `query_start = min-1`，`query_end = max` |
| `sstart/send` | `target_start = min-1`，`target_end = max`；`sstart > send` → `strand = "-"` |
| `evalue` | `evalue` |
| `bitscore` | `bit_score` |
| `qlen/slen` | `query_length` / `target_length` |
| `stitle` | `target_description` |
| `sseq` | `target_aligned`（仅当请求） |

注意：

- BLAST 的 `length` 是比对列数，不是 query/target 的跨度；coverage 用区间跨度计算，ungapped coverage 用 `length-gaps`。
- `qstart/qend` 与 `length` 在含 gap 时可能不一致；不要用 `length/qlen` 代替 `query_coverage`。
- 反向命中时，`qstart/qend` 仍然是 query 正向坐标；不要交换 query 坐标。
- `blastn` 默认 task 是 `megablast`，因此适配器除 `tblastx`（无 `-task` 选项）外都显式传 `-task <task>`。
- `min_identity`/`perc_identity` 只对核酸任务有效；蛋白任务传入非零值会报
  `UnsupportedFeatureError`（BLAST+ 的 blastp/blastx/tblastn/tblastx 没有该选项）。
- `sseq` 是 target 的比对方向序列；如果 `strand="-"`，它与 target 正向序列反向互补。

### 2.5 蛋白模式

| 模式 | query | target | 说明 |
| --- | --- | --- | --- |
| `blastp` | protein | protein | 蛋白-蛋白 |
| `blastx` | nucleotide | protein | 翻译 query |
| `tblastn` | protein | nucleotide | 翻译 target |
| `tblastx` | nucleotide | nucleotide | 两边都翻译 |

`gside` 当前使用 `blastp/blastx/tblastn/tblastx`。第一版 `gseek` 先实现 `blastn`，M2 再补蛋白模式；结果模型不区分分子类型，由 `BackendSpec.capabilities.molecule` 声明。

### 2.6 常见坑

- `makeblastdb` 会把 header 的第一个空白前 token 作为 ID；目标 FASTA 的 ID 必须无空格，否则 `sseqid` 与预期不符。
- 空数据库/无 hit 时 BLAST 输出空文件，退出码 0；适配器返回空记录，不报错。
- `-outfmt` 字段顺序必须与解析器严格一致；建议在 `BackendSpec` 中把字段列表作为常量，规划命令和解析共用。
- BLAST 版本号可能带 `+`（如 `2.17.0+`），解析时取前三段数字。

## 3. minimap2

### 3.1 工具与版本

| 角色 | 二进制 | 探测命令 | 解析 |
| --- | --- | --- | --- |
| aligner/indexer | `minimap2` | `minimap2 --version` | `(\d+)\.(\d+)(?:-r(\d+))?` |

最低版本建议 `2.24`（`--secondary=no`、`-x asm5/asm20`、`--cs` 等都已稳定）；实际以 CI 锁定版本为准。

### 3.2 预设

| 预设 | 用途 | 备注 |
| --- | --- | --- |
| `sr` | 短读长 | 可加 `-k11 -w6 -B2` 提升分歧等位基因灵敏度（gapit 的经验） |
| `map-ont` | Nanopore | 长读 |
| `map-hifi` | PacBio HiFi | 长读高准确度 |
| `asm5` | 约 0.1% 分歧的组装 | gmlst 默认 |
| `asm10` | 约 1% 分歧 | |
| `asm20` | 约 5% 分歧 | gapit cluster 引擎使用 |
| `splice` | RNA | DNA 场景不使用 |

`gseek` 只允许显式预设或由 helper 选择；不要静默回退到默认，否则结果不可复现。

### 3.3 索引

```bash
minimap2 -x <preset> -d <index>/targets.mmi <targets.fasta>
```

- `.mmi` 与部分索引参数相关；index key 必须包含 `preset`、`-k/-w/-H/-I` 等影响索引的参数。
- 对 `asm5/asm10/asm20`，`-d` 生成的索引可复用；对 reads 预设同理。
- 如果只做一次性比对，可省略 `.mmi`，直接传 target FASTA；但 `gseek` 默认缓存索引。

### 3.4 比对命令

```bash
minimap2 \
  -x <preset> \
  -t <threads> \
  --secondary=no \
  -c \
  -o <workspace>/hits.paf \
  <target.fasta|targets.mmi> \
  <query.fasta|query.fastq> [<query2.fastq>]
```

- 需要 identity 时加 `-c`（CIGAR + NM）或 `--cs`；但注意某些版本/预设组合下 `-c` 可能改变输出行为，必须在 CI 中做矩阵测试。
- 需要 secondary 时去掉 `--secondary=no`，并解析 `tp`。
- 需要 SAM 时用 `-a`；`gseek` 第一版只解析 PAF，SAM 留给 BWA 适配器。
- 配对 reads：两个 query 文件作为位置参数；不要用 `-ipe`（那是 KMA 的语法）。

### 3.5 PAF 解析

12 个必需列：

| 列 | 字段 | 归一化 |
| --- | --- | --- |
| 0 | `qname` | `query_id` |
| 1 | `qlen` | `query_length` |
| 2 | `qstart` | `query_start` |
| 3 | `qend` | `query_end` |
| 4 | `strand` | `strand` |
| 5 | `tname` | `target_id` |
| 6 | `tlen` | `target_length` |
| 7 | `tstart` | `target_start` |
| 8 | `tend` | `target_end` |
| 9 | `nmatch` | `matches` |
| 10 | `alen` | `aligned_length` |
| 11 | `mapq` | `mapq` |

标签：

| 标签 | 含义 | 用途 |
| --- | --- | --- |
| `tp:A:P/S/I` | primary/secondary/inversion | primary 过滤 |
| `NM:i:` | 编辑距离（mismatch + gap） | identity |
| `AS:i:` | alignment score | `score` |
| `ms:i:` | max segment score | 可选 |
| `cg:Z:` | CIGAR | 计算 query/target 消耗、gap |
| `cs:Z:` | 差异串 | 精确差异、可选 |
| `de:f:` | gap-compressed divergence | 可选 |
| `rl:i:` | read group | 可选 |

identity 计算优先级：

1. 有 `NM`：`identity = 100 * (alen - NM) / alen`，`identity_method=paf_nm`；
2. 无 `NM`：`identity = 100 * nmatch / alen`，`identity_method=paf_matches`；
3. 需要与 gmlst 兼容时，可提供 `identity_nmatch_nm` 视图（`nmatch/(nmatch+NM)`）。

注意：`nmatch` 已经是匹配碱基数；`alen` 是比对块长度。两者与 `NM` 的组合在不同 minimap2 版本/参数下可能不一致，必须在 `tags` 中保留原始值。

### 3.6 常见坑

- `--secondary=no` 会抑制 secondary，但不会抑制 supplementary；需要按 `tp` 再过滤。
- PAF 的 `strand` 是 query 相对于 target 的方向；坐标本身都在正向序列上。
- 对于 `-x asm5` + `-c`，在某些 minimap2 2.30 构建/短序列上可能输出为空；适配器不能假设“有 CIGAR 就一定有行”，应以实际行数为准，并提供无 CIGAR 的回退。
- `-c` 与 `--cs` 的输出体积可能很大；默认只在需要 identity 时开启，且支持 `--cs=short`。

## 4. MUMmer 4 / nucmer

> **版本确认（2026-10-22）**：目标是 **MUMmer 4.x**，不是 MUMmer3。gmlst 依赖
> `mummer4 >=4.0`；当前环境为 conda 包 `mummer4-4.0.1`；`nucmer --version` 与
> `mummer --version` 均输出 `4.0.1`。MUMmer3 不在 1.0 范围内。详见
> [ADR-0001](decisions/0001-mummer4.md)。

### 4.1 工具与版本

| 角色 | 二进制 | 探测命令 | 解析 | 最低版本 |
| --- | --- | --- | --- | --- |
| aligner | `nucmer` | `nucmer --version` | `(\d+)\.(\d+)\.(\d+)` | 4.0.0 |
| parser | `show-coords` | 无独立版本；用 `nucmer` 版本 | — | 4.0.0 |
| 可选 | `delta-filter` | `delta-filter -h` 或跳过 | — | — |

版本低于 4.0.0 或解析失败时，适配器必须报 `ToolVersionError`，并提示安装
`mummer4`（conda-forge/bioconda），不要回退到 MUMmer3 的旧参数。

### 4.2 索引

`nucmer` 没有独立的持久索引；每次运行都会构建后缀数组。`IndexScope = NONE`，`IndexStore` 只记录合并后的 target FASTA 哈希，不缓存 `.delta`。

### 4.3 比对命令

```bash
nucmer --maxmatch -c 20 -l 20 -p <workspace>/out <target.fasta> <query.fasta>
show-coords -rcl -T <workspace>/out.delta
```

- `nucmer` 的参考是第一个位置参数（target），query 是第二个。
- `--maxmatch` 提高重复/短序列灵敏度；对大型基因组可能很慢，作为参数暴露。
- 适配器默认 `maxmatch=True`、`-c 20`、`-l 20`（300–800 bp 等位基因的 gmlst 经验值）；
  可通过 `params={"maxmatch": false, "min_cluster": ..., "min_match": ...}` 覆盖。
- `show-coords -rcl -T` 输出制表符分隔的 coords；`-r` 按参考排序，`-c` 只显示保守区间，`-l` 显示序列长度，`-T` 制表符。
- `delta-filter` 可以在 `show-coords` 前过滤；作为可选步骤。

### 4.4 coords 解析

`show-coords -rcl -T` 输出（MUMmer4）：

```text
[S1] [E1] [S2] [E2] [LEN 1] [LEN 2] [% IDY] [LEN R] [LEN Q] [COV R] [COV Q] [TAGS]
1    44   1    44   44      44      100.00  64      44      68.75    100.00  t1  q1
1    44   44   1    44      44      100.00  64      44      68.75    100.00  t1  q1
```

| 列 | 含义 | 归一化 |
| --- | --- | --- |
| 0/1 | S1/E1，reference 起止 | `target_start=min-1`, `target_end=max` |
| 2/3 | S2/E2，query 起止 | `query_start=min-1`, `query_end=max` |
| 4/5 | LEN 1/LEN 2 | `aligned_length` 取 `LEN 1`（或 min(LEN1,LEN2)） |
| 6 | % IDY | `identity` |
| 7/8 | LEN R/LEN Q | `target_length` / `query_length` |
| 9/10 | COV R/COV Q | `target_coverage` / `query_coverage`（除以 100） |
| 11/12 | TAGS | target/query IDs |

strand：

- 如果 `S2 <= E2`，`strand="+"`；如果 `S2 > E2`，`strand="-"`。
- 不要像 gmlst 当前实现那样硬编码 `"+"`，否则反向命中会丢失方向。

### 4.5 常见坑

- `nucmer` 会把命令行写到 stdout，`show-coords` 输出带表头；解析器必须跳过非数据行（`^[^\d]` 不够，因为负坐标/科学计数法可能出现；用严格的列数 + 数值解析）。
- MUMmer4 的 header 是 `[TAGS]` 一列，不是 `[TAGR] [TAGQ]` 两列；但数据行仍是 13 列。
- `show-coords` 可能输出重复/重叠的 coords；不要自动去重，保留原始记录。
- 超大基因组 + `--maxmatch` 会产生巨大的 `.delta`；需要流式解析和临时文件清理。
- `nucmer` 对 query/target 的命名有长度/字符限制；目标 FASTA 必须规范化。

## 5. KMA

### 5.1 工具与版本

| 角色 | 二进制 | 探测命令 | 解析 |
| --- | --- | --- | --- |
| aligner/indexer | `kma` | `kma -v` | `KMA-(\d+)\.(\d+)\.(\d+)` |

KMA 没有 `--version`；`kma -version` 是非法参数。`kma` 无参数运行会打印帮助并以非零退出，不要用它探测版本。

### 5.2 索引

```bash
kma index -i <targets.fasta> -o <index>/kma_db
```

产物：`kma_db.name`、`kma_db.length.b`、`kma_db.seq.b`、`kma_db.comp.b`（可能还有 `.fsa`）。

索引 key 包含：target 内容哈希、KMA 版本、`-k/-m/-hc/-c/-C/-ML` 等索引参数（如果使用）；`threads` 不进入 key。
`params` 会同时传给 `plan_index` 与 `plan_align`；`-k/-m/-hc/...` 在 align 阶段被接受
但不会拼进 align argv（它们只影响索引）。

### 5.3 比对命令

组装 genome（FASTA）：

```bash
kma -t_db <index>/kma_db -i <query.fasta> -asm -o <workspace>/out -t 4
```

单端 reads：

```bash
kma -t_db <index>/kma_db -i <query.fastq> -ill -o <workspace>/out -t 4
```

双端 reads：

```bash
kma -t_db <index>/kma_db -ipe <R1.fastq> <R2.fastq> -ill -o <workspace>/out -t 4
```

可选：`-mem_mode`（更快但灵敏度变化）、`-apm`、`-1t1` 等。所有影响结果的参数必须进入 recipe hash。

### 5.4 `.res` 解析

当前 KMA 1.6.8 的 `.res` 头部（以 `#` 开头，制表符分隔）：

```text
#Template  Score  Expected  Template_length  Template_Identity  Template_Coverage  Query_Identity  Query_Coverage  Depth  q_value  p_value
```

| 列 | 归一化 |
| --- | --- |
| `Template` | `target_id`；按最后一个 `_`/`-` 拆分 locus/allele 的工作留给消费者 |
| `Score` | `score` |
| `Template_length` | `target_length` |
| `Template_Identity` | `identity`，`identity_method=kma_template` |
| `Template_Coverage` | `target_coverage`（除以 100） |
| `Query_Identity` | `tags["query_identity"]` |
| `Query_Coverage` | `tags["query_coverage_percent"]` |
| `Depth` | `depth`（FASTQ 有意义；FASTA 可保留但标注） |
| `q_value` / `p_value` | `tags` |

注意：

- KMA 没有坐标；`query_start/end`、`target_start/end` 全部为 `None`。
- KMA 的 `Query_Coverage` 可能大于 100%（模板比 query 长、或 reads 多次覆盖）；不要 clamp，保留原始值到 tags。
- KMA 的 `-asm` 模式与 `-ill` 模式的字段含义略有不同；`input_kind` 进入 manifest。
- `.res` 文件可能不存在（无 hit 或运行失败）；适配器必须区分“空结果”和“缺少输出”。

### 5.5 常见坑

- KMA 的模板 ID 可能与目标 FASTA 的 header 不完全一致；保留原始 `Template` 字符串。
- KMA 输出 `.aln`、`.frag.gz` 等辅助文件；第一版只解析 `.res`。
- KMA 对重复模板/多拷贝的处理与 BLAST/minimap2 不同；不要跨后端假设结果可逐行对齐。
- KMA 的线程数对单样本可能没有明显加速；调度层应优先样本级并行。

## 6. BWA

### 6.1 工具与版本

| 角色 | 二进制 | 探测命令 | 解析 |
| --- | --- | --- | --- |
| aligner | `bwa` | `bwa`（无参数） | `Version:\s*(\d+\.\d+\.\d+(?:-r\d+)?)` |
| indexer | `bwa` | 同上 | 同上 |
| 可选 | `samtools` | `samtools --version` | `samtools\s+(\d+\.\d+)` |

BWA 无参数运行时把版本打印到 **stderr**，退出码 0。`bwa version` 不是合法子命令。

### 6.2 索引

```bash
bwa index -p <index>/targets <targets.fasta>
```

产物：`targets.amb`、`targets.ann`、`targets.bwt`、`targets.pac`、`targets.sa`。

索引 key 包含：target 内容哈希、BWA 版本、`-a`（算法）等参数。

### 6.3 比对命令

```bash
bwa mem \
  -t 4 \
  -R '@RG\tID:<run_id>\tSM:<sample>' \
  <index>/targets \
  <R1.fastq> [<R2.fastq>] \
  > <workspace>/out.sam
```

常用参数：

| 参数 | 作用 | 是否影响语义 |
| --- | --- | --- |
| `-k` | 最小 seed 长度 | 是 |
| `-w` | band width | 是 |
| `-A/-B/-O/-E` | 打分 | 是 |
| `-T` | 最小 score | 是 |
| `-a` | 输出所有（含 unmapped） | 是（结果集） |
| `-M` | 标记 shorter split hits as secondary | 是 |
| `-Y` | soft clipping | 是 |
| `-R` | read group | 否（但进入 manifest） |
| `-t` | 线程 | 否（进入 manifest，不进 recipe hash） |

第一版只支持 `bwa mem`；`bwa aln/samse` 作为后续兼容项。

### 6.4 SAM 解析

SAM 行（非 `@` 开头）至少 11 列：

| 列 | 字段 | 归一化 |
| --- | --- | --- |
| 0 | QNAME | `query_id` |
| 1 | FLAG | 解析 strand/secondary/supplementary/paired |
| 2 | RNAME | `target_id`（`*` → 跳过） |
| 3 | POS | `target_start = POS - 1` |
| 4 | MAPQ | `mapq` |
| 5 | CIGAR | 计算 reference 跨度、query 跨度、gap |
| 6 | RNEXT | 配对信息（可选） |
| 7 | PNEXT | 配对信息（可选） |
| 8 | TLEN | 配对信息（可选） |
| 9 | SEQ | `query_aligned` 或原始序列 |
| 10 | QUAL | 可选 |
| 11+ | tags | `NM:i:`, `AS:i:`, `MD:Z:`, `SA:Z:` 等 |

CIGAR 解析：

- `M/=/X` 同时消耗 query 和 reference；
- `I/S` 消耗 query；
- `D/N` 消耗 reference；
- `H/P` 不消耗；
- `reference_end = POS - 1 + sum(M/D/N/=/X)`；
- `query_end = query_start + sum(M/I/S/=/X)`；
- `aligned_length = sum(M/=/X)`；`gap_count = sum(I/D)`；
- `identity = 100 * (aligned_columns - NM) / aligned_columns`（`identity_method=sam_nm`）。

FLAG：

- `0x10`：reverse strand；
- `0x100`：secondary；
- `0x800`：supplementary；
- `0x4`：unmapped；
- `0x1/0x2/0x40/0x80`：paired/first/second。

注意：BWA 的 `NM` 包含 mismatch + indel，可能不等于 `MD` 推导值；以 `NM` 为准并保留 tags。

### 6.5 常见坑

- SAM 是 1-based；坐标转换必须严格。
- `bwa mem` 默认不输出 unmapped reads（除非 `-a`）；不要假设 reads 总数等于记录数。
- CIGAR 可能包含 `S/H`，计算 reference 跨度时不能计入。
- 配对 reads 的插入片段/配对坐标需要单独建模；第一版只保留 tags，不做配对一致性判定。
- `samtools` 只用于可选排序/压缩；第一版直接解析 SAM 文本，不强制依赖。

## 7. MinHash：mash 与 sourmash

### 7.1 工具与版本

| 实现 | 二进制 | 探测命令 | 解析 | 说明 |
| --- | --- | --- | --- | --- |
| mash | `mash` | `mash --version` | `(\d+\.\d+(?:\.\d+)?)` | 2.x |
| sourmash | `sourmash` | `sourmash --version` | `sourmash\s+(\d+\.\d+\.\d+)` | 4.x |

### 7.2 索引/sketch

mash：

```bash
mash sketch -k 21 -s 1000 -o <index>/targets <targets.fasta...>
# 可选 -i 为每条序列单独 sketch
```

sourmash：

```bash
sourmash sketch dna -p k=31,scaled=1000 -o <index>/targets.sig <targets.fasta...>
```

索引 key 包含：`k`、`sketch_size` 或 `scaled`、`seed`、`canonical`、target 内容哈希、工具版本。

### 7.3 距离命令

mash：

```bash
mash dist -p 4 -d 0.1 <reference.msh> <query.msh>
```

输出列：

```text
reference  query  distance  p-value  shared/total
```

注意：mash 的参考/查询顺序容易混淆；`gseek` 在结果模型中显式标注 `query_id` / `reference_id`，并在 manifest 中记录命令。

mash screen：

```bash
mash screen -p 4 <reference.msh> <query.fastq>
```

输出列：

```text
identity  shared/total  median_multiplicity  p-value  name  comment
```

sourmash：

```bash
sourmash search <query.sig> <reference.sig> --threshold 0.0 -o <workspace>/matches.csv
```

- `-o` 写 CSV；不要使用 `--csv`（4.9.4 不支持）。
- CSV 列表头随版本变化，解析器应按表头名取列，而不是硬编码列号。
- `gather` 输出的是“最小参考集合”，语义与 `search` 不同；适配器已实现
  `mode="gather"`，解析 gather CSV 并用 `f_orig_query` 作为 containment。
- 合并 `.sig` 的 `filename` 对所有行相同，不能作唯一 ID；`reference_id` 依次取
  签名 `name`、非签名文件的 `filename`、唯一 `md5`。原始 `filename` 保留在 tags。

### 7.4 结果模型

MinHash 结果不是 alignment，使用：

```python
DistanceRecord(
    query_id=...,
    reference_id=...,
    distance=...,
    pvalue=...,
    shared_hashes=...,
    total_hashes=...,
    containment=...,
    jaccard=...,
    ani=...,
    aligned_fraction=...,
    tags={...},
)
```

`distance` 与 `containment` 的语义由适配器声明：

- mash `dist`：`distance` 是 Mash distance；
- mash `screen`：`identity` 是 containment 估计，映射为 `containment`；
- sourmash `search`：默认 Jaccard similarity，`--containment` 时为 containment；
- ANI 估算：不要静默转换；如果提供 `ani`，必须在 tags 中记录公式和 k。

### 7.5 常见坑

- mash/sourmash 的 sketch 参数必须与数据库一致；k 不匹配时结果无意义。
- 当前只支持 gseek 自己 sketch 出的 `.sig`（reference 输入仍是 FASTA/FASTQ）；SBT/zip
  预构建库尚未接入，不要把它当作已支持能力。
- 低复杂度/短序列的 sketch 不稳定；结果模型保留 `shared/total` 供消费者判断置信度。
- MinHash 距离不是比对；不要把它塞进 `AlignmentRecord`。

## 8. 适配器实现顺序

> **实现状态（2026-10-22）**：M1（BLAST+/minimap2）与 M2（KMA/MUMmer4）已完成并
> 有真实工具集成测试；M1–M4 已实现。

| 阶段 | 适配器 | 状态 | 理由 |
| --- | --- | --- | --- |
| M1 | BLAST+（blastn）、minimap2 | **完成** | 三者共用，覆盖索引/流式/坐标三类核心模式 |
| M2 | KMA、MUMmer4 | **完成** | gmlst 的另外两个后端；覆盖无坐标/多步命令 |
| M3 | BWA、mash、sourmash | **完成** | 新增能力；覆盖 SAM/距离模型 |
| M4 | BLAST 蛋白模式、sourmash gather、skani | **完成** | 补齐 gside 的现有路径 |
| M5 | 可选：bwa-mem2、minimap2 SAM、其他 | 未开始 | 社区需求 |

## 9. 适配器验收清单

每个适配器合并前必须满足：

- [ ] `BackendSpec` 声明输入/输出/索引/配对/线程/流式能力。
- [ ] `probe()` 能解析版本并在缺失/过旧时给出可操作错误。
- [ ] `plan_index()` 生成的命令可重复、参数确定、输出路径在 workspace 内。
- [ ] `plan_align()` 生成的命令不依赖 shell，路径全部绝对化。
- [ ] 解析器对空输出、截断行、非 UTF-8、CRLF、超大行有明确行为。
- [ ] 坐标/identity/coverage 的归一化有单元测试。
- [ ] 至少一份真实工具输出作为 golden fixture。
- [ ] 与对应消费者（gside/gapit/gmlst）的现有模型做 parity 断言。
- [ ] 运行 manifest 包含 binary 路径、版本、argv、输入哈希、索引 key。
- [ ] 文档更新 `adapter-matrix.md` 和 `research.md`。
