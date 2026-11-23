"""夜間に回さない producer を 1 つ、既定パラメータで回して今回の保存を確かめる（日中キューの `model:*`・#1317）。

夜間の `nightly_scores` と同じ `run_models` を使う＝`shared_snapshot_cache`・永続化の検証（`VERIFIERS`）・
失敗の数え方（`failed_labels`）を書き写さない。**予測の写し（`snapshot_predictions`）はしない**——写しは
暦を持つ本番の源だけのもの（`prediction_snapshots.MU_SOURCES`）。

回せるのは `VERIFIERS` があり、夜間で回さない（`NIGHTLY_MODELS` に無い）モデルだけ。夜間のモデルをここで
回すと写しを飛ばすので拒む（夜間のモデルは `nightly_scores` で回す）。

実行: python -m scripts.refresh_producer --model macro_transformer
終了コード: 0＝回して保存を確かめた／1＝モデルが失敗した・保存を確かめられなかった／2＝回せないモデル名
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

logger = logging.getLogger(__name__)


def allowed_models() -> tuple[str, ...]:
    """ここで回せるモデル（永続化の検証があり、夜間では回さないもの）。"""
    from nightly_scores import NIGHTLY_MODELS, VERIFIERS

    return tuple(m for m in VERIFIERS if m not in NIGHTLY_MODELS)


async def run(model: str) -> list[dict]:
    """1 モデルを回し、`run_models` の結果を返す（失敗なら SystemExit・写しはしない）。"""
    from database import SessionLocal, _is_local
    from nightly_scores import failed_labels, run_models

    logger.info("producer の実行 開始: model=%s / 接続先=%s", model, "ローカル" if _is_local else "本番（リモート）")
    t0 = time.time()
    db = SessionLocal()
    try:
        entries = await run_models([model], db)
    finally:
        db.close()
    for e in entries:
        logger.info("%-8s %-18s %5.1f分  %s", "OK" if e["ok"] else "FAILED", e["model"],
                    e["elapsed_min"], e["verified"] or e["error"] or e["summary"] or "")
    logger.info("総所要時間: %.1f分", (time.time() - t0) / 60)
    failed = failed_labels(entries)
    if failed:
        raise SystemExit(f"失敗したモデル: {', '.join(failed)}")
    return entries


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="夜間に回さない producer を 1 つ回して保存を確かめる（#1317）")
    ap.add_argument("--model", required=True, help="プラグイン名（例: macro_transformer）")
    args = ap.parse_args(argv)
    allowed = allowed_models()
    if args.model not in allowed:
        ap.error(f"{args.model!r} はここでは回せない（回せるのは {', '.join(allowed) or 'なし'}。"
                 "夜間のモデルは nightly_scores で回す）")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    asyncio.run(run(args.model))


if __name__ == "__main__":
    main()
