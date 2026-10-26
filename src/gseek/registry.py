"""Adapter registry: built-ins, aliases, capability queries, and plugins."""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import threading
from collections.abc import Iterable, Iterator
from typing import Any

from gseek.adapter import BaseAdapter
from gseek.capabilities import BackendSpec, supports
from gseek.errors import ConfigError
from gseek.types import InputKind, Molecule, OutputKind

logger = logging.getLogger(__name__)

# Concrete adapter modules are appended here as milestones land. Keeping the
# list explicit (rather than importing gseek.adapters.* eagerly) keeps import
# time and optional dependencies under control.
BUILTIN_MODULES: tuple[str, ...] = (
    "gseek.adapters.blast",
    "gseek.adapters.minimap2",
    "gseek.adapters.kma",
    "gseek.adapters.mummer",
    "gseek.adapters.bwa",
    "gseek.adapters.mash",
    "gseek.adapters.sourmash",
    "gseek.adapters.skani",
    "gseek.adapters.sylph",
)

_REGISTRY: dict[str, type[BaseAdapter]] = {}
_REGISTRY_LOCK = threading.RLock()
_LOADED = False


def register_adapter(
    adapter: type[BaseAdapter] | BaseAdapter, *, replace: bool = False
) -> type[BaseAdapter]:
    """Register an adapter class (or instance) under its name and aliases."""
    adapter_class = adapter if isinstance(adapter, type) else type(adapter)
    spec = getattr(adapter_class, "spec", None)
    if not isinstance(spec, BackendSpec):
        raise ConfigError(
            f"adapter {adapter_class.__name__} must declare a BackendSpec in `spec`",
        )

    with _REGISTRY_LOCK:
        for name in spec.names:
            existing = _REGISTRY.get(name)
            if existing is not None and existing is not adapter_class and not replace:
                raise ConfigError(
                    f"adapter name {name!r} is already registered by {existing.__name__}",
                    context={"name": name},
                )
            _REGISTRY[name] = adapter_class
    return adapter_class


def unregister_adapter(name: str) -> None:
    """Remove every alias pointing at the adapter registered as *name*."""
    with _REGISTRY_LOCK:
        adapter_class = _REGISTRY.get(name)
        if adapter_class is None:
            raise ConfigError(f"no adapter registered as {name!r}")
        for alias, registered in list(_REGISTRY.items()):
            if registered is adapter_class:
                del _REGISTRY[alias]


def _load_once() -> None:
    global _LOADED
    if _LOADED:
        return
    with _REGISTRY_LOCK:
        if _LOADED:
            return
        for module_name in BUILTIN_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:  # pragma: no cover - only during development
                logger.debug("built-in adapter module %s is not available: %s", module_name, exc)
                continue
            adapter = getattr(module, "ADAPTER", None)
            if adapter is None:
                logger.warning("built-in adapter module %s has no ADAPTER", module_name)
                continue
            register_adapter(adapter)
        _load_entry_points()
        _LOADED = True


def _load_entry_points() -> None:
    try:
        entry_points = importlib.metadata.entry_points(group="gseek.adapters")
    except Exception as exc:  # pragma: no cover - metadata backend differences
        logger.debug("could not enumerate gseek.adapters entry points: %s", exc)
        return
    for entry_point in entry_points:
        try:
            loaded = entry_point.load()
            adapter = loaded if isinstance(loaded, type) else type(loaded)
            register_adapter(adapter)
        except Exception as exc:  # pragma: no cover - third-party plugin failure
            logger.warning("could not load gseek adapter plugin %s: %s", entry_point.name, exc)


def get_backend(name: str, **kwargs: Any) -> BaseAdapter:
    """Instantiate a registered adapter by name or alias."""
    _load_once()
    with _REGISTRY_LOCK:
        adapter_class = _REGISTRY.get(name)
    if adapter_class is None:
        available = ", ".join(sorted(set(_REGISTRY))) or "<none>"
        raise ConfigError(
            f"unknown backend {name!r}; available: {available}",
            context={"backend": name},
        )
    try:
        return adapter_class(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"could not construct backend {name!r}: {exc}") from exc


def get_adapter(name: str, **kwargs: Any) -> BaseAdapter:
    """Alias for :func:`get_backend`."""
    return get_backend(name, **kwargs)


def backend_specs() -> tuple[BackendSpec, ...]:
    """Return one spec per adapter, sorted by name."""
    _load_once()
    with _REGISTRY_LOCK:
        unique: dict[type[BaseAdapter], BackendSpec] = {}
        for adapter_class in _REGISTRY.values():
            unique[adapter_class] = adapter_class.spec
    return tuple(sorted(unique.values(), key=lambda spec: spec.name))


def registered_names() -> tuple[str, ...]:
    _load_once()
    with _REGISTRY_LOCK:
        return tuple(sorted(_REGISTRY))


def iter_adapters() -> Iterator[type[BaseAdapter]]:
    _load_once()
    with _REGISTRY_LOCK:
        unique = set(_REGISTRY.values())
    yield from sorted(unique, key=lambda cls: cls.spec.name)


def find_backends(
    *,
    input: InputKind | str | None = None,
    output: OutputKind | str | None = None,
    paired: bool | None = None,
    molecule: Molecule | str | None = None,
) -> tuple[BackendSpec, ...]:
    """Return specs that satisfy every supplied capability constraint."""
    input_kind = InputKind(input) if isinstance(input, str) else input
    output_kind = OutputKind(output) if isinstance(output, str) else output
    molecule_value = Molecule(molecule) if isinstance(molecule, str) else molecule
    return tuple(
        spec
        for spec in backend_specs()
        if supports(
            spec,
            input_kind=input_kind,
            output_kind=output_kind,
            paired=paired,
            molecule=molecule_value,
        )
    )


def _reset_for_tests(*, load_builtins: bool = False) -> None:
    """Clear the registry for test isolation.

    By default built-ins are *not* loaded after the reset, so tests that
    register a fake adapter see only that adapter. Pass ``load_builtins=True``
    to exercise the real built-in registry.
    """
    global _LOADED
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        _LOADED = not load_builtins


def _register_many(adapters: Iterable[type[BaseAdapter]]) -> None:
    for adapter in adapters:
        register_adapter(adapter)


__all__ = [
    "BUILTIN_MODULES",
    "backend_specs",
    "find_backends",
    "get_adapter",
    "get_backend",
    "iter_adapters",
    "register_adapter",
    "registered_names",
    "unregister_adapter",
]
