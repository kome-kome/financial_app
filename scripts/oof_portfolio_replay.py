"""OOF 運用再現（#1045・親 #1040・ADR-0069 の「参考」）。

実運用（画面の上位 K 社を買い、保有の中の下位 k 社を売って入れ替える）を、無リークの OOF 予測だけで
時系列に繰り返したことにして、年率・最大下落・入替率・コスト控除後・全社平均と TOPIX に対する超過を出す。

**過去の経路は 1 本しかなく、運とモデルの良さを区別できない。参考で、判定・ゲートには使わない**
（`DISCLAIMER` を出力と JSON に残す）。名前は「OOF 運用再現」で、`/api/backtest`（as-of の模擬売買・
終端が今日で共通の 1 断面＝検定力が無い・ADR-0041）とは別物（CONTEXT.md）。

## 手続き

- 予測は `sell_mu_source_bakeoff --in-layer`（#1043）と同じく M-4 を 1 回回して共通域の社ごとの OOF を取る
  （`_run_config(keep_common=True)`）。M-4 の基底は #1058 で本番の設定にそろえた（`M4_BASES_CAVEAT`）
- 買いの並びは各モデルの画面の既定（M-1 / M-2 は U = μ̂ − λR・生の R2、λ=0 の M-6 は μ̂＝`buy_scores`）。
  売りの並びは `--sell-source` の低い順（既定は M-6 の μ̂＝`sell_ranking.MU_SOURCE`）。`--sell-source s2_quantile_low`
  で本番の売りの並びのキー S2（ADR-0076）にできる（#1238）＝M-6 のレグの入力を捕まえ（`capture_m6_args`）、同じパネル・
  fold で計測道具（#1158）と同じ関数で OOF を作る（`s2_predictions`）。予測が全社同じ値の期は、その期だけ M-6 の μ̂ で
  並べる（`s2_sell_keys`・倒した期を JSON の `sell` に出す）。−R_macro は過去の時点で作り直せないので使わない
- test 期（3 か月おき）ごとに: 予測が無くなった保有を外す → 残りの中で売りの並びの下位 k 社を売る →
  買いの並びの上位から K 社になるまで補充する（その期に売った社は買い戻さない）。最初の期は上位 K 社を買う
- 期間のリターンは **y（52 週先）を繋げない**（四半期で繋ぐとラベルが重複する）。各社の月末の週の終値
  （`month_end_indices`＝学習ラベルの基準週と同じ）の比を test 期の間で取る。後の月に株価が無い社は、その
  月末以前の最後の終値で手仕舞ったとみなす（`DELIST_NOTE`・件数を出す）
- 保有は毎期等金額にそろえる前提。コストは片道 `cost_bps` ×（売った社数＋買った社数）÷ K を入れ替えの
  たびに次の区間のリターンから引く（値動きの比率のずれを直す売買のコストは数えない）
- 全社平均＝各区間の始めの期に予測がある全社を等金額で持った場合（コスト無し）

## 本番の設定の M-2（#1123・ADR-0070 決定 6）

上の M-2 は M-4 の基底（既定の設定）で、実際に買っている本番の設定（探索の 1 位）の M-2 ではない。そこで M-2 の行の
直後に `macro_gbdt:production`（表示は `M-2*`）を足す。設定は `plugin_tuned_params` を比較画面と同じ射影
（`model_comparison._production_config`）で写し、**単体の M-2 を本番と同じ手続き**（検証の取り方・単調制約・空欄埋め）
で回す。M-4 の基底に設定を渡す形は採らない（M-4 の基底は単調制約を読まず、早期打ち切りの検証も `rows` で本番の
`months` と違う＝本番の M-2 にならない）。

- **test 月を共通域にそろえる**: 単体の M-2 は M-1 より早く始まることがあり、3 か月おきの test 月の位置が共通域と
  ずれうる（ずれ方はパネルの月の並びで決まる。ずれると 1 期も重ならないことがある）。最初の学習期間だけを延ばして
  （`aligned_min_train`）共通域の最初の test 月から始める。学習の手続きは変わらない
- 共通域の外の予測は落とし、件数を `coverage` に出す。そろった期の数を出し、0 なら行を出さずに理由を出す
- `tuning_dry_run`・`tuning_objective_only` の内側で回す＝本番の μ̂（`macro_gbdt_scores`）を上書きしない
- 本番の設定は採点と同じ期間のデータで選んでいる＝**選択の楽観を含む**（`selection_optimism`・`PRODUCTION_NOTE`）
- 本番の設定が無い・そろえられない・回せないときは行を出さず、理由を表示と JSON の `production` に出す

日中枠 `measure:oof-replay`（並走に敏感）で単独に回す。結果は `scripts/.cache/oof_portfolio_replay.json`。

## 経路のずらし・信頼区間・入替の利得・帯ごとのコスト（#1167・親 #1156・どれも参考）

- **開始月をずらした経路**（`--start-shifts 0,1,2`・既定は `0`＝今までの 1 本）: test 月は 3 か月おきの格子なので、
  先頭の期を削っても同じ格子のままで別の経路にならない。M-4 の基底の walk-forward の最初の学習期間だけを +s か月
  （`shifted_min_train`・`plugins.macro_ensemble.walk_forward_cv_monthly` を計測の中でだけ包む＝本番のコードは変えない）
  して M-4 を回し直し、格子を s か月ずらした経路を作る。経路どうしの test 月は重ならない（重なれば止める）。
  本番の設定の M-2* は `production_predictions` が経路ごとの test 月へそろえる。`results` は先頭の経路のまま
  （既定の出力は変わらない）で、2 本以上なら `paths`（経路ごと）と `path_summary`（経路間の超過の平均・最小・最大）を足す。
  **同じ歴史の別の切り方で、独立な標本ではない**（`PATH_NOTE`）
- **信頼区間**: 区間ごとの「純リターン − 全社平均」の平均に `model_stats.bootstrap_mean_ci`（定常ブートストラップ・
  期の並びの相関を保つ）で `excess_ci`。年率の区間（`annual_excess_ci`）は区間を同じ方法で抜き直して年率に畳む
- **入替の利得**（`swap_gain`）: 2 回目以降の入れ替えごとに「買った社 − 売った社」の次の区間のリターンの平均の差。
  予測が切れて外した社（`forced`）は売った社に入れない。要約に平均（`swap_gain_mean`）と `swap_gain_ci`
- **売買代金の帯ごとのコスト**（`--cost-bands-bps 低,中,高`・既定は無効）: 各期の月末以前の直近 52 週の売買代金の平均
  （`TURNOVER_WEEKS`・13 週未満は帯を付けず一律のコスト）で社を三分位に分け（0＝売買代金が小さい）、帯ごとの片道コストで
  引いた `net_banded` と `annual_net_banded` を並べる。売買代金は `measure_index_event.load_turnover` で絞って読む
  （週次ローダへ列を足さない）。**コストを判定へ組み込まない**（ADR-0069 の決定 5）

## TOPIX に対する超過と、帯の内訳（#1224・どちらも参考）

- **TOPIX に対する超過**（引数なしで常に出す）: 全社平均（等金額）は小型株の比重が大きく、実際には買えない基準。
  運用の成否を比べる相手は TOPIX（docs/VISION.md）なので、`macro_data` の TOPIX の系列（連動 ETF の終値・配当なし。
  系列名は `portfolio_ledger.BENCHMARK_SERIES` を借りる）の同じ区間のリターンを `index` に、年率と超過を
  `annual_index`・`annual_excess_index`・`annual_excess_index_ci` に並べる。週次の足は `database.aggregate_weeks`
  （株価の週次表を作る関数）で作り、保有側と同じ `PriceBook` で月末の週の終値を引く＝月末の決め方がそろう。
  TOPIX の値が 1 区間でも取れなければ年率は None にして `n_index_missing` に数える（0 として計算しない）
- **帯の内訳**（`--cost-bands-bps` を付けたときだけ・行の `bands`）: 保有の延べ数（区間 × 保有の社）が売買代金の
  三分位（0＝小さい・none＝帯なし）に何割ずつ入っていたかと、帯ごとの「区間リターン − 全社平均」の平均
  （`band_breakdown`）。帯は売買代金がそろう全社の三等分なので、偏りが無ければ割合は約 1/3。記述だけで検定しない
- 既定の呼び出し（`index_ret`・`index_bars`・`bands` を渡さない）では、行と要約のキーは変わらない

## 買い方の規則の計測（#1223・親 #1227・事前登録 docs/prereg/portfolio-construction.md）

- `--portfolio` で、事前登録の 7 構成（`PORTFOLIO_LABELS`）を経路ごとに回し（`run_portfolio`）、§4 の判定を機械的に出す
  （`portfolio_decision`）。凍結値の照合（`--prereg`・`--prereg-sha`）が必須で、K・k・コストは事前登録の値のまま
- 合算（`replay_combined`）は各モデルの運用を単独と同じ手続きで別々に進め、保有をその和集合にする。売買・コスト・入替率は
  和集合の出入りで数え、分母はその期の和集合の保有数。業種の上限は買うときだけ当てる（M-6 の運用が先に買う）
- 売りの並びは本番と同じ S2 に固定する（`--sell-source s2_quantile_low` でなければ終了コード 2・#1238）。日中枠のジョブは
  `measure:oof-replay-portfolio`（3 経路・積むのは親 #1227）

## 同点の数え上げ（#1259・参考）

- `replay`・`replay_combined` は買い・売りの同点を `tie_order(期, 社)`（SHA-256）の順で割る（ADR-0077）。
  以前は社の順（EDINET コードの小さい順）で、予測が全社同じ値の期はコードの小さい社をそのまま買っていた
  （2026-11-17 に M-2* の 2021-02 と M-6 の経路 +2 か月の 2021-04 で見つかった・11/18 の計測で M-6 は経路の途中の期にもあった）
- `--tie-report` を付けたときだけ、各行に期ごとの買いのスコアの同点 `buy_ties`（`buy_ties`）を足し、全社同じ値の期と、
  上位 K 社の境目をまたぐ同点がある期を表示する。予測は作り直さず、作った買いのスコアを数えるだけ。付けなければ
  行と要約のキーは変わらない。日中枠のジョブは `measure:oof-replay-ties`（3 経路）

実行:
    python -m scripts.oof_portfolio_replay                      # 既定 K=10・k=2・10bp
    python -m scripts.oof_portfolio_replay --top-k 30 --sell-k 6 --cost-bps 20
    python -m scripts.oof_portfolio_replay --start-shifts 0,1,2 --cost-bands-bps 30,15,8
    python -m scripts.oof_portfolio_replay --smoke --allow-full-pull   # 間引きパネルで動作確認
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import defaultdict, namedtuple
from contextlib import contextmanager, nullcontext
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, aggregate_weeks, tuning_dry_run, tuning_objective_only  # noqa: E402
from model_comparison import VARIANT_PRODUCTION, _production_config, _safe_rollback  # noqa: E402
from model_stats import (  # noqa: E402
    DEFAULT_AVG_BLOCK, DEFAULT_N_BOOT, DEFAULT_SEED, _percentile, _stationary_bootstrap_sample, bootstrap_mean_ci,
)
from plugins import get_plugin, macro_ensemble  # noqa: E402
from plugins.macro_snapshots import _month_end_bound, month_end_indices, oof_backtest  # noqa: E402
from plugins.model_candidates import make_diag, summarize_diag  # noqa: E402
from plugins.utils import coerce_params, walk_forward_cv_monthly  # noqa: E402
from portfolio_ledger import BENCHMARK_SERIES  # noqa: E402
from scripts import prereg  # noqa: E402
from scripts._cache import set_refresh  # noqa: E402
from scripts.ensemble_base_bakeoff import _load_panel, _run_config  # noqa: E402
from scripts.sell_mu_source_bakeoff import (  # noqa: E402
    BASES, CANDIDATE, M4_BASES_CAVEAT, SELL_S2, SHORT, buy_scores, candidate_on_common, common_to_dicts,
    sell_candidate_fit_predict,
)

_OUT = Path(__file__).resolve().parent / ".cache" / "oof_portfolio_replay.json"

# 既定値（引数で変えられる）。K=10 は ADR-0069 の参考の K の 1 つ、k は #1043 と同じく保有の 20%、
# 10bp は日本株の片道コストの目安として置いた値（実測ではない）。
DEFAULT_TOP_K = 10
DEFAULT_SELL_K = 2
DEFAULT_COST_BPS = 10.0
# 開始月のずらし（#1167）。test 月の格子の刻み（M-4 の基底の `step_months=3`）より小さいずらしだけが別の格子になる
DEFAULT_START_SHIFTS = (0,)
MAX_START_SHIFT = 2
# 売買代金の帯（#1167・参考）: 直近 52 週の平均で三分位。13 週（四半期）未満しか無い社は帯を付けず一律のコスト
TURNOVER_WEEKS = 52
MIN_TURNOVER_WEEKS = 13
N_COST_BANDS = 3

DISCLAIMER = ("過去の経路は 1 本しかなく、運とモデルの良さを区別できない。参考で、判定・ゲートには使わない"
              "（ADR-0069）")
DELIST_NOTE = ("株価が途切れた保有は、その月末以前の最後の終値で手仕舞ったとみなす（最後の取引より後の損失は"
               "捉えない＝成績は楽観側にずれる）")
PATH_NOTE = ("開始月をずらした経路は、同じ歴史を別の 3 か月おきの格子で切ったもので、独立な標本ではない。経路の間の"
             "ばらつきは開始月の運の大きさの目安（参考・#1167）")
BAND_NOTE = ("帯ごとのコストは、各期の月末以前の直近 52 週の売買代金の三分位（0＝小さい）ごとに片道コストを変えた参考の"
             "想定（実測ではない）。判定には使わない（ADR-0069 の決定 5・#1167）")

# 比べる相手の指数（#1224）。系列名は売買の記録の成績（`portfolio_ledger`・docs/VISION.md）と同じ定数を借りる＝書き写さない
INDEX_SERIES = BENCHMARK_SERIES
# 指数を読み始める日を、最初の test 月の月初からどれだけ手前にするか（日）。その月に足が無くても手前の終値へ倒せるように取る
INDEX_LOOKBACK_DAYS = 31
INDEX_NOTE = ("TOPIX は macro_data の系列（連動 ETF の終値・配当なし）。保有側の株価も配当を含まないので条件はそろう。"
              "全社平均（等金額）は実際には買えない基準なので、TOPIX に対する超過を並べる。参考で、運用の成功指標"
              "（docs/VISION.md）の判定には使わない（#1224）")
BREAKDOWN_NOTE = ("帯の内訳は、保有の延べ数（区間 × 保有の社）を各期の売買代金の三分位（0＝小さい・none＝13 週未満で帯なし）に"
                  "分けた割合と、帯ごとの「区間リターン − 全社平均」の平均。帯は売買代金がそろう全社の三等分なので、偏りが"
                  "無ければ割合は約 1/3。記述だけで検定しない・判定には使わない（ADR-0069 の決定 5・#1224）")
NO_BAND = "none"
TIE_NOTE = ("同点の数え上げ（#1259・参考）: 期ごとに買いのスコアの一意な値の数と、上位 K 社・2K 社の境目の同点を数える。"
            "同点は (期, 社) のハッシュの順で割る（ADR-0077）ので、全社同じ値の期や境目をまたぐ同点がある期は、買う社が"
            "モデルでなくハッシュの順で決まる。境目は全社の順位で数える（2 期目以降の補充は保有を除いた順位なので目安）")

# 指数の週次足。`PriceBook` が読む 2 つの属性だけを持つ（株価の週次の行 `_WEEKLY_PX` と同じ名前）
_IndexBar = namedtuple("_IndexBar", "trade_date close_last")

# 本番の設定の行（#1123）。鍵は比較画面・λ の参考と同じ `<name>:production`（model_comparison.VARIANT_PRODUCTION）
PRODUCTION_MODEL = "macro_gbdt"
PRODUCTION_KEY = f"{PRODUCTION_MODEL}:{VARIANT_PRODUCTION}"
PRODUCTION_NOTE = ("M-2* は本番の設定（探索の 1 位）の M-2 を単体の手続きのまま回した予測。最初の学習期間だけを延ばして"
                   "test 月を共通域にそろえた。設定は採点と同じ期間のデータで選んでいる＝選択の楽観を含む（#1123）")


# 売りの並びの S2（#1238）。本番の売りの並びのキー（ADR-0076）と同じ「52 週先リターンの下側 20% 点」の分位回帰で、名前は
# 計測道具（#1158・`sell_mu_source_bakeoff`）の候補名を借りる。既定の売りの並びは M-6 の μ̂ のまま（既存の計測を変えない）
SELL_S2_SOURCE = SELL_S2
SELL_SOURCES = (*BASES, SELL_S2_SOURCE)
SELL_NOTE = ("売りの並びの S2 は、M-4 の M-6 のレグと同じパネル・fold・前処理で、当てる答えを 52 週先リターンの下側 20% 点に"
             "替えた分位回帰の OOF（計測道具 #1158 と同じ関数）。予測が全社同じ値の期は、その期だけ M-6 の μ̂ で並べる"
             "（本番の `sell_ranking.choose_sell_keys` と同じ考え方・#1238）")


def _sell_label(source: str) -> str:
    return SHORT.get(source) or ("S2" if source == SELL_S2_SOURCE else source)


@contextmanager
def capture_m6_args(store: dict):
    """M-4 の M-6 のレグ（`MacroEnsemblePlugin._m6_cv`）が受け取った入力 `(b, m6p, yms)` を `store["args"]` に捕まえる。

    S2 を M-6 と同じパネル・月・fold で回すため（`sell_mu_source_bakeoff` の `--sell-candidates` と同じ手口・#1238）。
    """
    orig = macro_ensemble.plugin._m6_cv

    def spy(b, m6p, yms, fit_predict=None):
        store["args"] = (b, m6p, yms)
        return orig(b, m6p, yms, fit_predict=fit_predict)

    with patch.object(macro_ensemble.plugin, "_m6_cv", spy):
        yield store


def s2_predictions(captured: dict, common: dict, bases: list) -> tuple[dict, dict]:
    """捕まえた M-6 のレグの入力で S2 を回し、M-4 の共通域にそろえた OOF `{(ym, 社): q̂}` と学習の要約を返す（#1238）。

    学習関数は計測道具の `sell_candidate_fit_predict`（l1_ratio・内側 CV は本番の M-6 から取る）、そろえ方は
    `candidate_on_common`（共通域の行が欠けたり実現リターンが食い違えば止める）。経路をずらした回は、呼び出し側が
    `shifted_min_train` の内側で呼ぶ＝M-6 と同じ区切りになる。
    """
    if "args" not in captured:
        raise RuntimeError("M-6 のレグの入力を捕まえられなかった（_m6_cv が呼ばれていない）＝S2 を同じ入力で回せない")
    b, m6p, yms = captured["args"]
    _preds, y = common_to_dicts(common, bases)
    diag = make_diag()
    aligned, _ctx = macro_ensemble.plugin._m6_cv(
        b, m6p, yms, fit_predict=sell_candidate_fit_predict(SELL_S2_SOURCE, m6p, diag))
    return candidate_on_common(aligned, y, SELL_S2_SOURCE), summarize_diag(diag)


def s2_sell_keys(s2: dict, m6: dict) -> tuple[dict, list[str]]:
    """S2 の OOF を売りのキーにする。予測が全社同じ値の期は、その期を M-6 の μ̂ で置き換え、置き換えた期を返す（#1238）。

    同じ値では順位が付かない（#1158 の計測で 19 期のうち 2 期）。期を飛ばすと保有の入れ替えが止まって経路が変わるので、
    本番（`sell_ranking.choose_sell_keys`）と同じく、その回は M-6 の μ̂ で並べる。期の中で S2 と μ̂ は混ぜない。
    """
    by_ym: dict[str, list] = defaultdict(list)
    for key in s2:
        by_ym[key[0]].append(key)
    out: dict = {}
    fallback: list[str] = []
    for ym in sorted(by_ym):
        keys = by_ym[ym]
        vals = [s2[k] for k in keys]
        if min(vals) == max(vals):
            fallback.append(ym)
            out.update({k: m6[k] for k in keys if k in m6})
        else:
            out.update({k: s2[k] for k in keys})
    return out, fallback


# ── 買い方の規則の計測（#1223・親 #1227・事前登録 docs/prereg/portfolio-construction.md）──────────
# 値は事前登録 §2〜§4 と同じ（変えるなら文書を凍結し直す・tests が文書の文言と照合する）。
PORTFOLIO_PREREG = "docs/prereg/portfolio-construction.md"
# 合算する運用の並び。各期は M-6 の運用が先に買う（業種の上限の当て方が順に依る・事前登録 §2.2）
COMBINED_LEGS = (CANDIDATE, PRODUCTION_KEY)
INDUSTRY_CAPS = {"D4": 4, "D3": 3}
PORTFOLIO_TOP_K, PORTFOLIO_SELL_K = 10, 2          # 各運用の K・k
PORTFOLIO_TOP_K20, PORTFOLIO_SELL_K20 = 20, 4      # 保有数をそろえた対照（A20・B20）
# 構成の記号 → 表示名。C が基準（今の運用）
PORTFOLIO_LABELS = {
    "C": "合算", "D4": "合算+業種上限4", "D3": "合算+業種上限3",
    "A": "M-6", "B": "M-2*", "A20": "M-6(20社)", "B20": "M-2*(20社)",
}
PORTFOLIO_BASELINE = "C"
PORTFOLIO_CANDIDATES = ("D4", "D3", "A")           # 採る候補の優先順（今の運用からの変更が小さい順）
PORTFOLIO_N_PATHS = 3                              # 経路の数（--start-shifts 0,1,2）
PORTFOLIO_MAX_EXCESS_DROP = 0.02                   # TOPIX に対する年率の超過の低下の許容（3 経路の平均）
_FLOAT_TOL = 1e-12                                 # 「−0.02 以上」を小数の丸め誤差で外さないための許容
PORTFOLIO_NOTE = ("買い方の規則の計測（#1227・事前登録 docs/prereg/portfolio-construction.md）。合算は各モデルの運用を別々に"
                  "進めた保有の和集合で、業種の上限は買うときだけ当てる。業種は今の値（過去の業種は持っていない）。"
                  "運用再現は参考（ADR-0069）で、この計測が決めるのは保有の組み方だけ")


class NotAligned(ValueError):
    """本番の設定の M-2 の test 月を共通域にそろえられない。"""


class PriceBook:
    """社ごとの月末の週の終値と、ある月末以前の最後の終値を引く。

    月末の週は `month_end_indices`（学習ラベルの基準週と同じ定義）で決める。その月に足が無い社
    （上場廃止・売買停止）は、`_month_end_bound` 以前の最後の正の終値へ倒し、「途切れた」と印を付ける。
    """

    def __init__(self, prices_by_co: dict):
        self._month_end: dict[str, dict[str, float]] = {}
        self._dates: dict[str, list[str]] = {}
        self._closes: dict[str, list[float]] = {}
        for ec, rows in prices_by_co.items():
            dates = [r.trade_date for r in rows]
            self._month_end[ec] = {dates[i][:7]: rows[i].close_last for i in month_end_indices(dates)}
            valid = [(r.trade_date, r.close_last) for r in rows if r.close_last and r.close_last > 0]
            self._dates[ec] = [d for d, _c in valid]
            self._closes[ec] = [c for _d, c in valid]

    def close_at(self, ec: str, ym: str) -> tuple[float | None, bool]:
        """`(終値, 途切れたか)`。月末の週の終値が正ならそれ、無ければ月末以前の最後の正の終値（途切れた）。"""
        c = (self._month_end.get(ec) or {}).get(ym)
        if c and c > 0:
            return float(c), False
        dates = self._dates.get(ec) or []
        hi = bisect.bisect_right(dates, _month_end_bound(ym))
        if hi == 0:
            return None, False
        return float(self._closes[ec][hi - 1]), True

    def period_return(self, ec: str, ym0: str, ym1: str) -> tuple[float | None, bool]:
        """ym0 の月末から ym1 の月末までのリターンと、ym1 で株価が途切れていたか。

        ym0 の月末の終値が無い社（その期に予測があれば起きない）は None。
        """
        c0, stale0 = self.close_at(ec, ym0)
        if c0 is None or stale0:
            return None, False
        c1, stale1 = self.close_at(ec, ym1)
        if c1 is None:
            return None, False
        return c1 / c0 - 1.0, stale1


def months_between(ym0: str, ym1: str) -> int:
    y0, m0 = map(int, ym0.split("-"))
    y1, m1 = map(int, ym1.split("-"))
    return (y1 - y0) * 12 + (m1 - m0)


def tie_order(ym: str, ec: str) -> bytes:
    """同点を割る順（ADR-0077・#1259）。`(期, 社)` の SHA-256 で、毎回同じ・入力の順に依らない・コード順に偏らない。

    社だけで決めると毎期同じ社が先に来る（コード順と同じ種類の固定の偏り）ので期も混ぜる。`hash()` は起動ごとに
    変わるので使わない。予測が全社同じ値の期は、事実上の無作為な社を買う（期待値は全社平均）。
    """
    return hashlib.sha256(f"{ym}|{ec}".encode()).digest()


def replay(periods: list[str], buy: dict, sell: dict,
           ret: Callable[[str, str, str], tuple[float | None, bool]],
           top_k: int, sell_k: int, cost_bps: float,
           band_cost: Callable[[str, str], float] | None = None,
           index_ret: Callable[[str, str], tuple[float | None, bool]] | None = None) -> list[dict]:
    """入れ替えを periods の順に繰り返し、区間ごとの明細を返す。

    buy = {(ym, 社): 買いのスコア}（高いほど買う）、sell = {(ym, 社): 売りのキー}（低いほど先に売る）。
    その期に予測がある社＝両方がそろう社。ret(社, ym0, ym1) は `(リターン, 途切れたか)`。
    区間 i は periods[i] で組んだ保有を periods[i+1] まで持った結果。最後の期では入れ替えない。
    同点は `tie_order(期, 社)` の順で割る（入力の順に依らない・ADR-0077）。

    参考（#1167）: `swap_gain` は 2 回目以降の入れ替えで「買った社 − 売った社」の、この区間のリターンの平均の差
    （どちらかの社のリターンが 1 つも取れない区間と初回は None）。予測が切れて外した社（`forced`）は売った社に
    入れない。`band_cost(ym, 社)` を渡すと、売った・外した・買った社ごとの片道コスト（bp）で引いた
    `cost_banded`・`net_banded` を足す（既定の `cost`・`net` は変えない）。

    参考（#1224）: `index_ret(ym0, ym1)`（`(指数のリターン, 月末の週の値が無く手前の値で代用したか)`）を渡すと、
    同じ区間の指数のリターン `index`（取れなければ None）と `index_stale` を足す。渡さなければキーは増えない。
    """
    by_ym: dict[str, set] = {}
    for ym, ec in buy.keys() & sell.keys():
        by_ym.setdefault(ym, set()).add(ec)
    cost_rate = cost_bps / 10_000.0
    holdings: list[str] = []
    intervals: list[dict] = []
    for i, ym in enumerate(periods[:-1]):
        domain = by_ym.get(ym, set())
        forced = sorted(ec for ec in holdings if ec not in domain)
        keep = [ec for ec in holdings if ec in domain]
        sold = sorted(keep, key=lambda ec: (sell[(ym, ec)], tie_order(ym, ec)))[:min(sell_k, len(keep))] if i else []
        kept = set(keep) - set(sold)
        candidates = sorted((ec for ec in domain if ec not in kept and ec not in sold),
                            key=lambda ec: (-buy[(ym, ec)], tie_order(ym, ec)))
        bought = candidates[:max(0, top_k - len(kept))]
        holdings = sorted(kept | set(bought))
        cost = cost_rate * (len(sold) + len(forced) + len(bought)) / top_k

        nxt = periods[i + 1]
        rets, stopped, missing = [], 0, 0
        for ec in holdings:
            r, stale = ret(ec, ym, nxt)
            if r is None:
                missing += 1
                continue
            rets.append(r)
            stopped += stale
        bench = [r for r, _s in (ret(ec, ym, nxt) for ec in sorted(domain)) if r is not None]
        gross = statistics.mean(rets) if rets else 0.0
        sold_r = [r for r, _s in (ret(ec, ym, nxt) for ec in sold) if r is not None]
        bought_r = [r for r, _s in (ret(ec, ym, nxt) for ec in bought) if r is not None]
        row = {
            "from": ym, "to": nxt, "months": months_between(ym, nxt),
            "holdings": holdings, "sold": sold, "forced": forced, "bought": bought,
            "gross": gross, "cost": cost, "net": gross - cost,
            "bench": statistics.mean(bench) if bench else 0.0,
            "n_domain": len(domain), "n_stopped": stopped, "n_missing": missing,
            "swap_gain": (statistics.mean(bought_r) - statistics.mean(sold_r)) if i and sold_r and bought_r else None,
        }
        if band_cost is not None:
            row["cost_banded"] = sum(band_cost(ym, ec) for ec in sold + forced + bought) / 10_000.0 / top_k
            row["net_banded"] = gross - row["cost_banded"]
        if index_ret is not None:
            row["index"], row["index_stale"] = index_ret(ym, nxt)
        intervals.append(row)
    return intervals


def _industry_counts(holdings, industry_of: dict) -> dict:
    """保有の業種ごとの社数。業種が空の社は数えない（上限の対象にしない・事前登録 §2.2）。"""
    counts: dict[str, int] = {}
    for ec in holdings:
        ind = industry_of.get(ec)
        if ind:
            counts[ind] = counts.get(ind, 0) + 1
    return counts


def replay_combined(periods: list[str], buys: dict[str, dict], sell: dict,
                    ret: Callable[[str, str, str], tuple[float | None, bool]],
                    top_k: int, sell_k: int, cost_bps: float,
                    industry_of: dict | None = None, industry_cap: int | None = None,
                    index_ret: Callable[[str, str], tuple[float | None, bool]] | None = None) -> list[dict]:
    """複数の運用（`buys` = {運用の名前: 買いのスコア}）を別々に進め、保有をその和集合にした入れ替え（#1223）。

    各運用の手続きは `replay` と同じ（K 社を持ち、2 回目以降は自分の保有の売りのキーの下位 k 社を売り、自分の並びの
    上位から K 社まで補充する・その期に自分が売った社は買い戻さない）。和集合の社に等金額で配る（両方が持つ社は 1 社）。

    - 売買の数は**和集合から出た社と入った社**で数える。片方の運用が売っても、もう片方が持ち続ける・買う社は売買に
      数えない。出た社のうち、どの運用にもその期の予測が無い社は `forced`、それ以外は `sold`
    - コストと入替率の分母は、その期の和集合の保有数（行の `size`。`summarize` の入替率もこれで割る）
    - `industry_cap` を渡すと、買うとき（最初の期と補充）だけ業種の上限を当てる。その時点の和集合（売った後の保有＋
      この期に先の運用が買った社）でその社の業種が上限に達していれば、その社を飛ばして次の順位へ進む。すでに和集合に
      ある社は業種の数を増やさないので買える。業種が空の社は数えない。上限のために持っている社を売らない
    - 買う順は `buys` の順（`COMBINED_LEGS` は M-6 が先）

    行には `replay` と同じ列に加えて、`size`（和集合の保有数）・`n_overlap`（2 つ以上の運用が持つ社数）・
    `max_industry`（業種ごとの保有数の最大）・`n_cap_skipped`（上限で飛ばした社の数）を足す。
    """
    industry_of = industry_of or {}
    legs = list(buys)
    domain: dict[str, dict[str, set]] = {}
    for leg in legs:
        by_ym: dict[str, set] = {}
        for ym, ec in buys[leg].keys() & sell.keys():
            by_ym.setdefault(ym, set()).add(ec)
        domain[leg] = by_ym
    cost_rate = cost_bps / 10_000.0
    holdings: dict[str, list[str]] = {leg: [] for leg in legs}
    union_prev: set = set()
    intervals: list[dict] = []
    for i, ym in enumerate(periods[:-1]):
        kept: dict[str, set] = {}
        sold_by: dict[str, set] = {}
        for leg in legs:
            dom = domain[leg].get(ym, set())
            keep = [ec for ec in holdings[leg] if ec in dom]
            sold = sorted(keep, key=lambda ec: (sell[(ym, ec)], tie_order(ym, ec)))[:min(sell_k, len(keep))] if i else []
            kept[leg], sold_by[leg] = set(keep) - set(sold), set(sold)
        union_now: set = set().union(*kept.values())
        skipped = 0
        for leg in legs:
            need = top_k - len(kept[leg])
            got: list[str] = []
            for ec in sorted((ec for ec in domain[leg].get(ym, set())
                              if ec not in kept[leg] and ec not in sold_by[leg]),
                             key=lambda ec: (-buys[leg][(ym, ec)], tie_order(ym, ec))):
                if len(got) >= need:
                    break
                ind = industry_of.get(ec)
                if (industry_cap is not None and ec not in union_now and ind
                        and _industry_counts(union_now, industry_of).get(ind, 0) >= industry_cap):
                    skipped += 1
                    continue
                got.append(ec)
                union_now.add(ec)
            holdings[leg] = sorted(kept[leg] | set(got))
        union = sorted(union_now)
        domain_any = set().union(*(domain[leg].get(ym, set()) for leg in legs))
        left = union_prev - union_now
        forced = sorted(ec for ec in left if ec not in domain_any)
        sold_u = sorted(left - set(forced))
        bought = sorted(union_now - union_prev)
        size = len(union)
        cost = cost_rate * (len(sold_u) + len(forced) + len(bought)) / size if size else 0.0

        nxt = periods[i + 1]
        rets, stopped, missing = [], 0, 0
        for ec in union:
            r, stale = ret(ec, ym, nxt)
            if r is None:
                missing += 1
                continue
            rets.append(r)
            stopped += stale
        bench = [r for r, _s in (ret(ec, ym, nxt) for ec in sorted(domain_any)) if r is not None]
        gross = statistics.mean(rets) if rets else 0.0
        sold_r = [r for r, _s in (ret(ec, ym, nxt) for ec in sold_u) if r is not None]
        bought_r = [r for r, _s in (ret(ec, ym, nxt) for ec in bought) if r is not None]
        held_by = [ec for ec in union if sum(ec in holdings[leg] for leg in legs) >= 2]
        row = {
            "from": ym, "to": nxt, "months": months_between(ym, nxt),
            "holdings": union, "sold": sold_u, "forced": forced, "bought": bought,
            "gross": gross, "cost": cost, "net": gross - cost,
            "bench": statistics.mean(bench) if bench else 0.0,
            "n_domain": len(domain_any), "n_stopped": stopped, "n_missing": missing,
            "swap_gain": (statistics.mean(bought_r) - statistics.mean(sold_r)) if i and sold_r and bought_r else None,
            "size": size, "n_overlap": len(held_by),
            "max_industry": max(_industry_counts(union, industry_of).values(), default=0),
            "n_cap_skipped": skipped,
        }
        if index_ret is not None:
            row["index"], row["index_stale"] = index_ret(ym, nxt)
        intervals.append(row)
        union_prev = union_now
    return intervals


def construction_stats(intervals: list[dict], industry_of: dict) -> dict:
    """保有の組み方の参考の値（事前登録 §3）: 期ごとの保有数・重複した社数・業種ごとの保有数の最大・上限で飛ばした数。"""
    if not intervals:
        return {"mean_holdings": None, "min_holdings": None, "mean_overlap": None,
                "max_industry": None, "n_cap_skipped": 0}
    sizes = [len(iv["holdings"]) for iv in intervals]
    return {
        "mean_holdings": statistics.mean(sizes), "min_holdings": min(sizes),
        "mean_overlap": statistics.mean(iv.get("n_overlap", 0) for iv in intervals),
        "max_industry": max(max(_industry_counts(iv["holdings"], industry_of).values(), default=0)
                            for iv in intervals),
        "n_cap_skipped": sum(iv.get("n_cap_skipped", 0) for iv in intervals),
    }


def _annualized(rets: list[float], years: float) -> float | None:
    wealth = math.prod(1.0 + r for r in rets)
    if years <= 0 or wealth <= 0:
        return None
    return wealth ** (1.0 / years) - 1.0


def max_drawdown(rets: list[float]) -> float:
    """区間ごとの資産の推移（始め 1）で、それまでの高値からの最大の下落率。"""
    wealth = peak = 1.0
    worst = 0.0
    for r in rets:
        wealth *= 1.0 + r
        peak = max(peak, wealth)
        worst = max(worst, 1.0 - wealth / peak)
    return worst


def summarize(intervals: list[dict], top_k: int) -> dict:
    """年率（コスト前・後・全社平均）・超過・最大下落・入替率。年率は実際の経過月数で割る幾何平均。

    入替率は、2 回目以降の入れ替えで手放した社数（予測が無くなった社を含む）÷ K の平均。合算の行（`replay_combined`）は
    K の代わりに、その期の和集合の保有数（行の `size`）で割る（#1223）。
    超過は、年率（コスト後）− 全社平均の年率。

    参考（#1167）: `excess_ci` は区間ごとの「純リターン − 全社平均」の平均の定常ブートストラップ CI
    （`bootstrap_mean_ci`）、`annual_excess_ci` は年率の超過の CI（`annual_excess_ci`）、`swap_gain_mean`・
    `swap_gain_ci` は入替の利得。全区間に `net_banded` があれば帯ごとのコストの年率（`annual_net_banded`）も出す。

    参考（#1224）: 区間に `index`（指数のリターン）があれば、指数の年率 `annual_index`・年率（コスト後）との差
    `annual_excess_index`・その CI `annual_excess_index_ci` を足す。**指数の値が 1 区間でも取れなければ 3 つとも None**
    にして `n_index_missing` に数える（欠けた区間を 0 として繋ぐと、もっともらしい年率が黙って出る）。
    """
    if not intervals:
        return {"n_intervals": 0}
    years = sum(iv["months"] for iv in intervals) / 12.0
    ann_net = _annualized([iv["net"] for iv in intervals], years)
    ann_bench = _annualized([iv["bench"] for iv in intervals], years)
    turns = [(len(iv["sold"]) + len(iv["forced"])) / (iv.get("size") or top_k) for iv in intervals[1:]]
    gains = [iv["swap_gain"] for iv in intervals if iv.get("swap_gain") is not None]
    out = {
        "n_intervals":       len(intervals),
        "years":             round(years, 4),
        "annual_gross":      _annualized([iv["gross"] for iv in intervals], years),
        "annual_net":        ann_net,
        "annual_bench":      ann_bench,
        "annual_excess":     (ann_net - ann_bench) if ann_net is not None and ann_bench is not None else None,
        "max_drawdown":      max_drawdown([iv["net"] for iv in intervals]),
        "turnover":          statistics.mean(turns) if turns else None,
        "n_stopped":         sum(iv["n_stopped"] for iv in intervals),
        "n_missing":         sum(iv["n_missing"] for iv in intervals),
        "excess_ci":         bootstrap_mean_ci([iv["net"] - iv["bench"] for iv in intervals]),
        "annual_excess_ci":  annual_excess_ci(intervals),
        "swap_gain_mean":    statistics.mean(gains) if gains else None,
        "swap_gain_ci":      bootstrap_mean_ci(gains),
        "n_swaps":           len(gains),
    }
    if all("net_banded" in iv for iv in intervals):
        ann_banded = _annualized([iv["net_banded"] for iv in intervals], years)
        out["annual_net_banded"] = ann_banded
        out["annual_excess_banded"] = (ann_banded - ann_bench
                                       if ann_banded is not None and ann_bench is not None else None)
    if any("index" in iv for iv in intervals):
        missing = sum(1 for iv in intervals if iv.get("index") is None)
        ann_index = _annualized([iv["index"] for iv in intervals], years) if not missing else None
        out["annual_index"] = ann_index
        out["annual_excess_index"] = (ann_net - ann_index
                                      if ann_net is not None and ann_index is not None else None)
        out["annual_excess_index_ci"] = annual_excess_ci(intervals, bench_key="index") if not missing else None
        out["n_index_missing"] = missing
        out["n_index_stale"] = sum(1 for iv in intervals if iv.get("index_stale"))
    return out


def annual_excess_ci(intervals: list[dict], *, n_boot: int = DEFAULT_N_BOOT, avg_block: float = DEFAULT_AVG_BLOCK,
                     seed: int = DEFAULT_SEED, alpha: float = 0.05, bench_key: str = "bench") -> dict | None:
    """年率の超過（年率（コスト後）− 全社平均の年率）の定常ブートストラップ CI（参考・#1167）。

    区間を `model_stats._stationary_bootstrap_sample` で丸ごと抜き直し（純リターン・全社平均・月数を同じ区間から取る）、
    `summarize` と同じ幾何平均の年率に畳む。抜き直しの回数・平均ブロック長・種は `model_stats` の既定。
    2 区間未満、または年率が出せない（資産が 0 以下になる）抜き直しばかりなら None。
    `bench_key` は比べる相手の列（既定は全社平均の `bench`。指数に対する超過は `index`・#1224）。
    """
    if len(intervals) < 2:
        return None
    rng = random.Random(seed)
    vals: list[float] = []
    for _ in range(n_boot):
        pick = _stationary_bootstrap_sample(intervals, rng, avg_block)
        years = sum(iv["months"] for iv in pick) / 12.0
        net = _annualized([iv["net"] for iv in pick], years)
        bench = _annualized([iv[bench_key] for iv in pick], years)
        if net is not None and bench is not None:
            vals.append(net - bench)
    if not vals:
        return None
    vals.sort()
    return {"ci_lo": _percentile(vals, 100 * alpha / 2), "ci_hi": _percentile(vals, 100 * (1 - alpha / 2)),
            "n": len(intervals), "n_boot": len(vals)}


@contextmanager
def shifted_min_train(shift: int):
    """M-4 の基底の walk-forward の最初の学習期間を +shift か月して回す（#1167・計測の中でだけ）。

    `plugins.macro_ensemble.walk_forward_cv_monthly` を包み、`min_train_months` に shift を足す。学習の手続きは
    そのままで、test 月の格子だけが shift か月後ろへずれる（`aligned_min_train` と同じ考え）。本番のコード
    （`macro_ensemble` の `min_train_months=6`）は変えない。キーワードで渡されていない呼び出しは、ずらし損ねて
    黙って同じ格子になるので止める。shift=0 は何もしない。
    """
    if shift == 0:
        yield
        return
    from plugins import macro_ensemble

    def shifted(samples_by_ym, feature_names, **kwargs):
        if "min_train_months" not in kwargs:
            raise RuntimeError("M-4 の基底の walk-forward に min_train_months がキーワードで渡されていない"
                               "（ずらせないので止める）")
        return walk_forward_cv_monthly(samples_by_ym, feature_names,
                                       **{**kwargs, "min_train_months": kwargs["min_train_months"] + shift})

    with patch.object(macro_ensemble, "walk_forward_cv_monthly", side_effect=shifted):
        yield


def check_paths_disjoint(test_months: dict) -> None:
    """経路（{ずらし: test 月の集合}）どうしの test 月が重ならないことを確かめる。重なれば RuntimeError。

    重なるのは、ずらしが効かずに同じ格子で回ったとき（M-4 の呼び方が変わった等）。そのまま並べると「経路の間の
    ばらつき」が小さく見える＝例外を出さずにもっともらしい値を返すので止める。
    """
    shifts = sorted(test_months)
    for i, a in enumerate(shifts):
        for b in shifts[i + 1:]:
            both = sorted(test_months[a] & test_months[b])
            if both:
                raise RuntimeError(f"ずらし {a} と {b} の経路の test 月が重なる（{both[:3]}）。ずらしが効いていない")


def _month_end_date(ym: str) -> date:
    y, m = map(int, ym.split("-"))
    return date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)


def turnover_bands(turnover: dict, periods: Sequence[str], n_bins: int = N_COST_BANDS,
                   weeks: int = TURNOVER_WEEKS, min_weeks: int = MIN_TURNOVER_WEEKS) -> dict:
    """{(ym, 社): 帯}（0＝売買代金が小さい・#1167）。

    turnover = {社: {週の月曜(date): 売買代金}}（`measure_index_event.load_turnover` の形）。各期 ym について、
    その月末以前の直近 `weeks` 週の売買代金の平均を、`min_weeks` 週以上そろう社の中で `n_bins` 等分する
    （同値は社の順・`index_event_study._bins` と同じ切り方）。そろわない社は入れない（呼び出し側が一律のコストへ倒す）。
    """
    out: dict = {}
    for ym in periods:
        hi = _month_end_date(ym)
        lo = hi - timedelta(weeks=weeks)
        means = {}
        for ec, by_week in turnover.items():
            vals = [v for d, v in by_week.items() if lo < d <= hi]
            if len(vals) >= min_weeks:
                means[ec] = statistics.mean(vals)
        ordered = sorted(means, key=lambda ec: (means[ec], ec))
        for j, ec in enumerate(ordered):
            out[(ym, ec)] = j * n_bins // len(ordered)
    return out


def make_band_cost(bands: dict, band_bps: Sequence[float], flat_bps: float) -> Callable[[str, str], float]:
    """`replay` の `band_cost`。帯のある社は帯の片道コスト（bp）、帯の無い社は一律の `flat_bps`。"""
    def cost(ym: str, ec: str) -> float:
        band = bands.get((ym, ec))
        return float(band_bps[band]) if band is not None else float(flat_bps)
    return cost


def band_breakdown(intervals: list[dict], bands: dict,
                   ret: Callable[[str, str, str], tuple[float | None, bool]]) -> dict:
    """保有の延べ数（区間 × 保有の社）を売買代金の帯に分けた内訳（#1224・記述だけで検定しない）。

    戻り値は `{"0": {...}, "1": {...}, "2": {...}, "none": {...}}`（0＝売買代金が小さい・`none`＝帯の無い社）。
    帯は保有を組んだ期（区間の始め）のもの＝その月末より後の売買代金を見ない（`turnover_bands`）。各帯に:

    - `n_slots`・`share`: 延べ数と、全体に占める割合。帯は売買代金がそろう全社の三等分なので、偏りが無ければ約 1/3
    - `n_returns`・`mean_excess`: リターンが取れた延べ数と、その「区間リターン − その区間の全社平均」の平均（コスト前）
    - `contribution`: その帯の超過の合計 ÷ リターンが取れた延べ数の総数。**全ての帯で足すと、保有全体の延べ数あたりの
      平均超過になる**（どの帯が超過を運んだかの分解）
    """
    keys = [str(b) for b in range(N_COST_BANDS)] + [NO_BAND]
    slots = {k: 0 for k in keys}
    excess: dict[str, list[float]] = {k: [] for k in keys}
    for iv in intervals:
        for ec in iv["holdings"]:
            band = bands.get((iv["from"], ec))
            key = NO_BAND if band is None else str(band)
            slots[key] += 1
            r, _stale = ret(ec, iv["from"], iv["to"])
            if r is not None:
                excess[key].append(r - iv["bench"])
    n_slots = sum(slots.values())
    n_returns = sum(len(v) for v in excess.values())
    return {k: {"n_slots": slots[k],
                "share": slots[k] / n_slots if n_slots else None,
                "n_returns": len(excess[k]),
                "mean_excess": statistics.mean(excess[k]) if excess[k] else None,
                "contribution": sum(excess[k]) / n_returns if n_returns else None}
            for k in keys}


def index_since(first_ym: str) -> str:
    """指数を読み始める日（最初の test 月の月初の `INDEX_LOOKBACK_DAYS` 日前・ISO 文字列）。"""
    y, m = map(int, first_ym.split("-"))
    return (date(y, m, 1) - timedelta(days=INDEX_LOOKBACK_DAYS)).isoformat()


def load_index_daily(db, since: str) -> dict:
    """比べる相手の指数（`INDEX_SERIES`）の日次の終値 `{日付: 終値}`（#1224・読むだけ）。

    `since`（ISO 文字列）以降。2 列だけ引く（`preload_macro` と同じ理由＝ORM 行を引かない）。`close` は空を許さない列。
    """
    from database import MacroData as M
    rows = (db.query(M.trade_date, M.close)
            .filter(M.series_code == INDEX_SERIES, M.trade_date >= since).all())
    return {r.trade_date: float(r.close) for r in rows}


def index_weekly_bars(daily: dict) -> list:
    """日次の終値 `{日付: 終値}` を、株価の週次表と同じ規則の週次足（日付の昇順）にする（#1224）。

    `database.aggregate_weeks`（`stock_price_weekly` を作る関数）をそのまま使う＝足の日付は週の最後の営業日、終値は
    その日の値。`PriceBook` は月末の週を足の日付の月で決める（`month_end_indices`）ので、**週の月曜を日付にすると
    月をまたぐ週で保有側と 1 週ずれる**（`portfolio_ledger.load_benchmark` を使わない理由）。
    """
    weeks = aggregate_weeks((INDEX_SERIES, d, c, None) for d, c in daily.items())
    return sorted((_IndexBar(w["trade_date"], w["close_last"]) for w in weeks), key=lambda b: b.trade_date)


def path_summary(paths: dict) -> dict:
    """{買いの並び: {annual_excess: {ずらし: 値}, mean, min, max, n_paths}}（経路の間のばらつき・参考・#1167）。"""
    names = [n for res in paths.values() for n in res]
    out: dict = {}
    for name in dict.fromkeys(names):
        by_shift = {str(s): (res.get(name) or {}).get("summary", {}).get("annual_excess")
                    for s, res in paths.items()}
        vals = [v for v in by_shift.values() if v is not None]
        out[name] = {"annual_excess": by_shift, "n_paths": len(vals),
                     "mean": statistics.mean(vals) if vals else None,
                     "min": min(vals) if vals else None, "max": max(vals) if vals else None}
    return out


