"""Supabase の枠消費（Egress 累計・Database Size）を判定し、超過が近ければ非ゼロ終了する（#478 / #483）。

    python -m scripts.check_egress_health              # 判定（閾値超過なら exit 2）
    python -m scripts.check_egress_health --warn-only  # 常に exit 0（調査・閾値調整用）

## なぜ独立ジョブなのか

Egress 超過は **restricted になるまで誰も気づかない**。2026-07（61.2GB）・
2026-08（7.312GB）とも事後に発覚した。`notify-failure.yml` は failure しか拾わず、
**枠の消費は failure を出さない**。ここが非ゼロ終了すれば workflow_run の failure として
Issue が自動起票される＝**枠の消費を failure へ翻訳する**のがこのスクリプトの役目。

`macro-health`（#420）と同じ形。判定を収集パイプライン本体に埋めないのは、あちらを
非ゼロ終了させると `nightly-scores` の workflow_run チェーンごと巻き添えで止まるため。

## なぜ Management API ではないのか（#483 のブロッカー回避）

#483 は Supabase Management API で総量を取る設計だったが、PAT が `gh secret list` に
無く着手できないままだった。**判定に要るものは既に DB の中にある**——`db_egress` が
`app_settings.egress_cycle_bytes` へプロセス跨ぎ・マシン跨ぎの累計を積んでいる
（ADR-0037）。PAT が用意できたら「正本の総量」として足し、台帳の推定値との残差を
較正へ回す（排他ではない）。

## Database Size も同じジョブで見る理由

2026-08-19 時点で **Egress より先に Database Size が壁**になっている（430MB / 500MB）。
Egress は超えても翌サイクルで戻るが、**Database Size を超えると read-only** で
収集そのものが止まる。「枠の消費」という同じ問いなので、見る場所を分けない。

読むのは `app_settings` の2行と `pg_database_size` の1行だけ（Egress は数百バイト）。
本番書込なし・読取専用。出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。

実行: `python -m scripts.check_egress_health`（`-m` 必須）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from sqlalchemy import text                       # noqa: E402

import db_egress                                  # noqa: E402
from database import SessionLocal, get_setting    # noqa: E402

EXIT_UNHEALTHY = 2

# Database Size の枠と閾値。docs/DEPLOYMENT.md「外部サービス制約」が正本。
# Egress と違い**超えると read-only** になり収集が止まるので、警告は Egress より手前に置く。
#
# **判定に使う値と課金に使われる値は別物**（2026-08-19 に3つ揃って実測）:
#   Usage ページ      430MB … 課金クォータの判定値。超えると read-only
#   Infrastructure    409.8MB
#   pg_database_size  395MB … このスクリプトが読む値
# 当時は差（約 35MB）を係数へ織り込み、`pg_database_size` ÷ 2 進の 500MB に 0.85 を当てていた。
#
# **#1109（2026-11-01）で Usage 基準へ直した。** Usage 画面は「0.398 / 0.5 GB (80%)・詳細 379.71 MB」
# で、上限 0.5 GB は **10 進**（= 476.8 MiB）、詳細の MB は 2 進（379.71 MiB ≒ 398 MB）。2 進の 500 MiB を
# 上限にすると約 5% 大きく見て、スクリプトは 73.1%（`pg_database_size` 365.3 MiB）と出していた——
# 0.85 は Usage 基準で約 92% にあたり、思ったより遅く鳴る。いまは「`pg_database_size` ＋ Usage との差」
# を Usage 換算として 10 進の上限で割り、**Usage 画面と同じ割合**で判定する（2026-11-01 は 79.8% ↔ 画面 79.6%）。
# 差の内訳（WAL・システム領域）は固定ではない（8/19 は約 35MB・11/1 は約 14.4 MiB）ので、Usage 側と
# 割合が数ポイント以上ずれたら `DB_USAGE_GAP_BYTES` を測り直すこと。0.85 なら read-only までに 75MB 残る。
DB_QUOTA_BYTES = 500 * 1000 ** 2
DB_USAGE_GAP_BYTES = 15 * 1024 ** 2     # 2026-11-01 の実測 14.4 MiB（Usage 379.71 − pg 365.3）を切り上げ
DB_WARN_RATIO = 0.85


def collect(db) -> dict:
    """枠の消費を読む。閾値判定はしない（測るのと決めるのを分ける）。"""
    cycle_start = db_egress.current_cycle_start().isoformat()
    saved_start = get_setting(db, db_egress.CYCLE_START_KEY)
    raw_bytes = get_setting(db, db_egress.CYCLE_BYTES_KEY)

    # 印が現サイクルと違う＝まだ誰も今サイクルに書いていない＝累計は 0 から。
    # **古い印の値をそのまま読まない**（前サイクルの消費を今サイクルに繰り越すと、
    # リセット直後に必ず誤警報が出て、以後この通知が信用されなくなる）。
    try:
        egress_bytes = float(raw_bytes or 0) if saved_start == cycle_start else 0.0
    except ValueError:
        egress_bytes = 0.0

    db_bytes = float(db.execute(
        text("SELECT pg_database_size(current_database())")).scalar() or 0)
    db_usage_bytes = db_bytes + DB_USAGE_GAP_BYTES      # Usage 画面の数え方へ寄せた推定（#1109）

    return {
        "cycle_start": cycle_start,
        "saved_start": saved_start,
        "egress_bytes": egress_bytes,
        "egress_ratio": egress_bytes / db_egress.QUOTA_BYTES,
        "db_bytes": db_bytes,
        "db_usage_bytes": db_usage_bytes,
        "db_ratio": db_usage_bytes / DB_QUOTA_BYTES,
        "ledger_is_current": saved_start == cycle_start,
    }


def problems(snap: dict) -> list[str]:
    """起票に値する事象だけを返す（空なら健全）。"""
    found = []
    if snap["egress_ratio"] >= db_egress.CYCLE_WARN_RATIO:
        found.append(
            f"Egress {_fmt(snap['egress_bytes'])} / {_fmt(db_egress.QUOTA_BYTES)} "
            f"({snap['egress_ratio']:.0%} >= {db_egress.CYCLE_WARN_RATIO:.0%})")
    if snap["db_ratio"] >= DB_WARN_RATIO:
        found.append(
            f"Database Size（Usage 換算）{_fmt_db(snap['db_usage_bytes'])} / {_fmt_db(DB_QUOTA_BYTES)} "
            f"({snap['db_ratio']:.0%} >= {DB_WARN_RATIO:.0%})")
    return found


def _fmt(n_bytes: float) -> str:
    if n_bytes >= 1024 ** 3:
        return f"{n_bytes / 1024 ** 3:.2f}GB"
    return f"{n_bytes / 1024 ** 2:.1f}MB"


def _fmt_db(n_bytes: float) -> str:
    """Database Size は Usage 画面の上限と同じ **10 進**で出す（#1109）。"""
    return f"{n_bytes / 1000 ** 2:.1f}MB"


