"""プラグイン自動スキャン・レジストリ"""
import pkgutil
import importlib
import logging
from pathlib import Path
from .base import AnalysisPlugin, DependencyError

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


def ensure_dependencies(plugin: AnalysisPlugin, db) -> None:
    """plugin.depends_on の各 producer が produced_output 済みかを検査する。

    未充足（producer 未登録、または produced_output=False）があれば DependencyError を
    送出する。runner（/api/plugins/{name}/run）と専用エンドポイントが execute の前に呼び、
    depends_on を load-bearing にする（宣言だけでなく実行時に強制）。
    """
    unsatisfied: list[str] = []
    for name in getattr(plugin, "depends_on", []):
        dep = get_plugin(name)
        if dep is None or not dep.produced_output(db):
            unsatisfied.append(name)
    if unsatisfied:
        labels = "・".join((get_plugin(n).label if get_plugin(n) else n) for n in unsatisfied)
        raise DependencyError(plugin.name, unsatisfied, f"先に「{labels}」を実行してください")


async def execute_plugin(plugin: AnalysisPlugin, raw: dict, db) -> dict:
    """プラグイン実行の単一 in-process 入口。

    パラメータ契約（CONTEXT.md）に従い raw params を coerce してから依存を強制し、
    execute へ型付き params を渡す:
        coerce_params(plugin.params_schema(), raw) → ensure_dependencies → execute

    HTTP runner（/api/plugins/{name}/run・/api/recommend）とテストが共通で使う。
    例外（ValueError / DependencyError）は握らず送出し、呼び出し側（endpoint）が
    HTTP ステータスへマップする（gap-analysis→404・runner→400 の意図的差を保つため）。
    """
    from .utils import coerce_params
    typed = coerce_params(plugin.params_schema(), raw)
    ensure_dependencies(plugin, db)
    return await plugin.execute(typed, db)
