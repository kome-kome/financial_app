"""クライアント側 Egress 台帳とサーキットブレーカ（Issue #478）。

## なぜ必要か

Supabase 無料枠の Egress は 5GB/月。2026-07 に 61.2GB（1224%）、2026-08 に 7.312GB
（146%）と**2回続けて超過**し、いずれも organization 全体が restricted（402）になった。
2回目の超過では月 9.5GB のうち内訳が分かっているのは夜間バッチ 1.98GB だけで、
**残り 5GB 超の帰属が最後まで特定できなかった**。

特定できなかった理由は原因が難しいからではなく、**測る仕組みが `scripts/` 配下にしか
無かったから**（`scripts/_cache.py` の HIT/MISS ログ）。夜間バッチ本体（nightly_scores.py /
hyperparameter_search.py / recommend_factor_premia.py / macro_beta_inference.py）・
`routers/` の全 API・`collector*.py` は完全に無計測で、事後に「誰が食ったか」を答える
手段が存在しない。本モジュールはその穴を塞ぐ。

## 計測は二階建て

| 層 | 何 | いつ | 精度 |
|---|---|---|---|
| **正本** | サーバ側 `sum(octet_length(列::text))` | 手動・設計判断のとき | 実測（誤差数%） |
| **常時**（本モジュール） | 行数 × 較正済み B/行 | 全プロセス・全経路で自動 | 推定 |

**本モジュールの数字は正本ではない。** 役目は2つに限る——(1) 全経路の帰属を常時残して
「誰が食ったか」に答えられるようにすること、(2) 暴走をプロセス単位で止めること。
係数の正本は docs/DEPLOYMENT.md「Egress 設計」の実測表で、較正は #446 と同じ手順で行う。

## どう測るか

psycopg2 の既定カーソルは**クライアント側バッファ**なので、`execute()` が返った時点で
行は既にネットワークを渡り終えている。SQLAlchemy の `after_cursor_execute` フックは
その直後に呼ばれ、`cursor.rowcount`（転送された行数）と `cursor.description`（列数）を
**結果を消費せずに**読める。既存の挙動には一切干渉しない。

計上対象は「結果セットを返した文」＝`cursor.description` が None でない文。SELECT 以外でも
`INSERT ... RETURNING` は行が返る＝Egress なので拾う。逆に INSERT/UPDATE/DELETE 本体は
ingress（無料）なので description が None となり自動的に外れる。**文字列を見て
「SELECT で始まるか」で判定しない**——RETURNING を取り落とす。

**SQLite では SELECT の `rowcount` が -1 になる**（DB-API の仕様上、行数を事前に確定できない）。
これを 0 に丸めると「引いていない」と区別がつかなくなるため `unknown` バケットへ隔離する。
未計測を欠測値に化けさせないのは `macro_snapshots._VOLUME_NOT_LOADED` と同じ考え方
（#438 の「静かな停止」を作らない）。テスト経路は全部 SQLite なので、CI での計上は常に
unknown 側に積まれる＝実数の検証は実 PostgreSQL でしかできない。

## サーキットブレーカ

プロセス単位で行数とバイト数の上限を持ち、超えたら `EgressBudgetExceeded` を送出する。
GitHub Actions では例外＝ワークフロー failure ＝ `notify-failure.yml` が Issue を自動起票
するので、**ブレーカは自分で自分を報告する**。

上限の局所上書きは `egress_budget()` コンテキストマネージャで行い、抜けるとき必ず戻す。
プロセス全体の既定を書き換える口は用意しない（ADR-0032 の `db_timeouts` と同じ原則＝
「上書きは局所・既定は変えない」。安全網を恒久的に外すと、次に暴走したとき誰も止めない）。

## 歯止めは2軸ある（プロセス予算だけでは月枠を守れない）

**プロセス予算（`DEFAULT_MB_LIMIT`）は「1回の暴走」しか止められない。** 400MB/プロセス
なので、1日に 12 プロセス走れば 4.8GB/日を流しても一度も踏まれない。

過去2回の超過は種類が違っていた:

| | 形 | プロセス予算 |
|---|---|---|
| 2026-07（61.2GB） | 暴走型（検証フルロードの反復） | 効く |
| 2026-08（7.312GB） | **じわじわ型**（スパイク約2GB＋平常運転 約5GB） | **効かない** |

再発したのはじわじわ型なのに、対策は暴走型にしか効いていなかった。そこで
**請求サイクル単位の累計**（`cycle_*`）を別軸で持つ。置き場所は DB（`app_settings`）で、
理由は **ローカル CLI と GitHub Actions ランナーが同じカウンタを見られる唯一の場所**
だから（`weekly_price_cache` が世代印を DB に置いたのと同じ理屈＝
「印は書き手と読み手の両方から見える場所に置く」）。

累計は本番（リモート）接続のときだけ積む。ローカルミラー（#481）からの読取は
Supabase の Egress を1バイトも使わないので、積むと**ミラーへ逃がす動機を自分で壊す**。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import sys
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# 記号は ASCII だけを使う。cp932 の Windows コンソールへリダイレクトすると非 ASCII は
# UnicodeEncodeError で出力済みの内容ごとクラッシュする（既知の罠）。
_LOG_PREFIX = "[egress]"


class EgressBudgetExceeded(RuntimeError):
    """プロセスの Egress 予算を超えた。呼び出し側で握らず落とすこと（GHA では failure=自動起票）。"""


# ── 較正テーブル ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EgressCost:
    """B/行 の較正値。**出所の無い数字を混ぜないため measured_on / source_issue は必須**。"""
    bytes_per_row: float
    measured_on: str
    source_issue: str
    note: str


# (テーブル名, 転送列数) の実測値。docs/DEPLOYMENT.md「Egress 設計」の実測表が正本。
#
# 出所は2世代ある。**どちらもサーバ側 `sum(octet_length(列::text))` が正本**（#446 の測り方）:
#   #446（2026-08-06）… 消費側が実際に転送する**部分列**を測ったもの。部分列の推定はこちらが効く
#   #493（2026-08-19）… Egress リセット後に mirror 16 表を**全列**で測り直したもの（ランブック手順4）
# 部分列エントリを全列エントリで上書きしてはいけない（列ごとに B/値 が違うため過小評価になる）。
EGRESS_COST_TABLE: dict[tuple[str, int], EgressCost] = {
    # ── #446: 消費側が実際に投げる部分列（この形の SELECT が本番の主経路）
    ("stock_price_weekly", 3): EgressCost(
        32.1, "2026-08-06", "#446", "edinet_code/trade_date/close_last = 39.3MB / 1,282,436 行"),
    ("stock_price_weekly", 4): EgressCost(
        42.0, "2026-08-06", "#446", "+volume_sum = 51.4MB / 1,282,436 行"),
    ("macro_data", 3): EgressCost(
        40.1, "2026-08-06", "#446", "series_code/trade_date/close = 2.6MB / 67,906 行"),
    ("financial_metrics", 97): EgressCost(
        779.0, "2026-08-06", "#446", "VIEW 全列 = 22.5MB / 30,285 行（mirror 対象外＝#493 で未測）"),

    # ── #493: mirror 16 表の全列実測（app_settings は 0 行で測れず未収録）
    ("stock_price_weekly", 7): EgressCost(
        54.4, "2026-08-19", "#493", "全列 = 66.6MB / 1,284,465 行"),
    ("financial_records", 69): EgressCost(
        643.3, "2026-08-19", "#493",
        "全列 = 31.0MB / 50,478 行（#446 の 663.0 は sector_ols がロードする 4,430 行での値）"),
    ("statement_disclosure", 34): EgressCost(
        269.8, "2026-08-19", "#493", "全列 = 8.5MB / 32,855 行"),
    ("macro_data", 11): EgressCost(
        133.8, "2026-08-19", "#493", "全列 = 12.5MB / 97,776 行（#446 の 134.2 とほぼ一致＝検算）"),
    ("macro_beta_loadings", 7): EgressCost(
        121.4, "2026-08-19", "#493", "全列 = 10.5MB / 90,841 行"),
    ("companies", 14): EgressCost(
        131.5, "2026-08-19", "#493", "全列 = 0.6MB / 4,437 行（#446 は 118.0・同じ行数で +11%）"),
    ("regression_results", 8): EgressCost(
        84.4, "2026-08-19", "#493", "全列 = 0.4MB / 5,336 行"),
    ("macro_enet_scores", 7): EgressCost(
        79.8, "2026-08-19", "#493", "全列 = 0.1MB / 1,710 行"),
    ("macro_gbdt_scores", 7): EgressCost(
        67.8, "2026-08-19", "#493", "全列 = 0.1MB / 1,687 行"),
    ("macro_ensemble_scores", 6): EgressCost(
        58.9, "2026-08-19", "#493", "全列 = 0.1MB / 1,697 行"),
    ("macro_dlm_scores", 6): EgressCost(
        57.2, "2026-08-19", "#493", "全列 = 0.2MB / 3,549 行"),
    ("recommend_factor_premia", 9): EgressCost(
        139.8, "2026-08-19", "#493", "全列 = 2,236B / 16 行"),
    ("collection_logs", 9): EgressCost(
        114.0, "2026-08-19", "#493", "全列 = 228B / 2 行"),
    # JSON 列が支配的な2表。行数が 2〜3 しかなく B/行 は行の中身で桁が動く（保守側＝大きめ）
    ("macro_beta_meta", 7): EgressCost(
        3445.0, "2026-08-19", "#493", "全列 = 6,890B / 2 行。JSON 列が支配的"),
    ("plugin_tuned_params", 8): EgressCost(
        9908.0, "2026-08-19", "#493", "全列 = 29,724B / 3 行。JSON 列が支配的"),
}

# 列数が実測と違う組み合わせ用の B/列/行。上表を列数で割って導出した同じ実測由来の値。
# **部分列の実測がある表は、そちらから割った値を据え置く**（全列平均で上書きすると
# 部分列の推定が過小になる＝ブレーカが甘くなる。stock_price_weekly は 3列 10.7 に対し
# 全列平均 7.8 で、文字列列と数値列で B/値 が違うことがそのまま出ている）。
EGRESS_BYTES_PER_COLUMN: dict[str, EgressCost] = {
    "stock_price_weekly": EgressCost(10.7, "2026-08-06", "#446", "32.1 B/行 ÷ 3列（部分列由来を据え置き）"),
    "macro_data": EgressCost(13.4, "2026-08-06", "#446", "40.1 B/行 ÷ 3列（部分列由来を据え置き）"),
    "financial_metrics": EgressCost(8.03, "2026-08-06", "#446", "779 B/行 ÷ 97列"),
    "financial_records": EgressCost(9.3, "2026-08-19", "#493", "643.3 B/行 ÷ 69列"),
    "companies": EgressCost(9.4, "2026-08-19", "#493", "131.5 B/行 ÷ 14列"),
    "macro_beta_loadings": EgressCost(17.3, "2026-08-19", "#493", "121.4 B/行 ÷ 7列"),
    "recommend_factor_premia": EgressCost(15.5, "2026-08-19", "#493", "139.8 B/行 ÷ 9列"),
    "collection_logs": EgressCost(12.7, "2026-08-19", "#493", "114.0 B/行 ÷ 9列"),
    "macro_enet_scores": EgressCost(11.4, "2026-08-19", "#493", "79.8 B/行 ÷ 7列"),
    "regression_results": EgressCost(10.6, "2026-08-19", "#493", "84.4 B/行 ÷ 8列"),
    "macro_ensemble_scores": EgressCost(9.8, "2026-08-19", "#493", "58.9 B/行 ÷ 6列"),
    "macro_gbdt_scores": EgressCost(9.7, "2026-08-19", "#493", "67.8 B/行 ÷ 7列"),
    "macro_dlm_scores": EgressCost(9.5, "2026-08-19", "#493", "57.2 B/行 ÷ 6列"),
    "statement_disclosure": EgressCost(7.9, "2026-08-19", "#493", "269.8 B/行 ÷ 34列"),
    "macro_beta_meta": EgressCost(492.1, "2026-08-19", "#493", "3445.0 B/行 ÷ 7列。JSON 列が支配的"),
    "plugin_tuned_params": EgressCost(1238.5, "2026-08-19", "#493", "9908.0 B/行 ÷ 8列。JSON 列が支配的"),
}

# 未較正のテーブル用。上記の実測レンジ（JSON 表を除いて 7.9〜17.3）の上側を取り、
# **保守側＝多めに見積もる**。過小評価するとブレーカが踏まれず超過を許すので、
# 迷ったら大きい方へ倒す。#446 時点は 12.0 だったが、#493 の全表実測で
# macro_beta_loadings が 17.3 とそれを上回っていたため引き上げた。
DEFAULT_BYTES_PER_COLUMN = 17.5

# 既定予算。既知の最大実行（夜間バッチ = 週次 1,282,436 + fin 30,285 + macro 67,906
# + records 4,430 + companies 4,437 ≒ 1.39M 行 / 67.7MB）の約2倍に置く。
DEFAULT_ROW_LIMIT = 3_000_000
DEFAULT_MB_LIMIT = 400.0
_WARN_RATIOS = (0.5, 0.8)


# ── 請求サイクル単位の累計（プロセス予算とは別軸・上の docstring 参照）──────────

# Supabase 無料枠。docs/DEPLOYMENT.md「外部サービス制約」が正本。
QUOTA_BYTES = 5 * 1024 ** 3

# 請求サイクルの境界日。2026-08-19 実測のダッシュボード表記は "18 Aug 2026 - 18 Sep 2026"。
# **Egress がリセットされる日**であって、restricted が解ける日ではない（解除には
# "a short delay after your billing period resets" があり 8/18→8/19 に実際にずれた）。
EGRESS_CYCLE_DAY = 18

CYCLE_START_KEY = "egress_cycle_start"
CYCLE_BYTES_KEY = "egress_cycle_bytes"

# 累計の閾値。**ブロックを 100% に置かない**——枠を使い切った瞬間に全ジョブが死ぬより、
# 手前で止めて人が判断できる余地（残り 5%＝256MB）を残す方が復旧が速い。
# 80% は警告のみ。GHA では egress-health.yml が同じ値で exit 2 し Issue を自動起票する。
CYCLE_WARN_RATIO = 0.80
CYCLE_BLOCK_RATIO = 0.95

# 台帳 JSONL の既定の出力先。**環境変数が無くても書く**（#478 の穴3）。
# 過去2回の超過はどちらもローカル検証の反復が主因だったが、`FINAPP_EGRESS_LEDGER` を
# 人が手で立てる運用だったため 1バイトも記録が残っていなかった（2026-08-19 時点の
# .egress/ledger.jsonl は 1,961B・前日の1回のみ）。「覚えていれば計測される」を
# 「黙っていても計測される」へ倒す。無効化は FINAPP_EGRESS_LEDGER=0。
DEFAULT_LEDGER_PATH = ".egress/ledger.jsonl"


def bytes_per_row(table: str, n_cols: int) -> float:
    """(テーブル, 列数) の B/行。実測 → 列単価 → 既定 の順に解決する。"""
    exact = EGRESS_COST_TABLE.get((table, n_cols))
    if exact is not None:
        return exact.bytes_per_row
    per_col = EGRESS_BYTES_PER_COLUMN.get(table)
    rate = per_col.bytes_per_row if per_col is not None else DEFAULT_BYTES_PER_COLUMN
    return rate * max(n_cols, 1)


# ── 文からのテーブル名抽出 ─────────────────────────────────────────────────

# 最初に現れる FROM / JOIN / INSERT INTO / UPDATE の対象を主テーブルとみなす。JOIN を含む文は
# 主テーブルへまとめて計上される（帰属の粒度としてはこれで足りる）。`FROM (` のような
# サブクエリは識別子にマッチしないので次の候補へ送られる。
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|INSERT\s+INTO|UPDATE)\s+(?:ONLY\s+)?"
    r'"?([A-Za-z_][A-Za-z0-9_$]*)"?(?:\s*\.\s*"?([A-Za-z_][A-Za-z0-9_$]*)"?)?',
    re.IGNORECASE,
)

UNKNOWN_TABLE = "?"


def extract_table(statement: str) -> str:
    """SQL 文から主テーブル名を取り出す。判別できなければ "?"（捨てずに残す）。"""
    if not statement:
        return UNKNOWN_TABLE
    m = _TABLE_RE.search(statement)
    if m is None:
        return UNKNOWN_TABLE
    # schema.table 形式なら table 側を採る
    return (m.group(2) or m.group(1)).lower()


# ── 台帳 ───────────────────────────────────────────────────────────────────

@dataclass
class _Bucket:
    calls: int = 0
    rows: int = 0
    est_bytes: float = 0.0
    unknown_calls: int = 0      # rowcount が取れなかった呼び出し（SQLite 等）


@dataclass
class _External:
    label: str
    n_bytes: int
    note: str


class Ledger:
    """プロセス単位の累計。スレッド跨ぎで積むので lock で守る
    （execute_plugin が asyncio.to_thread でワーカースレッドへ逃がすため）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.buckets: dict[str, _Bucket] = {}
        self.external: list[_External] = []
        self.calls = 0
        self.rows = 0
        self.est_bytes = 0.0
        self.unknown_calls = 0
        self._warned: set[str] = set()

    # -- 記録 ---------------------------------------------------------------

    def record(self, statement: str, rowcount: int, n_cols: int) -> None:
        """1文ぶんの転送を計上する。rowcount < 0 は unknown へ隔離（0 に丸めない）。"""
        # サイクル台帳自身の読み書きは計上しない。計上すると `_check_budget` →
        # `cycle_snapshot` → `_load_cycle` → SELECT → `record` の無限再帰になる。
        # スレッドローカルなので、他スレッドの計測は止めない。
        if _in_cycle_io():
            return
        table = extract_table(statement)
        with self._lock:
            b = self.buckets.setdefault(table, _Bucket())
            b.calls += 1
            self.calls += 1
            if rowcount is None or rowcount < 0:
                b.unknown_calls += 1
                self.unknown_calls += 1
            else:
                est = rowcount * bytes_per_row(table, n_cols)
                b.rows += rowcount
                b.est_bytes += est
                self.rows += rowcount
                self.est_bytes += est
        _check_budget()

    def record_external(self, label: str, n_bytes: int, note: str = "") -> None:
        """SQLAlchemy を通らない転送（pg_dump 等）を手で計上する。予算には含める。"""
        with self._lock:
            self.external.append(_External(label, int(n_bytes), note))
            self.est_bytes += float(n_bytes)
        _check_budget()

    # -- 参照 ---------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "rows": self.rows,
                "est_bytes": self.est_bytes,
                "unknown_calls": self.unknown_calls,
                "tables": {
                    t: {"calls": b.calls, "rows": b.rows,
                        "est_bytes": b.est_bytes, "unknown_calls": b.unknown_calls}
                    for t, b in self.buckets.items()
                },
                "external": [{"label": e.label, "bytes": e.n_bytes, "note": e.note}
                             for e in self.external],
            }

    def reset(self) -> None:
        with self._lock:
            self.buckets.clear()
            self.external.clear()
            self.calls = self.rows = self.unknown_calls = 0
            self.est_bytes = 0.0
            self._warned.clear()

    def warn_once(self, key: str) -> bool:
        with self._lock:
            if key in self._warned:
                return False
            self._warned.add(key)
            return True


