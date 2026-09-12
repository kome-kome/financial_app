"""収束ゲートの余裕を run 横断で読む（Issue #612）。

`macro_beta_meta` に積まれた過去 run の収束診断を並べ、**変数ごとの `r_hat` の p99 と
閾値までの余裕**を1枚の表に出す。DB は読むだけで書かない。

なぜ要るのか
------------
ゲートの余裕（`alpha` の p99 が閾値 1.05 までいくら残っているか）は run ごとに
`macro_beta_meta.hyperparams.diagnostics.by_param` へ入っているが、**run をまたいで
並べる手段が無かった**。そのため 2026-09-06 と 2026-09-11 の2点を比べるだけでも人が
Issue へコメントを書き残すしかなく、#612 では毎回 DB を手で引き直していた。余裕が
縮んでいるのかどうかは「何点たまったか」で決まる量なので、溜める手段を持たないと
判断がいつまでも先送りになる。

読むときの注意（表にも印として出る）
------------------------------------
- **`n_divergences > 0` の run を推移へ混ぜない**。2026-09-07 の run は発散344回で
  `alpha` の p99 が 1.1843 まで悪化したが、これはサンプリングが壊れた回で、規模の
  影響ではない（並走を止めたら 0 に戻った）。混ぜると「規模とともに縮んでいる」と
  誤読する
- **`by_param` を持たない旧 run（#608 以前）を推移へ混ぜない**。`gate_values` が
  全体の `r_hat_max` 1本へフォールバックするので変数名が `r_hat_max` になる。しかも
  その値は #356 の丸め時代のもので 1.00/1.01/1.02 の3値へ量子化されており、生値と
  並べられない

判定は**本番と共有する**。`macro_beta_inference.gate_values` / `gate_verdict` /
`MONTHLY_RHAT_THRESHOLD` / `PERSIST_MARGIN_WARN` を import して使い、p99 の比較を
ここへ書き写さない（ADR-0002 の #613 節が bench について定めたのと同じ作法——本番と
別基準の表を見ても設定を選べない）。

実行:
    python -m scripts.macro_beta_gate_history
    python -m scripts.macro_beta_gate_history --limit 10
    python -m scripts.macro_beta_gate_history --threshold 1.01   # strict で見直す
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts._textwidth import pad                       # noqa: E402

# 既定で読む run 数。`hyperparams` は run あたり数KB（`r_hat_worst` を3変数ぶん持つ）ある
# ので、全件読みを既定にしない。
DEFAULT_LIMIT = 20


def _diagnostics(hyperparams) -> dict:
    """`macro_beta_meta.hyperparams` から診断 dict を取り出す。

    JSON 列なので通常は dict で返るが、テキストで渡された場合（他経路の写し・テスト）にも
    耐えるよう文字列を許す。
    """
    hp = hyperparams or {}
    if isinstance(hp, str):
        hp = json.loads(hp) or {}
    return hp.get("diagnostics") or {}


def summarize_runs(rows, threshold: float | None = None) -> list[dict]:
    """run の行（`run_id` / `status` / `created_at` / `hyperparams`）を要約へ畳む。

    純関数——DB も PyMC も要らないのでテストから生値を食わせられる。返すのは入力と同じ順。

    各要素:

    - `verdict` / `worst`: `gate_verdict` の合否と、それを決めた変数（本番と同一判定）
    - `gate`: 変数名 → `{"p99", "margin", "thin"}`。`margin` は `threshold - p99`、
      `thin` は `PERSIST_MARGIN_WARN` を切ったか（`log_gate_report` の警告と同じ条件）
    - `n_stock`: `by_param["alpha"]["n"]`（alpha は銘柄ごとの切片なので個数＝銘柄数）
    - `healthy`: `n_divergences == 0`。診断に無ければ None（不明を True と混ぜない）
    - `legacy`: `by_param` を持たない旧 run（判定が全体の `r_hat_max` へフォールバックする）
    """
    from macro_beta_inference import (MONTHLY_RHAT_THRESHOLD, PERSIST_MARGIN_WARN,
                                      gate_values, gate_verdict)

    th = MONTHLY_RHAT_THRESHOLD if threshold is None else threshold
    out: list[dict] = []
    for row in rows:
        diag = _diagnostics(row.get("hyperparams"))
        by_param = diag.get("by_param") or {}
        verdict, worst = gate_verdict(diag, th)
        n_div = diag.get("n_divergences")
        gate = {}
        for name, p99 in gate_values(diag).items():
            gate[name] = {"p99": p99, "margin": th - p99,
                          "thin": p99 > th - PERSIST_MARGIN_WARN}
        out.append({
            "run_id":        row.get("run_id"),
            "status":        row.get("status"),
            "created_at":    row.get("created_at"),
            "threshold":     th,
            "verdict":       verdict,
            "worst":         worst,
            "n_stock":       (by_param.get("alpha") or {}).get("n"),
            "n_divergences": n_div,
            "healthy":       None if n_div is None else (n_div == 0),
            "legacy":        not by_param,
            "gate":          gate,
        })
    return out


def fetch_runs(limit: int = DEFAULT_LIMIT) -> list[dict]:
    """最新 `limit` 件の run を**古い順**で返す（表示順＝推移の順）。"""
    from sqlalchemy import select

    from database import MacroBetaMeta, engine

    stmt = (select(MacroBetaMeta.run_id, MacroBetaMeta.status,
                   MacroBetaMeta.created_at, MacroBetaMeta.hyperparams)
            .order_by(MacroBetaMeta.created_at.desc(), MacroBetaMeta.id.desc())
            .limit(limit))
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(stmt)]
    return list(reversed(rows))


def _flags(item: dict) -> str:
    """推移から外して読むべき理由を短い ASCII の印で返す。"""
    marks = []
    if item["healthy"] is False:
        marks.append("div={0}".format(item["n_divergences"]))
    elif item["healthy"] is None:
        marks.append("div=?")
    if item["legacy"]:
        marks.append("old-gate")
    return ",".join(marks)


def _fmt_created(value) -> str:
    return str(value)[:16] if value else "-"


def render(items: list[dict]) -> str:
    """要約を表へ。**ASCII の区切りだけ**を使う（cp932 の標準出力で落ちないため）。"""
    if not items:
        return "macro_beta_meta に run がありません。"

    from macro_beta_inference import PERSIST_MARGIN_WARN

    th = items[-1]["threshold"]
    lines = [
        "収束ゲートの履歴（古い順）: threshold={0:.4f} / margin_warn={1:.4f}".format(
            th, PERSIST_MARGIN_WARN),
        "判定は macro_beta_inference.gate_verdict（本番と同一）。",
        "",
    ]

    head = ("run_id", "created_at", "status", "n_stock", "n_div", "verdict", "worst", "flags")
    widths = [max(len(head[0]), *(len(str(i["run_id"] or "-")) for i in items)),
              16, 12, 7, 6, 7,
              max(len(head[6]), *(len(i["worst"] or "-") for i in items)),
              max(len(head[7]), *(len(_flags(i)) for i in items))]
    lines.append("  ".join(pad(h, w) for h, w in zip(head, widths)))
    lines.append("  ".join("-" * w for w in widths))
    for i in items:
        cells = (i["run_id"] or "-", _fmt_created(i["created_at"]),
                 i["status"] or "(none)",
                 "-" if i["n_stock"] is None else "{0:,}".format(i["n_stock"]),
                 "-" if i["n_divergences"] is None else str(i["n_divergences"]),
                 i["verdict"], i["worst"] or "-", _flags(i) or "-")
        lines.append("  ".join(pad(c, w) for c, w in zip(cells, widths)))

    # 変数別の推移。**alpha の余裕が縮んでいるかを読むのがこのセクションの用途**なので、
    # 印の付いた run も落とさず並べる（落とすと「比べられる点が何点あるか」が見えない）。
    names = sorted({n for i in items for n in i["gate"]})
    for name in names:
        lines += ["", "[{0}] 閾値までの余裕（THIN = margin_warn を切った）".format(name)]
        for i in items:
            g = i["gate"].get(name)
            if not g:
                continue
            lines.append("  {0}  {1}  n={2:<7}  p99={3:.4f}  margin={4:+.4f}  {5:<5}  {6}".format(
                _fmt_created(i["created_at"]), pad(i["run_id"] or "-", widths[0]),
                "-" if i["n_stock"] is None else i["n_stock"],
                g["p99"], g["margin"], "THIN" if g["thin"] else "",
                _flags(i) or ""))

    usable = [i for i in items if i["healthy"] and not i["legacy"]]
    lines += [
        "",
        "推移として比べられる run（healthy かつ by_param あり）: {0}件 / {1}件".format(
            len(usable), len(items)),
        "  div=N は発散した run（サンプリングが壊れた回で、規模の影響ではない）。",
        "  old-gate は by_param を持たない旧 run（r_hat_max が #356 の丸め値で生値と並べられない）。",
    ]
    n_scales = {i["n_stock"] for i in usable if i["n_stock"] is not None}
    if len(n_scales) < 2:
        lines.append("  比べられる run の n_stock は {0} の1種類のみ＝**規模依存は未実測**（#612）。"
                     .format(sorted(n_scales) or "-"))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="収束ゲートの余裕を run 横断で読む（#612）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="読む run 数（新しい順に取り、古い順で表示。既定 {0}）".format(DEFAULT_LIMIT))
    ap.add_argument("--threshold", type=float, default=None,
                    help="比べる閾値（既定は無人の月次実行と同じ MONTHLY_RHAT_THRESHOLD）")
    args = ap.parse_args(argv)

    items = summarize_runs(fetch_runs(args.limit), args.threshold)
    print(render(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
