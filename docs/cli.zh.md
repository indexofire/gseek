# CLI 参考

[English](https://indexofire.github.io/gseek/) | **简体中文**

CLI 是公共 API 的薄封装：只做参数解析、调用 API、输出 JSON、映射退出码；没有
交互式提示，也不会执行 shell 字符串。适合 CI、Snakemake/Nextflow 和人工排障。
完整选项矩阵见 [API 参考 §10](api.zh.md#10-cli)。

## 全局选项

```bash
gseek --version
gseek --error-format {json,text} <command> ...
```

| 选项 | 说明 |
| --- | --- |
| `--version` | 打印 `gseek 0.10.3` |
| `--error-format json` | 默认；stderr 一行 `gseek.error/1` JSON |
| `--error-format text` | 人类可读的 `gseek: message` + `hint:` |

`--error-format` 必须放在子命令之前。

## 命令

| 命令 | 用途 |
| --- | --- |
| `gseek backends [--json]` | 能力矩阵与适配器元数据 |
| `gseek doctor [--json]` | 探测外部工具路径/版本；缺失返回 1 |
| `gseek index list [--json]` | 列出内容寻址索引缓存 |
| `gseek index gc [--apply] [--json]` | 垃圾回收；默认 dry-run |
| `gseek manifest verify PATH [--json]` | 检查 manifest 记录的文件哈希 |
| `gseek verify PATH [--json]` | 同上，便捷别名 |
| `gseek replay PATH [--check] [--json]` | 重新规划并重放，不执行旧 argv |
| `gseek align ...` | 比对 FASTA/FASTQ 到 target |
| `gseek distance ...` | mash/sourmash/skani 距离/ANI |

## align

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

关键选项：

- `--query` / `--target` 可重复；`--paired` 时 `--query` 给两个 FASTQ。
- `--param KEY=VALUE` 传适配器参数；值先按 JSON 解析，失败按字符串。
- `--index auto|reuse|rebuild|none`。
- `--manifest` 写 `gseek.run/1`；`-o` 写结果 JSON，缺省 stdout。

## distance

```bash
# FASTA reference: gseek builds or reuses an index
gseek distance --backend sourmash --query genome.fna --reference refs/ -o out.json

# prebuilt mash sketch
gseek distance \
  --backend mash \
  --query assembly.fna \
  --reference refs.msh \
  --external-reference \
  --param max_distance=1.0
```

`--external-reference` 表示 `--reference` 是预构建产物：单个 `.msh` / `.sig` /
`.zip`，或 sourmash rocksdb / skani database 目录，而不是待建索引的 FASTA。
gseek 不会重新 sketch；目录按确定性 tree hash 记录。

## 机器可读输出

- 结果写 **stdout**，一个 JSON 文档；`-o` 改写文件。
- 错误写 **stderr**，默认是 `gseek.error/1` JSON。
- 信息类命令（`backends`、`doctor`、`index list`、`index gc`、`verify`、
  `replay`）的 `--json` 输出一个 JSON 文档，而不是文本。
- 进度与提示不会污染 stdout。

示例：

```bash
gseek backends --json | jq '.[].name'
gseek doctor --json | jq '.[] | select(.ok == false)'
```

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 未分类的 `GseekError`（例如 `doctor` 发现缺失工具） |
| `2` | 用法或配置错误 |
| `3` | 工具缺失或版本不支持 |
| `4` | 执行、索引、缓存或 manifest 错误 |
| `5` | 输入或解析错误 |
| `6` | replay `result_hash` 不一致 |
| `130` | 用户中断 |

## 自动化注意事项

1. 不要解析人类可读输出；用 `--json` 或 `align`/`distance` 的 JSON 文档。
2. 失败时先看进程退出码，再解析 stderr 的 `gseek.error/1` JSON。
3. `gseek replay --check` 可安全用于不可信 manifest：它按 adapter 重新规划，
   不会执行记录下来的 argv。
4. 并发调用共享索引文件锁；`gseek index gc --apply` 应避开正在构建的任务。
5. 外部工具必须能通过 `GSEEK_<BINARY>`、`GSEEK_PATH_PREPEND` 或 `PATH` 找到；
   先用 `gseek doctor` 检查。