LEDGER = Ledger()


# ── 予算 ───────────────────────────────────────────────────────────────────

_override: dict[str, Optional[float]] = {"rows": None, "mb": None}
_override_lock = threading.RLock()


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def limits() -> tuple[float, float]:
    """(行数上限, MB上限)。egress_budget() の上書きが最優先、次に環境変数、最後に既定。"""
    with _override_lock:
        rows = _override["rows"]
        mb = _override["mb"]
    if rows is None:
        rows = _env_number("FINAPP_EGRESS_ROW_LIMIT", DEFAULT_ROW_LIMIT)
    if mb is None:
        mb = _env_number("FINAPP_EGRESS_MB_LIMIT", DEFAULT_MB_LIMIT)
    return float(rows), float(mb)


def enforcing() -> bool:
    """FINAPP_EGRESS_ENFORCE=0 で送出だけ止める（計測は続く）。"""
    return os.environ.get("FINAPP_EGRESS_ENFORCE", "1").strip() != "0"


def _log(message: str) -> None:
    """標準エラーへ 1 行出す（標準出力は各スクリプトの成果物なので混ぜない）。"""
    try:
        print(f"{_LOG_PREFIX} {message}", file=sys.stderr, flush=True)
    except Exception:      # atexit で stderr が閉じている等。報告のために本体を落とさない
        pass


