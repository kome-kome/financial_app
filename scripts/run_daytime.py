"""平日日中の枠で、重い計算を**キューから窓に収まるだけ**進める（Issue #618・#707）。

## なぜ日中の枠が要るのか

2026-09-07 に `macro_beta` を手動で回したところ、**同一パネル・同一設定・同一コードなのに
発散が 0 → 344 回に増え**、収束ゲートに落ちて隔離された（`mb_20260907T015257Z`）。
9/6 の run（n_divergences=0）との差は、**その7時間の裏で重いテストを並走させたこと**しか
見当たらない。本番の推論経路には `bench_macro_beta.apply_thread_limits` に相当する
スレッド固定が無く、XLA が使うコア数は実行時の混み具合で変わる。コア数が変われば浮動小数の
加算順序が変わり、NUTS は初期のごく小さな差が軌道を分岐させるので、発散の有無まで動きうる。

つまり**「重い計算の裏で作業をしない」という運用条件が、結果の再現性に直結している**。
人が会社に居て PC を触らない平日 8:00〜16:00 は、その条件が構造的に満たされる唯一の時間帯で、
そこを専用の枠にする。

## なぜキューにするのか

曜日固定の献立表にすると「今週はこれを先にやりたい」が効かない。都度手動で登録する形は
**積み忘れても何も起きず、忘れたことに気づけない**。キューなら順番をあとから積み直せて、
残数はログと watchdog のレポートに出る。

**失敗しても先頭は取り除く。** 残すと同じ計算を毎日繰り返して先へ進まなくなる（それが
このバッチを作る動機そのもの）。失敗は Issue で起票されるので、再試行したいときは積み直す。

ただし**結論を出して失敗した**のと**結論を出す前にプロセスごと消された**のは別物で、
後者は Issue にも足跡にも現れない（#639）。in-flight マーカーがこの2つを見分け、消された
仕事だけを**1回だけ**キュー先頭へ戻す。2回目は戻さず起票して捨てる。

## 平日以外に消化したいとき（`run_daytime.ps1 -Now`）

休暇などで平日昼に PC を触れる日は、枠を1回ぶん前倒しできると消化が進む。ただし
`run_daytime.ps1` を対話ターミナルで直に叩くと、プロセスが端末の子孫になって画面を
閉じた瞬間に死ぬ（#515 と同型）。`-Now` は**登録済みタスクを `Start-ScheduledTask` で
叩く**形にしてあり、セッション0・実行上限8時間・二重起動防止（`MultipleInstances
IgnoreNew`）がそのまま効く。

**`parallel_sensitive=True` の仕事は `-Force` 無しでは起動しない。** 手動キックは人が
PC を触っている時間帯に叩かれるのが前提で、それはこのバッチが避けるために作られた条件
そのものだから。収集系（`interim` / `disclosures`）は所要が延びるだけなので素通しする。

## 窓に入らない仕事は積ませない

窓は 8時間（480分・1日ぶんの予算 445分）。実測は macro_beta 380〜419分・M-3 探索
306〜369分・M-2 探索 176〜179分でいずれも収まるが、**M-1 探索は 752分で入らない**（ADR-0046 で専用タスクへ出したまま）。
`JOBS` に無い名前と、予算が窓を超える仕事は `enqueue` の時点で弾く。

## 1回の実走で何件取り出すか（#707・ADR-0060）

**1日1件ではなく、窓に収まるだけ取り出して順に回す。** 守りたいのは「重い計算の裏で別の
作業を並走させない」ことであって、件数ではない——同じ窓の中で**順番に**回すのは並走ではない。

1日1件だった頃、先頭が `gate:macro`(7分) / `gate:ttm`(30分) / `gate:demean`(7分) と並んだ
2026-09-20 のキューは、合計44分の仕事のために 445分の窓を3日ぶん食い潰していた。しかも
この3つは**同じデータ世代で並べてから判断する**設計（#615・#424）なので、別々の日に出ても
揃うまで判断できない。

件数の決め方は `select_jobs` を参照。要点は2つ:

- **予算は窓から導く**（`JOB_BUDGET_MIN / 件数` の等分）。`measured_min` は**どれだけ取り出すかの
  計画にだけ**使い、打ち切りの閾値にはしない——パネルは毎晩伸びるので、所要から逆算した予算は
  必ず陳腐化する
- **先頭の1件は無条件**。これが無いと `bench:rhat-scale`（実測440分）が自分で自分を弾く。
  先頭が窓に入ることは `enqueue` の検査（`measured_min <= JOB_BUDGET_MIN`）が保証している

件数の上限定数は置かない。窓からも約束からも導けない恣意的な数になるため（上限の役は
`JOB_HEADROOM` と等分が果たす）。

## 暦（#681・ADR-0056）

キューは「積み忘れても何も起きない」を解くために作ったが、**積むのが人である限り、同じ穴は
キューの手前に残る**。H1（半期）と会社予想の収集は手で積んだときにしか走らず、次の提出の波を
逃しても失敗として現れなかった。日付で決まる仕事は暦（`SCHEDULE`）が積む。

- **毎月 `day` 日以降の最初の実走で、キューの先頭へ1回だけ積む。** 先頭なので待ちに上限があり、
  watchdog の閾値（`batch_freshness.PRODUCERS`）を約束から導ける。末尾だと待ちに上限が無い
- **今月ぶんが既に入っていれば積まない**（手で回した月に二重に回さない）。判定は成果物の
  `created_at` で行い、watchdog と同じ読み手（`Scheduled.produced`）を使う
- **月次系のバッチと時間が重なる日は、並走に敏感な仕事を取り出さない。** 月次（1日）・
  マクロ・ベータ（2日）・M-1 探索（3日）は 01:00 起動・16時間の窓で、8:00 からの日中枠と
  重なる。並走は所要ではなく結論を変える（#618）。重なる日は `run_monthly*.TRIGGER_*` と
  `WINDOW_MIN` から導く（書き写さない）。その日は敏感でない仕事（収集）だけを探して回す
- **祝日と年末年始も、並走に敏感な仕事を取り出さない**（#684）。トリガは月〜金の固定で
  祝日を知らず、「人が会社に居て PC を触らない」という前提がその日だけ崩れる。
  祝日は表（`HOLIDAYS`）で持ち、表が今日の年を持たなければ見送らずにそう出す。
  `-Now -Force`（叩いたら触らないという約束）だけが、当日限りの解除印で祝日の見送りを外せる。
  月次の重なりは人の有無と関係が無いので外せない

実行:
    python -m scripts.run_daytime                       # キュー先頭から窓に収まるだけ
    python -m scripts.run_daytime --dry-run             # 実行計画だけ
    python -m scripts.run_daytime --queue               # キューの中身を見る
    python -m scripts.run_daytime --peek                # 次の1件を JSON で（キューは減らさない）
    python -m scripts.run_daytime --enqueue beta        # 末尾へ積む
    python -m scripts.run_daytime --enqueue beta,tune:macro_gbdt
    python -m scripts.run_daytime --clear-queue         # 空にする
    python -m scripts.run_daytime --allow-holiday       # 今日だけ祝日の見送りを外す（-Now -Force が使う）

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from scripts import batch_common as bc
from scripts import run_monthly, run_monthly_beta, run_monthly_m1
from scripts.batch_common import LOG_DIR, ROOT, Runner, Step  # noqa: F401 （既存 import 互換）

KEY_LAST_RUN = "daytime_last_run"
KEY_LAST_SUCCESS = "daytime_last_success"
KEY_QUEUE = "daytime_queue"

# 取り出したが結論を出していない仕事の印（#639）。値は
# `{"job": ..., "started_at": ..., "requeued": 0}`。詳細は `reclaim_inflight` を参照。
KEY_INFLIGHT = "daytime_inflight"

# 中断で消えた仕事をキューへ戻す回数の上限。**1回だけ**——2回続けて消えるのは環境側の
# 問題で、戻し続けると `pop_queue` の docstring が警告している「毎日同じ計算を繰り返して
# 先へ進まない」状態そのものになる。
MAX_REQUEUE = 1

ISSUE_LABELS = bc.ISSUE_LABELS

# タスクスケジューラの窓（`install_daytime_task.ps1` の既定 `-Hours 8`）。
# 8:00 起動で 16:00 まで。**この値と下の予算はセットでしか意味を持たない**ので
# `tests/test_run_daytime.py` が ps1 側の既定と突き合わせる。
#
# 17:20 の夜間バッチまで 80分空ける。窓を 9時間に広げると余裕が 20分になり、日中枠が
# 長引いた日に夜間とメモリを取り合う——それは 2026-09-07 に macro_beta の発散を
# 0 → 344 回へ増やした条件そのものなので、広げない。
WINDOW_MIN = 8 * 60

# 起動時刻（`install_daytime_task.ps1` の既定 `-Time 08:00`）。月次と重なる日の導出に使う
# （#681）。`tests/test_run_daytime.py` が ps1 側の既定と突き合わせる。
TRIGGER_TIME = "08:00"

# 平日トリガの正常な最長間隔（金 -> 月の 72時間）。`batch_freshness.WATCHED` と暦の
# producer（`SCHEDULE_CADENCE_H`）が共有する。
CADENCE_H = 72.0

# 暦と「今日」は JST で数える（トリガが JST の 8:00 なので）。tzdata に依存しない固定オフセット。
JST = timezone(timedelta(hours=9))

# 窓からマージンと deps_smoke を引いた、**1回の実走ぶん**の上限（ADR-0040）。
# **実測から逆算した値ではない**——パネルは毎晩伸びるので所要は据え置かず伸びる。
# 複数件を取り出す日は、この値を件数で等分して各件の予算にする（#707・`budget_share`）。
DEPS_SMOKE_MIN = 5
MARGIN_MIN = 30
JOB_BUDGET_MIN = WINDOW_MIN - DEPS_SMOKE_MIN - MARGIN_MIN   # = 445

# 2件目以降を足すかを判断するときの余裕（#707・ADR-0060）。「**所要が倍に伸びても
# 打ち切られない**」という約束であって、実測から逆算した値ではない。
#
# ここだけが `measured_min` を読む——読むのは「何件取り出すか」の計画のためで、
# 予算（打ち切りの閾値）は常に窓の等分から導く。所要比で按分すると、伸びた仕事ほど
# 予算も勝手に増える＝「実測から逆算しない」という ADR-0040 の方針が崩れる。
JOB_HEADROOM = 2.0


@dataclass(frozen=True)
class Job:
    """日中枠で回せる仕事1つ。`argv` は `{python}` を実行中の実行ファイルで置換する。"""
    name: str
    argv: tuple[str, ...]
    why: str
    measured_min: float          # 直近の実測所要（分）。窓に入るかの判断材料

    # **裏で作業されると結果そのものが変わるか。** True は「所要が延びる」ではなく
    # 「同じ入力から違う答えが出る」を意味する（#618・macro_beta で発散が 0 → 344 回）。
    # 平日8時の自動枠はどちらでも同じだが、`-Now` の手動キックはここで分岐する——
    # 人が PC を触っている時間帯に叩かれるのが手動キックの前提だから。
    #
    # **既定値を置かない。** 置くと新しい仕事を足したときに黙って非敏感側へ倒れ、
    # 忘れたことが失敗として現れない（CLAUDE.md「増やしたら登録表へ1行足す」と同型）。
    parallel_sensitive: bool

    needs_deps_smoke: bool = False


JOBS: dict[str, Job] = {
    "beta": Job(
        name="macro_beta",
        argv=("{python}", "macro_beta_inference.py",
              "--draws", "800", "--tune", "800", "--target-accept", "0.95",
              "--chains", "2", "--r-hat-threshold", "1.05",
              "--nuts-sampler", "numpyro", "--init", "adapt_diag",
              "--max-tree-depth", "8,10"),
        why="M-1 の入力 macro_beta_loadings（PyMC/NUTS 階層マクロ・ベータ）。"
            "**引数は scripts/run_monthly_beta.py と同一**（片方だけ動かすと、"
            "同じ名前の別物を測ることになる）。`--force` は渡さない＝通常のゲート判定。",
        measured_min=379.7,      # 2026-09-11 日中枠の実測（並走なし・ゲート通過）。9/7 は並走ありで 419.2分・隔離
        parallel_sensitive=True,  # 発散 0 → 344 の実測そのもの
        needs_deps_smoke=True,
    ),
    "tune:macro_gbdt": Job(
        name="tune:macro_gbdt",
        argv=("{python}", "hyperparameter_search.py", "--model", "macro_gbdt",
              "--strategy", "random", "--n-iter", "150",
              "--objective", "rank_ic", "--persist", "--persist-scores", "--seed", "0"),
        why="M-2 の探索。9/1 の月次では 176.3分で150件を完走したが品質ゲートで persist を"
            "スキップした（#590）。**2026-09-08 の日中枠で 179.4分・151件を完走し persist まで"
            "到達した**——ADR-0047 の同一パネル比較で champion 0.1442 に対し新 0.2268。"
            "`plugin_tuned_params` が50日固着していた穴（#504 の producer 監視が捉えた分）は"
            "これで埋まった。**引数は run_monthly.py と同一**。",
        measured_min=179.4,      # 2026-09-08 ローカル実測（9/1 月次は 176.3分）
        # 探索は CV を回して rank-IC の大小で候補を選ぶ。数値のわずかな揺れが順位を
        # 入れ替えれば、**永続化される重みが変わる**（所要ではなく結論が変わる）。
        parallel_sensitive=True,
    ),
    # ── 昇格ゲートの実測（#615）────────────────────────────────────────────
    # M-1 のマクロ特徴量は共通域で rank-IC を −0.0920 下げている（#604 の実測）。
    # `use_macro` は主効果と交差項を**同時に**動かすので、どちらが効いているのかを
    # 分ける軸を #615 で足した。**この実測は対話セッション中に回してはいけない**——
    # 並走すると結果そのものが変わる（#618・macro_beta で発散が 0 → 344 回）。
    #
    # **入力は毎回ローカル DB から作り直す（#674）。** 9/7 に積んだ時点では週次株価の
    # キャッシュがあったが、待っている間の 9/8 に #620 の修復で `_stale_pre620/` へ退避され、
    # 9/14 に順番が来たジョブは `--allow-full-pull` が無く 0.1分で exit=1 になった（1日ぶんの
    # 枠が消え、キューにも戻らない）。`--refresh-cache` も要る——キャッシュは世代の印を
    # 持たず、財務（8/31）とマクロ（9/3）が #655 の分割補正より前のまま黙って返る。
    # 「97万行の pull はストールしやすい」は Supabase 時代の理由で、正本がローカルに
    # 移った（#503）いまは当てはまらない。
    "gate:interactions": Job(
        name="gate_interactions",
        argv=("{python}", "-m", "scripts.momentum_gate", "--interactions", "--stride", "1",
              "--allow-full-pull", "--refresh-cache"),
        why="交互作用（財務 × マクロの交差項）の有無を共通 (ym,ec) 域で測る（#615）。"
            "スモーク（stride=5）では nointer +0.2603 に対し inter +0.0989 で "
            "diff=-0.1615（95%CI[-0.2719,-0.0467]）と出たが、**--smoke の共通域は "
            "間引きで壊れるので判定には使わない**（ADR-0050）。これは stride=1 の本測定。",
        # 2026-09-15 日中枠の実測（並走なし・`--refresh-cache` のキャッシュ再取得込み）。
        # 見積りの 300分は「サンプル5倍でパネル構築も CV も伸びる」と置いた値だったが、
        # 実際の CV は1条件あたり約45秒（ログの `rank-IC=... (43.5s)` / `(44.6s)`）で済んだ。
        measured_min=6.9,
        # 昇格ゲートの実測。差が −0.1615 か −0.16 かではなく「符号と CI が 0 をまたぐか」で
        # 採否が決まるので、並走で揺れた値を根拠に採否を決めると判断ごと誤る。
        parallel_sensitive=True,
    ),
    # 上の本測定（2026-09-15）で交差項の差は有意でなかった（rank-IC diff=-0.0696
    # 95%CI[-0.1556,+0.0137]）。代わりに**選ばれた列**が仮説の前提を崩した——交差項なしでも
    # 20列中19列がマクロ主効果で、財務は pbr の1列だけ（#604 のマクロなしは財務4列）。
    # 「交差項が上限を食い尽くした」より「マクロ主効果が上限20を占めて財務列を締め出した」が
    # 有力なので、#615 のコメントで決めた次の手順どおり列数上限を振る（ADR-0050 の 9/15 追記）。
    "gate:max-features": Job(
        name="gate_max_features",
        argv=("{python}", "-m", "scripts.momentum_gate", "--max-features", "5,10,20,30,40",
              "--stride", "1", "--allow-full-pull", "--refresh-cache"),
        why="BIC の列数上限（max_features）を 5/10/20/30/40 で振り、本番値 20 との差を共通域で"
            "測る（#615）。交互作用の本測定では差が有意でなく、選ばれた列はマクロ主効果が"
            "20列中19列を占めていた。**上限を上げると財務の列が戻り rank-IC が回復するか**を見る。"
            "値は ADR-0050 の 2026-09-07 追記に書いたもの。",
        # 実測 20.1分（2026-09-18・`--refresh-cache` 込み・並走なし）。見積りは 30分だった。
        # 前半約10分はキャッシュの取り直しとパネル構築、CV は1条件 15〜86秒（列数に比例）。
        measured_min=20.1,
        parallel_sensitive=True,   # gate:interactions と同じ理由（採否が CI の符号で決まる）
    ),
    # #604（2026-09-06）の `use_macro` の測定は**分割補正（#655・#656）より前のデータ**だった。
    # 補正が消したリーク（ADR-0055 決定7）は per/pbr＝財務列を通って入っていたので、財務だけの
    # `nomacro`（+0.2254）の優位は目減りしている可能性がある。`gate:max-features` と同じ
    # データ世代で並べてから #615 の既定を決める（#684）。
    "gate:macro": Job(
        name="gate_macro",
        argv=("{python}", "-m", "scripts.momentum_gate", "--macro",
              "--stride", "1", "--allow-full-pull", "--refresh-cache"),
        why="マクロ特徴量の有無（use_macro）を共通 (ym,ec) 域で測り直す（#615・#604 の再測定）。"
            "9/6 の本測定は分割補正（#655/#656）より前のデータで、補正が消したリークは財務列を"
            "通っていた。**列数上限の本測定と同じデータ世代で並べる**ために今のデータで測る。",
        # 2026-09-20 の実測（日曜に前倒しで消化・並走なし）。見積り 7.0 から差し替えた。
        measured_min=5.4,
        parallel_sensitive=True,   # gate:interactions と同じ理由（採否が CI の符号で決まる）
    ),
    # TTM 行（#424 子2）を学習パネルへ入れるかの昇格ゲート（子3・ADR-0051 決定9）。既定は
    # 通期のみのままで、切り替えるのはこのゲートを通ってから。
    "gate:ttm": Job(
        name="gate_ttm",
        argv=("{python}", "-m", "scripts.momentum_gate", "--fin-rows",
              "--stride", "1", "--allow-full-pull", "--refresh-cache"),
        why="学習パネルの行の基準（通期のみ / 通期＋TTM）を共通 (ym,ec) 域で比べる"
            "（#424 子3・ADR-0051 決定9）。対象は M-2 / M-6・4検定・alpha 0.0125。"
            "TTM 行が0件、または断面の特徴量が1行も変わらないときは非0で止まる"
            "（黙って同じものを比べると「差なし」だけが残る）。",
        # 2026-09-20 の実測（日曜に前倒しで消化・並走なし）。見積り 30.0 から差し替えた。
        # 見積りが 2.3倍に外れたのは、`--refresh-cache` のパネル構築を2条件ぶん数えていたため。
        # 実際は `financial_metrics_with_ttm` の取り直し（79.5MB）が増えるだけで、CV は 5.5分。
        measured_min=12.8,
        parallel_sensitive=True,   # 他の gate と同じ理由（採否が CI の符号で決まる）
    ),
    # 9/18 の列数上限の本測定で「上限が財務の列を締め出している」は否定された（上限 40 でも財務は
    # 2列）。残った見立ては「M-1 の目的変数は素の52週先リターンなので、BIC（二乗誤差）は相場全体の
    # 変動を説明するマクロ列を選ぶ。評価は月内の順位」＝学習と評価のずれ（ADR-0050 の 9/18 追記）。
    # 当初は `gate:macro` の結果を見てから足すとしていたが、どちらの結果でも意味を持つ測定なので
    # 前倒しした（ADR-0050 の 9/19 追記）。
    "gate:demean": Job(
        name="gate_demean",
        argv=("{python}", "-m", "scripts.momentum_gate", "--demean-target",
              "--stride", "1", "--allow-full-pull", "--refresh-cache"),
        why="M-1 の目的変数から月ごとの全銘柄平均を引いた条件（demean）と素の条件（raw・本番）を"
            "共通 (ym,ec) 域で比べる（#615）。**選ばれた列の構成がマクロ主効果から離れ、rank-IC が"
            "上がるか**を見る。変換は BIC 選択の前に掛かり、届かなければ非0で止まる。",
        # 2026-09-20 の実測（日曜に前倒しで消化・並走なし）。見積り 7.0 から差し替えた。
        measured_min=8.1,
        parallel_sensitive=True,   # 他の gate と同じ理由（採否が CI の符号で決まる）
    ),

    # ── リスク軸（#709・ADR-0050 の 2026-09-21 追記）─────────────────────────
    # ここまでの gate は**すべて μ̂ の順位だけ**を測ってきたが、M-1 が画面へ出すのは
    # `U = μ − λR` の並びで、R は測定の外にあった。#615 でマクロは μ 側では月内の順位を
    # 動かせないと確定した——その機構（月末の週の値は同じ月の全銘柄でほぼ同一）は
    # **リスク軸には当たらない**（`r_macro` の β は per-stock）。
    #
    # 3条件（mu_only / r2 / r_macro）は `Cond` が同一なので、パネルと CV は1回しか回らない
    # ＝所要は `gate:demean` の2条件ぶんとほぼ同じで、増えるのは R の算出と変換だけ。
    "gate:risk-axis": Job(
        name="gate_risk_axis",
        argv=("{python}", "-m", "scripts.momentum_gate", "--risk-axis",
              "--stride", "1", "--allow-full-pull", "--refresh-cache"),
        why="M-1 の U = μ − λR のリスク軸を振る（#709）。mu_only（リスクを引かない）/ r2"
            "（実現ボラ・本番既定＝分母）/ r_macro（マクロ起因リスク）を同一パネル・同一 CV で"
            "比べる。**r_macro は時点不変の 2026年スナップショット＝未来情報を含む上限**なので、"
            "負ければ採用しないと結論でき、勝っても既定を変える根拠にはならない。",
        # **未実測**。`gate:demean`（2条件・8.1分）を土台に、3条件ぶんの R 算出（`_realized_vol`
        # を 73ヶ月 × 約3,558社ぶん）と変換を足して置いた。初回の実走で差し替える。
        measured_min=15.0,
        parallel_sensitive=True,   # 他の gate と同じ理由（採否が CI の符号で決まる）
    ),

    # ── 最新業績の供給（#424 の子タスク1・ADR-0051）────────────────────────
    # #503 で GHA cron を止めて以降、H1 と会社予想は**どこからも収集されていない**
    # （呼び出し元が `collect-interim.yml` / `collect-disclosures.yml` の手動トリガだけ）。
    # 実測 2026-09-07: H1 は period_end MAX 2025-09-30（11.2ヶ月）・会社予想は
    # disc_date MAX 2026-04-17（4.7ヶ月）。**月次本体の空きは 67分しかなく入らない**
    # （GHA 実測 2h31m）ので、日中枠（予算445分）で回す。
    #
    # **2026-09-08 の初実走（ローカル 62.0分）は新規0件だった**——候補 9657件の内訳が
    # 既収集 5736 + Q2以外 3905 + ZIP 失敗 16 で、収穫が1件も無い。当時は「3月期の H1 は
    # 11月提出なので、それより前に積んでも取るものが無い」と読んだが**誤りだった**。真因は
    # #647 で、新様式の半期報告書は DEI の当期種別を `HY` と名乗るのに `Q2` だけを H1 と
    # みなし、3905件を捨てていた。修正後の 2026-09-16 の実走は saved=3967・failed=0 で、
    # H1 の `year=2026` は 298 → 3875行、`max(period_end)` は 2026-07-31 まで進んだ。
    # **積むのは暦（`SCHEDULE`・毎月16日以降）で、手では積まない**（#681）。
    #
    # ZIP 失敗 16件は #630 で決着した。**CSV 形式を持たない書類**（`csvFlag='0'`＝外国会社等の
    # HTML のみ提出）で、EDINET は `type=5` に HTTP 200 + JSON を返すため `BadZipFile` に化けていた。
    # 待っても現れない恒久的失敗なので候補選別の手前で外す（9/16 の実走で `CSV 無しで除外 17件`）。
    "interim": Job(
        name="collect_interim",
        argv=("{python}", "collector.py", "--interim", "--years", "2"),
        why="半期(H1)財務の差分収集（EDINET 半期報告書・旧四半期Q2）。`skip_existing=True` で"
            "収集済み doc_id は再取得しない＝冪等。`--years 2` は 2025-10 以降の欠落を埋める幅で、"
            "GHA の既定 6 年は初回バックフィル用の値。",
        # 2026-09-16 ローカル実測（#647 修正後の初実走・3971件を取得して 3967件を保存）。
        # 9/8 の 62.0分は同じ件数を取得して捨てていた回で、所要はほぼ変わらない。
        measured_min=64.6,
        # 収集は EDINET の応答待ちが所要の大半で、CPU の取り合いは所要を延ばすだけ。
        # 取得した XBRL の中身は裏で何が動いていても同じ＝結論は変わらない。
        parallel_sensitive=False,
    ),
    "disclosures": Job(
        name="collect_disclosures",
        argv=("{python}", "collector.py", "--disclosures"),
        why="会社予想（決算短信サマリー）の差分収集（J-Quants /fins/summary）。"
            "`statement_disclosure` の最終 disc_date から今日までを日付単位で埋める。"
            "ADR-0051 の案C（サプライズ特徴量）を将来採るなら入力になる。",
        # 2026-09-07 の見積り: 最終 disc_date 2026-04-17 から 143暦日 ×
        # `JQUANTS_RATE_SLEEP`(20秒) = 47.7分が**上限**（非営業日は HTTP 400 で即返り
        # sleep も払わないので実際は短い）。60 は余裕込み。**実走で差し替える。**
        # 2026-09-08 実測 14.1分（43日・4052件）。上の見積りは最終 disc_date が
        # 4.7ヶ月前だった初回ぶんで、以後は毎回この程度に収まる。
        measured_min=14.1,
        # 積むのは暦（`SCHEDULE`・毎月1日以降）。読む消費者はまだ無いが、無料プランは2年より
        # 古い日を返さないので、止めた期間はあとから埋められない（#681）。
        parallel_sensitive=False,   # interim と同じ理由（J-Quants の応答待ちが所要の大半）
    ),
    "tune:macro_dlm": Job(
        name="tune:macro_dlm",
        argv=("{python}", "hyperparameter_search.py", "--model", "macro_dlm",
              "--strategy", "grid",
              "--objective", "rank_ic", "--persist", "--persist-scores", "--seed", "0"),
        why="M-3 の探索。実測 1.04〜1.26分/件 × 294件 ＝ 306〜369分。9/1 の月次では"
            "250分の予算で 199/294 まで進んで打ち切られ、当時は完走しないと何も残らなかった"
            "（#638・ADR-0054 で締切の手前から畳んで永続化するようになった）。",
        measured_min=369.0,
        parallel_sensitive=True,   # tune:macro_gbdt と同じ理由（順位が入れ替わると重みが変わる）
    ),

    # ── 分割補正の第2経路が学習へ与える影響の実測（#656・ADR-0055 決定7）──────
    # 係数表を bps_path=False / True で往復させ、同じ手続きで OOF rank-IC を2回測る。
    # **1プロセスで前後を回すのは、補正前の断面がもう DB に残っていないから**——補正は
    # VIEW が係数表を LEFT JOIN して当てているので、見るには作り直すしかない。
    #
    # **#659 で既定が True へ倒れたのでキューへ積んだ**（2026-09-12）。倍率を `bs_bps` の
    # 年次比ではなく翌年の `issued_shares` 比から取るようにして、公式 `AdjFactor` との
    # 一致率が 0.367 -> 0.962 になった（`corporate_actions.DEFAULT_BPS_PATH`）。
    # それまで積まなかったのは、**本番に入っていない設定の rank-IC** を 3 時間かけて
    # 測ることになるからである。
    "oof:split-bias": Job(
        name="oof_split_bias",
        argv=("{python}", "-m", "scripts.measure_split_bias_oof",
              "--models", "macro_gbdt,macro_enet"),
        why="第2経路（#656）を入れる前後の OOF rank-IC。M-1 は strict（macro_nan_ok=False）で"
            "パネルを M-2/M-6 と共有できず同一共通域の比較が成立しないため対象外"
            "（ADR-0045/ADR-0050 と同じ制約）。**差の符号は採否の条件にしない**"
            "（ADR-0055 決定7。歪みは未来情報のリークでありうるので補正で下がるのが正しい）。",
        # 2026-09-17 日中枠の実測（並走なし）。見積りの 180分は「上流の walk-forward 学習を
        # 2回払う」と置いた値だったが、モデル比較は1回あたり約4分（`[before] モデル比較 完了
        # (256.7秒)` / `[after] ... (245.9秒)`）、係数表の全置換は各4秒前後で済んだ。
        measured_min=8.6,
        # 測った rank-IC そのものが成果物なので、並走で揺れた値を根拠に読むと判断ごと誤る
        # （#618・macro_beta で発散が 0 → 344 回）。
        parallel_sensitive=True,
    ),

    # ── 収束ゲートの余裕の規模依存（#664・ADR-0002 #612 節）────────────────────
    # #609 の3案（alpha だけ別閾値／MCSE／p95）は「銘柄数が増えると p99 の余裕が縮む」を
    # 前提にしているが、健全な run は同一規模（3,837銘柄）の2点しかなく一度も測られていない。
    # 合成パネル（DB を触らない）で銘柄数 × sampler seed を振る。**サンプリング設定は `beta` と
    # 同一**（`tests/test_run_daytime.py` が照合する）——違えば本番のゲートの話にならない。
    # 1セル1プロセスで JSONL へ追記し、締切の手前で畳み、`--resume` で済んだセルを飛ばすので、
    # **終わらなければ積み直せば続きから回る**（全セル済みなら即 exit 0）。
    "bench:rhat-scale": Job(
        name="bench_rhat_scale",
        argv=("{python}", "-m", "scripts.grid_macro_beta", "--mode", "synth",
              "--n-stock", "250", "500", "1000", "2000",
              "--seed", "0", "1", "2", "--panel-seed", "0",
              "--chains", "2", "--tune", "800", "--draws", "800",
              "--depths", "8,10", "--target-accepts", "0.95",
              "--nuts-sampler", "numpyro", "--init", "adapt_diag",
              # 合成 250銘柄・md 8,10 の実測（`.logs/bench_609_gate.jsonl`・draws 400 で 233.2秒 /
              # 1,226,400歩）。見積りは並べ替えと締切判定にだけ使い、実測で較正される。
              "--us-per-step", "190.2",
              "--resume", "--view", "scale", "--out", ".logs/bench_664_scale.jsonl"),
        why="収束ゲート（変数別 r_hat p99）の余裕が銘柄数とともに縮むかを測る（#664）。"
            "合成パネルで 250/500/1000/2000 銘柄 × seed 3つ。#609 の3案を選ぶ前提の実測。"
            "表は `python -m scripts.bench_macro_beta_report --view scale "
            "--inputs .logs/bench_664_scale.jsonl`。",
        # **実走で裏づけた＝差し替えない**（#664・ADR-0002 の #664 節・決定5）。2026-09-21 の
        # 初回は 257.7分・9/12セルで終わったが、これは締切の手前で畳んだ値＝**窓とセル境界の
        # 位置を測った値**であって仕事の所要ではない（残り3セルで約600分ある）。440 は「1回で
        # 窓を使い切る」意図の表現で、実走は 257.7分使って次のセル（約203分）が入らず停止した
        # ＝意図どおり。**締切で畳む仕事の END 時刻は所要を名乗っていない**——値はセル境界が
        # どこに落ちたかで毎回動くので、CLAUDE.md の「`measured_min` は実測へ差し替える」をここへ
        # 当てると値が仕事と無関係に彷徨い、`--peek` が窓の使用量を過小に見せる。222.5分
        # （= `JOB_BUDGET_MIN / 2`）を下回った回で差し替えると、窓を使い切る仕事に2件目が付く。
        measured_min=440.0,
        # MCMC そのもの。並走で発散が 0 → 344 回に増えた実測がある（#618）。
        parallel_sensitive=True,
        needs_deps_smoke=True,     # beta と同じ依存（pymc / numpyro / jax）
    ),

    # ── 静的プリセットの重みの walk-forward 推定（#625・#546 の前提1・ADR-0059）──
    # 推定は大小順（プリセットの性格）を保ったまま rank-IC を直接高め、embargo 12か月を
    # 空けた OOF の rank-IC を同じ月の静的重みと対にして検定する。`PRESETS` は変えない
    # （反映は #546 が補正後 α を通ったものだけ行う）。パネルは毎晩伸びるので、#546 の判断の
    # 直前に回し直す用に置く。
    "wf:preset-weights": Job(
        name="preset_weight_walkforward",
        argv=("{python}", "-m", "scripts.preset_weight_walkforward", "--json", "--weights-out"),
        why="静的4プリセットの重みを walk-forward で推定し、OOF で静的と比べる（#625・ADR-0059）。"
            "昇格の根拠は OOF の対比較だけで、最終重みを preset_ic_gate で測るのは in-sample。",
        # 2026-09-19 ローカル実測（夜間バッチ終了後・並走なし・時点再現の gap 込みのパネル構築）。
        measured_min=1.9,
        # 閉形式のモーメント・SLSQP・seed 固定のブートストラップ＝同じ入力から同じ答えが出る。
        # 裏で何が動いていても所要が延びるだけで、結論は変わらない。
        parallel_sensitive=False,
    ),
}

SPEC = bc.BatchSpec(
    name="日中バッチ",
    log_prefix="daytime",
    key_run=KEY_LAST_RUN,
    key_success=KEY_LAST_SUCCESS,
    job_label="daytime-local",
    issue_title="[ops] ローカル日中バッチ失敗: {failed}",
    headline="ローカル日中バッチ（`scripts/run_daytime.py`）でステップが失敗した。",
)


# ── キュー（app_settings に JSON 配列で持つ）──────────────────────────────────

def _session():
    from database import SessionLocal
    return SessionLocal()


def read_queue(db=None) -> list[str]:
    """キューの中身。壊れた値は空として扱う（**例外にしない**＝バッチが起動不能になる）。"""
    from database import get_setting

    own = db is None
    db = db or _session()
    try:
        raw = get_setting(db, KEY_QUEUE)
    finally:
        if own:
            db.close()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in items if isinstance(x, str)] if isinstance(items, list) else []


def write_queue(items: Sequence[str], db=None) -> None:
    from database import upsert_setting

    own = db is None
    db = db or _session()
    try:
        upsert_setting(db, KEY_QUEUE, json.dumps(list(items), ensure_ascii=False))
    finally:
        if own:
            db.close()


def enqueue(names: Sequence[str], db=None) -> list[str]:
    """末尾へ積む。**未知の名前と窓に入らない仕事はここで弾く**（走ってから気づかない）。"""
    for n in names:
        job = JOBS.get(n)
        if job is None:
            raise SystemExit(
                f"未知の仕事 {n!r}。積めるのは {sorted(JOBS)} のいずれか")
        if job.measured_min > JOB_BUDGET_MIN:
            raise SystemExit(
                f"{n!r} は実測 {job.measured_min:.0f}分で日中枠の予算 {JOB_BUDGET_MIN}分に入らない。"
                "専用タスク（夜間の窓16時間）で回すこと")
    items = read_queue(db) + list(names)
    write_queue(items, db)
    return items


def pop_queue(db=None) -> Optional[str]:
    """先頭を取り出して**取り除いてから**返す。

    **失敗しても戻さない。** 戻すと同じ計算を毎日繰り返して先へ進まなくなる——この
    バッチを作った動機がまさにそれで、失敗は Issue に残るので再試行は積み直しで行う。

    戻すのは `reclaim_inflight` が扱う**中断**（結論を出す前にプロセスごと消えた場合）だけで、
    それも1回に限る。ここでの「失敗」＝ exit≠0・品質ゲート・予算打ち切りは対象外。
    """
    items = read_queue(db)
    if not items:
        return None
    head, rest = items[0], items[1:]
    write_queue(rest, db)
    return head


# ── in-flight マーカー（#639）────────────────────────────────────────────────
#
# `pop_queue` は「失敗しても戻さない」。この判断は正しいが、**結論を出して失敗した**のと
# **結論を出す前にプロセスごと消された**のを同一視していた。前者は Issue に残るので人が
# 判断できる。後者は何も残らない——2026-09-09 に Windows Update の再起動が
# `tune:macro_dlm` を 285分（255/294件）で殺し、285分の計算とキューの1件が同時に消えた。
# `daytime_last_run` は閾値の内側だったので watchdog も起票しなかった。
#
# 2つを見分ける印がこのマーカー。pop の直後に書き、**Python が生きていれば finally で必ず
# 消える**。OS ごと消されたときだけ残るので、残っていること自体が「中断された」証拠になる。
#
# マーカーが取り戻すのは**キューの1件だけ**。285分の計算そのものは #638・ADR-0054 の
# 逐次永続化が拾う（暫定ベストのパラメータは残る。producer スコアは残らない）。

_STATE_RUNNING = "running"    # pop して実行中。残っていたら中断された
_STATE_QUEUED = "queued"      # 中断されてキューへ戻した。次に pop されるのを待っている


def read_inflight(db=None) -> Optional[dict]:
    """マーカーの中身。**壊れた値は無いものとして扱う**（`read_queue` と同じ方針）。

    ここで例外にすると、値が1つ壊れただけでバッチが起動不能になる——「走らなかったことを
    検知する」ための仕組みが、それ自体を起こしてしまう。
    """
    from database import get_setting

    own = db is None
    db = db or _session()
    try:
        raw = get_setting(db, KEY_INFLIGHT)
    finally:
        if own:
            db.close()
    if not raw:
        return None
    try:
        mark = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return mark if isinstance(mark, dict) else None


def inflight_jobs(mark: Optional[dict]) -> list[str]:
    """マーカーが持つ**まだ結論を出していない**仕事の並び（#707）。

    `jobs` が無い古い形式（`{"job": "..."}`）は1件として読む——マーカーは DB に残りうるので、
    複数件へ広げた回のデプロイで前夜の中断を取り落とさない。壊れた値は無いものとして扱う。
    """
    if not mark:
        return []
    jobs = mark.get("jobs")
    if isinstance(jobs, list):
        return [j for j in jobs if isinstance(j, str)]
    job = mark.get("job")
    return [job] if isinstance(job, str) and job else []


def write_inflight(jobs: Sequence[str], state: str, requeued: int, db=None) -> None:
    from database import upsert_setting

    jobs = list(jobs)
    payload = {
        "jobs": jobs,
        # 古い読み手（`--queue` の表示・起票の文面）のために先頭も残す。
        "job": jobs[0] if jobs else "",
        "state": state,
        "requeued": int(requeued),
        "at": bc.utc_now_iso(),
    }
    own = db is None
    db = db or _session()
    try:
        upsert_setting(db, KEY_INFLIGHT, json.dumps(payload, ensure_ascii=False))
    finally:
        if own:
            db.close()


def clear_inflight(db=None) -> None:
    from database import upsert_setting

    own = db is None
    db = db or _session()
    try:
        upsert_setting(db, KEY_INFLIGHT, "")
    finally:
        if own:
            db.close()


def notify_interrupted(jobs: Sequence[str], mark: dict, run=subprocess.run) -> Optional[str]:
    """戻す上限に達した仕事を起票する。**gh が無くても落とさない**（`bc.notify` と同じ）。"""
    jobs = list(jobs)
    job = jobs[0] if jobs else "不明"
    shown = ", ".join(f"`{j}`" for j in jobs) or "`不明`"
    body = "\n".join([
        f"日中バッチが {shown} を **{MAX_REQUEUE + 1} 回続けて、結論を出す前に**失っている。",
        "",
        "| 項目 | 値 |",
        "|---|---|",
        f"| 仕事 | {shown} |",
        f"| 最後に取り出した時刻 | {mark.get('at', '不明')} |",
        f"| キューへ戻した回数 | {mark.get('requeued', 0)} |",
        "",
        "1回目の中断はキュー先頭へ自動で戻すが、2回目は戻さず捨てる（#639）。"
        "戻し続けると毎日同じ計算を繰り返して先へ進まなくなるため。",
        "",
        "### 確認すること",
        "",
        "1. `.logs/daytime_*.log` の末尾に `END` 行があるか"
        "（無ければプロセスごと消えている＝バッチの失敗ではない）",
        "2. System イベントログの Kernel-Power 109 / Windows Update の再起動",
        "3. Windows Update のアクティブ時間"
        "（`HKLM\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings`）が窓を覆っているか",
        "4. 原因が解消したら `run_daytime.ps1 -Enqueue " + ",".join(jobs) + "` で積み直す",
        "",
        "---",
        "この Issue は `scripts/run_daytime.py` による自動起票（#639）。",
    ])
    argv = ["gh", "issue", "create",
            "--title", f"[ops] 日中バッチが {job} を繰り返し失っている",
            "--body", body]
    for label in ISSUE_LABELS:
        argv += ["--label", label]
    try:
        proc = run(argv, cwd=str(bc.ROOT), capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
    except OSError as e:
        return f"gh を起動できない: {e}"
    if proc.returncode != 0:
        return f"gh issue create が失敗: {(proc.stderr or '').strip()[:200]}"
    return None


def reclaim_inflight(db=None, run=subprocess.run) -> list[str]:
    """前回の中断を回収する。戻り値はログへ書く行（何も起きなければ空）。

    `state` が `running` のまま残っているマーカーだけが「中断された」を意味する。
    `queued`（＝すでに戻してある）はまだ pop されていないだけなので触らない。
    """
    mark = read_inflight(db)
    if not mark or mark.get("state") != _STATE_RUNNING:
        return []

    jobs = inflight_jobs(mark)
    requeued = mark.get("requeued", 0)
    requeued = requeued if isinstance(requeued, int) else 0
    at = mark.get("at", "不明")

    # 出力に cp932 で表現できない記号を混ぜない（em dash など）。`Runner.write` は
    # print を先に呼ぶので、ここで落ちると回収そのものが走らなくなる。
    lines: list[str] = []
    gone = [j for j in jobs if j not in JOBS]
    known = [j for j in jobs if j in JOBS]
    if gone:
        lines.append(f"[inflight] 前回取り出した {', '.join(repr(g) for g in gone)} が"
                     f" JOBS に無い（定義が消えたか typo）。戻さず捨てる")
    if not known:
        clear_inflight(db)
        return lines

    label = ", ".join(known)
    if requeued >= MAX_REQUEUE:
        clear_inflight(db)
        lines.append(f"[inflight] {label} は {MAX_REQUEUE + 1} 回続けて結論を出す前に消えた"
                     f"（最後の取り出し {at}）。戻さず捨てる。"
                     f"戻し続けると毎日同じ計算を繰り返して先へ進まないため")
        note = notify_interrupted(known, mark, run=run)
        lines.append(f"[warn] 通知できなかった: {note}" if note
                     else "[inflight] 起票した")
        return lines

    # **重複を作らない**（マーカーとキューが食い違っていたときに同じ仕事を2回走らせない）。
    # ただし取り除くのは**戻す1件につき1つまで**——`bench:rhat-scale` のように同じ名前を
    # わざと複数積む運用があり、全部消すと残りの回まで黙って消える。
    rest = list(read_queue(db))
    dropped: list[str] = []
    for j in known:
        if j in rest:
            rest.remove(j)
            dropped.append(j)
    write_queue(known + rest, db)
    write_inflight(known, _STATE_QUEUED, requeued + 1, db)
    lines.append(f"[inflight] 前回 {label} が結論を出す前に消えた（最後の取り出し {at}）。"
                 f"キュー先頭へ戻した（{requeued + 1}/{MAX_REQUEUE} 回目）")
    if dropped:
        # 黙って減らさない。同じ名前を複数積んでいた回は、ここで1つ相殺されている。
        lines.append(f"[inflight] {', '.join(dropped)} はキューにも残っていたので"
                     f"1つ相殺した（二重に走らせないため。足すなら -Enqueue）")
    return lines


def carried_requeue(job: str, db=None) -> int:
    """`job` がキューへ戻された仕事なら、その回数。無関係なら 0。

    回数を引き継がないと `MAX_REQUEUE` が数えられず、戻すたびに 0 から数え直して
    無限に戻り続ける。複数件を戻した回は**先頭が一致するか**で見る——戻した並びは
    そのままキュー先頭へ入るので、次の実走でも先頭に来る。
    """
    mark = read_inflight(db)
    if not mark or mark.get("state") != _STATE_QUEUED:
        return 0
    if inflight_jobs(mark)[:1] != [job]:
        return 0
    n = mark.get("requeued", 0)
    return n if isinstance(n, int) else 0


# ── 暦（#681・ADR-0056）──────────────────────────────────────────────────────
#
# 日付で決まる仕事を積む側と、月次系バッチと重なる日に重い計算を出さない側の2つ。
# どちらも「今日」を引数に取る純関数を芯にして、実走・ドライラン・`--peek`・`--queue` が
# 同じ判断を共有する（見せる計画と実際に走る1件がずれない）。

KEY_SCHEDULE = "daytime_schedule"   # {job: "YYYY-MM"}＝その月の暦を処理済みか

# 月をまたいだ間隔の上限（31日）に足す余裕。**先頭へ積んでも当日に走るとは限らない**:
#   - 平日トリガなので、day 日が土曜なら月曜まで待つ（CADENCE_H）
#   - 暦の仕事が同じ日に2つ期限を迎えると、2つめは翌営業日（+24時間）
# `batch_freshness.PRODUCERS` の閾値はこれに窓を足して導く（実測から逆算しない・ADR-0042）。
SCHEDULE_CADENCE_H = 31 * 24.0 + CADENCE_H + 24.0

MONTHLY_BATCHES = (run_monthly, run_monthly_beta, run_monthly_m1)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """DB の naive datetime を UTC とみなす（接続の TimeZone は UTC 固定・ADR-0043）。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cp932(text: str) -> str:
    """ログ行を cp932 で書ける文字だけにする（`Runner.write` は print を先に呼ぶ）。"""
    return text.encode("cp932", "replace").decode("cp932")


