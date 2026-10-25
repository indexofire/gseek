"""Shared legacy/gseek/shadow switch for gseek consumers.

Consumers keep their own legacy implementation (gside marker rules, gapit hit
semantics, gmlst allele calls); this module only owns the environment switch,
shadow comparison, and fallback policy.

Typical use::

    engine = Engine.from_env("GAPIT_ENGINE")
    return engine.run(
        legacy=run_legacy,
        gseek=run_gseek,
        compare=compare_results,
    )

Shadow mode always returns the legacy result. A gseek failure or comparison
exception is logged and the legacy result is returned; in ``gseek`` mode a
failure propagates normally.
"""

from __future__ import annotations

import enum
import logging
import operator
import os
from collections.abc import Callable, Mapping
from typing import TypeVar

from gseek.errors import ConfigError

T = TypeVar("T")

_LOGGER = logging.getLogger("gseek.compat")


class EngineMode(enum.Enum):
    """Supported engine-selection modes."""

    LEGACY = "legacy"
    GSEEK = "gseek"
    SHADOW = "shadow"


_MODES = {mode.value: mode for mode in EngineMode}


class Engine:
    """Resolve one environment variable and dispatch legacy/gseek/shadow."""

    def __init__(
        self,
        mode: EngineMode | str,
        *,
        env_var: str = "GSEEK_ENGINE",
        logger: logging.Logger | None = None,
    ) -> None:
        if isinstance(mode, EngineMode):
            self.mode = mode
        else:
            raw = mode.strip().lower()
            try:
                self.mode = _MODES[raw]
            except KeyError as exc:
                raise ConfigError(
                    f"unknown engine mode {mode!r}; expected one of {sorted(_MODES)}",
                    context={"mode": str(mode)},
                ) from exc
        self.env_var = env_var
        self.logger = logger or _LOGGER

    @classmethod
    def from_env(
        cls,
        env_var: str,
        *,
        default: EngineMode | str = EngineMode.LEGACY,
        env: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
    ) -> Engine:
        """Read ``env_var`` (empty/unset uses ``default``) and validate it."""
        values = os.environ if env is None else env
        raw = values.get(env_var, "").strip().lower()
        return cls(raw or default, env_var=env_var, logger=logger)

    def run(
        self,
        legacy: Callable[[], T],
        gseek: Callable[[], T],
        *,
        compare: Callable[[T, T], bool] | None = None,
    ) -> T:
        """Run the selected path; shadow runs both and returns legacy.

        ``compare`` receives ``(legacy_result, gseek_result)``. The default is
        value equality. Iterators should be materialized by the caller before
        shadow comparison.
        """
        if self.mode is EngineMode.LEGACY:
            return legacy()
        if self.mode is EngineMode.GSEEK:
            return gseek()

        legacy_result = legacy()
        try:
            gseek_result = gseek()
        except Exception as exc:  # shadow is a verification/fallback mode
            self.logger.warning(
                "%s=shadow: gseek path failed, returning legacy: %s",
                self.env_var,
                exc,
            )
            return legacy_result
        comparator = compare or operator.eq
        try:
            same = comparator(legacy_result, gseek_result)
        except Exception as exc:
            self.logger.warning(
                "%s=shadow: comparison failed, returning legacy: %s",
                self.env_var,
                exc,
            )
            return legacy_result
        if not same:
            self.logger.warning(
                "%s=shadow: legacy and gseek results differ",
                self.env_var,
            )
        return legacy_result


__all__ = ["Engine", "EngineMode"]
