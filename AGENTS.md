# AGENTS.md

本文件面向在本仓库工作的 AI agent 与人类贡献者。它定义**不可协商的工程约定**。开始任何任务前，先读：

1. `README.md`
2. `docs/api.md`
3. `dev/development-plan.md`
4. `dev/adapter-matrix.md`
5. `dev/compatibility.md`
6. `dev/decisions/` 下的 ADR

## 1. 项目身份

| 项 | 值 |
| --- | --- |
| 项目名 | **gseek** |
| 导入包 | `gseek` |
| PyPI 分发名 | `gseek` |
| CLI | `gseek` |
| Python | `>=3.12` |
| 运行时依赖 | **零**（stdlib only） |
| 原项目名 | `gcore`（因 PyPI 名称被 Gcore 占用，2026-10-22 更名） |

不要在新增代码、文档、schema、环境变量中重新引入 `gcore`。更名细节见 `dev/decisions/0002-project-rename.md`。

## 2. 当前状态

- M0 基座 + M1/M2/M3/M4 适配器完成：结果模型、执行层、索引缓存、manifest、Runner、API、CLI。
- `gseek backends` 现在包含 **blast**、**minimap2**、**kma**、**mummer**、**bwa**、**mash**、**sourmash**、**skani**；真实工具集成测试通过。
- BLAST 支持核酸与蛋白任务；mash 支持 dist/screen；sourmash 支持 search/gather；`gseek replay` 已实现。
- 消费者迁移/parity（gside/gapit/gmlst）与 1.0 发布尚未完成。

## 3. TDD 是强制流程

**没有失败的测试，不写生产代码。**

### 3.1 Red-Green-Refactor

每个行为变更按以下顺序：

1. **Red**：先写一个最小测试，表达期望行为。
   - 新功能：`tests/test_<area>.py::test_<behavior>`
   - Bug 修复：先写复现该 bug 的回归测试
   - 重命名/重构：先写“新契约”测试（例如 `tests/test_project_identity.py`）
2. **确认失败**：运行该测试，确认失败原因是“行为缺失”，不是拼写/导入/夹具错误。
   ```bash
   pixi run test tests/test_area.py::test_behavior
   ```
   如果测试因为错误原因失败，先修测试。
3. **Green**：写**最少**的生产代码让该测试通过。不要顺手实现未测试的功能。
4. **确认通过**：只跑目标测试 + 相关模块测试。
5. **Refactor**：在测试全绿的前提下重构，保持行为不变。
6. **全量门禁**：
   ```bash
   pixi run check
   ```
   必须同时通过 `ruff`、`pyright`、全部 `pytest`。

### 3.2 例外

- 纯文档、注释、README、ADR：不需要生产代码测试，但必须更新相关文档链接。
- 纯配置（CI、pixi、pre-commit）：如果影响构建/测试行为，先加一个验证命令或测试。
- 纯格式化：可以在测试绿的前提下直接执行，随后跑全量门禁。

### 3.3 适配器开发的 TDD 顺序

实现一个适配器时，测试顺序固定：

1. `BackendSpec` 能力/工具/版本约束测试。
2. 命令规划测试（argv、cwd、env、输出路径、超时）。
3. 解析器 golden 测试（真实输出 fixture，含反向、gapped、空结果、CRLF）。
4. 坐标/identity/coverage 归一化测试。
5. 索引 key / artifacts 测试。
6. 集成测试（`@pytest.mark.integration`，无工具时 skip）。
7. 消费者 parity 测试（gmlst/gapit/gside 的现有 golden）。

只写命令规划、不写解析，或只写解析、不写错误路径，都不算完成。

## 4. 架构边界

```text
api → runner → adapters → execution / index / io / model
                  │
                  └→ capabilities / errors
```

必须遵守：

1. **只有 `execution.py` 启动子进程。** 适配器不得直接 `subprocess`。
2. **适配器是纯规划器/解析器。** `plan_index`/`plan_align` 返回命令；`parse_align` 返回记录。
3. **不做业务结论。** 不实现 allele calling、ST 查找、物种规则、抗性判定。
4. **不丢原始记录。** 归并是显式视图（`best_per_query_target`、`deduplicate_intervals` 等）。
5. **坐标统一。** 0-based half-open、正向序列坐标、strand 单独字段。
6. **identity/coverage 带来源。** 保留 `identity_method`、原始 `NM`/`nmatch`/`alen`。
7. **索引内容寻址。** 不用 mtime；key 包含 adapter 版本、语义参数、target 内容哈希、工具版本。
8. **默认确定性。** 无 shell、绝对路径、`LC_ALL=C`、稳定排序。
9. **零运行时依赖。** 新增第三方库必须先有 ADR；可选功能放 extras。
10. **schema 版本化。** `gseek.alignment/1`、`gseek.run/1` 等；破坏性变更升版本。

