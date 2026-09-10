"""ローカル駆動バッチの足跡（`app_settings` の `*_last_run`）を**測る**層（#515・ADR-0042 / #561）。

判定に必要なものだけをここに置き、**起票・CLI・ログ**は `scripts/check_batch_freshness.py`
（watchdog）が持つ。読み手は2人:

- `scripts/check_batch_freshness.py` … 毎日 JST 20:00、止まっていれば Issue を起票する
- `routers/morning.py` … `/api/morning` の鮮度ブロックへ「昨夜のバッチは走ったか」を出す（#561）

## なぜ watchdog から切り出したのか

`scripts/check_batch_freshness.py` は **import した瞬間に** `load_dotenv()` と
`os.environ["FINAPP_DB_TARGET"] = "local"` を実行する（S4U のセッション0で `.env` を
取り落とすと別 DB を読むため、そこでは正しい）。だが **API プロセスから import すると
Render（prod）の接続先設定を書き換えかねない**——接続先の食い違いは #508 と同型で
静かに壊れる（別の DB を読んで「古い」と表示し、誰も気づかない）。

副作用を持たないこちらを共有し、watchdog は re-export で受ける。**判定を2箇所に書かない**
——閾値の写し間違いは「永久に警告が出ない」形で現れる。

## 閾値は約束から導出する（実測から逆算しない）

各バッチは「cadence ごとに1回、窓の中のどこかで足跡を書く」と約束している。健全な世界で
経過が取りうる上限はそのまま **`cadence + 窓`**。両項ともリポジトリに定数として在るので、
**窓を広げれば閾値も自動で広がる**。副産物として「実行中は鳴らない」が構造的に成立し、
判定が観測時刻に依存しない（実起動は名目 17:20 から +31分〜+1h41m ずれる・#551）。

## 測るのは2軸ある: 「走ったか」と「値が前進したか」

`WATCHED` / `collect()` が見るのは**足跡**で、答えるのは「バッチが起動したか」だけ。
`PRODUCERS` / `collect_producers()` が見るのは**成果物の最終更新**で、答えるのは
「走った結果として値が前進したか」。2026-09-01 の月次は前者が ok・後者が固着という状態を
作り、`plugin_tuned_params` が 50〜59日 古いまま誰にも気づかれなかった（#504）。
producer 側は watchdog の起票だけに出す（`/api/morning` のレスポンス構造は変えない）。

## 見るのは `*_last_run` であって `*_last_success` ではない

`monthly_last_success` は #512 が解けるまで**設計上ずっと古い**（`macro_beta` が毎月
`exit=124` で落ちる想定）。成功で判定すると初日から常時 failure になり、通知そのものが
信用されなくなる。成功側は**報告には必ず載せる**（「走っていない」と「走ったが通っていない」を
読む人が1秒で切り分けられる）が、判定には使わない。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:                      # `python -m scripts.*` からの import 用
    sys.path.insert(0, str(ROOT))

# **import 時に副作用を持つモジュールをここから呼ばない。** 参照するのは定数だけで、
# `scripts/run_*.py` と `scripts/batch_common.py` はトップレベルが定数定義に限られている
# （`SPEC` は純 dataclass）。この前提が崩れると API プロセスが巻き込まれる。
from scripts import (run_backup, run_daytime, run_monthly,  # noqa: E402
                     run_monthly_beta, run_monthly_m1, run_nightly)

# watchdog 自身の足跡。監視対象と同じ表に置く（見る場所を分けない）。
KEY_LAST_RUN = "watchdog_last_run"

# watchdog の ExecutionTimeLimit（分）。`install_watchdog_task.ps1` と CI が照合し、同時に
# 自己監視の閾値（24h + これ）の源にもなる。**24h より十分小さいこと**が要点で、
# MultipleInstances IgnoreNew の下ではハングした1本が翌日を抑止するため。
SELF_WINDOW_MIN = 15


@dataclass(frozen=True)
class Watched:
    """監視対象1本。閾値は `cadence + 窓` の導出であって、直接置く定数ではない。"""
    label: str
    key_run: str
    key_success: Optional[str]
    cadence_h: float
    window_min: float
    issue_title: str
    task_name: str
    log_prefix: str
    missing_is_problem: bool = True

    @property
    def stale_h(self) -> float:
        return self.cadence_h + self.window_min / 60.0


# cadence の根拠:
#   nightly … daily トリガ（install_nightly_task.ps1）＝24時間
#   monthly … 同一日付の最長間隔。12/28->01/28 も 31日で、インストーラが -Day 1..28 に
#             制限しているのでこの上限は -Day を動かしても成立する
#   backup  … weekly トリガ（install_backup_task.ps1）＝7日
#   自分     … daily トリガ。**初回は missing になるが、それは正常**（自分の行を書くのは
#             自分だけで、第三者の書き手が居ない＝missing は「まだ1回目」を意味する）
#
# キー名と窓は run_nightly / run_monthly から import する。書き写した瞬間、typo が
# 「永久に警告が出ない」形で現れる——この watchdog がまさに検知したい失敗モードを自分で踏む。
WATCHED: tuple[Watched, ...] = (
    Watched(
        label="夜間バッチ",
        key_run=run_nightly.KEY_LAST_RUN,
        key_success=run_nightly.KEY_LAST_SUCCESS,
        cadence_h=24.0,
        window_min=run_nightly.WINDOW_MIN,
        issue_title="[ops] ローカル夜間バッチが走っていない",
        task_name="financial_app-nightly",
        log_prefix="nightly",
    ),
    Watched(
        label="月次バッチ",
        key_run=run_monthly.KEY_LAST_RUN,
        key_success=run_monthly.KEY_LAST_SUCCESS,
        cadence_h=31 * 24.0,
        window_min=run_monthly.WINDOW_MIN,
        issue_title="[ops] ローカル月次バッチが走っていない",
        task_name="financial_app-monthly",
        log_prefix="monthly",
    ),
    Watched(
        label="月次バッチ（マクロ・ベータ）",
        key_run=run_monthly_beta.KEY_LAST_RUN,
        key_success=run_monthly_beta.KEY_LAST_SUCCESS,
        cadence_h=31 * 24.0,
        window_min=run_monthly_beta.WINDOW_MIN,
        # 走らないと M-1 の入力（`macro_beta_loadings`）が固着する——それが #579 の症状
        # そのもので、2026-08-01 から5週間気づけなかった。
        issue_title="[ops] ローカルのマクロ・ベータ推論バッチが走っていない",
        task_name="financial_app-monthly-beta",
        log_prefix="monthly_beta",
    ),
    Watched(
        label="月次バッチ（M-1 探索）",
        key_run=run_monthly_m1.KEY_LAST_RUN,
        key_success=run_monthly_m1.KEY_LAST_SUCCESS,
        cadence_h=31 * 24.0,
        window_min=run_monthly_m1.WINDOW_MIN,
        # **タイトルに数字を入れない**（`tests/...::test_title_has_no_date_or_count`）。
        # 「M-1」と書きたくなるが、固定値か日付かをテストは区別できないので一律で禁じてある。
        issue_title="[ops] ローカルのマクロ×リスク-リターン探索バッチが走っていない",
        task_name="financial_app-monthly-m1",
        log_prefix="monthly_m1",
    ),
    Watched(
        label="週次バックアップ",
        key_run=run_backup.KEY_LAST_RUN,
        key_success=run_backup.KEY_LAST_SUCCESS,
        cadence_h=7 * 24.0,
        window_min=run_backup.WINDOW_MIN,
        # 走らないと、#503 で通した復元経路があっても**戻せるのは最後に人が手で叩いた日まで**
        # になる。取り忘れは失敗として現れないので、ここに載せる以外に現す手段が無い（#606）。
        issue_title="[ops] ローカルのバックアップバッチが走っていない",
        task_name="financial_app-backup",
        log_prefix="backup",
    ),
    Watched(
        label="日中バッチ",
        key_run=run_daytime.KEY_LAST_RUN,
        key_success=run_daytime.KEY_LAST_SUCCESS,
        # 平日 8:15 のトリガ（`install_daytime_task.ps1`）。**24 ではなく 72**——金曜に
        # 走ると次は月曜なので、土日を挟む間隔が正常な最長になる。24 にすると毎週土曜に
        # 「走っていない」と鳴り、鳴りっぱなしの警告は読まれなくなる。
        cadence_h=72.0,
        window_min=run_daytime.WINDOW_MIN,
        # 走らないと、重い計算を進める場所そのものが消える。キューが空の日も足跡は残るので、
        # ここが鳴るのは「タスクが起動しなかった」ときだけ（空振りとは区別できる）。
        issue_title="[ops] ローカル日中バッチが走っていない",
        task_name="financial_app-daytime",
        log_prefix="daytime",
    ),
    Watched(
        label="watchdog 自身",
        key_run=KEY_LAST_RUN,
        key_success=None,
        cadence_h=24.0,
        window_min=SELF_WINDOW_MIN,
        issue_title="[ops] watchdog 自身が走っていなかった",
        task_name="financial_app-watchdog",
        log_prefix="watchdog",
        missing_is_problem=False,
    ),
)


def _parse(raw: Optional[str]) -> Optional[datetime]:
    """足跡の文字列を datetime へ。読めなければ None（呼び出し側が raw と突き合わせる）。"""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_hours(then: Optional[datetime], now: datetime) -> Optional[float]:
    return None if then is None else (now - then).total_seconds() / 3600.0


def status_of(row: dict) -> str:
    """ok / missing / unreadable / stale のどれか。

    **「まだ一度も走っていない」と「止まった」を同じ顔にしない**（`check_egress_health.py`
    の「消費ゼロと計測停止は台帳の上で同じ顔をする」と同型）。どちらも異常だが原因が違う
    ——前者はタスク登録を疑い、後者は実行環境の破損を疑う。
    """
    if row["run_raw"] is None:
        return "missing"
    if row["run_age_h"] is None:
        return "unreadable"
    return "ok" if row["run_age_h"] <= row["watched"].stale_h else "stale"


def _get_setting(db, key: str) -> Optional[str]:
    """DB アクセスの継ぎ目。ここに閉じておくとテストが DB 環境なしで import できる。"""
    from database import get_setting
    return get_setting(db, key)


def db_label() -> str:
    """接続先の表示名。**生の接続文字列は絶対に出さない**（Issue は公開されうる）。

    「見ている DB が違う」は「走っていない」と全く同じ顔をするので、この1行が最大の疑いを消す。
    """
    try:
        from database import db_target_info
        return db_target_info().get("db_label", "不明")
    except Exception as e:      # noqa: BLE001 — 表示のために判定を落とさない
        return f"不明（{e}）"


# 足跡の status を画面の語彙へ。**「まだ一度も走っていない」と「止まった」を同じ顔に
# しない**のは判定側と同じで、ここが決めるのは色だけ。
BATCH_LEVEL = {"ok": "fresh", "stale": "alert",
               "missing": "alert", "unreadable": "alert"}


def summarize(snap: dict, fmt_time=None) -> dict:
    """`collect()` の結果を**画面表示用**の形へ畳む（#561 / #563）。

    読み手は `/api/morning` の鮮度カードと `/api/stats`（ダッシュボードの「自動収集」）の
    2つで、**両方で同じ語彙・同じ閾値を使う**ためにここへ置く。片方に「何時間前なら緑」を
    書くと、窓を広げたときにもう片方だけが黙って古くなる。

    `level` は**夜間バッチの鮮度**（`gates_verdict` が立つ行）。月次と watchdog は行として
    返すだけで総合判定には効かせない——既定の推奨経路は月次成果物に依存せず、混ぜると
    次の月次まで毎日 warn が出続けて狼少年になる。

    `fmt_time` は時刻の整形関数（既定は ISO 文字列のまま）。ここで `api` を import しないのは
    循環（api -> routers -> batch_freshness）を作らないためで、**依存は呼び出し側から注入する**。
    """
    fmt_time = fmt_time or (lambda dt: dt.isoformat() if dt else None)
    rows, level = [], "unknown"
    for row in snap["rows"]:
        w = row["watched"]
        if row["status"] == "missing" and not w.missing_is_problem:
            # 自分の行を書くのは自分だけ＝watchdog の初回 missing は正常
            row_level = "fresh"
        else:
            row_level = BATCH_LEVEL.get(row["status"], "alert")
        gates = w.key_run == run_nightly.KEY_LAST_RUN
        if gates:
            level = row_level
        rows.append({
            "label": w.label,
            "task_name": w.task_name,
            "status": row["status"],
            "level": row_level,
            "last_run": fmt_time(_parse(row["run_raw"])),
            "last_success": fmt_time(_parse(row["success_raw"])),
            "age_h": row["run_age_h"],
            "stale_h": w.stale_h,
            "gates_verdict": gates,
        })
    return {"level": level, "rows": rows, "db_label": snap["db_label"]}


def collect(db, now: datetime, get=None) -> dict:
    """足跡を読む。閾値判定はしない（測るのと決めるのを分ける）。"""
    get = get or _get_setting
    rows = []
    for w in WATCHED:
        run_raw = get(db, w.key_run)
        success_raw = get(db, w.key_success) if w.key_success else None
        row = {
            "watched": w,
            "run_raw": run_raw,
            "success_raw": success_raw,
            "run_age_h": _age_hours(_parse(run_raw), now),
            "success_age_h": _age_hours(_parse(success_raw), now),
        }
        row["status"] = status_of(row)
        rows.append(row)
    return {"now": now, "rows": rows, "db_error": None, "db_label": db_label(),
            "gh_error": None}


# ── producer の鮮度（#504）──────────────────────────────────────────────────
# 上の `Watched` が見るのは「バッチが走ったか」（`*_last_run`）で、**走った結果として値が
# 前進したか**は見ていない。2026-09-01 の月次は `tune:macro_dlm` が予算切れ（exit=124）・
# `tune:macro_gbdt` が品質ゲートで persist スキップ（exit=1）となり、失敗自体は #587 として
# 起票・クローズされた。だが**穴はその後も埋まらず**、`plugin_tuned_params` は macro_gbdt が
# 50日・macro_dlm が 59日 古いまま誰にも気づかれなかった（2026-09-07 実測）。
# 「走った」と「値が前進した」は別の事実で、前者だけを見ていると後者は静かに固着する。
#
# `macro_beta` は `status=quarantined` で保全されると run 自体は残るが producer は読まない
# （#609）ので、**live の行だけ**を見る＝隔離が続けば固着として現れる。


@dataclass(frozen=True)
class Produced:
    """月次バッチが更新する成果物1つ。閾値は `Watched` と同じく `cadence + 窓` の導出。"""
    label: str
    issue_title: str
    cadence_h: float
    window_min: float
    batch_label: str        # 誰が更新するか（Issue 本文の誘導先）
    task_name: str          # 確認コマンド用
    source: str             # どこを読んだか（テーブルと条件）
    read: object            # (db) -> Optional[datetime]

    @property
    def stale_h(self) -> float:
        return self.cadence_h + self.window_min / 60.0


def _tuned_at(plugin_name: str):
    """`plugin_tuned_params` の1行。**`database` の import は関数内に閉じる**（副作用回避）。"""
    def read(db):
        from sqlalchemy import select
        from database import PluginTunedParams
        return db.execute(select(PluginTunedParams.tuned_at)
                          .where(PluginTunedParams.plugin_name == plugin_name)).scalar()
    return read


def _macro_beta_live_at(db):
    """**live の行だけ**を見る。隔離（quarantined）は producer が読まない＝固着と同じ。

    `status IS NULL` は列が無かった時代の run で、`database.py` の定義どおり live 扱い。
    語は `database.MACRO_BETA_STATUS_LIVE` から取る（文字列を直接書かない）。
    """
    from sqlalchemy import func, or_, select
    from database import MACRO_BETA_STATUS_LIVE, MacroBetaMeta
    return db.execute(
        select(func.max(MacroBetaMeta.created_at)).where(
            or_(MacroBetaMeta.status == MACRO_BETA_STATUS_LIVE,
                MacroBetaMeta.status.is_(None)))).scalar()


def _factor_premia_at(db):
    from sqlalchemy import func, select
    from database import RecommendFactorPremium
    return db.execute(select(func.max(RecommendFactorPremium.computed_at))).scalar()


def _jpx_industry_at(db):
    """JPX 業種マスタを最後に取得できた時刻（#632）。

    **`companies.industry` の中身は見ない**——既存値は取得が止まっても残り続けるので、
    「更新できているか」の証拠にならない（実測で業種が空なのは 3,725社中2社のまま6晩動かず、
    画面も壊れなかった）。見るのは取得側が書く足跡だけ。
    """
    from database import KEY_JPX_INDUSTRY_LAST_SUCCESS
    return _parse(_get_setting(db, KEY_JPX_INDUSTRY_LAST_SUCCESS))


# cadence は `Watched` と同じ「同一日付の最長間隔」＝31日。窓は各バッチの `WINDOW_MIN` から
# 取る（書き写さない）。**探索は窓いっぱいまで使いうる**ので、窓を広げれば閾値も広がるという
# 関係はここでも成立する（#638・ADR-0054 以後は打ち切られても畳んで永続化するが、書くのは
# やはり窓の終わり際なので、閾値の導出は変わらない）。
PRODUCERS: tuple[Produced, ...] = (
    Produced(
        label="マクロ×リスク-リターン探索の結果",
        issue_title="[ops] マクロ×リスク-リターン探索の結果が更新されていない",
        cadence_h=31 * 24.0,
        window_min=run_monthly_m1.WINDOW_MIN,
        batch_label="月次バッチ（M-1 探索）",
        task_name="financial_app-monthly-m1",
        source="plugin_tuned_params.tuned_at (plugin_name='macro_risk_return')",
        read=_tuned_at("macro_risk_return"),
    ),
    Produced(
        label="マクロ勾配ブースティング探索の結果",
        issue_title="[ops] マクロ勾配ブースティング探索の結果が更新されていない",
        cadence_h=31 * 24.0,
        window_min=run_monthly.WINDOW_MIN,
        batch_label="月次バッチ",
        task_name="financial_app-monthly",
        source="plugin_tuned_params.tuned_at (plugin_name='macro_gbdt')",
        read=_tuned_at("macro_gbdt"),
    ),
    Produced(
        label="動的線形モデル探索の結果",
        issue_title="[ops] 動的線形モデル探索の結果が更新されていない",
        cadence_h=31 * 24.0,
        window_min=run_monthly.WINDOW_MIN,
        batch_label="月次バッチ",
        task_name="financial_app-monthly",
        source="plugin_tuned_params.tuned_at (plugin_name='macro_dlm')",
        read=_tuned_at("macro_dlm"),
    ),
    Produced(
        label="マクロ・ベータの推論結果",
        issue_title="[ops] マクロ・ベータの推論結果が live で更新されていない",
        cadence_h=31 * 24.0,
        window_min=run_monthly_beta.WINDOW_MIN,
        batch_label="月次バッチ（マクロ・ベータ）",
        task_name="financial_app-monthly-beta",
        source="macro_beta_meta.created_at (status=live)",
        read=_macro_beta_live_at,
    ),
    Produced(
        label="ファクタープレミアムの重み",
        issue_title="[ops] ファクタープレミアムの重みが更新されていない",
        cadence_h=31 * 24.0,
        window_min=run_monthly.WINDOW_MIN,
        batch_label="月次バッチ",
        task_name="financial_app-monthly",
        source="max(recommend_factor_premia.computed_at)",
        read=_factor_premia_at,
    ),
    Produced(
        # 唯一、月次ではなく**夜間バッチ**が更新する producer（#632）。取得が止まっても
        # 既存の業種は残るため、画面にも `*_last_run` にも現れない——2026-09-03 の拡張子変更は
        # 6晩連続の 404 になりながら `exit=0` で通った。業種は `sector_ols` の分割キーなので、
        # 止まっている間に上場した社は業種別回帰の母集団から静かに漏れ続ける。
        label="JPX 業種マスタ",
        issue_title="[ops] JPX 業種マスタが更新されていない",
        cadence_h=24.0,
        window_min=run_nightly.WINDOW_MIN,
        batch_label="夜間バッチ",
        task_name="financial_app-nightly",
        source="app_settings.jpx_industry_last_success",
        read=_jpx_industry_at,
    ),
)


# heavy プラグインの成果物をここで見るか、見ないなら理由は何か。**忘れても失敗として
# 現れない**ので `tests/test_check_batch_freshness.py` が `HEAVY_AUTOMATION` と照合する
# （`plugins/progress.py::PROGRESS_COVERAGE` と同じ作法）。理由を書いた `exempt:` は可・
# 空理由は不可。
PRODUCER_COVERAGE: dict[str, str] = {
    "macro_risk_return": "watched",
    "macro_gbdt": "watched",
    "macro_dlm": "watched",
    "sector_ols":
        "exempt: 日次（run_nightly）で毎晩回り、`nightly_scores.VerificationError` が"
        "「execute は成功したが DB への永続化を確認できなかった」をその場で失敗にする。"
        "2経路で見ると同じ事実に Issue が二重に立つ",
    "macro_enet":
        "exempt: 同上（日次・VerificationError が毎晩検証する）",
    "macro_ensemble":
        "exempt: #570 で退役（hidden・ADR-0044）＝回す相手が居らず、成果物も更新されない",
    "macro_gbdt_rank":
        "exempt: producer を持たない（produced_output=False）。永続化する μ̂ が無い（#362）",
}

PRODUCER_EXEMPT_PREFIX = "exempt:"


def _as_utc(value):
    """DB から返る naive datetime を UTC とみなす。

    `SESSION_FIXES`（ADR-0043）で接続の TimeZone は UTC に固定されており、書き手も
    `datetime.now(timezone.utc)` なので naive 値の意味は UTC。**ここで JST とみなすと
    9時間ぶん若く見え、固着を見逃す**（#565 と同型の壊れ方）。
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def producer_status_of(row: dict) -> str:
    """ok / missing / stale。`Watched` と同じ語彙を使う（読む人が覚え直さなくて済む）。"""
    if row["last"] is None:
        return "missing"
    return "ok" if row["age_h"] <= row["produced"].stale_h else "stale"


def collect_producers(db, now: datetime, read=None) -> list[dict]:
    """成果物の最終更新を読む。**`collect()` とは別関数**にしてある。

    `collect()` の戻りは `/api/morning` が `summarize()` 経由で画面に出しており、行を
    混ぜるとレスポンス構造が変わる。producer の固着は月単位でしか起きないので毎朝の
    カードには載せず、watchdog の起票だけに出す（#504 の判断）。
    """
    rows = []
    for p in PRODUCERS:
        try:
            last = _as_utc((read or (lambda db, p: p.read(db)))(db, p))
        except Exception as e:      # noqa: BLE001 — 1つの読み損ねで全体を落とさない
            rows.append({"produced": p, "last": None, "age_h": None,
                         "status": "unreadable", "error": str(e)[:200]})
            continue
        row = {"produced": p, "last": last, "error": None,
               "age_h": _age_hours(last, now)}
        row["status"] = producer_status_of(row)
        rows.append(row)
    return rows
