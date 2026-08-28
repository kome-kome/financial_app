"""朝の「読むだけ」ダッシュボード API ルーター（Issue #423 子3）。

`GET /api/morning` は**一切学習しない**。夜間バッチ（`daily-incremental` →
`nightly-scores`）が永続化した結果（`financial_metrics` VIEW ／ `regression_results`
／ producer の `*_scores`）を読んで買い推奨ランキングへ合成し、**鮮度ブロックを必ず
一緒に返す**。heavy=False の経路だけを使うので Render Free（30秒上限）でも動く。

鮮度を必須にしているのは、夜間バッチは必ずいつか失敗し、そのとき画面には昨日の
スコアが何食わぬ顔で並ぶため（#414 の 19日連続 failure が誰にも気づかれなかった実例）。
`overall_verdict` は「発注してよいか」の 1 語に集約し、赤なら該当ワークフローの
Actions ページへ誘導する。**ランキング自体は隠さない**——見えなくすると別の手段で
古い値を見に行くだけなので、出した上で「この結果で発注しない」を明示する。
"""
import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

import api
import plugins as plugin_registry
from database import (
    RegressionResult, business_days_between, get_producer_asof,
    PRICE_STALE_WARN_BDAYS, PRICE_STALE_ALERT_BDAYS,
)

router = APIRouter()
log = logging.getLogger(__name__)

# 「赤いのは分かったが次に何を見ればいいのか」で止まらせないための導線（#423 子3）。
#
# **#503 で駆動が GitHub Actions からローカルのタスクスケジューラへ移った。** 以前ここは
# `actions/workflows/<name>.yml` を指していたが、それらの cron は全てコメントアウト済みで
# （`.github/workflows/` で生きている cron は `egress-health.yml` の1本だけ）、
# **押すともう動いていないページに着く**。復旧手順の正本は DEPLOYMENT.md なのでそちらへ送る
# （#561）。アンカーは付けない——見出しを直した瞬間に静かに壊れるため。
_RUNBOOK_URL = "https://github.com/kome-kome/financial_app/blob/main/docs/DEPLOYMENT.md"

# 各ブロックを前進させる駆動主体。表記は `nightly_scores.HEAVY_AUTOMATION` の
# `local:<スクリプト>` 語彙と揃える（画面と登録簿で別の名前を使わない）。
_DRIVER_NIGHTLY = "local:scripts/run_nightly.py"
_DRIVER_MONTHLY = "local:scripts/run_monthly.py"

# 手で回すときのコマンド。画面から読める場所に置く（DEPLOYMENT.md を開かずとも打てる）。
_CMD_NIGHTLY = "./run_nightly.ps1"
_CMD_MONTHLY = "./run_monthly.ps1"
_CMD_MACRO = "python collector.py --macro"

# 足跡の status を画面の語彙へ。**「まだ一度も走っていない」と「止まった」を同じ顔にしない**
# のは watchdog 側と同じで、ここが決めるのは色だけ。
_BATCH_LEVEL = {"ok": "fresh", "stale": "alert",
                "missing": "alert", "unreadable": "alert"}

# gap_ratio（sector_ols・nightly-scores が毎晩更新）の許容鮮度。夜間バッチが毎日
# 走る前提なので、丸2日以上動いていなければ何かが壊れている。
GAP_WARN_DAYS  = 2
GAP_ALERT_DAYS = 7

# μ̂（producer スコア）の許容鮮度。スナップショットは週次グリッドなので日次より緩い。
MU_WARN_BDAYS  = 7
MU_ALERT_BDAYS = 14

# 既定の μ̂ 出所。売り側 spread の実測で M-6 を既定に採った（ADR-0022）。
# **買い側 rank-IC と売り側 spread は順位が一致しない**ため、ここは「表示用の as-of を
# 出す対象」であって買いスコアへの結線ではない。
# #423 子4 で recommend 側に mu 指標＋mu_source を通したが**既定は OFF**（PRESETS の
# どれにも mu 重みが無い・ADR-0030）。morning は preset 経由でしか重みを渡さないので、
# μ̂ が朝のランキングへ入るのは「mu 重みを持つプリセットを既定にする」＝昇格ゲート
# （ADR-0028）を通した後になる。それまでこの値は鮮度表示専用のままでよい。
DEFAULT_MU_SOURCE = "macro_enet"

