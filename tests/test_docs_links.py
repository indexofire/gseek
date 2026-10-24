from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = [_ROOT / "README.md", *_ROOT.joinpath("docs").glob("*.md")]
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _relative_links(path: Path) -> list[str]:
    links: list[str] = []
    for raw in _LINK.findall(path.read_text(encoding="utf-8")):
        target = raw.strip().split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        links.append(target)
    return links


@pytest.mark.parametrize("path", _DOCS, ids=lambda path: path.name)
def test_markdown_relative_links_exist(path: Path) -> None:
    missing = [target for target in _relative_links(path) if not (path.parent / target).exists()]
    assert not missing, f"{path.relative_to(_ROOT)} has missing links: {missing}"
