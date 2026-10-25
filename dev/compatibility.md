# gseek 兼容性与迁移

> 目标：让 gside/gapit/gmlst 在不改变用户可见行为的前提下，逐步把比对层切换到 `gseek`。  
> 原则：**先冻结行为，再替换实现；先影子模式，再删除旧代码。**

## 1. 迁移总览

```text
Phase 0  冻结黄金用例
   │
Phase 1  gmlst: blastn + minimap2 (feature flag)
   │
Phase 2  gapit: blastn + minimap2; gside: marker/reads
   │
Phase 3  gmlst: kma + nucmer; gside: mash/sourmash
   │
Phase 4  BWA/MinHash 新能力; 删除旧适配器; 1.0
```

每个阶段都必须同时满足：

1. 旧路径的黄金测试仍全绿；
2. gseek 路径与旧路径的规范化结果逐字段一致（或有文档化的差异）；
3. 真实样本端到端结果一致；
4. 性能不退化；
5. 回滚开关可用。

## 2. gmlst 迁移

### 2.1 现有模型

`gmlst/aligners/base.py`：

```python
AlleleMatch(
    locus, allele_id, identity, coverage, strand, score, depth,
    alignment_length, sequence, copy_count,
    query_contig, query_contig_length, query_start, query_end,
    allele_length, allele_start, allele_end,
)

AlignmentResult(sample_id, matches, failed_loci, backend, runtime_seconds, fragments)
```

### 2.2 字段映射

gmlst 的方向是 **alleles = query，genome = target**（通过 `-subject` 避免建库）。gseek 推荐 **genome = query，alleles = target**（索引可缓存）。因此映射时 query/target 互换：

| gmlst 字段 | gseek 来源（genome=query, alleles=target） | 说明 |
| --- | --- | --- |
| `locus` | `split_id(record.target_id)` 的第一段 | 业务解析留在 gmlst |
| `allele_id` | `split_id(record.target_id)` 的第二段 | 同上 |
| `identity` | `record.identity` | 百分比 |
| `coverage` | `record.target_coverage` | allele 被覆盖比例 |
| `strand` | `record.strand` | query 相对 target |
| `score` | `record.score` 或 `record.bit_score` | 后端特定；gmlst 目前用 identity*coverage 归一化 |
| `depth` | `record.depth` | KMA FASTQ |
| `alignment_length` | `record.aligned_length` | |
| `sequence` | `record.target_aligned` 或从 query FASTA 切片 | novel allele 提取；注意方向 |
| `copy_count` | `views.count_distinct_intervals(records, key)` | gmlst 目前按 distinct subject intervals 计数 |
| `query_contig` | `record.query_id` | genome contig |
| `query_contig_length` | `record.query_length` | |
| `query_start/end` | `record.query_start/end` | 0-based half-open；gmlst 期望 0-based |
| `allele_length` | `record.target_length` | |
| `allele_start/end` | `record.target_start/end` | 0-based half-open |
| `failed_loci` | `set(loci) - set(mapped loci)` | 由 gmlst 计算，gseek 不提供 locus 概念 |
| `fragments` | `result.records` 全量 | gseek 默认不 collapse；gmlst 的 `select_best_matches` 用 `best_per_query_target()` |
| `sample_id` | gmlst 自己传入/保留 | gseek 不做样本命名 |

### 2.3 行为差异与处理

| 行为 | gmlst 现状 | gseek | 迁移处理 |
| --- | --- | --- | --- |
| 最佳行选择 | `select_best_matches`：identity desc，再 coverage desc，平局保留 first-seen | `best_per_query_target()` 同规则，但排序稳定键不同 | parity 测试逐字段比较；平局时用 `record_index` 保留原始顺序 |
| fragment 上限 | 每 locus 最多 100 条，按 identity/length 排序 | 默认不设上限；可传 `max_records` | gmlst 继续调用自己的 `cap_fragments` 视图 |
| BLAST coverage | `aln_len/qlen` | `target_coverage`（区间跨度/target 长度）+ `ungapped_target_coverage` | 对无 gap 的等位基因两者一致；有 gap 时用 `ungapped_target_coverage` 或保留旧公式的 view |
| PAF identity | `nmatch/(nmatch+NM)` | 默认 `(alen-NM)/alen` | 提供 `identity_nmatch_nm` 属性；gmlst 迁移时先用旧公式做 parity，再评估是否统一 |
| PAF coverage | `(qend-qstart)/qlen` | `query_coverage` | 一致 |
| KMA coverage | `Template_Coverage/100` | `target_coverage` | 一致 |
| KMA identity | `Template_Identity` | `identity` | 一致 |
| KMA depth | 仅 FASTQ 保留 | `depth` 原样；由消费者决定是否使用 | 一致 |
| nucmer strand | 硬编码 `"+"` | 从 `S2>E2` 推断 | **行为修复**：golden 可能变化；需在迁移说明中标注 |
| minimap2 secondary | `--secondary=no` | 默认 primary only（可配置） | 命令规划需保留 `--secondary=no` |
| BLAST FASTQ | 拒绝，提示用 KMA | 能力声明拒绝，错误码 `UNSUPPORTED_INPUT` | 保持提示文案 |
| 空结果 | 返回空 matches + failed_loci | 返回空 records + stats | 一致 |
| 运行时 | `runtime_seconds` 仅 align 阶段 | `stats.runtime_seconds` 含 parse；`manifest` 分阶段 | 迁移时用 `manifest` 的 align 命令耗时，或接受微小差异 |

