"""企業イベント台帳 — 株数や株価の基準を変える企業イベントの知識を1か所に置く（#746・ADR-0062）。

「どの社のどの日に、1株あたりの基準が変わるイベント（分割・併合・スピンオフ・新株予約権の
無償割当など）があったか」は、次の読み手が共有する1つの知識である。

- 分割補正係数 F の表（`split_adjustment_factors`・`financial_metrics` VIEW が当てる・ADR-0055）
- TTM 行の合成（材料の間の分割を弾く・TTM 行自身の F・ADR-0051）
- J-Quants catchup の価格行の選別（スピンオフの権利落ち前を書かない・#651）
- 株価修復（`scripts/repair_splits_from_jquants.py`・スピンオフ換算と保留の窓・#568／#652）
- 測定（`scripts/measure_split_valuation_bias.py`・`scripts/measure_split_leak.py`）

以前はこの知識が約8つの module に散っていて、本番（`collector_prices`・`ttm_composite`）が
測定用スクリプトを import していた。読み手ごとに入力を組み立てるので、登録表を読む読み手と
読まない読み手が生まれていた（#739 保留の窓・#740 スピンオフ）。

## 使い方

F や分割窓を得る道は2つだけにする。

- 本番: `build_ledger(db)` ——入力（通期行・公式 `AdjFactor`・その受信区間・週次株価の系列）を
  **台帳が自分で読む**。パイプラインは一晩に1回だけ作り、F の表の書き込み
  （`rebuild_split_adjustment_factors(db, ledger=...)`）と TTM 合成
  （`ttm_composite.rebuild_ttm_financial_records(db, ledger=...)`）へ同じものを渡す。
- テスト: `compute_ledger(rows, official=..., coverage=..., series=...)` ——DB に触らない。
  入力は**すべてキーワードで必須**にしてある（1つ渡し忘れても検出はもっともらしい結果を返す）。

**公式イベントは台帳が1回だけ読み、F の検出と TTM の窓の両方に同じ値を使う。** 公式イベントの
見え方を変えるときは `compute_ledger` の1か所を変えれば両方に効く——保留の窓（#652）に入る
公式イベントは、そこで外してから両方へ渡す（#739）。

## 歪みの向きは列によって逆になる

分割 1:F が年 Y に起きたとき、年 y < Y の行は:

    per        = (生株価 / F) / eps          -> 真値へは × F   （過小＝割安に見える）
    pbr        = (生株価 / F) / bps          -> 真値へは × F   （過小＝割安に見える）
    market_cap = (生株価 / F) * 旧株数       -> 真値へは × F   （過小）
    div_yield  = dps / (生株価 / F)          -> 真値へは / F   （過大＝高利回りに見える）
    nc_ratio   = net_cash / market_cap       -> 真値へは / F   （過大）

累積倍率は「その行**より後**に起きた全イベントの積」。`e.year > y` であって `>=` ではない
（分割当年の行は既に新基準なので歪まない）。逆分割は F < 1 となり向きが反転するが同じ式で扱う。
向きの唯一の源は `COLUMN_DIRECTION`。

## 分割比をどこから取るか

**全件は DB 内在の2列だけで復元する**: `issued_shares`（期末発行済株式総数）の年次比と、
`bs_bps` の逆比が同じ倍率で一致すること。両者は XBRL の別タグ＝独立した書き手なので、
両方が同じ比で逆向きに動くことが交差検証になる。`period_type='annual'` の全行で
`issued_shares` は非 NULL なので、J-Quants 契約窓（2年）の外も遡れる。

ただしこれは「株数と1株純資産が両方動いた」という**必要条件**しか見ていないので、
`scripts/measure_split_valuation_bias.py verify-sample` で公式 `AdjFactor` とサンプル突合して
一致率を出す（陰性対照つき）。

**株数が分割に追随しない社のために第2経路がある**（#656）。`bs_bps` の年次比を候補ゲートに、
`pl_eps` の比を交差検証にする。株数と1株指標が同じ年に動く前提を外した経路で、
既定は `DEFAULT_BPS_PATH`。

**第2経路の倍率は候補ゲートから取らず、翌年の `issued_shares` 比から取る**（#659）。
`bs_bps` は分割以外（内部留保・評価差額）でも増えるので年次比は `F / (1 + g)` になり、
「分割があった」は言えても「何倍か」を決められない（実測の一致率 0.367）。倍率だけを
独立な第3の信号＝1年遅れの株数比へ移し、その信号が無い年は**採らない**。

**第1経路には純資産総額のチェックがある**（#657・既定 `DEFAULT_EQUITY_TOL`）。株数と同じ向きに
`bs_total_equity` が許容を超えて動いたら増資と読んで採らない。ただし `bs_bps ≈ 純資産 / 株数`
が成り立つ社では bps の交差検証と同じものを見ているので、**落とせるのは両者が食い違う社だけ**
で、深い割引の増資（純資産がほとんど増えない）は分離できない。

**上場廃止をまたいで別の実体の行が隣り合うペアは、どちらの経路でも比べない**（#672）。
同じ EDINET コードのまま上場廃止→再上場した社（実測 E05714）は、欠損年をまたいで旧社と新社の行が
ペアになり、株数と `bs_bps` がたまたま逆向きに動くと交差検証を通る。判定は週次株価に1年以上の
空白（系列の開始が遅い、または途中で途切れる）がペアの期間の中にあるかで行う（`listing_gap_in_pair`）。

## 構成

登録表 → 検出器（純関数）→ 台帳（純関数）→ I/O の順に並べる。**I/O より上は database /
collector_prices / httpx / scripts をトップレベルで import しない**（テストで固定）——DB 無しで
回せることと、本番が測定用スクリプトへ依存しないことの両方を守るため。
"""
from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, NamedTuple, Optional, Sequence

log = logging.getLogger("collector")


# ── 登録表（公式 AdjFactor が持たない／当ててはいけない企業イベント）────────────

# --- 公式 AdjFactor が持たない企業イベント（#568）--------------------------------
# **J-Quants の `AdjC` は株式分配型スピンオフ（子会社株の現物配当）を遡及調整しない。**
# Yahoo（= DB）は権利落ち日より前の全期間へ
#     (権利付最終日の終値 − 分配株式の初値) / 権利付最終日の終値
# を掛ける。両者はどちらも「調整済み」だが中身が違い、権利落ち日を境に
# 公式 / DB が 1.0 から離れる（**公式 > DB の向き**＝分割の取り残しとは逆）。
#
# 週次リターンを入力に持つ M-1 / M-2 / M-6 には DB 側が正しい——受け取った子会社株の価値が
# 連続性として残る。**DB を公式へ寄せてはいけない**（権利落ち日に偽の暴落を作る）。
# 突合では逆に、公式値へこの係数を掛けて DB のスケールへ換算してから比べる。
#
# 登録は「社を除外する」ではなく「換算して比べる」。DB の値が変われば（Yahoo が調整を
# やめる・未調整値で取り直す等）再び段差として検出される（ADR-0053 の「名簿」にしない）。
# 係数は丸めた小数を書き写さず、根拠の2つの値から式で持つ。
#
# 収集経路（J-Quants）はこの表を**書き込みを止める向きにだけ**使う（#651・`before_spinoff_ex_date`）。
# 係数を掛けて書かないのは、登録の誤りを本番の株価へ入れないため——止める向きなら、誤登録で
# 起きるのは「公式値で上書きされない行が残る」ことだけで、値そのものは壊れない。
#
# {edinet_code: ((権利落ち日, 係数, 根拠), ...)}
SPINOFF_ADJUSTMENTS: dict = {
    # メルコHD（現バッファロー・6676）→ シマダヤ（250A）を 1:1 で分配。権利付最終日 2024-09-26
    # 終値 3820円・権利落ち 2024-09-27・効力／上場 2024-10-01・シマダヤ初値 1760円。
    # 係数の逆数 1.854369 は #466 で実測した公式 / DB 比と一致する。
    "E02086": (("2024-09-27", (3820.0 - 1760.0) / 3820.0,
                "シマダヤ(250A) 株式分配型スピンオフ・効力 2024-10-01・#568"),),
}


def spinoff_factor(edinet_code: str, trade_date: str) -> float:
    """公式 `AdjC` に掛けると DB（Yahoo）のスケールになる係数。登録が無ければ 1.0。

    `trade_date` は "YYYY-MM-DD"。権利落ち日**より前**の日にだけ掛かる（当日以降は 1.0）。
    同じ社に複数あれば積をとる。
    """
    f = 1.0
    for ex_date, factor, _ in SPINOFF_ADJUSTMENTS.get(edinet_code, ()):
        if str(trade_date)[:10] < ex_date:
            f *= factor
    return f


def before_spinoff_ex_date(edinet_code: str, trade_date: str) -> bool:
    """登録済みスピンオフの権利落ち日**より前**の日付か（#651）。登録が無ければ False。

    この日付の公式 `AdjC` はスピンオフを調整しておらず `AdjC == C` のままなので、#620 の
    「`AdjC != C` を書かない」選別を素通りする。一方 DB（Yahoo）は係数を掛けたスケールで
    持つ＝書けば同じ列に2つのスケールが入る。J-Quants の取得経路はこの行を書かない。
    """
    d = str(trade_date)[:10]
    return any(d < ex_date for ex_date, _, _ in SPINOFF_ADJUSTMENTS.get(edinet_code, ()))