## 5. MUMmer 版本决策

**目标：MUMmer 4.x，不是 MUMmer 3。**

依据：

- `gmlst` 的 pixi 依赖为 `mummer4 = ">=4.0"`。
- 当前测试环境 conda 包：`mummer4-4.0.1-pl5321h9948957_0`。
- `nucmer --version` 与 `mummer --version` 均输出 `4.0.1`。
- `show-coords -rcl -T` 的 MUMmer4 输出为 13 列（`[TAGS]` 表头 + 两个 tag 列）。

适配器要求：

- `ToolRequirement(binary="nucmer", version_args=("--version",), min_version=(4, 0, 0))`。
- 使用 `nucmer` + `show-coords -rcl -T`，不依赖 MUMmer3 的旧参数/输出。
- MUMmer3 不在 1.0 范围内；如需支持，新增独立适配器与 ADR，不要在同一解析器里猜版本。
- 反向命中用 `S2 > E2` 判断，不能硬编码 `+`。

决策记录：`dev/decisions/0001-mummer4.md`。

## 6. 代码规范

- 行宽 100；`ruff` 规则 `E,W,F,I,UP,B,C4,SIM,RET,RUF`；`ruff format`。
- 类型：`pyright` standard 必须 0 error；新增核心模块尽量使用精确类型，避免 `Any`。
- 公共 API 必须有 docstring 与 `__all__`。
- 单文件建议 ≤ 350 行；超过时按职责拆分。
- 使用 `pathlib.Path`，不要拼字符串路径。
- 异常使用 `gseek.errors` 的类型化错误；不要抛裸 `RuntimeError`。
- 测试使用 `tmp_path`；单元测试不得依赖网络或外部二进制。
- 需要外部工具的测试标记 `@pytest.mark.integration`，无工具时 `pytest.skip`。

## 7. 目录约定

```text
src/gseek/
├── api.py             # 便捷入口
├── adapter.py         # 请求/计划/协议
├── capabilities.py    # 能力声明
├── execution.py       # 唯一子进程层
├── index.py           # 内容寻址索引
├── io.py              # FASTA/FASTQ
├── model/             # 结果与 manifest
├── provenance.py      # hash/recipe/manifest
├── registry.py        # 适配器注册
├── runner.py          # 编排
├── adapters/          # M1–M3 + M4 protein/gather/skani
└── cli.py
tests/
├── test_*.py          # 单元/契约
├── fixtures/          # 真实输出（后续）
├── parity/            # 消费者 parity（后续）
└── integration/       # 外部工具（M1–M4 已加入）
docs/
└── api.md             # 公共 API 参考（用户/适配器作者）
dev/
├── development-plan.md
├── adapter-matrix.md
├── compatibility.md
├── research.md
└── decisions/         # ADR
```

## 8. 验证命令

```bash
pixi install                 # 首次
pixi run test                # 全量单测
pixi run test-cov            # 覆盖率（fail_under=85）
pixi run lint                # ruff check + format --check
pixi run typecheck           # pyright
pixi run check               # lint + typecheck + test
pixi run pre-commit          # 全部 pre-commit hook
pixi run test tests/test_x.py::test_y   # 目标测试
```

提交前必须：

- [ ] 先有失败测试（Red），并确认失败原因。
- [ ] 目标测试通过（Green）。
- [ ] `pixi run check` 全绿。
- [ ] 新行为有测试；bug 有回归测试。
- [ ] 文档/ADR/CHANGELOG 按需更新。
- [ ] 没有提交 `dist/`、`.pixi/`、`.scratch/`、缓存或临时文件。

## 9. 安全与可复现

- 永不使用 `shell=True`；argv 列表逐项传入。
- replay **不执行 manifest 中的任意 argv**；只按 adapter 重新规划。
- manifest 不记录密钥；环境变量只记录 allowlist。
- 所有输出路径必须位于 workspace 或 index staging 内。
- 不静默吞掉解析错误；错误必须带文件/行号/后端。
- 外部工具缺失/版本过旧必须给出可操作提示。

## 10. 提交与发布

- 提交信息用英文、祈使句、短标题；正文说明测试与行为。
- PR 必须包含：行为说明、测试说明、验证命令、文档影响。
- 版本遵循 SemVer；1.0 前 API 可调整，但 schema 变更必须记录。
- 发布前：更新 `CHANGELOG.md`、`src/gseek/version.py`、跑 `pixi run check`、构建 wheel/sdist 并在干净环境安装验证。