### 2.4 迁移开关

建议在 gmlst 中加：

```python
# gmlst/aligners/__init__.py
USE_GSEEK = os.getenv("GMLST_USE_GSEEK", "0") == "1"

def get_aligner(backend, **kwargs):
    if USE_GSEEK and backend in {"blastn", "minimap2", "kma", "nucmer"}:
        return GseekAlignerAdapter(backend, **kwargs)
    return _legacy_get_aligner(backend, **kwargs)
```

影子模式：

```python
result_old = legacy_align(...)
result_new = gseek_align(...)
diff = compare_normalized(result_old, result_new)
if diff and os.getenv("GMLST_GSEEK_SHADOW_STRICT") == "1":
    raise AssertionError(diff)
logger.warning("gseek shadow diff: %s", diff.summary())
return result_old
```

### 2.5 gmlst 验收清单

- [ ] `test_aligner_golden.py` 的 BLAST/PAF 解析断言全过。
- [ ] `test_minimap2_aligner.py` 的命令拼装断言（CIGAR/profile）有对应 gseek plan 测试。
- [ ] `test_kma_aligner.py` 的 `.res` 解析断言全过。
- [ ] nucmer 反向命中新增测试；golden 更新需说明。
- [ ] 真实 scheme（至少一个 MLST + 一个 cgMLST）的 typing 结果与旧路径一致。
- [ ] `--max-workers` 路径下索引只构建一次；结果顺序一致。
- [ ] novel allele 提取的 sequence 方向正确。
- [ ] 失败/缺失工具的错误信息仍可操作。

## 3. gapit 迁移

### 3.1 现有模型

- `BlastRow`（pydantic, frozen）：15 列。
- `PafRecord`（pydantic, frozen）：12 列 + `tp/NM/cs`。
- `Hit`：应用层字段（gene/database/accession/product/coverage_map/fragments）。

### 3.2 字段映射

| gapit 字段 | gseek 来源 | 说明 |
| --- | --- | --- |
| `qseqid` | `record.query_id` | |
| `qstart/qend` | `record.query_start + 1` / `record.query_end` | gapit 保留 BLAST 1-based inclusive；转换时加 1 |
| `qlen` | `record.query_length` | |
| `sseqid` | `record.target_id` | |
| `sstart/send` | 由 `record.target_start/end` + `strand` 还原 | gapit 需要原始方向 |
| `slen` | `record.target_length` | |
| `sstrand` | `record.strand` → `"minus"/"plus"` | |
| `evalue` | `record.evalue` | |
| `length` | `record.aligned_length` | |
| `pident` | `record.identity` | |
| `gaps` | `record.gap_count` | |
| `gapopen` | `record.gap_openings` | |
| `stitle` | `record.target_description` | |

`Hit` 的派生字段：

| gapit | gseek/views | 说明 |
| --- | --- | --- |
| coverage_pct | `views.target_coverage(record, mode="ungapped")` | gapit 用 `(length-gaps)/slen` |
| coverage_map | gapit 自己的 `minimap()` | 渲染逻辑留在 gapit |
| aligned_len | `aligned_length - gap_count` | |
| gene/database/accession/product | gapit 的 `decode_seqid` | 业务解析留在 gapit |
| fragments | `views.fragment_merge()` 或 gapit 自己 | gapit 的 `--merge-fragments` 是应用策略 |
| merged | gapit | |

### 3.3 行为差异与处理

