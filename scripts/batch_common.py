"""ローカル駆動バッチの共通基盤（Issue #503 Phase 2 / #504）。

## なぜ切り出すか

正本がローカルへ移り（#503・ADR-0038）、GHA が回していた定期実行はすべてこちら側へ来た。
日次（`run_nightly.py`）と月次（`run_monthly.py`）は cadence も中身も違うが、**「走らなかった
ことを検知する」ための骨格は同じ**である:

- ステップ間で止めない（片方の失敗で全部落とすと、翌朝分かるのが最初の失敗だけになる）
- `app_settings` へ足跡を残す（失敗より「そもそも起動しなかった」の方が静かで危ない）
- 失敗は `gh issue create` で起票する。**ただし通知の失敗が本業を止めることは無い**

この骨格を2本にコピーすると、片方だけ直す事故が必ず起きる（このリポジトリでは
`XBRL_MAP` の手書き・`DEFAULT_MACRO_FEATURES` の並び・`27週` の導出などで繰り返し
「唯一の源を持つ」形へ寄せてきた）。ここが唯一の源になる。

## 各バッチが持つのは「何を回すか」だけ

`BatchSpec` に名前・ログ接頭辞・`app_settings` のキー・Issue の文面を、`steps_for()` に
実行するコマンド列を書く。それ以外（順序を守る・失敗を数える・ログを綴じる）は本モジュール。

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
from typing import Callable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / ".logs"

# 失敗を起票するときのラベル（GHA の notify-failure と同じ運用に載せる）。
ISSUE_LABELS = ("ops", "priority:high")


@dataclass(frozen=True)
class Step:
    """1ステップぶんの実行単位。`why` は失敗時のログに出す＝何が止まったかを言葉で残す。"""
    name: str
    argv: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class BatchSpec:
    """バッチ1本ぶんの識別情報。実行するコマンド列は `steps` として別に渡す。"""
    name: str            # ログ・引数説明に出す名前（「夜間バッチ」など）
    log_prefix: str      # .logs/<prefix>_YYYYMMDD.log
    key_run: str         # app_settings: 最後に走った時刻（成否によらず）
    key_success: str     # app_settings: 最後に全ステップ成功した時刻
    job_label: str       # FINAPP_JOB（Egress 台帳の帰属ラベル・#478）
    issue_title: str     # 失敗時の Issue 件名。"{failed}" を含めること
    headline: str        # Issue 本文の1行目


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_path(prefix: str, now: Optional[datetime] = None) -> Path:
    """日次ローテートのログパス。日付は**ローカル時刻**で切る（人が翌朝見るため）。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d")
    return LOG_DIR / f"{prefix}_{stamp}.log"


def _proc_tail(proc) -> str:
    """`subprocess.run` を差し替えたテスト用のフォールバック要約行。

    実運用では子の出力をログへ直結する（`Runner.run`）ので `proc.stdout` は None で、
    要約は `_tail_since` がログから読む。テストが stdout 文字列付きの偽プロセスへ
    差し替えたときだけここが効く。
    """
    text = getattr(proc, "stdout", None) or getattr(proc, "stderr", None) or ""
    if not isinstance(text, str):
        return ""
    lines = text.strip().splitlines()
    return lines[-1] if lines else ""


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
        """1ステップ実行し exit code を返す。**例外は投げない**（次のステップへ進むため）。

        子の出力は**ログファイルへ直結**する（`stdout=self._fh` / `stderr=STDOUT`）。
        `capture_output=True` で完了まで溜め込んでいた頃は、途中で親ごと落ちると
        **START 行だけが残って出力は全部消えた**——2026-08-21 の macro_beta がまさにこれで、
        7時間走った形跡がどこにも残らず「走っているのか死んでいるのか」を12時間区別できなかった。
        直結なら kill されてもそこまでの出力はディスクに残る。

        `PYTHONUNBUFFERED` / `PYTHONIOENCODING` を子へ渡すのが対で必要:
          - 前者が無いと子（Python）はブロックバッファリングし、結局まとめて書く＝直結の意味が消える
          - 後者が無いと Windows の子は cp932 で書き、utf-8 で開いたこのログが化ける

        末尾行（END 行に載せる要約）は proc.stdout ではなく**書かれたログの続きから**読む。
        """
        self.write(f"[{utc_now_iso()}] START {step.name}: {step.why}")
        started = datetime.now(timezone.utc)
        pos = None
        if self._fh:
            self._fh.flush()          # 子が fd へ直接書くので、親のバッファを先に吐く
            try:
                pos = self._fh.tell()
            except (OSError, ValueError):
                pos = None
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            proc = subprocess.run(step.argv, cwd=str(ROOT), env=env,
                                  stdout=self._fh or subprocess.DEVNULL,
                                  stderr=subprocess.STDOUT)
        except OSError as e:
            self.write(f"[{utc_now_iso()}] ERROR {step.name}: 起動できない: {e}")
            return 127
        tail = self._tail_since(pos) or _proc_tail(proc)
        mins = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
        self.write(f"[{utc_now_iso()}] END   {step.name}: exit={proc.returncode} "
                   f"({mins:.1f}分) | {tail[:160]}")
        return proc.returncode

    def _tail_since(self, pos: Optional[int]) -> str:
        """このステップが書いた範囲の最終行。読めなければ空文字（END 行は必ず出す）。"""
        if pos is None:
            return ""
        try:
            self._fh.flush()
            with self.log.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                lines = fh.read().strip().splitlines()
        except OSError:
            return ""
        return lines[-1] if lines else ""


