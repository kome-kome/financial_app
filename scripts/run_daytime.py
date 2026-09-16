"""平日日中の枠で、重い計算を**キューから1日1件ずつ**進める（Issue #618）。

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

窓は 8時間（480分・1件あたりの予算 445分）。実測は macro_beta 380〜419分・M-3 探索
306〜369分・M-2 探索 176〜179分でいずれも収まるが、**M-1 探索は 752分で入らない**（ADR-0046 で専用タスクへ出したまま）。
`JOBS` に無い名前と、予算が窓を超える仕事は `enqueue` の時点で弾く。

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

実行:
    python -m scripts.run_daytime                       # キュー先頭を1件
    python -m scripts.run_daytime --dry-run             # 実行計画だけ
    python -m scripts.run_daytime --queue               # キューの中身を見る
    python -m scripts.run_daytime --peek                # 次の1件を JSON で（キューは減らさない）
    python -m scripts.run_daytime --enqueue beta        # 末尾へ積む
    python -m scripts.run_daytime --enqueue beta,tune:macro_gbdt
    python -m scripts.run_daytime --clear-queue         # 空にする

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

# 窓からマージンと deps_smoke を引いた、1件あたりの上限（ADR-0040）。
# **実測から逆算した値ではない**——パネルは毎晩伸びるので所要は据え置かず伸びる。
DEPS_SMOKE_MIN = 5
MARGIN_MIN = 30
JOB_BUDGET_MIN = WINDOW_MIN - DEPS_SMOKE_MIN - MARGIN_MIN   # = 445


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
        # **未実測**。2条件の gate:interactions が 6.9分（CV は1条件あたり約45秒）なので、
        # 5条件でも20分前後と見て余裕を置いた。**実走で差し替える。**
        measured_min=30.0,
        parallel_sensitive=True,   # gate:interactions と同じ理由（採否が CI の符号で決まる）
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
    # 一致率が 0.367 -> 0.962 になった（`measure_split_valuation_bias.DEFAULT_BPS_PATH`）。
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
        # **未実測**。`oof_backtest` 自体は純後処理で軽く、重いのは上流の walk-forward 学習。
        # それを**2回**払ううえ係数表の全置換も2回入る。実走で差し替える。
        measured_min=180.0,
        # 測った rank-IC そのものが成果物なので、並走で揺れた値を根拠に読むと判断ごと誤る
        # （#618・macro_beta で発散が 0 → 344 回）。
        parallel_sensitive=True,
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


def write_inflight(job: str, state: str, requeued: int, db=None) -> None:
    from database import upsert_setting

    payload = {
        "job": job,
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


def notify_interrupted(job: str, mark: dict, run=subprocess.run) -> Optional[str]:
    """戻す上限に達した仕事を起票する。**gh が無くても落とさない**（`bc.notify` と同じ）。"""
    body = "\n".join([
        f"日中バッチが `{job}` を **{MAX_REQUEUE + 1} 回続けて、結論を出す前に**失っている。",
        "",
        "| 項目 | 値 |",
        "|---|---|",
        f"| 仕事 | `{job}` |",
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
        "4. 原因が解消したら `run_daytime.ps1 -Enqueue " + job + "` で積み直す",
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

    job = mark.get("job")
    requeued = mark.get("requeued", 0)
    requeued = requeued if isinstance(requeued, int) else 0
    at = mark.get("at", "不明")

    if not isinstance(job, str) or job not in JOBS:
        clear_inflight(db)
        return [f"[inflight] 前回取り出した {job!r} が JOBS に無い（定義が消えたか typo）。"
                f"戻さず捨てる"]

    if requeued >= MAX_REQUEUE:
        clear_inflight(db)
        # 出力に cp932 で表現できない記号を混ぜない（em dash など）。`Runner.write` は
        # print を先に呼ぶので、ここで落ちると回収そのものが走らなくなる。
        lines = [f"[inflight] {job} は {MAX_REQUEUE + 1} 回続けて結論を出す前に消えた"
                 f"（最後の取り出し {at}）。戻さず捨てる。"
                 f"戻し続けると毎日同じ計算を繰り返して先へ進まないため"]
        note = notify_interrupted(job, mark, run=run)
        lines.append(f"[warn] 通知できなかった: {note}" if note
                     else "[inflight] 起票した")
        return lines

    items = [x for x in read_queue(db) if x != job]     # 重複を作らない
    write_queue([job] + items, db)
    write_inflight(job, _STATE_QUEUED, requeued + 1, db)
    return [f"[inflight] 前回 {job} が結論を出す前に消えた（最後の取り出し {at}）。"
            f"キュー先頭へ戻した（{requeued + 1}/{MAX_REQUEUE} 回目）"]


def carried_requeue(job: str, db=None) -> int:
    """`job` がキューへ戻された仕事なら、その回数。無関係なら 0。

    回数を引き継がないと `MAX_REQUEUE` が数えられず、戻すたびに 0 から数え直して
    無限に戻り続ける。
    """
    mark = read_inflight(db)
    if not mark or mark.get("state") != _STATE_QUEUED or mark.get("job") != job:
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


def select_job(queue: Sequence[str], today: date) -> tuple[Optional[str], Optional[str]]:
    """今日取り出す1件と、先頭以外を選んだ／何も選ばなかった理由（ログ行）。

    月次と重なる日は、**並走に敏感でない仕事を先頭から探す**（残りの順番は崩さない）。
    未知の名前は判断材料が無いので敏感側に倒す（`--peek` と同じ方針）。
    """
    if not queue:
        return None, None
    if today.day not in monthly_overlap_days():
        return queue[0], None
    for key in queue:
        job = JOBS.get(key)
        if job is not None and not job.parallel_sensitive:
            if key == queue[0]:
                return key, None
            return key, (f"[calendar] {today.day}日は月次系のバッチと時間が重なるので、"
                         f"並走に敏感な仕事を飛ばして {key} を取り出す")
    return None, (f"[calendar] {today.day}日は月次系のバッチと時間が重なるので、"
                  f"並走に敏感な仕事は取り出さない（キューの {len(queue)}件は重ならない日に回す）")


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

def steps_for(python: str, job_key: Optional[str]) -> tuple[Step, ...]:
    """キューから取った1件ぶんのステップ列。`job_key` が None（キューが空）なら空タプル。"""
    if job_key is None:
        return ()
    job = JOBS.get(job_key)
    if job is None:
        # キューに積んだ後で JOBS から消えた場合。**黙って何もしないのではなく失敗にする**。
        return (Step(f"unknown:{job_key}", (python, "-c", "raise SystemExit(2)"),
                     why=f"キューにある {job_key!r} が JOBS に無い（定義が消えたか typo）",
                     budget_min=1),)

    steps: list[Step] = []
    if job.needs_deps_smoke:
        steps.append(Step(
            "deps_smoke", (python, "-m", "scripts.check_heavy_imports"),
            why="重い依存（pymc / jax / numpyro 等）が実際に import できるかを確かめる。"
                "未評価 DLL の初回ロードをここが引き受ける（2026-09-01 に Smart App Control が"
                "jaxlib の DLL を弾いて macro_beta が exit=1 で落ちた）",
            budget_min=DEPS_SMOKE_MIN))
    argv = tuple(python if a == "{python}" else a for a in job.argv)
    steps.append(Step(job.name, argv, why=job.why, budget_min=JOB_BUDGET_MIN))
    return tuple(steps)


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # キュー操作は共通パーサの守備範囲外なので先に処理する（バッチを起動しない）。
    if "--queue" in args:
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
            if state == _STATE_RUNNING:
                print(f"  [inflight] 前回 {mark.get('job')!r} が結論を出す前に消えている"
                      f"（最後の取り出し {mark.get('at', '不明')}）。次の実走で回収する")
            elif state == _STATE_QUEUED:
                print(f"  [inflight] {mark.get('job')!r} は中断から戻した仕事"
                      f"（{mark.get('requeued', 0)}/{MAX_REQUEUE} 回目）")
        # **暦と月次の重なりもここに出す**（#681）。次の実走で何が積まれ、何が見送られるかを
        # セッション開始時に読めるようにする。読むだけで書かない。
        today = _today()
        print("  [schedule] 暦: " + " / ".join(f"{s.job}=毎月{s.day}日以降" for s in SCHEDULE))
        planned, notes = apply_schedule(today, write=False)
        for line in notes:
            print("  " + line)
        _, why = select_job(planned, today)
        if why:
            print("  " + why)
        elif today.day in monthly_overlap_days():
            print(f"  [calendar] 今日（{today.day}日）は月次系のバッチと時間が重なる日。"
                  "並走に敏感な仕事は取り出さない")
        return 0
    if "--peek" in args:
        # `run_daytime.ps1 -Now` が「次の1件を今すぐ叩いてよいか」を判断するための機械可読口。
        # **キューは減らさない**（判断だけして走らせないことがある）。暦と月次の重なりは
        # 実走と同じ関数で当てる＝見せた1件と実際に走る1件がずれない（#681）。
        today = _today()
        items, _ = apply_schedule(today, write=False)
        key, _ = select_job(items, today)
        job = JOBS.get(key) if key is not None else None
        print(json.dumps({
            "key": key,
            "name": job.name if job else None,
            "known": job is not None,
            # **未知の仕事は敏感側に倒す。** 判断材料が無いときに黙って走らせない。
            "sensitive": job.parallel_sensitive if job else (key is not None),
            "measured_min": job.measured_min if job else None,
            "remaining": len(items),
            # キューに仕事があるのに今日は何も取り出さない（月次系のバッチと重なる日）。
            "blocked": key is None and bool(items),
        }))
        return 0
    if "--clear-queue" in args:
        write_queue([])
        # **マーカーも消す。** 残すと、消したはずの仕事を次の実走が黙って積み直す。
        clear_inflight()
        print("日中枠のキューを空にした")
        return 0
    for i, a in enumerate(args):
        if a == "--enqueue":
            names = [x for x in args[i + 1].split(",") if x] if i + 1 < len(args) else []
            if not names:
                raise SystemExit("--enqueue には仕事の名前が要る（カンマ区切りで複数可）")
            items = enqueue(names)
            print(f"積んだ: {', '.join(names)} / キューは {len(items)}件")
            return 0

    dry = "--dry-run" in args
    today = _today()

    # **キューを読む前に回収する**（#639）。戻した仕事がそのまま今日の1件になる。
    # ドライランは「何も実行していない」を守るので読み書きしない。
    notes = [] if dry else reclaim_inflight()
    # 暦は回収の後に当てる（#681）。期限を迎えた収集は回収した仕事よりも前に並ぶ——
    # 収集は短く、先頭で待たせないことが watchdog の閾値の前提になっている。
    items, sched_notes = apply_schedule(today, write=not dry)
    job_key, why = select_job(items, today)
    notes += sched_notes + ([why] if why else [])
    if notes:
        if dry:
            for line in notes:
                print(line)
        else:
            # ログは追記モードなので、この後の run_batch の出力の前に並ぶ。
            with bc.Runner(log_path()) as runner:
                for line in notes:
                    runner.write(line)

    if job_key is None:
        # **空を失敗にしない**（平日毎日走るので、積んでいない日に毎回起票すると煩い）。
        # 空だったこと・見送ったことは watchdog のレポートと足跡に出る。
        if items:
            print("今日は取り出せる仕事が無い（月次系のバッチと時間が重なる日）。キューはそのまま")
        else:
            print("日中枠のキューが空。今日は何もしない（積むには --enqueue <名前>）")
        if not dry:
            record_footprint({})
        return 0
    if not dry:
        take(job_key)

    hooks = bc.Hooks(log_path=log_path, record_footprint=record_footprint, notify=notify)
    if dry:
        return bc.run_batch(SPEC, steps_for(sys.executable, job_key), hooks, args)

    # マーカーは pop の直後に立て、**戻り値によらず finally で消す**。Python が生きていれば
    # 必ず消えるので、残っていること自体が「OS ごと消された」証拠になる（#639）。
    write_inflight(job_key, _STATE_RUNNING, carried_requeue(job_key))
    try:
        return bc.run_batch(SPEC, steps_for(sys.executable, job_key), hooks, args)
    finally:
        clear_inflight()


if __name__ == "__main__":
    raise SystemExit(main())
