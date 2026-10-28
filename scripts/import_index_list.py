"""指数イベント台帳の一覧を取り込む（#1003・ADR-0068）。

JPX の一覧 PDF（`.index_lists/raw/`）を `sec_code,kind` の正規化 CSV（`.index_lists/<公表>.csv`）にする。
台帳（`index_events.load_ledger`）はこの CSV だけを読む。一覧は無断転載不可でリポジトリが PUBLIC なので、
PDF も CSV もコミットしない（`.gitignore`）。

## 何を照合するか

1. 元の PDF の sha256 が登録表（`index_events.REVIEWS`）と一致すること——どの資料かを固定する
2. 節（`（１）`・`(1)` 等の見出し）ごとに通し番号が 1 から欠けなく続き、節の中でコードが重複しないこと
3. 種別ごとの件数と、新規追加の市場区分の内訳が登録表（事前登録 §2 の照合値）と一致すること
4. 正規化 CSV の sha256 が登録表と一致すること——一致しなければ書かない（既存の CSV を壊さない）

## 2 列の一覧（#1065）

TOPIX 以外の指数の一覧（`index_events.CONFOUNDER_LISTS`）のうち、1 つの表の左右の列に種別が並ぶもの
（グロース 250 の定期選定の「追加」「除外」）は、登録表の sections を `{"left": 種別, "right": 種別}` にする。
見出し行（`コード` が 2 回出る行）の 2 つ目の `コード` の位置より左のコードを left、右を right とし、注記
（`*` の直後がコードでない行）か次の節（`2.`）の行で読み終える。通し番号は無いので、件数と重複だけを照合する。

## 文字の取り出し

Git for Windows 同梱の `pdftotext`（Xpdf 4.x）の `-enc UTF-8 -table` を使う。新しい Python の依存を
入れずに、3 本とも件数が公表値と完全に一致した（2026-10-21）。poppler 版は `-table` を持たないので
失敗として止まる。場所は `--pdftotext` で渡すか PATH に置く。

実行（リポジトリの直下で）:
    python -I scripts/import_index_list.py --all
    python -I scripts/import_index_list.py topix-2026-10 [--root <.index_lists>] [--pdftotext <exe>]

`-I`（カレントフォルダから Python のモジュールを読まない）でも動くよう、先頭で自分のリポジトリを
`sys.path` に足す。DL した資料のフォルダ（`--root` の中）からは実行させない。
出力は日本語と ASCII のみ（cp932 で書ける文字だけ）。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import index_events as IE  # noqa: E402

# 照合に失敗したときの終了コード（argparse の引数欠落と同じ 2）
EXIT_MISMATCH = 2
GIT_PDFTOTEXT = Path(r"C:\Program Files\Git\mingw64\bin\pdftotext.exe")

# `No. コード 銘柄名 [市場区分]` の行。コードは 4 文字（英字入りの新コード 130A 等を含む）
_ROW = re.compile(r"^\s*(\d+)\s+([0-9][0-9A-Z]{3})\s+(.*?)\s*$")
# 節の見出し: 行頭の `（１）` / `(1)`
_HEAD = re.compile(r"^\s*[（(]\s*([0-9０-９])\s*[）)]")
MARKETS = ("プライム", "スタンダード", "グロース")


class IndexListError(ValueError):
    """元資料が違う・取り出せない・登録表と合わない。"""


def parse_list_text(text: str, sections: Mapping) -> list:
    """`pdftotext -table` の出力から `[(sec_code, kind, 市場区分), ...]` を作る。

    `sections` は `{見出し番号: 種別}`（見出しの無い一覧は `{"0": 種別}`）。知らない見出し・
    欠けた節・通し番号の欠け・節の中の重複は `IndexListError`。
    """
    current = "0"
    found = defaultdict(list)
    for line in text.splitlines():
        head = _HEAD.match(line)
        if head:
            current = unicodedata.normalize("NFKC", head.group(1))
            continue
        row = _ROW.match(line)
        if row:
            rest = row.group(3)
            market = next((m for m in MARKETS if rest.endswith(m)), "")
            found[current].append((int(row.group(1)), row.group(2), market))
    if set(found) != set(sections):
        raise IndexListError(f"節が登録表と違う（一覧 {sorted(found)} / 登録表 {sorted(sections)}）")
    out = []
    for sec_no, rows in found.items():
        numbers = [n for n, _, _ in rows]
        if numbers != list(range(1, len(rows) + 1)):
            gaps = sorted(set(range(1, max(numbers) + 1)) - set(numbers))[:10]
            raise IndexListError(f"節 {sec_no}: 通し番号が 1 から続いていない（欠け {gaps}・行数 {len(rows)}）")
        codes = Counter(code for _, code, _ in rows)
        dup = sorted(c for c, n in codes.items() if n > 1)
        if dup:
            raise IndexListError(f"節 {sec_no}: コードが重複している {dup[:10]}")
        out.extend((code, sections[sec_no], market) for _, code, market in rows)
    return out


TWO_COLUMN = ("left", "right")
_CODE = re.compile(r"(?<![0-9A-Z])([0-9][0-9A-Z]{3})(?![0-9A-Z])")
# 読み終える行: 注記（`*` の直後がコードでない行。新規上場の社の行は `*  618A` のように印が先頭に付く）か次の節
_TWO_COLUMN_END = re.compile(r"^\s*([*＊](?!\s*[0-9][0-9A-Z]{3}(?![0-9A-Z]))|[0-9０-９]\s*[.．])")


def parse_two_column_text(text: str, sections: Mapping) -> list:
    """左右 2 列の一覧から `[(sec_code, kind, ""), ...]` を作る（#1065）。

    見出し行が無い・1 行に 3 つ以上のコードがある・列の中でコードが重複するときは `IndexListError`。
    """
    lines = text.splitlines()
    head = next((i for i, line in enumerate(lines) if line.count("コード") >= 2), None)
    if head is None:
        raise IndexListError("2 列の一覧の見出し行（コード が 2 回出る行）が無い")
    split = lines[head].index("コード", lines[head].index("コード") + 1)
    found = defaultdict(list)
    for line in lines[head + 1:]:
        if _TWO_COLUMN_END.match(line):
            break
        codes = [(m.start(), m.group(1)) for m in _CODE.finditer(line)]
        if len(codes) > 2:
            raise IndexListError(f"1 行に 3 つ以上のコードがある: {[c for _, c in codes]}")
        for pos, code in codes:
            found["left" if pos < split else "right"].append(code)
    out = []
    for side in TWO_COLUMN:
        dup = sorted(c for c, n in Counter(found[side]).items() if n > 1)
        if dup:
            raise IndexListError(f"{side} の列でコードが重複している {dup[:10]}")
        out.extend((code, sections[side], "") for code in found[side])
    return out


def check_counts(key: str, parsed: Sequence, review) -> None:
    """種別ごとの件数と市場区分の内訳を登録表と照合する。"""
    got = dict(Counter(kind for _, kind, _ in parsed))
    if got != dict(review.counts):
        raise IndexListError(f"{key}: 件数が登録表と違う（一覧 {got} / 登録表 {review.counts}）")
    for kind, want in review.market_counts.items():
        markets = Counter(m for _, k, m in parsed if k == kind)
        got_m = {m: markets.get(m, 0) for m in want}
        if got_m != dict(want) or sum(markets.values()) != sum(want.values()):
            raise IndexListError(f"{key}: {kind} の市場区分が登録表と違う（一覧 {dict(markets)} / 登録表 {want}）")


def find_pdftotext(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    found = shutil.which("pdftotext")
    if found:
        return Path(found)
    if GIT_PDFTOTEXT.is_file():
        return GIT_PDFTOTEXT
    raise IndexListError(f"pdftotext が見つからない（--pdftotext で渡す。Git for Windows なら {GIT_PDFTOTEXT}）")


def extract_text(pdf: Path, pdftotext: Path) -> str:
    try:
        proc = subprocess.run([str(pdftotext), "-enc", "UTF-8", "-table", str(pdf), "-"],
                              capture_output=True, check=False)
    except OSError as e:
        raise IndexListError(f"pdftotext を起動できない（{pdftotext}）: {e}") from None
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise IndexListError(f"pdftotext が失敗した（exit {proc.returncode}・-table を持たない版かもしれない）: {err}")
    return proc.stdout.decode("utf-8")


def import_review(key: str, root: Path, pdftotext: Path, *,
                  reviews: Optional[Mapping] = None) -> tuple:
    """一覧 1 本を取り込み、`(正規化 CSV のバイト列, sha256)` を返す。ファイルは書かない。"""
    reviews = registry_of(key) if reviews is None else reviews
    if key not in reviews:
        raise IndexListError(f"未登録の公表: {key}（登録済み: {sorted(reviews)}）")
    review = reviews[key]
    src = root / review.source
    if not src.is_file():
        raise IndexListError(f"{key}: 元資料が無い: {src}")
    actual = IE.sha256_hex(src.read_bytes())
    if actual != review.source_sha256:
        raise IndexListError(f"{key}: 元資料の sha256 が登録表と違う（{actual} / 登録表 {review.source_sha256}）")
    parse = parse_two_column_text if set(review.sections) == set(TWO_COLUMN) else parse_list_text
    parsed = parse(extract_text(src, pdftotext), review.sections)
    check_counts(key, parsed, review)
    data = IE.normalized_bytes((code, kind) for code, kind, _ in parsed)
    return data, IE.sha256_hex(data)


def registry_of(key: str) -> Mapping:
    """キーが載っている登録表（TOPIX の `REVIEWS` か、交絡の記述用の `CONFOUNDER_LISTS`）。"""
    return IE.CONFOUNDER_LISTS if key in IE.CONFOUNDER_LISTS else IE.REVIEWS


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="指数イベント台帳の一覧 PDF を正規化 CSV にする")
    ap.add_argument("keys", nargs="*", help=f"公表のキー（{', '.join([*IE.REVIEWS, *IE.CONFOUNDER_LISTS])}）")
    ap.add_argument("--all", action="store_true", help="登録表の全部（REVIEWS と CONFOUNDER_LISTS）を取り込む")
    ap.add_argument("--root", default=str(IE.DEFAULT_LISTS_DIR), help="一覧の置き場（既定: リポジトリの .index_lists）")
    ap.add_argument("--pdftotext", help="pdftotext の場所（既定: PATH → Git for Windows 同梱）")
    args = ap.parse_args(argv)
    keys = [*IE.REVIEWS, *IE.CONFOUNDER_LISTS] if args.all else args.keys
    if not keys:
        ap.error("公表のキーか --all を指定する")
    root = Path(args.root)
    if _inside(Path.cwd(), root):
        print(f"[import_index_list] NG: 資料のフォルダの中から実行しない（cwd={Path.cwd()}）", file=sys.stderr)
        return EXIT_MISMATCH
    try:
        pdftotext = find_pdftotext(args.pdftotext)
        results = [(key, *import_review(key, root, pdftotext)) for key in keys]
    except IndexListError as e:
        print(f"[import_index_list] NG: {e}", file=sys.stderr)
        return EXIT_MISMATCH
    rc = 0
    for key, data, sha in results:
        want = registry_of(key)[key].normalized_sha256
        rows = IE.parse_normalized(data)
        counts = dict(Counter(kind for _, kind in rows))
        if want and want != sha:
            print(f"{key}: sha256 {sha} が登録表 {want} と違うので書かない（件数 {counts}）")
            rc = EXIT_MISMATCH
            continue
        out = root / f"{key}.csv"
        out.write_bytes(data)
        note = "登録表と一致" if want else "登録表が空（この値を index_events の登録表へ書く）"
        print(f"{key}: 件数 {counts} sha256 {sha} -> {out}（{note}）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