def aligned_min_train(yms: Sequence[str], first_test: str, min_train: int, embargo: int) -> int | None:
    """walk-forward の最初の test 月を `first_test` にする最初の学習期間（月数）。そろえられなければ None。

    test 月は `yms[min_train + embargo]` から step おき（`walk_forward_cv_monthly`）。最初の学習期間を延ばすと、
    学習の手続きはそのままで test 月だけが後ろへずれる。本番の最小より短くはしない（短くなるなら None）。
    """
    yms = sorted(yms)
    if first_test not in yms:
        return None
    shifted = yms.index(first_test) - embargo
    return shifted if shifted >= min_train else None


def production_predictions(panel: tuple, db, test_yms: Sequence[str]) -> tuple[dict | None, dict]:
    """本番の設定の M-2 の OOF 予測 `{(ym, 社): 予測}` と付記。回せなければ予測は None で付記に理由（#1123）。

    設定は `plugin_tuned_params` を比較画面と同じ射影（`_production_config`）で写し、`predictions_with_params` で回す。
    """
    plugin = get_plugin(PRODUCTION_MODEL)
    params, info = _production_config(db, plugin, PRODUCTION_MODEL, {})
    if params is None:
        return None, {"available": False, **info}
    preds, _oof, extra = predictions_with_params(panel, db, test_yms, params)
    if preds is None:
        return None, {"available": False, **info, **extra}
    return preds, {"available": True, **info, **extra}


