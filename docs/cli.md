# CLI

CLI 是公共 API 的薄封装：只做参数解析、调用 API、输出 JSON、映射退出码；没有交互式
提示，也不会执行 shell 字符串。适合 CI、Snakemake/Nextflow 和人工排障。

完整选项矩阵见 [API 参考 §12](api.md#12-cli)；本页给常用路径与自动化约定。

## 全局选项

```bash
gseek --version
gseek --error-format {json,text} <command> ...
```

| 选项 | 说明 |
| --- | --- |
| `--version` | 打印 `gseek 0.9.0` |
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
# FASTA 参考，走普通索引
gseek distance --backend sourmash --query genome.fna --reference refs/ -o out.json

# 预构建 mash sketch
gseek distance \
  --backend mash \
  --query assembly.fna \
  --reference refs.msh \
  --external-reference \
  --param max_distance=1.0
```

`--external-reference` 表示 `--reference` 是预构建产物：单个 `.msh` / `.sig` /
`.zip`，或 sourmash rocksdb / skani database 目录。不建索引、不重新 sketch
reference；目录按确定性 tree hash 记录。

## 机器可读输出

- `backends --json`、`doctor --json`、`index list/gc --json`、`verify --json`、
  `replay --json` 输出单个 JSON 文档。
- `align` / `distance` 默认输出一个 JSON 文档；`-o` 原子写入文件。
- 正常结果写 **stdout**；错误与诊断写 **stderr**。
- 命令不读取 stdin，不弹交互提示。

示例：

```bash
gseek backends --json | jq '.[].name'
gseek doctor --json | jq '.[] | select(.ok == false)'
```

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 未预期错误，或 `doctor` 发现缺失工具 |
| `2` | 用法/配置/不支持 |
| `3` | 工具缺失或版本错误 |
| `4` | 执行、索引、缓存或 manifest 错误 |
| `5` | 输入或解析错误 |
| `6` | replay `result_hash` 不一致 |
| `130` | 用户中断 |

具体异常类与 `code` 见 [错误模型](errors.md)。

## 自动化注意事项

1. 不要解析人类输出；用 `--json` 或 `align`/`distance` 的 JSON 文档。
2. 失败时先看进程退出码，再解析 stderr 的 `gseek.error/1`。
3. `replay` 不会执行 manifest 中记录的任意命令；只按 adapter 重新规划。
4. 并发调用同一索引时，`IndexStore` 使用文件锁；`index gc --apply` 建议避开运行中的任务。
5. 外部工具必须能被 `GSEEK_<BINARY>`、`GSEEK_PATH_PREPEND` 或 `PATH` 找到；
   先用 `gseek doctor` 检查。