def h1_created_at(db) -> Optional[datetime]:
    """半期（H1）の行が最後に**新しく**入った時刻。

    `updated_at` は使わない——株価の補完など既存行の更新でも進むので、収集が前進した証拠に
    ならない（JPX 業種マスタで `companies.industry` を見ないのと同じ理由・#632）。
    `'H1'` は `collector_interim.INTERIM_PERIOD_TYPE` と同じ値。あちらは import 時に `.env` を
    読むのでここからは import せず、一致はテストが照合する。
    """
    from sqlalchemy import func, select
    from database import FinancialRecord
    return _utc(db.execute(
        select(func.max(FinancialRecord.created_at))
        .where(FinancialRecord.period_type == "H1")).scalar())


def disclosure_created_at(db) -> Optional[datetime]:
    """会社予想（`statement_disclosure`）の行が最後に新しく入った時刻。

    upsert は `created_at` を上書きしない（`upsert_statement_disclosures`）ので、同じ日を
    取り直しても進まない。
    """
    from sqlalchemy import func, select
    from database import StatementDisclosure
    return _utc(db.execute(select(func.max(StatementDisclosure.created_at))).scalar())


@dataclass(frozen=True)
class Scheduled:
    """日付で決まる仕事1つ。**毎月 `day` 日以降の最初の実走で、キュー先頭へ1回だけ積む。**"""
    job: str                                            # JOBS のキー
    day: int                                            # 1〜28（2月にも必ず来る日）
    produced: Callable[[object], Optional[datetime]]    # 今月ぶんが入ったか（watchdog と共有）
    source: str                                         # produced が読む場所（起票の本文へ出す）
    why: str


