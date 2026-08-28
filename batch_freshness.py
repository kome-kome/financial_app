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
from scripts import run_monthly, run_nightly       # noqa: E402

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