def predictions_with_params(panel: tuple, db, test_yms: Sequence[str],
                            params: dict) -> tuple[dict | None, dict | None, dict]:
    """M-2 を params で単体の手続きのまま回した OOF 予測 `{(ym, 社): 予測}`、その回の `oof_backtest`（M-2 自身の
    母集団）、付記。そろえられない・回せないときは予測と oof は None で、付記に理由（#1123・#1155）。

    `oof_backtest` へ渡る予測と社の並び（`meta_by_ym`）を横取りする。変えるのは最初の学習期間だけで、test 月を
    `test_yms`（M-4 の共通域）の最初の月から始める（モジュールの docstring）。読み込み済みのパネルを使い回す（DB から
    引き直さない。空欄埋めは M-2 自身が行う）。`tuning_dry_run`・`tuning_objective_only` の内側で回す＝本番の μ̂ と
    探索の結果を上書きしない。種は呼び出し側が `plugins.tuning.seed_override` で変えられる（既定は M-2 の既定の種）。
    """
    plugin = get_plugin(PRODUCTION_MODEL)
    test_yms = sorted(test_yms)
    if not test_yms:
        return None, None, {"reason": "not_aligned", "error": "共通域に test 月が無い"}

    def aligned_wf(samples_by_ym, feature_names, **kwargs):
        m = aligned_min_train(list(samples_by_ym), test_yms[0], kwargs.get("min_train_months", 18),
                              kwargs.get("embargo_months", 0))
        if m is None:
            raise NotAligned(f"test 月を共通域の最初の月（{test_yms[0]}）にそろえられない")
        return walk_forward_cv_monthly(samples_by_ym, feature_names, **{**kwargs, "min_train_months": m})

    prices_by_co, fin_by_co, companies, macro_cache = panel
    try:
        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value=macro_cache), \
             patch("plugins.macro_gbdt.walk_forward_cv_monthly", side_effect=aligned_wf), \
             patch("plugins.macro_gbdt.oof_backtest", wraps=oof_backtest) as spy, \
             tuning_dry_run(), tuning_objective_only():
            result = plugin.execute(coerce_params(plugin.params_schema(), params), db)
    except NotAligned as e:
        return None, None, {"reason": "not_aligned", "error": str(e)}
    except Exception as e:  # noqa: BLE001 — 1 つの設定が回せなくても呼び出し側の他の行は出す
        _safe_rollback(db)
        return None, None, {"reason": "error", "error": str(e)}
    if spy.call_args is None:
        return None, None, {"reason": "error", "error": "M-2 の OOF を捕まえられなかった（oof_backtest が呼ばれていない）"}
    resid = spy.call_args.args[0]
    meta = spy.call_args.kwargs.get("meta_by_ym") or {}
    preds = {(ym, sid): float(yh) for ym, rows in resid.items()
             for (yh, _y), (sid, _industry) in zip(rows, meta.get(ym, [])) if sid is not None}
    matched = sorted(set(resid) & set(test_yms))
    if not matched:
        return None, None, {"reason": "not_aligned", "error": "test 月が共通域と 1 期も重ならない"}
    oof = result.get("oof_backtest") if isinstance(result, dict) else None
    return preds, oof, {"n_periods_target": len(test_yms), "n_periods_matched": len(matched)}