# --- 公式 AdjFactor だけが当て、DB へ当ててはいけない調整（#652）-------------------
# スピンオフ（上の表）の**逆向き**: 公式は調整するが、DB（= Yahoo = 実際の約定値）は調整しない
# のが正しい企業イベント。実例は新株予約権の無償割当（買収防衛策）で、公式 `AdjC` は権利落ち日
# より前の全期間へ「全員が行使した場合」の理論係数を機械的に掛けるが、差別的行使条件と取得条項が
# 付いた予約権は市場がほとんど織り込まない（#652 の観測: E34165 は理論上の権利落ちで約 −33% の
# はずが、公式の段差が現れた前後の実際の終値は −3.5%。Yahoo も split として持たず DB と全期間一致）。
# 公式へ寄せると、実際の取引に無い跳ねが週次リターン（M-1 / M-2 / M-6 の入力）に入る。
#
# **登録は換算ではなく書き込みの停止**（`repair_splits_from_jquants.judge_company`）。スピンオフの
# ように公式値を DB のスケールへ換算するには正確な権利落ち日が要るが、この種のイベントは日程が
# 変わりやすく（延期・差止め）、日付を誤って換算すると残った段差を公式イベントが「説明」して
# **誤った係数が書き込まれる**。止める向きなら、誤登録で起きるのは「書かない」ことだけ。
#
# **台帳も同じ登録を読む**（#739）。`compute_ledger` は窓に入る公式イベントを F の検出と TTM の
# 分割窓の両方から外す——DB の株価が遡及調整されていなければ、1株指標との基準の不一致も起きて
# おらず、F を掛ける理由が無い。以前は修復スクリプトだけがこの表を読み、TTM は公式イベントを素通しで
# 窓にしていたので、イベントが catchup の取得範囲に届いた晩から同社の過去の TTM 行に F が付くはずだった。
#
# **鍵は社ではなく「社＋日付の窓」**（ADR-0053 の採らなかった案C「社の名簿」にしない）。窓の外で
# 起きた公式イベントは今までどおり判定される。窓は日程の不確かさを吸収するために広く取る——
# 広いほど書かない側に倒れ、窓の中の本物の分割も止まるが、レポートに理由付きで出るので黙らない。
# 理論係数は照合に使わない（窓の中の公式イベントはすべて止める）。人が見比べるための根拠。
#
# {edinet_code: ((窓の始まり, 窓の終わり, 理論係数, 根拠), ...)}。窓は両端を含む "YYYY-MM-DD"。
WITHHELD_OFFICIAL_ADJUSTMENTS: dict = {
    # SAAFHD（1447）第1回A新株予約権: 1株につき1個を無償割当・1個あたり目的株式 0.5株・
    # 行使価額 1円・行使期間 2026-11-01〜2027-01-31・対抗措置の行使条件と取得条項付き
    # （適時開示 2026-08-03）。基準日は 9/14 説と 9/24 説があり一次資料で未確認、株主による
    # 差止めの仮処分申立てもある（2026-09-14 に結果の開示）。理論係数は全員行使時の 1 / (1 + 0.5)。
    # 見直すのは #652 の (a) 防衛策の撤回 か (b) 行使による株式交付 のどちらかが来たとき。
    "E34165": (("2026-09-01", "2026-12-31", 1.0 / (1.0 + 0.5),
                "SAAFHD(1447) 新株予約権無償割当（買収防衛策）・DB（実約定値）が正・日程未確認・#652"),),
}


def withheld_official_events(edinet_code: str, events: list) -> list:
    """公式イベント `[(日付, AdjFactor)]` のうち、登録済みの窓に入るものを返す（#652）。

    戻り値は `[(日付, AdjFactor, 理論係数, 根拠)]`（日付順）。登録が無い社・窓の外なら `[]`。
    読み手は2つある。修復スクリプトは1件でも返ればその社の補正を**書かない**
    （`repair_splits_from_jquants.judge_company`）。台帳は返ったイベントを F の検出と TTM の窓から
    外す（`compute_ledger`・#739）。
    """
    out = []
    for d, factor in events:
        day = str(d)[:10]
        for start, end, expected, reason in WITHHELD_OFFICIAL_ADJUSTMENTS.get(edinet_code, ()):
            if start <= day <= end:
                out.append((day, factor, expected, reason))
                break
    return sorted(out)


#: `AdjFactor` をイベントとみなす下限（浮動小数の 1.0 ゆらぎを拾わない）。
#: 収集（#661・catchup が残す）と修復（`repair_splits_from_jquants.extract_events`）が共有する。
ADJ_FACTOR_EVENT_EPS = 1.0e-6


def adj_factor_event(q: dict) -> Optional[float]:
    """J-Quants の日次バー1行が公式の企業イベントなら、その `AdjFactor` を返す。無ければ None。

    `AdjFactor` は**過去の株価に掛ける係数**（1:2 分割なら 0.5）で、分割補正係数 F とは向きが
    逆（CONTEXT.md）。スピンオフは載らない（#568）。数値に読めない値はイベントとみなさない。
    """
    f = q.get("AdjFactor")
    if f is None:
        return None
    try:
        f = float(f)
    except (TypeError, ValueError):
        return None
    return f if abs(f - 1.0) > ADJ_FACTOR_EVENT_EPS else None


# ── 検出器（純関数・ネットワークにも DB にも触らない）──────────────────────────

# 候補ゲート。対数対称に使う（逆側は 1/1.4 = 0.7143）。分割と併合で検出力を変えない。
DEFAULT_MIN_RATIO = 1.4
# bps 逆比との一致許容。偽陽性（増資・自社株買い）を止めているのは閾値ではなくこちら。
DEFAULT_BPS_TOL = 0.15
# 定番比へのスナップ許容（対数距離）。
DEFAULT_SNAP_TOL = 0.02
# 第2経路（`bs_bps` を候補ゲート・`pl_eps` を交差検証にする経路・#656）を既定で使うか。
# **`rebuild_split_adjustment_factors` が既定のまま呼ぶ＝ここが毎晩の係数表の中身を決める。**
#
# **True にしたのは実測で倒したからである**（2026-09-12・`verify-sample --bps-path
# --source bps --coverage partial --n 30 --controls 10`）。公式 `AdjFactor` との一致率は
# **0.962（25/26）**・陰性対照の見逃し **0 社**で、#654 の第1経路の 0.967 とほぼ同水準。
#
# **この経路が使えるようになったのは倍率の出どころを変えたからである**（#659）。
# `bs_bps` の年次比を倍率に使っていた頃の一致率は **0.367（11/30）**で、外れた 16 件
# すべてで検出 < 公式だった。`bs_bps` は分割以外（内部留保・有価証券の評価差額）でも
# 増えるので、年次比は真の分割比 F に対し `F / (1 + g)` になる。実測の g は **18%〜67%**
# と幅が広く、隣り合う定番比の間隔（例 2.0 と 2.5）を超える。上向きスナップでも
# **0.778（21/27）**で止まった。**bps 比は「分割があった」は言えるが「何倍か」を
# 決められない**——だから倍率だけを翌年の `issued_shares` 比へ移した（`detect_events`）。
#
# 第3信号を要求したぶん件数は減る（257 イベント/245社 -> 114/113）。**減ったのは
# 倍率の根拠が無い分**で、最新年のイベントは翌年の決算が入れば自動的に係数表へ入る。
DEFAULT_BPS_PATH = True
# 第1経路の純資産総額チェック（#657）の許容。`None` は無効。
# **`rebuild_split_adjustment_factors` が既定のまま呼ぶ＝ここが毎晩の係数表の中身を決める。**
#
# 株数と同じ向きに `bs_total_equity` が `1 + tol` 倍を超えて動いたら、増資（併合側なら減資）と
# 読んで採らない。**この信号で分離できるのは `bs_bps` と純資産総額が食い違う社だけ**である
# ——`bps逆比 / 株数比 = 1 / 純資産比` なので、`bs_bps ≈ 純資産 / 株数` が成り立つ社では
# bps の交差検証（`DEFAULT_BPS_TOL`）が既に純資産の伸びを見ている（2026-09-13 実測: 第1経路
# 492 件の整合度の中央値 1.000・5〜95% 0.951〜1.077）。深い割引の増資は純資産をほとんど
# 増やさないので、どちらの信号でも分離できない。
#
# **1.0（純資産が株数と同じ向きに2倍を超えて動いたら採らない）にしたのは、実測の前に宣言した
# 規則を当てはめた結果である**（2026-09-13・ADR-0055 決定4-4）。格子のうち (a) 契約窓内の全数
# 突合で公式と一致する本物を1件も落とさない (b) 公式に無いイベントを1件以上落とす (c) 窓外で
# 落とす第1経路のイベントに Yahoo の一致する split が無い、を全部満たす最小値を採った。
# 0.25〜0.60 は (c) で落ちた——E36173 は純資産 x1.9848 だが Yahoo に 1:2 分割があり本物。
# 全数突合の偽陽性 6/94 のうち、この値で落とせるのは E05716（純資産 x2.1611）の1件だけで、
# **E01121 は偽陽性と確定したが落とせない**（x1.3027 で落とすと E36173 も落ちる）。
# 余裕は両側とも薄い（E36173 まで 0.015・E05716 まで 0.16）ので、動かすなら測り直すこと。
DEFAULT_EQUITY_TOL: Optional[float] = 1.0
# 合成（分割＋増資）とみなす残差の範囲。これを外れたら丸めずに unsnapped で別枠へ出す。
COMPOSITE_LO, COMPOSITE_HI = 0.8, 1.25

# 上場廃止をまたぐペアの判定（#672・ADR-0055 決定4-7・`listing_gap_in_pair`）。
# **欠損年の長さだけでは切らない**——本物の分割にも欠損年はある（XBRL の取りこぼし・決算期変更。
# 実測 E03078 は 2018→2020 で gap_years 2）。長さは「少なくとも1期ぶんの通期行が無い」という
# 前提条件にだけ使い、別実体と読む根拠は**ペアの期間の中にこの社の市場価格が無い区間がある**
# ことに置く。上場廃止→再上場は週次株価に2つの形で現れる: 系列の開始が遅い（旧社の価格が
# 表に無い・E05714）と、系列の途中で途切れる（旧社の価格が残る・E03530 SBI新生銀行）。
# 365 日は「少なくとも1年、この社に市場価格が無かった」＝欠損した1期と釣り合う長さ。
# 実測（2026-09-14）で annual 全 30,379 行の隣接ペアのうち該当は E05714 2020→2026 と
# E03530 2023→2026 の2件（後者は交差検証で落ちるので今はイベントにならない）。
LISTING_GAP_MIN_YEARS = 2
LISTING_GAP_MIN_DAYS = 365

# 公式の日次バーの「連続して受け取った区間」を切る空白（暦日・#668・ADR-0055 決定4-8・`bars_spans`）。
# 東証の休場は年末年始・GW でも暦日 6〜7 日なので、これを超える空白は取得漏れか売買停止と読み、
# **その空白をまたぐイベント窓は「公式で確かめた」に数えない**（外す側へ倒さない＝補正を残す）。
MAX_BAR_GAP_DAYS = 10

# 実在する分割・併合比。0.05 は 1:20 併合。
#
# **比を足すのは「検出したイベントの本物の比が公式 `AdjFactor` か Yahoo で確かめられ、かつ
# 足した前後で F が変わる行が全部確かめられたとき」だけ**（#669・ADR-0055 決定4-6／4-7）。
# 表の比は既存イベントの丸め先も変える（間に比が入ると composite の寄り先が動く）ので、
# 1社の裏付けだけで足すと、分割でない株数の動きまで補正へ入ることがある。足したら
# `verify-sample --census` の一致率・偽陽性をやり直すこと。
#   1/3  : E03717 unbanked の 3:1 併合（2024-09-27・公式 1/3・Yahoo 1:3）
#   1/15 : E37831 INEST の 15:1 併合（2025-09-29・公式 1/15・Yahoo 1:15）
#   15   : E05698 UT グループの 1:15（2025-12-29・公式/Yahoo とも 15）。#669 では E05714
#          （2020年に上場廃止し 2026年に別の株数で再上場・分割なし）が composite で巻き込まれる
#          ので保留し、#672 で上場廃止をまたぐペアを比べないようにしてから足した。第2経路で
#          E23634 アミタ HD も 15 で入るが、Yahoo の 5:1（2021-12-29）と 3:1（2022-09-29）は
#          どちらも F が変わる 2018〜2020 年の行より後にあるので、その3行の F=15 は正しい。
# **6 は足していない**——E05426 の 5.768 は 1:5 分割（Yahoo 2024-03-28）＋増資で、
# 今の composite 5.0 が正しい。
CANONICAL_RATIOS: tuple[float, ...] = (
    1.1, 1.2, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0, 15.0,
    1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10, 1 / 15, 1 / 20,
)