_LEVEL_ORDER = {"fresh": 0, "warn": 1, "alert": 2, "empty": 2, "unknown": 2}


def _worst(*levels: str) -> str:
    """総合判定は fresh / warn / alert の3語へ正規化する。

    データ空（empty）や判定不能（unknown）は「発注してよいか」の観点では赤と同じ
    ＝ alert に寄せる。理由は reasons に個別に出るので情報は失われない。
    """
    worst = max(_LEVEL_ORDER.get(lv, 2) for lv in levels)
    return {0: "fresh", 1: "warn", 2: "alert"}[worst]


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    """足跡の ISO 文字列を datetime へ（表示用）。読めなければ None を返して素通しする。"""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_bdays(d: Optional[str]) -> Optional[int]:
    if not d:
        return None
    try:
        return business_days_between(date.fromisoformat(str(d)[:10]), date.today())
    except ValueError:
        return None


def _gap_ratio_block(db: Session) -> dict:
    """regression_results（sector_ols の gap_ratio）の鮮度。"""
    row = (
        db.query(func.max(RegressionResult.computed_at),
                 func.count(RegressionResult.gap_ratio))
        .filter(RegressionResult.gap_ratio.isnot(None))
        .first()
    )
    computed_at, n_rows = (row or (None, 0))
    if not computed_at:
        return {"computed_at": None, "n_rows": 0, "age_days": None, "level": "empty",
                "driver": _DRIVER_NIGHTLY, "command": _CMD_NIGHTLY, "url": _RUNBOOK_URL}
    ref = computed_at if computed_at.tzinfo else computed_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - ref).days
    level = ("alert" if age_days > GAP_ALERT_DAYS
             else "warn" if age_days > GAP_WARN_DAYS else "fresh")
    return {
        "computed_at": api._utc_to_jst_str(computed_at),
        "n_rows": n_rows or 0,
        "age_days": age_days,
        "level": level,
        "driver": _DRIVER_NIGHTLY,
        "command": _CMD_NIGHTLY,
        "url": _RUNBOOK_URL,
    }


def _mu_block(db: Session, mu_source: str) -> dict:
    """producer スコア（μ̂）の as-of。未蓄積なら level=empty で返す（例外にしない）。"""
    asof = get_producer_asof(db, mu_source)
    # μ̂ の更新主体は producer で違う（macro_enet は夜間、M-1 系は月次探索の
    # --persist-scores 副作用）。毎晩前進するのは既定の macro_enet だけ。
    is_nightly = mu_source == DEFAULT_MU_SOURCE
    base = {"source": mu_source,
            "driver": _DRIVER_NIGHTLY if is_nightly else _DRIVER_MONTHLY,
            "command": _CMD_NIGHTLY if is_nightly else _CMD_MONTHLY,
            "url": _RUNBOOK_URL}
    if not asof:
        return {**base, "snapshot_date": None, "snapshot_date_min": None,
                "n_stale": 0, "age_bdays": None, "level": "empty"}
    age = _age_bdays(asof["snapshot_date"])
    level = "unknown" if age is None else (
        "alert" if age > MU_ALERT_BDAYS else "warn" if age > MU_WARN_BDAYS else "fresh")
    return {**base, **asof, "age_bdays": age, "level": level}


def _macro_block(db: Session) -> dict:
    """マクロ系列の鮮度（#420 の判定を画面へも出す）。

    macro_health は collector 側の系列定義と plugins の既定特徴量を読むため、
    import は関数内に閉じる（API 起動時のコストを増やさない）。
    """
    try:
        from macro_health import check_macro_freshness
        r = check_macro_freshness(db)
    except Exception as e:                     # 判定不能でも朝の表示は止めない
        log.info("マクロ鮮度の判定に失敗（表示は継続）: %s", e)
        return {"level": "unknown", "n_critical_bad": None, "worst": [],
                "driver": _DRIVER_NIGHTLY, "command": _CMD_MACRO, "url": _RUNBOOK_URL}
    bad = [e for e in r["stale"] + r["missing"] if e["critical"]]
    return {
        "level": "alert" if bad else "fresh",
        "n_critical_bad": len(bad),
        "n_stale_total": len(r["stale"]) + len(r["missing"]),
        "worst": [{"code": e["code"], "last": e["last"], "lag_days": e["lag_days"]}
                  for e in bad[:5]],
        "driver": _DRIVER_NIGHTLY,
        "command": _CMD_MACRO,
        "url": _RUNBOOK_URL,
    }