SCHEDULE: tuple[Scheduled, ...] = (
    Scheduled(
        job="disclosures",
        day=1,
        produced=disclosure_created_at,
        source="max(statement_disclosure.created_at)",
        why="J-Quants 無料プランは84日遅れで届き、提出日の集中が無いので月1回で取りこぼさない。"
            "1日を選んだのは月次本体と時間が重なる日だから（その日は重い計算を取り出さないので、"
            "枠を収集で使えば日中枠がまるごと空かない）",
    ),
    Scheduled(
        job="interim",
        day=16,
        produced=h1_created_at,
        source="max(financial_records.created_at) WHERE period_type='H1'",
        why="半期報告書の提出期限は期末+45日で、月末が期末なら各月14〜15日に集中する"
            "（3月期の H1 は 11/14）。その直後に取り込む",
    ),
)


def read_schedule_marks(db=None) -> dict[str, str]:
    """暦の処理済み印。**壊れた値は空として扱う**（`read_queue` と同じ方針）。

    空に倒すと今月ぶんをもう一度判定するだけで、成果物が入っていれば積まない。
    """
    from database import get_setting

    own = db is None
    db = db or _session()
    try:
        raw = get_setting(db, KEY_SCHEDULE)
    finally:
        if own:
            db.close()
    try:
        marks = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    if not isinstance(marks, dict):
        return {}
    return {str(k): v for k, v in marks.items() if isinstance(v, str)}


