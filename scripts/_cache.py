"""検証スクリプト用ローカルキャッシュ（Issue #355・Egress 超過の恒久対策）。

`scripts/` 配下の検証スクリプトは本番 Supabase 直結（DATABASE_URL）で、フルラン反復の
たびに週次株価 95 万行や開示全件を再 pull していた。同一データの再取得が Egress を食い、
2026-07 は 61.2/5 GB（1,224%）まで超過して organization 全体が restricted になった。

本モジュールは「初回だけ本番から pull → ローカル pickle 保存 → 以降はキャッシュ読み」を
汎用ヘルパー化し、フルラン 2 回目以降の Egress をほぼ 0 にする。検証専用でありキャッシュは
`scripts/.cache/`（gitignore 配下）に置く。

キャッシュキーはデータ形状で決める（テーブル・列が同じロードは同一キーを共有）。同じ
`weekly_prices_close` を複数スクリプトが使えば、片方が作ったキャッシュを他方も再利用できる。

無効化は明示リフレッシュのみ（`set_refresh(True)` / CLI の `--refresh-cache`）。検証用途では
最新性より再現性・低 Egress を優先し、TTL による自動失効は設けない。データを取り直したい
ときは `--refresh-cache` を付けて実行する。

**HIT/MISS は必ず標準エラーへ出す**（Issue #478）。2026-08 に Egress が2回目の枠超過
（7.312/5GB＝146%）を起こしたとき、本モジュール導入後にもかかわらず「キャッシュが効かない
経路が残っている」のか「キーが実質毎回ミスしている」のかを事後に切り分けられなかった。
黙ってミスしても気づけない構造は #438（Yahoo の静かな配信停止）と同型なので、1 実行ごとに
ヒット率が目に入る状態にしておく。
"""
from __future__ import annotations

import atexit
import os
import pickle
import sys
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
_REFRESH_ENV = "SCRIPTS_CACHE_REFRESH"  # set_refresh() 経由でプロセス内共有

# プロセス内の累計。atexit で 1 行に畳んで出す（「実質毎回ミス」を1行で見抜くための指標）。
_stats = {"hits": 0, "misses": 0, "produced_bytes": 0}

# 記号は ASCII だけを使う。cp932 の Windows コンソールへリダイレクトすると非 ASCII 記号は
# UnicodeEncodeError で出力済みの内容ごとクラッシュする（既知の罠）。
_LOG_PREFIX = "[cache]"


def cache_path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.pkl"


def set_refresh(flag: bool) -> None:
    """--refresh-cache 指定を全 cached() 呼び出しへ伝える（既存キャッシュを無視して再取得）。"""
    os.environ[_REFRESH_ENV] = "1" if flag else "0"


def _refresh_requested() -> bool:
    return os.environ.get(_REFRESH_ENV) == "1"


def _log(message: str) -> None:
    """標準エラーへ 1 行出す（標準出力は各スクリプトの成果物なので混ぜない）。"""
    print(f"{_LOG_PREFIX} {message}", file=sys.stderr, flush=True)


def _mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 * 1024):.1f}MB"


def _emit_summary() -> None:
    """プロセス終了時に累計を 1 行で出す（cached() を一度も使わなければ黙る）。

    `produced` は pickle のバイト数であって Egress そのものではない（pickle はバイナリ、
    psycopg2 はテキストプロトコル）。実測の正本はサーバ側 `sum(octet_length(列::text))`
    ＝ #446 の測り方で、ここの数字は「どれだけ本番から引き直したか」の相対的な目安。
    """
    if not (_stats["hits"] or _stats["misses"]):
        return
    _log(f"summary hits={_stats['hits']} misses={_stats['misses']} "
         f"produced={_mb(_stats['produced_bytes'])} dir={_CACHE_DIR}")


atexit.register(_emit_summary)


def cached(key: str, producer: Callable[[], T]) -> T:
    """key のキャッシュがあれば読み、無ければ producer() を実行して保存し返す。

    --refresh-cache（set_refresh(True)）時は既存キャッシュを無視して producer() を再実行する。
    書き込みは tmp→replace のアトミック置換で、中断による破損キャッシュを残さない。

    HIT / MISS / REFRESH を標準エラーへ 1 行ずつ出す（Issue #478）。MISS と REFRESH は
    本番 DB を引いた＝Egress を使ったことを意味する。
    """
    path = cache_path(key)
    refresh = _refresh_requested()
    if not refresh and path.exists():
        with path.open("rb") as f:
            value = pickle.load(f)
        _stats["hits"] += 1
        _log(f"HIT     {key} ({_mb(path.stat().st_size)})")
        return value

    _log(f"{'REFRESH' if refresh else 'MISS   '} {key} -> pulling from DB")
    value = producer()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pkl.tmp")
    with tmp.open("wb") as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    size = path.stat().st_size
    _stats["misses"] += 1
    _stats["produced_bytes"] += size
    _log(f"{'REFRESH' if refresh else 'MISS   '} {key} saved ({_mb(size)})")
    return value