def buy_ties(buy: dict, ks: Sequence[int]) -> dict:
    """期ごとの買いのスコアの同点（#1259・参考）。`{ym: {n, n_unique, flat, at_k}}` を返す。

    `at_k[str(k)]` は全社の順位で k 番目の社と同じスコアの社の数 `tied` と、そのかたまりが k 番目と k+1 番目を
    またぐか `crosses`（またぐなら、上位 k 社に入る社は `tie_order` の順で決まる）。社が k 社以下の期は境目が無い（None）。
    スコアは `replay` と同じく厳密な等号で比べる。
    """
    by_ym: dict[str, list[float]] = defaultdict(list)
    for (ym, _ec), v in buy.items():
        by_ym[ym].append(v)
    out: dict[str, dict] = {}
    for ym in sorted(by_ym):
        vals = sorted(by_ym[ym], reverse=True)
        n_unique = len(set(vals))
        at_k = {}
        for k in ks:
            if len(vals) <= k:
                at_k[str(k)] = {"tied": None, "crosses": None}
                continue
            v = vals[k - 1]
            at_k[str(k)] = {"tied": vals.count(v), "crosses": vals[k] == v}
        out[ym] = {"n": len(vals), "n_unique": n_unique, "flat": n_unique == 1, "at_k": at_k}
    return out