def write_schedule_marks(marks: dict[str, str], db=None) -> None:
    from database import upsert_setting

    own = db is None
    db = db or _session()
    try:
        upsert_setting(db, KEY_SCHEDULE, json.dumps(marks, ensure_ascii=False, sort_keys=True))
    finally:
        if own:
            db.close()


def plan_schedule(queue: Sequence[str], marks: dict[str, str], today: date,
                  produced_at: Callable[[Scheduled], Optional[datetime]],
                  ) -> tuple[list[str], dict[str, str], list[str]]:
    """暦を当てたあとの (キュー, 印, ログ行)。**書き込まない**。

    `produced_at` が例外を出したら「測れない」とみなし、**積む側へ倒す**——収集は冪等なので
    余計に1回走るだけだが、積まない側へ倒すと次に気づくのは watchdog の閾値（1か月超）になる。
    """
    month = today.strftime("%Y-%m")
    marks = dict(marks)
    due: list[str] = []
    notes: list[str] = []
    for s in SCHEDULE:
        if today.day < s.day or marks.get(s.job) == month:
            continue
        marks[s.job] = month
        anchor = datetime(today.year, today.month, s.day, tzinfo=JST)
        try:
            last = produced_at(s)
        except Exception as e:      # noqa: BLE001 — 測れないことで暦ごと止めない
            last = None
            notes.append(_cp932(f"[schedule] {s.job}: 今月ぶんが入ったか測れない"
                                f"（{str(e)[:120]}）。積む側へ倒す"))
        if last is not None and last >= anchor:
            notes.append(f"[schedule] {s.job}: 今月ぶんは {last.astimezone(JST):%Y-%m-%d %H:%M} JST"
                         f" に入っている。積まない")
            continue
        due.append(s.job)
        prev = "無し" if last is None else f"{last.astimezone(JST):%Y-%m-%d} JST"
        notes.append(f"[schedule] {s.job}: 毎月{s.day}日以降の定期投入。キュー先頭へ積む（前回 {prev}）")
    items = due + [x for x in queue if x not in due] if due else list(queue)
    return items, marks, notes


