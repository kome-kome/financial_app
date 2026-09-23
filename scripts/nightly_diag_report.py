"""夜間 producer の診断値を夜をまたいで読む（Issue #726・ADR-0061）。

`nightly_model_diagnostics` に積まれた診断値（選ばれた正則化の強さ・OOF 成績・業種別の統計）を
並べる。DB は読むだけで書かない。

なぜ要るのか
------------
夜間の `sector_ols` / `macro_enet` は毎晩 α を選び直し、M-6 は OOF 成績まで計算していたが、
その値は捨てられていた（#726）。表に積むようにしても、夜をまたいで並べる手段が無ければ
「α が候補の端に張り付いている」「値が動いたのはデータのせいか、コードのせいか」は結局
人が SQL を書き直して確かめるしかない（#612 の `macro_beta_gate_history` と同じ事情）。

3つの見方（`--view`）
---------------------
- `timeline`（既定）: 追っている値が**前の夜から動いた夜だけ**を出す。動いた夜には、同時に
  何が変わっていたか（コード・前処理の世代・データ）を並べる。**どれも変わっていないのに
  動いた夜は `no-context-change`**——#697 型の並び依存の候補だが、件数と断面日が同じでも
  中身の値が入れ替わることはある（入力のハッシュは取っていない・ADR-0061 決定6）ので、
  断定ではなく「調べる候補」として読む
- `edges`: α が候補の端に張り付いた夜と箇所
- `sectors`: sector_ols の業種ごとの ridge α の推移（動いた夜だけ）

実行:
    python -m scripts.nightly_diag_report
    python -m scripts.nightly_diag_report --model macro_enet --since 2026-10-01
    python -m scripts.nightly_diag_report --view edges
    python -m scripts.nightly_diag_report --view sectors

出力の区切りは ASCII 記号だけ（Windows cp932 のリダイレクトで落ちないため）。
接続文字列は出さず、「ローカル／本番（リモート）」の別だけを出す。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VIEWS = ("timeline", "edges", "sectors")


# ── 純関数（DB を触らない・テストから生値を食わせる）─────────────────────────

def _diag(row: dict) -> dict:
    d = row.get("diagnostics") or {}
    return json.loads(d) if isinstance(d, str) else d


def headline(model: str, diag: dict) -> dict:
    """夜をまたいで追う値。これが動いた夜だけを timeline に出す。"""
    if model == "sector_ols":
        return {
            "n_total":              diag.get("n_total"),
            "n_sectors":            diag.get("n_sectors"),
            "n_alpha_at_low_edge":  diag.get("n_alpha_at_low_edge"),
            "n_alpha_at_high_edge": diag.get("n_alpha_at_high_edge"),
            "alpha_by_sector":      {s.get("industry"): s.get("alpha")
                                     for s in diag.get("sectors") or []},
        }
    if model == "macro_enet":
        fm = diag.get("final_model") or {}
        cv = diag.get("cv_diagnostics") or {}
        oof = diag.get("oof") or {}
        return {
            "n_train_samples":    diag.get("n_train_samples"),
            "alpha":              fm.get("alpha"),
            "l1_ratio":           fm.get("l1_ratio"),
            "n_nonzero":          fm.get("n_nonzero"),
            "cv_alpha_at_path_min": cv.get("alpha_at_path_min"),
            "cv_alpha_at_path_max": cv.get("alpha_at_path_max"),
            "rank_ic":            (oof.get("rank_ic") or {}).get("mean"),
            "rank_ic_industry_neutral": (oof.get("rank_ic_industry_neutral") or {}).get("mean"),
            "short_side_spread":  oof.get("short_side_spread"),
        }
    return {}


def _data_size(model: str, diag: dict):
    return diag.get("n_total") if model == "sector_ols" else diag.get("n_train_samples")


def _code_changed(prev: str | None, cur: str | None) -> str | None:
    """コードが変わったか。"code" = 変わった / "code?" = 判定できない / None = 同じ。"""
    if prev != cur:
        return "code"
    if not cur or cur == "unknown" or cur.endswith("+dirty"):
        # 同じ dirty 印でも、未コミットの中身は夜の間に変わりうる
        return "code?"
    return None


def timeline(rows: list[dict]) -> list[dict]:
    """モデルごとに、追っている値が前の夜から動いた夜だけを返す（最初の夜は基準として出す）。

    rows は古い順。各要素: run_id / model / created_at / changed（動いた値の名前）/
    causes（同時に変わっていたもの: code / code? / preprocess / data）/ values（今夜の値）。
    """
    prev_by_model: dict[str, dict] = {}
    out: list[dict] = []
    for row in rows:
        model = row.get("model")
        diag = _diag(row)
        cur = {"headline": headline(model, diag), "row": row, "size": _data_size(model, diag)}
        prev = prev_by_model.get(model)
        prev_by_model[model] = cur
        if prev is None:
            out.append({"run_id": row.get("run_id"), "model": model,
                        "created_at": row.get("created_at"), "changed": ["(baseline)"],
                        "causes": [], "values": cur["headline"]})
            continue
        changed = [k for k, v in cur["headline"].items() if prev["headline"].get(k) != v]
        if not changed:
            continue
        causes = []
        code = _code_changed(prev["row"].get("code_version"), row.get("code_version"))
        if code:
            causes.append(code)
        if prev["row"].get("preprocess_version") != row.get("preprocess_version"):
            causes.append("preprocess")
        if (prev["row"].get("snapshot_date") != row.get("snapshot_date")
                or prev["size"] != cur["size"]):
            causes.append("data")
        out.append({"run_id": row.get("run_id"), "model": model,
                    "created_at": row.get("created_at"), "changed": changed,
                    "causes": causes or ["no-context-change"], "values": cur["headline"]})
    return out


def edges(rows: list[dict]) -> list[dict]:
    """α（と l1_ratio）が候補の端に張り付いた箇所を夜ごとに返す。張り付きの無い夜は出さない。"""
    out: list[dict] = []
    for row in rows:
        model = row.get("model")
        diag = _diag(row)
        hits: list[str] = []
        if model == "sector_ols":
            for s in diag.get("sectors") or []:
                if s.get("alpha_edge"):
                    hits.append("{0} alpha={1} {2} (n={3})".format(
                        s.get("industry"), s.get("alpha"), s.get("alpha_edge"), s.get("n")))
        elif model == "macro_enet":
            fm = diag.get("final_model") or {}
            cv = diag.get("cv_diagnostics") or {}
            if fm.get("alpha_at_path_min"):
                hits.append("final alpha={0} low (path min)".format(fm.get("alpha")))
            if fm.get("alpha_at_path_max"):
                hits.append("final alpha={0} high (path max = all-zero)".format(fm.get("alpha")))
            if fm.get("l1_ratio_at_grid_edge"):
                hits.append("final l1_ratio={0} at grid edge {1}".format(
                    fm.get("l1_ratio"), fm.get("l1_ratio_grid")))
            for key, side in (("alpha_at_path_min", "low"), ("alpha_at_path_max", "high")):
                frac = cv.get(key)
                if frac:
                    hits.append("cv folds at {0} edge: {1:.0%}".format(side, frac))
        if hits:
            out.append({"run_id": row.get("run_id"), "model": model,
                        "created_at": row.get("created_at"), "hits": hits})
    return out


def sector_alpha_changes(rows: list[dict]) -> dict[str, list[tuple]]:
    """業種 → [(run_id, alpha), ...]。α が前の夜から変わった夜（と最初の夜）だけを残す。"""
    out: dict[str, list[tuple]] = {}
    for row in rows:
        if row.get("model") != "sector_ols":
            continue
        for s in _diag(row).get("sectors") or []:
            seq = out.setdefault(s.get("industry"), [])
            if not seq or seq[-1][1] != s.get("alpha"):
                seq.append((row.get("run_id"), s.get("alpha")))
    return out


# ── 表示 ────────────────────────────────────────────────────────────────────

def _fmt_value(v) -> str:
    if isinstance(v, float):
        return "{0:.6g}".format(v)
    if isinstance(v, dict):
        return "{{{0} sectors}}".format(len(v))
    return "-" if v is None else str(v)


def render_timeline(items: list[dict]) -> str:
    if not items:
        return "nightly_model_diagnostics に行がありません。"
    lines = ["追っている値が動いた夜（古い順）"]
    for i in items:
        lines.append("")
        lines.append("{0}  {1}  changed={2}  causes={3}".format(
            i["run_id"], i["model"], ",".join(i["changed"]), ",".join(i["causes"]) or "-"))
        for k, v in i["values"].items():
            mark = "*" if k in i["changed"] else " "
            lines.append("  {0} {1} = {2}".format(mark, k, _fmt_value(v)))
    lines += [
        "",
        "causes: code = コードが変わった / code? = 判定できない（unknown か未コミット変更あり）/",
        "        preprocess = 前処理の世代が変わった / data = 断面日か件数が変わった /",
        "        no-context-change = どれも変わっていない（並び依存などの調べる候補・断定ではない）",
    ]
    return "\n".join(lines)


def render_edges(items: list[dict]) -> str:
    if not items:
        return "候補の端に張り付いた夜はありません。"
    lines = ["候補の端に張り付いた夜（古い順）"]
    for i in items:
        lines.append("")
        lines.append("{0}  {1}".format(i["run_id"], i["model"]))
        lines += ["  - " + h for h in i["hits"]]
    return "\n".join(lines)


def render_sectors(changes: dict[str, list[tuple]]) -> str:
    if not changes:
        return "sector_ols の行がありません。"
    from scripts._textwidth import display_width, pad

    width = max(display_width(k or "-") for k in changes)
    lines = ["業種ごとの ridge alpha（動いた夜だけ・古い順）", ""]
    for industry in sorted(changes, key=lambda k: k or ""):
        seq = changes[industry]
        trail = "  ->  ".join("{0}: {1}".format(r, _fmt_value(a)) for r, a in seq)
        lines.append("{0}  {1}".format(pad(industry or "-", width), trail))
    return "\n".join(lines)


# ── DB ──────────────────────────────────────────────────────────────────────

def fetch_rows(model: str | None = None, since: str | None = None) -> list[dict]:
    """行を**古い順**で返す。`since` は "YYYY-MM-DD"（created_at の UTC 日付で切る）。"""
    from datetime import datetime

    from sqlalchemy import select

    from database import NightlyModelDiagnostic as T
    from database import engine

    stmt = select(T.run_id, T.model, T.snapshot_date, T.code_version,
                  T.preprocess_version, T.diagnostics, T.created_at)
    if model:
        stmt = stmt.where(T.model == model)
    if since:
        stmt = stmt.where(T.created_at >= datetime.strptime(since, "%Y-%m-%d"))
    stmt = stmt.order_by(T.created_at, T.id)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="夜間 producer の診断値を夜をまたいで読む（#726）")
    ap.add_argument("--model", choices=("sector_ols", "macro_enet"), default=None,
                    help="モデルを絞る（既定: 全部）")
    ap.add_argument("--since", default=None, help="この日付（UTC・YYYY-MM-DD）以降の行だけ読む")
    ap.add_argument("--view", choices=VIEWS, default="timeline", help="見方（既定: timeline）")
    args = ap.parse_args(argv)

    from database import _is_local
    rows = fetch_rows(args.model, args.since)
    print("接続先={0} / 行数={1}".format("ローカル" if _is_local else "本番（リモート）", len(rows)))
    if args.view == "timeline":
        print(render_timeline(timeline(rows)))
    elif args.view == "edges":
        print(render_edges(edges(rows)))
    else:
        print(render_sectors(sector_alpha_changes(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
