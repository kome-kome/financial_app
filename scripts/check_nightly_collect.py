"""夜間バッチの収集ログを晩ごとに並べて読む（#556 の並行フェッチ・#620 のスケール選別）。

## なぜログだけを見るのか

見たい値は**すべて `.logs/nightly_*.log` に出ている**（`_pipeline_incremental.py` が
毎晩そこへ書く）。DB を引くと「今の値」しか分からず、**その晩に何が起きたか**は残らない。
ロールアウトの確認は「前の晩と比べて悪化していないか」なので、晩ごとの記録の方が要る。

DB にもネットワークにも触らない＝いつ叩いても副作用が無い。

## 1回の実測を基準線にしない

同じ逐次実装のまま、夜ごとに 0.646 → 0.936 s/社と **+45% 振れた前例がある**（#556）。
だから既定で3晩ぶんを並べ、増減そのものは**警告にしない**。曜日も併記する——平日の
gap-fill は前営業日バーの唯一の取得者で全社が対象になるが、週末は既に追いついている社が
多く母数が桁で違う（実測 9/5(土) 442社 に対し 9/7(月) 4078社）。曜日を見ずに社数を
比べると、実装の効果ではなく暦を測ることになる。

## 「0」と「不明」を混ぜない

`スケール不一致で不採用 N行` は **0 のとき行ごと出ない**（`_pipeline_incremental.py` の
条件付き連結）。行が無いことを 0 と読むと、ログ書式が変わったときに静かに健全へ倒れる。
そこで **catchup 行が有るのに記載が無ければ 0、catchup 行そのものが無ければ `None`（不明）**
として区別する。表では不明を `-` で出す。

実行

    python -m scripts.check_nightly_collect              # 直近3晩
    python -m scripts.check_nightly_collect --nights 5
    python -m scripts.check_nightly_collect --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector_utils import force_utf8_stdout   # noqa: E402
from scripts._textwidth import display_width, pad   # noqa: E402

LOG_DIR = Path(__file__).resolve().parents[1] / ".logs"
LOG_GLOB = "nightly_*.log"
WEEKDAY_JA = "月火水木金土日"

# ── ログ行のパターン（文言は `_pipeline_incremental.py` / `collector_prices.py` が実際に
#     出しているものだけ。ここで表現を変えると黙って読めなくなるので写さず合わせる）──
RE_STAMP      = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO")
RE_GAP_START  = re.compile(r"fill_recent_stock_price_gap_yahoo: (\d+)/(\d+)社を補完")
# `新規日付 N件` の後ろは #622 で「・並行度 …・HTTP失敗 …」が続くようになった。
# **閉じ括弧まで要求すると新形式を黙って読み落とす**（実測: 2026-09-08 の実ログで
# 投入行数・所要が丸ごと `-` になり、「収集が終わっていない可能性」という偽の警告が出た）。
RE_GAP_END    = re.compile(r"fill_recent_stock_price_gap_yahoo: (\d+)件を株価テーブルへ"
                           r"集約保存（うち新規日付 (\d+)件")
RE_GAP_SKIP   = re.compile(r"Yahoo Finance gap-fill: スキップ（(.+?)・基準セッション")
RE_PRICELESS  = re.compile(r"価格ゼロ (\d+)社（うち解決済み (\d+)社）")
RE_REJECTED   = re.compile(r"解決済みなのに空 (\d+)社")
# `（うち404=N）` は #556 で足した内訳。**任意グループにする**——無い晩（旧書式）は
# 404 の内訳が「不明」であって 0 ではない。
RE_HTTP       = re.compile(r"Yahoo 並行度 (\d+)・HTTP失敗 "
                           r"429=(\d+) 5xx=(\d+) 4xx=(\d+)(?:（うち404=(\d+)）)? その他=(\d+)")
RE_CATCHUP    = re.compile(r"J-Quants catchup \((.+?)〜(.+?)\): (\d+)件 upsert")
RE_MISMATCH   = re.compile(r"スケール不一致で不採用 (\d+)行（(\d+)社）")
RE_RT_HIT     = re.compile(r"\*\*往復段差 (\d+)社\*\*")
# #644 で括弧の中に「・判定済みの非該当 N帯を除外」が続くようになった。**閉じ括弧まで
# 要求すると新形式を黙って読み落とす**（#622 の `RE_GAP_END` と同じ轍）。
RE_RT_NONE    = re.compile(r"往復段差: なし（調整差のある (\d+)社を検査")
RE_RT_EXCLUDED = re.compile(r"判定済みの非該当 (\d+)帯を除外")
RE_RT_NOTGT   = re.compile(r"往復段差: 検査対象なし")
RE_RT_FAIL    = re.compile(r"往復段差の検知に失敗")
RE_FRESH      = re.compile(r"株価鮮度: p50=(\S+) / p05=(\S+) / max=(\S+) / level=(\S+)"
                           r"（(\d+)銘柄・5営業日超の遅れ (\d+)銘柄）")


# ── 純関数（ファイルにもネットワークにも触らない・ここがテスト対象）────────────

def _stamp(line: str):
    m = RE_STAMP.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None


def parse_nightly_log(text: str) -> dict:
    """夜間ログ1本から収集の指標を抜く。**取れなかった項目は `None`（0 ではない）**。"""
    r: dict = {
        "gap_target": None, "gap_universe": None, "gap_skip_reason": None,
        "upserted": None, "new_rows": None, "gap_minutes": None,
        "priceless": None, "priceless_resolved": None, "exchange_rejected": None,
        "concurrency": None, "http_429": None, "http_5xx": None,
        "http_4xx": None, "http_404": None, "http_other": None,
        "catchup_upserted": None, "scale_mismatch_rows": None,
        "scale_mismatch_companies": None,
        "roundtrip": None, "roundtrip_companies": None, "roundtrip_excluded": None,
        "fresh_p50": None, "fresh_p05": None, "fresh_max": None,
        "fresh_level": None, "fresh_codes": None, "fresh_stale5d": None,
    }
    t_start = t_end = None
    catchup_without_mismatch = False

    for line in text.splitlines():
        if (m := RE_GAP_START.search(line)):
            r["gap_target"], r["gap_universe"] = int(m.group(1)), int(m.group(2))
            t_start = t_start or _stamp(line)
        if (m := RE_GAP_END.search(line)):
            r["upserted"], r["new_rows"] = int(m.group(1)), int(m.group(2))
            t_end = _stamp(line) or t_end
        if (m := RE_GAP_SKIP.search(line)):
            r["gap_skip_reason"] = m.group(1)
        if (m := RE_PRICELESS.search(line)):
            r["priceless"], r["priceless_resolved"] = int(m.group(1)), int(m.group(2))
            # この行が出た時点で「解決済みなのに空」は 0 が既定（非0のときだけ連結される）。
            r["exchange_rejected"] = r["exchange_rejected"] or 0
        if (m := RE_REJECTED.search(line)):
            r["exchange_rejected"] = int(m.group(1))
        if (m := RE_HTTP.search(line)):
            r["concurrency"] = int(m.group(1))
            r["http_429"], r["http_5xx"] = int(m.group(2)), int(m.group(3))
            r["http_4xx"] = int(m.group(4))
            r["http_404"] = int(m.group(5)) if m.group(5) is not None else None
            r["http_other"] = int(m.group(6))
        if (m := RE_CATCHUP.search(line)):
            r["catchup_upserted"] = int(m.group(3))
            if (m2 := RE_MISMATCH.search(line)):
                r["scale_mismatch_rows"] = int(m2.group(1))
                r["scale_mismatch_companies"] = int(m2.group(2))
            else:
                catchup_without_mismatch = True
        if (m := RE_RT_HIT.search(line)):
            r["roundtrip"], r["roundtrip_companies"] = "検出", int(m.group(1))
        elif RE_RT_NONE.search(line):
            r["roundtrip"], r["roundtrip_companies"] = "なし", 0
        elif RE_RT_NOTGT.search(line):
            r["roundtrip"], r["roundtrip_companies"] = "検査対象なし", 0
        elif RE_RT_FAIL.search(line):
            r["roundtrip"] = "検知失敗"
        # 除いた帯の数は 0 でも必ず出る（#644）＝記載の無い晩は記録が入る前の書式で「不明」
        if (m := RE_RT_EXCLUDED.search(line)):
            r["roundtrip_excluded"] = int(m.group(1))
        if (m := RE_FRESH.search(line)):
            r["fresh_p50"], r["fresh_p05"] = m.group(1), m.group(2)
            r["fresh_max"], r["fresh_level"] = m.group(3), m.group(4)
            r["fresh_codes"], r["fresh_stale5d"] = int(m.group(5)), int(m.group(6))

    # `スケール不一致で不採用 N行` は **0 件のとき行ごと出ない**（catchup 行への条件付き連結）。
    # 行の不在を 0 と読むと、#620 以前のログ——**選別そのものが存在しない晩**——まで
    # 「0件＝健全」に見えてしまう。手掛かりは往復段差の行で、**#620 で同じ PR に入った1組**
    # だから、それが出ている晩は選別も走っている＝記載が無ければ本物の 0。出ていない晩は
    # `None`（不明）のまま残す。
    if catchup_without_mismatch and r["roundtrip"] is not None:
        r["scale_mismatch_rows"] = r["scale_mismatch_companies"] = 0

    if t_start and t_end and t_end >= t_start:
        r["gap_minutes"] = round((t_end - t_start).total_seconds() / 60, 1)
    return r


def seconds_per_company(row: dict):
    """1社あたりの秒。**所要と社数の両方が取れた晩だけ**返す（片方欠けたら None）。"""
    if not row.get("gap_minutes") or not row.get("gap_target"):
        return None
    return round(row["gap_minutes"] * 60 / row["gap_target"], 3)


def warnings_for(row: dict) -> list:
    """その晩の警告。**増減は入れない**——夜ごとの分散に埋もれるので人が見る（#556）。"""
    w = []
    if row.get("upserted") is None and row.get("gap_skip_reason") is None:
        w.append("gap-fill の完了行もスキップ行も無い＝収集が終わっていない可能性")
    for key, label in (("http_429", "429（レート制限）"), ("http_5xx", "5xx")):
        if row.get(key):
            w.append(f"Yahoo {label} が {row[key]}件"
                     "＝並行度を下げる（FINAPP_YAHOO_CONCURRENCY=1 で逐次）")
    # 404 は上場廃止社が毎晩一定数返す値（#556・実測 約320件）。それ以外の 4xx は拒否
    # （401/403 等）の疑いで、429 と同じく絞られた合図になりうる。内訳が無い晩（旧書式）は
    # 判定しない——「不明」を 0 と読んで健全へ倒さない。
    if row.get("http_4xx") is not None and row.get("http_404") is not None:
        n_other_4xx = row["http_4xx"] - row["http_404"]
        if n_other_4xx > 0:
            w.append(f"Yahoo 404以外の 4xx が {n_other_4xx}件＝アクセス拒否（401/403 等）の疑い"
                     "＝並行度を下げる（FINAPP_YAHOO_CONCURRENCY=1 で逐次）")
    if row.get("exchange_rejected"):
        w.append(f"解決済みなのに空 {row['exchange_rejected']}社＝取引所が張り替わった合図")
    if row.get("roundtrip_companies"):
        w.append(f"往復段差 {row['roundtrip_companies']}社"
                 "＝`python -m scripts.repair_scale_mixture` で確認する（#620）")
    if row.get("roundtrip") == "検知失敗":
        w.append("往復段差の検知が例外で落ちた（収集自体は継続している）")
    if row.get("fresh_level") and row["fresh_level"] != "fresh":
        w.append(f"株価鮮度 level={row['fresh_level']}")
    return w


