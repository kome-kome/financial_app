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

## 取消（#849）

スレッドは外から止められないので**協調型**にする。画面から回した実行には HTTP runner が
`Cancellation` を渡し、`emit` が送った直後に取消の有無を見て `AnalysisCancelled` を投げる
（止まれるのは間引き後に sink まで届いた点だけ）。

**保存を始めたら取消を受け付けない。** sector_ols は業種ごとに commit するので、途中で止めると
`regression_results` に新旧が混ざる。保存する処理は `persisting()` で包み、入る直前を最後の
取消点にする。受付と保存の開始は同じロックで判定する＝「受け付けた直後に保存が始まって、
保存の後で止まる」すり抜けが起きない。バッチ経路（`Cancellation` 未設定）では何もしない。
"""
import contextlib
import contextvars
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

# sink は (step, current, total)。current/total が 0 のときは件数を持たない
# ステップ通知（「キャッシュから復元」等）を意味する。
ProgressSink = Callable[[str, int, int], None]


class AnalysisCancelled(Exception):
    """画面の取消で、保存を始める前に止めた（#849）。保存済みの結果は前回のまま。

    `except Exception` で握る箇所（`model_comparison` のモデル単位の except 等）は、
    取消だけ先に送出し直すこと——握ると止まらずに次へ進む。
    """


@dataclass
class Cancellation:
    """1回の画面実行の取消状態（#849）。HTTP runner が作り、`progress_sink` で渡す。

    `request()` はイベントループ側、`emit` / `persisting` は計算のスレッド側から触るので、
    判定はロックの中で行う。
    """
    requested: bool = False
    saving: int = 0                     # persisting() の入れ子の深さ。>0 の間は受け付けない
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def request(self) -> bool:
        """取消を求める。保存を始めていれば受け付けず False を返す。"""
        with self._lock:
            if self.saving:
                return False
            self.requested = True
            return True

    def raise_if_requested(self) -> None:
        with self._lock:
            if self.requested and not self.saving:
                raise AnalysisCancelled("取消しました")

    def _enter_saving(self) -> None:
        with self._lock:
            if self.requested and not self.saving:
                raise AnalysisCancelled("取消しました")
            self.saving += 1

    def _leave_saving(self) -> None:
        with self._lock:
            self.saving -= 1

# 全社ループ（約4,400社）を1件ずつ流すと JobState の _LOG_MAX=500 を溢れさせ、
# 画面へ届く前に前段のステップ名が押し出される。ループ側は every= で間引く。
EVERY_COMPANIES = 100
EVERY_SECTORS = 1
EVERY_CHUNKS = 1

_sink: contextvars.ContextVar[Optional[ProgressSink]] = contextvars.ContextVar(
    "finapp_progress_sink", default=None)
_cancel: contextvars.ContextVar[Optional[Cancellation]] = contextvars.ContextVar(
    "finapp_progress_cancel", default=None)

# persisting() が入るときに流すステップ名（画面のログに「ここから止まらない」と出す）
PERSIST_STEP = "結果を保存（ここからは取消できません）"


@contextlib.contextmanager
def progress_sink(fn: ProgressSink, cancel: Optional[Cancellation] = None) -> Iterator[None]:
    """この文脈で実行される emit を fn へ流す。HTTP runner だけが使う。

    `cancel` を渡すと、emit と persisting() がその取消状態を見る（#849）。
    ContextVar なので入れ子・並行実行しても互いを踏まない（token で必ず戻す）。
    """
    token = _sink.set(fn)
    cancel_token = _cancel.set(cancel)
    try:
        yield
    finally:
        _cancel.reset(cancel_token)
        _sink.reset(token)


@contextlib.contextmanager
def persisting(step: str = PERSIST_STEP) -> Iterator[None]:
    """保存の区間（#849）。**入る直前が最後の取消点**で、中では取消を受け付けない。

    保存する処理（`replace_macro_*_scores`・業種ごとの commit）を必ずこれで包む。包み忘れても
    失敗としては現れない（保存の途中で止まり、新旧が混ざる）ので、`writes` を持つ heavy は
    `tests/test_plugin_progress.py` が `progress.persisting(` の実在を照合する。
    取消状態が無い経路（バッチ・`/api/recommend`）では何もしない。
    """
    cancel = _cancel.get()
    if cancel is not None:
        cancel._enter_saving()
    try:
        emit(step)
        yield
    finally:
        if cancel is not None:
            cancel._leave_saving()


def active() -> bool:
    """sink が設定されているか（進捗文字列の組み立て自体を避けたい呼び出し側向け）。"""
    return _sink.get() is not None


def emit(step: str, current: int = 0, total: int = 0, *, every: int = 1) -> None:
    """進捗を1件流す。sink 未設定なら何もしない。

    `every` は間引き幅。**最初（current=0）と最後（current=total）は必ず流す**——
    間引きで終端を落とすと「4300/4400 のまま完了」に見え、止まったのか終わったのかが
    区別できなくなる。

    取消（#849）は送った**後**に見る＝止まった場所が画面のログの最後の行に残る。
    """
    sink = _sink.get()
    if sink is None:
        return
    if every > 1 and current and current != total and current % every:
        return
    sink(step, current, total)
    cancel = _cancel.get()
    if cancel is not None:
        cancel.raise_if_requested()


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
