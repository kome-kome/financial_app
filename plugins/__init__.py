"""プラグイン自動スキャン・レジストリ"""
import pkgutil
import importlib
import logging
from pathlib import Path
from .base import AnalysisPlugin

_registry: dict[str, AnalysisPlugin] = {}


def _load():
    here = Path(__file__).parent
    for _, mod_name, _ in pkgutil.iter_modules([str(here)]):
        if mod_name.startswith("_") or mod_name in ("base", "utils"):
            continue
        try:
            mod = importlib.import_module(f"plugins.{mod_name}")
            if hasattr(mod, "plugin") and isinstance(mod.plugin, AnalysisPlugin):
                _registry[mod.plugin.name] = mod.plugin
        except Exception as e:
            logging.warning(f"プラグイン {mod_name} の読み込み失敗: {e}")


_load()


def get_plugin(name: str) -> AnalysisPlugin | None:
    return _registry.get(name)


def list_plugins() -> list[AnalysisPlugin]:
    return list(_registry.values())