def run_replays(common: dict, bases: list, prices_by_co: dict, db, models: list[str],
                sell_source: str, top_k: int, sell_k: int, cost_bps: float,
                production: dict | None = None, extra_m2: dict | None = None,
                band_cost: Callable[[str, str], float] | None = None,
                index_bars: list | None = None, bands: dict | None = None,
                sell_keys: dict | None = None, tie_report: bool = False) -> dict:
    """買いの並び（models）ごとに OOF 運用再現を回す。

    `production`（本番の設定の M-2 の `{(ym, 社): 予測}`・#1123）があり models に M-2 が入っていれば、M-2 の行の
    直後に `PRODUCTION_KEY` の行を足す。共通域（y がある鍵）の外の予測は落とし、件数を `coverage` に出す。
    `extra_m2`（`{行の名前: M-2 の予測}`・#1155 の時間順に選んだ M-2 など）は、M-2 の並び方（`buy_scores`）で
    末尾に足す（models に M-2 が無くても足す）。`band_cost`（#1167）は全ての行の `replay` へそのまま渡す。

    参考（#1224）: `index_bars`（`index_weekly_bars` の戻り）を渡すと、保有側と同じ `PriceBook` で月末の週の終値を引いて
    全ての行に指数に対する超過を足す。`bands`（`turnover_bands` の戻り）を渡すと、行に帯の内訳 `bands` を足す。
    どちらも渡さなければ（既定）、行と要約のキーは変わらない。

    `sell_keys`（#1238）を渡すと、売りの並びは `preds[sell_source]` ではなくそれ（S2 の売りのキー・`s2_sell_keys`）を使う。

    `tie_report`（#1259）が真なら、全ての行に買いのスコアの同点 `buy_ties`（上位 K 社・2K 社の境目）を足す。
    """
    preds, y = common_to_dicts(common, bases)
    book = PriceBook(prices_by_co)
    sell = sell_keys if sell_keys is not None else preds[sell_source]
    index_book = PriceBook({INDEX_SERIES: index_bars}) if index_bars is not None else None

    def index_ret(ym0: str, ym1: str) -> tuple[float | None, bool]:
        return index_book.period_return(INDEX_SERIES, ym0, ym1)

    def one(name: str, pred: dict) -> dict:
        buy, ordering = buy_scores(name, pred, y, prices_by_co, db)
        periods = sorted({ym for ym, _ec in buy.keys() & sell.keys()})
        intervals = replay(periods, buy, sell, book.period_return, top_k, sell_k, cost_bps, band_cost,
                           index_ret if index_book is not None else None)
        row = {"ordering": ordering, "summary": summarize(intervals, top_k), "intervals": intervals}
        if bands is not None:
            row["bands"] = band_breakdown(intervals, bands, book.period_return)
        if tie_report:
            row["buy_ties"] = buy_ties(buy, (top_k, 2 * top_k))
        return row

    def coverage(pred: dict, inside: dict) -> dict:
        return {"n_rows": len(inside), "n_outside_common": len(pred) - len(inside),
                "n_common_without_prediction": len(y.keys() - inside.keys())}

    out: dict[str, dict] = {}
    for name in models:
        out[name] = one(name, preds[name])
        if name == PRODUCTION_MODEL and production is not None:
            inside = {k: v for k, v in production.items() if k in y}
            out[PRODUCTION_KEY] = {
                **one(name, inside), "selection_optimism": True,
                "coverage": {"n_rows": len(inside), "n_outside_common": len(production) - len(inside),
                             "n_common_without_prediction": len(preds[name].keys() - inside.keys())},
            }
    for key, pred in (extra_m2 or {}).items():
        inside = {k: v for k, v in pred.items() if k in y}
        out[key] = {**one(PRODUCTION_MODEL, inside), "coverage": coverage(pred, inside)}
    return out


