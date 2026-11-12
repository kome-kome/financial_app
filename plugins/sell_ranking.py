"""売り候補ランキング（保有銘柄の中から売る銘柄を選ぶ）。

**手法は 1 つだけ**（ADR-0071）: 保有銘柄を M-6（`macro_enet`）の μ̂（52週先の期待リターン）の低い順に
並べ、**下位 1/5 に「売却」**、残りに「保有継続」を付ける。μ̂ が無い保有は「データ不足」で末尾。

この形は実測した運用そのものに合わせてある（ADR-0071）:
  - μ̂ の出所は売り側spread（全社・ADR-0022）で最も良い M-6。M-1 は p=0.10 で劣後、M-2 は有意に劣後
  - 「1 つ前の期に買いの並びの上位にいた社（＝保有に近い母集団）の中で、μ̂ の下位 1/5 を売る」と
    層の平均を下回る社を選べていた（上位層の売り側spread・#1043・ADR-0069）。下位の切り方は
    `macro_snapshots.in_layer_short_side` と同じ（昇順の先頭 m // 5 社）
  - −R_macro を足すと全社でも層の中でも有意に悪化した（#1073）

外したもの（どれも売りとして測られていない・または測って負けた）: 財務指標のプリセット（バランス型・
割高警戒型・業績悪化重視）と観点ウェイト、μ の出所の切替（M-1・M-3）、売りスコアの絶対閾値と
「一部売却」、価格トレンドによる札の補正、R3 足切り。価格トレンド（13週リターン・52週高値からの下落）と
損益は**参考として表示するだけ**で、並びにも札にも使わない。

売り専用の候補 S2（52 週先リターンの下側 20% 点・#1158 の事前登録で採用）で並べる経路も持つが、**既定では
使わない**（`SELL_KEY_SOURCE`・#1202）。本番の切り替えは新しい ADR を添えて #1201 で行う。
"""
import re
from collections import namedtuple
from typing import Any

from .base import AnalysisPlugin


# μ̂ の出所（唯一）。売り側spread で最良（ADR-0022・ADR-0071）。変えるときは ADR を添えて
# `scripts.sell_mu_source_bakeoff` の実測で決める。
MU_SOURCE = "macro_enet"
# 下位 1/SELL_QUANTILES を売る。`in_layer_short_side(n_quantiles=5)` と同じ切り方（m // 5 社）。
SELL_QUANTILES = 5

# 並びのキーの出どころ（#1202）。既定は MU_SOURCE（M-6 の μ̂）＝ADR-0071 のまま。`SELL_KEY_S2` にすると、M-6 が
# 同じパネルで解いて `sell_quantile_scores` へ保存した S2 の予測（#1158 で採用）で並べる。**画面のパラメータには
# しない**（ADR-0071 決定 4 で μ の出所の選択を外した）。切り替えは新しい ADR を添えて #1201 で行う。
# S2 の表が空（未保存・全社同値の回）か、断面の日付が M-6 と食い違う（S2 だけ古い）ときは、保有**全体**を M-6 の
# μ̂ で並べ、倒した理由を応答に出す。保有ごとに混ぜない（下側の分位と平均は尺度が違い、混ぜると順位が壊れる）。
SELL_KEY_S2 = "sell_quantile"
SELL_KEY_SOURCE = MU_SOURCE

# ── 転送列の契約（Issue #482）────────────────────────────────────────────────
# financial_metrics は派生値込みの計算 VIEW。ここで読むのは表示と損益のフォールバックに使う列だけ。
SELL_SELECT_COLS = ("edinet_code", "sec_code", "company_name", "industry", "year",
                    "stock_price", "is_active", "delisted_date")

# 週次株価の転送量制御（Issue #482）。_compute_trend が読むのは week_start と close_last の
# 2列だけで、必要な最長窓は 52週ドローダウン（closes[-52:]）＝364日。余裕1ヶ月を足して 400日
# とする（recommend._MOMENTUM_LOOKBACK_DAYS と同値）。下限は week_start（PK 第2列）へ掛けて
# (edinet_code, week_start) の範囲スキャンにする——trade_date は非インデックス列。
# holdings は textarea 入力で件数上限が無く IN 句へ直接載るためチャンクも要る。
_TREND_LOOKBACK_DAYS = 400
_TREND_CODE_BATCH = 500
_WeeklyBar = namedtuple("_WeeklyBar", "week_start close_last")

# トレンド判定の閾値（13週リターン）。表示だけに使う。
_TREND_UP   = 0.10    # +10% 以上 → 上昇
_TREND_DOWN = -0.10   # −10% 以下 → 下落
_MIN_WEEKS_FOR_TREND = 8   # 週次データがこれ未満なら trend=不明