# 歪みの向き。+1 は「真値へ × F」、-1 は「真値へ / F」。取り違えると全部逆になる。
COLUMN_DIRECTION: dict[str, int] = {
    "per": +1, "pbr": +1, "market_cap": +1, "div_yield": -1, "nc_ratio": -1,
}


class AnnualRow(NamedTuple):
    edinet_code: str
    year: int
    period_end: Optional[str]
    issued_shares: Optional[float]
    bs_bps: Optional[float]
    pl_eps: Optional[float]
    dps: Optional[float]
    stock_price: Optional[float]
    per: Optional[float]
    pbr: Optional[float]
    div_yield: Optional[float]
    market_cap: Optional[float]
    # 純資産総額（#657）。末尾に既定値付きで置くのは、12 列の位置指定で作る既存の呼び出しを
    # 壊さないため。
    bs_total_equity: Optional[float] = None


class ShareEvent(NamedTuple):
    edinet_code: str
    year: int
    prev_year: int
    gap_years: int
    period_end: Optional[str]
    prev_period_end: Optional[str]
    sh_ratio: float
    bps_ratio: float
    canonical: Optional[float]
    residual: float
    kind: str            # split | reverse | composite | unsnapped
    # どちらの経路が拾ったか（#656）。"shares" は株数比を候補ゲートにした第1経路、
    # "bps" は `bs_bps` の年次比を候補ゲートにし `pl_eps` の比で交差検証した第2経路。
    # 既定値を持つのは、既存の呼び出し（テストの `ev()` ヘルパー含む）を壊さないため。
    source: str = "shares"
    # 第2経路の倍率を決めた第3の信号＝**翌年**の `issued_shares` 比（#659）。
    # 第1経路では None（倍率は当年の `sh_ratio` そのもの）。
    lagged_sh_ratio: Optional[float] = None
    # 第1経路のペアの純資産総額比（#657）。チェックの有無によらず記録する（判定できなければ
    # None）。第2経路では None——倍率の出どころが別の年のペアなので、同じ量ではない。
    equity_ratio: Optional[float] = None
    # 第2経路で**翌年の行が無い**ときに倍率を決めた公式 `AdjFactor` の株数比（#661）。
    # イベント窓の中の公式イベントの積の逆数。`lagged_sh_ratio` とは排他（翌年があれば翌年を使う）。
    official_ratio: Optional[float] = None


class MatchResult(NamedTuple):
    edinet_code: str
    year: int
    detected: float               # canonical（無ければ生比）
    raw_detected: float           # スナップ前の生比（経路の候補ゲートに使った量）
    official: Optional[float]
    n_official_events: int
    status: str
    # status: agree | agree_raw_only | disagree_magnitude | no_official_event
    #         | no_official_event_partial | no_official_bars | official_only | out_of_coverage
    #         | no_sec_code


def _log(x: float) -> float:
    return math.log(x)


def snap_to_canonical(ratio: float, *, tol: float = DEFAULT_SNAP_TOL
                      ) -> tuple[Optional[float], float, str]:
    """観測比を実在する分割比へ寄せる。戻り値 (canonical, residual, kind)。

    株価の遡及調整は**分割にしか掛からない**（時価発行増資では過去株価は調整されない）ので、
    観測した株数比をそのまま歪み倍率に使うと合成ケースで過大評価になる。定番比へ寄せ、
    残差 `ratio / canonical` を増資・自社株買い成分として分離する。

    実測の合成例 4.7891 は 5.0 へ寄る（対数残差 0.043）。4.0 は選ばれない（同 0.18）。
    どの定番比にも合成としても寄らない比（例 7.0 は最近傍 5.0 に対し残差 1.4）は
    **勝手に丸めず** `unsnapped` にして主集計から外す
    （丸めると「測っていない値」が数字に混ざる）。
    """
    if ratio <= 0:
        return None, ratio, "unsnapped"
    best = min(CANONICAL_RATIOS, key=lambda c: abs(_log(ratio / c)))
    residual = ratio / best
    if abs(_log(residual)) <= _log(1 + tol):
        return best, residual, ("split" if best > 1 else "reverse")
    if COMPOSITE_LO <= residual <= COMPOSITE_HI:
        return best, residual, "composite"
    return None, ratio, "unsnapped"


def _usable(row: AnnualRow) -> Optional[str]:
    """イベント判定に使える行か。使えないなら理由コードを返す。"""
    if row.issued_shares is None or row.issued_shares <= 0:
        return "missing_shares"
    if row.bs_bps is None:
        return "missing_bps"
    if row.bs_bps < 0:
        return "negative_bps"          # 債務超過。逆比の符号が反転して幽霊イベントになる
    if row.bs_bps == 0:
        return "missing_bps"
    return None


def equity_ratio(prev: AnnualRow, cur: AnnualRow) -> Optional[float]:
    """純資産総額の前年比。どちらかが欠損・0 以下なら判定できないので None。"""
    a, b = prev.bs_total_equity, cur.bs_total_equity
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return b / a


def equity_contradicts_split(sh_ratio: float, eq_ratio: float, tol: float) -> bool:
    """株数の動きを「分割ではなく増資（併合側なら減資）」と読む純資産の動きか（#657）。

    **片側でしか判定しない。** 分割は純資産を変えないが、増資は株数と純資産を同じ向きに動かす。
    株数が増えた年に純資産が減るのは赤字・減損であって、増資の証拠にはならない。
    """
    le = _log(eq_ratio)
    return le * _log(sh_ratio) > 0 and abs(le) > _log(1 + tol)


def listing_gap_in_pair(prev: AnnualRow, cur: AnnualRow,
                        series: Optional[tuple[str, Sequence[tuple[str, str]]]],
                        coverage_start: Optional[str], *,
                        min_gap_years: int = LISTING_GAP_MIN_YEARS,
                        min_hole_days: int = LISTING_GAP_MIN_DAYS
                        ) -> Optional[tuple[Optional[str], str]]:
    """このペアは上場廃止をまたいで別の実体の行が隣り合ったものか（#672）。該当すれば価格の空白を返す。

    同じ EDINET コードのまま上場廃止→再上場すると、欠損年をまたいで旧社と新社の行がペアになる
    （実測 E05714: 2020-03-31 の旧ソニーフィナンシャルHD と 2026-03-31 の新社・株数比 15.56）。
    **ペアの期間の中に、この社の市場価格が1年以上無く、当期末までに価格が戻っている区間があれば、
    その間この銘柄は取引されていなかった**と読む。条件は次の2つ:

    - `gap_years >= min_gap_years`（少なくとも1期ぶんの通期行が無い）
    - 価格の空白 (a, b) のうち、b（価格が戻った週）が窓 `(max(前期末, coverage_start), 当期末]` の
      中にあり、窓の中に入っている長さ `b - max(a, 窓の始まり)` が `min_hole_days` 日以上のものがある

    空白は2種類: 系列の開始前 `(None, 最初の週)`（旧社の価格が表に無い・E05714）と、系列の途中の
    `series[1]` の各区間（旧社の価格が残っている・E03530）。`series` の形は
    `(最初の week_start, ((空白直前の週, 空白直後の週), ...))`（`load_price_series`）。

    戻り値は該当した空白 `(a, b)`（系列の開始前なら a は None）。該当しなければ None。

    価格が当期末より後に戻る空白は数えない——当期末の行が上場廃止中に提出されたものなら、旧社と
    同じ実体でありうるので何も言えない。`coverage_start` は週次株価表全体の開始日＝「価格が無い」を
    観測できる下限で、それより前はどの社にも価格が無いので空白に数えない。**系列を持たない社
    （`series` が None）は判定しない**——上場廃止済みで価格が消えた社と区別できないので、今日どおり
    比べる側へ倒す。
    """
    if series is None or coverage_start is None:
        return None
    if cur.year - prev.year < min_gap_years:
        return None
    p0, p1 = _iso(prev.period_end), _iso(cur.period_end)
    if not p0 or not p1:
        return None
    start, holes = series
    w0 = max(date.fromisoformat(p0), date.fromisoformat(coverage_start[:10]))
    w1 = date.fromisoformat(p1)
    for a, b in ((None, start), *holes):
        back = date.fromisoformat(b[:10])
        if not (w0 < back <= w1):
            continue
        since = w0 if a is None else max(date.fromisoformat(a[:10]), w0)
        if (back - since).days >= min_hole_days:
            return (a, b)
    return None


