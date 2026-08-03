"""nightly_scores.py — 夜間スコア更新バッチ（Issue #432・親 #423）。

`daily-incremental`（差分収集）が成功した夜にだけ producer プラグインを回し、
朝は Render が永続化済みの結果を読むだけにするためのバッチ CLI。
`hyperparameter_search.py` / `macro_beta_inference.py` と同じ「Render では動かせない
heavy を GitHub Actions 上で実行し、本番 Supabase へ直接永続化する」様式。

実行:
    python nightly_scores.py                        # 既定（sector_ols）
    python nightly_scores.py --models sector_ols    # 明示

いま登録しているのは `sector_ols`（`regression_results.gap_ratio` の生成元）のみ。
M-6 / M-2 の日次化は #423 の子2 で追加する（`NIGHTLY_MODELS` へ足すだけで載る）。

設計上の約束（触る前に読むこと）:
  - 1モデルの失敗が他モデルを巻き込まない（tune-hyperparameters.yml の
    `fail-fast: false` と同じ思想）。全モデル実行後に、失敗が1件でもあれば非ゼロ終了する
    （→ notify-failure.yml が Issue を起票する・#414）。
  - 「例外が出なかった」を永続化の証明にしない。実行後に DB へ直接クエリし、
    このプロセスの開始時刻より新しい書き込みがあることを確認する（VERIFIERS）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("nightly_scores")

# 夜間バッチが回す producer の実行順。
NIGHTLY_MODELS: tuple[str, ...] = ("sector_ols",)

# params_schema() の default から変えたいキーだけを書く（残りは coerce_params が補完）。
# sector_ols の regularization=ridge: 既定 features は per-share 10項目で、PL同士・BS同士の
# 比例関係から VIF>10 が頻発する（params_schema の説明どおり）。本番 regression_results の
# 最新行も ridge であり、夜間バッチで ols へ戻すと過去の値と系列が入れ替わる。
NIGHTLY_PARAMS: dict[str, dict] = {
    "sector_ols": {"regularization": "ridge"},
}


class VerificationError(RuntimeError):
    """execute は成功したが、DB への永続化を確認できなかった。"""


def _aware_utc(dt: datetime) -> datetime:
    """DB ドライバによっては tz-naive で返るため UTC とみなして揃える。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _verify_sector_ols(db, started_at: datetime) -> str:
    """regression_results が今回の実行で更新されたことを直接クエリで確認する。"""
    from sqlalchemy import func

    from database import RegressionResult

    max_computed, n_gap = (
        db.query(func.max(RegressionResult.computed_at),
                 func.count(RegressionResult.gap_ratio))
        .one()
    )
    if max_computed is None:
        raise VerificationError("regression_results が空です（永続化されていません）")
    max_computed = _aware_utc(max_computed)
    if max_computed < started_at:
        raise VerificationError(
            f"regression_results の max(computed_at)={max_computed.isoformat()} が"
            f" 実行開始 {started_at.isoformat()} より古い＝今回の書き込みが反映されていません"
        )
    return (f"max(computed_at)={max_computed.isoformat()} / "
            f"gap_ratio 非NULL {n_gap}件")


VERIFIERS = {
    "sector_ols": _verify_sector_ols,
}


def _summarize(result: dict) -> str:
    """execute の戻り値をログ1行へ畳む。

    スカラーはそのまま、短い文字列リスト（`features_used` 等）は中身を出す。
    実際に採用された説明変数は運用上の必須情報で、`sector_ols` の
    `_select_features` は欠損の多い列を**黙って**自動ドロップするため、
    ログに残らないと「なぜ対象社数が減ったか」を後から追えない。
    巨大配列（`results` / `sector_stats`）は件数だけにする。
    """
    parts: list[str] = []
    for k, v in result.items():
        if isinstance(v, (int, float, str, bool)):
            parts.append(f"{k}={v}")
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v) and len(v) <= 20:
            parts.append(f"{k}=[{','.join(v)}]")
        elif isinstance(v, list):
            parts.append(f"{k}(n={len(v)})")
    return ", ".join(parts) or "(要約できる項目なし)"


async def run_models(models: list[str], db) -> list[dict]:
    """モデルを順に実行し、1件ごとの結果 dict を返す（例外は握って次のモデルへ進む）。"""
    from plugins import execute_plugin, get_plugin

    entries: list[dict] = []
    for name in models:
        started_at = datetime.now(timezone.utc)
        t0 = time.time()
        entry: dict = {"model": name, "ok": False, "summary": None,
                       "verified": None, "error": None}
        try:
            plugin = get_plugin(name)
            if plugin is None:
                raise ValueError(f"プラグイン '{name}' が見つかりません")
            logger.info("[%s] 実行開始（params=%s）", name, NIGHTLY_PARAMS.get(name, {}))
            result = await execute_plugin(plugin, dict(NIGHTLY_PARAMS.get(name, {})), db)
            entry["summary"] = _summarize(result)
            verify = VERIFIERS.get(name)
            if verify is not None:
                entry["verified"] = verify(db, started_at)
            entry["ok"] = True
            logger.info("[%s] 完了: %s", name, entry["summary"])
            if entry["verified"]:
                logger.info("[%s] 永続化を確認: %s", name, entry["verified"])
        except Exception as e:   # noqa: BLE001 — 1モデルの失敗で他を止めない
            entry["error"] = f"{type(e).__name__}: {e}"
            logger.exception("[%s] 失敗: %s", name, entry["error"])
        entry["elapsed_min"] = round((time.time() - t0) / 60, 1)
        entries.append(entry)
    return entries


async def _run(args: argparse.Namespace) -> None:
    from database import SessionLocal, _is_local

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        raise SystemExit("--models が空です")

    logger.info("夜間スコア更新 開始: models=%s / 接続先=%s",
                models, "ローカル" if _is_local else "本番（リモート）")
    t0 = time.time()
    db = SessionLocal()
    try:
        entries = await run_models(models, db)
    finally:
        db.close()

    logger.info("=" * 60)
    for e in entries:
        status = "OK" if e["ok"] else "FAILED"
        logger.info("%-8s %-14s %5.1f分  %s", status, e["model"], e["elapsed_min"],
                    e["verified"] or e["error"] or e["summary"] or "")
    logger.info("総所要時間: %.1f分", (time.time() - t0) / 60)
    logger.info("=" * 60)

    failed = [e["model"] for e in entries if not e["ok"]]
    if failed:
        raise SystemExit(f"失敗したモデル: {', '.join(failed)}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="夜間スコア更新バッチ（Issue #432・親 #423）"
    )
    ap.add_argument("--models", default=",".join(NIGHTLY_MODELS),
                    help=f"実行する producer をカンマ区切りで指定（既定: {','.join(NIGHTLY_MODELS)}）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
