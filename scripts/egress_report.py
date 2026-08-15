"""Egress 台帳のロールアップ（Issue #478）。

`db_egress` が残した記録を読み、**ジョブ別・テーブル別**に積み上げて「今月どこが枠を
食っているか」を出す。DB に一切繋がないので実行しても Egress は増えない。

入力は2種類:

- **JSONL**（`FINAPP_EGRESS_LEDGER` を設定したプロセスが 1 実行 1 行で append）。
  テーブル別の完全な内訳を持つ。
- **run ログ**（`gh run view <id> --log > run.txt` で落としたテキスト）。
  `[egress] summary ...` 行を拾う。**テーブル別は top3 しか残らない**ので内訳は部分的。

実行:
    python -m scripts.egress_report                       # 既定の .egress/ledger.jsonl
    python -m scripts.egress_report --month 2026-08
    python -m scripts.egress_report --log run.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Supabase 無料枠。docs/DEPLOYMENT.md「外部サービス制約」が正本。
QUOTA_GB = 5.0

DEFAULT_LEDGER = Path(__file__).resolve().parent.parent / ".egress" / "ledger.jsonl"

# `[egress] summary job=X total=67.7MB rows=1391494 calls=12 top=a:39.3MB,b:22.5MB`
_SUMMARY_RE = re.compile(r"\[egress\]\s+summary\s+(.*)$")
_KV_RE = re.compile(r"(\w+)=([^\s]+)")


def _mb_to_bytes(token: str) -> float:
    return float(token.rstrip("MB")) * 1024 * 1024


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"warn: skipping malformed ledger line in {path}", file=sys.stderr)
    return rows


def _load_log(path: Path) -> list[dict]:
    """run ログの `[egress] summary` 行を JSONL 相当の dict へ変換する（内訳は top3 のみ）。"""
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SUMMARY_RE.search(line)
        if m is None:
            continue
        kv = dict(_KV_RE.findall(m.group(1)))
        tables = {}
        for part in kv.get("top", "").split(","):
            if ":" in part:
                name, mb = part.rsplit(":", 1)
                try:
                    tables[name] = {"calls": 0, "rows": 0,
                                    "est_bytes": _mb_to_bytes(mb), "unknown_calls": 0}
                except ValueError:
                    pass
        rows.append({
            "ts": "",                                   # ログ行には時刻を載せていない
            "job": kv.get("job", "unknown"),
            "rows": int(kv.get("rows", 0)),
            "calls": int(kv.get("calls", 0)),
            "est_bytes": _mb_to_bytes(kv.get("total", "0MB")),
            "unknown_calls": int(kv.get("unknown_rowcount", 0)),
            "tables": tables,
            "external": [],
            "_partial": True,                           # テーブル内訳が top3 止まりの印
        })
    return rows


def _fmt(n_bytes: float) -> str:
    if n_bytes >= 1024 ** 3:
        return f"{n_bytes / 1024 ** 3:.2f}GB"
    return f"{n_bytes / 1024 ** 2:.1f}MB"


def report(entries: list[dict], month: str | None) -> int:
    if month:
        entries = [e for e in entries if str(e.get("ts", "")).startswith(month)]
    if not entries:
        print("台帳が空です。FINAPP_EGRESS_LEDGER を設定して実行するか --log を指定してください。")
        return 1

    by_job: dict[str, dict] = defaultdict(lambda: {"runs": 0, "bytes": 0.0, "rows": 0, "unknown": 0})
    by_table: dict[str, dict] = defaultdict(lambda: {"bytes": 0.0, "rows": 0, "calls": 0})
    total = 0.0
    partial = False

    for e in entries:
        partial = partial or bool(e.get("_partial"))
        job = by_job[e.get("job", "unknown")]
        job["runs"] += 1
        job["bytes"] += float(e.get("est_bytes", 0))
        job["rows"] += int(e.get("rows", 0))
        job["unknown"] += int(e.get("unknown_calls", 0))
        total += float(e.get("est_bytes", 0))
        for name, v in (e.get("tables") or {}).items():
            t = by_table[name]
            t["bytes"] += float(v.get("est_bytes", 0))
            t["rows"] += int(v.get("rows", 0))
            t["calls"] += int(v.get("calls", 0))

    label = month or "全期間"
    print(f"== Egress ロールアップ ({label}・{len(entries)} 実行) ==")
    print(f"推定合計: {_fmt(total)}  /  無料枠 {QUOTA_GB}GB = {total / (QUOTA_GB * 1024 ** 3):.1%}")
    print("  ※ 推定であって正本ではない。正本はサーバ側 sum(octet_length(列::text))")
    if partial:
        print("  ※ run ログ由来の行を含む: テーブル別内訳は top3 までしか残っていない")

    print("\n-- ジョブ別 --")
    print(f"  {'job':<28} {'runs':>5} {'egress':>10} {'rows':>12} {'unknown':>8}")
    for name, v in sorted(by_job.items(), key=lambda kv: kv[1]["bytes"], reverse=True):
        print(f"  {name:<28} {v['runs']:>5} {_fmt(v['bytes']):>10} {v['rows']:>12,} {v['unknown']:>8}")

    print("\n-- テーブル別 --")
    print(f"  {'table':<28} {'egress':>10} {'rows':>12} {'calls':>7}")
    for name, v in sorted(by_table.items(), key=lambda kv: kv[1]["bytes"], reverse=True):
        print(f"  {name:<28} {_fmt(v['bytes']):>10} {v['rows']:>12,} {v['calls']:>7}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Egress 台帳のロールアップ（DB 非接続）")
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_LEDGER,
                    help=f"db_egress の JSONL 台帳（既定 {DEFAULT_LEDGER}）")
    ap.add_argument("--log", type=Path, action="append", default=[],
                    help="gh run view --log を保存したテキスト（複数指定可）")
    ap.add_argument("--month", help="YYYY-MM で絞る（JSONL のみ・ログ行は時刻を持たない）")
    args = ap.parse_args()

    entries = _load_jsonl(args.jsonl)
    for p in args.log:
        entries.extend(_load_log(p))
    return report(entries, args.month)


if __name__ == "__main__":
    raise SystemExit(main())
