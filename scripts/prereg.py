"""事前登録文書の凍結と照合（#1007・親 #1001）。

## 何を守るか

測る前に「何をどう測り、どの結果ならどう判断するか」を文書（`docs/prereg/*.md`）に書いて
固定する。結果を見てから窓・対照・閾値を選ぶと、効いているように見えるだけの結論になる
（ADR-0045 / ADR-0050）。文書は main へのマージで凍結し、その時点の本文の sha256 を凍結値とする。

計測道具（#1008〜#1011）は `add_arguments()` で `--prereg <path> --prereg-sha <hex>` を必須にし、
`stamp_from_args()` で照合する。不一致・欠落なら終了コード 2 で止まり、一致すれば出力 JSON へ
入れる印（本文の sha256・HEAD SHA・事前登録の Issue の URL）を返す。

## 本文と追記節

本文 = マーカー行 `APPEND_MARKER` より上。stage A（盲検の下調べ）の結果は凍結の後で書き足すので、
マーカーより下（追記節）は sha256 の対象から外す。マーカーは 1 行だけ置く（0 行・2 行以上は失敗）。

追記節には凍結値そのものを書く（「凍結値（本文の sha256）: 」の後に 16 進 64 桁をバッククォートで
囲んだ 1 行）。`tests/test_prereg.py` が
本文と照合するので、本文を変えた PR は CI で落ちる。凍結し直す（開封前なら正当な修正）ときは、
本文と凍結値を同じ PR で変える＝差分で誰にでも見える。

## 改行をそろえる理由

このリポジトリは `core.autocrlf=true` で `.gitattributes` を持たないので、Windows の作業ツリーでは
CRLF、CI では LF になる。そろえずに計算すると、同じ文書でも手元と CI で指紋が食い違う。
BOM も外す（エディタが付けても中身は同じ）。

実行: python -m scripts.prereg docs/prereg/index-event-2026.md [--check <sha256>]
出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]

APPEND_MARKER = "<!-- prereg:append-below -->"
# 照合に失敗したときの終了コード（argparse の引数欠落と同じ 2）
EXIT_MISMATCH = 2

_SHA_RE = re.compile(r"[0-9a-f]{64}")
# 追記節に書く凍結値の行
_FROZEN_RE = re.compile(r"^凍結値（本文の sha256）: `([0-9a-f]{64})`$", re.M)
# 本文に書く事前登録の Issue の行（本文に置くので、Issue の URL も凍結される）
_ISSUE_RE = re.compile(r"^- 事前登録の Issue: (https://github\.com/[^/\s]+/[^/\s]+/issues/\d+)$", re.M)


class PreregError(ValueError):
    """事前登録文書が読めない・形が崩れている・凍結値と一致しない。"""


def normalize(text: str) -> str:
    """BOM を外し、改行を LF にそろえる。"""
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def split_document(text: str) -> tuple[str, str]:
    """(本文, 追記節) に分ける。マーカー行がちょうど 1 行でなければ PreregError。"""
    lines = normalize(text).split("\n")
    at = [i for i, line in enumerate(lines) if line.strip() == APPEND_MARKER]
    if len(at) != 1:
        raise PreregError(f"マーカー行 {APPEND_MARKER} が {len(at)} 行ある（1 行だけ置く）")
    i = at[0]
    return "\n".join(lines[:i]), "\n".join(lines[i + 1:])


def body_sha256(text: str) -> str:
    """本文（マーカー行より上）の sha256（UTF-8）。"""
    body, _ = split_document(text)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def recorded_sha(text: str) -> Optional[str]:
    """追記節に書いた凍結値。無ければ None。"""
    _, appendix = split_document(text)
    found = _FROZEN_RE.findall(appendix)
    if len(found) > 1:
        raise PreregError(f"追記節に凍結値が {len(found)} 行ある（1 行だけ置く）")
    return found[0] if found else None


def issue_url(text: str) -> str:
    """本文に書いた事前登録の Issue の URL。無ければ PreregError。"""
    body, _ = split_document(text)
    found = _ISSUE_RE.findall(body)
    if len(found) != 1:
        raise PreregError(f"本文に「- 事前登録の Issue: <URL>」の行が {len(found)} 行ある（1 行だけ置く）")
    return found[0]


def read_document(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise PreregError(f"事前登録文書が無い: {p}")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise PreregError(f"事前登録文書が UTF-8 で読めない: {p} ({e.reason})") from None


def _display_path(path: str | Path) -> str:
    p = Path(path).resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _head_sha() -> str:
    # 書き写さない: 夜間バッチが診断値へ残す印と同じもの（"<sha>" / "<sha>+dirty" / "unknown"）
    from nightly_scores import _code_version
    return _code_version()


def verify(path: str | Path, expected_sha: str) -> dict:
    """文書の本文が凍結値と一致すれば、出力 JSON へ入れる印を返す。違えば PreregError。"""
    expected = (expected_sha or "").strip().lower()
    if not _SHA_RE.fullmatch(expected):
        raise PreregError(f"--prereg-sha は 16 進 64 桁: {expected_sha!r}")
    text = read_document(path)
    actual = body_sha256(text)
    if actual != expected:
        raise PreregError(
            f"本文の sha256 が凍結値と一致しない: 実際 {actual} / 指定 {expected}。"
            "本文（マーカー行より上）が変わっている")
    return {
        "prereg_path": _display_path(path),
        "prereg_sha256": actual,
        "prereg_issue": issue_url(text),
        "head_sha": _head_sha(),
    }


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """計測道具の CLI に `--prereg` と `--prereg-sha` を必須の引数として足す。"""
    parser.add_argument("--prereg", required=True,
                        help="事前登録文書のパス（例: docs/prereg/index-event-2026.md）")
    parser.add_argument("--prereg-sha", required=True,
                        help="凍結値（本文の sha256・追記節に記録したもの）")


def stamp_from_args(args: argparse.Namespace) -> dict:
    """`add_arguments` で受けた引数を照合する。失敗なら理由を出して終了コード 2 で止まる。"""
    try:
        return verify(args.prereg, args.prereg_sha)
    except PreregError as e:
        print(f"[prereg] NG: {e}", file=sys.stderr)
        raise SystemExit(EXIT_MISMATCH) from None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="事前登録文書の本文の sha256 を出す・凍結値と照合する")
    ap.add_argument("path", help="事前登録文書のパス")
    ap.add_argument("--check", metavar="SHA256", help="この凍結値と照合する（不一致なら終了コード 2）")
    args = ap.parse_args(argv)
    try:
        text = read_document(args.path)
        actual = body_sha256(text)
        recorded = recorded_sha(text)
        issue_url(text)
        if args.check is not None:
            verify(args.path, args.check)
    except PreregError as e:
        print(f"[prereg] NG: {e}", file=sys.stderr)
        return EXIT_MISMATCH
    print(f"body sha256: {actual}")
    if recorded is None:
        print("recorded   : (none) -- 追記節に凍結値が無い（凍結前）")
    else:
        print(f"recorded   : {recorded} ({'match' if recorded == actual else 'MISMATCH'})")
    if args.check is not None:
        print("[prereg] OK: 本文は凍結値と一致")
    # 追記節の凍結値と本文が食い違うのは、凍結の後で本文を変えたということ。黙って 0 を返さない
    return EXIT_MISMATCH if recorded is not None and recorded != actual else 0


if __name__ == "__main__":
    sys.exit(main())
