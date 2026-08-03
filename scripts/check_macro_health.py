"""マクロ系列の鮮度を判定し、既定モデルが使う系列が古ければ非ゼロ終了する（Issue #420）。

    python -m scripts.check_macro_health          # 判定（不健全なら exit 2）
    python -m scripts.check_macro_health --warn-only   # 常に exit 0（調査用）

**なぜ収集パイプライン本体から分離しているか**
`daily-incremental` を非ゼロ終了させると、`nightly-scores` の workflow_run チェーン
（conclusion == 'success' 条件・#432）が発火しなくなり、マクロを一切使わない
sector_ols の夜間更新まで巻き添えで止まる。収集元 A の障害が収集元 B を止めない
という #425 の構造をワークフロー間にも適用し、判定は独立ジョブ（macro-health.yml）
に持たせる。収集ジョブ側は同じレポートをログに出すだけで終了コードを変えない。

読むのは `macro_data` の GROUP BY 集約 1 本のみ（Egress を食わない・#355）。
本番書込なし・読取専用。出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。

実行: `python -m scripts.check_macro_health`（`-m` 必須）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from database import SessionLocal          # noqa: E402
from macro_health import check_macro_freshness, format_report  # noqa: E402

EXIT_UNHEALTHY = 2


def main() -> int:
    ap = argparse.ArgumentParser(description="マクロ系列の鮮度ゲート（#420）")
    ap.add_argument("--warn-only", action="store_true",
                    help="不健全でも exit 0（誤検知の調査・閾値チューニング用）")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        result = check_macro_freshness(db)
    finally:
        db.close()

    for line in format_report(result):
        print(line)

    if result["n_critical_bad"] and not args.warn_only:
        print(f"[マクロ健全性] exit {EXIT_UNHEALTHY}（#414 が Issue を起票する）")
        return EXIT_UNHEALTHY
    return 0


if __name__ == "__main__":
    sys.exit(main())