def apply_schedule(today: date, db=None, write: bool = True) -> tuple[list[str], list[str]]:
    """暦を当てる。戻り値は (当てたあとのキュー, ログ行)。`write=False` は読むだけ。"""
    own = db is None
    db = db or _session()

    def produced_at(s: Scheduled) -> Optional[datetime]:
        try:
            return s.produced(db)
        except Exception:
            # 失敗した文の後始末をしないと、この後のキュー書き込みまで巻き込まれる。
            rollback = getattr(db, "rollback", None)
            if rollback is not None:
                rollback()
            raise

    try:
        items, marks, notes = plan_schedule(read_queue(db), read_schedule_marks(db),
                                            today, produced_at)
        if write and notes:
            write_queue(items, db)
            write_schedule_marks(marks, db)
    finally:
        if own:
            db.close()
    return items, notes


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def monthly_overlap_days() -> frozenset[int]:
    """日中枠と時間が重なりうる月次系バッチの起動日（月の何日か）。

    **約束（起動時刻＋窓）から導き、実測の所要では判定しない。** マクロ・ベータは実測 380分で
    8:00 前に終わる月が多いが、窓は16時間あり、延びた月には重なる。
    """
    start = _minutes(TRIGGER_TIME)
    end = start + WINDOW_MIN
    days: set[int] = set()
    for mod in MONTHLY_BATCHES:
        s = _minutes(mod.TRIGGER_TIME)
        e = s + mod.WINDOW_MIN
        if s < end and start < e:
            days.add(mod.TRIGGER_DAY)
        if e > 24 * 60 and start < e - 24 * 60:     # 窓が日をまたぐ
            days.add(mod.TRIGGER_DAY + 1)
    return frozenset(days)


