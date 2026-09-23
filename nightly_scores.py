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
  - **計算した診断値を捨てない**（#726・ADR-0061）。選ばれた α・OOF 成績・業種別の統計を
    `nightly_model_diagnostics` へ1モデル1行で積む。μ̂ の永続化と検証が済んだ後に書き、
    書けたかを直接クエリで確かめ、書けなければ非ゼロ終了する（μ̂ は巻き戻さない）。
    入れるのは `DIAG_EXTRACTORS` が許可したキーだけ——結果を丸ごと入れない（社別の行・社名）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger("nightly_scores")

ROOT = Path(__file__).resolve().parent

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
# ここで変えると本番の μ̂ が「評価していない構成」で生成される。**`use_momentum=False` は
# 惰性ではなく実測の結論**——ADR-0045 で M-2/M-6 の ON/OFF を honest OOF で比較し、共通
# (ym,ec) 域では4検定すべて補正後 α を通らず符号も負だった（`python -m scripts.momentum_gate`）。
# M-6 は
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
    # M-1 だけ別タスク（毎月3日 JST 01:00 → run_monthly_m1.ps1・#579 で2日から移動）。
    # 2日は macro_beta（M-1 の入力）が使う。実測 約752分で月次本体の窓に入らないため
    # 切り出した（#584）。**切り出しの判断は #638 以後も変わらない**——畳めば結果は残るが、
    # 残るのは探索空間の一部を見た結果であって、毎月それでよいわけではない。
    "macro_risk_return": "local:scripts/run_monthly_m1.py",
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


# ── 診断値の記録（#726・ADR-0061）──────────────────────────────────────────
# 夜間の producer が毎晩計算しているのに捨てていた値（選ばれた正則化の強さ・OOF 成績・業種別の統計）を
# `nightly_model_diagnostics` へ積む。読むのは `python -m scripts.nightly_diag_report` だけで、
# 本番のスコアも起票もこれを読まない（警報の基準は実測が溜まってから決める）。
#
# **抽出はモデルごとの allowlist**。結果の dict を丸ごと入れると `results`（社別の行・社名）まで
# 入り、表が肥大するうえ、何が入っているかを誰も説明できなくなる（リポジトリは public）。
# `NIGHTLY_MODELS` を増やしたらここへ1行足す——忘れても失敗として現れないので
# `tests/test_nightly_scores.py::TestDiagnosticsRegistry` が照合する。
#
# 書き込みの失敗は `batch_freshness.PRODUCERS` では見ない。その場で直接クエリして確かめ、
# 書けていなければ非ゼロ終了する（sector_ols を PRODUCERS の対象外にしたのと同じ理由＝
# 2経路で見ると同じ事実に Issue が二重に立つ）。

# OOF 成績のうちスカラーで持つもの（`plugins.macro_snapshots.oof_backtest` の戻りのキー）。
_OOF_SCALAR_KEYS: tuple[str, ...] = (
    "n_periods", "n_periods_quantile", "n_oof_samples",
    "long_short_spread", "hit_rate", "short_side_spread", "short_side_hit_rate",
    "effective_turnover", "annual_turnover",
    "interval_coverage", "interval_tau", "n_interval_calib",
)
# OOF 成績のうち小さな dict / 系列で持つもの（期別の系列は「どの期が動いたか」を読むのに要る）。
_OOF_NESTED_KEYS: tuple[str, ...] = (
    "rank_ic", "rank_ic_industry_neutral", "monotonicity", "quantile_returns",
    "rank_ic_by_period", "short_side_spread_by_period",
)


def _json_safe(obj):
    """JSON 列へ入れられる形へ揃える。

    PostgreSQL の JSON は NaN / Infinity を受け付けない（書き込みが例外になる）ので None にする。
    numpy のスカラーは Python の数へ、tuple は list へ、dict のキーは文字列へ。
    """
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (bool, str)) or obj is None:
        return obj
    kind = getattr(getattr(obj, "dtype", None), "kind", "")
    if kind == "b":
        return bool(obj)
    if isinstance(obj, int) or kind in ("i", "u"):
        return int(obj)
    try:
        f = float(obj)
    except (TypeError, ValueError):
        return str(obj)
    return f if math.isfinite(f) else None


def ridge_alpha_edge(alpha) -> str | None:
    """ridge の α が候補（`plugins.utils.RIDGE_ALPHAS`）の端なら "low" / "high"、内側なら None。"""
    from plugins.utils import RIDGE_ALPHAS
    if alpha is None:
        return None
    a = float(alpha)
    if math.isclose(a, min(RIDGE_ALPHAS), rel_tol=1e-9):
        return "low"
    if math.isclose(a, max(RIDGE_ALPHAS), rel_tol=1e-9):
        return "high"
    return None