def _batch_block(db: Session, get=None, now: Optional[datetime] = None) -> dict:
    """ローカル駆動バッチの足跡（#561）。「昨夜そもそも走ったのか」を画面へ出す。

    **判定は `batch_freshness.collect()` と共有する**（watchdog と同じ閾値・同じ語彙）。
    自前で経過を測ると、窓を広げたときに片方だけ黙って古くなる。

    ここが無かった間、鮮度ブロックは「スコアが古い」までは言えたが「バッチが走っていない」
    とは言えなかった。両者は原因が違う（前者は producer の失敗、後者は起動の失敗）のに
    画面では同じ顔をする。結果として**健全なのに「止まっているのでは」と疑われ**、
    正本をローカルへ移した後にアプリが使われなくなった（#561 の発端）。
    ADR-0031「登録があること != 動いていること」の裏返しで、ここは
    **動いていること != 動いていると分かること**にあたる。

    `get` は `app_settings` 読み取りの継ぎ目（テストが足跡を注入する）。
    """
    # `batch_freshness` は import 時副作用を持たないが、API 起動時のコストは増やさない
    # （`_macro_block` と同じ作法）。**`scripts/check_batch_freshness.py` の方を import
    # しないこと**——あちらは import 時に FINAPP_DB_TARGET を書き換える。
    from batch_freshness import collect
    from scripts.run_nightly import KEY_LAST_RUN as _NIGHTLY_KEY

    now = now or datetime.now(timezone.utc)
    try:
        snap = collect(db, now, get=get)
    except Exception as e:                     # 判定不能でも朝の表示は止めない
        log.info("バッチ鮮度の判定に失敗（表示は継続）: %s", e)
        return {"level": "unknown", "rows": [], "driver": _DRIVER_NIGHTLY,
                "command": _CMD_NIGHTLY, "url": _RUNBOOK_URL}

    rows, level = [], "unknown"
    for row in snap["rows"]:
        w = row["watched"]
        if row["status"] == "missing" and not w.missing_is_problem:
            # 自分の行を書くのは自分だけ＝watchdog の初回 missing は正常
            row_level = "fresh"
        else:
            row_level = _BATCH_LEVEL.get(row["status"], "alert")
        gates = w.key_run == _NIGHTLY_KEY
        if gates:
            level = row_level
        rows.append({
            "label": w.label,
            "task_name": w.task_name,
            "status": row["status"],
            "level": row_level,
            "last_run": api._utc_to_jst_str(_parse_iso(row["run_raw"])),
            "last_success": api._utc_to_jst_str(_parse_iso(row["success_raw"])),
            "age_h": row["run_age_h"],
            "stale_h": w.stale_h,
            "gates_verdict": gates,
        })
    return {"level": level, "rows": rows, "db_label": snap["db_label"],
            "driver": _DRIVER_NIGHTLY, "command": _CMD_NIGHTLY, "url": _RUNBOOK_URL}