def parse_holdings(text: str) -> tuple[list[dict], list[str]]:
    """保有入力テキストを構造化する。

    1 行 = `証券コード[, @取得単価][, 購入日]`。区切りはカンマ/空白いずれも可。
      例: "7203, @1500, 2024-03-10" / "6758 @12000" / "9984"
    取得単価は `@1500` でも `1500` でも可。`YYYY-MM-DD` 形式のトークンは購入日とみなす。

    Returns: (parsed, invalid)
      parsed  = [{"sec_code", "avg_cost"(float|None), "buy_date"(str|None)}, ...]
      invalid = 解釈できなかった行の原文リスト
    """
    parsed: list[dict] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = re.split(r"[,\s]+", line)
        code = tokens[0].strip().lstrip("#")
        if not re.fullmatch(r"\d{3,4}", code):
            invalid.append(line)
            continue
        if code in seen:        # 重複行はスキップ（先勝ち）
            continue
        seen.add(code)
        avg_cost = None
        buy_date = None
        for tok in tokens[1:]:
            tok = tok.strip()
            if not tok:
                continue
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
                buy_date = tok
                continue
            num = tok.lstrip("@¥\\")
            try:
                avg_cost = float(num)
            except ValueError:
                pass   # 不明トークンは無視（コードは有効なので行自体は採用）
        parsed.append({"sec_code": code, "avg_cost": avg_cost, "buy_date": buy_date})
    return parsed, invalid


def _compute_trend(weekly_rows: list) -> dict:
    """週次株価から価格トレンドを算出する（参考表示のみ・札には使わない）。

    weekly_rows: _WeeklyBar（同一 edinet_code・week_start と close_last の2フィールド）。
                 week_start 昇順でなくてよい。
    Returns: {"trend", "ret_13w"(%|None), "drawdown_52w"(%|None), "last_close"(float|None)}
    """
    series = sorted(
        ((r.week_start, r.close_last) for r in weekly_rows if r.close_last and r.close_last > 0),
        key=lambda x: x[0],
    )
    if len(series) < 2:
        return {"trend": "不明", "ret_13w": None, "drawdown_52w": None,
                "last_close": series[-1][1] if series else None}

    closes = [c for _, c in series]
    last_close = closes[-1]

    def _ago(n_weeks: int) -> float:
        idx = len(closes) - 1 - n_weeks
        return closes[idx] if idx >= 0 else closes[0]

    ret_13w = None
    if len(closes) >= _MIN_WEEKS_FOR_TREND:
        prev = _ago(13)
        if prev > 0:
            ret_13w = last_close / prev - 1.0

    high_52w = max(closes[-52:])
    drawdown_52w = (last_close / high_52w - 1.0) if high_52w > 0 else None

    if ret_13w is None:
        trend = "不明"
    elif ret_13w <= _TREND_DOWN:
        trend = "下落"
    elif ret_13w >= _TREND_UP:
        trend = "上昇"
    else:
        trend = "横ばい"

    return {
        "trend": trend,
        "ret_13w": round(ret_13w * 100, 1) if ret_13w is not None else None,
        "drawdown_52w": round(drawdown_52w * 100, 1) if drawdown_52w is not None else None,
        "last_close": last_close,
    }


def n_to_sell(n_ranked: int) -> int:
    """μ̂ がそろった保有 n_ranked 社のうち売る社数（下位 1/5・切り捨て）。

    5 社未満なら 0（下位 1/5 が 1 社に満たない）。実測の切り方（`in_layer_short_side`）と同じで、
    少ない保有に合わせて 1 社へ切り上げる規則は測っていないので足さない。
    """
    return n_ranked // SELL_QUANTILES


def assign_actions(mus: dict[str, float | None]) -> tuple[list[str], dict[str, str]]:
    """{キー: μ̂} から（並び順, {キー: 札}）を返す。

    μ̂ の昇順（同値はキーの昇順）に並べ、先頭 `n_to_sell` 社が SELL、残りが HOLD。μ̂ が None の
    キーは「データ不足」で末尾（入力順を保つ）。
    """
    ranked = sorted((k for k, v in mus.items() if v is not None), key=lambda k: (mus[k], k))
    missing = [k for k, v in mus.items() if v is None]
    n_sell = n_to_sell(len(ranked))
    actions = {k: ("SELL" if i < n_sell else "HOLD") for i, k in enumerate(ranked)}
    actions.update({k: "データ不足" for k in missing})
    return ranked + missing, actions


