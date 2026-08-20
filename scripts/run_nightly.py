"""夜間バッチのローカル駆動（Issue #503 Phase 2・ADR-0038）。

## なぜ必要か

正本がローカル PostgreSQL へ移り、GHA の cron を全て止めた（#503）。GHA はクラウドで
走るのでローカル DB へは書けない＝**収集とスコア更新の駆動主体をこちら側へ持ってくる**
必要がある。Windows タスクスケジューラから叩かれるのがこのモジュール。

`.ps1` ではなく Python で実体を書いている理由は2つある: PowerShell だと BOM 無しで
cp932 扱いになって日本語が化ける／`python -c` に渡す文字列のクォートが native exe の
引数で剥がれる、という**実行するまで出ない**罠が2つとも回避できること。もう一つは、
ステップ順や失敗時の扱いを pytest で縛れること（`tests/test_run_nightly.py`）。
`run_nightly.ps1` はこのモジュールを呼ぶだけの薄い起動口である。

## 設計の中心: ステップ間で止めない

収集が落ちてもスコア更新は前日データで走らせ、**両方の結果をログに残す**。片方の失敗で
全部を落とすと、翌朝に分かるのが「最初の失敗」だけになり、原因の切り分けにもう1日かかる。
`nightly_scores.py` が1モデルの失敗で他を止めないのと同じ方針を、1段上へ広げたもの。

## 「走らなかった」ことを検知できるようにする

失敗より **そもそも起動しなかった** 方が静かで危ない（PC がスリープ・タスクが無効化・
venv が壊れた）。失敗は誰も見ていなければ気づけないので、実行のたびに `app_settings` へ
足跡を残す:

- `nightly_last_run`      … 最後に走った時刻（成否によらず）
- `nightly_last_success`  … 最後に全ステップ成功した時刻

GHA の notify-failure（#414）に相当する通知は `gh issue create` で行う。gh CLI は
認証済みの前提で、**使えなくてもバッチは落とさない**（通知の失敗が本業を止めるのは本末転倒）。

実行:
    python -m scripts.run_nightly              # 全ステップ
    python -m scripts.run_nightly --dry-run    # 実行計画だけ出す
    python -m scripts.run_nightly --steps incremental,scores
    python -m scripts.run_nightly --no-issue   # 失敗しても Issue を起票しない

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / ".logs"

# 最終成功・最終実行の足跡を置く app_settings のキー。
KEY_LAST_RUN = "nightly_last_run"
KEY_LAST_SUCCESS = "nightly_last_success"

# 失敗を起票するときのラベル（GHA の notify-failure と同じ運用に載せる）。
ISSUE_LABELS = ("ops", "priority:high")


@dataclass(frozen=True)
class Step:
    """1ステップぶんの実行単位。`why` は失敗時のログに出す＝何が止まったかを言葉で残す。"""
    name: str
    argv: tuple[str, ...]
    why: str


def steps_for(python: str) -> tuple[Step, ...]:
    """実行するステップ列。**順序に意味がある**（鮮度 → スコア）。

    スコアは株価・財務を入力に取るので、収集より先に回すと前日のデータで上書きされる。
    #423 の「鮮度が先」と同じ依存順で、GHA では daily-incremental → nightly-scores の
    workflow_run チェーンがこれを表現していた。ローカルでは並びがその契約になる。
    """
    return (
        Step("incremental", (python, "collector.py", "--incremental"),
             why="株価・財務の差分収集（鮮度の担い手。Yahoo gap-fill を含む）"),
        Step("macro", (python, "collector.py", "--macro"),
             why="マクロ系列の収集（M-1/M-2/M-3 の入力）"),
        Step("scores", (python, "nightly_scores.py"),
             why="sector_ols / macro_enet のスコア更新（producer の永続化）"),
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_path(now: Optional[datetime] = None) -> Path:
    """日次ローテートのログパス。日付は**ローカル時刻**で切る（人が翌朝見るため）。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d")
    return LOG_DIR / f"nightly_{stamp}.log"


class Runner:
    """ステップ実行とログ出力。テストから差し替えられるよう subprocess を1箇所に閉じる。"""

    def __init__(self, log: Path, echo=print):
        self.log = log
        self.echo = echo
        self._fh = None

    def __enter__(self):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.log.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        if self._fh:
            self._fh.close()
        return False

    def write(self, line: str) -> None:
        self.echo(line)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def run(self, step: Step) -> int:
        """1ステップ実行し exit code を返す。**例外は投げない**（次のステップへ進むため）。"""
        self.write(f"[{_utc_now_iso()}] START {step.name}: {step.why}")
        try:
            proc = subprocess.run(step.argv, cwd=str(ROOT), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except OSError as e:
            self.write(f"[{_utc_now_iso()}] ERROR {step.name}: 起動できない: {e}")
            return 127
        if self._fh:
            self._fh.write(proc.stdout or "")
            self._fh.write(proc.stderr or "")
            self._fh.flush()
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]
        self.write(f"[{_utc_now_iso()}] END   {step.name}: exit={proc.returncode} | {tail[0][:160]}")
        return proc.returncode


