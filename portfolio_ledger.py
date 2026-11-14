"""売買の記録（`trade_ledger`）から保有・損益・期間リターンを導出する（#1222）。

運用の成功指標（docs/VISION.md「運用の成功指標」）を測る層。**予測ではなく、実際に持った銘柄の成績**を出す。

## 2 つのブロック

- **純関数ブロック**（`validate_entry` 〜 `performance`）: database・scripts を import しない。入力は dict と
  list だけで、テストは作り物の値で書ける。
- **DB ブロック**（`load_*`・`summary`）: 表を読んで純関数へ渡す。**書かない**（書くのは
  `database.insert_trade_ledger_entry` だけで、口は収集ルーターの下）。

## 株価の水準（分割）

DB の株価（`stock_price_weekly` / `stock_price_daily`）は Yahoo の遡及調整済みで、分割があると過去の値も
**今の水準**に直る。一方、記録は約定当時の生の値。そこで分割を 1 行（`side="split"`）として記録してもらい、
`to_current_scale` がその行より前の売買を今の水準へ直してから計算する（株数 × 比率・単価 ÷ 比率）。
分割の行が無いまま DB の株価だけが直ると損益が黙って食い違うので、`split_warnings` が公式の係数
（`jquants_adj_factor_events`）と突き合わせて知らせる。**自動では補正しない**（係数の表は全期間・全社を
覆っている保証が無い＝覆っていない所で黙って間違う）。

## 期間リターンの出し方

週ごとに、保有の評価額から入出金の影響を除いた増減をつなぐ（時間加重）。その週の買いは週の初め、売りは
週の終わりにあったとみなす:

    r_w = (V_w + 売りの受取 − V_{w−1} − 買いの支払) / (V_{w−1} + 買いの支払)

こうすると、買った値段から週末の終値までの値動きも、売った値段までの値動きも落とさない。保有も買いも
無い週は飛ばす。TOPIX（`BENCHMARK_SERIES`）は**同じ週だけ**をつないで比べる（保有していない週の値動きは
どちらにも入れない）。週末の終値で測るので、週の途中の下落は見えない（日次は直近 6 か月しか持たない）。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# 行の種類。`void` は別の行（`voids_id`）を取り消す行で、取り消された行と一緒に計算から外れる。
SIDE_BUY, SIDE_SELL, SIDE_SPLIT, SIDE_VOID = "buy", "sell", "split", "void"
SIDES = (SIDE_BUY, SIDE_SELL, SIDE_SPLIT, SIDE_VOID)

# 比べる相手（docs/VISION.md）。`macro_data` の系列名で、中身は TOPIX 連動 ETF の 1306.T（配当なし）。
BENCHMARK_SERIES = "TOPIX"

# 証券コード: 数字 4 桁か、2024 年以降の英字入り（例 130A）。3 桁は 0 を補って 4 桁にする。
_SEC_CODE_RE = re.compile(r"^[0-9][0-9A-Z]{3}$")
MEMO_MAX = 200

# 株数の端数（分割で生じる）を「持っていない」とみなす閾値。
QTY_EPS = 1e-9

# 公式の分割の日付と、記録の分割の行の日付のずれをどこまで同じ分割とみなすか（日）。
# 公式は権利落ち日、利用者が書くのは効力発生日や証券会社の反映日になりうる。
SPLIT_MATCH_DAYS = 10
# 公式の係数が 1 からこれ以上離れていれば分割・併合とみなす（配当落ちなどの微小な調整を拾わない）。
SPLIT_FACTOR_TOL = 0.01


# ── 純関数ブロック ────────────────────────────────────────────────────────────

def normalize_sec_code(raw) -> str:
    """証券コードを 4 桁の大文字へそろえる。形が合わなければ ValueError。"""
    code = str(raw or "").strip().upper()
    if code.isdigit() and len(code) == 3:
        code = "0" + code
    if not _SEC_CODE_RE.match(code):
        raise ValueError(f"証券コードの形が違います: {raw!r}（数字 4 桁か、130A のような 4 文字）")
    return code


def _iso_date(raw, label: str) -> str:
    try:
        return date.fromisoformat(str(raw or "")[:10]).isoformat()
    except ValueError:
        raise ValueError(f"{label}は YYYY-MM-DD で指定してください: {raw!r}") from None


def _positive(raw, label: str) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label}は数値で指定してください: {raw!r}") from None
    if not (v > 0) or v != v or v == float("inf"):
        raise ValueError(f"{label}は 0 より大きい数で指定してください: {raw!r}")
    return v


def validate_entry(raw: dict, today: date | None = None) -> dict:
    """入力 1 行を検証して、`TradeLedgerEntry` へそのまま渡せる dict を返す（違反は ValueError）。

    種類ごとに要る項目だけを残し、要らない項目は None にする（買いの行に分割の比率が残らない）。
    `void` は `voids_id` だけを見る——取り消す相手が在るか・取り消せる行かは `check_against_ledger`。
    """
    side = str(raw.get("side") or "").strip().lower()
    if side not in SIDES:
        raise ValueError(f"売買の別が違います: {raw.get('side')!r}（{' / '.join(SIDES)}）")
    memo = str(raw.get("memo") or "").strip() or None
    if memo and len(memo) > MEMO_MAX:
        raise ValueError(f"メモは {MEMO_MAX} 文字までです")
    out = {"side": side, "sec_code": None, "trade_date": None, "price": None, "quantity": None,
           "split_ratio": None, "fee": None, "voids_id": None, "memo": memo}

    if side == SIDE_VOID:
        try:
            out["voids_id"] = int(raw.get("voids_id"))
        except (TypeError, ValueError):
            raise ValueError("取り消す行の番号（voids_id）を指定してください") from None
        return out

    out["sec_code"] = normalize_sec_code(raw.get("sec_code"))
    out["trade_date"] = _iso_date(raw.get("trade_date"), "日付")
    if date.fromisoformat(out["trade_date"]) > (today or date.today()):
        raise ValueError(f"日付が未来です: {out['trade_date']}")

    if side == SIDE_SPLIT:
        ratio = _positive(raw.get("split_ratio"), "分割の比率")
        if abs(ratio - 1.0) < 1e-12:
            raise ValueError("分割の比率が 1 です（1 対 2 の分割なら 2、2 対 1 の併合なら 0.5）")
        out["split_ratio"] = ratio
        return out

    out["price"] = _positive(raw.get("price"), "単価")
    out["quantity"] = _positive(raw.get("quantity"), "株数")
    fee = raw.get("fee")
    if fee in (None, ""):
        out["fee"] = 0.0
    else:
        try:
            out["fee"] = float(fee)
        except (TypeError, ValueError):
            raise ValueError(f"手数料は数値で指定してください: {fee!r}") from None
        if out["fee"] < 0 or out["fee"] != out["fee"]:
            raise ValueError("手数料は 0 以上で指定してください")
    return out


def effective_entries(rows: list[dict]) -> list[dict]:
    """取消の行と、取り消された行を外して、日付・id の順に並べる。

    取消の行を取り消すことはできない（`check_against_ledger` が弾く）ので、取消は 1 段だけ見ればよい。
    """
    voided = {r["voids_id"] for r in rows if r["side"] == SIDE_VOID and r.get("voids_id") is not None}
    live = [r for r in rows if r["side"] != SIDE_VOID and r["id"] not in voided]
    return sorted(live, key=lambda r: (r["trade_date"], r["id"]))


def to_current_scale(entries: list[dict]) -> list[dict]:
    """売買の株数・単価を**今の水準**（DB の株価と同じ水準）へ直す。分割の行は落とす。

    ある売買より**後の日付**の分割の比率をすべて掛ける（株数 × 比率・単価 ÷ 比率）。同じ日の分割は
    その日の売買より前に効いたとみなす（分割の効力日の約定は、分割後の株数・単価で書かれる）。
    入力は `effective_entries` の戻り。金額（単価 × 株数）と手数料は変わらない。
    """
    splits: dict[str, list[tuple[str, float]]] = {}
    for e in entries:
        if e["side"] == SIDE_SPLIT:
            splits.setdefault(e["sec_code"], []).append((e["trade_date"], e["split_ratio"]))
    out = []
    for e in entries:
        if e["side"] == SIDE_SPLIT:
            continue
        factor = 1.0
        for d, ratio in splits.get(e["sec_code"], ()):
            if d > e["trade_date"]:
                factor *= ratio
        out.append(dict(e, quantity=e["quantity"] * factor, price=e["price"] / factor))
    return out


def positions(entries: list[dict], as_of: str | None = None) -> tuple[dict, list[str]]:
    """今の水準の売買（`to_current_scale` の戻り）から、銘柄ごとの保有と実現損益を出す。

    戻り値は `({sec_code: {quantity, avg_cost, realized_pnl, buy_date}}, problems)`。
    - 取得単価は移動平均で、買いの手数料を含める。売りの手数料は実現損益から引く
    - `buy_date` は今の保有を持ち始めた日（全部売ってから買い直したら、買い直した日）
    - 持っている株数を超える売りは、持っている分だけ売ったことにして `problems` へ書く（落とさない）
    - `as_of` を渡すと、その日までの売買だけで計算する
    """
    book: dict[str, dict] = {}
    problems: list[str] = []
    for e in entries:
        if as_of is not None and e["trade_date"] > as_of:
            continue
        p = book.setdefault(e["sec_code"],
                            {"quantity": 0.0, "avg_cost": None, "realized_pnl": 0.0, "buy_date": None})
        qty, fee = e["quantity"], e.get("fee") or 0.0
        if e["side"] == SIDE_BUY:
            held_cost = (p["avg_cost"] or 0.0) * p["quantity"]
            if p["quantity"] <= QTY_EPS:
                p["buy_date"] = e["trade_date"]
            p["quantity"] += qty
            p["avg_cost"] = (held_cost + e["price"] * qty + fee) / p["quantity"]
        else:
            if qty > p["quantity"] + QTY_EPS:
                problems.append(
                    f"{e['sec_code']} の {e['trade_date']} の売り（{qty:g} 株）が、持っている株数"
                    f"（{p['quantity']:g} 株）を超えています。分割の行が抜けていないか確かめてください")
                qty = p["quantity"]
            if qty > QTY_EPS:
                p["realized_pnl"] += (e["price"] - p["avg_cost"]) * qty - fee
                p["quantity"] -= qty
            if p["quantity"] <= QTY_EPS:
                p["quantity"], p["avg_cost"], p["buy_date"] = 0.0, None, None
    return book, problems


def check_against_ledger(entry: dict, rows: list[dict]) -> None:
    """検証済みの 1 行を、今ある記録に足してよいかを確かめる（違反は ValueError）。

    - 取消: 相手が在る・取消の行ではない・まだ取り消されていない
    - どの行も: 足した後の記録で、売りが持っている株数を超えない（買い・分割の取消や、日付を遡った
      売り・併合で、後の売りが宙に浮くのを防ぐ）
    """
    if entry["side"] == SIDE_VOID:
        target = next((r for r in rows if r["id"] == entry["voids_id"]), None)
        if target is None:
            raise ValueError(f"取り消す行が見つかりません: {entry['voids_id']}")
        if target["side"] == SIDE_VOID:
            raise ValueError("取消の行は取り消せません（正しい内容の行を足してください）")
        if any(r["side"] == SIDE_VOID and r.get("voids_id") == target["id"] for r in rows):
            raise ValueError(f"その行はすでに取り消されています: {target['id']}")
    trial = effective_entries(rows + [dict(entry, id=max((r["id"] for r in rows), default=0) + 1)])
    _, problems = positions(to_current_scale(trial))
    if problems:
        raise ValueError(problems[0])


def prepare_entry(raw: dict, rows: list[dict], today: date | None = None) -> dict:
    """入力 1 行を、表へ書ける形にして返す（検証 → 記録との照合 → 取消の行の補完・違反は ValueError）。

    書く口はこれだけを呼ぶ。取消の行には、取り消す相手の証券コードと日付を写す（表の必須列を満たし、
    銘柄で絞ったときに取消も一緒に見える）。
    """
    entry = validate_entry(raw, today)
    check_against_ledger(entry, rows)
    if entry["side"] == SIDE_VOID:
        target = next(r for r in rows if r["id"] == entry["voids_id"])
        entry["sec_code"], entry["trade_date"] = target["sec_code"], target["trade_date"]
    return entry


def week_start(day: str) -> str:
    """'YYYY-MM-DD' → その週の月曜（`database.iso_week_start` と同じ・純関数ブロックに置くための写し）。"""
    d = date.fromisoformat(day[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def _weeks_between(first: str, last: str) -> list[str]:
    out, d, end = [], date.fromisoformat(first), date.fromisoformat(last)
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def _max_drawdown(index: list[float]) -> float:
    """指数の列の最大下落（0 以下の比率）。空なら 0。"""
    peak, worst = None, 0.0
    for v in index:
        peak = v if peak is None or v > peak else peak
        worst = min(worst, v / peak - 1.0)
    return worst


def performance(entries: list[dict], prices: dict[str, dict[str, float]],
                bench: dict[str, float]) -> dict:
    """今の水準の売買から、週ごとの時間加重リターンと TOPIX との比較を出す。

    - `entries`: `to_current_scale` の戻り
    - `prices`: {sec_code: {週の月曜: その週の最後の終値}}
    - `bench`: {週の月曜: その週の最後の終値}（比べる相手）

    株価が無い週は直前の週の値で埋め、一度も無ければ取得単価で評価して `problems` へ書く。比べる相手の
    値が前後どちらかの週に無い週は、相手のリターンを 0 として `bench_missing_weeks` に数える（黙って
    落とすと、相手だけ週が少なくなる）。
    """
    empty = {"weeks": [], "months": [], "total_return": None, "bench_return": None, "excess": None,
             "max_drawdown": None, "bench_max_drawdown": None, "n_weeks": 0,
             "bench_missing_weeks": 0, "problems": []}
    if not entries:
        return empty
    last_candidates = [w for series in prices.values() for w in series] + list(bench)
    first_week = week_start(entries[0]["trade_date"])
    last_week = max(last_candidates + [week_start(entries[-1]["trade_date"])])
    by_week: dict[str, list[dict]] = {}
    for e in entries:
        by_week.setdefault(week_start(e["trade_date"]), []).append(e)

    qty: dict[str, float] = {}
    cost: dict[str, float] = {}       # 株価が一度も無いときの評価に使う直近の約定単価
    last_px: dict[str, float] = {}
    problems: set[str] = set()
    weeks_out, idx, bidx = [], 1.0, 1.0
    prev_value, bench_missing = 0.0, 0
    # 最初の週のリターンには、その前の週の相手の値が要る（`load_benchmark` が 1 週前から読む）。
    before = [w for w in bench if w < first_week]
    prev_bench = bench[max(before)] if before else None

    for w in _weeks_between(first_week, last_week):
        bought = sold = 0.0
        for e in by_week.get(w, ()):
            amount = e["price"] * e["quantity"]
            fee = e.get("fee") or 0.0
            cost[e["sec_code"]] = e["price"]
            if e["side"] == SIDE_BUY:
                qty[e["sec_code"]] = qty.get(e["sec_code"], 0.0) + e["quantity"]
                bought += amount + fee
            else:
                qty[e["sec_code"]] = max(0.0, qty.get(e["sec_code"], 0.0) - e["quantity"])
                sold += amount - fee
        value = 0.0
        for code, q in qty.items():
            if q <= QTY_EPS:
                continue
            px = prices.get(code, {}).get(w)
            if px is not None:
                last_px[code] = px
            elif code not in last_px:
                problems.add(f"{code} の株価が無いため、約定単価で評価しています")
            value += q * last_px.get(code, cost[code])

        bench_now = bench.get(w)
        base = prev_value + bought
        if base > 0:
            r = (value + sold - prev_value - bought) / base
            if bench_now is not None and prev_bench is not None:
                br = bench_now / prev_bench - 1.0
            else:
                br, bench_missing = 0.0, bench_missing + 1
            idx, bidx = idx * (1.0 + r), bidx * (1.0 + br)
            weeks_out.append({"week": w, "value": value, "ret": r, "bench_ret": br,
                              "index": idx, "bench_index": bidx})
        prev_value = value
        if bench_now is not None:
            prev_bench = bench_now

    if not weeks_out:
        return dict(empty, problems=sorted(problems))

    months, m_idx, m_bidx = [], 1.0, 1.0
    for i, wk in enumerate(weeks_out):
        nxt = weeks_out[i + 1]["week"][:7] if i + 1 < len(weeks_out) else None
        if nxt != wk["week"][:7]:
            months.append({"month": wk["week"][:7],
                           "ret": wk["index"] / m_idx - 1.0,
                           "bench_ret": wk["bench_index"] / m_bidx - 1.0,
                           "excess": wk["index"] / m_idx - wk["bench_index"] / m_bidx})
            m_idx, m_bidx = wk["index"], wk["bench_index"]

    total, btotal = weeks_out[-1]["index"] - 1.0, weeks_out[-1]["bench_index"] - 1.0
    return {
        "weeks": weeks_out, "months": months,
        "total_return": total, "bench_return": btotal, "excess": total - btotal,
        "max_drawdown": _max_drawdown([1.0] + [w["index"] for w in weeks_out]),
        "bench_max_drawdown": _max_drawdown([1.0] + [w["bench_index"] for w in weeks_out]),
        "n_weeks": len(weeks_out), "bench_missing_weeks": bench_missing,
        "problems": sorted(problems),
    }


def split_warnings(live: list[dict], official: dict[str, list[tuple[str, float]]]) -> list[str]:
    """公式の分割の記録があるのに、記録に分割の行が無い保有を知らせる。

    - `live`: `effective_entries` の戻り（分割の行を含む・生の値）
    - `official`: {sec_code: [(日付, 過去の株価に掛ける係数)]}（1 対 2 の分割なら 0.5）

    見るのは**今持っている銘柄**の、持ち始めた日より後の公式の記録だけ。公式の係数の逆数と記録の比率は
    突き合わせない（日付が近い分割の行があれば書いてあるとみなす）——比率の書き間違いは、売りが
    持ち株を超えたとき・損益が跳ねたときに別の形で見える。
    """
    book, _ = positions(to_current_scale(live))
    recorded: dict[str, list[date]] = {}
    for e in live:
        if e["side"] == SIDE_SPLIT:
            recorded.setdefault(e["sec_code"], []).append(date.fromisoformat(e["trade_date"]))
    out = []
    for code, p in sorted(book.items()):
        if p["quantity"] <= QTY_EPS:
            continue
        for day, factor in sorted(official.get(code, ())):
            if day <= p["buy_date"] or abs(factor - 1.0) < SPLIT_FACTOR_TOL:
                continue
            d = date.fromisoformat(day)
            if any(abs((d - r).days) <= SPLIT_MATCH_DAYS for r in recorded.get(code, ())):
                continue
            out.append(
                f"{code}: {day} に公式の分割の記録があります（1 株が {1.0 / factor:g} 株）。"
                "分割の行を足さないと、株数と損益が食い違います")
    return out


# ── DB ブロック（読むだけ）────────────────────────────────────────────────────

def load_rows(db) -> list[dict]:
    """記録を全行、id の順で dict にして返す（取消の行も含む）。"""
    from database import TradeLedgerEntry as T
    cols = ("id", "sec_code", "side", "trade_date", "price", "quantity", "split_ratio", "fee",
            "voids_id", "memo", "created_at")
    return [{c: getattr(r, c) for c in cols} for r in db.query(T).order_by(T.id).all()]


def _companies(db, sec_codes: list[str]) -> dict[str, dict]:
    """証券コード → {edinet_code, name}。同じ証券コードが複数あれば上場中を優先する。"""
    from database import Company
    if not sec_codes:
        return {}
    rows = (db.query(Company.sec_code, Company.edinet_code, Company.name, Company.is_active)
            .filter(Company.sec_code.in_(sec_codes)).all())
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (not r.is_active, r.edinet_code)):
        out.setdefault(r.sec_code, {"edinet_code": r.edinet_code, "name": r.name})
    return out


def load_weekly_prices(db, edinet_by_sec: dict[str, str], since_week: str) -> dict[str, dict[str, float]]:
    from database import StockPriceWeekly as W
    if not edinet_by_sec:
        return {}
    sec_by_edinet = {v: k for k, v in edinet_by_sec.items()}
    rows = (db.query(W.edinet_code, W.week_start, W.close_last)
            .filter(W.edinet_code.in_(list(sec_by_edinet)), W.week_start >= since_week).all())
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(sec_by_edinet[r.edinet_code], {})[r.week_start] = r.close_last
    return out


def load_benchmark(db, since_week: str) -> dict[str, float]:
    """比べる相手の、週ごとの最後の終値。1 週前から読む（最初の週のリターンに前の週の値が要る）。"""
    from database import MacroData as M
    start = (date.fromisoformat(since_week) - timedelta(days=7)).isoformat()
    rows = (db.query(M.trade_date, M.close)
            .filter(M.series_code == BENCHMARK_SERIES, M.trade_date >= start)
            .order_by(M.trade_date).all())
    out: dict[str, float] = {}
    for r in rows:
        out[week_start(r.trade_date)] = r.close      # 日付の昇順なので、週の最後の値が残る
    return out


def load_official_splits(db, edinet_by_sec: dict[str, str]) -> dict[str, list[tuple[str, float]]]:
    from database import JQuantsAdjFactorEvent as J
    if not edinet_by_sec:
        return {}
    sec_by_edinet = {v: k for k, v in edinet_by_sec.items()}
    rows = (db.query(J.edinet_code, J.event_date, J.adj_factor)
            .filter(J.edinet_code.in_(list(sec_by_edinet))).all())
    out: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        out.setdefault(sec_by_edinet[r.edinet_code], []).append((r.event_date, r.adj_factor))
    return out


def summary(db) -> dict:
    """保有・損益・期間リターン・注意をまとめて返す（読むだけ）。

    `performance.weeks` は長くなるので、画面が要る列だけに絞らず全部返す（週 1 行・数年でも数百行）。
    """
    from database import latest_prices

    rows = load_rows(db)
    live = effective_entries(rows)
    scaled = to_current_scale(live)
    book, problems = positions(scaled)
    sec_codes = sorted({e["sec_code"] for e in live})
    comp = _companies(db, sec_codes)
    edinet_by_sec = {s: c["edinet_code"] for s, c in comp.items()}
    unknown = [s for s in sec_codes if s not in comp]
    if unknown:
        problems.append("企業の表に無い証券コードです（株価を読めません）: " + "・".join(unknown))

    latest = latest_prices(db, list(edinet_by_sec.values()))
    holdings, realized, unrealized = [], 0.0, 0.0
    for code in sorted(book):
        p = book[code]
        realized += p["realized_pnl"]
        if p["quantity"] <= QTY_EPS:
            continue
        quote = latest.get(edinet_by_sec.get(code)) or {}
        px = quote.get("price")
        pnl = (px - p["avg_cost"]) * p["quantity"] if px is not None else None
        unrealized += pnl or 0.0
        holdings.append({
            "sec_code": code, "name": comp.get(code, {}).get("name"),
            "quantity": p["quantity"], "avg_cost": p["avg_cost"], "buy_date": p["buy_date"],
            "last_close": px, "last_close_date": quote.get("date"),
            "market_value": px * p["quantity"] if px is not None else None,
            "unrealized_pnl": pnl,
            "unrealized_pct": (px / p["avg_cost"] - 1.0) * 100 if px is not None else None,
        })

    prices, bench = {}, {}
    if scaled:
        since = week_start(scaled[0]["trade_date"])
        prices, bench = load_weekly_prices(db, edinet_by_sec, since), load_benchmark(db, since)
    perf = performance(scaled, prices, bench)
    return {
        "holdings": holdings,
        "realized_pnl": realized, "unrealized_pnl": unrealized,
        "performance": perf,
        "benchmark": BENCHMARK_SERIES,
        "warnings": split_warnings(live, load_official_splits(db, edinet_by_sec)),
        "problems": problems + perf["problems"],
        "n_entries": len(live),
    }