def format_report(snap: dict) -> list[str]:
    lines = [
        "== Supabase 枠消費 ==",
        f"請求サイクル: {snap['cycle_start']} 開始（台帳の印: {snap['saved_start'] or '未設定'}）",
        f"Egress 累計 : {_fmt(snap['egress_bytes'])} / {_fmt(db_egress.QUOTA_BYTES)} "
        f"({snap['egress_ratio']:.1%})  [warn {db_egress.CYCLE_WARN_RATIO:.0%} / "
        f"block {db_egress.CYCLE_BLOCK_RATIO:.0%}]",
        f"Database    : Usage 換算 {_fmt_db(snap['db_usage_bytes'])} / {_fmt_db(DB_QUOTA_BYTES)} "
        f"({snap['db_ratio']:.1%})  [warn {DB_WARN_RATIO:.0%}]  "
        f"（pg_database_size {snap['db_bytes'] / 1024 ** 2:.1f}MiB ＋ 差 {DB_USAGE_GAP_BYTES / 1024 ** 2:.0f}MiB）",
    ]
    if not snap["ledger_is_current"]:
        # サイクル切替の直後は必ずここを通る（誰もまだ書いていない）。**このジョブ自身の
        # 実行が印を進める**ので、翌日も未設定のままなら計測側が動いていない疑い。
        # 「消費ゼロ」と「計測が止まっている」は台帳の上では同じ顔をする（#438 と同型）。
        lines.append(
            "  ※ 台帳の印が現サイクルではない＝今サイクルの記録がまだ無い。"
            "翌日も同じなら db_egress の書き込み経路を疑うこと")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Supabase 枠消費のゲート（#478 / #483）")
    ap.add_argument("--warn-only", action="store_true",
                    help="閾値を超えても exit 0（誤検知の調査・閾値チューニング用）")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        snap = collect(db)
    finally:
        db.close()

    for line in format_report(snap):
        print(line)

    found = problems(snap)
    if not found:
        print("[枠消費] OK")
        return 0

    for p in found:
        print(f"[枠消費] 超過: {p}")
    if args.warn_only:
        print("[枠消費] --warn-only のため exit 0")
        return 0
    print(f"[枠消費] exit {EXIT_UNHEALTHY}（notify-failure.yml が Issue を起票する）")
    return EXIT_UNHEALTHY


if __name__ == "__main__":
    sys.exit(main())