def choose_sell_keys(source: str, mus: dict[str, float | None], ec_by_key: dict[str, str | None],
                     s2: dict, mu_snapshot_date: str | None) -> tuple[dict, str, str | None]:
    """並びに使う {キー: 値} と、実際に使った出どころ・M-6 へ倒した理由を返す（#1202）。

    `source` が MU_SOURCE なら μ̂ のまま（理由 None）。`SELL_KEY_S2` なら `s2`（`database.get_sell_quantile_scores`
    の戻り）の q20 で並べる。ただし S2 の表が空か、S2 の断面の日付が M-6 の代表値（`mu_snapshot_date`）と違えば、
    保有全体を μ̂ で並べて理由を返す。S2 だけ欠けた保有は「データ不足」（μ̂ と混ぜない）。
    """
    if source == MU_SOURCE:
        return mus, MU_SOURCE, None
    if source != SELL_KEY_S2:
        raise ValueError(f"未知の並びのキーの出どころ: {source}（{MU_SOURCE} / {SELL_KEY_S2} のみ）")
    q20 = s2.get("q20") or {}
    if not q20:
        return mus, MU_SOURCE, "S2 の予測が保存されていない（未実行・全社同値の回・読めない）ので M-6 の μ̂ で並べた"
    s2_date = s2.get("snapshot_date")
    if not mu_snapshot_date or s2_date != mu_snapshot_date:
        return mus, MU_SOURCE, (f"S2 の予測の日付（{s2_date}）が M-6 の μ̂（{mu_snapshot_date}）と違う"
                                "ので M-6 の μ̂ で並べた")
    return {k: (q20.get(ec) if ec else None) for k, ec in ec_by_key.items()}, SELL_KEY_S2, None