def _mb(n_bytes: float) -> str:
    return f"{n_bytes / (1024 * 1024):.1f}MB"


def _check_budget() -> None:
    row_limit, mb_limit = limits()
    snap_rows = LEDGER.rows
    snap_mb = LEDGER.est_bytes / (1024 * 1024)

    for name, value, limit in (("rows", snap_rows, row_limit), ("mb", snap_mb, mb_limit)):
        if limit <= 0:
            continue
        ratio = value / limit
        for w in _WARN_RATIOS:
            if ratio >= w and LEDGER.warn_once(f"{name}:{w}"):
                _log(f"WARN {name} {value:.0f}/{limit:.0f} ({ratio:.0%}) - {top_tables_line()}")
        if ratio >= 1.0:
            msg = (f"Egress budget exceeded: {name}={value:.0f} limit={limit:.0f} "
                   f"({top_tables_line()})")
            if not enforcing():
                if LEDGER.warn_once(f"{name}:over"):
                    _log(f"OVER (enforce=0, continuing) {msg}")
                continue
            raise EgressBudgetExceeded(
                msg + " -- 上限は FINAPP_EGRESS_ROW_LIMIT / FINAPP_EGRESS_MB_LIMIT、"
                      "一時的な解除は FINAPP_EGRESS_ENFORCE=0、"
                      "局所的な引き上げは db_egress.egress_budget()")

    # プロセス予算を通っても、請求サイクルの累計が枠に迫っていれば止める（別軸）
    _check_cycle_budget()


