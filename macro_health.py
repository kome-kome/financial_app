"""マクロ系列の鮮度ゲート（Issue #420）。

収集ワークフローは 1 系列が取れなくても例外を出さず `continue` する（`collect_macro_data`）。
ジョブは exit 0 で終わるため、#414 のワークフロー失敗通知でも**部分失敗は絶対に拾えない**。
マクロは M-1 / M-2 / M-3 / M-6 すべての入力で、1 系列が静かに欠けると strict の M-1 は
学習サンプル 0 件になりうる（#379 の既往）。ここで「収集したはずの系列が実際に新しいか」を
DB 側の `max(trade_date)` で突き合わせ、既定モデルが使う系列（critical）が古ければ
呼び出し側に非ゼロ終了させる＝ #414 の Issue 起票へ載せる。

**判定は収集直後に取るが、終了は全フェーズ完了後に行う**（パイプライン側の責務）。
マクロの不調で株価鮮度フェーズを巻き添えにしない＝ #425 で確立した
「片方の収集元の障害がもう片方を止めない」構造をここでも守る。
"""
from datetime import date
from typing import Optional

from sqlalchemy import func as sqla_func

from database import MacroData

# ── 期待更新頻度ごとの許容遅延（日）────────────────────────────────────────
# 2026-08-04 に本番 macro_data を実測した最大遅延を基準に、公表ラグ＋週末＋祝日分の
# 余裕を足した値。閾値を下げすぎると平常運転で誤検知し、通知が狼少年になる。
#   daily     実測最大 7日（US_EPU / BAA_SPREAD＝FRED 日次の公表ラグ＋週末）
#   monthly   実測最大 96日（JP_IIP＝e-Stat 鉱工業指数は約2か月ラグ）
#   quarterly 実測最大 80日（JP_REAL_GDP / JP_TRADE_BAL＝lag_days=135 補正後）
#   semiannual IMF WEO（年2回公表・DB 上は補間で日次に見えるが安全側で広く取る）
FREQ_STALE_DAYS: dict[str, int] = {
    "daily":      14,
    "weekly":     21,
    "monthly":    130,
    "quarterly":  210,
    "semiannual": 400,
}

# 系列定義に `freq` が無いグループの既定頻度。
_GROUP_DEFAULT_FREQ: dict[str, str] = {
    "MACRO_SERIES":       "daily",       # Yahoo/stooq の市場系
    "FRED_SERIES":        "daily",       # freq 明示のある実体経済系は各定義が優先
    "BOJ_SERIES":         "monthly",     # 全系列が freq を明示
    "OECD_SERIES":        "monthly",     # JP_CLI
    "ESRI_SERIES":        "quarterly",   # 四半期別GDP速報
    "IMF_SERIES":         "semiannual",  # WEO（4月/10月）
    "ESTAT_SERIES":       "monthly",     # CPI
    "ESTAT_INDEX_SERIES": "monthly",     # 鉱工業指数
    "GDELT_SERIES":       "daily",
    "WIKIMEDIA_SERIES":   "daily",
}

# 鮮度判定から除外する系列（理由必須）。**除外しても警告ログには出す**——
# 黙って対象外にすると「検知できない欠落」を自分で作ることになる（#414 と同型の罠）。
EXCLUDED_SERIES: dict[str, str] = {
    # FRED が 2024-04-30 で凍結（#253）。e-Stat の JP_IIP へ移行済みで収集対象からも外れているが、
    # macro_data には過去行が残るため保険として明示除外する。
    "JP_IP":  "FRED 側が 2024-04-30 で凍結（#253）。JP_IIP（e-Stat）へ移行済み",
    # Yahoo が ^JGB を廃止（404）。MACRO_SERIES には残っているが 1 行も蓄積されない。
    # M-1/M-3 は JP10Y_FRED（FRED 月次）を使うため既定モデルへの影響は無い。
    "JP10Y":  "Yahoo ^JGB 廃止で取得不能。既定モデルは JP10Y_FRED を使用",
    # 2026-07-17 以降 Yahoo ^BCOM が新しい行を返さない（2026-08-04 実測で Yahoo 側も同日止まり）。
    # TOPIX ^TPX と同型の配信停止の可能性。代替ティッカー選定は #438。
    "BCOM":   "Yahoo ^BCOM が 2026-07-17 以降配信停止（#438 で代替ティッカーを検討中）",
}


def expected_series() -> list[dict]:
    """収集対象の全系列を {code, name, freq, group} の一覧で返す。

    系列定義（collector_prices.py）が唯一の情報源。ここに列挙を二重管理しない。
    API キー未設定でスキップされるグループ（FRED / e-Stat）は収集自体が走らないため
    対象から外す（未設定環境で毎回 stale 扱いになるのを防ぐ）。
    """
    import collector_prices as cp

    groups: list[tuple[str, list[dict]]] = [
        ("MACRO_SERIES",       cp.MACRO_SERIES),
        ("BOJ_SERIES",         cp.BOJ_SERIES),
        ("OECD_SERIES",        cp.OECD_SERIES),
        ("ESRI_SERIES",        cp.ESRI_SERIES),
        ("IMF_SERIES",         cp.IMF_SERIES),
        ("GDELT_SERIES",       cp.GDELT_SERIES),
        ("WIKIMEDIA_SERIES",   cp.WIKIMEDIA_SERIES),
    ]
    if cp.FRED_API_KEY:
        groups.append(("FRED_SERIES", cp.FRED_SERIES))
    if cp.ESTAT_API_KEY:
        groups.append(("ESTAT_SERIES",       cp.ESTAT_SERIES))
        groups.append(("ESTAT_INDEX_SERIES", cp.ESTAT_INDEX_SERIES))

    out: list[dict] = []
    for group_name, series_list in groups:
        default_freq = _GROUP_DEFAULT_FREQ[group_name]
        for s in series_list:
            out.append({
                "code":  s["code"],
                "name":  s.get("name", s["code"]),
                "freq":  s.get("freq", default_freq),
                "group": group_name,
            })
    return out


