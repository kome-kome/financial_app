"""ローカル駆動バッチの足跡が止まっていないかを判定し、止まっていれば Issue を起票する（#515 手順3）。

    python -m scripts.check_batch_freshness                # 判定（停止していれば起票 + exit 2）
    python -m scripts.check_batch_freshness --warn-only    # 常に exit 0（調査・閾値調整用）
    python -m scripts.check_batch_freshness --dry-run      # 起票せず本文を表示（判定は変えない）
    python -m scripts.check_batch_freshness --now <ISO>    # 判定時刻を差し替える（欠落の再現）

## なぜ要るのか

2026-08-21、タスクスケジューラ起動の夜間バッチが `0xC000013A`（STATUS_CONTROL_C_EXIT）で
即死し、**ログも足跡も1バイトも残らなかった**。足跡（`app_settings`）は「走っていない」ことを
正しく示していたが、**読む側が存在しなかった**ので、別件の調査でたまたま気づくまで丸1日
誰も知らなかった。**バッチが起動前に死ぬと failure が出ない**＝`batch_common.notify` の
起票経路は発火しない。ADR-0031「登録があること != 動いていること」がそのまま出た形で、
ここがその「見る側」。1日飛ぶと翌日の Yahoo gap-fill が 4,000社超へ膨らむ（#475）＝
損害は当日の鮮度で終わらない。

ADR-0040 が「窓の終わりの打ち切りは failure として現れない」と書き残した穴もここで塞がる。
打ち切られると `record_footprint` に到達しない＝足跡が**まったく進まない**ので、鮮度として現れる。

## 閾値は約束から導出する（実測から逆算しない）

各バッチは「cadence ごとに1回、窓の中のどこかで足跡を書く」と約束している。健全な世界で
経過が取りうる上限はそのまま **`cadence + 窓`**。両項ともリポジトリに定数として在るので、
**窓を広げれば閾値も自動で広がる**（ADR-0040 の「窓と予算はセット」を1段外へ延ばした形）。

副産物として「実行中に警告を出さない」が構造的に成立する——閾値に含まれる窓の項が、
そのまま「まだ走っていてよい時間」の許容だからで、プロセスの生存を覗く必要がない。
2026-08 の実測もこれと整合する: 名目 17:20 に対し実起動は 17:51（+31分）〜19:01（+1h41m）で
（`WakeToRun` 未設定 + `StartWhenAvailable` で PC がスリープなら復帰後に走る）、
**名目時刻を基準にすると誤検知する**が、経過ベースなら遅延は窓の項が吸収する。

## 見るのは `*_last_run` であって `*_last_success` ではない

`monthly_last_success` は**設計上ずっと古い**。`run_monthly.py:111` のとおり #512 が解けるまで
`macro_beta` は毎月 `exit=124` で落ちる想定で、成功で鳴らすと**恒久的に open な Issue**が
できる＝「初日から常時 failure」で通知そのものが信用されなくなる。加えて「走ったが失敗した」は
バッチ自身の起票が既にカバーしており、それが破れる唯一の経路（gh の不調）では**この watchdog も
起票できない**。だから成功側は報告と Issue 本文に必ず載せる（読む人が「走っていない」と
「走ったが通っていない」を1秒で切り分けられる）が、判定には使わない。

## なぜ GitHub Actions ではないのか

正本はローカル PostgreSQL（#503・ADR-0038）で、**GHA からこの DB は見えない**。
`check_egress_health.py` は GHA で走り `notify-failure.yml` が起票を担うが、その経路は使えない。

## watchdog 自身が走らなかったら

**自分も監視対象に含める。** 読んでから書くので、次に走ったとき自分の沈黙期間を検出できる
——リアルタイムには自分の死を検知できないが、事後に隠しはしない。PC が数日止まって復帰すると
`StartWhenAvailable` で夜間バッチと watchdog の両方が追いつくが**順序は保証されない**。
夜間バッチが先だと `nightly_last_run` が更新され、飛んだ日は足跡から永久に消える
（足跡は最後の1回しか持たない）。自己足跡だけがその期間を証言できる。
残余（PC の恒久停止・タスク削除・venv 消滅）は検知できない。最後の環は人で、それは既に
`/api/morning` の as-of ブロック（#416/#417）が担っている。

読取のみ（自分の足跡1行の upsert を除く）。出力は ASCII 記号のみ（Windows cp932 対策）。

実行: `python -m scripts.check_batch_freshness`（`-m` 必須）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

# **パスを明示する。** 裸の load_dotenv() は cwd から上へ探すが、S4U（セッション0）では
# cwd の保証が弱い。`.env` を取り落とすと DATABASE_URL_LOCAL が既定へ落ちて**別の DB を読む**
# ——別 DB の足跡は当然古いので、毎日確実に誤警報が出る。
load_dotenv(ROOT / ".env")

# `database` は **import 時**に resolve_database_url(os.environ) を評価する（database.py:132）。
# 後から立てても遅いので、import より前にここで固定する。
os.environ["FINAPP_DB_TARGET"] = "local"
os.environ.setdefault("FINAPP_EGRESS_LEDGER", "0")
os.environ.setdefault("FINAPP_JOB", "watchdog")

from scripts import batch_common as bc                       # noqa: E402
# 判定ロジック（`Watched` / `WATCHED` / `collect`）は `batch_freshness.py`（ルート）へ
# 切り出し、`/api/morning` と共有する（#561）。**このモジュールは import 時に
# `FINAPP_DB_TARGET` を書き換える**ので、API プロセスからは import させない——接続先の
# 食い違いは #508 と同型で静かに壊れる。ここは起票・CLI・ログだけを担う。
from batch_freshness import (                                # noqa: E402,F401
    KEY_LAST_RUN, SELF_WINDOW_MIN, WATCHED, Watched, _parse, collect, db_label, status_of,
)

EXIT_UNHEALTHY = 2
# 「問題を見つけているのに誰にも伝えられていない」は最も静かな故障で、他のどこにも現れない。
# タスクスケジューラの LastTaskResult が唯一の常時観測点なので、そこで見分けられるようにする。
EXIT_NOTIFY_FAILED = 3


# `gh auth status` の待ち時間。ネットワークが詰まっても watchdog の窓（15分）を食わない値。
GH_TIMEOUT_SEC = 20.0


DB_ERROR_TITLE = "[ops] watchdog がローカル DB を読めない"
GH_ERROR_TITLE = "[ops] watchdog が gh を使えない（通知経路が死んでいる）"


class _Echo:
    """標準出力と `.logs/watchdog_YYYYMMDD.log` の両方へ。

    タスクスケジューラ経由では **stdout はどこにも出ない**ので、ログを自分で開かないと
    「何を見て何を判断したか」が1バイトも残らない。書けなくても落とさない
    （`Runner.write` と同じ作法——付帯処理の失敗で本業を止めない）。
    """

    def __init__(self, fh=None):
        self._fh = fh

    def __call__(self, line: str) -> None:
        print(line)
        if self._fh is None:
            return
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except (OSError, ValueError):
            pass


def check_gh(run=subprocess.run, which=None) -> Optional[str]:
    """通知経路の到達性。使えるなら None、駄目なら理由を返す。

    **健全なときこそ確かめる必要がある。** 異常を検出したときにしか `gh` を呼ばない設計だと、
    「通知経路が死んでいる」ことは**異常が起きた当日に初めて分かる**——つまり一番届いてほしい
    回に届かない。#515 が「登録の成功 != 実行の成功」だったのと同じ形で、ここは
    **実行の成功 != 通知が届く**。

    S4U はセッション0で走るので PATH も `hosts.yml` の解決も対話セッションと変わりうる。
    実際、2026-08-26 の初回疎通では健全だったために `gh` が1度も起動されず、
    通知経路は未検証のまま残った。
    """
    which = which or shutil.which
    if which("gh") is None:
        return "PATH から gh が見つからない（S4U はセッション0で走るので環境が対話時と違う）"
    try:
        proc = run(["gh", "auth", "status"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=GH_TIMEOUT_SEC)
    except OSError as e:
        return f"gh を起動できない: {e}"
    except subprocess.TimeoutExpired:
        return f"gh auth status が {GH_TIMEOUT_SEC:.0f}秒で返らない"
    if proc.returncode != 0:
        return f"gh の認証が通っていない: {((proc.stderr or '') + (proc.stdout or ''))[:200]}"
    return None


def problems(snap: dict) -> list[dict]:
    """起票に値する事象だけを返す（空なら健全）。対象ごとに1件＝Issue も対象ごとに1本。"""
    if snap["db_error"]:
        return [{"title": DB_ERROR_TITLE, "row": None, "status": "db_error",
                 "message": f"ローカル DB を読めない: {snap['db_error']}"}]
    found = []
    if snap.get("gh_error"):
        # **これ自体は起票できない**（起票の手段が死んでいる）。notify が失敗して exit 3 になり、
        # タスクの LastTaskResult とログだけが観測点として残る——それが正しい姿で、
        # 「通知できないことを黙る」よりは痕跡が残る。
        found.append({"title": GH_ERROR_TITLE, "row": None, "status": "gh_error",
                      "message": f"通知経路が使えない: {snap['gh_error']}"})
    for row in snap["rows"]:
        w, status = row["watched"], row["status"]
        if status == "ok":
            continue
        if status == "missing":
            if not w.missing_is_problem:
                continue
            message = (f"{w.label}: 足跡 `{w.key_run}` が app_settings に無い"
                       f"（一度も走っていないか、行が消えた）")
        elif status == "unreadable":
            message = (f"{w.label}: 足跡 `{w.key_run}` を日時として読めない"
                       f"（値: {row['run_raw']!r}）")
        else:
            message = (f"{w.label}: 最後の実行が {row['run_age_h']:.1f}時間前"
                       f"（閾値 {w.stale_h:.1f}時間 = cadence {w.cadence_h:.0f}時間"
                       f" + 窓 {w.window_min / 60.0:.1f}時間）")
        found.append({"title": w.issue_title, "row": row,
                      "status": status, "message": message})
    return found


def _pad(text: str, width: int) -> str:
    """表示幅で揃える。`f"{s:<14}"` は文字数で数えるので全角混じりだと列が崩れる。"""
    shown = sum(2 if unicodedata.east_asian_width(c) in "FWA" else 1 for c in text)
    return text + " " * max(0, width - shown)


def _fmt_age(age_h: Optional[float]) -> str:
    if age_h is None:
        return "未設定"
    if age_h >= 48:
        return f"{age_h / 24:.1f}日前"
    return f"{age_h:.1f}時間前"


def format_report(snap: dict) -> list[str]:
    lines = [
        f"== バッチ鮮度（判定時刻 {snap['now'].isoformat(timespec='seconds')}） ==",
        f"接続先: {snap['db_label']}",
        # **健全な回にも必ず出す。** 通知経路の生死は、異常が起きた日に初めて分かるのでは遅い。
        "通知経路: " + ("gh 到達可" if not snap.get("gh_error") else f"使えない（{snap['gh_error']}）"),
    ]
    if snap["db_error"]:
        lines.append(f"DB を読めない: {snap['db_error']}")
        return lines
    for row in snap["rows"]:
        w = row["watched"]
        success = "-" if w.key_success is None else _fmt_age(row["success_age_h"])
        lines.append(
            f"{_pad(w.label, 16)}: 実行 {_pad(_fmt_age(row['run_age_h']), 12)}"
            f"成功 {_pad(success, 12)}[閾値 {w.stale_h:.1f}時間]")
    return lines


def issue_body(problem: dict, snap: dict) -> str:
    """起票・追記の本文。**同じ本文をコメントにも使う**（追記のたびに現況が載る）。

    連続 N 日目のカウンタは持たない——足跡の生値が動かないまま経過だけ伸びるコメントが
    並ぶこと自体が経過の証拠で、状態を持つとその状態が壊れたときに嘘をつく。
    """
    head = [
        f"判定時刻: {snap['now'].isoformat(timespec='seconds')}",
        f"接続先: {snap['db_label']}",
        "",
        f"**{problem['message']}**",
        "",
    ]
    row = problem["row"]
    if problem["status"] == "gh_error":
        # この本文が Issue になることは無い（起票の手段が死んでいる）。ログに残す用。
        body = head + [
            "**この検出自体は起票できない**——通知の手段そのものが使えないため。"
            "痕跡はタスクの `LastTaskResult`（exit 3）と `.logs/watchdog_YYYYMMDD.log` にだけ残る。",
            "",
            "S4U はセッション0で走るので、PATH も `gh` の `hosts.yml` の解決も対話セッションと"
            "変わりうる。対話セッションで `gh auth status` が通っても、"
            "**セッション0で通るとは限らない**。",
        ]
    elif row is None:
        body = head + [
            "ローカル PostgreSQL へ接続できないため足跡を読めなかった。"
            "Postgres が落ちているなら夜間バッチも同時に死んでいる。",
        ]
    else:
        w = row["watched"]
        body = head + [
            "| 項目 | 値 |",
            "|---|---|",
            f"| `{w.key_run}` | {row['run_raw'] or '(未設定)'} |",
            f"| `{w.key_success}` | {row['success_raw'] or '(未設定)'} |"
            if w.key_success else "| 最終成功 | - |",
            f"| 閾値 | {w.stale_h:.1f}時間（cadence {w.cadence_h:.0f}時間"
            f" + 窓 {w.window_min / 60.0:.1f}時間） |",
            f"| 検出 | {problem['status']} |",
            "",
            "### 確認すること",
            "",
            f"1. `Get-ScheduledTaskInfo -TaskName {w.task_name}` の `LastTaskResult`"
            f"（`0xC000013A` なら #515 と同型・`267009` は実行中）",
            f"2. `.logs/{w.log_prefix}_YYYYMMDD.log` の有無と中身。"
            "**実行中はエクスプローラ上のサイズが 0 のまま**なので中身を直接読むこと",
            "3. `app_settings` を直接クエリして足跡を確認（ログの見た目では判定しない）",
        ]
    return "\n".join(body + [
        "",
        "---",
        "この Issue は `scripts/check_batch_freshness.py` による自動起票（#515 手順3）。",
        "同じ対象の再検出は新規起票せず本 Issue へコメント追記される。",
        "**復旧を確認したらクローズしてください**"
        "（open のまま放置すると次の欠落がコメントに埋もれます）。",
    ])


def _gh(run, argv: Sequence[str]) -> subprocess.CompletedProcess:
    return run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace")


def _find_open_issue(run, title: str) -> tuple[Optional[int], Optional[str]]:
    """同一タイトルの open Issue 番号を返す。見つからない・失敗なら None。

    **ラベルで絞らない**——誰かが `ops` を外した瞬間に重複起票が始まる
    （`.github/workflows/notify-failure.yml:165` の理由をそのまま継ぐ）。突き合わせは
    `--jq` に任せず Python 側で行う（シェル依存を持ち込まずテストできる）。
    **listing が失敗したら新規起票へ倒す**（重複より沈黙の方が悪い）。
    """
    proc = _gh(run, ["gh", "issue", "list", "--state", "open", "--limit", "200",
                     "--json", "number,title"])
    if proc.returncode != 0:
        return None, f"gh issue list が失敗（新規起票へ倒す）: {(proc.stderr or '')[:200]}"
    try:
        issues = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        return None, f"gh issue list の出力を JSON として読めない（新規起票へ倒す）: {e}"
    for issue in issues:
        if issue.get("title") == title:
            return issue.get("number"), None
    return None, None


def notify(found: list[dict], snap: dict, say=print, run=subprocess.run,
           dry_run: bool = False) -> list[str]:
    """対象ごとに起票（既存があれば追記）。**失敗しても例外にしない**。

    `batch_common.notify` と同じ思想で、通知の失敗が判定そのものを握り潰さないようにする。
    """
    errors: list[str] = []
    for problem in found:
        title, body = problem["title"], issue_body(problem, snap)
        if dry_run:
            say(f"[dry-run] title: {title}")
            for line in body.splitlines():
                say(f"[dry-run] | {line}")
            continue
        try:
            existing, warn = _find_open_issue(run, title)
            if warn:
                errors.append(warn)
            if existing is not None:
                proc = _gh(run, ["gh", "issue", "comment", str(existing), "--body", body])
                action = f"既存 Issue #{existing} へ追記"
            else:
                argv = ["gh", "issue", "create", "--title", title, "--body", body]
                for label in bc.ISSUE_LABELS:
                    argv += ["--label", label]
                proc = _gh(run, argv)
                action = "新規起票"
        except OSError as e:
            errors.append(f"gh を起動できない: {e}")
            continue
        if proc.returncode != 0:
            errors.append(f"{action}に失敗: {(proc.stderr or '')[:200]}")
        else:
            say(f"[鮮度] {action}: {title}")
    return errors


def _upsert_setting(db, key: str, value: str) -> None:
    from database import upsert_setting
    upsert_setting(db, key, value)


def record_self(db) -> Optional[str]:
    """watchdog 自身の足跡。**読んだ後に書く**（逆順だと自己監視が永久に沈黙する）。"""
    try:
        _upsert_setting(db, KEY_LAST_RUN, bc.utc_now_iso())
    except Exception as e:      # noqa: BLE001 — 足跡の失敗で watchdog を落とさない
        return f"watchdog の足跡を残せなかった: {e}"
    return None


def _open_session():
    from database import SessionLocal
    return SessionLocal()


def _log_path() -> Path:
    """ログの置き場。**継ぎ目にしてあるのはテストが差し替えるため。**

    ここを `bc.log_path("watchdog")` の直呼びにしていた間、`main()` を通るテストが
    **本番の `.logs/watchdog_YYYYMMDD.log` へ書き込んでいた**（2026-08-26 に実測）。
    このログは「異常時に何を見て判断したか」の唯一の記録なので、`pytest` を回すたびに
    偽の行——固定した判定時刻、フェイクの接続先、意図的に落とした DB——が混ざると、
    運用中に読んでも何が本物か分からなくなる。テストは副作用を持ってはいけない。
    """
    return bc.log_path("watchdog")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ローカル駆動バッチの鮮度ゲート（#515）")
    ap.add_argument("--warn-only", action="store_true",
                    help="停止を検出しても exit 0（誤検知の調査・閾値チューニング用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="起票せず本文を表示する（判定と exit code は変えない）")
    ap.add_argument("--no-footprint", action="store_true",
                    help="自分の足跡を書かない（検証時に自己監視の状態を汚さない）")
    ap.add_argument("--now", default=None,
                    help="判定時刻を ISO8601 で差し替える（欠落状態の再現・検証用）")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc) if not args.now else _parse(args.now)
    if now is None:
        print(f"[鮮度] --now を日時として読めない: {args.now!r}")
        return 1

    try:
        fh = _log_path().open("a", encoding="utf-8")
    except OSError:
        fh = None
    say = _Echo(fh)
    try:
        db = None
        footprint_error = None
        try:
            db = _open_session()
            snap = collect(db, now)
            if not (args.dry_run or args.no_footprint):
                footprint_error = record_self(db)
        except Exception as e:      # noqa: BLE001 — 接続不能も「判定結果」として扱う
            snap = {"now": now, "rows": [], "db_error": str(e)[:300],
                    "db_label": db_label(), "gh_error": None}
        finally:
            if db is not None:
                db.close()

        # 通知経路は**健全な回にも**確かめる（--dry-run は gh を叩かないので見送る）。
        # ここを異常検出時だけにすると、通知が死んでいることは一番届いてほしい回に判明する。
        snap["gh_error"] = None if args.dry_run else check_gh()

        if args.now:
            say(f"[鮮度] --now で判定時刻を差し替えている: {args.now}")
        for line in format_report(snap):
            say(line)
        if footprint_error:
            say(f"[warn] {footprint_error}")

        found = problems(snap)
        if not found:
            say("[鮮度] OK")
            return 0

        for problem in found:
            say(f"[鮮度] 停止: {problem['message']}")

        if snap.get("gh_error") and not args.dry_run:
            # 通知の手段が死んでいると分かっている。その手段で起票を試すのは無駄なので
            # 叩かない——**痕跡は exit code とログにだけ残す**、が唯一できること。
            errors = [f"通知経路が使えないため起票を試みない: {snap['gh_error']}"]
        else:
            errors = notify(found, snap, say=say, run=subprocess.run, dry_run=args.dry_run)
        for error in errors:
            say(f"[warn] {error}")

        if args.warn_only:
            say("[鮮度] --warn-only のため exit 0")
            return 0
        # 起票が届かなくても Windows 側に痕跡を残す（gh 認証切れの保険）。
        code = EXIT_NOTIFY_FAILED if errors else EXIT_UNHEALTHY
        say(f"[鮮度] exit {code}（タスクの LastTaskResult に残る）")
        return code
    finally:
        if fh is not None:
            fh.close()


if __name__ == "__main__":
    sys.exit(main())
