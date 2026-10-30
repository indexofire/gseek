"""Bilingual docs must stay structurally in sync and use rendered diagrams."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _ROOT / "docs"
_ARROWS = re.compile(r"[→▼▲│└┘├┤]")
_HEADING = re.compile(r"^(#{1,6})\s")
_TABLE_RULE = re.compile(r"^\|\s*:?-{3,}")


def _pairs() -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for page in sorted(_DOCS.rglob("*.md")):
        if page.name.endswith(".zh.md"):
            continue
        pairs.append((page, page.with_name(f"{page.stem}.zh.md")))
    return pairs


def _without_fences(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    outside: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            outside.append(line)
    return outside


def _heading_levels(path: Path) -> list[int]:
    levels: list[int] = []
    for line in _without_fences(path):
        match = _HEADING.match(line)
        if match:
            levels.append(len(match.group(1)))
    return levels


def _code_blocks(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    blocks: list[tuple[str, tuple[str, ...]]] = []
    in_fence = False
    language = ""
    body: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            if not in_fence:
                in_fence = True
                language = line[3:].strip()
                body = []
            else:
                blocks.append((language, tuple(body)))
                in_fence = False
            continue
        if in_fence:
            body.append(line)
    return blocks


def _code_languages(path: Path) -> list[str]:
    return [language for language, _ in _code_blocks(path)]


def _table_count(path: Path) -> int:
    return sum(1 for line in _without_fences(path) if _TABLE_RULE.match(line))


def _image_count(path: Path) -> int:
    return len(re.findall(r"!\[[^\]]*\]\([^)]+\)", path.read_text(encoding="utf-8")))


def _bullet_count(path: Path) -> int:
    return sum(1 for line in _without_fences(path) if re.match(r"^\s*(?:[-*]|\d+\.)\s+", line))


def test_every_english_page_has_a_chinese_pair() -> None:
    missing = [str(page.relative_to(_ROOT)) for page, chinese in _pairs() if not chinese.is_file()]
    assert not missing, f"missing Chinese translations: {missing}"


def test_bilingual_pages_share_the_same_heading_structure() -> None:
    mismatched = [
        (str(page.relative_to(_ROOT)), _heading_levels(page), _heading_levels(chinese))
        for page, chinese in _pairs()
        if chinese.is_file() and _heading_levels(page) != _heading_levels(chinese)
    ]
    assert not mismatched, "heading structure differs:\n" + "\n".join(map(str, mismatched))


def test_bilingual_pages_share_code_block_structure_and_tables() -> None:
    mismatched = []
    for page, chinese in _pairs():
        if not chinese.is_file():
            continue
        if _code_languages(page) != _code_languages(chinese):
            mismatched.append(f"{page.relative_to(_ROOT)}: code blocks differ")
        if _table_count(page) != _table_count(chinese):
            mismatched.append(f"{page.relative_to(_ROOT)}: table count differs")
        if _image_count(page) != _image_count(chinese):
            mismatched.append(f"{page.relative_to(_ROOT)}: image count differs")
        if _bullet_count(page) != _bullet_count(chinese):
            mismatched.append(f"{page.relative_to(_ROOT)}: bullet count differs")
    assert not mismatched, "\n".join(mismatched)


def test_text_diagrams_are_rendered_as_images() -> None:
    offenders: list[str] = []
    for page in _DOCS.rglob("*.md"):
        for language, body in _code_blocks(page):
            if language == "text" and _ARROWS.search("\n".join(body)):
                offenders.append(str(page.relative_to(_ROOT)))
    assert not offenders, f"ASCII diagrams must be rendered images: {sorted(set(offenders))}"


def test_doc_diagram_assets_exist() -> None:
    diagrams = _DOCS / "assets" / "diagrams"
    for name in (
        "architecture",
        "architecture.zh",
        "run-pipeline",
        "run-pipeline.zh",
        "request-flow",
        "request-flow.zh",
    ):
        assert (diagrams / f"{name}.png").is_file(), name


def test_homepages_use_the_new_hero_and_architecture_diagram() -> None:
    for name in ("index.md", "index.zh.md"):
        text = (_DOCS / name).read_text(encoding="utf-8")
        assert 'class="gseek-hero"' in text, name
        assert "assets/diagrams/architecture" in text, name