# ── 祝日（#684）──────────────────────────────────────────────────────────────
#
# タスクのトリガは月〜金の固定で、祝日を知らない。日中枠の前提「人が会社に居て PC を
# 触らない」はその日だけ崩れるので、月次の重なりと同じ形で並走に敏感な仕事を見送る。
#
# 国民の祝日・振替休日・国民の休日は内閣府「国民の祝日について」
# （https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html）と 2026-09-17 に照合した。
# **計算で導かない**——春分・秋分は前年2月の官報で決まり、祝日そのものも法改正で動く
# （2020・2021 年の移動）。内閣府は翌年分を毎年2月に公表するので、
# `tests/test_run_daytime.py` はその年の10月以降に翌年ぶんが無ければ落ちる（足し忘れを失敗にする）。
HOLIDAYS: dict[int, tuple[date, ...]] = {
    2026: (
        date(2026, 1, 1), date(2026, 1, 12), date(2026, 2, 11), date(2026, 2, 23),
        date(2026, 3, 20), date(2026, 4, 29), date(2026, 5, 3), date(2026, 5, 4),
        date(2026, 5, 5), date(2026, 5, 6), date(2026, 7, 20), date(2026, 8, 11),
        date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 10, 12),
        date(2026, 11, 3), date(2026, 11, 23),
    ),
    2027: (
        date(2027, 1, 1), date(2027, 1, 11), date(2027, 2, 11), date(2027, 2, 23),
        date(2027, 3, 21), date(2027, 3, 22), date(2027, 4, 29), date(2027, 5, 3),
        date(2027, 5, 4), date(2027, 5, 5), date(2027, 7, 19), date(2027, 8, 11),
        date(2027, 9, 20), date(2027, 9, 23), date(2027, 10, 11), date(2027, 11, 3),
        date(2027, 11, 23),
    ),
}

# 年末年始（祝日ではないが、人が家に居る前提は同じ）。見送る側への誤りは数日の遅れで済み、
# 見送らない側への誤りは結論を変える（#618）ので、休みの会社が多い範囲を安全側に取る。
YEAR_END_DAYS: tuple[tuple[int, int], ...] = (
    (12, 29), (12, 30), (12, 31), (1, 1), (1, 2), (1, 3))

# `-Now -Force` が書く当日限りの解除印。値は JST の日付（`YYYY-MM-DD`）。
# **効く範囲を日付で閉じる**——消し忘れても翌日には効かない。
KEY_HOLIDAY_OVERRIDE = "daytime_holiday_override"


def is_holiday(today: date) -> Optional[bool]:
    """祝日・年末年始なら True。**表が今日の年を持っていなければ None**（分からない）。"""
    table = HOLIDAYS.get(today.year)
    if table is None:
        return None
    return today in table or (today.month, today.day) in YEAR_END_DAYS


def holiday_table_note(today: date) -> Optional[str]:
    """表が今日の年を持たないときのログ行。**見送らない側へ倒す**ので、黙らせない。"""
    if is_holiday(today) is not None:
        return None
    return (f"[calendar] 祝日表（run_daytime.HOLIDAYS）が {today.year} 年を持っていない。"
            f"祝日の見送りが効かない（内閣府の一覧から足すこと）")


def read_holiday_override(today: date, db=None) -> bool:
    """今日の解除印があるか。壊れた値・別の日の値は「無い」とみなす。"""
    from database import get_setting

    own = db is None
    db = db or _session()
    try:
        raw = get_setting(db, KEY_HOLIDAY_OVERRIDE)
    finally:
        if own:
            db.close()
    return raw == today.isoformat()


def write_holiday_override(today: date, db=None) -> None:
    from database import upsert_setting

    own = db is None
    db = db or _session()
    try:
        upsert_setting(db, KEY_HOLIDAY_OVERRIDE, today.isoformat())
    finally:
        if own:
            db.close()