@contextmanager
def egress_budget(*, mb: Optional[float] = None, rows: Optional[float] = None):
    """`with` の内側だけ予算を差し替える（抜けるとき必ず戻す）。

    正当に重い一回性の処理（初回ミラー pull・全件バックフィル）にだけ使う。
    ADR-0032 の `db_timeouts` と同じく **プロセス全体の既定は変えない**。
    プロセス単位の値なのでスレッドごとの独立した上書きにはならない。
    """
    with _override_lock:
        prev = dict(_override)
        if mb is not None:
            _override["mb"] = float(mb)
        if rows is not None:
            _override["rows"] = float(rows)
    try:
        yield
    finally:
        with _override_lock:
            _override.update(prev)


# ── 請求サイクル累計 ───────────────────────────────────────────────────────

# サイクル台帳の読み書き中フラグ。**スレッドローカル**にしてあるのは、フラグを立てて
# いる間だけ他スレッドの計測まで止めてしまうのを避けるため（execute_plugin が
# asyncio.to_thread でワーカースレッドへ逃がすので、並行実行は常に起こりうる）。
_cycle_io = threading.local()
_cycle_lock = threading.RLock()
_cycle_state: dict = {"loaded": False, "start": None, "base_bytes": 0.0}
_cycle_enabled: Optional[bool] = None