def detect_events(rows: Sequence[AnnualRow], *,
                  min_ratio: float = DEFAULT_MIN_RATIO,
                  bps_tol: float = DEFAULT_BPS_TOL,
                  snap_tol: float = DEFAULT_SNAP_TOL,
                  bps_path: bool = DEFAULT_BPS_PATH,
                  equity_tol: Optional[float] = DEFAULT_EQUITY_TOL,
                  official_events: Optional[Mapping[str, Sequence[tuple[str, float]]]] = None,
                  price_series: Optional[Mapping[str, tuple[str, Sequence[tuple[str, str]]]]] = None,
                  official_coverage: Optional[Mapping[str, Sequence[Sequence[str]]]] = None,
                  ) -> tuple[list[ShareEvent], dict]:
    """株数基準が変わった年を検出する。戻り値 (events, stats)。

    経路は2本あり、**どちらも「候補ゲート1つ ＋ 独立した第2の書き手による交差検証1つ」**と
    いう同じ形をしている。閾値が偽陽性を止めているのではなく、交差検証が止めている。

    - 第1経路（`source="shares"`）: `issued_shares` の年次比が閾値を超えること（候補ゲート）と、
      `bs_bps` の逆比が同じ倍率で一致すること（交差検証）。増資・自社株買いはここで落ちる。
    - 第2経路（`source="bps"`・#656）: `bs_bps` の年次比が閾値を超えること（候補ゲート）と、
      `pl_eps` の比が同じ倍率で一致すること（交差検証）。減損・大幅赤字はここで落ちる。

    **倍率の出どころは経路ごとに違う**（#659）。第1経路は候補ゲートに使った `sh_ratio` を
    そのまま倍率にできるが、第2経路はできない。`bs_bps` は分割以外（内部留保・有価証券の
    評価差額）でも増えるので、年次比は真の分割比 F に対し `F / (1 + g)` になる。実測の g は
    18%〜67% と幅が広く、隣り合う定番比の間隔（例 2.0 と 2.5）を超えるため、スナップ先が
    系統的に1段小さい側へ落ちる（公式との一致率 0.367・外れた16件すべてで検出 < 公式）。
    そこで第2経路の倍率は**翌年の `issued_shares` 比という独立な第3の信号**から取る。
    しまむら型は bps が動いた翌年に株数が動くので、そこに真の分割比が現れる。
    **第3の信号が取れない年（翌年の行がまだ無い等）は採らない**——倍率を間違えた補正は
    系統誤差を別の系統誤差へ置き換えるだけで、ADR-0055 決定4 が採った取引とは別物になる。
    係数表は毎晩全置換なので、翌年の決算が入れば自動で補正が入る。

    **第2経路が要るのは、株数と1株指標が同じ年に動くとは限らないから。** 分割を 1 株指標には
    反映しているのに `issued_shares` が据え置きの社は第1経路の候補にすら上がらない
    （実測 E03137 しまむらは `shares x1.0000` のまま `bps x1.8670 / eps x1.8971`）。
    逆にこの社は株数が動いた年の `bs_bps` が**上がって**いるため、第1経路の交差検証でも落ちる。
    同一年ペアの中で株数と bps を突き合わせる設計では原理的に拾えない。

    **同じ (edinet_code, year) を両経路が拾ったら第1経路を採る**（畳む）。株数は分割で必ず動く
    量で、bps のように内部留保や配当で毎年動く量より基準として素直だからである。

    欠損年があるときは `year-1` ではなく**直前の使える行**とペアを組み、`gap_years` を残す。
    2以上なら複数イベントの積を1件と見ている可能性があるので、突合サンプルへ優先的に入れる。

    **`equity_tol` を与えると、第1経路に純資産総額のチェックが加わる**（#657）。bps の交差検証を
    通ったあと、株数と同じ向きに `bs_total_equity` が `1 + equity_tol` 倍を超えて動いていたら
    採らない（`stats["equity"]["rejected"]` に残す）。純資産が欠損・0 以下で判定できない社は
    **今日までどおり採って** `n_unknown` に数える——「増資でない」とも「増資だ」とも読まない。
    第2経路には掛けない（倍率は翌年のペアから取る別の主張で、成長企業の本物の分割を巻き込む）。
    第1経路が落としたペアを第2経路が独立に拾うことは妨げない（畳むのは第1経路が**採った**とき
    だけ、という上の規則と揃える）。

    **`official_events` を与えると、翌年の行が無い第2経路のペアだけ公式 `AdjFactor` から倍率を
    取る**（#661・ADR-0055 決定4-5）。形は `{edinet_code: [(日付, AdjFactor), ...]}`。
    イベント窓（`event_window`）の中の公式イベントの積の逆数を第3の信号として、動いているか・
    向きが合うかを翌年の株数と同じ規則で判定し、**比は定番比へ丸めずそのまま倍率にする**
    （`AdjFactor` は株価の遡及調整に使われた係数そのもので、増資の分が混ざらない）。**窓の中に公式イベントが
    無ければ今日までどおり採らない**——行が無いことを「分割は無かった」とは読まない（取り込み前・
    夜の取りこぼし・エンバーゴ中と区別できない）ので、公式が落ちた晩も補正が誤るのではなく
    採らない側へ倒れる。翌年の行があるペアは公式を倍率に使わず、食い違いを
    `stats["bps_path"]["official_crosscheck"]` に数えるだけにする。
    None（既定）なら公式を一切見ない＝測定器の CLI はこちらを使う。公式から倍率を決めた
    イベントを公式と突合すると、定義上必ず一致して一致率が黙って膨らむためである。

    **`price_series` を与えると、上場廃止をまたいで別の実体の行が隣り合うペアを比べない**
    （#672・ADR-0055 決定4-7）。形は
    `{edinet_code: (最初の week_start, ((空白直前の週, 空白直後の週), ...))}`
    （`load_price_series`）。判定は `listing_gap_in_pair` で、週次株価表全体の開始日は
    各社の最初の週の最小値を使う。該当ペアは**どちらの経路にも渡さず**
    `stats["listing_gap"]["rejected"]` に残す。第2経路が翌年の株数を先読みするときも、
    (当年, 翌年) が該当ペアなら「同じ実体の翌年の行は無い」として扱う（翌年の行が無い年と同じ
    分岐＝公式か倍率待ちへ）。写像に居ない社は判定しない。None（既定）なら判定しない。

    **`official_coverage` を与えると、第1経路のイベントを公式の不在で外す**（#668・ADR-0055 決定4-8）。
    形は `{edinet_code: [(最初のバーの日, 最後のバーの日), ...]}`（併合済み・`merge_spans`）で、
    `official_events` と一緒に渡す（片方だけなら `ValueError`）。bps の交差検証と純資産比チェックを
    通って採る直前に、その社の区間がイベント窓を覆い（`window_confirmed`）、かつ窓の中に公式イベントが
    無ければ採らず `stats["official_absence"]["rejected"]` に残す。**区間が無い社・窓がはみ出す社・
    窓の中に公式イベントがある社は今日までどおり採る**——`jquants_adj_factor_events` に行が無いだけでは
    「分割は無かった」と読めない（決定4-5）ので、読めるのは社単位で取得した記録が窓を覆うときだけ。
    公式にある事象が分割でなくても、登録が無ければこの規則は補正を残す向きにしか効かない。
    **保留の窓（`WITHHELD_OFFICIAL_ADJUSTMENTS`・#652 の新株予約権無償割当）に登録した公式イベントは、
    台帳がここへ渡す前に外す**（#739）。その社では「窓の中に公式イベントが無い」と読まれ、区間が窓を
    覆えば第1経路を外す向きに効く——登録の意味（DB の株価は遡及調整されていない＝F を掛けない）と
    同じ向きである。夜間ログでは「公式に分割が無い」の行として出るので、同じ晩の「保留の窓で外した
    公式イベント」の行と合わせて読む。
    **第2経路には掛けない**——期末後・提出前の分割を1株指標だけが先取りする社を拾う経路で、効力日が
    イベント窓の外に出うる。第1経路が不在で落としたペアを第2経路が独立に拾うことは妨げない（純資産比
    チェックと同じ規則）。測定器の CLI には渡さない（渡すと偽陽性が突合から消え、偽陽性率を測れない）。
    """
    if official_coverage is not None and official_events is None:
        raise ValueError("official_coverage は official_events と一緒に渡す"
                         "（不在を読む相手が無いのに判定したことにしない）")
    gate = _log(min_ratio)
    coverage_start = min(v[0] for v in price_series.values()) if price_series else None
    listing_gap_rejected: list[dict] = []
    n_lagged_listing_gap = 0
    by_ec: dict[str, list[AnnualRow]] = defaultdict(list)
    for r in rows:
        by_ec[r.edinet_code].append(r)

    events: list[ShareEvent] = []
    skipped: Counter = Counter()
    candidate_ecs: set[str] = set()
    n_candidates = 0
    bps_candidate_ecs: set[str] = set()
    n_bps_candidates = 0
    bps_rejected: Counter = Counter()
    # bps 経路が「翌年の株数の動き」を倍率に使ったとき、その動きの年を覚えておく。
    # 第1経路が同じ動きを別イベントとして採っていたら、あとで畳む（#659）。
    lagged_year_of: dict[int, int] = {}          # events の添字 -> 倍率に使った翌年の year
    shares_pairs: set[tuple[str, int]] = set()   # 第1経路が採った (ec, year)
    equity_rejected: list[dict] = []
    n_equity_unknown = 0
    # 倍率の出どころが無くて採らなかった第2経路のペア（#661）。取り込み CLI の対象選びと
    # 夜間ログが読む。件数は `bps_rejected["no_lagged_row"]` と常に一致する。
    awaiting: list[dict] = []
    # 公式の不在で外した第1経路のイベント（#668）。
    absence_rejected: list[dict] = []

    for ec, rs in by_ec.items():
        # **使える行だけを先に並べる。** 翌年の株数を見るには次の行を先読みする必要があり、
        # 逐次ループのままでは書けない（`prev` は持てても `next` は持てない）。
        usable: list[AnnualRow] = []
        for cur in sorted(rs, key=lambda r: r.year):
            why = _usable(cur)
            if why:
                skipped[why] += 1
                continue
            usable.append(cur)

        series = price_series.get(ec) if price_series else None
        for i in range(1, len(usable)):
            prev, cur = usable[i - 1], usable[i]
            hole = listing_gap_in_pair(prev, cur, series, coverage_start)
            if hole is not None:
                # 旧社と新社の行を比べても分割の証拠にならない（#672）。**経路に渡す前に外す**
                # ——株数も bps も実体が替わったぶん動くので、どちらの交差検証も素通りしうる。
                listing_gap_rejected.append({
                    "edinet_code": ec, "year": cur.year, "prev_year": prev.year,
                    "prev_period_end": _iso(prev.period_end), "period_end": _iso(cur.period_end),
                    "no_price_after": hole[0], "price_back": hole[1],
                    "sh_ratio": cur.issued_shares / prev.issued_shares,
                    "bps_ratio": prev.bs_bps / cur.bs_bps,
                })
                continue
            nxt = usable[i + 1] if i + 1 < len(usable) else None
            next_crosses = (nxt is not None
                            and listing_gap_in_pair(cur, nxt, series, coverage_start) is not None)
            if next_crosses:
                nxt = None          # 別の実体の翌年行は、倍率を決める第3の信号にならない
            took = False
            sh_ratio = cur.issued_shares / prev.issued_shares
            bps_ratio = prev.bs_bps / cur.bs_bps
            if abs(_log(sh_ratio)) >= gate:
                n_candidates += 1
                candidate_ecs.add(ec)
                if abs(bps_ratio / sh_ratio - 1.0) <= bps_tol:
                    canonical, residual, kind = snap_to_canonical(sh_ratio, tol=snap_tol)
                    eq_ratio = equity_ratio(prev, cur)
                    if equity_tol is not None and eq_ratio is None:
                        n_equity_unknown += 1
                    if (equity_tol is not None and eq_ratio is not None
                            and equity_contradicts_split(sh_ratio, eq_ratio, equity_tol)):
                        # 純資産が株数と一緒に動いた＝増資（併合側なら減資）と読む（#657）。
                        # 種別はスナップしてから決めたものを残す（どの種別を落としたかが要る）。
                        equity_rejected.append({
                            "edinet_code": ec, "year": cur.year, "prev_year": prev.year,
                            "period_end": cur.period_end, "prev_period_end": prev.period_end,
                            "kind": kind, "canonical": canonical, "sh_ratio": sh_ratio,
                            "bps_ratio": bps_ratio, "equity_ratio": eq_ratio,
                        })
                    else:
                        ev = ShareEvent(
                            edinet_code=ec, year=cur.year, prev_year=prev.year,
                            gap_years=cur.year - prev.year,
                            period_end=cur.period_end, prev_period_end=prev.period_end,
                            sh_ratio=sh_ratio, bps_ratio=bps_ratio,
                            canonical=canonical, residual=residual, kind=kind,
                            source="shares", equity_ratio=eq_ratio)
                        win = event_window(ev)
                        if (official_coverage is not None
                                and window_confirmed(ev, official_coverage.get(ec))
                                and official_ratio_in_window(official_events.get(ec, ()),
                                                             win)[0] is None):
                            # 公式のバーを窓の全期間ぶん受け取ったのに企業イベントが無い＝分割ではない
                            # （#668）。深い割引の増資は形でも純資産比でも本物と分けられない（決定4-4）。
                            absence_rejected.append({
                                "edinet_code": ec, "year": cur.year, "prev_year": prev.year,
                                "kind": kind, "canonical": canonical, "sh_ratio": sh_ratio,
                                "window": win,
                            })
                        else:
                            events.append(ev)
                            shares_pairs.add((ec, cur.year))
                            took = True

            if bps_path and abs(_log(bps_ratio)) >= gate:
                n_bps_candidates += 1
                bps_candidate_ecs.add(ec)
                if took:
                    # 第1経路が同じペアを既に採った。両方が同じ実体を指しているので
                    # 2件に数えない（数えると `cumulative_factors` が比を二乗する）。
                    bps_rejected["dup_with_shares"] += 1
                elif (prev.pl_eps is None or cur.pl_eps is None
                        or prev.pl_eps <= 0 or cur.pl_eps <= 0):
                    # **符号が跨ぐ年・赤字の年は比の意味が壊れる。** 赤字継続（両年とも負）でも
                    # 比は数学的には出るが、赤字幅の増減が分割比に化けるので落とす側を採る。
                    bps_rejected["eps_sign"] += 1
                elif abs((prev.pl_eps / cur.pl_eps) / bps_ratio - 1.0) > bps_tol:
                    # 交差検証で落ちた本体。減損・大幅赤字・タグ基準の変更はここへ来る
                    # （bps だけが動いて eps が追随しない）。
                    bps_rejected["eps_mismatch"] += 1
                elif nxt is None:
                    if next_crosses:
                        n_lagged_listing_gap += 1
                    # **翌年の株数が無い。** 最新年のイベントはここへ来る（翌年の決算がまだ
                    # 提出されていない）。公式 `AdjFactor` が窓の中にあればそれを第3の信号に
                    # する（#661）。無ければ採らずに次回へ送る＝係数表は毎晩全置換なので、
                    # 公式が入るか翌年の行が入れば自動で補正が入る（#659）。
                    cand = ShareEvent(
                        edinet_code=ec, year=cur.year, prev_year=prev.year,
                        gap_years=cur.year - prev.year,
                        period_end=cur.period_end, prev_period_end=prev.period_end,
                        sh_ratio=sh_ratio, bps_ratio=bps_ratio,
                        canonical=None, residual=bps_ratio, kind="unsnapped", source="bps")
                    off, _ = (official_ratio_in_window(official_events.get(ec, ()),
                                                       event_window(cand))
                              if official_events is not None else (None, 0))
                    if off is None:
                        bps_rejected["no_lagged_row"] += 1
                        awaiting.append({
                            "edinet_code": ec, "year": cur.year,
                            "prev_period_end": _iso(prev.period_end),
                            "period_end": _iso(cur.period_end), "bps_ratio": bps_ratio,
                        })
                    elif abs(_log(off)) < gate:
                        # 公式は窓の中で動いているが、1株指標の動きを説明するほどではない
                        # （1:1.1 の無償割当など）。`lagged_flat` と同じ理由で採らない。
                        bps_rejected["official_flat"] += 1
                    elif _log(off) * _log(bps_ratio) < 0:
                        bps_rejected["official_direction"] += 1
                    else:
                        # **公式の比は定番比へ丸めない。** 丸めは「株数比に増資の分が混ざる」のを
                        # 切り離すための仕組みで、`AdjFactor` は株価の遡及調整に使われた係数
                        # そのもの＝F の正本である。丸めると定番比の表に無い 1:6 が 5 へ寄って
                        # F が 17% 小さく入り、1:7 は採られない（実測 2026-09-13: E38205 /
                        # E38979 / E02128）。
                        events.append(cand._replace(
                            canonical=off, residual=1.0,
                            kind="split" if off > 1 else "reverse", official_ratio=off))
                else:
                    lag = nxt.issued_shares / cur.issued_shares
                    if abs(_log(lag)) < gate:
                        # 翌年も株数が動いていない。1株指標だけが動いた理由を分割だと
                        # 言い切れる材料が無いので採らない。
                        bps_rejected["lagged_flat"] += 1
                    elif _log(lag) * _log(bps_ratio) < 0:
                        # 向きが逆（bps は分割方向・株数は併合方向）。同じ事象ではない。
                        bps_rejected["lagged_direction"] += 1
                    else:
                        canonical, residual, kind = snap_to_canonical(lag, tol=snap_tol)
                        if canonical is None:
                            bps_rejected["lagged_unsnapped"] += 1
                        else:
                            # **倍率は `lag` から決め、`bps_ratio` / `sh_ratio` は観測値の
                            # まま残す**（あとから「当年は株数が動いていない」が読める）。
                            lagged_year_of[len(events)] = nxt.year
                            events.append(ShareEvent(
                                edinet_code=ec, year=cur.year, prev_year=prev.year,
                                gap_years=cur.year - prev.year,
                                period_end=cur.period_end, prev_period_end=prev.period_end,
                                sh_ratio=sh_ratio, bps_ratio=bps_ratio,
                                canonical=canonical, residual=residual, kind=kind,
                                source="bps", lagged_sh_ratio=lag))

    # **同じ株数の動きを2回数えない。** bps 経路が翌年の動きを倍率に使い、かつ第1経路が
    # その翌年を独立したイベントとして採っていたら、`cumulative_factors` が比を二乗する
    # （`dup_with_shares` と同じ実害）。第1経路を残す側に倒すのは、株数が分割で必ず動く量で
    # 基準として素直だからである。判定はループを抜けてから——第1経路が採るかどうかは
    # 翌年のペアを処理するまで決まらない。
    if lagged_year_of:
        kept: list[ShareEvent] = []
        for i, e in enumerate(events):
            ly = lagged_year_of.get(i)
            if ly is not None and (e.edinet_code, ly) in shares_pairs:
                bps_rejected["dup_lagged_with_shares"] += 1
                continue
            kept.append(e)
        events = kept

    # 翌年の株数から倍率を決めたイベントを公式と突き合わせる（#661）。**イベントは変えず数えるだけ**
    # ——両方が取れる年で2つの書き手が合っているかを、毎晩追加の取得なしに測り続けるため。
    # 公式イベントが窓に無いものは数えない（取り込み前・エンバーゴ中と区別できない）。
    crosscheck = {"agree": 0, "disagree": 0, "disagreements": []}
    if official_events is not None:
        for e in events:
            if e.source != "bps" or e.lagged_sh_ratio is None:
                continue
            m = match_event(e, official_events.get(e.edinet_code, ()))
            if m.official is None:
                continue
            if m.status == "agree":
                crosscheck["agree"] += 1
            else:
                crosscheck["disagree"] += 1
                crosscheck["disagreements"].append({
                    "edinet_code": e.edinet_code, "year": e.year, "status": m.status,
                    "lagged": e.canonical, "official": m.official,
                })

    bps_events = [e for e in events if e.source == "bps"]
    stats = {
        "n_candidate_pairs": n_candidates,
        "n_events": len(events),
        "n_event_companies": len({e.edinet_code for e in events}),
        "n_candidate_companies": len(candidate_ecs),
        "skipped": dict(skipped),
        "by_kind": dict(Counter(e.kind for e in events)),
        "n_gap_years_ge2": sum(1 for e in events if e.gap_years >= 2),
        "n_events_by_source": dict(Counter(e.source for e in events)),
        "bps_path": {
            "enabled": bool(bps_path),
            "n_candidate_pairs": n_bps_candidates,
            "n_candidate_companies": len(bps_candidate_ecs),
            "n_events": len(bps_events),
            "n_event_companies": len({e.edinet_code for e in bps_events}),
            "by_kind": dict(Counter(e.kind for e in bps_events)),
            "rejected": dict(bps_rejected),
            # 倍率の出どころ（#661）。official は翌年の行が無い年に公式 AdjFactor で埋めた件数。
            "magnitude_source": {
                "lagged_shares": sum(1 for e in bps_events if e.lagged_sh_ratio is not None),
                "official": sum(1 for e in bps_events if e.official_ratio is not None),
            },
            "awaiting_magnitude": awaiting,
            "official": {
                "enabled": official_events is not None,
                "crosscheck": crosscheck,
            },
        },
        "equity": {
            "enabled": equity_tol is not None,
            "tol": equity_tol,
            "n_rejected": len(equity_rejected),
            "rejected_by_kind": dict(Counter(r["kind"] for r in equity_rejected)),
            "n_unknown": n_equity_unknown,
            "rejected": equity_rejected,
        },
        "official_absence": {
            "enabled": official_coverage is not None,
            "n_companies_with_record": len(official_coverage or {}),
            "n_rejected": len(absence_rejected),
            "rejected": absence_rejected,
        },
        "listing_gap": {
            "enabled": price_series is not None,
            "coverage_start": coverage_start,
            "n_rejected": len(listing_gap_rejected),
            "rejected": listing_gap_rejected,
            # 第2経路が翌年の株数を先読みしようとして、そのペアが該当したので使わなかった件数。
            "n_lagged": n_lagged_listing_gap,
        },
    }
    return events, stats