def _extract_sector_ols(result: dict) -> dict:
    from plugins.utils import RIDGE_ALPHAS
    sectors = []
    for s in result.get("sector_stats") or []:
        is_ridge = s.get("method") == "ridge"
        sectors.append({
            "industry":   s.get("industry"),
            "n":          s.get("n"),
            "r2":         s.get("r2"),
            "adj_r2":     s.get("adj_r2"),
            "method":     s.get("method"),
            "alpha":      s.get("alpha") if is_ridge else None,
            "alpha_edge": ridge_alpha_edge(s.get("alpha")) if is_ridge else None,
            "n_high_vif": len((s.get("collinearity_warnings") or {}).get("high_vif") or []),
        })
    return {
        "ridge_alphas":      list(RIDGE_ALPHAS),
        "features_used":     list(result.get("features_used") or []),
        "dropped_features":  list(result.get("dropped_features") or []),
        "n_sectors":         result.get("n_sectors"),
        "n_total":           result.get("n_total"),
        "n_skipped_sectors": result.get("n_skipped_sectors"),
        "n_shrunk_sectors":  result.get("n_shrunk_sectors"),
        "shrink_threshold":  result.get("shrink_threshold"),
        "n_alpha_at_low_edge":  sum(1 for s in sectors if s["alpha_edge"] == "low"),
        "n_alpha_at_high_edge": sum(1 for s in sectors if s["alpha_edge"] == "high"),
        "sectors":           sectors,
    }


def _extract_macro_enet(result: dict) -> dict:
    oof = result.get("oof_backtest") or {}
    final = dict(result.get("final_model") or {})
    grid = final.get("l1_ratio_grid") or []
    if grid and final.get("l1_ratio") is not None:
        final["l1_ratio_at_grid_edge"] = any(
            math.isclose(final["l1_ratio"], g, abs_tol=1e-4) for g in (min(grid), max(grid)))
    return {
        "n_train_samples":   result.get("n_train_samples"),
        "n_companies":       result.get("n_companies"),
        "selected_features": list(result.get("selected_features") or []),
        "feature_coefs":     dict(result.get("feature_coefs") or {}),
        "final_model":       final,
        # `coef` は最終 fold の係数ベクトルで、最終モデルの `feature_coefs` と重複する
        "cv_diagnostics":    {k: v for k, v in (result.get("cv_diagnostics") or {}).items()
                              if k != "coef"},
        "cv_metrics":        (result.get("cv_metrics") or {}).get("enet"),
        "oof": {k: oof.get(k) for k in _OOF_SCALAR_KEYS + _OOF_NESTED_KEYS if k in oof},
    }


DIAG_EXTRACTORS: dict[str, Callable[[dict], dict]] = {
    "sector_ols": _extract_sector_ols,
    "macro_enet": _extract_macro_enet,
}


def _edge_warnings(model: str, diag: dict) -> list[str]:
    """選ばれた α が候補の端に張り付いた箇所（WARN ログ用・起票はしない）。"""
    out: list[str] = []
    if model == "sector_ols":
        for s in diag.get("sectors") or []:
            if s.get("alpha_edge"):
                out.append(f"{s['industry']}: alpha={s['alpha']} ({s['alpha_edge']} edge, n={s['n']})")
    elif model == "macro_enet":
        fm = diag.get("final_model") or {}
        for key, side in (("alpha_at_path_min", "low"), ("alpha_at_path_max", "high")):
            if fm.get(key):
                out.append(f"final model: alpha={fm.get('alpha')} ({side} edge of the alpha path)")
    return out


def _code_version(run=subprocess.run) -> str:
    """作業ツリーの HEAD と未コミット変更の有無（"<sha>" / "<sha>+dirty" / "unknown"）。

    夜間バッチは作業ツリーをそのまま import するので、値が動いた夜に「コードが変わったのか」を
    切り分ける印になる。引数は固定（shell を通さない）。記録するのは SHA と印だけでパスは持たない。
    取れなくても診断値の書き込みは止めない（"unknown" で書く）。
    """
    try:
        sha = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                  text=True, timeout=10, check=True).stdout.strip()
        dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                    capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("code_version を取得できない（%s）。unknown として記録する", type(e).__name__)
        return "unknown"
    if len(sha) != 40:
        logger.warning("git rev-parse の出力が SHA ではない。unknown として記録する")
        return "unknown"
    return sha + ("+dirty" if dirty else "")


def _redact(text: str) -> str:
    """例外文字列から環境変数の機微な値を消す（`collector_utils.redact_secrets`・#577）。"""
    from collector_utils import redact_secrets
    return redact_secrets(text)


