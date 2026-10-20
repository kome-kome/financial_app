"""本番予測の月次の写し（#1005・親 #1001）。

`*_scores`（μ̂）と `regression_results`（gap_ratio）は全置換・最新だけで、過去の予測が残らない。
「予測が通用しなくなった」を直接確かめる唯一の方法は、**固定した予測と 52 週後の実現値を突き合わせる
本当の OOS** なので、月に1回その時点の予測を `prediction_snapshots` へ写して積む。TOPIX の入替に
限らず、どのレジーム変化でも同じ材料になる。

## 守ること

- **写すだけ**。新しい推定はしない（ADR-0061 決定1）。読む入口は画面・売りランキングと同じ
  （μ̂ は `read_producer_scores`、gap は各社の最新年度の `financial_metrics.gap_ratio`）——別の読み方を
  書くと、写した値が画面の値とずれる。M-1 は保存表を持たず読むたびに `macro_beta` から計算するので、
  同じ入口で写すしかない。
- **源ごとに、当月分が無いときだけ書く**。定常では「月初の最初の夜間」と同じで、その夜に失敗しても
  翌晩に取り直せる。同じ月の 2 回目は書かない（追記のみ・更新も削除もしない）。
- **空の源は書かずに失敗として返す**。`get_producer_scores` は例外を空の dict にするので、
  空を「予測が無い月」として黙って通すと全件失敗が隠れる。社の行があるのに予測の値（μ̂ / gap）が
  全部空のときも同じ——M-1 は当日のマクロ値（`plugins.utils.current_macro_snapshot`）を渡さないと
  r_macro しか返さない（#1005 の初回で 3,839 行が μ̂ 無しで書かれた）。
- **行はローカル正本だけに積む**。接続先がローカルでなければ書かない（本番の断面へ写さない）。

実行（今月分を手で写す。夜間バッチ `nightly_scores.py` も最後に同じ関数を呼ぶ）:
    python -m prediction_snapshots [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date, datetime
from typing import Callable, Optional, Sequence

logger = logging.getLogger("prediction_snapshots")

# 写す源。永続化している全ての mu_source と sector_ols の gap。macro_ensemble（M-4）は退役
# （#570・ADR-0044）で 2026-08 から更新されないので写さない。
MU_SOURCES: tuple = ("macro_enet", "macro_gbdt", "macro_dlm", "macro_risk_return")
GAP_SOURCE = "sector_ols"
SOURCES: tuple = MU_SOURCES + (GAP_SOURCE,)

STATUS_WROTE = "wrote"
STATUS_EXISTS = "exists"        # 当月分が既にある（書かない・正常）
STATUS_EMPTY = "empty"          # 源が空（書かない・失敗）
STATUS_ERROR = "error"          # 読めない・行数が合わない（書かない・失敗）
STATUS_NOT_LOCAL = "not_local"  # 接続先がローカルでない（書かない・正常）
FAILED_STATUSES = (STATUS_EMPTY, STATUS_ERROR)


class SnapshotError(RuntimeError):
    """写したはずの行数が DB と合わない。"""


def jst_today(now: Optional[datetime] = None) -> date:
    """写した日（JST）。夜間バッチは JST 17:20 起動なので UTC の日付とは別の日になりうる。"""
    from collector_utils import JST

    return (now or datetime.now(JST)).astimezone(JST).date()


def _num(value) -> Optional[float]:
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _read_mu(db, source: str) -> list:
    from database import get_producer_asof
    from plugins import get_plugin
    from plugins.utils import current_macro_snapshot

    plugin = get_plugin(source)
    if plugin is None:
        raise ValueError(f"プラグイン '{source}' が見つからない")
    # 当日のマクロ値を渡す（売りランキング・買い推奨と同じ）。渡さないと M-1 は r_macro しか返さず
    # μ̂ が全行で空になる（#1005 の初回で実際に起きた）。
    scores = plugin.read_producer_scores(db, current_macro_snapshot(db)) or {}
    asof = (get_producer_asof(db, source) or {}).get("snapshot_date")
    return [{"edinet_code": ec, "mu": _num(rec.get("mu")), "r1_prime": _num(rec.get("r1_prime")),
             "gap_ratio": None, "data_asof": asof}
            for ec, rec in sorted(scores.items())]


def _read_gap(db) -> list:
    """各社の最新年度の gap_ratio（`sell_ranking` / `recommend` と同じ `latest_year_subq`）。

    同じ年度に通期行が 2 本ある社（決算期変更）は期末の新しい方を採る（主キーを社で一意にする）。
    """
    from database import FinancialMetric, latest_year_subq

    subq = latest_year_subq(db, FinancialMetric)
    q = (db.query(FinancialMetric.edinet_code, FinancialMetric.gap_ratio, FinancialMetric.period_end)
         .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code)
               & (FinancialMetric.year == subq.c.max_year))
         .filter(FinancialMetric.gap_ratio.isnot(None)))
    best: dict = {}
    for ec, gap, period_end in q:
        if ec not in best or (period_end is not None and (best[ec][1] is None or period_end > best[ec][1])):
            best[ec] = (gap, period_end)
    return [{"edinet_code": ec, "mu": None, "r1_prime": None, "gap_ratio": _num(gap),
             "data_asof": period_end.isoformat() if period_end else None}
            for ec, (gap, period_end) in sorted(best.items())]


def value_column(source: str) -> str:
    """その源の予測の値の列（μ̂ の源は `mu`・sector_ols は `gap_ratio`）。"""
    return "gap_ratio" if source == GAP_SOURCE else "mu"


def read_source(db, source: str) -> list:
    """源 1 つの予測を `[{edinet_code, mu, r1_prime, gap_ratio, data_asof}, ...]` で返す（書かない）。"""
    return _read_gap(db) if source == GAP_SOURCE else _read_mu(db, source)


def _has_month(db, source: str, month: str) -> bool:
    from database import PredictionSnapshot

    return db.query(PredictionSnapshot.edinet_code).filter(
        PredictionSnapshot.source == source,
        PredictionSnapshot.snapshot_date.like(f"{month}-%")).first() is not None


def _count(db, source: str, snapshot_date: str) -> int:
    from database import PredictionSnapshot

    return db.query(PredictionSnapshot).filter(
        PredictionSnapshot.source == source,
        PredictionSnapshot.snapshot_date == snapshot_date).count()


def snapshot_month(db, *, today: date, code_version: Optional[str], preprocess_version: Optional[str],
                   sources: Sequence[str] = SOURCES,
                   reader: Optional[Callable] = None,
                   is_local: Optional[bool] = None) -> dict:
    """当月分が無い源だけ写す。戻り値は `{源: (状態, 詳細)}`（状態は `STATUS_*`）。

    1 つの源の失敗で他を止めない。表は書き込み側でも `checkfirst` で作る——`init_db()` は API の
    起動時にしか走らないので、表を足した直後の夜間バッチが「表が無い」で落ちるのを防ぐ（既存の表には
    触らない・前例は `scripts/backfill_adj_factor_events.py`）。
    """
    from database import PredictionSnapshot, _is_local, insert_prediction_snapshots

    if not (_is_local if is_local is None else is_local):
        return {s: (STATUS_NOT_LOCAL, "接続先がローカルではないので写さない") for s in sources}
    reader = read_source if reader is None else reader
    PredictionSnapshot.__table__.create(bind=db.get_bind(), checkfirst=True)
    snapshot_date = today.isoformat()
    month = snapshot_date[:7]
    out: dict = {}
    for source in sources:
        try:
            if _has_month(db, source, month):
                out[source] = (STATUS_EXISTS, f"{month} は写し済み")
                continue
            rows = reader(db, source)
            if not rows:
                out[source] = (STATUS_EMPTY, "源に予測が無い（読み出しの失敗かもしれない）")
                continue
            # 社の行はあるのに予測の値が全部空＝読み方の誤り（M-1 にマクロ値を渡し忘れる等）。書かない
            col = value_column(source)
            if all(r[col] is None for r in rows):
                out[source] = (STATUS_ERROR, f"{len(rows)} 社の行があるのに {col} が全部空（読み方の誤り）")
                continue
            payload = [dict(row, snapshot_date=snapshot_date, source=source,
                            code_version=code_version, preprocess_version=preprocess_version)
                       for row in rows]
            insert_prediction_snapshots(db, payload)
            got = _count(db, source, snapshot_date)
            if got != len(payload):
                raise SnapshotError(f"{len(payload)} 行を書いたはずが DB には {got} 行")
            asof = sorted({r["data_asof"] for r in rows if r["data_asof"]})
            out[source] = (STATUS_WROTE, f"{got} 行（data_asof {asof[0] if asof else '不明'}"
                                         f"{'〜' + asof[-1] if len(asof) > 1 else ''}）")
        except Exception as e:   # noqa: BLE001 — 1 つの源の失敗で他を止めない
            db.rollback()
            out[source] = (STATUS_ERROR, f"{type(e).__name__}: {e}")
    return out


def failed_sources(result: dict) -> list:
    """失敗として数える源（空・読めない・行数が合わない）。"""
    return [s for s, (status, _) in result.items() if status in FAILED_STATUSES]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="本番予測の月次の写し（当月分が無い源だけ写す・#1005）")
    ap.add_argument("--dry-run", action="store_true", help="書かずに源ごとの行数と data_asof だけ出す")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from database import SessionLocal
    from nightly_scores import _code_version
    from plugins.utils import PREPROCESS_VERSION

    with SessionLocal() as db:
        if args.dry_run:
            for source in SOURCES:
                rows = read_source(db, source)
                asof = sorted({r["data_asof"] for r in rows if r["data_asof"]})
                print(f"{source}: {len(rows)} 行 data_asof {asof[:1]}〜{asof[-1:]}")
            return 0
        result = snapshot_month(db, today=jst_today(), code_version=_code_version(),
                                preprocess_version=PREPROCESS_VERSION)
    for source, (status, detail) in result.items():
        print(f"{source}: {status} {detail}")
    return 1 if failed_sources(result) else 0


if __name__ == "__main__":
    sys.exit(main())