def cumulative_factors(rows: Sequence[AnnualRow], events: Sequence[ShareEvent], *,
                       use_canonical: bool = True) -> dict[tuple[str, int], float]:
    """行ごとの累積歪み倍率 F。**その行より後**に起きたイベントの積だけを掛ける。

    `e.year > row.year` が正しい。`>=` にすると分割当年の行まで被害に数え、社あたり1行ずつ
    被害行数が膨らむ。当年の行は既に新株数基準の提出値なので歪んでいない。

    `use_canonical=True` では `unsnapped`（丸められない比）を**無視する**＝主集計から外す。
    上限見積りが要るときは False を渡して観測比の積を取る。
    """
    ev_by_ec: dict[str, list[ShareEvent]] = defaultdict(list)
    for e in events:
        ratio = e.canonical if use_canonical else e.sh_ratio
        if ratio is None:
            continue
        ev_by_ec[e.edinet_code].append(e._replace(canonical=ratio))

    out: dict[tuple[str, int], float] = {}
    for r in rows:
        f = 1.0
        for e in ev_by_ec.get(r.edinet_code, ()):
            if e.year > r.year:
                f *= e.canonical
        out[(r.edinet_code, r.year)] = f
    return out


def _iso(d) -> Optional[str]:
    if d is None:
        return None
    return str(d)[:10]


def event_window(ev: ShareEvent, *, slack_days: int = 45) -> Optional[tuple[str, str]]:
    """突合に使う日付窓 (前期末 - slack, 当期末 + slack]。

    分割の効力発生日と、株数がどちらの期に計上されるかのズレを吸収するための余裕。
    """
    p0, p1 = _iso(ev.prev_period_end), _iso(ev.period_end)
    if not p0 or not p1:
        return None
    return ((date.fromisoformat(p0) - timedelta(days=slack_days)).isoformat(),
            (date.fromisoformat(p1) + timedelta(days=slack_days)).isoformat())


