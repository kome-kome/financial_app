"""拒否権に使う K（上位何社で測るか）を、事前登録した規則で決める計測 CLI（#1044・親 #1040・ADR-0069）。

規則は `docs/prereg/top-k-veto.md`（本文は main へのマージで凍結）。各モデルの上位K社超過リターンの
期別系列について、平均・SE（定常ブートストラップ）・MDE を出し、**全モデルで MDE ≤ 0.5 × |平均| を
満たす最小の K** を返す。該当が無ければ「該当なし」＝拒否権は導入せず参考のまま。

## 守ること

- **`--prereg` と `--prereg-sha` を必須にし、本文の sha256 が凍結値と一致しなければ終了コード 2**
  （`scripts/prereg.py`）。出力 JSON に本文の sha256・HEAD SHA・事前登録の Issue の URL を入れる。
- **規則の入力は各モデル自身の系列のぶれだけ。** モデル間の差は見ない（候補の勝ち負けを見てから
  K を選ぶと、効いているように見えるだけの規則になる・ADR-0045 / ADR-0050）。
- **系列はモデル比較と同じ手続き（`model_comparison.run_comparison`・ADR-0041）から取る。** 各モデルに
  画面の並びの採点（`oof_backtest_display`・#1042）があればその系列、無ければ予想の採点
  （`oof_backtest`）の `top_k_excess_return_by_period` を使う。λ>0 の M-1 / M-2 は②、λ=0 の M-6 は①に
  なる——モデルごとの指定をここへ書き写さない（画面の既定が変わったら追随する）。
- **1 モデルでも系列がそろわなければ K を決めずに止まる**（終了コード 3）。一部のモデルだけで
  「全モデルで満たす」を判定しない。
- 日中キューで単独に回す（`measure:top-k-veto`・並走に敏感）。

終了コード: 0 = 完了（K が決まったか「該当なし」）/ 2 = 事前登録の不一致 / 3 = 入力がそろわない。

実行:
    python -m scripts.measure_top_k_veto --prereg docs/prereg/top-k-veto.md --prereg-sha <凍結値>
    # モデル比較を回し直さず、`scripts.model_comparison_run --json` の出力から計算し直す:
    python -m scripts.measure_top_k_veto --prereg ... --prereg-sha ... --from-json <path>

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_stats import bootstrap_mean_se  # noqa: E402
from scripts import prereg  # noqa: E402

# 事前登録 §2 の対象と K の候補（本文と同じ値。変えるなら文書を凍結し直す）。
MODELS = ("macro_risk_return", "macro_gbdt", "macro_enet")      # M-1・M-2・M-6
MODEL_SHORT = {"macro_risk_return": "M-1", "macro_gbdt": "M-2", "macro_enet": "M-6"}
CANDIDATE_K = (5, 10, 30)
# 事前登録 §3 の MDE: (z_{1-α} + z_{power}) × √2 × SE（片側 α・検出力・対比較の SE を保守側に √2 倍）。
ALPHA = 0.05
POWER = 0.80
PAIR_SE_FACTOR = math.sqrt(2.0)
# 事前登録 §4 の判定: MDE ≤ MDE_RATIO × |系列の平均|。
MDE_RATIO = 0.5

Z_ALPHA = NormalDist().inv_cdf(1.0 - ALPHA)
Z_POWER = NormalDist().inv_cdf(POWER)

DEFAULT_OUT = ROOT / "scripts" / ".cache" / "top_k_veto.json"
EXIT_INPUT = 3


def mde(se: float) -> float:
    """事前登録 §3 の MDE。"""
    return (Z_ALPHA + Z_POWER) * PAIR_SE_FACTOR * se


def judge_series(values: list[float]) -> dict:
    """1 本の期別系列（時系列順）の平均・SE・MDE・合否。2 期未満は SE が無く不合格。"""
    mean = statistics.mean(values) if values else None
    se = bootstrap_mean_se(values)
    m = mde(se) if se is not None else None
    passed = mean is not None and m is not None and m <= MDE_RATIO * abs(mean)
    return {"n": len(values), "mean": mean, "se": se, "mde": m, "passed": passed}


def choose_k(table: dict[str, dict[int, dict]]) -> int | None:
    """事前登録 §4: 全モデルで合格する最小の K。無ければ None（拒否権は導入しない）。

    table[model][K] = `judge_series` の結果。各モデルの合否はそのモデル自身の系列だけで決まり、
    モデル間の差は入らない。
    """
    if not table:
        return None
    for k in sorted(CANDIDATE_K):
        if all(rows[k]["passed"] for rows in table.values()):
            return k
    return None


def series_of(entry: dict) -> tuple[str, dict]:
    """モデル比較の 1 モデルから `(出どころ, top_k_excess_return_by_period)` を取る。

    画面の並びの採点があればそれ（"display"）、無ければ予想の採点（"forecast"）。期別系列のキーが
    無い（#1041 より前の結果）なら ValueError——空の系列として読むと黙って「該当なし」になる。
    """
    display = entry.get("oof_backtest_display")
    source, oof = ("display", display) if display else ("forecast", entry.get("oof_backtest") or {})
    if "top_k_excess_return_by_period" not in oof:
        raise ValueError(f"{entry.get('name')}: {source} の採点に上位K社の期別系列が無い（#1041 より前の結果）")
    return source, oof["top_k_excess_return_by_period"]


def build_table(models: list[dict]) -> tuple[dict[str, dict[int, dict]], dict[str, str]]:
    """モデル比較の `models` から `(table, sources)` を作る。対象のモデルが欠けていれば ValueError。"""
    by_name = {m.get("name"): m for m in models}
    missing = [n for n in MODELS if not (by_name.get(n) or {}).get("available")]
    if missing:
        detail = ", ".join(f"{n}({(by_name.get(n) or {}).get('reason', 'absent')})" for n in missing)
        raise ValueError(f"対象のモデルが実行できていない: {detail}")
    table: dict[str, dict[int, dict]] = {}
    sources: dict[str, str] = {}
    for name in MODELS:
        source, by_k = series_of(by_name[name])
        sources[name] = source
        table[name] = {
            k: judge_series([v for _ym, v in sorted((by_k.get(str(k)) or {}).items())])
            for k in CANDIDATE_K
        }
    return table, sources


def _run_comparison() -> list[dict]:
    """モデル比較を対象のモデルだけで回す（UI・`model_comparison_run` と同じ手続き）。"""
    from database import SessionLocal
    from model_comparison import run_comparison

    db = SessionLocal()
    try:
        res = asyncio.run(run_comparison(db, render_light_mode=False, only_models=list(MODELS)))
    finally:
        db.close()
    return res.get("models") or []


def _fmt(v, digits: int = 4) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def _print_table(table: dict, sources: dict, chosen: int | None) -> None:
    print("model  source    K   n   mean      SE        MDE       0.5|mean|  pass")
    for name, rows in table.items():
        for k, r in rows.items():
            half = None if r["mean"] is None else MDE_RATIO * abs(r["mean"])
            print(f"{MODEL_SHORT[name]:<6} {sources[name]:<9} {k:<3} {r['n']:<3} "
                  f"{_fmt(r['mean']):>8}  {_fmt(r['se']):>8}  {_fmt(r['mde']):>8}  {_fmt(half):>9}  "
                  f"{'yes' if r['passed'] else 'no'}")
    print(f"chosen K: {chosen if chosen is not None else 'none (veto not introduced)'}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.measure_top_k_veto",
        description="拒否権に使う K を事前登録の規則で決める（各モデルの系列のぶれだけを見る）")
    prereg.add_arguments(ap)
    ap.add_argument("--from-json", help="scripts.model_comparison_run --json の出力を読む（モデル比較を回さない）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="結果の JSON")
    args = ap.parse_args(argv)

    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    stamp = prereg.stamp_from_args(args)

    from plugins.macro_snapshots import TOP_K_REFERENCE
    if not set(CANDIDATE_K) <= set(TOP_K_REFERENCE):
        print(f"[top_k_veto] NG: 規則の K {CANDIDATE_K} が参考の K {TOP_K_REFERENCE} に無い"
              "（oof_backtest がその K の系列を出していない）", file=sys.stderr)
        return EXIT_INPUT

    if args.from_json:
        models = json.loads(Path(args.from_json).read_text(encoding="utf-8")).get("models") or []
    else:
        models = _run_comparison()
    try:
        table, sources = build_table(models)
    except ValueError as e:
        print(f"[top_k_veto] NG: {e}", file=sys.stderr)
        return EXIT_INPUT
    chosen = choose_k(table)
    _print_table(table, sources, chosen)

    payload = {
        **stamp,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "input": args.from_json or "model_comparison.run_comparison",
        "rule": {"models": list(MODELS), "candidate_k": list(CANDIDATE_K), "alpha_one_sided": ALPHA,
                 "power": POWER, "pair_se_factor": PAIR_SE_FACTOR, "mde_ratio": MDE_RATIO},
        "sources": sources,
        "table": {name: {str(k): r for k, r in rows.items()} for name, rows in table.items()},
        "chosen_k": chosen,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
