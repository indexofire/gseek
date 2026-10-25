# Quickstart

目标：5 分钟内完成第一次比对，并得到可 `verify` / `replay` 的 manifest。

## 1. 环境

- Python **≥ 3.12**
- 外部工具按需安装，不随 Python 包分发：

```bash
pixi add blast minimap2 mummer4 kma bwa mash sourmash skani
# 或
conda install -c bioconda blast minimap2 mummer4 kma bwa mash sourmash skani
```

`gseek` 自身零运行时依赖。

## 2. 安装

PyPI 已发布 0.8.0。也可以使用本地 wheel 或源码：

```bash
# 本地 wheel（推荐验证发布物）
uv pip install dist/gseek-0.8.0-py3-none-any.whl

# 或源码开发模式
pip install -e ".[dev]"

# 或临时注入源码
export PYTHONPATH=/path/to/gseek/src
```

确认：

```bash
gseek --version          # gseek 0.8.0
gseek backends           # 8 个后端
gseek doctor             # 检查外部工具；缺失时返回 1
```

## 3. 第一个 CLI 比对

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

- `--index auto`：命中内容寻址缓存则复用，否则构建。
- `--manifest`：记录输入哈希、工具版本、命令、结果哈希。
- `-o`：结果 JSON；不写则输出到 stdout。
- 退出码：`0` 成功；`2` 用法错误；`3` 工具缺失；`4` 执行/索引；`5` 输入/解析；`6` replay 不一致。

查看结果：

```bash
python -m json.tool runs/sample.json | head -40
```

## 4. 第一个 Python 调用

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
```

`gseek.distance(...)` 用于 mash/sourmash/skani：

```python
result = gseek.distance(
    backend="mash",
    query=Path("assembly.fna"),
    reference=Path("refs.msh"),
    external_reference=True,   # reference 是预构建 .msh，不是 FASTA
    params={"max_distance": 1.0},
    manifest=Path("runs/mash.manifest.json"),
)
```

CLI 等价：

```bash
gseek distance \
  --backend mash \
  --query assembly.fna \
  --reference refs.msh \
  --external-reference \
  --param max_distance=1.0 \
  --manifest runs/mash.manifest.json
```

sourmash 也可以把 **rocksdb 目录**作为 external reference：

```bash
gseek distance \
  --backend sourmash \
  --query genome.fna \
  --reference refs.rocksdb \
  --external-reference \
  --param k=31 \
  --param scaled=1000
```

## 5. 校验与重放

```bash
gseek verify runs/sample.manifest.json
gseek replay runs/sample.manifest.json --check --json
```

- `verify` 校验 manifest 中记录的文件哈希；不一致返回 `4`。
- `replay` 按 adapter 重新规划，**不会执行 manifest 里的 argv**；`--check` 比较
  canonical `result_hash`，不一致返回 `6`。

## 6. 出错时

默认错误是 stderr 上的一行 `gseek.error/1` JSON：

```json
{"schema":"gseek.error/1","code":"TOOL_NOT_FOUND","message":"...","context":{},"hint":"..."}
```

需要人类可读输出时：

```bash
gseek --error-format text align --backend blast --query q.fna --target t.fna
```

库调用方捕获 `gseek.GseekError`，读取 `code`、`context`、`hint`、`exit_code`。
详见 [错误模型](errors.md)。

## 7. 下一步

- [CLI 约定](cli.md)：JSON、退出码、自动化注意事项
- [API 参考](api.md)：结果模型、适配器协议、索引与 schema
- [适配器矩阵](../dev/adapter-matrix.md)：各工具参数与版本
- [兼容性迁移](../dev/compatibility.md)：gside/gapit/gmlst 的字段差异