def _reasons(price: dict, gap: dict, mu: dict, macro: dict, batch: dict) -> list[str]:
    """赤・黄の理由を人が読める順で並べる（画面はこれをそのまま出す）。"""
    out = []
    # **バッチの停止は先頭に置く。** gap_ratio や μ̂ の古さはその結果でしかないので、
    # 原因を先に読ませないと下流の症状から順に辿ることになる（#561）。
    night = next((r for r in batch.get("rows", []) if r.get("gates_verdict")), None)
    if night and night["level"] != "fresh":
        if night["status"] == "stale":
            out.append(f"夜間バッチが {night['age_h']:.0f}時間 走っていない"
                       f"（閾値 {night['stale_h']:.0f}時間・最終実行 {night['last_run']}）")
        elif night["status"] == "missing":
            out.append("夜間バッチの足跡が app_settings に無い"
                       "（一度も走っていないか、行が消えた）")
        else:
            out.append("夜間バッチの足跡を日時として読めない")
    elif batch.get("level") == "unknown":
        out.append("バッチの足跡を判定できなかった（DB かモジュールの読み取りに失敗）")
    if price.get("level") in ("warn", "alert"):
        out.append(f"株価の中央値が {price.get('price_asof_p50')}"
                   f"（{price.get('stale_bdays')}営業日前・{price.get('n_stale_over_5d')}銘柄が"
                   f"{PRICE_STALE_WARN_BDAYS}営業日超の遅れ）")
    elif price.get("level") == "empty":
        out.append("株価データが空")
    if gap["level"] in ("warn", "alert"):
        out.append(f"gap_ratio の更新が {gap['age_days']}日前（夜間スコア更新が止まっている可能性）")
    elif gap["level"] == "empty":
        out.append("gap_ratio が未生成（sector_ols が一度も走っていない）")
    if mu["level"] in ("warn", "alert"):
        out.append(f"μ̂（{mu['source']}）のスナップショットが {mu['snapshot_date']}"
                   f"（{mu['age_bdays']}営業日前）")
    elif mu["level"] == "empty":
        out.append(f"μ̂（{mu['source']}）が未蓄積")
    if macro["level"] == "alert":
        codes = "・".join(e["code"] for e in macro["worst"]) or "不明"
        out.append(f"既定モデルが使うマクロ系列が古い/欠測: {codes}")
    return out


@router.get("/api/morning")
async def morning(
    preset: str = "バランス型",
    top_n: int = 20,
    mu_source: str = DEFAULT_MU_SOURCE,
    db: Session = Depends(api.get_db),
):
    """朝の買い推奨ランキング＋鮮度ブロックを 1 回で返す（学習・再計算なし）。"""
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")

    p = plugin_registry.get_plugin("recommend")
    try:
        rec = await plugin_registry.execute_plugin(
            p, {"preset": preset, "top_n": top_n}, db)
    except (plugin_registry.DependencyError, ValueError) as e:
        log.info("morning: recommend を依存/検証で拒否: %s", e)
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Morning error: %s", e, exc_info=True)
        raise HTTPException(500, "朝の集計に失敗しました。")

    price = rec.get("price_freshness") or {"level": "empty"}
    gap   = _gap_ratio_block(db)
    mu    = _mu_block(db, mu_source)
    macro = _macro_block(db)
    batch = _batch_block(db)

    # **verdict へ混ぜるのは夜間バッチだけ**（`batch["level"]` がそれ）。月次と watchdog は
    # 表示に留める——既定の推奨経路（recommend / sector_ols / macro_enet）は月次成果物に
    # 依存せず、月次で更新される M-1 系 μ̂ の鮮度は `_mu_block` が別に見ている。ここで月次を
    # 混ぜると次の月次まで毎日 warn が出続けて狼少年になり、**本当に止まった回に効かなくなる**
    # （`common.js` が接続先バッジの向きで避けたのと同じ失敗）。
    verdict = _worst(price.get("level", "empty"), gap["level"], mu["level"],
                     macro["level"], batch["level"])
    return {
        "generated_at": api._utc_to_jst_str(datetime.now(timezone.utc)),
        "preset": preset,
        "freshness": {
            "price": price,
            "gap_ratio": gap,
            "mu": mu,
            "macro": macro,
            "batch": batch,
            "overall_verdict": verdict,
            # 赤でもランキングは返す（隠すと別経路で古い値を見に行くだけ）。
            # 「発注してよいか」だけを明示する。
            "tradable": verdict == "fresh",
            "reasons": _reasons(price, gap, mu, macro, batch),
            # #503 以降、次に見るべきはワークフローの実行履歴ではなくローカル運用の手順書。
            "runbook_url": _RUNBOOK_URL,
        },
        "recommend": rec,
    }
