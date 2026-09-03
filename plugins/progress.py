"""heavy プラグイン実行の進捗を画面へ流す唯一の経路（Issue #545・#423 子8）。

`heavy=True` のプラグインは分〜十数分かかる。進捗が無いと画面は完了まで沈黙し、
**「走っている」と「死んだ」を利用者が区別できない**——バッチ側で `capture_output=True` が
「順調に長い」と「死んだ」を潰していた問題（#504 / PR #511）と同じ構造が、画面側にだけ
残っていた。

**heartbeat は生存を示すが進行を示さない**（#504 で実測）。経過時間だけを流すと固まって
いても健全に見えるため、ここを通す通知は必ず「現在のステップ名」を持ち、件数が数えられる
場面では「処理済み/全体」も持つ。

## 仕組み

sink は ContextVar で渡す。`execute` のシグネチャは全プラグイン共通の `(params, db)` に
固定されており（パラメータ契約）引数は増やせない。加えて `execute_plugin` は execute を
`asyncio.to_thread` でワーカースレッドへ逃がす（#357）。**ContextVar なら to_thread が
コンテキストを複製するため素通しで伝播する**（`tuning_dry_run` / `shared_snapshot_cache`
と同じ手）＝ `execute_plugin` を一切変えずに済む。

**sink 未設定なら emit は完全な no-op**。これが月次バッチ（`scripts/run_monthly*.py` の
tune / macro_beta）や `/api/recommend`・`/api/gap-analysis` 経路の非破壊を構造的に担保する
（「画面から実行したときだけ進捗が生える」）。
"""
import contextlib
import contextvars
from typing import Callable, Iterator, Optional

# sink は (step, current, total)。current/total が 0 のときは件数を持たない
# ステップ通知（「キャッシュから復元」等）を意味する。
ProgressSink = Callable[[str, int, int], None]

# 全社ループ（約4,400社）を1件ずつ流すと JobState の _LOG_MAX=500 を溢れさせ、
# 画面へ届く前に前段のステップ名が押し出される。ループ側は every= で間引く。
EVERY_COMPANIES = 100
EVERY_SECTORS = 1
EVERY_CHUNKS = 1

_sink: contextvars.ContextVar[Optional[ProgressSink]] = contextvars.ContextVar(
    "finapp_progress_sink", default=None)


@contextlib.contextmanager
def progress_sink(fn: ProgressSink) -> Iterator[None]:
    """この文脈で実行される emit を fn へ流す。HTTP runner だけが使う。

    ContextVar なので入れ子・並行実行しても互いを踏まない（token で必ず戻す）。
    """
    token = _sink.set(fn)
    try:
        yield
    finally:
        _sink.reset(token)


def active() -> bool:
    """sink が設定されているか（進捗文字列の組み立て自体を避けたい呼び出し側向け）。"""
    return _sink.get() is not None


def emit(step: str, current: int = 0, total: int = 0, *, every: int = 1) -> None:
    """進捗を1件流す。sink 未設定なら何もしない。

    `every` は間引き幅。**最初（current=0）と最後（current=total）は必ず流す**——
    間引きで終端を落とすと「4300/4400 のまま完了」に見え、止まったのか終わったのかが
    区別できなくなる。
    """
    sink = _sink.get()
    if sink is None:
        return
    if every > 1 and current and current != total and current % every:
        return
    sink(step, current, total)


# ── heavy プラグインの進捗カバレッジ表（#545）───────────────────────────────
# **「heavy を足したが進捗が無い」は実行時に失敗として現れない**（画面が沈黙するだけで
# 例外もログも出ない）ので、ADR-0031 の `HEAVY_AUTOMATION` / #515 の `WATCHED` と同じく
# 表で縛り、`tests/test_plugin_progress.py` が実体と照合する。
#
#   "common"          : macro_snapshots の共通骨格（週次ロード / マクロ前読み /
#                       スナップショット構築）を通るため自動的に進捗を持つ
#   "own"             : 自前で progress.emit を呼ぶ
#   "exempt: <理由>"  : 進捗を持たない（理由必須・空理由は CI が落とす）
PROGRESS_COVERAGE: dict[str, str] = {
    "macro_risk_return": "common",
    "macro_gbdt":        "common",
    "macro_gbdt_rank":   "common",
    "macro_enet":        "common",
    "macro_ensemble":    "common",
    "macro_dlm":         "common",
    "sector_ols":        "own",
    # `AnalysisPlugin` ではなく `routers/analysis.py::SPECIAL_ANALYSES` の特例エントリ（#593）。
    # 内部で heavy 3本を順に回すので**実行が最も長いのがここ**。共通骨格の進捗はそのまま
    # 流れるが「3本のどれを回しているか」は出ないため、`model_comparison.run_comparison`
    # がモデル単位で自前に emit する。
    "model_comparison":  "own",
}