def run_portfolio(common: dict, bases: list, prices_by_co: dict, db, production: dict | None,
                  sell_source: str, cost_bps: float, industry_of: dict,
                  index_bars: list | None = None, sell_keys: dict | None = None) -> tuple[dict | None, str | None]:
    """事前登録 §2.2 の 7 構成（`PORTFOLIO_LABELS`）を 1 つの経路で回す（#1223）。戻り値は `(構成ごとの行, 回せない理由)`。

    買いの並びは M-6（M-4 の基底）と M-2*（本番の設定・`production`）の画面の既定（`buy_scores`）。どの構成も同じ test 月
    （両方の並びと売りのキーがそろう月）で回す＝構成どうしを同じ期で比べる。M-2* の予測が無い経路は回さない（理由を返す）。
    """
    if not production:
        return None, "M-2*（本番の設定）の予測が無い"
    preds, y = common_to_dicts(common, bases)
    book = PriceBook(prices_by_co)
    sell = sell_keys if sell_keys is not None else preds[sell_source]
    index_book = PriceBook({INDEX_SERIES: index_bars}) if index_bars is not None else None
    index_ret = ((lambda ym0, ym1: index_book.period_return(INDEX_SERIES, ym0, ym1))
                 if index_book is not None else None)
    buy_a, _ = buy_scores(CANDIDATE, preds[CANDIDATE], y, prices_by_co, db)
    buy_b, _ = buy_scores(PRODUCTION_MODEL, {k: v for k, v in production.items() if k in y}, y, prices_by_co, db)
    yms = (lambda buy: {ym for ym, _ec in buy.keys() & sell.keys()})
    periods = sorted(yms(buy_a) & yms(buy_b))
    if len(periods) < 2:
        return None, f"M-6 と M-2* の test 月が 2 つ以上そろわない（{len(periods)} 期）"
    ret = book.period_return

    def single(buy: dict, top_k: int, sell_k: int) -> list[dict]:
        return replay(periods, buy, sell, ret, top_k, sell_k, cost_bps, index_ret=index_ret)

    def combined(cap: int | None) -> list[dict]:
        return replay_combined(periods, {COMBINED_LEGS[0]: buy_a, COMBINED_LEGS[1]: buy_b}, sell, ret,
                               PORTFOLIO_TOP_K, PORTFOLIO_SELL_K, cost_bps, industry_of, cap, index_ret)

    built = {
        "C": (combined(None), 2 * PORTFOLIO_TOP_K),
        **{key: (combined(cap), 2 * PORTFOLIO_TOP_K) for key, cap in INDUSTRY_CAPS.items()},
        "A": (single(buy_a, PORTFOLIO_TOP_K, PORTFOLIO_SELL_K), PORTFOLIO_TOP_K),
        "B": (single(buy_b, PORTFOLIO_TOP_K, PORTFOLIO_SELL_K), PORTFOLIO_TOP_K),
        "A20": (single(buy_a, PORTFOLIO_TOP_K20, PORTFOLIO_SELL_K20), PORTFOLIO_TOP_K20),
        "B20": (single(buy_b, PORTFOLIO_TOP_K20, PORTFOLIO_SELL_K20), PORTFOLIO_TOP_K20),
    }
    return {key: {"label": PORTFOLIO_LABELS[key], "summary": summarize(ivs, k),
                  "construction": construction_stats(ivs, industry_of), "intervals": ivs}
            for key, (ivs, k) in built.items()}, None


def portfolio_decision(paths: dict) -> dict:
    """事前登録 §4 の判定を機械的に当てはめる（#1223）。`paths` = {経路: run_portfolio の行 or None}。

    候補 X を採るのは、(1) 全ての経路で X の最大下落が基準 C より小さく、(2) 経路の平均で X − C の TOPIX に対する年率の
    超過が −`PORTFOLIO_MAX_EXCESS_DROP` 以上のとき。複数が満たせば `PORTFOLIO_CANDIDATES` の順に 1 つ、どれも満たさ
    なければ C（今のまま）。経路が `PORTFOLIO_N_PATHS` 本そろわない・回せない経路がある・TOPIX の値が取れない区間が
    ある構成は判定しない（`judged=False` と理由）。検定はしない（経路は独立な標本ではない）。
    """
    rule = {"baseline": PORTFOLIO_BASELINE, "candidates": list(PORTFOLIO_CANDIDATES),
            "n_paths": PORTFOLIO_N_PATHS, "max_excess_drop": PORTFOLIO_MAX_EXCESS_DROP}
    reasons = []
    if len(paths) != PORTFOLIO_N_PATHS:
        reasons.append(f"経路が {len(paths)} 本（{PORTFOLIO_N_PATHS} 本で判定する）")
    for shift, rows in paths.items():
        if rows is None:
            reasons.append(f"経路 +{shift}: 構成を回せていない")
            continue
        for key in (PORTFOLIO_BASELINE, *PORTFOLIO_CANDIDATES):
            s = rows[key]["summary"]
            if s.get("n_index_missing") or s.get("annual_excess_index") is None or s.get("max_drawdown") is None:
                reasons.append(f"経路 +{shift}・{key}: TOPIX に対する超過か最大下落が無い")
    if reasons:
        return {"judged": False, "decision": None, "reasons": reasons, "candidates": {}, "rule": rule}
    candidates = {}
    for key in PORTFOLIO_CANDIDATES:
        dd = {str(s): (rows[key]["summary"]["max_drawdown"], rows[PORTFOLIO_BASELINE]["summary"]["max_drawdown"])
              for s, rows in paths.items()}
        diffs = {str(s): rows[key]["summary"]["annual_excess_index"]
                 - rows[PORTFOLIO_BASELINE]["summary"]["annual_excess_index"] for s, rows in paths.items()}
        mean_diff = statistics.mean(diffs.values())
        dd_smaller = all(x < c for x, c in dd.values())
        candidates[key] = {"max_drawdown_vs_baseline": dd, "dd_smaller_on_every_path": dd_smaller,
                           "excess_index_diff": diffs, "excess_index_diff_mean": mean_diff,
                           "passes": dd_smaller and mean_diff >= -PORTFOLIO_MAX_EXCESS_DROP - _FLOAT_TOL}
    decision = next((k for k in PORTFOLIO_CANDIDATES if candidates[k]["passes"]), PORTFOLIO_BASELINE)
    return {"judged": True, "decision": decision, "reasons": [], "candidates": candidates, "rule": rule}


def _pct(v) -> str:
    return f"{v * 100:+.2f}%" if isinstance(v, (int, float)) else "n/a"


def _label(name: str) -> str:
    """表示名。本番の設定の行（`<name>:production`）は `M-2*`。"""
    base, _, variant = name.partition(":")
    return SHORT[base] + ("*" if variant else "")