def blocked_by(today: date, holiday_override: bool = False) -> Optional[str]:
    """並走に敏感な仕事を今日取り出さない理由の種類（`monthly` / `holiday`）。無ければ None。

    **月次を先に見る。** 月次の重なりは人の有無と関係が無いので、解除印では外れない。
    """
    if today.day in monthly_overlap_days():
        return "monthly"
    if not holiday_override and is_holiday(today):
        return "holiday"
    return None


def _block_reason(kind: str, today: date) -> tuple[str, str]:
    """(理由, 残りをいつに回すか)。ログ行の組み立てにだけ使う。"""
    if kind == "monthly":
        return f"{today.day}日は月次系のバッチと時間が重なる", "重ならない日"
    return f"{today:%m/%d} は祝日・年末年始で人が PC を触りうる", "次の平日"


def budget_share(n: int) -> float:
    """`n` 件を1回の実走で回すときの、1件あたりの予算（分）。

    **窓の等分であって所要の按分ではない**（#707）。`Σ 予算 + deps_smoke + マージン = 窓` が
    件数によらず成り立つので、`batch_common.window_problem` の検査は今までどおり通る。

    予算は「割り当て」ではなく**打ち切りの閾値**なので、7分で終わる仕事に 148分 を渡しても
    残りが無駄になるわけではない（次の仕事はすぐ始まる）。
    """
    return JOB_BUDGET_MIN / max(n, 1)


def _eligible(key: str, blocked: bool) -> bool:
    """月次と重なる日・祝日に取り出してよい仕事か。

    未知の名前は判断材料が無いので敏感側に倒す（`--peek` と同じ方針）。
    """
    job = JOBS.get(key)
    return job is not None and not job.parallel_sensitive if blocked else True


def select_jobs(queue: Sequence[str], today: date, holiday_override: bool = False,
                ) -> tuple[list[str], Optional[str]]:
    """今日取り出す仕事の並びと、先頭以外を選んだ／何も選ばなかった理由（ログ行）。

    **先頭の1件は無条件**（`enqueue` が `measured_min <= JOB_BUDGET_MIN` を検査済み）。
    2件目以降は、足したときの等分予算に**全員が余裕込みで収まる**なら足し、収まらなければ
    そこで打ち切る（順番を飛ばして先を漁らない）。

        share(N) = JOB_BUDGET_MIN / N
        足せる条件: すべての j について  measured_min(j) * JOB_HEADROOM <= share(N)

    月次と重なる日・祝日は、**並走に敏感な仕事だけを読み飛ばして**敏感でない仕事を同じ規則で
    集める（#681・#684 の挙動を複数件へ広げたもの。残りの順番は崩さない）。

    同じ名前は1日に2回選ばない——ステップ名は `results` 辞書のキーなので、重複すると結果が
    片方に潰れる。未知の名前は**単独で**選び、`steps_for` が声を上げて失敗する形へ渡す。
    """
    if not queue:
        return [], None
    kind = blocked_by(today, holiday_override)
    blocked = kind is not None

    head: Optional[str] = next((k for k in queue if _eligible(k, blocked)), None)
    if head is None:
        reason, later = _block_reason(kind, today)      # type: ignore[arg-type]
        return [], (f"[calendar] {reason}ので、"
                    f"並走に敏感な仕事は取り出さない（キューの {len(queue)}件は{later}に回す）")

    why: Optional[str] = None
    if head != queue[0]:
        reason, _ = _block_reason(kind, today)          # type: ignore[arg-type]
        why = (f"[calendar] {reason}ので、"
               f"並走に敏感な仕事を飛ばして {head} を取り出す")

    selected = [head]
    if JOBS.get(head) is None:
        return selected, why        # 定義を失った仕事は単独で走らせて失敗させる
    for key in queue[queue.index(head) + 1:]:
        if not _eligible(key, blocked):
            continue                # 敏感な仕事だけを読み飛ばす（先頭を選んだときと同じ規則）
        job = JOBS.get(key)
        if job is None or key in selected:
            break
        trial = selected + [key]
        share = budget_share(len(trial))
        if all(JOBS[j].measured_min * JOB_HEADROOM <= share for j in trial):
            selected = trial
        else:
            break
    return selected, why


def select_job(queue: Sequence[str], today: date, holiday_override: bool = False,
               ) -> tuple[Optional[str], Optional[str]]:
    """`select_jobs` の先頭1件（既存の読み手のための薄い包み）。"""
    keys, why = select_jobs(queue, today, holiday_override)
    return (keys[0] if keys else None), why


def take(key: str, db=None) -> None:
    """`key` の最初の1件をキューから取り除く（月次と重なる日は先頭とは限らない）。

    `pop_queue` と同じく**取り除いてから走らせる**——失敗しても戻さない。
    """
    items = read_queue(db)
    if key in items:
        items.remove(key)
    write_queue(items, db)


def _today() -> date:
    """JST の今日。テストが差し替える継ぎ目。"""
    return datetime.now(JST).date()


# ── ステップ組み立て ─────────────────────────────────────────────────────────

def steps_for_many(python: str, job_keys: Sequence[str],
                   ) -> tuple[tuple[Step, ...], dict[str, str]]:
    """今日取り出した仕事ぶんのステップ列と、`{ステップ名: 仕事のキー}` の対応（#707）。

    対応表が要るのは in-flight マーカーを縮めるため——終わったステップがどの仕事だったかを
    名前から引けないと、中断の回収が完了済みの仕事まで巻き戻す。

    `deps_smoke` は**必要な仕事が1つでもあれば先頭に1本だけ**置く（件数ぶん並べない）。
    予算は `JOB_BUDGET_MIN` の等分で、`DEPS_SMOKE_MIN` は走るかによらず窓から引いてある。
    """
    if not job_keys:
        return (), {}

    share = budget_share(len(job_keys))
    steps: list[Step] = []
    owner: dict[str, str] = {}
    if any(getattr(JOBS.get(k), "needs_deps_smoke", False) for k in job_keys):
        steps.append(Step(
            "deps_smoke", (python, "-m", "scripts.check_heavy_imports"),
            why="重い依存（pymc / jax / numpyro 等）が実際に import できるかを確かめる。"
                "未評価 DLL の初回ロードをここが引き受ける（2026-09-01 に Smart App Control が"
                "jaxlib の DLL を弾いて macro_beta が exit=1 で落ちた）",
            budget_min=DEPS_SMOKE_MIN))
    for key in job_keys:
        job = JOBS.get(key)
        if job is None:
            # キューに積んだ後で JOBS から消えた場合。**黙って何もしないのではなく失敗にする**。
            step = Step(f"unknown:{key}", (python, "-c", "raise SystemExit(2)"),
                        why=f"キューにある {key!r} が JOBS に無い（定義が消えたか typo）",
                        budget_min=1)
        else:
            argv = tuple(python if a == "{python}" else a for a in job.argv)
            step = Step(job.name, argv, why=job.why, budget_min=share)
        steps.append(step)
        owner[step.name] = key
    return tuple(steps), owner


def steps_for(python: str, job_key: Optional[str]) -> tuple[Step, ...]:
    """キューから取った1件ぶんのステップ列。`job_key` が None（キューが空）なら空タプル。"""
    steps, _ = steps_for_many(python, [] if job_key is None else [job_key])
    return steps


def heavy_models() -> tuple[str, ...]:
    """このバッチが回しうる heavy プラグイン名（`HEAVY_AUTOMATION` の照合先）。

    キューの中身は実行時にしか決まらないので、**JOBS 全体**から抜き出す（列挙を二重に持たない）。
    """
    out: list[str] = []
    for key in JOBS:
        for m in bc.models_from_steps(steps_for(sys.executable, key)):
            if m not in out:
                out.append(m)
    return tuple(out)


def log_path(now=None) -> Path:
    return bc.log_path(SPEC.log_prefix, now)


def record_footprint(results: dict[str, int]) -> Optional[str]:
    return bc.record_footprint(results, SPEC.key_run, SPEC.key_success)


def issue_body(results: dict[str, int], log: Path) -> str:
    return bc.issue_body(results, log, SPEC.headline)


def notify(results: dict[str, int], log: Path, run=subprocess.run) -> Optional[str]:
    return bc.notify(results, log, SPEC.issue_title, issue_body(results, log), run=run)


