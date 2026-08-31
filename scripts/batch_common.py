"""ローカル駆動バッチの共通基盤（Issue #503 Phase 2 / #504）。

## なぜ切り出すか

正本がローカルへ移り（#503・ADR-0038）、GHA が回していた定期実行はすべてこちら側へ来た。
日次（`run_nightly.py`）と月次（`run_monthly.py`）は cadence も中身も違うが、**「走らなかった
ことを検知する」ための骨格は同じ**である:

- ステップ間で止めない（片方の失敗で全部落とすと、翌朝分かるのが最初の失敗だけになる）
- `app_settings` へ足跡を残す（失敗より「そもそも起動しなかった」の方が静かで危ない）
- 失敗は `gh issue create` で起票する。**ただし通知の失敗が本業を止めることは無い**
- 長時間ステップは heartbeat を刻む（**無音は「順調」と「死亡」を区別しない**・#522）

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

# 子の待ち合わせ中に経過を刻む間隔（Issue #522）。**無音は「順調」と「死亡」を区別しない**。
HEARTBEAT_SEC = 300.0
# heartbeat 行の目印。END 行の要約（`_tail_since`）はこの行を進捗と見なさない。
HEARTBEAT_MARK = "[heartbeat]"
# END 行の要約を作るためにログの末尾から読むバイト数（Issue #521）。
# 全量を読むと、直結にした意味（数時間ぶんをディスクへ流す）が END 行の瞬間に消える。
TAIL_BYTES = 8192

# ステップの時間予算を超えたときの exit code（Issue #530）。GNU `timeout` の慣例に合わせる。
# 既存の 126（ログハンドルが無い）/ 127（起動できない）と衝突しない値を選んでいる。
TIMEOUT_EXIT = 124
# 予算超過で kill したあと、子が終わるのを待つ猶予（秒）。ここを待たないとゾンビが残る。
KILL_GRACE_SEC = 30.0
# Σ予算と窓の差として最低限空けておく分数（Issue #530）。予算の合計をぴったり窓に
# 合わせると、起動のオーバーヘッドや1ステップの端数で最後のステップが窓から溢れる。
WINDOW_MARGIN_MIN = 30.0


def _mem_line(root_pid: Optional[int] = None) -> str:
    """heartbeat / env 行へ載せるメモリ実測。**測れなくてもバッチを止めない**。

    `root_pid` はステップの子プロセス。**ツリー合計で測る**必要がある——`venv` の
    `python.exe` はランチャースタブで、`Popen` が持つ pid を単体で測ると実体が GB 級でも
    4MB と返る（`sysmem._win_process_tree` 参照）。
    """
    try:
        import sysmem
        return sysmem.format_line(root_pid)
    except Exception as e:      # noqa: BLE001 — 計測は本業ではない。失敗も黙らせず1行で出す
        return f"mem 測定不可（{type(e).__name__}）"


@dataclass(frozen=True)
class Step:
    """1ステップぶんの実行単位。`why` は失敗時のログに出す＝何が止まったかを言葉で残す。

    `budget_min` は**このステップに許す分数**（Issue #530）。None なら無期限。

    予算が要るのは、バッチ全体の窓（タスクスケジューラの `ExecutionTimeLimit`）が
    **1ステップに食い尽くされうる**から。窓の終わりに来る打ち切りは「失敗」として現れず
    （タスクスケジューラがプロセスを止めるだけで `record_footprint` も `notify` も走らない）、
    後ろのステップは走った形跡すら残さずに消える——2026-09-01 の月次で `macro_beta` が
    16時間を食い、`tune:*` 3本が一度も起動しない、というのが実際に起きようとしていた形
    （#512 の実測: GHA 116分に対しローカルは741.5分でも未完走）。

    予算で切れば、それは `TIMEOUT_EXIT` を返す**普通の失敗**になる＝足跡・起票の経路に乗り、
    後続ステップはそのまま走る。
    """
    name: str
    argv: tuple[str, ...]
    why: str
    budget_min: Optional[float] = None


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
    """`subprocess.Popen` を差し替えたテスト用のフォールバック要約行。

    実運用では子の出力をログへ直結する（`Runner.run`）ので `proc.stdout` は None で、
    要約は `_tail_since` がログから読む。テストが stdout 文字列付きの偽プロセスへ
    差し替えたときだけここが効く。
    """
    text = getattr(proc, "stdout", None) or getattr(proc, "stderr", None) or ""
    if not isinstance(text, str):
        return ""
    lines = text.strip().splitlines()
    return lines[-1] if lines else ""


def kill_tree(proc, echo=None) -> None:
    """子を**プロセスツリーごと**終わらせる（Issue #530）。例外は投げない。

    Windows で `proc.kill()` だけでは足りない: venv の `python.exe` は**ランチャースタブ**で、
    CPU を持つ実体は子 PID の側にいる（#512 の調査でスタブの CPU 0秒を見て「ハングしている」と
    誤読しかけた実例がある）。スタブだけ殺すと実体が走り続け、予算で切ったつもりの計算が
    次のステップと CPU を奪い合う。`taskkill /T` でツリーごと落とせば、親の `wait()` も素直に返る。

    POSIX では `terminate()`（SIGTERM）で猶予を与えてから `kill()`。こちらはテストが走る
    CI（ubuntu）の経路でもある。
    """
    def _say(msg: str) -> None:
        if echo:
            echo(msg)

    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=KILL_GRACE_SEC)
            return
        except Exception as e:      # noqa: BLE001 — kill に失敗してもバッチは続ける
            _say(f"[warn] taskkill に失敗（{e}）。kill() へフォールバックする")
    try:
        proc.terminate()
        proc.wait(timeout=KILL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:           # noqa: BLE001
            pass
    except Exception as e:          # noqa: BLE001
        _say(f"[warn] 子を終了できなかった: {e}")


class Runner:
    """ステップ実行とログ出力。テストから差し替えられるよう subprocess を1箇所に閉じる。"""

    def __init__(self, log: Path, echo=print, heartbeat_sec: float = HEARTBEAT_SEC):
        self.log = log
        self.echo = echo
        self.heartbeat_sec = heartbeat_sec
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
        """1行を echo とログの両方へ。**ログへ書けなくても例外にしない**（Issue #521）。

        閉じたハンドル（`with` の外で使い回された Runner）では `write`/`flush` が `ValueError`
        を投げる。ここで漏らすと START 行を書いた時点でステップループごと落ち、足跡も起票も
        走らない＝「黙って走らなかった」を検知するためのモジュールが、それ自体を起こす。
        echo は先に済ませるので、少なくとも標準出力には残る。
        """
        self.echo(line)
        if self._fh is None:
            return
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except (OSError, ValueError):
            pass

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

        **待ち合わせは heartbeat 付き**（Issue #522）。`HEARTBEAT_SEC` ごとに経過を刻むので、
        長時間ステップが無音になっても「まだ待っている」ことがログに残る。ここへ置いたのは
        `Runner.run` が**全ステップを通る唯一の場所**だから——スクリプト側の opt-in にすると
        登録漏れが構造的に起きる（`macro_beta_inference.py` だけが持っていた状態がまさにそれ）。

        刻むのは待っている**親自身**なので、これは「親が生きていて子がまだ終わっていない」の
        直接の証拠になる。子の内部が進んでいるかは別問題で、そちらは子が自分で刻む
        （`macro_beta_inference._heartbeat` は NUTS サンプリング中を刻む＝親＝ステップ生存、
        子＝サンプリング生存の2階建て）。**heartbeat は生存を示すが進行を示さない**ので、
        本当に進んでいるかの裏取りは CPU 時間で行う。

        heartbeat には**子ツリーの常駐メモリと機械の空き物理メモリ**を併記する（`_mem_line`）。
        2026-09-01 の初実走で `tune:macro_risk_return` は 156分間 heartbeat を出し続けながら
        探索候補を1件も進めなかったが、CPU は 39% で余っており空きメモリが 0.6GB まで枯れて
        ページアウトしていた——**所要だけを記録したログからは「遅い」と「止まっている」も、
        その原因も読み取れない**。資源はログに出ていなければ無かったことになる。

        `step.budget_min` があれば**そこで打ち切る**（Issue #530）。待ち時間を
        `min(heartbeat_sec, 残り予算)` にしてあるので、刻みは保ったまま予算ちょうどで起きる。
        超過したら `kill_tree` でツリーごと落とし、`TIMEOUT_EXIT` を返す——**バッチ全体の窓を
        1ステップに食い尽くさせない**ためで、これが無いと窓の終わりにタスクスケジューラが
        黙ってプロセスを止め、後続ステップは走った形跡すら残さずに消える。
        """
        self.write(f"[{utc_now_iso()}] START {step.name}: {step.why}")
        started = datetime.now(timezone.utc)
        if self._fh is None:
            # コンテキストマネージャの外で呼ばれた（＝呼び出し側のバグ）。DEVNULL へ流すと
            # 子の出力が丸ごと消え、END 行も診断ゼロになる（Issue #521）。**走らせない**で
            # 失敗として返し、通常の失敗経路（足跡・起票）に乗せて見えるようにする。
            self.write(f"[{utc_now_iso()}] ERROR {step.name}: "
                       f"ログハンドルが無い（Runner を with で使っていない）ため実行しない")
            return 126
        try:
            self._fh.flush()          # 子が fd へ直接書くので、親のバッファを先に吐く
            pos = self._fh.tell()
        except (OSError, ValueError):
            # 閉じたハンドル（with の外で使い回された）。flush 自体が ValueError を投げる
            # ので、tell と同じ try の中に入れる——ここで漏らすと #521 の穴が別の行で開く。
            pos = None
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            proc = subprocess.Popen(step.argv, cwd=str(ROOT), env=env,
                                    stdout=self._fh, stderr=subprocess.STDOUT)
        except (OSError, ValueError) as e:
            # 閉じたハンドルを渡すと `fileno()` が **ValueError** を投げる（OSError ではない）。
            # 取り逃がすと run_batch のステップループごと落ち、足跡も起票も走らない＝
            # 「黙って走らなかった」を検知するためのモジュールが、それ自体を起こす（#521）。
            self.write(f"[{utc_now_iso()}] ERROR {step.name}: 起動できない: {e}")
            return 127
        # `if step.budget_min` にしない——0 が falsy で「無期限」へ化ける。
        budget_sec = None if step.budget_min is None else max(step.budget_min, 0.0) * 60.0
        killed = False
        while True:
            waited_sec = (datetime.now(timezone.utc) - started).total_seconds()
            wait_for = self.heartbeat_sec
            if budget_sec is not None:
                # 予算ちょうどで起きる。heartbeat の刻みは保ったまま、最後の待ちだけ短くなる。
                wait_for = min(wait_for, max(budget_sec - waited_sec, 0.0))
            try:
                returncode = proc.wait(timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                waited = (datetime.now(timezone.utc) - started).total_seconds()
                if budget_sec is not None and waited >= budget_sec:
                    # 打ち切りの行は**この後**（要約を採ったあと）に書く。先に書くと
                    # `_tail_since` がそれを拾い、END 行の要約が「打ち切った」になって
                    # **子がどこまで進んでいたか**が消える——一番知りたいのはそちら。
                    kill_tree(proc, echo=self.write)
                    killed = True
                    try:
                        returncode = proc.wait(timeout=KILL_GRACE_SEC)
                    except Exception:      # noqa: BLE001 — 反応しない子で END 行を落とさない
                        returncode = TIMEOUT_EXIT
                    break
                self.write(f"{HEARTBEAT_MARK} {step.name} 継続中: 経過 {waited / 60.0:.0f}分"
                           f" | {_mem_line(proc.pid)}")
        tail = self._tail_since(pos) or _proc_tail(proc)
        if killed:
            # 打ち切った子の returncode（Windows なら taskkill の 1、POSIX なら -SIGTERM）は
            # 「なぜ落ちたか」を伝えない。予算超過であることが分かる値へ翻訳する。
            returncode = TIMEOUT_EXIT
            self.write(f"[{utc_now_iso()}] TIMEOUT {step.name}: "
                       f"予算 {step.budget_min:.0f}分を超過したのでツリーごと打ち切った")
        mins = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
        self.write(f"[{utc_now_iso()}] END   {step.name}: exit={returncode} "
                   f"({mins:.1f}分) | {tail[:160]}")
        return returncode

    def _tail_since(self, pos: Optional[int]) -> str:
        """このステップが書いた範囲の最終行。読めなければ空文字（END 行は必ず出す）。

        **末尾 `TAIL_BYTES` だけを読む**（Issue #521）。以前は `pos` から EOF まで丸ごと str へ
        読み、さらに全行の list を作っていた——ログを直結にした理由は「数時間ぶんの出力を
        ディスクへ流す」ことなのに、END 行を書く瞬間にその全量をメモリへ載せ直していた。
        `pipeline` は約3,700社を1社ずつ、月次の `tune:*` は1ステップで数十〜数百MBを吐きうる。

        heartbeat 行は進捗ではないので要約に選ばない。ただし窓内が heartbeat しか無いときは、
        空文字よりマシなので最後の1行をそのまま使う。
        """
        if pos is None:
            return ""
        try:
            self._fh.flush()
            size = self.log.stat().st_size
            start = max(pos, size - TAIL_BYTES)
            with self.log.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read()
        except (OSError, ValueError):
            return ""
        text = chunk.decode("utf-8", errors="replace")
        # 窓の先頭は行の途中で切れている可能性がある（マルチバイトの途中も含む）ので落とす。
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if start > pos and lines:
            lines = lines[1:]
        if not lines:
            return ""
        progress = [ln for ln in lines if HEARTBEAT_MARK not in ln]
        return (progress or lines)[-1]


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
    ]
    # exit code の凡例は**出た値だけ**出す。全部並べると本文が定型文で埋まり、
    # 「今回どれが起きたのか」が読み取りにくくなる。
    legend = {
        TIMEOUT_EXIT: f"{TIMEOUT_EXIT}: ステップの時間予算を超過して打ち切った（#530）。"
                      "バッチ全体の窓を守るための打ち切りで、後続ステップはそのまま走っている",
        126: "126: ログハンドルが無い（`Runner` を `with` で使っていない）ため実行しなかった（#521）",
        127: "127: プロセスを起動できなかった（コマンド・venv・ログハンドルを疑う）",
    }
    hits = [legend[c] for c in sorted(set(results.values())) if c in legend]
    if hits:
        lines += ["", "exit code:"] + [f"- {h}" for h in hits]
    lines += [
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
        # 実走前に接続先とセッションを確認できる（本番へ向いていないかを叩く前に見る）。
        for line in env_lines():
            print(line)
        for s in selected:
            budget = f"[予算 {s.budget_min:.0f}分] " if s.budget_min is not None else "[予算なし] "
            print(f"  {s.name}: {budget}{' '.join(s.argv)}  # {s.why}")
        print("ドライラン（何も実行していない）")
        return 0

    results: dict[str, int] = {}
    with Runner(log) as runner:
        runner.write("=" * 70)
        runner.write(f"[{utc_now_iso()}] {spec.name}開始（正本=ローカル・#503）")
        # **どこで走ったかを最初に残す**（#550）。S4U はセッション0で走り、`.env` も PATH も
        # 対話セッションと変わりうるのに、これまでログから一切読めなかった。
        for line in env_lines():
            runner.write(line)
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


# 値に機微が入りうる環境変数名の手掛かり。**プレフィックス一致で全部出す**方針なので、
# 将来 FINAPP_*_KEY のようなものが増えても素通りしないよう、名前の側で伏せる。
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CRED")


def _session_id() -> str:
    """Windows のセッション ID。S4U はセッション0で走るので対話実行と区別できる。

    S4U 化（#515）以降、**バッチは対話セッションと別の環境で走る**。どちらで走ったかは
    切り分けの最初の分岐なのに、これまでログのどこにも出ていなかった。
    """
    try:
        import ctypes
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return f"{sid.value}{'（S4U/サービス側）' if sid.value == 0 else '（対話）'}"
    except Exception:      # noqa: BLE001 — 診断用の1行のためにバッチを落とさない
        pass
    return "不明"


def env_lines() -> list[str]:
    """実行環境の要点。ログの先頭に置いて「どこで走ったか」を残す（#550）。

    **生の接続文字列は絶対に出さない**（`db_target_info()` の表示名だけ使う）。`.logs` の中身は
    Issue 本文へ貼られうるし、リポジトリは public である。

    なぜ要るか: S4U（#515）でバッチはセッション0で走るようになり、`.env` の解決も PATH も
    対話セッションと変わりうる。**取り落としても書き込み自体は成功する**ので、別の DB を見て
    いても静かに正常終了する——#503 の反転で `render.yaml` に prod を書き忘れ、空の DB に
    繋がって0件に化けた（#508）のと同型で、接続先の食い違いは沈黙する。
    """
    lines = [
        f"[env] python : {sys.executable}"
        f"{' (venv)' if sys.prefix != getattr(sys, 'base_prefix', sys.prefix) else ' (venv ではない)'}",
        f"[env] cwd    : {os.getcwd()}",
    ]
    try:
        from database import db_target_info
        lines.append(f"[env] DB     : {db_target_info().get('db_label', '不明')}")
    except Exception as e:     # noqa: BLE001 — ここで落ちるとバッチが始まらない
        lines.append(f"[env] DB     : 解決できない（{type(e).__name__}: {str(e)[:120]}）")
    lines.append(f"[env] session: {_session_id()}")
    # 資源も「どこで走ったか」の一部。GHA（2コア7GB）とローカルを所要で比べるときの前提で、
    # ここが無いと「ローカルは N 倍遅い」がマシンの話なのかコードの話なのか後から切り分け
    # られない（#512 はこの区別が付かないまま残った）。
    lines.append(f"[env] {_mem_line()}")
    finapp = {k: v for k, v in sorted(os.environ.items()) if k.startswith("FINAPP_")}
    shown = " ".join(
        f"{k}={'***' if any(h in k.upper() for h in _SECRET_HINTS) else v}"
        for k, v in finapp.items())
    lines.append(f"[env] FINAPP : {shown or '(未設定)'}")
    return lines


def window_problem(steps: Sequence[Step], window_min: float,
                   margin_min: float = WINDOW_MARGIN_MIN) -> Optional[str]:
    """ステップ予算がバッチの窓に収まっているか。問題があれば理由の文字列、無ければ None。

    **窓（タスクスケジューラの `ExecutionTimeLimit`）と予算はセットでしか意味を持たない**
    （Issue #530）。片方だけ動かすと、窓を広げたのに予算が古いまま（＝窓を使い切れない）か、
    予算を足したのに窓が足りない（＝最後のステップが黙って打ち切られる）になる。**どちらも
    失敗としては現れない**ので、CI でここを照合する。

    予算の無いステップを許さないのは、1本でも無期限があれば窓の保証がその時点で消えるから。
    """
    missing = [s.name for s in steps if s.budget_min is None]
    if missing:
        return f"予算の無いステップがある（窓の保証が消える）: {', '.join(missing)}"
    total = sum(s.budget_min for s in steps)      # type: ignore[misc]
    if total + margin_min > window_min:
        return (f"Σ予算 {total:.0f}分 + マージン {margin_min:.0f}分 が窓 {window_min:.0f}分 を超える")
    return None


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
    "env_lines", "window_problem",
]