# ── ファイル走査と表示 ──────────────────────────────────────────────────────

def collect_nights(log_dir: Path, nights: int) -> list:
    """新しい順に `nights` 晩ぶん読む。戻り値は古い順（左から時系列に並べるため）。"""
    files = sorted(log_dir.glob(LOG_GLOB), key=lambda p: p.name)[-nights:]
    out = []
    for p in files:
        row = parse_nightly_log(p.read_text(encoding="utf-8", errors="replace"))
        row["log"] = p.name
        row["date"] = p.stem.replace("nightly_", "")
        try:
            d = datetime.strptime(row["date"], "%Y%m%d")
            row["weekday"] = WEEKDAY_JA[d.weekday()]
        except ValueError:
            row["weekday"] = "?"
        out.append(row)
    return out


def _fmt(v) -> str:
    return "-" if v is None else str(v)


ROWS = [
    ("対象社数 / 母数",      lambda r: f"{_fmt(r['gap_target'])} / {_fmt(r['gap_universe'])}"),
    ("価格ゼロ（解決済み）", lambda r: f"{_fmt(r['priceless'])}（{_fmt(r['priceless_resolved'])}）"),
    ("解決済みなのに空",     lambda r: _fmt(r["exchange_rejected"])),
    ("投入行数",             lambda r: _fmt(r["upserted"])),
    ("新規日付 new_rows",    lambda r: _fmt(r["new_rows"])),
    ("gap-fill 所要（分）",  lambda r: _fmt(r["gap_minutes"])),
    ("秒/社",                lambda r: _fmt(seconds_per_company(r))),
    ("Yahoo 並行度",         lambda r: _fmt(r["concurrency"])),
    ("HTTP 429 / 5xx",       lambda r: f"{_fmt(r['http_429'])} / {_fmt(r['http_5xx'])}"),
    ("HTTP 4xx（404）/ その他", lambda r: f"{_fmt(r['http_4xx'])}（{_fmt(r['http_404'])}）"
                                          f" / {_fmt(r['http_other'])}"),
    ("catchup upsert",       lambda r: _fmt(r["catchup_upserted"])),
    ("スケール不採用 行(社)", lambda r: f"{_fmt(r['scale_mismatch_rows'])}"
                                       f"（{_fmt(r['scale_mismatch_companies'])}）"),
    ("往復段差",             lambda r: f"{_fmt(r['roundtrip'])}"
                                       + (f" {r['roundtrip_companies']}社"
                                          if r.get("roundtrip_companies") else "")),
    ("往復段差 除外帯",      lambda r: _fmt(r["roundtrip_excluded"])),
    ("鮮度 p50",             lambda r: _fmt(r["fresh_p50"])),
    ("鮮度 p05",             lambda r: _fmt(r["fresh_p05"])),
    ("鮮度 level",           lambda r: _fmt(r["fresh_level"])),
    ("5営業日超の遅れ",      lambda r: _fmt(r["fresh_stale5d"])),
]


