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

# GitHub Actions の該当 run へ誘導するためのリポジトリ URL。
# 「赤いのは分かったが次に何を見ればいいのか」で止まらせないための導線（#423 子3）。
_ACTIONS_BASE = "https://github.com/kome-kome/financial_app/actions/workflows"

# gap_ratio（sector_ols・nightly-scores が毎晩更新）の許容鮮度。夜間バッチが毎日
# 走る前提なので、丸2日以上動いていなければ何かが壊れている。
GAP_WARN_DAYS  = 2
GAP_ALERT_DAYS = 7

# μ̂（producer スコア）の許容鮮度。スナップショットは週次グリッドなので日次より緩い。
MU_WARN_BDAYS  = 7
MU_ALERT_BDAYS = 14

# 既定の μ̂ 出所。売り側 spread の実測で M-6 を既定に採った（ADR-0022）。
# **買い側 rank-IC と売り側 spread は順位が一致しない**ため、ここは「表示用の as-of を
# 出す対象」であって買いスコアへの結線ではない（結線は #423 子4 の担当）。
DEFAULT_MU_SOURCE = "macro_enet"

_LEVEL_ORDER = {"fresh": 0, "warn": 1, "alert": 2, "empty": 2, "unknown": 2}


def _worst(*levels: str) -> str:
    """総合判定は fresh / warn / alert の3語へ正規化する。

    データ空（empty）や判定不能（unknown）は「発注してよいか」の観点では赤と同じ
    ＝ alert に寄せる。理由は reasons に個別に出るので情報は失われない。
    """
    worst = max(_LEVEL_ORDER.get(lv, 2) for lv in levels)
    return {0: "fresh", 1: "warn", 2: "alert"}[worst]


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
                "workflow": "nightly-scores.yml",
                "url": f"{_ACTIONS_BASE}/nightly-scores.yml"}
    ref = computed_at if computed_at.tzinfo else computed_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - ref).days
    level = ("alert" if age_days > GAP_ALERT_DAYS
             else "warn" if age_days > GAP_WARN_DAYS else "fresh")
    return {
        "computed_at": api._utc_to_jst_str(computed_at),
        "n_rows": n_rows or 0,
        "age_days": age_days,
        "level": level,
        "workflow": "nightly-scores.yml",
        "url": f"{_ACTIONS_BASE}/nightly-scores.yml",
    }


def _mu_block(db: Session, mu_source: str) -> dict:
    """producer スコア（μ̂）の as-of。未蓄積なら level=empty で返す（例外にしない）。"""
    asof = get_producer_asof(db, mu_source)
    base = {"source": mu_source, "workflow": "nightly-scores.yml",
            "url": f"{_ACTIONS_BASE}/nightly-scores.yml"}
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
                "workflow": "macro-health.yml",
                "url": f"{_ACTIONS_BASE}/macro-health.yml"}
    bad = [e for e in r["stale"] + r["missing"] if e["critical"]]
    return {
        "level": "alert" if bad else "fresh",
        "n_critical_bad": len(bad),
        "n_stale_total": len(r["stale"]) + len(r["missing"]),
        "worst": [{"code": e["code"], "last": e["last"], "lag_days": e["lag_days"]}
                  for e in bad[:5]],
        "workflow": "macro-health.yml",
        "url": f"{_ACTIONS_BASE}/macro-health.yml",
    }


def _reasons(price: dict, gap: dict, mu: dict, macro: dict) -> list[str]:
    """赤・黄の理由を人が読める順で並べる（画面はこれをそのまま出す）。"""
    out = []
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

    verdict = _worst(price.get("level", "empty"), gap["level"], mu["level"], macro["level"])
    return {
        "generated_at": api._utc_to_jst_str(datetime.now(timezone.utc)),
        "preset": preset,
        "freshness": {
            "price": price,
            "gap_ratio": gap,
            "mu": mu,
            "macro": macro,
            "overall_verdict": verdict,
            # 赤でもランキングは返す（隠すと別経路で古い値を見に行くだけ）。
            # 「発注してよいか」だけを明示する。
            "tradable": verdict == "fresh",
            "reasons": _reasons(price, gap, mu, macro),
            "actions_url": f"{_ACTIONS_BASE}/daily-incremental.yml",
        },
        "recommend": rec,
    }