def _in_cycle_io() -> bool:
    return getattr(_cycle_io, "active", False)


@contextmanager
def _cycle_io_scope():
    prev = _in_cycle_io()
    _cycle_io.active = True
    try:
        yield
    finally:
        _cycle_io.active = prev


def current_cycle_start(today: Optional[date] = None) -> date:
    """`today` が属する請求サイクルの開始日（直近の `EGRESS_CYCLE_DAY`）。"""
    d = today or datetime.now(timezone.utc).date()
    if d.day >= EGRESS_CYCLE_DAY:
        return d.replace(day=EGRESS_CYCLE_DAY)
    last_month_end = d.replace(day=1) - timedelta(days=1)
    # 境界日が存在しない月（29〜31 日を指定した場合）でも末日へ丸めて必ず解ける
    return last_month_end.replace(day=min(EGRESS_CYCLE_DAY, last_month_end.day))


def _running_under_pytest() -> bool:
    """テスト実行中か。差し替え可能にするため関数に切ってある。"""
    return "pytest" in sys.modules


def cycle_tracking_enabled() -> bool:
    """本番（リモート）接続のときだけ累計を積むか。

    ローカル PostgreSQL のミラー（#481・`FINAPP_DB_TARGET=local`）からの読取は
    Supabase の Egress を1バイトも使わない。ここを積むと「ミラーで検証したのに枠が
    減る」ことになり、**ミラーへ逃がす動機を自分で壊す**。台帳 JSONL には従来どおり
    残るので、帰属の記録が失われるわけではない。

    緊急停止は `FINAPP_EGRESS_CYCLE=0`（プロセス予算と JSONL 台帳は生きたまま）。

    **pytest 実行中は必ず無効。** かつてローカルの pytest は `database.py` を import した
    時点で本番 Supabase 向けの engine を作っていた（`.env` の `DATABASE_URL` を読むため）。
    有効なままだと `record()` を呼ぶすべてのテストが本番へ接続しに行き、`emit_summary` の
    atexit が本番へ書き込みまで行う——実際に全体テストが 10分超ハングして気づいた。

    #503 で既定が `local` へ反転したので、素の pytest はもう本番 engine を作らない。
    それでもこのガードは残す: `FINAPP_DB_TARGET=prod` を立てたシェルで pytest を回せば
    同じ経路が復活するし、**ガードを外す理由が「いまは踏まないから」では弱い**（踏んだ
    ときの被害が本番書込なので、条件の変化で復活する種類の事故である）。
    有効時の挙動はこの関数ごと差し替えて検証する。
    """
    global _cycle_enabled
    if _cycle_enabled is not None:
        return _cycle_enabled
    if _running_under_pytest():
        _cycle_enabled = False
        return False
    if os.environ.get("FINAPP_EGRESS_CYCLE", "1").strip() == "0":
        _cycle_enabled = False
        return False
    try:
        from database import _is_local
    except Exception:
        _cycle_enabled = False      # database を import できない文脈では積まない
        return False
    _cycle_enabled = not _is_local
    return _cycle_enabled