def record_footprint(results: dict[str, int], key_run: str, key_success: str) -> Optional[str]:
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
        now = utc_now_iso()
        upsert_setting(db, key_run, now)
        if all(code == 0 for code in results.values()):
            upsert_setting(db, key_success, now)
        return None
    except Exception as e:      # noqa: BLE001
        return f"app_settings へ書けない: {e}"
    finally:
        if db is not None:
            db.close()


def issue_body(results: dict[str, int], log: Path, headline: str) -> str:
    failed = [n for n, c in results.items() if c != 0]
    lines = [
        headline,
        "",
        f"- 実行時刻(UTC): {utc_now_iso()}",
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


def notify(results: dict[str, int], log: Path, title: str, body: str,
           run=subprocess.run) -> Optional[str]:
    """失敗を Issue として起票する。gh が無い/失敗しても None 以外を返すだけで落とさない。"""
    failed = [n for n, c in results.items() if c != 0]
    if not failed:
        return None
    argv = ["gh", "issue", "create", "--title", title.format(failed=", ".join(failed)),
            "--body", body]
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


@dataclass(frozen=True)
class Hooks:
    """バッチ側のモジュール関数。**main の中で解決して渡す**——そうすればテストが
    モジュール属性を差し替えたとき（monkeypatch）にそのまま効く。"""
    log_path: Callable[[], Path]
    record_footprint: Callable[[dict[str, int]], Optional[str]]
    notify: Callable[[dict[str, int], Path], Optional[str]]


def build_parser(spec: BatchSpec, step_names: Sequence[str]) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=f"{spec.name}のローカル駆動（#503）")
    ap.add_argument("--steps", help=f"実行するステップをカンマ区切りで限定"
                                    f"（既定は全部: {','.join(step_names)}）")
    ap.add_argument("--dry-run", action="store_true", help="実行計画だけ出して何もしない")
    ap.add_argument("--no-issue", action="store_true", help="失敗しても Issue を起票しない")
    return ap


def select_steps(steps: Sequence[Step], picked_csv: Optional[str]) -> tuple[Step, ...]:
    if not picked_csv:
        return tuple(steps)
    picked = [s.strip() for s in picked_csv.split(",") if s.strip()]
    known = {s.name for s in steps}
    unknown = [p for p in picked if p not in known]
    if unknown:
        raise SystemExit(f"中止: 未知のステップ {unknown}"
                         f"（有効なのは {[s.name for s in steps]}）")
    return tuple(s for s in steps if s.name in picked)


def run_batch(spec: BatchSpec, steps: Sequence[Step], hooks: Hooks,
              argv: Optional[Sequence[str]] = None) -> int:
    """バッチ本体。戻り値は**失敗したステップ数**（0 なら全部成功）。"""
    ap = build_parser(spec, [s.name for s in steps])
    args = ap.parse_args(argv)

    # 正本はローカル。**明示的に立てる**——親プロセスが prod を持っていても引きずらない。
    os.environ["FINAPP_DB_TARGET"] = "local"
    os.environ.setdefault("FINAPP_JOB", spec.job_label)

    selected = select_steps(steps, args.steps)

    log = hooks.log_path()
    if args.dry_run:
        print(f"log: {log}")
        for s in selected:
            print(f"  {s.name}: {' '.join(s.argv)}  # {s.why}")
        print("ドライラン（何も実行していない）")
        return 0

    results: dict[str, int] = {}
    with Runner(log) as runner:
        runner.write("=" * 70)
        runner.write(f"[{utc_now_iso()}] {spec.name}開始（正本=ローカル・#503）")
        for step in selected:
            results[step.name] = runner.run(step)      # 失敗しても次へ進む
        runner.write("-" * 70)
        for name, code in results.items():
            runner.write(f"  {'OK    ' if code == 0 else 'FAILED'} {name} (exit={code})")

        note = hooks.record_footprint(results)
        if note:
            runner.write(f"[warn] 足跡を残せなかった: {note}")
        if not args.no_issue:
            note = hooks.notify(results, log)
            if note:
                runner.write(f"[warn] 通知できなかった: {note}")
        runner.write(f"[{utc_now_iso()}] {spec.name}終了")

    return sum(1 for c in results.values() if c != 0)


def models_from_steps(steps: Sequence[Step], flag: str = "--model") -> tuple[str, ...]:
    """ステップの argv から `--model X` を抜き出す。

    「このバッチが実際に回すモデル」を**列挙で二重に持たない**ための関数。
    `HEAVY_AUTOMATION`（ADR-0031）の登録が実体を指しているかを CI で照合するのに使う。
    """
    found: list[str] = []
    for step in steps:
        argv = list(step.argv)
        for i, token in enumerate(argv[:-1]):
            if token == flag:
                found.append(argv[i + 1])
    return tuple(found)


__all__ = [
    "ROOT", "LOG_DIR", "ISSUE_LABELS", "Step", "BatchSpec", "Hooks", "Runner",
    "utc_now_iso", "log_path", "record_footprint", "issue_body", "notify",
    "build_parser", "select_steps", "run_batch", "models_from_steps",
]
