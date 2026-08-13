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
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


# (テーブル名, 転送列数) の実測値。docs/DEPLOYMENT.md「Egress 設計」の #446 実測表が正本。
EGRESS_COST_TABLE: dict[tuple[str, int], EgressCost] = {
    ("stock_price_weekly", 3): EgressCost(
        32.1, "2026-08-06", "#446", "edinet_code/trade_date/close_last = 39.3MB / 1,282,436 行"),
    ("stock_price_weekly", 4): EgressCost(
        42.0, "2026-08-06", "#446", "+volume_sum = 51.4MB / 1,282,436 行"),
    ("macro_data", 3): EgressCost(
        40.1, "2026-08-06", "#446", "series_code/trade_date/close = 2.6MB / 67,906 行"),
    ("macro_data", 11): EgressCost(
        12.2 * 11, "2026-08-06", "#446", "ORM 全列 = 8.7MB / 67,906 行"),
    ("financial_metrics", 97): EgressCost(
        779.0, "2026-08-06", "#446", "VIEW 全列 = 22.5MB / 30,285 行"),
    ("financial_records", 69): EgressCost(
        663.0, "2026-08-06", "#446", "ORM 全列 = 2.8MB / 4,430 行（sector_ols）"),
    ("companies", 14): EgressCost(
        118.0, "2026-08-06", "#446", "ORM 全列 = 0.5MB / 4,437 行"),
}

# 列数が実測と違う組み合わせ用の B/列/行。上表を列数で割って導出した同じ実測由来の値。
EGRESS_BYTES_PER_COLUMN: dict[str, EgressCost] = {
    "stock_price_weekly": EgressCost(10.7, "2026-08-06", "#446", "32.1 B/行 ÷ 3列"),
    "macro_data": EgressCost(13.4, "2026-08-06", "#446", "40.1 B/行 ÷ 3列"),
    "financial_metrics": EgressCost(8.03, "2026-08-06", "#446", "779 B/行 ÷ 97列"),
    "financial_records": EgressCost(9.6, "2026-08-06", "#446", "663 B/行 ÷ 69列"),
    "companies": EgressCost(8.4, "2026-08-06", "#446", "118 B/行 ÷ 14列"),
}

# 未較正のテーブル用。上記の実測レンジ（8.0〜13.4）の上側を取り、**保守側＝多めに見積もる**。
# 過小評価するとブレーカが踏まれず超過を許すので、迷ったら大きい方へ倒す。
DEFAULT_BYTES_PER_COLUMN = 12.0

# 既定予算。既知の最大実行（夜間バッチ = 週次 1,282,436 + fin 30,285 + macro 67,906
# + records 4,430 + companies 4,437 ≒ 1.39M 行 / 67.7MB）の約2倍に置く。
DEFAULT_ROW_LIMIT = 3_000_000
DEFAULT_MB_LIMIT = 400.0
_WARN_RATIOS = (0.5, 0.8)


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
    parts.append(top_tables_line())
    return " ".join(parts)


def job_label() -> str:
    return os.environ.get("FINAPP_JOB") or Path(sys.argv[0]).name or "unknown"


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
    _log(summary_line())
    path = os.environ.get("FINAPP_EGRESS_LEDGER")
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
    """テスト専用。台帳・上書き予算・サマリ出力済みフラグを初期化する。"""
    global _emitted
    _emitted = False
    LEDGER.reset()
    with _override_lock:
        _override["rows"] = None
        _override["mb"] = None