| 行为 | gapit 现状 | gseek | 迁移处理 |
| --- | --- | --- | --- |
| dedup key | `(qseqid, qstart, qend)`，忽略 strand，first wins，且 key 在 coverage 过滤前占用 | 默认不去重 | gapit 继续用自己的 `_swap_dedup` 视图；或 `result.deduplicate(key=...)` 需要支持“先占 key 再过滤” |
| coverage filter | `(length-gaps)/slen >= mincov`，用未舍入浮点 | `ungapped_target_coverage` | 用 gseek 属性或保留 gapit 计算 |
| primary filter | `tp:A:S` 过滤 | 默认 primary only | 一致 |
| BLAST 蛋白模式 | `blastx` + `-task blastx-fast`，无 `-perc_identity` | M4 支持 | 迁移时保留参数差异 |
| 输入规范化 | `seqconvert` 转 FASTA + stdin | gseek 支持 FASTA/FASTQ；GBK/EMBL 转换留在 gapit | gseek 只接收 FASTA/FASTQ |
| 输出顺序 | `sorted(hits, key=(sequence,start))` | 稳定排序键不同 | gapit 继续排序 |
| 错误 envelope | `gapit.error/1` + exit code | `gseek.error/1` | gapit 捕获并转换，保持自己的 envelope |

### 3.4 gapit 验收清单

- [ ] `tests/golden/*.tsv|json|md` 全过。
- [ ] `tests/parity/` 与 abricate 的 parity 不退化。
- [ ] reads/cluster 路径的 PAF 解析与聚合一致。
- [ ] 蛋白模式（M4）参数与 gapit 现有一致。
- [ ] 错误 envelope/exit code 不变。
- [ ] 大输入下不物化全部记录（流式/上限）。

## 4. gside 迁移

### 4.1 现有模型

- `Hit`（BLAST tabular）。
- `KmerDistance`（mash/sourmash）。
- `AniHit`（skani）。
- `screen_reads()` 返回 `list[dict]`（gene/identity/coverage/ref_len/seqid）。

### 4.2 字段映射

BLAST `Hit`：

| gside | gseek |
| --- | --- |
| query_id | `record.query_id` |
| subject_id | `record.target_id` |
| identity | `record.identity` |
| query_coverage | `record.query_coverage * 100` |
| subject_coverage | `record.target_coverage * 100` |
| evalue/bit_score | `record.evalue` / `record.bit_score` |
| query_start/end | `record.query_start/end` |
| subject_start/end | `record.target_start/end` |
| strand | `record.strand` |
| alignment_length | `record.aligned_length` |
| query_length/subject_length | `record.query_length/target_length` |
| mismatches | `record.mismatches` |
| backend | `result.backend` |

MinHash：

| gside | gseek |
| --- | --- |
| `KmerDistance.query_id` | `DistanceRecord.query_id` |
| `reference_id` | `reference_id` |
| `distance` | `distance` |
| `pvalue` | `pvalue` |
| `shared_hashes` | `shared_hashes` |
| `total_hashes` | `total_hashes` |
| `backend` | `result.backend` |

skani `AniHit`：M4 增加 `SkaniAdapter` 后再映射；第一版不在范围内。

### 4.3 行为差异与处理

| 行为 | gside 现状 | gseek | 迁移处理 |
| --- | --- | --- | --- |
| reads preset | 按平均读长 `<300` 选 `sr`，否则 `map-ont` | 适配器提供 `preset` 参数；可提供 helper | gside 保留选择逻辑，显式传 preset |
| reads 聚合 | 按 target 区间并集 breadth + alignment-length 加权 identity | `summarize_by_target()` | 用 gseek 视图；核对权重公式 |
| secondary | `--secondary=no` + `tp` 过滤 | 默认 primary only | 一致 |
| sourmash 输出 | `search -o /dev/stdout --csv`（4.9.4 不支持 `--csv`） | `-o file` + 按表头解析 CSV | **修复 gside 的调用**；golden 更新 |
| mash 顺序 | `mash dist query ref`，输出第一列是 ref | `mash dist ref query`，输出首列映射到 `reference_id` | 保持输出语义；迁移时对调参数 |
| BLAST 蛋白 | 支持 5 种 task | M4 | 先迁移 marker（blastn），蛋白后补 |
| 无工具/无数据库 | 返回结构化 error，不崩溃 | `ToolNotFound`/`Diagnostic` | gside 捕获并保持 JSON 字段 |
| skani | 独立 ANI | M4 | 暂不迁移 |

### 4.4 gside 验收清单

