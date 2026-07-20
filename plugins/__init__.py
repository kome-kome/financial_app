"""プラグイン自動スキャン・レジストリ"""
import asyncio
import pkgutil
import importlib
import logging
import threading
from contextlib import contextmanager
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


# ── プラグイン実行中カウンタ（heartbeat watchdog の保留判定用・Issue #357）────
# execute はワーカースレッドで走るため、カウンタ更新はロックで保護する。
_exec_lock = threading.Lock()
_executing_count = 0


@contextmanager
def _execution_guard():
    """実行中カウンタを try/finally でインクリメント/デクリメントする。"""
    global _executing_count
    with _exec_lock:
        _executing_count += 1
    try:
        yield
    finally:
        with _exec_lock:
            _executing_count -= 1


def any_executing() -> bool:
    """プラグイン実行中か（api._shutdown_due が収集ジョブと同格の保留条件として参照）。"""
    return _executing_count > 0


async def execute_plugin(plugin: AnalysisPlugin, raw: dict, db) -> dict:
    """プラグイン実行の単一 in-process 入口。

    パラメータ契約（CONTEXT.md）に従い raw params を coerce してから依存を強制し、
    execute へ型付き params を渡す:
        coerce_params(plugin.params_schema(), raw) → ensure_dependencies → execute

    execute（同期・CPU-bound）は asyncio.to_thread でワーカースレッドへオフロードし、
    イベントループを塞がない（/heartbeat が止まり watchdog が誤停止する Issue #357 の
    根本対策）。tuning 系 ContextVar（tuning_dry_run / tuning_objective_only /
    tuning_snapshot_cache）は to_thread がコンテキストを複製するため伝播する
    （loop.run_in_executor 直接使用では伝播しない）。db セッションはスレッドへの
    逐次ハンドオフのみ（await 中にループ側から触らない）。

    HTTP runner（/api/plugins/{name}/run・/api/recommend）とテストが共通で使う。
    例外（ValueError / DependencyError）は握らず送出し、呼び出し側（endpoint）が
    HTTP ステータスへマップする（gap-analysis→404・runner→400 の意図的差を保つため）。
    coerce・依存検証はガード前に置き、早期 reject でカウンタが立たないようにする。
    """
    from .utils import coerce_params
    typed = coerce_params(plugin.params_schema(), raw)
    ensure_dependencies(plugin, db)
    with _execution_guard():
        return await asyncio.to_thread(plugin.execute, typed, db)