def _load_cycle() -> None:
    """サイクル累計を **1プロセス1回だけ** DB から読む。

    失敗しても本処理は落とさず、以降は再試行もしない（毎クエリで DB を叩かない）。
    読めなかった場合は base=0 のまま進む＝**ゲートが甘くなる方向**へ倒れるが、ここで
    raise すると計測の失敗が本業を止めることになり本末転倒（`_append_jsonl` と同じ原則）。
    """
    with _cycle_lock:
        if _cycle_state["loaded"]:
            return
        _cycle_state["loaded"] = True
        if not cycle_tracking_enabled():
            return
        wanted = current_cycle_start().isoformat()
        _cycle_state["start"] = wanted
        try:
            with _cycle_io_scope():
                from database import SessionLocal, get_setting
                with SessionLocal() as db:
                    if get_setting(db, CYCLE_START_KEY) == wanted:
                        _cycle_state["base_bytes"] = float(
                            get_setting(db, CYCLE_BYTES_KEY) or 0)
                    # 印が違う＝サイクルが切り替わった。累計は 0 から数え直す
        except Exception as exc:
            _log(f"WARN cycle ledger read failed: {exc}")


def cycle_snapshot() -> dict:
    """サイクル累計の現在値。DB は初回だけ読み、以降はメモリ演算のみ。"""
    _load_cycle()
    with _cycle_lock:
        base = float(_cycle_state["base_bytes"])
        start = _cycle_state["start"]
    total = base + LEDGER.est_bytes
    return {
        "start": start,
        "base_bytes": base,
        "process_bytes": LEDGER.est_bytes,
        "total_bytes": total,
        "ratio": (total / QUOTA_BYTES) if QUOTA_BYTES else 0.0,
    }


