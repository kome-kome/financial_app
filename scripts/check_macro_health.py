"""マクロ系列の鮮度を判定し、既定モデルが使う系列が古ければ非ゼロ終了する（Issue #420）。

    python -m scripts.check_macro_health          # 判定（不健全なら exit 2）
    python -m scripts.check_macro_health --warn-only   # 常に exit 0（調査用）

**起動元はローカル夜間バッチの `macro_health` ステップ**（`scripts/run_nightly.py`・#876）。
`pipeline`（収集）の直後に走り、exit 2 なら夜間バッチの失敗通知（`batch_common.notify`）が
Issue を起票する。GHA の `daily-incremental` に連動していた独立ワークフローは #503 で親ごと
発火しなくなったので、#876 で削除してこちらへ移した。

**なぜ収集パイプライン本体から分離しているか**
`collect_macro_data` は 1 系列が取れなくても `continue` するので、収集は exit 0 で通る。
そこで鮮度切れを収集の終了コードへ混ぜると、「収集が落ちた」と「系列が古い」を
起票の上で見分けられなくなる。判定は別ステップに持たせ、収集側は同じレポートを
ログに出すだけで終了コードを変えない。夜間バッチはステップ間で止めないので、
鮮度切れの夜も後続の `scores` は走る（マクロを使わない sector_ols を巻き添えにしない・#425）。

読むのは `macro_data` の GROUP BY 集約 1 本のみ（Egress を食わない・#355）。
本番書込なし・読取専用。出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。

実行: `python -m scripts.check_macro_health`（`-m` 必須）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from database import SessionLocal          # noqa: E402
from macro_health import check_macro_freshness, format_report  # noqa: E402

EXIT_UNHEALTHY = 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="マクロ系列の鮮度ゲート（#420）")
    ap.add_argument("--warn-only", action="store_true",
                    help="不健全でも exit 0（誤検知の調査・閾値チューニング用）")
    args = ap.parse_args(argv)

    db = SessionLocal()
    try:
        result = check_macro_freshness(db)
    finally:
        db.close()

    for line in format_report(result):
        print(line)

    if result["n_critical_bad"] and not args.warn_only:
        print(f"[マクロ健全性] exit {EXIT_UNHEALTHY}（夜間バッチの失敗通知が Issue を起票する・#876）")
        return EXIT_UNHEALTHY
    return 0


if __name__ == "__main__":
    sys.exit(main())