def build_parser():
    """共通パーサ（`--steps` / `--dry-run` / `--no-issue`）にキュー操作を足したもの（#692）。

    **キューに触る前に解析し終える**ために、全部の引数をここへ載せる。以前は `"--x" in args`
    の手書き判定のあと、実走経路の最後（`bc.run_batch` の中）で初めて解析していたため、
    `--help` や打ち間違いの引数が `take` の後で `SystemExit` になり、キュー先頭の仕事が
    黙って消えた（exit 0 でヘルプが出るだけで、in-flight マーカーも `finally` で消える）。

    `--steps` のヘルプに並べる名前は JOBS 全体から作る。検証は今日取り出す件数が決まってから、
    `take` の前に実際のステップ列に対して行う（`main` 参照）。
    """
    names = ["deps_smoke"] + [j.name for j in JOBS.values()]
    ap = bc.build_parser(SPEC, list(dict.fromkeys(names)))
    # 2つ渡したときに片方が黙って勝たないよう、互いに排他にする。
    ops = ap.add_mutually_exclusive_group()
    ops.add_argument("--queue", action="store_true",
                     help="キューの中身・暦の予定・今日の見送りを表示する（書き込まない）")
    ops.add_argument("--peek", action="store_true",
                     help="今日取り出す仕事を JSON で出す（キューは減らさない。"
                          "run_daytime.ps1 -Now が使う）")
    ops.add_argument("--allow-holiday", action="store_true",
                     help="今日だけ祝日の見送りを外す（run_daytime.ps1 -Now -Force が使う）")
    ops.add_argument("--clear-queue", action="store_true",
                     help="キューと in-flight マーカーを空にする")
    ops.add_argument("--enqueue", metavar="NAMES",
                     help="末尾へ積む（カンマ区切りで複数可）。積める名前: " + ", ".join(sorted(JOBS)))
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # **DB に触る前に解析する**（#692）。`--help` と未知の引数はここで終わる。
    ns = build_parser().parse_args(args)

    # キュー操作はバッチを起動しない。
    if ns.queue:
        items = read_queue()
        print(f"日中枠のキュー: {len(items)}件")
        for i, name in enumerate(items, 1):
            job = JOBS.get(name)
            note = f"実測 {job.measured_min:.0f}分" if job else "**未知の仕事**"
            print(f"  {i}. {name}  ({note})")
        if not items:
            print("  （空）積むには --enqueue <名前>。積める名前: " + ", ".join(sorted(JOBS)))
        # **中断はここに出す**（セッション開始時に必ず見る画面・#639）。マーカーが
        # running のまま残っているのは「前回プロセスごと消えた」を意味する。
        mark = read_inflight()
        if mark:
            state = mark.get("state")
            held = ", ".join(inflight_jobs(mark)) or "不明"
            if state == _STATE_RUNNING:
                print(f"  [inflight] 前回 {held} が結論を出す前に消えている"
                      f"（最後の取り出し {mark.get('at', '不明')}）。次の実走で回収する")
            elif state == _STATE_QUEUED:
                print(f"  [inflight] {held} は中断から戻した仕事"
                      f"（{mark.get('requeued', 0)}/{MAX_REQUEUE} 回目）")
        # **暦と月次の重なりもここに出す**（#681）。次の実走で何が積まれ、何が見送られるかを
        # セッション開始時に読めるようにする。読むだけで書かない。
        today = _today()
        print("  [schedule] 暦: " + " / ".join(f"{s.job}=毎月{s.day}日以降" for s in SCHEDULE))
        planned, notes = apply_schedule(today, write=False)
        for line in notes:
            print("  " + line)
        override = read_holiday_override(today)
        picked, why = select_jobs(planned, today, override)
        kind = blocked_by(today, override)
        if picked:
            # **今日どこまで進むかをここで読めるようにする**（#707）。セッション開始時に
            # 必ず見る画面なので、件数と予算が分からないと消化の見込みが立たない。
            total = sum(JOBS[k].measured_min for k in picked if k in JOBS)
            print(f"  [today] 今日取り出す: {', '.join(picked)}"
                  f"（{len(picked)}件・Σ実測 {total:.0f}分・予算 各{budget_share(len(picked)):.0f}分）")
        if why:
            print("  " + why)
        elif kind == "monthly":
            print(f"  [calendar] 今日（{today.day}日）は月次系のバッチと時間が重なる日。"
                  "並走に敏感な仕事は取り出さない")
        elif kind == "holiday":
            print(f"  [calendar] 今日（{today:%m/%d}）は祝日・年末年始。"
                  "並走に敏感な仕事は取り出さない（-Now -Force で今日だけ外せる）")
        if override and is_holiday(today):
            print("  [calendar] 今日は祝日の見送りを解除してある（-Now -Force の解除印）")
        note = holiday_table_note(today)
        if note:
            print("  " + note)
        return 0
    if ns.peek:
        # `run_daytime.ps1 -Now` が「今すぐ叩いてよいか」を判断するための機械可読口。
        # **キューは減らさない**（判断だけして走らせないことがある）。暦と月次の重なりは
        # 実走と同じ関数で当てる＝見せた並びと実際に走る並びがずれない（#681・#707）。
        today = _today()
        items, _ = apply_schedule(today, write=False)
        override = read_holiday_override(today)
        keys, _ = select_jobs(items, today, override)
        key = keys[0] if keys else None
        kind = blocked_by(today, override)
        job = JOBS.get(key) if key is not None else None
        print(json.dumps({
            "key": key,
            "name": job.name if job else None,
            "known": job is not None,
            # **未知の仕事は敏感側に倒す。** 判断材料が無いときに黙って走らせない。
            # 複数件を取り出す日（#707）は**1件でも敏感なら敏感**（安全側）。
            "sensitive": any(
                JOBS[k].parallel_sensitive if k in JOBS else True for k in keys),
            "measured_min": job.measured_min if job else None,
            # 今日取り出す全件と、その合計（#707）。既存の `key` / `measured_min` は
            # 先頭1件のまま＝古い読み手を壊さない。
            "keys": keys,
            "total_measured_min": round(
                sum(JOBS[k].measured_min for k in keys if k in JOBS), 1),
            "remaining": len(items),
            # キューに仕事があるのに今日は何も取り出さない（月次系のバッチと重なる日・祝日）。
            "blocked": key is None and bool(items),
            "blocked_by": kind if (key is None and items) else None,
            # 祝日の見送りが効いている（`-Now -Force` は解除印を書いてから叩く・#684）。
            "holiday_skip": kind == "holiday",
        }))
        return 0
    if ns.allow_holiday:
        # `run_daytime.ps1 -Now -Force` が叩く。**今日（JST）だけ**祝日の見送りを外す。
        # 月次の重なりは外れない（`blocked_by` が月次を先に見る）。
        today = _today()
        write_holiday_override(today)
        state = {True: "祝日", False: "祝日ではない", None: "祝日表の範囲外"}[is_holiday(today)]
        print(f"今日（{today.isoformat()}・{state}）の祝日の見送りを解除した（翌日には効かない）")
        return 0
    if ns.clear_queue:
        write_queue([])
        # **マーカーも消す。** 残すと、消したはずの仕事を次の実走が黙って積み直す。
        clear_inflight()
        print("日中枠のキューを空にした")
        return 0
    if ns.enqueue is not None:
        names = [x for x in ns.enqueue.split(",") if x]
        if not names:
            raise SystemExit("--enqueue には仕事の名前が要る（カンマ区切りで複数可）")
        items = enqueue(names)
        print(f"積んだ: {', '.join(names)} / キューは {len(items)}件")
        return 0

    dry = ns.dry_run
    today = _today()

    # **キューを読む前に回収する**（#639）。戻した仕事がそのまま今日の1件になる。
    # ドライランは「何も実行していない」を守るので読み書きしない。
    notes = [] if dry else reclaim_inflight()
    # 暦は回収の後に当てる（#681）。期限を迎えた収集は回収した仕事よりも前に並ぶ——
    # 収集は短く、先頭で待たせないことが watchdog の閾値の前提になっている。
    items, sched_notes = apply_schedule(today, write=not dry)
    job_keys, why = select_jobs(items, today, read_holiday_override(today))
    table_note = holiday_table_note(today)
    notes += sched_notes + ([why] if why else []) + ([table_note] if table_note else [])
    if notes:
        if dry:
            for line in notes:
                print(line)
        else:
            # ログは追記モードなので、この後の run_batch の出力の前に並ぶ。
            with bc.Runner(log_path()) as runner:
                for line in notes:
                    runner.write(line)

    if not job_keys:
        # **空を失敗にしない**（平日毎日走るので、積んでいない日に毎回起票すると煩い）。
        # 空だったこと・見送ったことは watchdog のレポートと足跡に出る。
        if items:
            print("今日は取り出せる仕事が無い（月次系のバッチと重なる日か祝日）。キューはそのまま")
        else:
            print("日中枠のキューが空。今日は何もしない（積むには --enqueue <名前>）")
        if not dry:
            record_footprint({})
        return 0
    # `--steps` の打ち間違いも **取り除く前に** 弾く（#692）。後だと `select_steps` の
    # SystemExit で仕事が戻らない。
    steps, owner = steps_for_many(sys.executable, job_keys)
    selected = bc.select_steps(steps, ns.steps)
    # `--steps` で絞られたら、**生き残ったステップの仕事だけ**を取り除く（#707）。
    # 全部取り除くと、走らせていない仕事がキューから黙って消える。
    taken = [k for k in job_keys if k in {owner.get(s.name) for s in selected}]
    if not dry:
        for key in taken:
            take(key)

    # 実走経路に届く引数は `--steps` / `--dry-run` / `--no-issue` だけ（キュー操作は上で
    # return 済み）で、`run_batch` 側の共通パーサが受け付ける集合と同じ＝二度目の解析は必ず通る。
    if dry:
        hooks = bc.Hooks(log_path=log_path, record_footprint=record_footprint, notify=notify)
        return bc.run_batch(SPEC, steps, hooks, args)

    # マーカーは take の直後に立て、**戻り値によらず finally で消す**。Python が生きていれば
    # 必ず消えるので、残っていること自体が「OS ごと消された」証拠になる（#639）。
    # **1件終わるごとに縮める**（#707）＝3件目で消えた回に、完了済みの1・2件目まで
    # 巻き戻さない。縮めるのは終わった仕事だけで、成否は問わない（結論を出した失敗は
    # 戻さないという `pop_queue` の判断をそのまま保つ）。
    remaining = list(taken)
    carried = carried_requeue(remaining[0]) if remaining else 0
    write_inflight(remaining, _STATE_RUNNING, carried)

    def _done(step: Step, code: int) -> None:
        key = owner.get(step.name)
        if key in remaining:
            remaining.remove(key)
            # 回数は引き継ぐ。落とすと `MAX_REQUEUE` が数えられず無限に戻り続ける。
            write_inflight(remaining, _STATE_RUNNING, carried)

    hooks = bc.Hooks(log_path=log_path, record_footprint=record_footprint, notify=notify,
                     on_step_done=_done)
    try:
        return bc.run_batch(SPEC, steps, hooks, args)
    finally:
        clear_inflight()


if __name__ == "__main__":
    raise SystemExit(main())