def record_footprint(results: dict[str, int]) -> Optional[str]:
    """`app_settings` へ足跡を書く。**DB が死んでいてもバッチは落とさない**。

    ここで raise すると「収集は通ったのに記録だけ失敗した」ときに全体が異常終了し、
    翌朝の判断を誤らせる。書けなかった旨を戻り値で返してログに出すに留める。
    """
    try:
        from database import SessionLocal, upsert_setting
    except Exception as e:      # noqa: BLE001
        return f"database を import できない: {e}"
    db = None
    try:
        db = SessionLocal()
        now = _utc_now_iso()
        upsert_setting(db, KEY_LAST_RUN, now)
        if all(code == 0 for code in results.values()):
            upsert_setting(db, KEY_LAST_SUCCESS, now)
        return None
    except Exception as e:      # noqa: BLE001
        return f"app_settings へ書けない: {e}"
    finally:
        if db is not None:
            db.close()


def issue_body(results: dict[str, int], log: Path) -> str:
    failed = [n for n, c in results.items() if c != 0]
    lines = [
        "ローカル夜間バッチ（`scripts/run_nightly.py`）でステップが失敗した。",
        "",
        f"- 実行時刻(UTC): {_utc_now_iso()}",
        f"- ログ: `{log}`",
        "",
        "| step | exit |",
        "|---|---|",
    ]
    lines += [f"| {n} | {c} |" for n, c in results.items()]
    lines += [
        "",
        f"失敗したステップ: {', '.join(failed)}",
        "",
        "> 正本はローカル PostgreSQL（#503・ADR-0038）。GHA からは回していないので、",
        "> このバッチが止まると鮮度も止まる（失敗が GitHub 上に現れないことに注意）。",
    ]
    return "\n".join(lines)


def notify(results: dict[str, int], log: Path, run=subprocess.run) -> Optional[str]:
    """失敗を Issue として起票する。gh が無い/失敗しても None 以外を返すだけで落とさない。"""
    failed = [n for n, c in results.items() if c != 0]
    if not failed:
        return None
    argv = ["gh", "issue", "create",
            "--title", f"[ops] ローカル夜間バッチ失敗: {', '.join(failed)}",
            "--body", issue_body(results, log)]
    for label in ISSUE_LABELS:
        argv += ["--label", label]
    try:
        proc = run(argv, cwd=str(ROOT), capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
    except OSError as e:
        return f"gh を起動できない: {e}"
    if proc.returncode != 0:
        return f"gh issue create が失敗: {(proc.stderr or '').strip()[:200]}"
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="夜間バッチのローカル駆動（#503）")
    ap.add_argument("--steps", help="実行するステップをカンマ区切りで限定（既定は全部）")
    ap.add_argument("--dry-run", action="store_true", help="実行計画だけ出して何もしない")
    ap.add_argument("--no-issue", action="store_true", help="失敗しても Issue を起票しない")
    args = ap.parse_args(argv)

    # 正本はローカル。**明示的に立てる**——親プロセスが prod を持っていても引きずらない。
    os.environ["FINAPP_DB_TARGET"] = "local"
    os.environ.setdefault("FINAPP_JOB", "nightly-local")

    all_steps = steps_for(sys.executable)
    if args.steps:
        picked = [s.strip() for s in args.steps.split(",") if s.strip()]
        unknown = [p for p in picked if p not in {s.name for s in all_steps}]
        if unknown:
            raise SystemExit(f"中止: 未知のステップ {unknown}"
                             f"（有効なのは {[s.name for s in all_steps]}）")
        all_steps = tuple(s for s in all_steps if s.name in picked)

    log = log_path()
    if args.dry_run:
        print(f"log: {log}")
        for s in all_steps:
            print(f"  {s.name}: {' '.join(s.argv)}  # {s.why}")
        print("ドライラン（何も実行していない）")
        return 0

    results: dict[str, int] = {}
    with Runner(log) as runner:
        runner.write("=" * 70)
        runner.write(f"[{_utc_now_iso()}] 夜間バッチ開始（正本=ローカル・#503）")
        for step in all_steps:
            results[step.name] = runner.run(step)      # 失敗しても次へ進む
        runner.write("-" * 70)
        for name, code in results.items():
            runner.write(f"  {'OK    ' if code == 0 else 'FAILED'} {name} (exit={code})")

        note = record_footprint(results)
        if note:
            runner.write(f"[warn] 足跡を残せなかった: {note}")
        if not args.no_issue:
            note = notify(results, log)
            if note:
                runner.write(f"[warn] 通知できなかった: {note}")
        runner.write(f"[{_utc_now_iso()}] 夜間バッチ終了")

    return sum(1 for c in results.values() if c != 0)


if __name__ == "__main__":
    raise SystemExit(main())
