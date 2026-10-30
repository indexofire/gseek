"""Documentation contracts: links resolve and the API reference is discoverable."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_IGNORED_PARTS = {".pixi", ".scratch", ".git", "dist", "build", "site"}
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    return [path for path in ROOT.rglob("*.md") if not (_IGNORED_PARTS & set(path.parts))]


def test_markdown_relative_links_resolve() -> None:
    broken: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for link in _LINK_RE.findall(text):
            target = link.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / target).resolve().exists():
                broken.append(f"{path.relative_to(ROOT)} -> {link}")
    assert not broken, "broken markdown links:\n" + "\n".join(broken)


def test_api_reference_is_linked_and_complete() -> None:
    api = ROOT / "docs" / "api.md"
    assert api.is_file()
    text = api.read_text(encoding="utf-8")
    for heading in (
        "## 2. Public API",
        "## 3. Result models",
        "## 4. Adapter API",
        "## 5. Execution layer",
        "## 6. Index layer",
        "## 9. Errors",
        "## 10. CLI",
    ):
        assert heading in text, f"missing API section: {heading}"

    chinese_api = ROOT / "docs" / "api.zh.md"
    assert chinese_api.is_file()
    chinese_text = chinese_api.read_text(encoding="utf-8")
    for heading in ("## 2. 公共 API", "## 3. 结果模型", "## 10. CLI"):
        assert heading in chinese_text, f"missing Chinese API section: {heading}"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/api.md" in readme
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "docs/api.md" in agents