- [ ] `test_marker_golden.py`、`test_agreement.py` 全过。
- [ ] `test_read_screening_units.py` 的聚合公式一致。
- [ ] `test_ani_golden.py`（mash）全过。
- [ ] `test_sourmash_golden.py` 全过（修正 `--csv` 后）。
- [ ] 物种鉴定仲裁结果不变。
- [ ] 缺工具/缺库的 JSON 结构不变。

## 5. 通用兼容工具

### 5.1 当前 parity 方式

`gseek.testing` 尚未发布（早期草案，不在 0.8.0 公共 API 中）。当前 parity 使用
`tests/parity/test_real_tool_fixtures.py` 中的显式字段断言，直接对照消费者代码里的
公式，而不是依赖一个通用的记录比较器。

| 消费者/后端 | 对照公式 |
| --- | --- |
| gside BLAST | `coverage = 100 * aligned_length / query_length` |
| gside minimap2 | `identity = 100 * nmatch / alen` → `identity_from_matches` |
| gapit BLAST | 15 列 outfmt；`sstrand` → `strand`，坐标 1-based→0-based |
| gapit minimap2 | `identity = 100 * (alen - NM) / alen` → `identity_from_nm` |
| gmlst BLAST | `query=allele`、`subject=contig`、`coverage=alen/qlen` |
| gmlst minimap2 | `identity = 100 * nmatch / (nmatch + NM)` → `identity_nmatch_nm` |
| gmlst KMA | `identity=Template_Identity`、`coverage=Template_Coverage/100` |
| gmlst MUMmer | `identity=[% IDY]`、`coverage=COV Q/100`；gseek 额外恢复反向 strand |

### 5.2 视图映射

| 消费者 | 视图 |
| --- | --- |
| gmlst | `best_per_query_target()`、`aligned_length`、`count_distinct_occurrences()` |
| gapit | `primary_only()`、`identity_from_nm`、`ungapped_target_coverage` |
| gside | `summarize_by_target()`、`identity_from_matches`、`distance` |

### 5.3 兼容性测试资产

```text
tests/fixtures/                 # 真实工具原始输出（生成命令/版本见 README）
├── blast/blastn_nucl.tsv
├── minimap2/align.paf
├── mummer/coords.txt
├── kma/align.res
├── bwa/align.sam
├── mash/{dist,screen}.tsv
├── sourmash/{search,gather}.csv
└── skani/search.tsv

tests/parity/
└── test_real_tool_fixtures.py  # 字段/公式 parity

tests/integration/
└── test_replay_matrix.py       # 8 后端 × ≥2 记录 manifest replay
```

每个 fixture 都来自真实工具输出；`tests/fixtures/README.md` 记录了工具版本、
输入种子和生成命令。replay 矩阵断言 manifest 的 `result_hash` 等于规范化记录哈希，
并对同一 manifest 执行 `replay --check`。

## 6. 迁移风险清单

| 风险 | 触发点 | 检查 |
| --- | --- | --- |
| 方向翻转导致 coverage 取错 | gmlst BLAST 从 alleles=query 改为 genome=query | 用 `target_coverage`；parity 对比 |
| 1-based/0-based 混用 | gapit 需要原始 BLAST 坐标 | 转换测试；round-trip |
| identity 公式变化 | minimap2 | 显式 `identity_method`；gmlst 先用旧公式 |
| 平局顺序变化 | best-per | 保留原始 `record_index`；稳定排序 |
| 索引 key 漏参数 | BLAST `-task`、minimap2 preset、KMA `-k` | 适配器参数清单 + 测试 |
| 空结果/缺文件 | 所有后端 | 空 fixture + 错误路径测试 |
| 工具版本差异 | 多环境 | CI 固定版本 + manifest 记录 |
| 输出舍入差异 | TSV/JSON | 核心保留原始；展示层统一 |
| 性能退化 | 记录物化/重复哈希 | 流式 + 缓存 + 基准 |

## 7. 迁移完成标准

- [ ] 三个消费者都通过 feature flag 使用 gseek。
- [ ] 旧路径保留至少一个 release，可一键回滚。
- [ ] parity 测试在 CI 中常驻，覆盖所有已迁移后端。
- [ ] 文档明确：哪些字段由 gseek 提供，哪些仍由消费者计算。
- [ ] 删除旧适配器后，代码行数净减少 ≥ 30%。
- [ ] 用户可见输出（CLI/JSON/TSV）无意外变化。
- [ ] 性能基准不退化。