def _flush_cycle() -> None:
    """このプロセスの推定転送量をサイクル累計へ加算する（`emit_summary` から1回）。"""
    if not cycle_tracking_enabled():
        return
    # **delta が 0 でも印は進める。** 印が現サイクルに追いついていないと「消費ゼロ」と
    # 「計測そのものが止まっている」を区別できず（#438 と同型）、`egress-health` の
    # 「記録がまだ無い」注記が毎日出て本当の異常が埋もれる。加算 0 は無害。
    delta = max(0, int(LEDGER.est_bytes))
    start = current_cycle_start().isoformat()
    try:
        with _cycle_io_scope():
            from sqlalchemy import text
            from database import SessionLocal
            with SessionLocal() as db:
                # サイクル印を更新する。**値が変わったときだけ行を返す**ので、
                # 「サイクルが切り替わったか」を追加の SELECT 無しで判定できる。
                rolled = db.execute(text(
                    "INSERT INTO app_settings (key, value, updated_at) "
                    "VALUES (:k, :v, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now() "
                    "WHERE app_settings.value IS DISTINCT FROM excluded.value "
                    "RETURNING 1"
                ), {"k": CYCLE_START_KEY, "v": start}).fetchone() is not None

                # **読んで足して書く3手に分けない。** 夜間バッチと手元の CLI が同時に
                # 走ると、後から書いた方が相手の加算を潰す。1文の中で加算する。
                db.execute(text(
                    "INSERT INTO app_settings (key, value, updated_at) "
                    "VALUES (:k, :d, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = ("
                    "  CASE WHEN :rolled THEN 0 "
                    "       ELSE COALESCE(NULLIF(app_settings.value, '')::bigint, 0) END "
                    "  + :n)::text, updated_at = now()"
                ), {"k": CYCLE_BYTES_KEY, "d": str(delta), "rolled": rolled, "n": delta})
                db.commit()
    except Exception as exc:
        _log(f"WARN cycle ledger write failed: {exc}")


def _check_cycle_budget() -> None:
    """サイクル累計が閾値を超えたら警告／送出する（プロセス予算とは独立に効く）。"""
    if not cycle_tracking_enabled():
        return
    snap = cycle_snapshot()
    ratio = snap["ratio"]
    if ratio < CYCLE_WARN_RATIO:
        return
    line = (f"cycle {_mb(snap['total_bytes'])}/{QUOTA_BYTES / 1024 ** 3:.0f}GB "
            f"({ratio:.0%}) since={snap['start']} - {top_tables_line()}")
    if ratio >= CYCLE_BLOCK_RATIO:
        msg = f"Egress cycle budget exceeded: {line}"
        if not enforcing():
            if LEDGER.warn_once("cycle:over"):
                _log(f"OVER (enforce=0, continuing) {msg}")
        else:
            raise EgressBudgetExceeded(
                msg + f" -- 請求サイクルの累計が無料枠の {CYCLE_BLOCK_RATIO:.0%} に達しました。"
                      "一時的な解除は FINAPP_EGRESS_ENFORCE=0、"
                      "累計そのものを止めるなら FINAPP_EGRESS_CYCLE=0")
    if LEDGER.warn_once("cycle:warn"):
        _log(f"WARN {line}")


# ── 報告 ───────────────────────────────────────────────────────────────────

def _ascii(text: str) -> str:
    """cp932 コンソールでも壊れないよう ASCII へ落とす（ジョブ名は外部入力）。"""
    return text.encode("ascii", "replace").decode("ascii")


def top_tables_line(n: int = 3) -> str:
    snap = LEDGER.snapshot()
    top = sorted(snap["tables"].items(), key=lambda kv: kv[1]["est_bytes"], reverse=True)[:n]
    if not top:
        return "top=-"
    return "top=" + ",".join(f"{t}:{_mb(v['est_bytes'])}" for t, v in top)


