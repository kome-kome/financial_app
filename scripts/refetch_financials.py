"""scripts/refetch_financials.py — 書類を取り直して financial_records の既存値を上書きする（#870）。

#852 で直したのは「これから取る書類の読み方」だけで、DB の既存の行は誤ったまま残る
（例: トヨタ 2026年度の売上高が連結 50.68兆円ではなく単体 18.26兆円）。既存の補完経路
（`collector.py --refill-pl-bs` 等・共通骨格 `_refill_records_from_xbrl`）は **NULL の列だけ**を
埋めるので、「値は入っているが違う」誤りは1行も直らない。生タグは DB に無い
（`xbrl_raw_documents` は0行）ので、`doc_id` から書類を取り直すしかない。

実行:
    python -m scripts.refetch_financials --edinet-code E02144 E00317    # 試運転（既定・DB へ書かない）
    python -m scripts.refetch_financials --apply                        # 全件を続きから書く（日中キューの形）

**既定は試運転**。DB へ1行も書かず（進捗も保存しない）、変わる行数・列ごとの件数・代表例
（旧値 -> 新値）を出す。`--apply` のときだけ書く。全件の書き込みは #858 で人が試運転の
差分を読んでから日中キュー（`run_daytime.JOBS["refetch:financials"]`）へ積む。

書き込み規則:
- 読み方は通常の収集と同じ `parse_xbrl_csv` -> `calc_derived`（分岐を写さない）。期末は DB の行が
  持つものを使う。`calc_derived` が pl/cf に入れて永続化する派生額（`cf_free_cf`・`pl_ebitda`・
  `pl_nonoperating_income`）も同じ経路で直る。
- 書類から読めた列（None でない値）だけを置き換える。取れなかった列は消さない（NULL で上書きしない）。
- 値が同じ列は触らず、変化に数えない。
- 触るのは `database.financial_columns()` が返す列と、DEI から読んだ `accounting_standard`（#859）だけ
  ＝行のキー（edinet_code・year・period_end・period_type）・会社名・業種・doc_id・source・市場データ
  （株価・時価総額・PER・PBR・配当利回り）は触らない。
- `cf_free_cf` は営業CFと投資CFが両方読めたときだけ。`calc_derived` は欠けた入力を 0 とみなして必ず
  `free_cf` を作るので、そのまま使うと CF の読めない書類で既存値を 0 や営業CFだけの値で潰す。
- 書類の DEI（書類が名乗る当期末日）が行の `period_end` と違う・読めないときは書かない（期末不一致）。
  上書きは既存の値を消す操作なので、別の期の書類で上書きする事故を止める。
- PER・PBR・時価総額は、評価額の入力（`VALUATION_INPUTS`）から計算して保存した値なので、入力が
  変わると過去の行では古くなる（夜間が直すのは各社の最新行だけ）。ここでは再計算せず、入力が
  変わった社を一覧に出すだけにする（再計算は株価の列まで書き換え、試運転の差分に出ない変化を生む）。

再開と締切:
- `--apply` かつ絞り込み無し（`--limit` は可）のときだけ、`app_settings.refetch_financials_cursor` に
  「最後に失敗しなかった行の id」をデータと同じ commit で保存し、次の実行はその次の id から始める。
  試運転と絞り込んだ実行（サンプル確認）は進捗を読まず書かない。`--restart` で最初から。
- 親バッチの締切（`FINAPP_STEP_DEADLINE_UTC`・`hyperparameter_search.resolve_deadline()`）の手前で
  新しい書類を取りに行くのをやめ、確定して exit 0 で終わる（ADR-0054 と同じ形・1件目は必ず処理する）。

失敗の数え方: 取得の失敗（`fetch_xbrl_csv` が None）と読み取りの失敗（bs/pl/cf が全部空）を別々に
数えて doc_id を出す。処理した全件が失敗したら exit 1（「走ったが全部失敗した」を成功に見せない）。
`EDINET_MAX_CONSECUTIVE_FAILURES` 件続けて失敗したら API キー切れ・通信障害とみなして止め exit 1
（進捗は最後に成功した行までしか進めない＝次の実行で失敗した区間を取り直す）。

ローカル正本専用（ADR-0038）。接続先が local でなければ `SystemExit`。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import httpx

import database as D
from collector_financials import (
    _DEI_PEND, _extract_dei, accounting_standard_from_dei, calc_derived, fetch_xbrl_csv,
    parse_xbrl_csv,
)
from collector_utils import EDINET_MAX_CONSECUTIVE_FAILURES, RATE_SLEEP
from database import FinancialRecord, financial_columns, get_setting, upsert_setting

# 進捗（再開位置）の置き場。既存の app_settings を使う＝起動時に必ず走る init_db() へ DDL を足さない。
CURSOR_KEY = "refetch_financials_cursor"

# 1回に読む行数（数万行を一度にメモリへ載せない）と、--apply で commit する間隔（行数）。
CHUNK_ROWS = 500
COMMIT_EVERY = 50

# 列ごとに出す代表例の件数、一覧で出す失敗 doc_id の上限。
EXAMPLES_PER_COLUMN = 3   # 既定。--examples で増やせる
FAILED_IDS_SHOWN = 50

# 締切の判定（scripts/grid_macro_beta.py の fits_before と同じ形）。これまでで最も時間のかかった
# 1件 × 安全率 ＋ 余白 が締切までに入らなければ、新しい書類を取りに行かない。
DEADLINE_SAFETY = 1.25
DEADLINE_MARGIN_MIN = 2.0

# PER・PBR・時価総額・配当利回りの入力（collector_prices._compute_market_values の引数）。
VALUATION_INPUTS = ("pl_eps", "bs_bps", "issued_shares", "bs_total_equity", "dps")

EXIT_FAILED = 1

FAILURES = ("fetch_failed", "parse_failed")

Fetch = Callable[[str], Awaitable[object]]


def guard_local_target() -> None:
    """ローカル正本以外へは繋がない（ADR-0038: Supabase の Postgres へ書き戻す経路は作らない）。"""
    if D.DB_TARGET != "local" or not D._is_local:
        raise SystemExit(
            f"接続先が local ではありません（FINAPP_DB_TARGET={D.DB_TARGET!r} / "
            f"is_local={D._is_local}）。このスクリプトはローカル正本専用です。"
        )


# ── 差分（DB に触らない）────────────────────────────────────────────────────

def document_columns(df, edinet_code: str, period_end: str) -> Optional[dict]:
    """取り直した書類から、上書きの候補になる {列名: 値} を返す（None の値は含めない）。

    読み取りに失敗した（bs/pl/cf が全部空）ときは None。
    """
    parsed = parse_xbrl_csv(df, edinet_code, period_end)
    if not any(parsed.get(cat) for cat in ("bs", "pl", "cf")):
        return None
    cf = parsed.get("cf", {})
    # calc_derived は cf を書き換える（free_cf を足す）ので、入力の有無は呼ぶ前に見る。
    has_fcf_inputs = cf.get("operating_cf") is not None and cf.get("investing_cf") is not None
    rec = calc_derived(parsed)
    if not has_fcf_inputs:
        rec["cf"].pop("free_cf", None)
    return {col: v for col, v in financial_columns(rec).items() if v is not None}


def diff_columns(row, values: dict) -> list[tuple[str, object, object]]:
    """書類の値と行の値が違う列だけを (列名, 旧値, 新値) で返す。同じ値は変化に数えない。"""
    return [(col, getattr(row, col), new) for col, new in values.items()
            if getattr(row, col) != new]


# ── 本体 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Filters:
    """対象の絞り込み（試運転とサンプル確認に使う）。どれか1つでも指定すると進捗を読まず書かない。"""
    edinet_codes: tuple[str, ...] = ()
    doc_ids: tuple[str, ...] = ()
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    period_type: Optional[str] = None

    @property
    def narrowed(self) -> bool:
        return bool(self.edinet_codes or self.doc_ids or self.period_type
                    or self.year_from is not None or self.year_to is not None)

    def describe(self) -> str:
        parts = []
        if self.edinet_codes:
            parts.append("edinet_code=" + ",".join(self.edinet_codes))
        if self.doc_ids:
            parts.append("doc_id=" + ",".join(self.doc_ids))
        if self.year_from is not None or self.year_to is not None:
            parts.append(f"year={self.year_from or ''}..{self.year_to or ''}")
        if self.period_type:
            parts.append(f"period_type={self.period_type}")
        return " ".join(parts) or "なし（全件）"


@dataclass
class Report:
    apply: bool
    filters: Filters
    use_cursor: bool
    start_after: int = 0
    targets: int = 0
    processed: int = 0
    changed_rows: int = 0
    unchanged_rows: int = 0
    column_counts: dict = field(default_factory=dict)
    examples: dict = field(default_factory=dict)
    examples_per_column: int = EXAMPLES_PER_COLUMN
    fetch_failed: list = field(default_factory=list)
    parse_failed: list = field(default_factory=list)
    period_mismatch: list = field(default_factory=list)
    valuation_changed: set = field(default_factory=set)
    stopped_by_deadline: bool = False
    aborted_consecutive: bool = False
    cursor: Optional[int] = None
    remaining: int = 0

    @property
    def failed(self) -> int:
        return len(self.fetch_failed) + len(self.parse_failed)


def _target_query(db, filters: Filters):
    q = db.query(FinancialRecord).filter(FinancialRecord.doc_id.isnot(None))
    if filters.edinet_codes:
        q = q.filter(FinancialRecord.edinet_code.in_(filters.edinet_codes))
    if filters.doc_ids:
        q = q.filter(FinancialRecord.doc_id.in_(filters.doc_ids))
    if filters.year_from is not None:
        q = q.filter(FinancialRecord.year >= filters.year_from)
    if filters.year_to is not None:
        q = q.filter(FinancialRecord.year <= filters.year_to)
    if filters.period_type:
        q = q.filter(FinancialRecord.period_type == filters.period_type)
    return q


def fits_before(deadline: Optional[datetime], now: datetime, est_sec: float) -> bool:
    """次の1件（安全率と余白込み）が締切までに収まるか。締切が無ければ常に True。"""
    if deadline is None:
        return True
    need = timedelta(seconds=est_sec * DEADLINE_SAFETY, minutes=DEADLINE_MARGIN_MIN)
    return now + need <= deadline


def _period_str(pe) -> str:
    return pe.isoformat() if hasattr(pe, "isoformat") else (str(pe) if pe else "")


async def _process_row(row, fetch: Fetch, apply: bool, report: Report, log) -> str:
    """1行ぶん: 取り直し -> 読み取り -> 期末照合 -> 差分（--apply なら書き換え）。結果の種別を返す。"""
    pe = _period_str(row.period_end)
    try:
        df = await fetch(row.doc_id)
    except Exception as e:  # fetch_xbrl_csv は握って None を返す。差し替えた取得関数の例外もここで数える
        log(f"  取得失敗 {row.edinet_code} {row.doc_id}: {type(e).__name__}")
        df = None
    if df is None or getattr(df, "empty", True):
        report.fetch_failed.append(row.doc_id)
        return "fetch_failed"

    try:
        values = document_columns(df, row.edinet_code, pe)
    except Exception as e:  # 1書類の読み取りの不具合で、数日がかりの実行を止めない
        log(f"  読み取り失敗 {row.edinet_code} {row.doc_id}: {type(e).__name__}: {e}")
        values = None
    if values is None:
        report.parse_failed.append(row.doc_id)
        return "parse_failed"

    dei = _extract_dei(df)
    doc_pe = (dei.get(_DEI_PEND) or "")[:10]
    if doc_pe != pe:
        report.period_mismatch.append((row.edinet_code, row.doc_id, pe, doc_pe or None))
        return "period_mismatch"
    # 会計基準も書類（DEI）由来。読めたときだけ候補にする（#859・既存行は #858 の取り直しで埋まる）
    std = accounting_standard_from_dei(dei, row.doc_id)
    if std is not None:
        values["accounting_standard"] = std

    changes = diff_columns(row, values)
    if not changes:
        report.unchanged_rows += 1
        return "unchanged"
    report.changed_rows += 1
    for col, old, new in changes:
        report.column_counts[col] = report.column_counts.get(col, 0) + 1
        ex = report.examples.setdefault(col, [])
        if len(ex) < report.examples_per_column:
            ex.append((row.edinet_code, row.year, row.period_type, pe, old, new))
        if col in VALUATION_INPUTS:
            report.valuation_changed.add(row.edinet_code)
        if apply:
            setattr(row, col, new)
    return "changed"


async def refetch(db, *, apply: bool = False, filters: Filters = Filters(),
                  limit: Optional[int] = None, restart: bool = False,
                  sleep_sec: float = RATE_SLEEP, deadline: Optional[datetime] = None,
                  fetch: Optional[Fetch] = None,
                  now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                  commit_every: int = COMMIT_EVERY,
                  examples_per_column: int = EXAMPLES_PER_COLUMN, log=print) -> Report:
    """対象の行を id の昇順に取り直す。試運転（apply=False）は何も書かず最後に rollback する。

    `fetch(doc_id)` と `now()` はテストで差し替える（本物の EDINET と時計に触れない）。
    """
    use_cursor = apply and not filters.narrowed
    start_after = 0
    if use_cursor and not restart:
        start_after = int(get_setting(db, CURSOR_KEY) or 0)
    report = Report(apply=apply, filters=filters, use_cursor=use_cursor, start_after=start_after,
                    examples_per_column=examples_per_column)

    base = _target_query(db, filters)
    report.targets = base.filter(FinancialRecord.id > start_after).count()
    if limit is not None:
        report.targets = min(report.targets, limit)
    log(f"[refetch] mode={'apply' if apply else 'dry-run'} 対象 {report.targets}行"
        f"（絞り込み: {filters.describe()}・開始 id>{start_after}"
        f"{'・進捗を保存' if use_cursor else '・進捗は読まず書かない'}）")

    last_id = start_after       # 最後に処理した行
    safe_cursor = start_after   # 最後に失敗しなかった行（進捗として保存する値）
    streak = 0
    slowest_sec = 0.0
    since_commit = 0

    def _commit() -> None:
        if use_cursor:
            upsert_setting(db, CURSOR_KEY, str(safe_cursor))  # 書き換えた行と同じ commit で確定する
            report.cursor = safe_cursor
        else:
            db.commit()

    async with contextlib.AsyncExitStack() as stack:
        if fetch is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=60))
            fetch = lambda doc_id: fetch_xbrl_csv(client, doc_id)  # noqa: E731

        done = False
        while not done:
            rows = (base.filter(FinancialRecord.id > last_id)
                    .order_by(FinancialRecord.id).limit(CHUNK_ROWS).all())
            if not rows:
                break
            for row in rows:
                if limit is not None and report.processed >= limit:
                    done = True
                    break
                # 1件目は締切を見ずに必ず処理する（ADR-0054。見積りが外れても何かは進む）
                if report.processed and not fits_before(deadline, now(), slowest_sec):
                    report.stopped_by_deadline = True
                    done = True
                    break

                t0 = time.monotonic()
                outcome = await _process_row(row, fetch, apply, report, log)
                await asyncio.sleep(sleep_sec)
                slowest_sec = max(slowest_sec, time.monotonic() - t0)
                last_id = row.id
                report.processed += 1

                if outcome in FAILURES:
                    streak += 1
                else:
                    streak = 0
                    safe_cursor = row.id
                if streak >= EDINET_MAX_CONSECUTIVE_FAILURES:
                    report.aborted_consecutive = True
                    done = True
                    break

                if report.processed % COMMIT_EVERY == 0:
                    log(f"[refetch {report.processed}/{report.targets}] 変化 {report.changed_rows}行・"
                        f"失敗 {report.failed}・期末不一致 {len(report.period_mismatch)}")
                if apply:
                    since_commit += 1
                    if since_commit >= commit_every:
                        _commit()
                        since_commit = 0

    if apply:
        _commit()
    else:
        db.rollback()

    report.remaining = base.filter(
        FinancialRecord.id > (safe_cursor if use_cursor else last_id)).count()
    return report


# ── 出力 ──────────────────────────────────────────────────────────────────

def _fmt(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float) and v.is_integer():
        return f"{v:,.0f}"
    return repr(v)


def format_report(r: Report) -> str:
    """結果の要約。cp932 の標準出力でも落ちないよう、記号は ASCII だけを使う。"""
    mode = "apply（書き込み）" if r.apply else "dry-run（試運転・DB へは書いていない）"
    lines = [
        "=== refetch_financials 結果 ===",
        f"モード: {mode}",
        f"絞り込み: {r.filters.describe()}",
        f"処理 {r.processed}/{r.targets}行: 変化 {r.changed_rows} / 変化なし {r.unchanged_rows} / "
        f"期末不一致 {len(r.period_mismatch)} / 取得失敗 {len(r.fetch_failed)} / "
        f"読み取り失敗 {len(r.parse_failed)}",
    ]
    if r.changed_rows == 0:
        lines.append("変化は0件でした（0件が続くなら、判定が効いていない可能性を疑ってください）")
    else:
        lines.append("列ごとの変化件数:")
        for col, n in sorted(r.column_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {col:<28} {n}")
        lines.append(f"代表例（列ごとに最大{r.examples_per_column}件・旧値 -> 新値）:")
        for col in sorted(r.examples, key=lambda c: (-r.column_counts[c], c)):
            for ec, year, pt, pe, old, new in r.examples[col]:
                lines.append(f"  {col:<28} {ec} {year} {pt} {pe}  {_fmt(old)} -> {_fmt(new)}")
    if r.period_mismatch:
        lines.append("期末不一致（書かなかった・行の期末 / 書類の期末）:")
        for ec, doc_id, pe, doc_pe in r.period_mismatch[:FAILED_IDS_SHOWN]:
            lines.append(f"  {ec} {doc_id} {pe} / {doc_pe or 'DEI なし'}")
    for label, ids in (("取得失敗", r.fetch_failed), ("読み取り失敗", r.parse_failed)):
        if ids:
            more = f" ほか{len(ids) - FAILED_IDS_SHOWN}件" if len(ids) > FAILED_IDS_SHOWN else ""
            lines.append(f"{label}の doc_id: {' '.join(ids[:FAILED_IDS_SHOWN])}{more}")
    if r.valuation_changed:
        lines.append("評価額の入力（" + "・".join(VALUATION_INPUTS) + "）が変わった社: "
                     + " ".join(sorted(r.valuation_changed)))
        lines.append("  これらの社の過去の行の PER・PBR・時価総額は古いまま。再計算は "
                     "update_market_data_from_history(db, point_in_time=True, only=[...])")
    if r.aborted_consecutive:
        lines.append(f"連続 {EDINET_MAX_CONSECUTIVE_FAILURES} 件の失敗で停止した（API キー・通信を確かめる）。"
                     "進捗は最後に成功した行までしか進めていない")
    if r.stopped_by_deadline:
        lines.append("締切の手前で停止した（次の書類が締切までに入らない）。次の実行で続きから始まる"
                     if r.use_cursor else "締切の手前で停止した")
    if r.use_cursor:
        lines.append(f"進捗: id {r.cursor} まで確定・残り {r.remaining}行"
                     + ("（全件処理済み。やり直すなら --restart）" if r.remaining == 0 else ""))
    else:
        lines.append(f"未処理: {r.remaining}行")
    return "\n".join(lines)


def exit_code(r: Report) -> int:
    """全件失敗・連続失敗での停止は失敗（exit 1）。締切での停止は正常（exit 0）。"""
    if r.aborted_consecutive or (r.processed and r.failed == r.processed):
        return EXIT_FAILED
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────

def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="python -m scripts.refetch_financials",
        description="書類を取り直して financial_records の既存値を上書きする（#870）。既定は試運転。")
    p.add_argument("--apply", action="store_true", help="DB へ書く（無ければ試運転＝何も書かない）")
    p.add_argument("--edinet-code", nargs="+", default=[], help="対象の社（EDINET コード）")
    p.add_argument("--doc-id", nargs="+", default=[], help="対象の書類（doc_id）")
    p.add_argument("--year-from", type=int, default=None, help="対象の年度の下限（含む）")
    p.add_argument("--year-to", type=int, default=None, help="対象の年度の上限（含む）")
    p.add_argument("--period-type", choices=("annual", "H1"), default=None, help="期種で絞る")
    p.add_argument("--limit", type=int, default=None, help="処理する行数の上限（進捗は保存する）")
    p.add_argument("--restart", action="store_true", help="保存した進捗を無視して最初から（--apply 用）")
    p.add_argument("--sleep", type=float, default=RATE_SLEEP, help="EDINET リクエストの間隔（秒）")
    p.add_argument("--examples", type=int, default=EXAMPLES_PER_COLUMN,
                   help=f"列ごとに出す代表例の件数（既定 {EXAMPLES_PER_COLUMN}）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    guard_local_target()
    from hyperparameter_search import resolve_deadline
    deadline = resolve_deadline()
    if deadline is not None:
        print(f"deadline: {deadline.isoformat()}（次の書類が入らなければ新しく取りに行かない）", flush=True)
    filters = Filters(
        edinet_codes=tuple(args.edinet_code), doc_ids=tuple(args.doc_id),
        year_from=args.year_from, year_to=args.year_to, period_type=args.period_type,
    )
    db = D.SessionLocal()
    try:
        report = asyncio.run(refetch(
            db, apply=args.apply, filters=filters, limit=args.limit, restart=args.restart,
            sleep_sec=args.sleep, deadline=deadline, examples_per_column=args.examples,
            log=lambda m: print(m, flush=True),
        ))
    finally:
        db.close()
    print(format_report(report), flush=True)
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
