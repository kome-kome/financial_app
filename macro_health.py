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
# **較正には「その時点で停止していない系列」だけを使う**（ADR-0028 規則2）。停止中の系列の
# 実測値で線を引くと、その停止を検知できない閾値になる——2026-08-04 以前の monthly=130 は
# JP_IIP の 96日（e-Stat が2026年3月分で配信を止めた結果の値）を「正常な公表ラグ」とみなして
# 引かれており、#451 の欠落を構造的に見逃していた。
#
# 目安は健全系列の**理論最大**（＝観測周期 + 実配信ラグ − lag_days）の約2倍。閾値を下げすぎ
# ると平常運転で誤検知し通知が狼少年になるので、実測値ではなく理論最大から取る。
#   daily      理論最大 7日（US_EPU / BAA_SPREAD＝FRED 日次の公表ラグ＋週末）→ 2.0倍
#   monthly    理論最大 53日（JP_CPI_TOTAL＝30 + 53 − 30。実測は JP_MONETARY_BASE の 50日が
#              最大）→ 約2.0倍。**JP_IIP の 96日は停止中なので較正に使わない**
#   quarterly  理論最大 90日（JP_REAL_GDP＝91 + 134 − 135）。実測 80日は次の四半期速報を
#              待っている平常運転の値
#   semiannual IMF WEO（年2回公表・DB 上は補間で日次に見えるが安全側で広く取る）
#
# freq 既定は**粗い網**であり精密な検知は期待しない。lag_days が大きい系列ほど既定との乖離が
# 開く（JP_CLI は理論最大 5日に対して 105日＝21倍）ため、検知力は系列個別の `stale_days` で
# 与える（下記）。
FREQ_STALE_DAYS: dict[str, int] = {
    "daily":      14,
    "weekly":     21,
    "monthly":    105,
    "quarterly":  210,
    "semiannual": 400,
}

# 観測周期（日）。`lag_days` の過大を検知する物差しに使う（下記 future 判定）。
FREQ_PERIOD_DAYS: dict[str, int] = {
    "daily":      1,
    "weekly":     7,
    "monthly":    31,
    "quarterly":  92,
    "semiannual": 183,
}

# ── 系列個別の許容遅延（`stale_days`）──────────────────────────────────────
# 系列定義（collector_prices.py）が `stale_days` を持つときは freq 既定より優先する。
# **こちらが検知の本命**で、freq 既定は指定の無い系列を拾う保険（ADR-0028）。
#
# 用途は `lag_days` で trade_date を後ろへシフトしている系列。シフト分だけ last が
# 「新しく」見えるため freq 既定のままでは配信停止の検知がその日数ぶん遅れる（#444）。
# 値は **健全時の理論最大 + 観測周期**＝「公表を1回スキップしたら鳴る」で与える
# （lag_days≧60 の4系列に付与済み: JP_CLI=35 / JP_UNEMP=60 / JP_IIP=60 / JP_IIP_INVENTORY=60）。
#
# **freq 既定を一律に `- lag_days` してはいけない**：`lag_days` は先読み防止の保守的
# シフト量であって実配信ラグの推定値ではない（ADR-0028）。2026-08-04 の本番実測では
# JP_REAL_GDP / JP_TRADE_BAL（lag_days=135・実測lag 80日 > 210-135=75）が即 CRITICAL に
# 落ちる。80日は次の四半期速報を待っている平常運転の値であって遅延ではない。

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
    "MOF_SERIES":         "daily",       # 財務省 国債金利（営業日ごと・#458）
}

# 鮮度判定から除外する系列（理由必須）。**除外しても警告ログには出す**——
# 黙って対象外にすると「検知できない欠落」を自分で作ることになる（#414 と同型の罠）。
EXCLUDED_SERIES: dict[str, str] = {
    # FRED が 2024-04-30 で凍結（#253）。e-Stat の JP_IIP へ移行済みで収集対象からも外れているが、
    # macro_data には過去行が残るため保険として明示除外する。
    "JP_IP":  "FRED 側が 2024-04-30 で凍結（#253）。JP_IIP（e-Stat）へ移行済み",
    # JP10Y（Yahoo ^JGB / stooq 10jpy.b）は #442 で `MACRO_SERIES` から削除したため、除外指定も
    # 不要になった（収集しない系列は `total` の分母にも鮮度判定にも出てこない）。既定モデルが使う
    # のは月次 `JP10Y_FRED` で、こちらは通常どおり critical として監視する。
    # BCOM は #438 で解決したため除外を解いた（2026-08-06）。指数 `^BCOM` の配信停止は続いて
    # いるが、収集元を連動 ETN `DJP` へ差し替えて全期間を再収集済み＝他の市場系と同じ日次鮮度に
    # 戻る。**除外を残すと今度は DJP の停止を検知できなくなる**ので、直った時点で必ず外す。
    # e-Stat の統計表（0004052177 / 0004052179）が 2026-06-03 更新を最後に止まり、2026年3月分
    # （trade_date=2026-04-30）以降が入らない。世代間隔は約23か月で月次更新の痕跡が無く、API
    # 経由では次回更新が最悪2028年になりうる（#451 で実 API 実測）。昇格ゲートでも4検定すべて
    # 非有意だったため既定特徴量からは棄却済み（`_GATE_REJECTED_FEATURES`）＝critical からは
    # 既に外れているが、stale の常時表示でレポートを慎重にしないよう明示除外する。
    # **収集は継続する**（e-Stat がいずれ更新すればデータは貯まる）。
    "JP_IIP":           "e-Stat 側が年単位更新で 2026年3月分から進まない（#451・昇格ゲート棄却済み）",
    "JP_IIP_INVENTORY": "同上（#451・昇格ゲート棄却済み）",
}


