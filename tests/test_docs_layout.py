"""Docs presentation contract: top tabs, page content on the left, TOC on the right."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _ROOT / "docs"


class _DocsLoader(yaml.SafeLoader):
    """SafeLoader that tolerates the pymdownx callable tag used by MkDocs."""


_DocsLoader.add_constructor(
    "tag:yaml.org,2002:python/object/apply:pymdownx.slugs.slugify",
    lambda loader, node: None,
)


def _config() -> dict:
    text = (_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    return yaml.load(text, Loader=_DocsLoader)


def _i18n() -> dict:
    for plugin in _config()["plugins"]:
        if isinstance(plugin, dict) and "i18n" in plugin:
            return plugin["i18n"]
    raise AssertionError("mkdocs.yml has no i18n plugin")


def test_desktop_primary_sidebar_is_hidden() -> None:
    config = _config()
    assert config["extra_css"] == ["stylesheets/extra.css"]

    css = (_DOCS / "stylesheets" / "extra.css").read_text(encoding="utf-8")
    assert "@media screen and (min-width: 76.25em)" in css
    assert ".md-sidebar--primary" in css
    assert "display: none" in css


def test_top_tabs_and_right_hand_toc_are_enabled() -> None:
    config = _config()
    features = config["theme"]["features"]
    assert "navigation.tabs" in features
    assert "navigation.path" in features

    extensions = config["markdown_extensions"]
    assert any(isinstance(extension, dict) and "toc" in extension for extension in extensions)
    assert "md_in_html" in extensions


def _sections() -> dict[str, list]:
    sections: dict[str, list] = {}
    for item in _config()["nav"]:
        if not isinstance(item, dict):
            continue
        title, entries = next(iter(item.items()))
        if isinstance(entries, list):
            sections[title] = entries
    return sections


def _relative_link(landing: Path, target: str) -> str:
    return os.path.relpath(_DOCS / target, landing.parent).replace(os.sep, "/")


def test_sections_have_landing_pages_that_navigate_to_children() -> None:
    sections = _sections()
    assert set(sections) == {"Getting Started", "Usage", "Reference"}

    for title, entries in sections.items():
        landing = entries[0]
        assert isinstance(landing, str), title
        assert landing == f"{title.lower().replace(' ', '-')}/index.md"

        landing_path = _DOCS / landing
        assert landing_path.is_file(), landing
        chinese_path = landing_path.with_name(f"{landing_path.stem}.zh.md")
        assert chinese_path.is_file(), chinese_path

        english_text = landing_path.read_text(encoding="utf-8")
        chinese_text = chinese_path.read_text(encoding="utf-8")
        for entry in entries[1:]:
            child = next(iter(entry.values()))
            assert (_DOCS / child).is_file(), child
            assert f"]({_relative_link(landing_path, child)})" in english_text, (title, child)

            chinese_child = str(Path(child).with_suffix(".zh.md"))
            assert f"]({_relative_link(landing_path, chinese_child)})" in chinese_text, (
                title,
                chinese_child,
            )


def test_chinese_tabs_are_translated() -> None:
    i18n = _i18n()
    assert i18n["reconfigure_material"] is True
    assert i18n["reconfigure_search"] is True

    languages = {language["locale"]: language for language in i18n["languages"]}
    translations = languages["zh"]["nav_translations"]
    expected = {
        "Home": "首页",
        "Getting Started": "快速上手",
        "Usage": "使用",
        "Reference": "参考",
        "Quickstart": "快速开始",
        "Tutorial": "教程",
        "CLI": "CLI 参考",
        "API Reference": "API 参考",
        "Errors": "错误模型",
    }
    assert {label: translations[label] for label in expected} == expected
