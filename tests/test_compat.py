"""Tests for the shared legacy/gseek/shadow switch."""

from __future__ import annotations

import logging

import pytest

from gseek.compat import Engine, EngineMode
from gseek.errors import ConfigError


def test_from_env_defaults_to_legacy() -> None:
    engine = Engine.from_env("GAPIT_ENGINE", env={})
    assert engine.mode is EngineMode.LEGACY


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        ("gseek", EngineMode.GSEEK),
        ("shadow", EngineMode.SHADOW),
        ("  LEGACY  ", EngineMode.LEGACY),
    ],
)
def test_from_env_values(raw: str, mode: EngineMode) -> None:
    engine = Engine.from_env("GSIDE_ENGINE", env={"GSIDE_ENGINE": raw})
    assert engine.mode is mode


def test_from_env_rejects_unknown_mode() -> None:
    with pytest.raises(ConfigError, match="unknown engine mode"):
        Engine.from_env("GAPIT_ENGINE", env={"GAPIT_ENGINE": "automatic"})


def test_legacy_mode_does_not_call_gseek() -> None:
    calls: list[str] = []
    engine = Engine(EngineMode.LEGACY)
    assert engine.run(lambda: calls.append("legacy") or 1, lambda: calls.append("gseek") or 2) == 1
    assert calls == ["legacy"]


def test_gseek_mode_does_not_call_legacy() -> None:
    calls: list[str] = []
    engine = Engine(EngineMode.GSEEK)
    assert engine.run(lambda: calls.append("legacy") or 1, lambda: calls.append("gseek") or 2) == 2
    assert calls == ["gseek"]


def test_shadow_returns_legacy_and_logs_equal(caplog: pytest.LogCaptureFixture) -> None:
    engine = Engine(EngineMode.SHADOW)
    with caplog.at_level(logging.WARNING, logger="gseek.compat"):
        assert engine.run(lambda: {"x": 1}, lambda: {"x": 1}) == {"x": 1}
    assert caplog.text == ""


def test_shadow_logs_difference_and_returns_legacy(caplog: pytest.LogCaptureFixture) -> None:
    engine = Engine(EngineMode.SHADOW, env_var="GSIDE_ENGINE")
    with caplog.at_level(logging.WARNING, logger="gseek.compat"):
        result = engine.run(lambda: {"x": 1}, lambda: {"x": 2})
    assert result == {"x": 1}
    assert "legacy and gseek results differ" in caplog.text


def test_shadow_falls_back_when_gseek_fails(caplog: pytest.LogCaptureFixture) -> None:
    engine = Engine(EngineMode.SHADOW)

    def fail() -> int:
        raise RuntimeError("gseek exploded")

    with caplog.at_level(logging.WARNING, logger="gseek.compat"):
        assert engine.run(lambda: 7, fail) == 7
    assert "gseek path failed" in caplog.text


def test_shadow_uses_custom_comparator() -> None:
    engine = Engine(EngineMode.SHADOW)
    seen: list[tuple[int, int]] = []

    def compare(left: int, right: int) -> bool:
        seen.append((left, right))
        return True

    assert engine.run(lambda: 1, lambda: 2, compare=compare) == 1
    assert seen == [(1, 2)]


def test_gseek_mode_propagates_failure() -> None:
    engine = Engine(EngineMode.GSEEK)

    def fail() -> int:
        raise RuntimeError("gseek exploded")

    with pytest.raises(RuntimeError, match="gseek exploded"):
        engine.run(lambda: 1, fail)