def critical_series_codes() -> set[str]:
    """既定モデルが実際に読む series_code の集合。

    M-1/M-2/M-6（月次スナップショット）と M-3（週次 DLM）の DEFAULT_MACRO_FEATURES から
    逆引きする。昇格ゲートで棄却された系列（`_GATE_REJECTED_FEATURES`）や保留枠は既定に
    入らないため自動的に critical から外れる＝GDELT/Wikimedia の一時失敗で毎日落ちない。
    重い依存（numpy/pandas）を収集経路へ持ち込まないよう関数内 import にする。
    """
    from plugins.macro_snapshots import (
        DEFAULT_MACRO_FEATURES as _SNAP_DEFAULTS, _MACRO_MAP,
    )
    from plugins.macro_dlm import (
        DEFAULT_MACRO_FEATURES as _DLM_DEFAULTS, _DLM_MACRO_MAP,
    )
    codes = {_MACRO_MAP[f][0] for f in _SNAP_DEFAULTS if f in _MACRO_MAP}
    codes |= {_DLM_MACRO_MAP[f][0] for f in _DLM_DEFAULTS if f in _DLM_MACRO_MAP}
    return codes


def check_macro_freshness(db, as_of: Optional[date] = None) -> dict:
    """系列ごとの max(trade_date) を許容遅延と突き合わせる。

    DB 側で GROUP BY 集約するため転送は系列数行のみ（Egress を食わない・#355）。
    戻り値:
        {
          "as_of": date, "checked": int,
          "stale":    [{code, name, freq, last, lag_days, limit, critical, group}, ...],
          "missing":  [同上（last=None・lag_days=None）],
          "excluded": [{code, reason, last}, ...],
          "n_critical_bad": int,   # critical かつ stale/missing の本数
        }
    """
    as_of = as_of or date.today()
    rows = (
        db.query(MacroData.series_code, sqla_func.max(MacroData.trade_date))
        .group_by(MacroData.series_code).all()
    )
    latest = {code: mx for code, mx in rows}
    critical = critical_series_codes()

    stale: list[dict] = []
    missing: list[dict] = []
    excluded: list[dict] = []
    for s in expected_series():
        code = s["code"]
        last_str = latest.get(code)
        if code in EXCLUDED_SERIES:
            excluded.append({"code": code, "reason": EXCLUDED_SERIES[code], "last": last_str})
            continue

        limit = FREQ_STALE_DAYS[s["freq"]]
        entry = {**s, "last": last_str, "limit": limit, "critical": code in critical}
        if not last_str:
            missing.append({**entry, "lag_days": None})
            continue
        lag = (as_of - date.fromisoformat(str(last_str)[:10])).days
        if lag > limit:
            stale.append({**entry, "lag_days": lag})

    bad = [e for e in stale + missing if e["critical"]]
    return {
        "as_of": as_of,
        "checked": len(expected_series()) - len(excluded),
        "stale": sorted(stale, key=lambda e: -(e["lag_days"] or 0)),
        "missing": missing,
        "excluded": excluded,
        "n_critical_bad": len(bad),
    }


def format_report(result: dict) -> list[str]:
    """健全性チェック結果をログ 1 行ずつのリストへ整形する（ASCII 記号のみ）。"""
    lines = [
        f"[マクロ健全性] {result['checked']}系列を判定"
        f"（stale={len(result['stale'])} / missing={len(result['missing'])}"
        f" / excluded={len(result['excluded'])}・基準日 {result['as_of']}）"
    ]
    for e in result["missing"]:
        mark = "CRITICAL" if e["critical"] else "warn"
        lines.append(f"  [{mark}] {e['code']}: macro_data に1行も無い"
                     f"（{e['group']} / {e['freq']}）")
    for e in result["stale"]:
        mark = "CRITICAL" if e["critical"] else "warn"
        lines.append(f"  [{mark}] {e['code']}: last={e['last']}"
                     f" lag={e['lag_days']}日 > 許容{e['limit']}日（{e['freq']}）")
    for e in result["excluded"]:
        lines.append(f"  [除外] {e['code']}: {e['reason']}（last={e['last']}）")
    if result["n_critical_bad"]:
        lines.append(f"[マクロ健全性] 既定モデルが使う系列が {result['n_critical_bad']} 本"
                     f" 不健全。パイプラインを非ゼロ終了させる（#420）")
    else:
        lines.append("[マクロ健全性] 既定モデルが使う系列はすべて健全")
    return lines
