# Quickstart

[English](https://indexofire.github.io/gseek/) | **简体中文**

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

可以选择 PyPI 发布的 0.10.3、本地构建的 wheel，或源码开发模式：

```bash
pip install gseek==0.10.3

# 或安装本地构建的 wheel
uv pip install dist/gseek-0.10.3-py3-none-any.whl

# 或从源码运行
pip install -e ".[dev]"
export PYTHONPATH=/path/to/gseek/src
```

确认安装：

```bash
gseek --version          # gseek 0.10.3
gseek backends           # 能力矩阵，8 个后端
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

关键参数：

- `--index auto`：命中内容寻址缓存则复用，否则构建。
- `--manifest`：写出 `gseek.run/1`，记录输入哈希、工具版本、命令与结果哈希。
- `-o`：结果 JSON；不写则输出到 stdout。

退出码：`0` 成功；`2` 用法错误；`3` 工具缺失；`4` 执行/索引；`5` 输入/解析；
`6` replay 不一致。

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

best = result.best_per_query_target()
```

## 5. 距离与 ANI

```python
result = gseek.distance(
    backend="mash",
    query=Path("assembly.fna"),
    reference=Path("refs.msh"),
    external_reference=True,   # 预构建 .msh，不是 FASTA
    params={"max_distance": 1.0},
    manifest=Path("runs/mash.manifest.json"),
)
```

```bash
gseek distance \
  --backend mash \
  --query assembly.fna \
  --reference refs.msh \
  --external-reference \
  --param max_distance=1.0 \
  --manifest runs/mash.manifest.json
```

`sourmash` 也可以把 **rocksdb 目录**作为 external reference：

```bash
gseek distance \
  --backend sourmash \
  --query genome.fna \
  --reference refs.rocksdb \
  --external-reference \
  --param k=31 \
  --param scaled=1000
```

`sourmash` 与 `skani` 的使用方式相同；参见 [API 参考](api.zh.md#24-gseekdistance)
与 [CLI 参考](cli.zh.md#distance)。

## 6. 校验与重放

```bash
gseek verify runs/sample.manifest.json
gseek replay runs/sample.manifest.json --check --json
```

- `verify` 校验 manifest 中记录的文件与哈希；不一致返回 `4`。
- `replay` 按 manifest 重新规划，并可比较新的 `result_hash`；不一致返回 `6`。
- replay **不会执行 manifest 里的 argv**；它让适配器重新规划。

## 7. 出错时

默认错误是 stderr 上的一行 `gseek.error/1` JSON：

```json
{"schema":"gseek.error/1","code":"TOOL_NOT_FOUND","message":"...","context":{},"hint":"..."}
```

需要人类可读输出时：

```bash
gseek --error-format text align --backend blast --query q.fna --target t.fna
```

库调用方捕获 `gseek.GseekError`，读取 `code`、`context`、`hint` 与 `exit_code`。
详见 [错误模型](errors.zh.md)。

## 8. 下一步

- [CLI 参考](cli.zh.md)：JSON 输出、退出码、自动化约定。
- [API 参考](api.zh.md)：结果模型、适配器协议、索引与 schema。
- [适配器矩阵](https://github.com/indexofire/gseek/blob/main/dev/adapter-matrix.md)：
  各工具参数与版本下限。
- [兼容性迁移](https://github.com/indexofire/gseek/blob/main/dev/compatibility.md)：
  gside、gapit、gmlst 的字段差异。
