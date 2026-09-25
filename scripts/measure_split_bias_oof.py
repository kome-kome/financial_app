"""第2経路（#656）を入れる前と後で OOF rank-IC を測り比べる（ADR-0055 決定7 の測定）。

## なぜ1プロセスで前後を回すのか

補正は `financial_metrics` VIEW が `split_adjustment_factors` を LEFT JOIN して当てている
（ADR-0055 決定1/2）。つまり「補正前の断面」はもう DB に残っていない——見るには係数表を
作り直すしかない。そこで**同じプロセスの中で係数表を往復させて2回測る**。

    1. bps_path=False で係数表を作り直す -> モデル比較（OOF）
    2. bps_path=True  で係数表を作り直す -> 同じ比較をもう一度
    3. 2つの rank-IC と差を出し、**係数表は True の状態で終える**

途中で死ぬと係数表が第1経路だけの状態で残るが、毎晩の収集（JST 17:20）が作り直すので
自己修復する。**係数表は全置換で冪等**なので、途中まで走っても壊れた中間状態は作らない。

## 測る手続きは持たない

`rebuild_split_adjustment_factors()` と `model_comparison.run_comparison()` を順に呼ぶだけ。
rank-IC も fold も有意性も各モデルの `oof_backtest` と `model_stats` が既に持っており、
ここで書き直すと**測ったものが本番と別物になる**（ADR-0041）。

## 差の符号は採否の条件にしない

ADR-0055 決定7 のとおり、この歪みは未来情報のリークでありうる（分割するのは株価が上がった
社なので、歪んだ PER は「この先上がる」という未来を過去の断面へ持ち込む）。**補正すると
rank-IC が下がるのが正しい**ので、下がったことを理由に第2経路を外してはいけない。

実行:
    python -m scripts.measure_split_bias_oof
    python -m scripts.measure_split_bias_oof --models macro_gbdt --json out.json
    python -m scripts.measure_split_bias_oof --summarize scripts/.cache/measure_split_bias_oof.json

接続先は `FINAPP_DB_TARGET`（既定 local＝ローカル正本・#503/ADR-0038）に従う。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corporate_actions import rebuild_split_adjustment_factors  # noqa: E402
from database import SessionLocal  # noqa: E402
from model_comparison import run_comparison  # noqa: E402

DEFAULT_MODELS = "macro_gbdt,macro_enet"
DEFAULT_JSON = Path(__file__).resolve().parent / ".cache" / "measure_split_bias_oof.json"


def _num(v, digits: int = 4) -> str:
    """None 安全な数値整形（欠測は '-'）。cp932 で落ちる記号は使わない。"""
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.{digits}f}"
    return str(v)


def _rank_ics(res: dict) -> dict[str, float | None]:
    """モデル名 -> OOF rank-IC の平均。実行不可のモデルは None で残す（黙って消さない）。"""
    out: dict[str, float | None] = {}
    for m in res.get("models") or []:
        name = m.get("name") or m.get("short") or "?"
        if not m.get("available"):
            out[name] = None
            continue
        out[name] = ((m.get("oof_backtest") or {}).get("rank_ic") or {}).get("mean")
    return out


def _rank_ics_by_period(res: dict) -> dict[str, dict[str, float]]:
    """モデル名 -> {期: rank-IC}。実行不可のモデルは空の dict で残す。"""
    out: dict[str, dict[str, float]] = {}
    for m in res.get("models") or []:
        name = m.get("name") or m.get("short") or "?"
        by = ((m.get("oof_backtest") or {}).get("rank_ic_by_period") or {}) \
            if m.get("available") else {}
        out[name] = {str(k): float(v) for k, v in by.items() if v is not None}
    return out


def sign_test_p(n_down: int, n_up: int) -> float | None:
    """両側の符号検定（正確な二項・同値は除いた数で数える）。`n_down + n_up == 0` は None。

    scipy に頼らないのは、ここが既存の依存だけで読み直せる再集計口だから。
    """
    n = n_down + n_up
    if n == 0:
        return None
    k = min(n_down, n_up)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def paired_by_period(before: dict[str, float], after: dict[str, float]) -> dict:
    """前後で**同じ期**どうしの差（after − before）を並べる。

    平均の差だけでは、数期の大きな揺れで出た差と全期で一様に下がった差を区別できない
    （2026-09-17 の実走は −0.02 前後で、どちらかを期別に数えないと決められなかった）。
    """
    periods = sorted(set(before) & set(after))
    diffs = {p: after[p] - before[p] for p in periods}
    n_down = sum(1 for d in diffs.values() if d < 0)
    n_up = sum(1 for d in diffs.values() if d > 0)
    return {
        "n": len(periods),
        "n_down": n_down,
        "n_up": n_up,
        "mean_diff": (sum(diffs.values()) / len(periods)) if periods else None,
        "sign_p": sign_test_p(n_down, n_up),
        "diff_by_period": diffs,
    }


def report(payload: dict) -> dict[str, dict]:
    """保存済みの結果（`main` が書く JSON と同じ形）を表にして出す。戻り値は期別の対応差。"""
    before, after = payload.get("before") or {}, payload.get("after") or {}
    ic_before, ic_after = _rank_ics(before), _rank_ics(after)
    print("")
    print("=== OOF rank-IC: 第2経路の前後 ===")
    print("model                    before      after       diff")
    for name in sorted(set(ic_before) | set(ic_after)):
        a, b = ic_before.get(name), ic_after.get(name)
        diff = (b - a) if (a is not None and b is not None) else None
        print(f"{name:<22} {_num(a):>10} {_num(b):>10} {_num(diff):>10}")

    by_before, by_after = _rank_ics_by_period(before), _rank_ics_by_period(after)
    paired = {name: paired_by_period(by_before.get(name, {}), by_after.get(name, {}))
              for name in sorted(set(by_before) | set(by_after))}
    print("")
    print("=== 期別の対応差（after - before・同じ期どうし） ===")
    print("model                  periods   down     up  mean_diff   sign_p")
    for name, r in paired.items():
        print(f"{name:<22} {r['n']:>7} {r['n_down']:>6} {r['n_up']:>6} "
              f"{_num(r['mean_diff']):>10} {_num(r['sign_p']):>8}")
    print("")
    print("注: ADR-0055 決定7 のとおり**差の符号は採否の条件にしない**。歪みは未来情報の")
    print("    リークでありうるので、補正で rank-IC が下がるのが正しい読みである。")
    print("    系統的に下がったこと自体はリークの証拠ではない（直接証拠は measure_split_leak）。")
    return paired


def _phase(label: str, *, bps_path: bool, models: list[str]) -> dict:
    """係数表を作り直してからモデル比較を回す。戻り値は生レスポンス。"""
    db = SessionLocal()
    try:
        t0 = time.time()
        n = rebuild_split_adjustment_factors(db, bps_path=bps_path)
        print(f"[{label}] 係数表を全置換: {n} 行 (bps_path={bps_path} / {time.time() - t0:.1f}秒)",
              flush=True)
        t1 = time.time()
        res = asyncio.run(run_comparison(db, render_light_mode=False, only_models=models))
        print(f"[{label}] モデル比較 完了 ({time.time() - t1:.1f}秒)", flush=True)
        return res
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()

    ap = argparse.ArgumentParser(
        prog="python -m scripts.measure_split_bias_oof",
        description="第2経路（#656）の前後で OOF rank-IC を測る（ADR-0055 決定7）")
    ap.add_argument("--models", default=DEFAULT_MODELS,
                    help=f"カンマ区切りのプラグイン名（既定 {DEFAULT_MODELS}）")
    ap.add_argument("--json", dest="json_out", nargs="?", const=str(DEFAULT_JSON),
                    default=str(DEFAULT_JSON))
    ap.add_argument("--summarize", metavar="JSON",
                    help="測り直さず、保存済みの JSON を集計し直して出す（係数表にも触れない）")
    args = ap.parse_args(argv)

    if args.summarize:
        report(json.loads(Path(args.summarize).read_text(encoding="utf-8")))
        return 0

    models = [s.strip() for s in args.models.split(",") if s.strip()]
    print(f"対象モデル: {models}")

    before = _phase("before", bps_path=False, models=models)
    # **after を必ず最後に回す。** 順序を入れ替えると係数表が第1経路だけの状態で終わり、
    # 次の夜間収集まで補正が外れたままになる。
    after = _phase("after", bps_path=True, models=models)

    payload = {
        "models": models,
        "rank_ic_before": _rank_ics(before), "rank_ic_after": _rank_ics(after),
        "before": before, "after": after,
    }
    paired = report(payload)

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload["paired_by_period"] = paired
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print(f"JSON: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