class SellRankingPlugin(AnalysisPlugin):
    name = "sell_ranking"
    label = "売り候補ランキング"
    description = (
        "保有銘柄を M-6 の期待リターン（μ̂）の低い順に並べ、下位 1/5 に「売却」を付けます。"
        "売りの当たり具合を過去のデータで測って最も良かった手法だけを使います。"
    )
    depends_on = [MU_SOURCE]   # μ̂ が無いと並べられない（未実行なら runner が 400）
    category = "⑤ 保有を見直す"
    ui_order = 510

    def params_schema(self) -> dict:
        return {
            "holdings": {
                "type": "textarea",
                "dtype": "str",
                "label": "保有銘柄（1行ずつ: 証券コード[, @取得単価][, 購入日]）",
                "default": "",
                "description": "例: 7203, @1500, 2024-03-10 / 6758 @12000 / 9984",
            },
        }

    def execute(self, params: dict, db: Any) -> dict:
        import datetime as _dt

        from database import (FinancialMetric, StockPriceWeekly, get_producer_asof,
                              iso_week_start, latest_year_subq, tradable_filters)
        from plugins import get_plugin
        from plugins.utils import current_macro_snapshot

        holdings, invalid = parse_holdings(params["holdings"])
        if not holdings:
            return {"count": 0, "n_sell": 0, "n_hold": 0, "n_no_data": 0,
                    "results": [], "not_found": [], "invalid": invalid,
                    "mu_source": MU_SOURCE, "mu_available": False, "mu_asof": None,
                    "sell_key_source": MU_SOURCE, "sell_key_fallback": None}

        # ── μ̂（M-6 の producer）──────────────────────────────────────────────
        producer = get_plugin(MU_SOURCE)
        mu_scores: dict = {}
        if producer and producer.produced_output(db):
            mu_scores = producer.read_producer_scores(db, current_macro_snapshot(db))
        mu_available = bool(mu_scores)
        # μ̂ の as-of（代表値=中央値・最古・古い銘柄数）。μ̂ は銘柄ごとに「その銘柄の最終週次バー」
        # 時点なので 1 個の日付には潰れない（Issue #417）。
        mu_asof = get_producer_asof(db, MU_SOURCE) if mu_available else None

        # ── 保有銘柄の最新年度レコードを sec_code で引く ──
        subq = latest_year_subq(db, FinancialMetric)
        _cols = [getattr(FinancialMetric, c) for c in SELL_SELECT_COLS]
        codes = [h["sec_code"] for h in holdings]
        rows = {r.sec_code: r for r in (
            db.query(*_cols)
              .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                          (FinancialMetric.year == subq.c.max_year))
              .filter(FinancialMetric.sec_code.in_(codes))
              .all()
        )}

        # ── 価格トレンド（参考表示）: 保有 edinet_code をまとめて取得 ──
        # 3列＋400日下限＋500社チャンク（#482）。
        ecodes = [r.edinet_code for r in rows.values() if r.edinet_code]
        weekly_by_ec: dict[str, list] = {}
        if ecodes:
            week_from = iso_week_start(
                (_dt.date.today() - _dt.timedelta(days=_TREND_LOOKBACK_DAYS)).isoformat())
            for i in range(0, len(ecodes), _TREND_CODE_BATCH):
                chunk = ecodes[i:i + _TREND_CODE_BATCH]
                for ec, ws, cl in (db.query(StockPriceWeekly.edinet_code,
                                            StockPriceWeekly.week_start,
                                            StockPriceWeekly.close_last)
                                     .filter(StockPriceWeekly.edinet_code.in_(chunk),
                                             StockPriceWeekly.week_start >= week_from)
                                     .all()):
                    weekly_by_ec.setdefault(ec, []).append(_WeeklyBar(ws, cl))

        not_found: list[str] = []
        by_code: dict[str, dict] = {}
        for h in holdings:
            r = rows.get(h["sec_code"])
            if r is None:
                not_found.append(h["sec_code"])
                continue
            ps = mu_scores.get(r.edinet_code) if r.edinet_code else None
            mu = ps.get("mu") if ps else None
            mom = _compute_trend(weekly_by_ec.get(r.edinet_code, []))
            last_close = mom["last_close"] if mom["last_close"] is not None else r.stock_price
            pnl_pct = None
            if h["avg_cost"] and h["avg_cost"] > 0 and last_close:
                pnl_pct = round((last_close - h["avg_cost"]) / h["avg_cost"] * 100, 1)
            by_code[r.sec_code] = {
                "sec_code":     r.sec_code,
                "edinet_code":  r.edinet_code,
                "company_name": r.company_name,
                "industry":     r.industry,
                "year":         r.year,
                "mu":           round(float(mu), 4) if mu is not None else None,
                "trend":        mom["trend"],
                "ret_13w":      mom["ret_13w"],
                "drawdown_52w": mom["drawdown_52w"],
                "last_close":   round(last_close, 1) if last_close else None,
                "avg_cost":     h["avg_cost"],
                "buy_date":     h["buy_date"],
                "pnl_pct":      pnl_pct,
                # 保有銘柄はユーザー入力のため is_active では除外しないが、上場廃止済みなら
                # 明示的に知らせる（Issue #315・ゾンビ化した保有への気付き）。
                "is_active":     r.is_active is not False,
                "delisted_date": r.delisted_date.isoformat() if r.delisted_date else None,
            }

        # 並びのキー（#1202）: 既定は μ̂。S2 のときは表と日付を確かめ、だめなら保有全体を μ̂ で並べる
        s2 = {}
        if SELL_KEY_SOURCE == SELL_KEY_S2:
            from database import get_sell_quantile_scores
            s2 = get_sell_quantile_scores(db)
        keys, key_source, key_fallback = choose_sell_keys(
            SELL_KEY_SOURCE, {c: s["mu"] for c, s in by_code.items()},
            {c: s["edinet_code"] for c, s in by_code.items()}, s2,
            (mu_asof or {}).get("snapshot_date"))
        if key_source == SELL_KEY_S2:
            for c, s in by_code.items():
                s["q20"] = round(float(keys[c]), 4) if keys[c] is not None else None
        order, actions = assign_actions(keys)
        scored = [by_code[c] for c in order]
        for rank, item in enumerate(scored, 1):
            item["rank"] = rank
            item["action"] = actions[item["sec_code"]]

        # TOPIX 移行措置・新規採用の事実の注記（#1013）。判定と順位を決めた後に付けるだけ。
        # 保有だけでは比率の分母にならないので、分母は今投資できる全社（tradable・最新年度）。
        universe = [ec for (ec,) in (
            db.query(FinancialMetric.edinet_code)
              .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                          (FinancialMetric.year == subq.c.max_year))
              .filter(*tradable_filters(db))
              .all())]
        from index_event_notes import annotate
        notes, index_events = annotate(
            db, [s["edinet_code"] for s in scored], universe, what="保有")
        for s in scored:
            s["index_event"] = notes.get(s["edinet_code"])

        return {
            "count":         len(scored),
            "n_sell":        sum(1 for s in scored if s["action"] == "SELL"),
            "n_hold":        sum(1 for s in scored if s["action"] == "HOLD"),
            "n_no_data":     sum(1 for s in scored if s["action"] == "データ不足"),
            "mu_source":     MU_SOURCE,
            "mu_available":  mu_available,
            "mu_asof":       mu_asof,
            # 実際に並べたキーの出どころ（#1202・既定は M-6）と、S2 から M-6 へ倒した理由（倒していなければ None）
            "sell_key_source":   key_source,
            "sell_key_fallback": key_fallback,
            "results":       scored,
            "not_found":     not_found,
            "invalid":       invalid,
            # 台帳の有無・保有のうちの件数・母集団での比率・表の上の 1 行・バナー（#1013）
            "index_events":  index_events,
        }


plugin = SellRankingPlugin()