def _record_diagnostics(db, name: str, result: dict, run_ctx: dict,
                        started_at: datetime) -> str | None:
    """診断値を1行書き、書けたことを直接クエリで確かめる。戻り値はログ用の要約。

    抽出器が無いモデル（`--models` で明示した非夜間モデル等）は書かずに None を返す。
    書けなかったら例外を送出する（呼び出し側が失敗として数える）。
    """
    from sqlalchemy import func

    from database import NightlyModelDiagnostic, insert_nightly_model_diagnostic

    extractor = DIAG_EXTRACTORS.get(name)
    if extractor is None:
        logger.info("[%s] 診断値の抽出器が無いので記録しない", name)
        return None
    diag = _json_safe(extractor(result))
    json.dumps(diag, allow_nan=False)      # 書く前に JSON として通ることを確かめる（NaN は上で消している）
    snapshot = (result.get("asof") or {}).get("snapshot_date")
    insert_nightly_model_diagnostic(
        db, run_id=run_ctx["run_id"], model=name,
        snapshot_date=str(snapshot) if snapshot else None,
        code_version=run_ctx["code_version"],
        preprocess_version=run_ctx["preprocess_version"],
        diagnostics=diag,
    )
    n_rows, created = (
        db.query(func.count(NightlyModelDiagnostic.id),
                 func.max(NightlyModelDiagnostic.created_at))
        .filter(NightlyModelDiagnostic.run_id == run_ctx["run_id"],
                NightlyModelDiagnostic.model == name)
        .one()
    )
    if not n_rows or created is None:
        raise VerificationError(
            f"nightly_model_diagnostics に run_id={run_ctx['run_id']} / model={name} の行が無い")
    if _aware_utc(created) < started_at:
        raise VerificationError(
            f"nightly_model_diagnostics の created_at={_aware_utc(created).isoformat()} が"
            f" 実行開始 {started_at.isoformat()} より古い")
    for w in _edge_warnings(name, diag):
        logger.warning("[%s] alpha が候補の端: %s", name, w)
    return f"run_id={run_ctx['run_id']} / code_version={run_ctx['code_version']}"


def _run_context(code_version: str | None = None) -> dict:
    from plugins.utils import PREPROCESS_VERSION
    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "code_version": code_version if code_version is not None else _code_version(),
        "preprocess_version": PREPROCESS_VERSION,
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


async def run_models(models: list[str], db, *, code_version: str | None = None) -> list[dict]:
    """モデルを順に実行し、1件ごとの結果 dict を返す（例外は握って次のモデルへ進む）。

    全体を `shared_snapshot_cache()` で包む。`load_data`（週次127万行）/`preload_macro`/
    `build_snapshots` はモデル間で結果が同一なので、包まないとモデル数だけ DB から
    再ロードして Supabase Egress（5GB/月）を食う（#443）。ContextVar なので
    `execute_plugin` の `asyncio.to_thread` オフロード先へも伝播する（plugins/__init__.py）。
    キャッシュ対象は**入力**（株価・財務・マクロ）だけで、producer が書く出力テーブルは
    含まないため、モデル間で書き込みが見えなくなることはない。

    `code_version` は診断値に添える印（既定は作業ツリーの git から取る・テストは明示で渡す）。
    """
    from plugins.macro_snapshots import shared_snapshot_cache

    run_ctx = _run_context(code_version)
    with shared_snapshot_cache():
        return await _run_models_inner(models, db, run_ctx)


async def _run_models_inner(models: list[str], db, run_ctx: dict) -> list[dict]:
    """run_models の本体（キャッシュコンテキストの中で呼ばれる前提）。"""
    from plugins import execute_plugin, get_plugin

    entries: list[dict] = []
    for name in models:
        started_at = datetime.now(timezone.utc)
        t0 = time.time()
        entry: dict = {"model": name, "ok": False, "summary": None,
                       "verified": None, "error": None,
                       "diagnostics": None, "diagnostics_error": None}
        result = None
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
            entry["error"] = _redact(f"{type(e).__name__}: {e}")
            logger.exception("[%s] 失敗: %s", name, entry["error"])
        # 診断値は μ̂ / gap_ratio の永続化と検証が済んだ後にだけ書く（失敗しても μ̂ は巻き戻さない）。
        if entry["ok"]:
            try:
                entry["diagnostics"] = _record_diagnostics(db, name, result, run_ctx, started_at)
                if entry["diagnostics"]:
                    logger.info("[%s] 診断値を記録: %s", name, entry["diagnostics"])
            except Exception as e:   # noqa: BLE001 — 失敗として数え、次のモデルへ進む
                db.rollback()
                entry["diagnostics_error"] = _redact(f"{type(e).__name__}: {e}")
                logger.error("[%s] 診断値を記録できなかった: %s", name, entry["diagnostics_error"])
        entry["elapsed_min"] = round((time.time() - t0) / 60, 1)
        entries.append(entry)
    return entries


def failed_labels(entries: list[dict]) -> list[str]:
    """非ゼロ終了の理由になるラベル（モデルの失敗と、診断値の記録の失敗を分けて出す）。"""
    out: list[str] = []
    for e in entries:
        if not e["ok"]:
            out.append(e["model"])
        elif e.get("diagnostics_error"):
            out.append(f"{e['model']}:diagnostics")
    return out


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
        if e.get("diagnostics_error"):
            logger.info("%-8s %-14s        診断値: %s", "FAILED", e["model"], e["diagnostics_error"])
    logger.info("総所要時間: %.1f分", (time.time() - t0) / 60)
    logger.info("=" * 60)

    failed = failed_labels(entries)
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