def official_ratio_in_window(official: Sequence[tuple[str, float]],
                             window: Optional[tuple[str, str]]
                             ) -> tuple[Optional[float], int]:
    """窓 (w0, w1] の中の公式イベントを株数比へ直した値と件数。無ければ (None, 0)。

    公式の `AdjFactor` は**過去株価に掛ける係数**なので 1:2 分割は 0.5 で返る。株数比へ
    直すため逆数を取る。同一窓に複数イベントがあれば積になる（DB 側の年次比も積なので整合する）。
    突合（`match_event`）と本番の倍率決め（`detect_events`・#661）が共有する＝窓の意味を割らない。
    """
    if window is None:
        return None, 0
    w0, w1 = window
    inside = [f for d, f in official if w0 < d <= w1 and f and f > 0]
    if not inside:
        return None, 0
    prod = 1.0
    for f in inside:
        prod *= f
    return 1.0 / prod, len(inside)


def in_coverage(ev: ShareEvent, coverage: Optional[tuple[str, str]], *,
                slack_days: int = 45, mode: str = "full") -> bool:
    """公式がこのイベントを判定できるか。

    `mode="full"`（既定）は窓が契約期間へ**完全に**収まるときだけ True。公式にイベントが
    無ければ「分割は無かった」と読めるので、偽陽性率まで測れる代わりに範囲が狭い。

    `mode="partial"` は窓が契約期間と**重なって**いれば True（#659）。
    **倍率が合っているかだけを測るための緩め方**で、重なりの中で公式イベントが見つかった件
    だけを分母に数える（見つからなかった件は「分割が無かった」と「契約窓の外で起きた」を
    区別できないので分母から外す＝`match_event` が `no_official_event_partial` を返す）。

    なぜ緩める必要があるか: 第2経路の倍率は翌年の `issued_shares` から取るので、突合には
    「公式が判定できる」と「翌年の行が提出済み」の両方が要る。契約窓は直近2年なので
    full では当期末が窓の後ろ寄りのイベントしか残らず、その翌年の決算はまだ存在しない
    ——**2つの条件は full のままでは今日の時点で排他**である（実測 2026-09-12: 契約窓
    2024-06-20〜2026-06-20 に対し、窓内の bps 経路イベント 57件はいずれも最新年）。
    """
    win = event_window(ev, slack_days=slack_days)
    if win is None:
        return False
    if not coverage or not coverage[0] or not coverage[1]:
        return True
    if mode == "partial":
        return win[0] <= coverage[1] and coverage[0] <= win[1]
    return coverage[0] <= win[0] and win[1] <= coverage[1]


def bars_spans(rows: Sequence[Mapping], *, max_gap_days: int = MAX_BAR_GAP_DAYS
               ) -> list[tuple[str, str, int]]:
    """公式の日次バーを、実際に受け取った連続区間 `[(最初の日, 最後の日, 本数), ...]` へ畳む（#668）。

    **「公式にイベントが無い」と読めるのは、この区間の中だけ**である。要求した期間ではなく受け取った
    バーで決めるのは、J-Quants が扱わない社（実測 E03474・契約窓内 0 本）も 429 が続いた社も `[]` で
    返り、「イベントが無い」と同じ形になるから。0 本なら `[]`。隣り合うバーの間隔が `max_gap_days`
    を超えたら区間を切る（取得漏れ・売買停止の中で起きたイベントを見落としうる）。
    """
    ds = sorted({str(r.get("Date"))[:10] for r in rows if r.get("Date")})
    out: list[tuple[str, str, int]] = []
    if not ds:
        return out
    start = prev = ds[0]
    n = 1
    for d in ds[1:]:
        if (date.fromisoformat(d) - date.fromisoformat(prev)).days > max_gap_days:
            out.append((start, prev, n))
            start, n = d, 0
        prev = d
        n += 1
    out.append((start, prev, n))
    return out


def merge_spans(spans: Iterable[Sequence[str]]) -> list[tuple[str, str]]:
    """重なる・接する区間 `(from, to)` を併合する（日付順）。**取得記録の併合の唯一の源**（#668）。

    取り込みを回すたびに区間を追記するので、同じ社に重なる区間が複数行ある。`to` の翌日から次の区間が
    始まる場合も1本にする。3要素以上の組は先頭2つだけを使う（`bars_spans` の出力をそのまま渡せる）。
    """
    out: list[list[str]] = []
    for s in sorted((str(x[0])[:10], str(x[1])[:10]) for x in spans):
        if out and date.fromisoformat(s[0]) <= date.fromisoformat(out[-1][1]) + timedelta(days=1):
            out[-1][1] = max(out[-1][1], s[1])
        else:
            out.append([s[0], s[1]])
    return [(a, b) for a, b in out]


def window_confirmed(ev: ShareEvent, spans: Optional[Sequence[Sequence[str]]], *,
                     slack_days: int = 45) -> bool:
    """イベント窓 `(w0, w1]` が、公式のバーを受け取った区間のどれか1本に完全に収まるか（#668）。

    包含の規則は `in_coverage(mode="full")` と同じ `from <= w0 and w1 <= to`。窓が週末に掛かると
    数日ぶん厳しくなるが、偽になる側は「確かめていない＝補正を残す」なので安全側である。
    `spans` は併合済み（`merge_spans`）を渡すこと——併合前の2本にまたがる窓は偽になる。
    """
    win = event_window(ev, slack_days=slack_days)
    if win is None or not spans:
        return False
    return any(str(s[0])[:10] <= win[0] and win[1] <= str(s[1])[:10] for s in spans)


def match_event(ev: ShareEvent, official: Sequence[tuple[str, float]], *,
                slack_days: int = 45, tol: float = 0.05,
                coverage: Optional[tuple[str, str]] = None,
                coverage_mode: str = "full",
                official_spans: Optional[Sequence[Sequence[str]]] = None) -> MatchResult:
    """検出したイベントを公式 `AdjFactor` と突き合わせる。

    公式の `AdjFactor` は**過去株価に掛ける係数**なので 1:2 分割は 0.5 で返る。株数比へ
    直すため逆数を取る。同一窓に複数イベントがあれば積になり、DB 側の年次比も積なので整合する。

    窓は (前期末 - slack, 当期末 + slack]。分割の効力発生日と株数の計上期のズレを吸収する。
    契約窓の外は `out_of_coverage` にして**一致率の分母から外す**（混ぜると理由なく下がる）。

    `coverage_mode="partial"` では窓と契約窓の重なりの中だけを探し、公式イベントが無ければ
    `no_official_event_partial` を返す（#659）。**この status は分母に入れない**——重なりの
    外で起きた分割は公式が返さないので、「分割が無かった」と区別できないためである。

    **`official_spans`（その社の公式バーを受け取った区間・`bars_spans` の出力）を渡すと、バーが窓を
    覆っていないときに `no_official_bars` を返す**（#668）。full 窓では窓が区間のどれにも収まらない
    とき、partial 窓ではバーが0本のとき。これも分母に入れない——J-Quants が扱わない社（実測 E03474・
    契約窓内 0 本）は「公式にイベントが無い」と同じ形で返り、#657 の全数突合はそれを偽陽性に数えていた。
    None なら従来どおり区間を見ない。
    """
    # 生比は**そのイベントの倍率を決めた量**を採る。bps 経路は #659 で倍率の出どころが
    # 翌年の株数比へ移ったので、そちらを見る（`bps_ratio` を見ると `agree_raw_only` が
    # 「もう倍率に使っていない量では合う」を数えることになり、意味が黙って壊れる）。
    # 第1経路は候補ゲート＝倍率なので `sh_ratio` のまま。
    if ev.source == "bps":
        # 翌年の株数 → 公式（#661・翌年の行が無い年だけ） → bps の年次比、の順。
        raw = next(r for r in (ev.lagged_sh_ratio, ev.official_ratio, ev.bps_ratio)
                   if r is not None)
    else:
        raw = ev.sh_ratio
    detected = ev.canonical if ev.canonical is not None else raw
    win = event_window(ev, slack_days=slack_days)
    if win is None or not in_coverage(ev, coverage, slack_days=slack_days,
                                      mode=coverage_mode):
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           None, 0, "out_of_coverage")
    w0, w1 = win
    if coverage_mode == "partial" and coverage and coverage[0] and coverage[1]:
        w0, w1 = max(w0, coverage[0]), min(w1, coverage[1])

    official_ratio, n_inside = official_ratio_in_window(official, (w0, w1))
    if official_ratio is None and official_spans is not None and (
            not official_spans if coverage_mode == "partial"
            else not window_confirmed(ev, merge_spans(official_spans), slack_days=slack_days)):
        return MatchResult(ev.edinet_code, ev.year, detected, raw, None, 0, "no_official_bars")
    if official_ratio is None:
        return MatchResult(
            ev.edinet_code, ev.year, detected, raw, None, 0,
            "no_official_event_partial" if coverage_mode == "partial"
            else "no_official_event")

    lim = _log(1 + tol)
    if abs(_log(official_ratio / detected)) <= lim:
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           official_ratio, n_inside, "agree")
    if abs(_log(official_ratio / raw)) <= lim:
        # 生比では合うがスナップ後で外れる＝スナップが悪さをしている側。分けて数える。
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           official_ratio, n_inside, "agree_raw_only")
    return MatchResult(ev.edinet_code, ev.year, detected, raw,
                       official_ratio, n_inside, "disagree_magnitude")


# ── 台帳（純関数）───────────────────────────────────────────────────────────────

class SplitWindow(NamedTuple):
    """分割イベントの窓。検出器の `ShareEvent` と公式 `AdjFactor` の両方をこの形へ寄せる。

    `start` は「この日より後」、`end` は「この日まで」を表す半開区間 (start, end]。
    公式イベントは日付が 1 点なので `start == end` になる。
    """
    start: Optional[date]
    end: Optional[date]
    canonical: Optional[float]
    source: str             # detected | awaiting | official

def split_windows(events: Sequence, awaiting: Sequence[dict],
                  official: Sequence[tuple]) -> list:
    """検出イベント・倍率待ち・公式 AdjFactor を 1 社ぶんの `SplitWindow` 列へ寄せる。

    3 つは形が違うだけで、言っているのは同じ「この窓の中で株数の基準が変わった」である。
    寄せておかないと、判定側が 3 通りの形を知ることになる（そのうち 1 つを足し忘れても
    もっともらしい結果が返る）。
    """
    out: list = []
    for e in events:
        out.append(SplitWindow(_as_date(e.prev_period_end), _as_date(e.period_end),
                               e.canonical, "detected"))
    for a in awaiting:
        out.append(SplitWindow(_as_date(a.get("prev_period_end")), _as_date(a.get("period_end")),
                               None, "awaiting"))
    for d, factor in official:
        day = _as_date(d)
        if day is None or not factor or factor == 1.0:
            continue
        # 公式は「過去株価に掛ける係数」なので 1:2 分割は 0.5。株数比へ直すため逆数を取る。
        out.append(SplitWindow(day - timedelta(days=1), day, 1.0 / factor, "official"))
    return out

def _as_date(v) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None

