"""nightly_scores.py — 夜間スコア更新バッチ（Issue #432/#443・親 #423）。

`daily-incremental`（差分収集）が成功した夜にだけ producer プラグインを回し、
朝は Render が永続化済みの結果を読むだけにするためのバッチ CLI。
`hyperparameter_search.py` / `macro_beta_inference.py` と同じ「Render では動かせない
heavy を GitHub Actions 上で実行し、本番 Supabase へ直接永続化する」様式。

実行:
    python nightly_scores.py                                   # 既定（NIGHTLY_MODELS 全部）
    python nightly_scores.py --models sector_ols               # 明示・一部だけ
    python nightly_scores.py --models sector_ols,macro_enet

登録済み:
  - `sector_ols`  → `regression_results`（`gap_ratio` の生成元・買い推奨のバランス型プリセット）
  - `macro_enet`  → `macro_enet_scores`（M-6 の μ̂・`sell_ranking` の**既定** mu_source・#402/#443）

M-2（`macro_gbdt`）は載せていない。既定 mu_source ではなく、`tune-hyperparameters.yml` の
`--persist-scores` による月次更新経路が現に生きているため（載せるなら同時に tune 側から外し、
探索 cadence と #291 の品質ゲートの関係を詰める必要がある）。M-4（`macro_ensemble`）は基底を
全部回してコストが合算になるのに M-6 単体を上回らないため当面除外（+0.0006・p=0.810・ADR-0022）。

設計上の約束（触る前に読むこと）:
  - 1モデルの失敗が他モデルを巻き込まない（tune-hyperparameters.yml の
    `fail-fast: false` と同じ思想）。全モデル実行後に、失敗が1件でもあれば非ゼロ終了する
    （→ notify-failure.yml が Issue を起票する・#414）。
  - 「例外が出なかった」を永続化の証明にしない。実行後に DB へ直接クエリし、
    このプロセスの開始時刻より新しい書き込みがあることを確認する（VERIFIERS）。
  - 全モデルを `shared_snapshot_cache()` で包む。`load_data`（週次127万行）・
    `preload_macro`・`build_snapshots` はモデル間で同一のため、包まないとモデルを
    増やすたびに Supabase Egress（5GB/月）が線形に増える（#443）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("nightly_scores")

# 夜間バッチが回す producer の実行順。軽い順に並べる（timeout で打ち切られたとき、
# 先に終わるものだけでも当日分が揃うように）。
NIGHTLY_MODELS: tuple[str, ...] = ("sector_ols", "macro_enet")

# params_schema() の default から変えたいキーだけを書く（残りは coerce_params が補完）。
# sector_ols の regularization=ridge: 既定 features は per-share 10項目で、PL同士・BS同士の
# 比例関係から VIF>10 が頻発する（params_schema の説明どおり）。本番 regression_results の
# 最新行も ridge であり、夜間バッチで ols へ戻すと過去の値と系列が入れ替わる。
#
# macro_enet（M-6）は**エントリを持たない＝params_schema の default をそのまま使う**。
# ADR-0021（昇格ゲート）・ADR-0022（既定 mu_source 切替）の実測はいずれも既定構成
# （use_momentum=False / price_features=[] / min_coverage=0.5 / l1_ratio=auto）で取った値で、
# ここで変えると本番の μ̂ が「評価していない構成」で生成される。M-6 は
# tune-hyperparameters.yml の matrix（M-1/M-2/M-3）に入っておらず tuned params も持たない。
NIGHTLY_PARAMS: dict[str, dict] = {
    "sector_ols": {"regularization": "ridge"},
}

# ── heavy=True の自動実行レジストリ（ADR-0031・Issue #423 子6）────────────────
# `heavy=True` は「Render 軽量モードでブロックする」フラグでしかなく、**誰がいつ回すか**
# は決まっていなかった。そのため heavy を足しても自動実行経路が無いまま放置される事故が
# 繰り返し起きている（sector_ols は自動経路ゼロで gap_ratio が33〜36日前＝#432／M-6 は
# 既定 mu_source なのに tune の matrix に無くローカル手動が唯一の更新経路＝#443／
# factor-premia は GHA 実行履歴ゼロで 37期の重みのまま固着＝#423 子5）。いずれも
# 「壊れた」のではなく「動かなかった」＝failure が出ないので notify-failure（#414）でも
# 検知できない。
#
# そこで **heavy なプラグインはここへ必ず登録する**ことを契約にする。値は3種類:
#   - "local:<スクリプト>"    … ローカルのバッチが回す（#504 で追加）。そのモジュールの
#                               `heavy_models()` にモデル名が現れることまで CI が確かめる
#   - ワークフローファイル名  … その GHA ワークフローが実際にこのモデルを回す
#   - "exempt: <理由>"        … 自動実行しないと決めた場合。理由を必ず書く
#
# `local:` を足したのは #503 で正本がローカル PostgreSQL へ移ったため。GHA はクラウドで
# 走るので正本へ書けず、**定期実行の主体がこちら側へ来た**。語彙が yml しか無かったあいだ、
# レジストリは「登録はあるが cron は止まっている」という嘘をついていた（#504）。
#
# 逸脱は `tests/test_nightly_scores.py::TestHeavyAutomationRegistry` が CI で落とす
# （新しい heavy を足して登録を忘れると赤くなる）。**登録があること ≠ 実際に動いている
# こと**である点に注意——`local:` の場合はさらに**タスクスケジューラへの登録**という
# CI からは見えない一段が挟まる（`scripts/install_*_task.ps1`）。鮮度そのものの監視は
# `/api/morning` の as-of ブロック（#416/#417）と macro-health（#420）が担当し、
# ここが見るのは「経路の有無」だけ。
HEAVY_AUTOMATION: dict[str, str] = {
    # 日次（タスクスケジューラ JST 17:20 → run_nightly.ps1 → nightly_scores.py）
    "sector_ols": "local:scripts/run_nightly.py",
    "macro_enet": "local:scripts/run_nightly.py",
    # 月次（タスクスケジューラ 毎月1日 JST 01:00 → run_monthly.ps1）。μ̂ は月次探索の
    # --persist-scores 副作用で更新される。cadence が探索に縛られている点は #423 子2 の
    # 宿題として残っている（GHA 時代は M-1 が 300分 timeout で cancelled を続けており＝
    # 子7、**登録があっても鮮度は出ていない**実例になっていた）。
    "macro_risk_return": "local:scripts/run_monthly.py",
    "macro_gbdt": "local:scripts/run_monthly.py",
    "macro_dlm": "local:scripts/run_monthly.py",
    # 自動実行しないと決めたもの（理由をここに残す＝「後で対応」を prose に書いて終わらせない）
    "macro_ensemble":
        "exempt: 基底 M-1/M-2/M-6 を内部で全部回すためコストが合算になるのに、"
        "M-6 単体を上回らない（+0.0006・p=0.810・ADR-0022）。既定 mu_source でもない。"
        "#570 で退役（hidden=True・ADR-0044）＝UI からも外れたので回す相手が居ない",
    "macro_gbdt_rank":
        "exempt: producer を持たない（produced_output=False）。スコアが順位で"
        "リターン単位ではないため永続化する μ̂ が無い（#362）。"
        "#570 で退役（hidden=True・ADR-0044）",
}

EXEMPT_PREFIX = "exempt:"
LOCAL_PREFIX = "local:"


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


def _make_score_table_verifier(model_cls_name: str, label: str):
    """`*_scores`（producer μ̂ 用の同型テーブル）向け verifier を作る。

    M-2/M-3/M-4/M-6 の μ̂ テーブルは列構成が同じ（edinet_code / mu / r1_prime /
    snapshot_date / snapshot_date_min / n_stale / created_at）で、いずれも
    `replace_*_scores` による**全置換**で書かれる。したがって「今回の実行で書けたか」は
    max(created_at) だけで判定でき、モデルごとに verifier を手書きする必要がない
    （`NIGHTLY_MODELS` へ足すだけで載る、を verifier 側でも保つ）。

    ログには件数だけでなく as-of（代表値＝中央値・最古・古い銘柄数・Issue #417）も残す。
    μ̂ が「いつの株価断面のものか」は運用上そのまま発注判断の可否に効くため。
    """

    def _verify(db, started_at: datetime) -> str:
        from sqlalchemy import func

        import database

        model_cls = getattr(database, model_cls_name)
        max_created, n_rows, snap, snap_min, n_stale = (
            db.query(
                func.max(model_cls.created_at),
                func.count(model_cls.edinet_code),
                func.max(model_cls.snapshot_date),
                func.min(model_cls.snapshot_date_min),
                func.max(model_cls.n_stale),
            ).one()
        )
        if not n_rows or max_created is None:
            raise VerificationError(
                f"{label} が空です（μ̂ が1件も永続化されていません）"
            )
        max_created = _aware_utc(max_created)
        if max_created < started_at:
            raise VerificationError(
                f"{label} の max(created_at)={max_created.isoformat()} が"
                f" 実行開始 {started_at.isoformat()} より古い＝今回の書き込みが反映されていません"
            )
        return (f"{n_rows}社 / snapshot_date={snap}"
                f"（最古 {snap_min} ・ 代表値より古い銘柄 {n_stale}社） / "
                f"max(created_at)={max_created.isoformat()}")

    return _verify


_verify_macro_enet = _make_score_table_verifier("MacroEnetScore", "macro_enet_scores")


VERIFIERS = {
    "sector_ols": _verify_sector_ols,
    "macro_enet": _verify_macro_enet,
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
    """モデルを順に実行し、1件ごとの結果 dict を返す（例外は握って次のモデルへ進む）。

    全体を `shared_snapshot_cache()` で包む。`load_data`（週次127万行）/`preload_macro`/
    `build_snapshots` はモデル間で結果が同一なので、包まないとモデル数だけ DB から
    再ロードして Supabase Egress（5GB/月）を食う（#443）。ContextVar なので
    `execute_plugin` の `asyncio.to_thread` オフロード先へも伝播する（plugins/__init__.py）。
    キャッシュ対象は**入力**（株価・財務・マクロ）だけで、producer が書く出力テーブルは
    含まないため、モデル間で書き込みが見えなくなることはない。
    """
    from plugins.macro_snapshots import shared_snapshot_cache

    with shared_snapshot_cache():
        return await _run_models_inner(models, db)


async def _run_models_inner(models: list[str], db) -> list[dict]:
    """run_models の本体（キャッシュコンテキストの中で呼ばれる前提）。"""
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
