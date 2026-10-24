# Contributing to gseek

感谢贡献。`gseek` 是 gside/gapit/gmlst 共用的 DNA 比对基础设施；正确性、可复现性和向后兼容比“多写代码”更重要。

## 1. 开发环境

```bash
pixi install
pixi run check
```

需要 Python ≥ 3.12。外部比对工具（BLAST+、minimap2、MUMmer4、KMA、BWA、mash、sourmash）只在集成测试中需要，不随包安装。

## 2. TDD：先失败，再实现

**没有失败的测试，不写生产代码。** 详细规则见 [AGENTS.md](AGENTS.md)。

### 2.1 最小示例

假设要新增 `gseek.views.best_per_query`：

1. 在 `tests/test_views.py` 写测试：
   ```python
   def test_best_per_query_keeps_highest_identity() -> None:
       ...
       assert best[0].identity == 99.0
   ```
2. 运行并确认失败：
   ```bash
   pixi run test tests/test_views.py::test_best_per_query_keeps_highest_identity
   ```
   失败原因必须是 `AttributeError`/`ImportError` 等“功能不存在”，不能是测试写错。
3. 写最少实现。
4. 目标测试通过。
5. 跑全量：
   ```bash
   pixi run check
   ```
6. 在绿的前提下重构。

### 2.2 Bug 修复

先写回归测试，确认它能在当前代码上复现 bug；再修复。禁止“先修再看”。

### 2.3 重构/重命名

先写新契约测试（例如 `tests/test_project_identity.py` 对 `gseek` 的断言），确认红；再改代码；最后全绿。

## 3. 测试分层

| 层级 | 位置 | 依赖外部工具 | 说明 |
| --- | --- | --- | --- |
| 单元 | `tests/test_*.py` | 否 | 模型、解析、命令规划、hash、index |
| golden | `tests/fixtures/` | 否 | 真实工具输出，逐字段断言 |
| 集成 | `tests/integration/` | 是 | `@pytest.mark.integration`，无工具 skip |
| parity | `tests/parity/` | 部分 | 与 gside/gapit/gmlst 旧模型对比 |

要求：

- 单元测试不联网、不依赖 PATH 里的工具。
- 所有临时文件用 `tmp_path`。
- 不 mock 掉被测逻辑；只 mock 进程边界（`Executor`/resolver）。
- 解析器测试必须覆盖：正常、反向、gapped、空结果、坏行、CRLF/空白。
- 新适配器必须包含命令规划测试与真实输出 golden。

## 4. 架构红线

- 只有 `src/gseek/execution.py` 启动子进程。
- 适配器只做规划/解析，不做生物学判定。
- 结果模型无损；归并是显式视图。
- 坐标 0-based half-open；strand 单独字段。
- 索引内容寻址，不用 mtime。
- 核心零运行时依赖。
- 不使用 `shell=True`。
- 具体规则见 [AGENTS.md](AGENTS.md)。

## 5. 代码风格

```bash
pixi run format
pixi run lint
pixi run typecheck
```

- `ruff` + `ruff format`，行宽 100。
- `pyright` standard 0 error。
- 公共 API 写 docstring 和类型注解。
- 异常使用 `gseek.errors`。
- 单文件建议 ≤ 350 行。

## 6. 文档

- 公共 API/行为变化更新 `docs/api.md`；设计/架构说明更新 `dev/` 对应文档。
- 架构/版本/命名决策写 ADR：`dev/decisions/NNNN-title.md`。
- 用户可见变化更新 `CHANGELOG.md`。
- MUMmer：目标为 **MUMmer 4.x**，见 [ADR-0001](dev/decisions/0001-mummer4.md)。

## 7. 提交与 PR

提交信息用英文祈使句：

```text
Add FASTA duplicate-ID validation
Fix PAF reverse-strand coordinate normalization
Rename package to gseek
```

PR 描述包含：

1. 行为变化（用户可见）
2. 测试说明（先失败的测试是哪一个）
3. 验证命令与结果
4. 文档/CHANGELOG/ADR 影响

## 8. 发布

1. 更新 `src/gseek/version.py` 与 `pyproject.toml` 的版本。
2. 更新 `CHANGELOG.md`。
3. `pixi run check`。
4. `python -m build` + `twine check dist/*`。
5. 在干净 venv 中 `pip install dist/*.whl`，跑 `gseek --version` 和最小 API 冒烟。
6. 打 tag、发布 PyPI/conda。

## 9. 许可证

贡献即同意以 MIT 许可证发布。详见 [LICENSE](LICENSE)。