def summary_line() -> str:
    snap = LEDGER.snapshot()
    parts = [
        f"summary job={_ascii(job_label())}",
        f"total={_mb(snap['est_bytes'])}",
        f"rows={snap['rows']}",
        f"calls={snap['calls']}",
    ]
    if snap["unknown_calls"]:
        parts.append(f"unknown_rowcount={snap['unknown_calls']}")
    if snap["external"]:
        ext = sum(e["bytes"] for e in snap["external"])
        parts.append(f"external={_mb(ext)}")
    if cycle_tracking_enabled():
        c = cycle_snapshot()
        parts.append(f"cycle={_mb(c['total_bytes'])}/{QUOTA_BYTES / 1024 ** 3:.0f}GB"
                     f"({c['ratio']:.0%})")
    parts.append(top_tables_line())
    return " ".join(parts)


def job_label() -> str:
    return os.environ.get("FINAPP_JOB") or Path(sys.argv[0]).name or "unknown"


def ledger_path() -> Optional[str]:
    """台帳 JSONL の出力先。**未設定なら既定パスへ書く**（#478 の穴3）。

    明示的な無効化は `FINAPP_EGRESS_LEDGER=0`（空文字も同じ）。
    既定オンにする理由は `DEFAULT_LEDGER_PATH` のコメントを参照。
    """
    raw = os.environ.get("FINAPP_EGRESS_LEDGER")
    if raw is None:
        return DEFAULT_LEDGER_PATH
    raw = raw.strip()
    if raw in ("", "0"):
        return None
    return raw


def _append_jsonl(path_str: str) -> None:
    snap = LEDGER.snapshot()
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "job": _ascii(job_label()),
        "argv": _ascii(" ".join(sys.argv))[:300],
        "pid": os.getpid(),
        "calls": snap["calls"],
        "rows": snap["rows"],
        "est_bytes": round(snap["est_bytes"]),
        "unknown_calls": snap["unknown_calls"],
        "tables": {t: {"calls": v["calls"], "rows": v["rows"],
                       "est_bytes": round(v["est_bytes"]),
                       "unknown_calls": v["unknown_calls"]}
                   for t, v in snap["tables"].items()},
        "external": snap["external"],
    }
    try:
        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception as exc:      # 台帳が書けないことで本処理を落とさない
        _log(f"WARN ledger write failed: {exc}")


_emitted = False


def emit_summary() -> None:
    """プロセス終了時に累計を1行で出す（1文も引いていなければ黙る）。

    **1プロセス1回しか出さない**。明示的に呼んだ後に atexit がもう一度発火すると、
    JSONL 台帳へ同じ実行が2行入り、ロールアップが実行数も転送量も二重に数える。
    """
    global _emitted
    if _emitted:
        return
    if LEDGER.calls == 0 and not LEDGER.external:
        return
    _emitted = True
    _flush_cycle()          # 先に加算する（失敗しても summary は必ず出す）
    _log(summary_line())
    path = ledger_path()
    if path:
        _append_jsonl(path)


atexit.register(emit_summary)


# ── engine への取り付け ────────────────────────────────────────────────────

# id() ではなく弱参照集合で持つ。engine が GC された後に id が再利用されると
# 「登録済み」と誤判定してリスナが張られない（テストで engine を作り直すと踏む）。
_installed: "weakref.WeakSet" = weakref.WeakSet()


def install(engine) -> bool:
    """engine に after_cursor_execute リスナを張る。二重登録は無視（二重計上を防ぐ）。"""
    if engine in _installed:
        return False
    from sqlalchemy import event

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        # executemany は一括 INSERT/UPDATE＝ingress。結果セットも返さない。
        if executemany:
            return
        try:
            description = cursor.description
            if description is None:
                return          # 結果セットを返さない文＝転送なし
            n_cols = len(description)
            rowcount = cursor.rowcount
        except Exception:
            return              # カーソルの状態が読めない場合は黙って見送る
        LEDGER.record(statement, rowcount, n_cols)

    _installed.add(engine)
    return True


def _reset_for_tests() -> None:
    """テスト専用。台帳・上書き予算・サマリ出力済みフラグ・サイクル状態を初期化する。"""
    global _emitted, _cycle_enabled
    _emitted = False
    _cycle_enabled = None
    LEDGER.reset()
    with _override_lock:
        _override["rows"] = None
        _override["mb"] = None
    with _cycle_lock:
        _cycle_state.update({"loaded": False, "start": None, "base_bytes": 0.0})
    _cycle_io.active = False