def expected_series() -> list[dict]:
    """収集対象の全系列を {code, name, freq, stale_days, group} の一覧で返す。

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
        ("MOF_SERIES",         cp.MOF_SERIES),
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
                # None なら freq 既定を使う（判定側で解決する）。
                "stale_days": s.get("stale_days"),
                # 観測基準日（期首/期末/収集日）。`lag_days` はここへ加算されるので、
                # anchor が違う群の lag_days を横並びで比べてはいけない（#447）。
                "anchor": cp.SERIES_ANCHOR[group_name],
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
          "future":   [同上 + {ahead_days, period}（last が未来日＝lag_days 過大）],
          "excluded": [{code, reason, last}, ...],
          "n_critical_bad": int,   # critical かつ stale/missing/周期超過の future の本数
        }

    **未来日（future）を独立に見る理由**: `lag_days` が実配信ラグを超えると `trade_date` が
    収集日より先になり、その観測はスナップショット（`trade_date <= ref_date`）に現れないまま
    情報だけが遅れる。しかも経過日数が負の間は stale 判定が構造的に成立しない——#444 で
    `JP10Y_FRED` に `lag_days=70` を与えて 6 日先の `trade_date` を作り、#447 まで
    どのゲートにも掛からなかった（ADR-0028 Consequences）。数日のずれは安全側の失敗なので
    warn に留め、**観測周期を超える**ずれ（＝`lag_days` が丸ごと1周期ぶん過大）だけ落とす。
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
    future: list[dict] = []
    excluded: list[dict] = []
    for s in expected_series():
        code = s["code"]
        last_str = latest.get(code)
        if code in EXCLUDED_SERIES:
            excluded.append({"code": code, "reason": EXCLUDED_SERIES[code], "last": last_str})
            continue

        # 系列個別の stale_days があれば freq 既定より優先（lag_days でシフトした系列用・#444）。
        limit = s["stale_days"] or FREQ_STALE_DAYS[s["freq"]]
        entry = {**s, "last": last_str, "limit": limit, "critical": code in critical}
        if not last_str:
            missing.append({**entry, "lag_days": None})
            continue
        lag = (as_of - date.fromisoformat(str(last_str)[:10])).days
        if lag < 0:
            period = FREQ_PERIOD_DAYS[s["freq"]]
            future.append({**entry, "lag_days": lag, "ahead_days": -lag, "period": period})
            continue
        if lag > limit:
            stale.append({**entry, "lag_days": lag})

    bad = [e for e in stale + missing if e["critical"]]
    bad += [e for e in future if e["critical"] and e["ahead_days"] > e["period"]]
    return {
        "as_of": as_of,
        "checked": len(expected_series()) - len(excluded),
        "stale": sorted(stale, key=lambda e: -(e["lag_days"] or 0)),
        "missing": missing,
        "future": sorted(future, key=lambda e: -e["ahead_days"]),
        "excluded": excluded,
        "n_critical_bad": len(bad),
    }


def format_report(result: dict) -> list[str]:
    """健全性チェック結果をログ 1 行ずつのリストへ整形する（ASCII 記号のみ）。"""
    lines = [
        f"[マクロ健全性] {result['checked']}系列を判定"
        f"（stale={len(result['stale'])} / missing={len(result['missing'])}"
        f" / future={len(result.get('future', []))}"
        f" / excluded={len(result['excluded'])}・基準日 {result['as_of']}）"
    ]
    for e in result["missing"]:
        mark = "CRITICAL" if e["critical"] else "warn"
        lines.append(f"  [{mark}] {e['code']}: macro_data に1行も無い"
                     f"（{e['group']} / {e['freq']}）")
    for e in result.get("future", []):
        mark = "CRITICAL" if e["critical"] and e["ahead_days"] > e["period"] else "warn"
        lines.append(f"  [{mark}] {e['code']}: last={e['last']} が基準日より"
                     f" {e['ahead_days']}日先（lag_days が実配信ラグ超過"
                     f"・観測周期{e['period']}日）")
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