def _print(results: dict, sell_source: str, top_k: int, sell_k: int, cost_bps: float,
           production: dict | None = None) -> None:
    print(f"\n=== OOF 運用再現（#1045・参考）K={top_k} k={sell_k} 片道 {cost_bps:g}bp・"
          f"売りの並び={_sell_label(sell_source)} ===", flush=True)
    print(f"注意: {DISCLAIMER}", flush=True)
    print(f"注意: {M4_BASES_CAVEAT}", flush=True)
    print(f"注意: {DELIST_NOTE}", flush=True)
    if production and production.get("available"):
        print(f"注意: {PRODUCTION_NOTE}", flush=True)
        print(f"  M-2* の設定: 探索 {production.get('tuned_at')}・そろった期 {production.get('n_periods_matched')}"
              f"/{production.get('n_periods_target')}・パネルの変化 {production.get('panel_changed')}・"
              f"射影で既定へ戻した設定 {production.get('stale_params') or 'なし'}", flush=True)
    elif production:
        print(f"注意: M-2*（本番の設定）は出さない: {production.get('reason')}: {production.get('error')}",
              flush=True)
    has_index = any("annual_index" in res["summary"] for res in results.values())
    if has_index:
        print(f"注意: {INDEX_NOTE}", flush=True)
    print(f"  {'買いの並び':<10}{'年率(前)':>10}{'年率(後)':>10}{'全社平均':>10}{'超過':>10}{'TOPIX':>10}{'対TOPIX':>10}"
          f"{'最大下落':>10}{'入替率':>8}{'区間':>6}{'途切れ':>7}", flush=True)
    for name, res in results.items():
        s = res["summary"]
        turn = f"{s['turnover']:>8.3f}" if s.get("turnover") is not None else f"{'n/a':>8}"
        dd = f"{s['max_drawdown'] * 100:.2f}%" if s.get("max_drawdown") is not None else "n/a"
        print(f"  {_label(name):<10}{_pct(s.get('annual_gross')):>10}{_pct(s.get('annual_net')):>10}"
              f"{_pct(s.get('annual_bench')):>10}{_pct(s.get('annual_excess')):>10}"
              f"{_pct(s.get('annual_index')):>10}{_pct(s.get('annual_excess_index')):>10}"
              f"{dd:>10}{turn}{s.get('n_intervals', 0):>6}"
              f"{s.get('n_stopped', 0):>7}", flush=True)
    # 指数の値が取れない区間は 0 として繋がず、年率を出さない（#1224）。出していない理由をここで言う
    lacking = {_label(name): res["summary"]["n_index_missing"] for name, res in results.items()
               if res["summary"].get("n_index_missing")}
    if lacking:
        print(f"注意: TOPIX の値が取れない区間があるので、TOPIX の年率と超過を出していない（区間の数: {lacking}）",
              flush=True)
    # 参考（#1167）: 区間の超過・年率の超過・入替の利得の 95%CI と、帯ごとのコスト後の年率の超過。対TOPIX は #1224
    print(f"  {'買いの並び':<10}{'区間の超過 [95%CI]':>30}{'年率の超過の95%CI':>24}{'対TOPIXの95%CI':>24}"
          f"{'入替の利得 [95%CI]':>30}{'帯コスト後の超過':>12}", flush=True)
    for name, res in results.items():
        s = res["summary"]
        print(f"  {_label(name):<10}{_fmt_ci(s.get('excess_ci')):>30}{_fmt_range(s.get('annual_excess_ci')):>24}"
              f"{_fmt_range(s.get('annual_excess_index_ci')):>24}"
              f"{_fmt_ci(s.get('swap_gain_ci'), s.get('swap_gain_mean')):>30}"
              f"{_pct(s.get('annual_excess_banded')):>12}", flush=True)
    if any("bands" in res for res in results.values()):
        _print_bands(results)


def _fmt_range(ci: dict | None) -> str:
    """`annual_excess_ci` の結果を「[下限,上限]」で。無ければ n/a。"""
    return f"[{_pct(ci['ci_lo'])},{_pct(ci['ci_hi'])}]" if ci else "n/a"


def _print_bands(results: dict) -> None:
    """保有の売買代金の帯の内訳（#1224・参考）。"""
    print(f"\n=== 保有の売買代金の帯の内訳（参考）===\n注意: {BREAKDOWN_NOTE}", flush=True)
    print(f"  {'買いの並び':<10}{'帯':>6}{'延べ数':>8}{'割合':>9}{'平均超過':>11}{'寄与':>11}", flush=True)
    for name, res in results.items():
        for band, b in (res.get("bands") or {}).items():
            share = f"{b['share'] * 100:.1f}%" if b.get("share") is not None else "n/a"
            print(f"  {_label(name):<10}{band:>6}{b['n_slots']:>8}{share:>9}{_pct(b.get('mean_excess')):>11}"
                  f"{_pct(b.get('contribution')):>11}", flush=True)


def _fmt_ci(ci: dict | None, mean: float | None = None) -> str:
    """`bootstrap_mean_ci` の結果を「平均 [下限, 上限]」で。CI が無ければ平均だけ（それも無ければ n/a）。"""
    if not ci:
        return _pct(mean)
    return f"{_pct(ci['mean'])} [{_pct(ci['ci_lo'])},{_pct(ci['ci_hi'])}]"


def _print_paths(paths: dict) -> None:
    """経路の間の年率の超過（#1167・参考）。"""
    summary = path_summary({s: p["results"] for s, p in paths.items()})
    shifts = list(paths)
    print(f"\n=== 開始月をずらした経路の年率の超過（参考）===\n注意: {PATH_NOTE}", flush=True)
    print(f"  {'買いの並び':<10}" + "".join(f"{'+' + str(s) + 'か月':>10}" for s in shifts)
          + f"{'平均':>10}{'最小':>10}{'最大':>10}", flush=True)
    for name, row in summary.items():
        print(f"  {_label(name):<10}" + "".join(f"{_pct(row['annual_excess'][str(s)]):>10}" for s in shifts)
              + f"{_pct(row['mean']):>10}{_pct(row['min']):>10}{_pct(row['max']):>10}", flush=True)


def _print_ties(paths: dict, top_k: int) -> None:
    """買いのスコアの同点（#1259・参考）。全社同じ値の期と、上位 K 社の境目をまたぐ同点がある期だけを並べる。"""
    print(f"\n=== 買いのスコアの同点（参考）===\n注意: {TIE_NOTE}", flush=True)
    for shift, p in paths.items():
        for name, row in p["results"].items():
            ties = row.get("buy_ties") or {}
            flat = [f"{ym}({t['n']}社)" for ym, t in ties.items() if t["flat"]]
            cross = [f"{ym}({t['at_k'][str(top_k)]['tied']}社)" for ym, t in ties.items()
                     if not t["flat"] and t["at_k"][str(top_k)]["crosses"]]
            print(f"  経路 +{shift}か月 {_label(name):<6} 全社同じ値: {' '.join(flat) or 'なし'}"
                  f" ／ 上位{top_k}社の境目をまたぐ同点: {' '.join(cross) or 'なし'}", flush=True)


def _print_portfolio(paths: dict, decision: dict) -> None:
    """買い方の規則の計測（#1223・事前登録 docs/prereg/portfolio-construction.md）の構成ごとの表と判定。"""
    print(f"\n=== 買い方の規則（事前登録・参考）===\n注意: {PORTFOLIO_NOTE}", flush=True)
    for shift, p in paths.items():
        rows = p.get("portfolio")
        if rows is None:
            print(f"  経路 +{shift}か月: 回せない（{p.get('portfolio_reason')}）", flush=True)
            continue
        print(f"  経路 +{shift}か月", flush=True)
        print(f"    {'構成':<16}{'年率(後)':>10}{'対TOPIX':>10}{'最大下落':>10}{'入替率':>8}{'保有数':>8}{'重複':>6}"
              f"{'業種最大':>8}{'上限で飛ばした':>14}", flush=True)
        for key, row in rows.items():
            s, c = row["summary"], row["construction"]
            dd = f"{s['max_drawdown'] * 100:.2f}%" if s.get("max_drawdown") is not None else "n/a"
            turn = f"{s['turnover']:.3f}" if s.get("turnover") is not None else "n/a"
            size = f"{c['mean_holdings']:.1f}" if c.get("mean_holdings") is not None else "n/a"
            overlap = f"{c['mean_overlap']:.1f}" if c.get("mean_overlap") is not None else "n/a"
            print(f"    {key + ' ' + row['label']:<16}{_pct(s.get('annual_net')):>10}{_pct(s.get('annual_excess_index')):>10}"
                  f"{dd:>10}{turn:>8}{size:>8}{overlap:>6}{c.get('max_industry') or 0:>8}"
                  f"{c.get('n_cap_skipped', 0):>14}", flush=True)
    if not decision["judged"]:
        print(f"判定（事前登録 §4）: 判定しない — {' / '.join(decision['reasons'])}", flush=True)
        return
    for key, c in decision["candidates"].items():
        print(f"  候補 {key}: 全経路で最大下落が C より小さい={c['dd_smaller_on_every_path']}・対TOPIX の差の平均"
              f" {_pct(c['excess_index_diff_mean'])}（許容 −{PORTFOLIO_MAX_EXCESS_DROP * 100:.0f} ポイント）"
              f" → {'満たす' if c['passes'] else '満たさない'}", flush=True)
    picked = decision["decision"]
    print(f"判定（事前登録 §4）: {picked}（{PORTFOLIO_LABELS[picked]}）"
          + ("＝今のまま" if picked == PORTFOLIO_BASELINE else "を採る候補"), flush=True)


def _start_shifts(text: str) -> tuple[int, ...]:
    """`--start-shifts` の値（例 "0,1,2"）。0〜MAX_START_SHIFT の相異なる整数。"""
    try:
        vals = tuple(int(v) for v in text.split(",") if v.strip() != "")
    except ValueError:
        raise argparse.ArgumentTypeError(f"整数のカンマ区切りにする: {text!r}") from None
    if not vals or len(set(vals)) != len(vals) or not all(0 <= v <= MAX_START_SHIFT for v in vals):
        raise argparse.ArgumentTypeError(
            f"0〜{MAX_START_SHIFT} の相異なる整数にする（3 か月おきの格子なので 3 以上は同じ格子に戻る）: {text!r}")
    return vals


def _band_bps(text: str) -> tuple[float, ...]:
    """`--cost-bands-bps` の値（売買代金の小さい帯から順に N_COST_BANDS 個の片道 bp）。"""
    try:
        vals = tuple(float(v) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"数のカンマ区切りにする: {text!r}") from None
    if len(vals) != N_COST_BANDS or any(v < 0 for v in vals):
        raise argparse.ArgumentTypeError(f"0 以上の数を {N_COST_BANDS} 個（小さい帯から）: {text!r}")
    return vals


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.oof_portfolio_replay",
                                 description="OOF 運用再現（#1045・参考で判定には使わない）")
    ap.add_argument("--models", nargs="+", choices=BASES, default=list(BASES),
                    help="買いの並びに使うモデル（各モデルの画面の既定の並び）")
    ap.add_argument("--sell-source", choices=SELL_SOURCES, default=CANDIDATE,
                    help=f"売りの並びに使う予測（既定は M-6 の μ̂。{SELL_S2_SOURCE}＝本番の売りの並びのキー S2・#1238）")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="保有する社数 K")
    ap.add_argument("--sell-k", type=int, default=DEFAULT_SELL_K, help="毎期売る保有の下位の社数 k")
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS, help="片道の売買コスト（bp）")
    ap.add_argument("--start-shifts", type=_start_shifts, default=DEFAULT_START_SHIFTS,
                    help="開始月のずらし（か月・カンマ区切り。例 0,1,2。既定 0＝1 本。#1167）")
    ap.add_argument("--cost-bands-bps", type=_band_bps, default=None,
                    help=f"売買代金の三分位ごとの片道コスト（bp・小さい帯から {N_COST_BANDS} 個。例 30,15,8。"
                         "既定は無効。参考・#1167）")
    ap.add_argument("--portfolio", action="store_true",
                    help=f"買い方の規則の 7 構成（合算・業種の上限・単独）を回し、事前登録の判定を出す（#1223・"
                         f"{PORTFOLIO_PREREG}。--prereg と --prereg-sha が要る）")
    ap.add_argument("--prereg", default=None, help=f"--portfolio の事前登録文書（{PORTFOLIO_PREREG}）")
    ap.add_argument("--prereg-sha", default=None, help="その凍結値（本文の sha256・追記節に記録したもの）")
    ap.add_argument("--tie-report", action="store_true",
                    help="各行に期ごとの買いのスコアの同点（全社同じ値・上位 K 社の境目）を足して表示する（#1259・参考）")
    ap.add_argument("--smoke", action="store_true", help="企業を絞った軽量パネルで動作確認")
    ap.add_argument("--smoke-companies", type=int, default=120)
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--out", default=str(_OUT), help="結果の JSON")
    return ap


