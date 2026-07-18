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
"""
from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
_REFRESH_ENV = "SCRIPTS_CACHE_REFRESH"  # set_refresh() 経由でプロセス内共有


def cache_path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.pkl"


def set_refresh(flag: bool) -> None:
    """--refresh-cache 指定を全 cached() 呼び出しへ伝える（既存キャッシュを無視して再取得）。"""
    os.environ[_REFRESH_ENV] = "1" if flag else "0"


def _refresh_requested() -> bool:
    return os.environ.get(_REFRESH_ENV) == "1"


def cached(key: str, producer: Callable[[], T]) -> T:
    """key のキャッシュがあれば読み、無ければ producer() を実行して保存し返す。

    --refresh-cache（set_refresh(True)）時は既存キャッシュを無視して producer() を再実行する。
    書き込みは tmp→replace のアトミック置換で、中断による破損キャッシュを残さない。
    """
    path = cache_path(key)
    if not _refresh_requested() and path.exists():
        with path.open("rb") as f:
            return pickle.load(f)
    value = producer()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pkl.tmp")
    with tmp.open("wb") as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    return value
