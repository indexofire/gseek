# ADR-0002: 项目更名为 gseek

- **状态**：Accepted
- **日期**：2026-10-22
- **决策者**：gseek 维护者
- **相关**：`../../README.md`、`../../AGENTS.md`、`../../pyproject.toml`、`../../tests/test_project_identity.py`

## Context

项目最初命名为 `gcore`。在准备发布时发现：

- PyPI 的 `gcore` 已被 **Gcore Python API library**（`gcore.com`，Apache-2.0）占用；
  `https://pypi.org/pypi/gcore/json` 返回 200。
- `pip install gcore` 会安装 Gcore 的 REST API 客户端，而不是本项目的 DNA 比对库，
  对用户是危险的误导。
- 用户明确要求将项目名称改为 **gseek**。
- 2026-10-22 检查 `https://pypi.org/pypi/gseek/json` 返回 404，名称可用。

## Decision

项目全面更名为 **gseek**，不保留 `gcore` 兼容 shim：

| 表面 | 旧 | 新 |
| --- | --- | --- |
| PyPI 分发名 | `gcore-bio`（本地曾用，未发布） | `gseek` |
| 导入包 | `gcore` | `gseek` |
| CLI / console script | `gcore` | `gseek` |
| 环境变量 | `GCORE_*` | `GSEEK_*` |
| schema | `gcore.*/1` | `gseek.*/1` |
| 错误 envelope | `gcore.error/1` | `gseek.error/1` |
| 插件入口组 | `gcore.adapters` | `gseek.adapters` |
| 异常基类 | `GcoreError` | `GseekError` |

仓库目录当前仍可能是 `gcore`（历史路径），但包名、文档、schema、CLI 一律以 `gseek` 为准。

## Consequences

正面：

- 避免安装到错误的 PyPI 包。
- 名称更短，且与“genome seek”语义一致。
- 统一了包名/CLI/schema/环境变量，减少认知负担。

负面/代价：

- 0.1.0 尚未发布，因此没有外部用户迁移成本；如果已有内部脚本，需要同步改导入名、
  CLI 和环境变量。
- 本地 editable install 需要重新安装（旧 `gcore-bio` dist-info 可能残留）。
- 旧文档/分支中的 `gcore` 引用会造成混淆；需要一次性清理。

## Migration checklist

- [x] `src/gcore` → `src/gseek`
- [x] `../../pyproject.toml` 的 `name`、scripts、entry-points、URLs、coverage source
- [x] `pixi.toml` workspace name
- [x] 所有 `import gcore` → `import gseek`
- [x] `GCORE_*` → `GSEEK_*`
- [x] `gcore.*` schema → `gseek.*`
- [x] README、AGENTS、CONTRIBUTING、docs、CHANGELOG
- [x] `../../tests/test_project_identity.py` 先红后绿（TDD）
- [ ] 重新安装 editable 包并验证 `gseek --version`
- [ ] 构建 wheel/sdist，确认 wheel 内只有 `gseek/`

## Alternatives considered

1. **分发名 `gcore-bio`、导入名 `gcore`**：可以避开 PyPI 冲突，但 CLI/schema/环境变量
   仍叫 gcore，容易与 Gcore 混淆；用户明确要求 gseek。拒绝。
2. **保留 `gcore` 兼容 shim**：会再次占用/混淆包名，且 0.1.0 尚未发布，没有兼容义务。
   拒绝。
3. **改名为 `gcore2`/`pygcore`**：语义不清，且没有解决品牌混淆。拒绝。
