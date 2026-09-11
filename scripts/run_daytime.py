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
from pathlib import Path
from typing import Optional, Sequence

from scripts import batch_common as bc
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
    "gate:interactions": Job(
        name="gate_interactions",
        argv=("{python}", "-m", "scripts.momentum_gate", "--interactions", "--stride", "1"),
        why="交互作用（財務 × マクロの交差項）の有無を共通 (ym,ec) 域で測る（#615）。"
            "スモーク（stride=5）では nointer +0.2603 に対し inter +0.0989 で "
            "diff=-0.1615（95%CI[-0.2719,-0.0467]）と出たが、**--smoke の共通域は "
            "間引きで壊れるので判定には使わない**（ADR-0050）。これは stride=1 の本測定。",
        # **未実測**。スモーク（stride=5）は約10分で終わったが、本測定はサンプルが5倍
        # （36,396 → 181,833）。パネル構築も CV も伸びるので保守的に置く。**実走で差し替える。**
        measured_min=300.0,
        # 昇格ゲートの実測。差が −0.1615 か −0.16 かではなく「符号と CI が 0 をまたぐか」で
        # 採否が決まるので、並走で揺れた値を根拠に採否を決めると判断ごと誤る。
        parallel_sensitive=True,
    ),

    # ── 最新業績の供給（#424 の子タスク1・ADR-0051）────────────────────────
    # #503 で GHA cron を止めて以降、H1 と会社予想は**どこからも収集されていない**
    # （呼び出し元が `collect-interim.yml` / `collect-disclosures.yml` の手動トリガだけ）。
    # 実測 2026-09-07: H1 は period_end MAX 2025-09-30（11.2ヶ月）・会社予想は
    # disc_date MAX 2026-04-17（4.7ヶ月）。**月次本体の空きは 67分しかなく入らない**
    # （GHA 実測 2h31m）ので、日中枠（予算445分）で回す。
    #
    # **2026-09-08 の初実走（ローカル 62.0分）は新規0件だった**——候補 9657件の内訳が
    # 既収集 5736 + Q2以外 3905 + ZIP 失敗 16 で、収穫が1件も無い。H1 の period_end MAX が
    # 2025-09-30 のまま動かないのは**収集漏れではなく提出時期**で、3月期企業の H1 は
    # 9/30 期末・11月提出。つまり**11月より前に積んでも取るものが無い**（積むのは11月以降）。
    # 「11.2ヶ月古い」という上の観測は鮮度の異常ではなく、H1 という指標の周期そのもの。
    #
    # ZIP 失敗 16件は #630 で決着した。**CSV 形式を持たない書類**（`csvFlag='0'`＝外国会社等の
    # HTML のみ提出）で、EDINET は `type=5` に HTTP 200 + JSON を返すため `BadZipFile` に化けていた。
    # 待っても現れない恒久的失敗なので候補選別の手前で外す。次の実走では `CSV 無しで除外 N件` が
    # 出て `failed` が減るはず。所要は 16件ぶんの往復が消えるだけなので上の実測から動かない。
    "interim": Job(
        name="collect_interim",
        argv=("{python}", "collector.py", "--interim", "--years", "2"),
        why="半期(H1)財務の差分収集（EDINET 半期報告書・旧四半期Q2）。`skip_existing=True` で"
            "収集済み doc_id は再取得しない＝冪等。`--years 2` は 2025-10 以降の欠落を埋める幅で、"
            "GHA の既定 6 年は初回バックフィル用の値。",
        measured_min=62.0,       # 2026-09-08 ローカル実測（GHA 6年 2h31m から差し替え）
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
        return 0
    if "--peek" in args:
        # `run_daytime.ps1 -Now` が「次の1件を今すぐ叩いてよいか」を判断するための機械可読口。
        # **キューは減らさない**（判断だけして走らせないことがある）。
        items = read_queue()
        head = items[0] if items else None
        job = JOBS.get(head) if head is not None else None
        print(json.dumps({
            "key": head,
            "name": job.name if job else None,
            "known": job is not None,
            # **未知の仕事は敏感側に倒す。** 判断材料が無いときに黙って走らせない。
            "sensitive": job.parallel_sensitive if job else (head is not None),
            "measured_min": job.measured_min if job else None,
            "remaining": len(items),
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

    # **キューを読む前に回収する**（#639）。戻した仕事がそのまま今日の1件になる。
    # ドライランは「何も実行していない」を守るので読み書きしない。
    notes = [] if dry else reclaim_inflight()
    if notes:
        # ログは追記モードなので、この後の run_batch の出力の前に並ぶ。
        with bc.Runner(log_path()) as runner:
            for line in notes:
                runner.write(line)

    job_key = read_queue()[0] if dry else pop_queue()
    if job_key is None:
        # **空を失敗にしない**（平日毎日走るので、積んでいない日に毎回起票すると煩い）。
        # 空だったことは watchdog のレポートと足跡に出る。
        print("日中枠のキューが空。今日は何もしない（積むには --enqueue <名前>）")
        record_footprint({})
        return 0

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
