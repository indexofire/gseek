# 错误模型

[English](errors.md) | **简体中文**

gseek 的失败分为**类型化异常**和**稳定的进程退出码**。库调用方应捕获
`gseek.GseekError` 并读取字段，而不是解析错误字符串。

## 1. 异常层级

所有已知错误继承 `GseekError`：

```python
from gseek import GseekError

try:
    result = gseek.align(...)
except GseekError as exc:
    print(exc.code)        # 稳定 code，例如 TOOL_NOT_FOUND
    print(exc.message)     # 人类可读描述
    print(exc.context)     # dict[str, str]，例如 backend/path
    print(exc.hint)        # 可操作建议，可能为空
    print(exc.exit_code)   # CLI 对应退出码
```

| 异常 | `code` | 退出码 | 典型原因 |
| --- | --- | --- | --- |
| `GseekError` | `GSEEK_ERROR`（可覆盖） | 1 | 基类/未分类 |
| `UsageError` | `USAGE_ERROR` | 2 | 参数组合非法、未知 backend |
| `ConfigError` | `CONFIG_ERROR` | 2 | 环境/配置缺失 |
| `UnsupportedFeatureError` | `UNSUPPORTED_FEATURE` | 2 | 适配器不支持该输入/参数 |
| `ToolNotFoundError` | `TOOL_NOT_FOUND` | 3 | 外部工具不在 PATH/覆盖路径 |
| `ToolVersionError` | `TOOL_VERSION_ERROR` | 3 | 版本过旧或无法解析 |
| `ExecutionError` | `EXECUTION_FAILED` | 4 | 子进程非零退出/超时 |
| `IndexBuildError` | `INDEX_BUILD_FAILED` | 4 | 索引构建失败 |
| `IndexLockError` | `INDEX_LOCK_TIMEOUT` | 4 | 并发索引锁超时 |
| `CacheError` | `CACHE_ERROR` | 4 | 缓存损坏/禁用时写入 |
| `ManifestError` | `MANIFEST_ERROR` | 4 | manifest 读取/校验失败 |
| `ParseError` | `PARSE_ERROR` | 5 | 工具输出无法解析 |
| `InputError` | `INPUT_ERROR` | 5 | 输入文件/格式错误 |
| `DuplicateIdError` | `DUPLICATE_SEQUENCE_ID` | 5 | FASTA/FASTQ ID 重复 |
| `ReplayMismatchError` | `REPLAY_MISMATCH` | 6 | replay 的 `result_hash` 不一致 |

`InputError` 的子类保留自己的 `code`，但沿用退出码 `5`。

## 2. JSON envelope

`GseekError.to_dict()` 和 CLI 默认 stderr 都使用 `gseek.error/1`：

```json
{
  "schema": "gseek.error/1",
  "code": "TOOL_NOT_FOUND",
  "message": "required tool 'blastn' was not found",
  "context": {"binary": "blastn"},
  "hint": "install BLAST+ or set GSEEK_BLASTN"
}
```

CLI 默认输出一行 JSON；需要人类可读格式：

```bash
gseek --error-format text align ...
# gseek: --param must be KEY=VALUE, got 'bad'
# hint: ...
```

## 3. 常见处理方式

### 工具缺失

```python
except ToolNotFoundError as exc:
    # 先跑 gseek doctor，或设置 GSEEK_<BINARY>
    raise SystemExit(f"install {exc.context.get('binary', 'the tool')}")
```

### 版本过旧

`ToolVersionError` 的 `context` 包含 `required` / `found`。不要静默降级；升级工具或
显式选择兼容适配器。

### 输入/解析错误

`ParseError` 带 `path` / `line_number`；`InputError` 带输入路径。不要只打印
`str(exc)`，保留上下文便于定位。

### replay 不一致

`ReplayMismatchError` 表示同一 recipe 的 canonical `result_hash` 不同。优先检查：

1. 外部工具版本是否变化；
2. 参考/输入文件哈希是否变化；
3. 是否使用了不同的 index 状态；
4. manifest 是否来自另一个 gseek 版本。

## 4. 不要做的事

- 不要按错误消息文本分支；用 `code` 或异常类型。
- 不要吞掉 `GseekError` 后返回空结果；调用方无法区分“无命中”和“失败”。
- 不要把 `KeyboardInterrupt` 包装成 `GseekError`；CLI 返回 `130`。
- 不要在 CLI 自动化里解析 `text` 错误格式；用默认 JSON。
