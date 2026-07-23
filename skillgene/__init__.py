"""Public package surface for SkillGene shared skill libraries."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import SkillGeneConfig
    from .config_store import ConfigStore
    from .skills import SkillManager

__all__ = [
    "SkillGeneConfig",
    "ConfigStore",
    "SkillManager",
]


_EXPORT_MAP = {
    "SkillGeneConfig": ("skillgene.config", "SkillGeneConfig"),
    "ConfigStore": ("skillgene.config_store", "ConfigStore"),
    "SkillManager": ("skillgene.skills", "SkillManager"),
}


def __getattr__(name: str):
    target = _EXPORT_MAP.get(name)
    if target is None:
        raise AttributeError(f"module 'skillgene' has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