def _portfolio_stamp(args) -> dict | int | None:
    """`--portfolio` の前提を確かめ、事前登録の印を返す（#1223）。前提を外れたら終了コード（2）を返す。

    `--portfolio` を付けない呼び出しは None（既定の出力は変えない）。`--portfolio` は事前登録の文書と凍結値を必須にし、
    K・k・コストは事前登録の値のまま（変えると別の計測になる）。`--prereg` だけを付けた呼び出しも止める。
    """
    if not (args.portfolio or args.prereg or args.prereg_sha):
        return None
    problems = []
    if not args.portfolio:
        problems.append("--prereg・--prereg-sha は --portfolio と一緒に使う")
    if not args.prereg or not args.prereg_sha or Path(args.prereg).as_posix() != PORTFOLIO_PREREG:
        problems.append(f"--portfolio には --prereg {PORTFOLIO_PREREG} と --prereg-sha が要る")
    if ((args.top_k, args.sell_k, args.cost_bps) != (PORTFOLIO_TOP_K, PORTFOLIO_SELL_K, DEFAULT_COST_BPS)
            or args.cost_bands_bps is not None):
        problems.append("--portfolio は事前登録の K・k・コストで回す（--top-k・--sell-k・--cost-bps・"
                        "--cost-bands-bps を変えない）")
    if args.portfolio and args.sell_source != SELL_S2_SOURCE:
        problems.append(f"--portfolio の売りの並びは本番と同じ S2（--sell-source {SELL_S2_SOURCE}・事前登録 §2.1）")
    if problems:
        print(f"[oof_replay] NG: {' / '.join(problems)}", file=sys.stderr)
        return prereg.EXIT_MISMATCH
    try:
        return prereg.verify(args.prereg, args.prereg_sha)
    except prereg.PreregError as e:
        print(f"[oof_replay] NG: {e}", file=sys.stderr)
        return prereg.EXIT_MISMATCH


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows の既定 cp932 では μ̂ 等が出力できずクラッシュするため UTF-8 に固定する（他の bakeoff と同流儀）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    if not 0 <= args.sell_k <= args.top_k or args.top_k < 1:
        print(f"[oof_replay] NG: 0 <= --sell-k <= --top-k かつ --top-k >= 1 にする"
              f"（top_k={args.top_k} sell_k={args.sell_k}）", file=sys.stderr)
        return 2
    stamp = _portfolio_stamp(args)            # 不一致は重い計算の前に止める（#1223）
    if isinstance(stamp, int):
        return stamp
    set_refresh(args.refresh_cache)

    panel = _load_panel(args)
    # 業種は今の値（companies.industry・事前登録 §2.2）。空は None＝上限の対象にしない
    industry_of = {ec: (getattr(c, "industry", None) or None) for ec, c in panel[2].items()}
    db = SessionLocal()
    paths: dict[int, dict] = {}
    try:
        runs = {}
        use_s2 = args.sell_source == SELL_S2_SOURCE
        s2_by_shift: dict[int, tuple[dict, dict]] = {}
        for shift in args.start_shifts:
            # 開始月のずらし（#1167）。ずらし 0 は今までと同じ 1 回の M-4
            label = "M-4(3基底・rank_ic_grid)" + (f"・開始 +{shift}か月" if shift else "")
            with shifted_min_train(shift):
                captured: dict = {}
                with capture_m6_args(captured) if use_s2 else nullcontext():
                    runs[shift] = _run_config(label, BASES, panel, db, weight_method="rank_ic_grid", keep_common=True)
                if use_s2:
                    # 売りの並びの S2（#1238）。ずらしの内側で回す＝M-6 のレグと同じ区切り
                    s2_by_shift[shift] = s2_predictions(captured, runs[shift]["common"], runs[shift]["bases"])
        check_paths_disjoint({s: set(r["common"]) for s, r in runs.items()})
        all_yms = sorted({ym for r in runs.values() for ym in r["common"]})
        # 比べる相手の指数（#1224・参考）。全ての経路に同じ週次足を渡す。test 月が 1 つも無ければ読まない
        index_bars = index_weekly_bars(load_index_daily(db, index_since(all_yms[0]))) if all_yms else []
        bands = None
        if args.cost_bands_bps is not None:
            # 売買代金の帯（#1167・参考）。週次ローダへ列を足さず、対象の社と期間に絞って読む
            from scripts.measure_index_event import load_turnover
            codes = {row[0] for r in runs.values() for rows in r["common"].values() for row in rows}
            turnover = load_turnover(db, codes, _month_end_date(all_yms[0]) - timedelta(weeks=TURNOVER_WEEKS),
                                     _month_end_date(all_yms[-1]))
            bands = turnover_bands(turnover, all_yms)
        band_cost = make_band_cost(bands, args.cost_bands_bps, args.cost_bps) if bands is not None else None
        for shift, run in runs.items():
            sell_keys, fallback = None, []
            if use_s2:
                m6_preds = common_to_dicts(run["common"], run["bases"])[0][CANDIDATE]
                sell_keys, fallback = s2_sell_keys(s2_by_shift[shift][0], m6_preds)
            prod_preds, prod_info = None, None
            if PRODUCTION_MODEL in args.models or args.portfolio:
                # 本番の設定の M-2（#1123）。test 月は（その経路の）M-4 の共通域にそろえる
                prod_preds, prod_info = production_predictions(panel, db, sorted(run["common"]))
            results = run_replays(run["common"], run["bases"], panel[0], db, args.models,
                                  args.sell_source, args.top_k, args.sell_k, args.cost_bps,
                                  production=prod_preds, band_cost=band_cost,
                                  index_bars=index_bars, bands=bands, sell_keys=sell_keys,
                                  tie_report=args.tie_report)
            paths[shift] = {"results": results, "production": prod_info, "has_production": prod_preds is not None,
                            "test_months": sorted(run["common"])}
            if use_s2:
                paths[shift]["sell"] = {"fallback_periods": fallback, "s2_fit": s2_by_shift[shift][1]}
            if args.portfolio:
                rows, why = run_portfolio(run["common"], run["bases"], panel[0], db, prod_preds,
                                          args.sell_source, args.cost_bps, industry_of, index_bars,
                                          sell_keys=sell_keys)
                paths[shift].update(portfolio=rows, portfolio_reason=why)
    finally:
        db.close()
    for shift, p in paths.items():
        if len(paths) > 1:
            months = p["test_months"]
            print(f"\n--- 経路: 開始 +{shift}か月（test 月 {months[0] if months else '-'}〜"
                  f"{months[-1] if months else '-'}・{len(months)} 期）---", flush=True)
        _print(p["results"], args.sell_source, args.top_k, args.sell_k, args.cost_bps, p["production"])
    if len(paths) > 1:
        _print_paths(paths)
    if bands is not None:
        print(f"注意: {BAND_NOTE}", flush=True)
    if args.tie_report:
        _print_ties(paths, args.top_k)
    decision = None
    if args.portfolio:
        decision = portfolio_decision({s: p["portfolio"] for s, p in paths.items()})
        _print_portfolio(paths, decision)

    head = paths[args.start_shifts[0]]
    payload = {
        "disclaimer": DISCLAIMER,
        "caveats":    ([M4_BASES_CAVEAT, DELIST_NOTE, INDEX_NOTE]
                       + ([PRODUCTION_NOTE] if any(p["has_production"] for p in paths.values()) else [])
                       + ([PATH_NOTE] if len(paths) > 1 else [])
                       + ([BAND_NOTE, BREAKDOWN_NOTE] if bands is not None else [])
                       + ([TIE_NOTE] if args.tie_report else [])),
        "params":     {"top_k": args.top_k, "sell_k": args.sell_k, "cost_bps": args.cost_bps,
                       "sell_source": args.sell_source, "models": args.models, "smoke": args.smoke,
                       "start_shifts": list(args.start_shifts),
                       "cost_bands_bps": list(args.cost_bands_bps) if args.cost_bands_bps is not None else None},
        # 比べる相手の指数（#1224）。使った週次足の範囲を残す＝公表されている値と突き合わせられる
        "index":      {"series": INDEX_SERIES, "n_weeks": len(index_bars),
                       "first": index_bars[0].trade_date if index_bars else None,
                       "last": index_bars[-1].trade_date if index_bars else None},
        # 本番の設定の M-2 の行の付記（設定・探索日・そろった期・回せなかった理由）。M-2 を選ばない回は None
        "production": head["production"],
        # 先頭の経路（既定はずらし 0＝今までの 1 本）
        "results":    head["results"],
    }
    if len(paths) > 1:
        payload["paths"] = {
            str(shift): {"test_months": {"first": p["test_months"][0] if p["test_months"] else None,
                                         "last": p["test_months"][-1] if p["test_months"] else None,
                                         "n": len(p["test_months"])},
                         "production": p["production"], "results": p["results"]}
            for shift, p in paths.items()}
        payload["path_summary"] = path_summary({s: p["results"] for s, p in paths.items()})
    if use_s2:
        # 売りの並びの S2（#1238）。経路ごとに、M-6 の μ̂ へ倒した期と S2 の学習の要約を残す（事前登録 §3 の参考）
        payload["caveats"].append(SELL_NOTE)
        payload["sell"] = {"source": SELL_S2_SOURCE,
                           "paths": {str(s): p["sell"] for s, p in paths.items()}}
        for s, p in paths.items():
            print(f"売りの並び S2（経路 +{s}か月）: M-6 の μ̂ へ倒した期 {p['sell']['fallback_periods'] or 'なし'}",
                  flush=True)
    if args.portfolio:
        # 買い方の規則の計測（#1223）。事前登録の印（本文の sha256・Issue・HEAD）と判定を残す
        payload["caveats"].append(PORTFOLIO_NOTE)
        payload["portfolio"] = {
            **stamp, "sell_source": args.sell_source, "decision": decision,
            "paths": {str(s): {"reason": p.get("portfolio_reason"), "rows": p.get("portfolio"),
                               "sell_fallback_periods": (p.get("sell") or {}).get("fallback_periods")}
                      for s, p in paths.items()},
        }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