def print_report(nights: list) -> None:
    if not nights:
        print("読める夜間ログが1本も無い（.logs/nightly_*.log）")
        return
    head = [f"{n['date'][4:6]}/{n['date'][6:]}({n['weekday']})" for n in nights]
    width = max([14] + [display_width(h) for h in head]
                + [display_width(get(n)) for _, get in ROWS for n in nights]) + 2
    label_w = max(display_width(lbl) for lbl, _ in ROWS) + 2

    print("夜間バッチの収集指標（左が古い）。`-` はログにその行が無い＝**不明**であって 0 ではない\n")
    print(" " * label_w + "".join(pad(h, width) for h in head))
    for label, get in ROWS:
        print(pad(label, label_w) + "".join(pad(get(n), width) for n in nights))

    print()
    flagged = 0
    for n in nights:
        ws = warnings_for(n)
        if not ws:
            continue
        flagged += 1
        print(f"[{n['date']}] {n['log']}")
        for w in ws:
            print(f"  ⚠ {w}")
    if not flagged:
        print("警告なし。**社数・所要の増減は警告にしていない**"
              "——夜ごとに ±45% 振れた前例があるので、上の表を人が見て判断する（#556）")


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="夜間バッチの収集ログを晩ごとに並べて読む（#556・#620）")
    ap.add_argument("--nights", type=int, default=3, help="読む晩の数（既定3）")
    ap.add_argument("--log-dir", default=str(LOG_DIR), help=f"ログ置き場（既定 {LOG_DIR}）")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args()

    nights = collect_nights(Path(args.log_dir), max(1, args.nights))
    if args.json:
        print(json.dumps(
            [dict(n, warnings=warnings_for(n), sec_per_company=seconds_per_company(n))
             for n in nights], ensure_ascii=False, indent=2))
    else:
        print_report(nights)
    return 2 if any(warnings_for(n) for n in nights) else 0


if __name__ == "__main__":
    raise SystemExit(main())