def factor_after(windows: Sequence[SplitWindow], filing_date: date) -> float:
    """TTM 行の分割補正係数 F ＝ 提出日より後にある窓の定番比の積。

    窓が提出日をまたぐイベントは呼び出し側（`ttm_composite.reject_reason`）が弾いているので、ここでは
    「丸ごと後」だけを掛ければ足りる。倍率の決まっていない窓（`canonical` が None）は
    掛けない＝通期側の `cumulative_factors(use_canonical=True)` と同じ扱いである。
    """
    f = 1.0
    for w in windows:
        if w.canonical and w.start is not None and w.start >= filing_date:
            f *= w.canonical
    return f

@dataclass(frozen=True)
class Ledger:
    """一晩ぶんの企業イベントの知識。`build_ledger` / `compute_ledger` だけが作る。

    `official` は検出器へ渡したものと**同じ**公式イベント（保留の窓に入るものを外したあと・#739）で、
    TTM の分割窓もここから作る。読み手ごとに読み直さないのは、片方の読み手だけが登録表
    （保留の窓など）を通る状態を作らないため。
    """
    rows: list          # AnnualRow（検出器の入力＝通期行）
    events: list        # ShareEvent（検出したイベント）
    stats: dict         # 検出器の内訳（夜間ログへ出す）
    factors: dict       # {(edinet_code, year): 累積 F}（F=1.0 の行も含む）
    official: dict      # {edinet_code: [(日付, AdjFactor), ...]}（検出器へ渡した公式イベント）

    def factor_rows(self) -> list[dict]:
        """`split_adjustment_factors` へ書く行。F=1.0 の行は持たない（VIEW が COALESCE で 1.0 を埋める）。

        寄与イベントの種別を行ごとに引く。**F の値は `factors` をそのまま使い、ここで積を取り直さない**
        （`tests/test_split_adjustment_factors.py` が「寄与集合の積 == factor」を照合して乖離を捕まえる）。
        """
        ev_by_ec: dict = defaultdict(list)
        for e in self.events:
            if e.canonical is not None:
                ev_by_ec[e.edinet_code].append(e)

        out = []
        for (ec, year), f in sorted(self.factors.items()):
            if f == 1.0:
                continue                      # 無補正の行は持たない（VIEW 側が COALESCE で 1.0 を埋める）
            contrib = [e for e in ev_by_ec.get(ec, ()) if e.year > year]
            out.append({
                "edinet_code": ec, "year": year, "factor": f,
                "n_events": len(contrib),
                "kinds": ",".join(sorted({e.kind for e in contrib})) or None,
            })
        return out

    def windows_by_company(self) -> dict:
        """TTM が使う分割窓 `{edinet_code: [SplitWindow, ...]}`。

        検出イベント・第2経路の倍率待ち・公式イベントの3種を社ごとに `split_windows` で寄せる。
        倍率待ちは分割そのものは起きている（倍率が決まっていないだけ）ので、材料の間にあれば
        TTM を作らない側へ効く。
        """
        events_by_ec: dict = defaultdict(list)
        awaiting_by_ec: dict = defaultdict(list)
        for e in self.events:
            events_by_ec[e.edinet_code].append(e)
        for a in ((self.stats.get("bps_path") or {}).get("awaiting_magnitude") or ()):
            awaiting_by_ec[a["edinet_code"]].append(a)
        return {
            ec: split_windows(events_by_ec.get(ec, ()), awaiting_by_ec.get(ec, ()),
                              self.official.get(ec, ()))
            for ec in set(events_by_ec) | set(awaiting_by_ec) | set(self.official)
        }


def _without_withheld(official: Mapping[str, Sequence[tuple[str, float]]]) -> tuple[dict, list]:
    """公式イベントから、保留の窓（`WITHHELD_OFFICIAL_ADJUSTMENTS`・#652）に入るものを外す（#739）。

    戻り値は `(残した公式イベント, 外した一覧)`。外した結果イベントが0件になった社は辞書から消す
    （公式イベントを持たない社と同じ扱い）。一覧の形は修復スクリプトの `judge_company` が出す
    `withheld` に揃え、社コードを足したもの。
    """
    kept: dict = {}
    withheld: list = []
    for ec, evs in official.items():
        held = withheld_official_events(ec, list(evs))
        days = {d for d, _, _, _ in held}
        rest = [e for e in evs if str(e[0])[:10] not in days]
        if rest:
            kept[ec] = rest
        withheld += [{"edinet_code": ec, "date": d, "factor": f, "expected_factor": x, "reason": r}
                     for d, f, x, r in held]
    return kept, withheld


def compute_ledger(rows: Sequence[AnnualRow], *,
                   official: Mapping[str, Sequence[tuple[str, float]]],
                   coverage: Mapping[str, Sequence[Sequence[str]]],
                   series: Mapping[str, tuple[str, Sequence[tuple[str, str]]]],
                   bps_path: Optional[bool] = None) -> Ledger:
    """検出器を回して台帳を作る。**DB に触らない**（テストはここを通す）。

    入力は `build_ledger` が DB から読むものと同じで、**どれもキーワードで必須**にしてある——
    どれかを渡し忘れても検出はもっともらしい結果を返し、測ったものが本番の係数表と別物になる。
    空で渡すのは、その入力が本当に無いときだけにする。

    - `official`: 公式イベント `{edinet_code: [(日付, AdjFactor), ...]}`。翌年の行が無い第2経路の
      倍率（#661）と、第1経路を公式の不在で外す判定（#668）に使い、TTM の分割窓にもなる。
      **保留の窓（#652）に入るものはここで外し**（#739）、外した一覧を `stats["withheld_official"]` に残す
    - `coverage`: 公式のバーを受け取った区間 `{edinet_code: [(最初のバー, 最後のバー), ...]}`。
      **併合前の生の区間でよい**（ここで `merge_spans` を通す＝併合の唯一の源）
    - `series`: 週次株価の系列（`load_price_series`）。上場廃止をまたぐペアを比べないため（#672）
    - `bps_path`: None なら検出器の既定（`DEFAULT_BPS_PATH`）に従う。明示するのは前後を測り
      比べるときだけ（`scripts/measure_split_bias_oof.py`）——呼び出し側が既定を書き写すと乖離する
    """
    rows = list(rows)
    # **保留の窓（#652）に入る公式イベントは、検出器にも TTM の窓にも渡さない**（#739）。公式だけが
    # 当てる調整（新株予約権の無償割当など）で、DB の株価（実約定値）は遡及調整されていない＝F を
    # 掛ける理由が無い。外すのはここ1か所——この `official` を検出器と `Ledger` の両方へ渡すので、
    # 片方の読み手だけが登録を通る形にならない。
    official, withheld = _without_withheld(official)
    merged = {ec: merge_spans(sp) for ec, sp in coverage.items()}
    kw = {} if bps_path is None else {"bps_path": bps_path}
    events, stats = detect_events(rows, official_events=official, price_series=series,
                                  official_coverage=merged, **kw)
    # use_canonical=True（既定）＝定番比へ寄せられなかった `unsnapped` を積から外す。
    factors = cumulative_factors(rows, events)
    # 夜間ログの「公式イベントを持つ社」の数（保留の窓を外したあと＝検出器が見た社数）。
    # 検出器の stats には無いのでここで足す。
    stats["n_official_companies"] = len(official)
    # 保留の窓で外した公式イベント。外したものがどこにも届かないので、ここにしか残らない。
    stats["withheld_official"] = {
        "n_registered_companies": len(WITHHELD_OFFICIAL_ADJUSTMENTS),
        "n_withheld": len(withheld),
        "withheld": withheld,
    }
    return Ledger(rows=rows, events=events, stats=stats, factors=factors, official=official)


# ── I/O ─────────────────────────────────────────────────────────────────────

def _load_annual_rows(db) -> list:
    """検出器の入力になる通期行（`AnnualRow`）を読む。

    **ORDER BY を省かない。** `detect_events` は `sorted(rs, key=lambda r: r.year)` で並べるが
    Python のソートは安定なので、**同じ year に annual 行が2本ある社**（会計期間変更。実測
    30,379 行に対し (ec, year) は 30,321＝58 組）ではペアの向きが入力順で決まる。
    無指定だと run ごとに検出結果が 1〜数件ぶれる。`period_end` まで入れて完全に決める
    （測定器の `_SQL_ANNUAL` は `edinet_code, year` までなので、この 58 組ぶんだけ
    結果が食い違いうる＝再現性を取る側を選ぶ）。
    """
    from database import FinancialRecord

    return [AnnualRow(*r) for r in db.query(
        FinancialRecord.edinet_code, FinancialRecord.year, FinancialRecord.period_end,
        FinancialRecord.issued_shares, FinancialRecord.bs_bps, FinancialRecord.pl_eps,
        FinancialRecord.dps, FinancialRecord.stock_price, FinancialRecord.per,
        FinancialRecord.pbr, FinancialRecord.div_yield, FinancialRecord.market_cap,
        # 純資産総額は第1経路の増資チェック（#657）が読む。`AnnualRow` の末尾の列なので末尾に置く。
        FinancialRecord.bs_total_equity,
    ).filter(FinancialRecord.period_type == "annual").order_by(
        FinancialRecord.edinet_code, FinancialRecord.year, FinancialRecord.period_end,
    ).all()]


def load_jquants_adj_factor_events(db) -> dict:
    """`{edinet_code: [(event_date, adj_factor), ...]}`（日付順）。検出器の `official_events` の形。

    書き手は J-Quants を叩く経路の側（夜間 catchup と `scripts/backfill_adj_factor_events.py`）で、
    台帳は**この表だけを読み J-Quants を叩かない**（外部サービスが落ちた晩に補正が静かに外れる
    経路を作らない・ADR-0055 決定4-5）。行が無いことを「分割は無かった」とは読まない。
    """
    from database import JQuantsAdjFactorEvent

    out: dict = {}
    for ec, d, f in db.query(
        JQuantsAdjFactorEvent.edinet_code, JQuantsAdjFactorEvent.event_date,
        JQuantsAdjFactorEvent.adj_factor,
    ).order_by(JQuantsAdjFactorEvent.edinet_code, JQuantsAdjFactorEvent.event_date).all():
        out.setdefault(ec, []).append((str(d)[:10], float(f)))
    return out


def load_jquants_adj_factor_coverage(db) -> dict:
    """`{edinet_code: [(first_bar_date, last_bar_date), ...]}`（日付順・**併合しない生の区間**）。

    重なる区間の併合は `merge_spans` が唯一の源で、`compute_ledger` が通す。
    """
    from database import JQuantsAdjFactorCoverage

    out: dict = {}
    for ec, d0, d1 in db.query(
        JQuantsAdjFactorCoverage.edinet_code, JQuantsAdjFactorCoverage.first_bar_date,
        JQuantsAdjFactorCoverage.last_bar_date,
    ).order_by(JQuantsAdjFactorCoverage.edinet_code, JQuantsAdjFactorCoverage.first_bar_date,
               JQuantsAdjFactorCoverage.last_bar_date).all():
        out.setdefault(ec, []).append((str(d0)[:10], str(d1)[:10]))
    return out


def load_price_series(db, *, min_hole_days: int) -> dict:
    """`{edinet_code: (最初の week_start, ((空白直前の週, 空白直後の週), ...))}`。検出器の `price_series` の形（#672）。

    分割補正の検出器が「上場廃止をまたいで別の実体の行が隣り合うペア」を比べないために読む。
    上場廃止→再上場は週次株価に2つの形で現れる——系列の開始が遅い（旧社の価格が表に無い・実測
    E05714）か、系列の途中に空白がある（旧社の価格が残っている・実測 E03530）。
    空白は隣り合う週の間隔が `min_hole_days` 以上のものだけを返す（閾値は `LISTING_GAP_MIN_DAYS`）。
    週次株価を1行も持たない社は含まない＝検出器は判定できないとして今日どおり採る。

    **全行を Python へ持ってこない**（週次は約 140万行）。間隔は SQL の LAG で測り、該当する数行だけを返す。
    """
    from sqlalchemy import func, text

    from database import StockPriceWeekly

    starts = {ec: str(ws)[:10] for ec, ws in db.query(
        StockPriceWeekly.edinet_code, func.min(StockPriceWeekly.week_start),
    ).group_by(StockPriceWeekly.edinet_code).all()}
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    days = ("julianday(week_start) - julianday(prev_ws)" if dialect == "sqlite"
            else "week_start::date - prev_ws::date")
    holes: dict = {}
    for ec, a, b in db.execute(text(
        "SELECT edinet_code, prev_ws, week_start FROM ("
        " SELECT edinet_code, week_start,"
        "        LAG(week_start) OVER (PARTITION BY edinet_code ORDER BY week_start) AS prev_ws"
        "   FROM stock_price_weekly) t"
        " WHERE prev_ws IS NOT NULL AND " + days + " >= :d"
        " ORDER BY edinet_code, week_start"), {"d": min_hole_days}).fetchall():
        holes.setdefault(ec, []).append((str(a)[:10], str(b)[:10]))
    return {ec: (s, tuple(holes.get(ec, ()))) for ec, s in starts.items()}


def build_ledger(db, *, bps_path: Optional[bool] = None) -> Optional[Ledger]:
    """入力を読んで台帳を作る。**書き込まない。** annual 行が0件なら None。

    係数表の洗い替え（`rebuild_split_adjustment_factors`）・TTM 合成
    （`ttm_composite.rebuild_ttm_financial_records`）・補正がリークを除いたかの測定
    （`scripts/measure_split_leak.py`・#685・ADR-0055 決定7）が共有する。**読み手ごとに入力を
    揃え直さない**——下の4つ（通期行・公式 AdjFactor・株価系列・受信区間）のどれかを読み忘れても
    検出はもっともらしい結果を返し、測ったものが本番の係数表と別物になる。
    """
    rows = _load_annual_rows(db)
    if not rows:
        return None
    return compute_ledger(
        rows,
        # 翌年の行が無い第2経路の倍率は、catchup が残した公式 AdjFactor から取る（#661・決定4-5）。
        # **ここでは J-Quants を叩かない**。表が空なら検出器は今日までどおり採らない側へ倒れる。
        # DB エラーは握らない（決定5）。
        official=load_jquants_adj_factor_events(db),
        # 公式のバーを社単位で受け取った区間（#668・決定4-8）。第1経路の偽陽性を「公式に分割が無い」と
        # **確かめられた**ときだけ外す。表が空なら何も外さない＝今日までどおり採る側へ倒れる。
        coverage=load_jquants_adj_factor_coverage(db),
        # 上場廃止をまたいで別の実体の行が隣り合うペアを比べないための週次株価の系列（開始日と途中の
        # 空白・#672・決定4-7）。**読み忘れると判定は黙って無効になり**、定番比の 15 が E05714 型の
        # 偽陽性を入れる。
        series=load_price_series(db, min_hole_days=LISTING_GAP_MIN_DAYS),
        bps_path=bps_path,
    )


def rebuild_split_adjustment_factors(db, *, bps_path: Optional[bool] = None,
                                     ledger: Optional[Ledger] = None) -> int:
    """`split_adjustment_factors` を作り直す。戻り値は書いた行数（F≠1.0 の行数）。

    バリュエーション基準の不一致（#655・ADR-0055）を `financial_metrics` VIEW が補正する
    ための係数表を全置換する。**毎晩作り直すのは F が行の固有値ではないから**——F は
    「その行の年より後に起きたイベントの累積積」なので、新しい分割が1件起きればその会社の
    過去全行の値が変わる。焼き付けた値は必ず陳腐化する。

    入力は `financial_records.issued_shares` と `bs_bps` と `pl_eps`（いずれも XBRL 由来）
    だけで、J-Quants の契約窓（2年）に依存しない＝2018年まで遡って復元できる。この復元は
    #654 が公式 `AdjFactor` と突合して**一致率 0.967（29/30・陰性対照の見逃し 0 社）**を
    確認した。上場廃止をまたいで別の実体の行が隣り合うペアを比べないために、週次株価の系列の開始日と
    途中の空白（`stock_price_weekly`）も読む（#672）。

    `ledger` はパイプラインが一晩に1回作った台帳（TTM 合成と同じもの）。渡さなければここで作る。
    `bps_path` は株数が追随しない社を拾う第2経路（#656）の ON/OFF で、**None なら検出器の既定
    （`DEFAULT_BPS_PATH`）に従う**。台帳は作った時点の `bps_path` で固まっているので、`ledger` と
    `bps_path` は同時に渡さない。**2026-09-12 時点の既定は True**で、根拠は公式 `AdjFactor` との
    一致率 0.962（25/26・陰性対照の見逃し 0 社）。倍率を `bs_bps` の年次比ではなく**翌年の
    `issued_shares` 比**から取るようにして 0.367 から上がった（#659）。第1経路の純資産総額
    チェック（#657）も同じく検出器側の `DEFAULT_EQUITY_TOL` に従う。

    **「入力が無い」と「入力はあるのに作れない」を分ける**。annual 行が0件ならスキップして
    0 を返す（初回ブートストラップ前・テストのスタブ DB）。行はあるのに係数が1件も作れない
    のは検出が壊れた側なので `RuntimeError` を上げる。**どちらの場合も既存の表には触らない**
    ——全置換の順序で「消してから失敗」にすると、補正が静かに全部外れた VIEW が残る
    （どの値も妥当な株価指標なのでエラーは出ない）。
    """
    if ledger is not None and bps_path is not None:
        raise ValueError("ledger と bps_path は同時に渡さない（台帳は作った時点の bps_path で固まっている）")
    if ledger is None:
        ledger = build_ledger(db, bps_path=bps_path)
    if ledger is None:
        # 入力そのものが無い＝初回ブートストラップ前、またはテストのスタブ DB。ここで失敗に
        # すると空の DB からの立ち上げが通らない。**「走らなかった」の検知はここではなく
        # 収集本体が担う**（annual 行が消えていれば前段がとうに失敗している）。
        log.warning("分割補正係数: annual 行が0件のためスキップした（係数表は温存）")
        return 0

    out = ledger.factor_rows()
    stats = ledger.stats
    if not out:
        # 入力はあるのに1件も作れなかった＝検出が壊れた側。**既存の表に触らず失敗する**。
        raise RuntimeError(
            "分割補正係数が1件も作れなかった（annual 行 %d / 候補ペア %s / 検出イベント %s）。"
            "既存の係数表は温存する" % (len(ledger.rows), stats.get("n_candidate_pairs"),
                                        stats.get("n_events")))

    from database import replace_split_adjustment_factors

    n = replace_split_adjustment_factors(db, out)
    db.commit()
    log.info("分割補正係数: %d 行 / %d 社 を全置換（イベント %d 件・種別 %s・経路 %s）",
             n, len({r["edinet_code"] for r in out}), stats.get("n_events"),
             stats.get("by_kind"), stats.get("n_events_by_source"))
    return _log_split_adjustment_stats(n, stats)


def _log_split_adjustment_stats(n: int, stats: dict) -> int:
    """係数表を書いたあとの内訳を夜間ログへ出す。戻り値は書いた行数をそのまま返す。"""
    bp = stats.get("bps_path") or {}
    if bp.get("enabled"):
        # 倍率待ちの残りと公式との食い違いは**毎晩出す**（#661）。倍率待ちが減らないまま
        # 公式イベント 0 社が続くなら、catchup が残せていない（あるいは表が消えた）合図。
        cc = (bp.get("official") or {}).get("crosscheck") or {}
        log.info("分割補正係数（第2経路）: 倍率の出どころ %s・倍率待ち %d 件・"
                 "公式イベントを持つ社 %d・翌年株数と公式の突合 一致 %d / 食い違い %d",
                 bp.get("magnitude_source"), len(bp.get("awaiting_magnitude") or ()),
                 stats.get("n_official_companies", 0), cc.get("agree", 0), cc.get("disagree", 0))
        for d in cc.get("disagreements") or ():
            log.info("分割補正係数（第2経路）: 公式と食い違い %s", d)
    # 比べなかったペアは**毎晩出す**（#672）。週次の系列が収集の都合で遅く始まる社（2024-05-27 に
    # 225 社）や、取得の失敗で途中が抜けた社が欠損年をまたぐと本物の分割まで外しうるので、
    # 一覧が増えたら中身を確かめる。
    lg = stats.get("listing_gap") or {}
    log.info("分割補正係数: 上場廃止をまたぐペアとして比べなかった %d 件（翌年先読み %d 件）",
             lg.get("n_rejected", 0), lg.get("n_lagged", 0))
    for r in lg.get("rejected") or ():
        log.info("分割補正係数: 上場廃止をまたぐペア %s", r)
    # 公式の不在で外した第1経路も**毎晩出す**（#668）。外れる社は取り込み CLI を回したときにしか増えない
    # ので、取り込み後の最初の晩に一覧が変わっていなければ区間の書き込みか読み込みが壊れている。
    oa = stats.get("official_absence") or {}
    log.info("分割補正係数: 公式に分割が無いと確かめて外した第1経路 %d 件（取得記録のある社 %d）",
             oa.get("n_rejected", 0), oa.get("n_companies_with_record", 0))
    for r in oa.get("rejected") or ():
        log.info("分割補正係数: 公式に分割が無い第1経路 %s", r)
    # 保留の窓（#652）で外した公式イベントも**毎晩出す**（#739）。外したものは F の検出にも TTM の
    # 分割窓にも届かない。登録した社のイベントが catchup の取得範囲に届くまでは 0 件が正常で、届いた
    # あとに 0 件へ戻ったら、登録の窓か台帳の読み込みが壊れている。同じ社の第1経路が上の「公式に分割が
    # 無い」で外れ、その窓にこの行の日付が入っていれば、外したのはこの登録である（`detect_events` の
    # #668 の段落）。
    wh = stats.get("withheld_official") or {}
    log.info("分割補正係数: 保留の窓で外した公式イベント %d 件（登録 %d 社）",
             wh.get("n_withheld", 0), wh.get("n_registered_companies", 0))
    for r in wh.get("withheld") or ():
        log.info("分割補正係数: 保留の窓で外した公式イベント %s", r)
    return n
