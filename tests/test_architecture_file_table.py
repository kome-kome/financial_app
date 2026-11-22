"""ARCHITECTURE.md §10「ファイル役割一覧」の行が育ち直さないよう守る（#1301）。

§10 の行は役割を1〜2文に留め、詳しい説明は各リファレンス（`PLUGIN_REFERENCE.md`・
`SCRIPTS_REFERENCE.md`・`MODULE_REFERENCE.md`）へ置く（#843・#1293）。行が長くなっても
テストもアプリも落ちないので、#843 で `scripts/` の詳細を分けた後も行が 300 字を超えて
育ち直していた（#1293 の時点で 4 本）。気づく手段が人の定期点検（`/tidy`）しか無かったので、
ここで機械的に止める。

もう1つ、素の `|` は表のセルを切る。GitHub の表は `\\|` と書いた `|` 以外を、コード（バッククォート）
の中でも区切りとして扱う。#1293 の前の `backtest.py` の行には素の `|w|` があり、表示でセルが
途中で切れていたが、これも失敗としては現れなかった。全行のセルが4つであることも確かめる。
"""

from __future__ import annotations

import re
from pathlib import Path

ARCHITECTURE_MD = Path(__file__).resolve().parents[1] / "docs" / "ARCHITECTURE.md"
SECTION_PREFIX = "## 10."
MAX_ROLE_CHARS = 300   # 役割1〜2文の上限の目安（#1293 で全行をこの内に収めた）
N_CELLS = 4            # ファイル / 種別 / 役割 / 主な依存先
ROLE_INDEX = 2
MIN_ROWS = 150         # 抽出が空振りしていないことの下限（2026-11-22 時点で 193 行）

SEPARATOR_RE = re.compile(r"^\|\s*:?-")
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


def section_lines(text: str, prefix: str = SECTION_PREFIX) -> list[str]:
    """見出し `prefix` で始まる節の本文（次の `## ` 見出しの手前まで）。"""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith(prefix)), None)
    if start is None:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start + 1:end]


def split_cells(row: str) -> list[str]:
    """GitHub の表の規則でセルに分ける（`\\|` だけは区切らない）。"""
    body = row.strip()
    body = body[1:] if body.startswith("|") else body
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in CELL_SPLIT_RE.split(body)]


def body_rows(lines: list[str]) -> list[str]:
    """表の見出し行と区切り行を除いた行。"""
    rows = [line for line in lines if line.startswith("|") and not SEPARATOR_RE.match(line)]
    return [row for row in rows if split_cells(row)[0] != "ファイル"]


def long_roles(rows: list[str], limit: int = MAX_ROLE_CHARS) -> list[tuple[str, int]]:
    out = []
    for row in rows:
        cells = split_cells(row)
        if len(cells) == N_CELLS and len(cells[ROLE_INDEX]) > limit:
            out.append((cells[0], len(cells[ROLE_INDEX])))
    return out


def bad_cell_counts(rows: list[str]) -> list[tuple[str, int]]:
    return [(split_cells(row)[0][:60], len(split_cells(row))) for row in rows if len(split_cells(row)) != N_CELLS]


def _rows() -> list[str]:
    return body_rows(section_lines(ARCHITECTURE_MD.read_text(encoding="utf-8")))


class TestFileRoleTable:
    def test_scan_is_not_vacuous(self):
        rows = _rows()
        assert len(rows) >= MIN_ROWS, (
            f"§10 の表から {len(rows)} 行しか読めていない（下限 {MIN_ROWS}）。見出し `{SECTION_PREFIX}` か"
            "表の形が変わると、長い行も素の `|` も見逃したまま通ってしまう"
        )

    def test_every_row_has_four_cells(self):
        bad = bad_cell_counts(_rows())
        assert not bad, (
            "§10 の行のセルが4つでない（素の `|` が表を切っている。コードの中でも `\\|` と書く）: "
            + "、".join(f"{name}（{n}セル）" for name, n in bad)
        )

    def test_no_role_cell_is_too_long(self):
        long = long_roles(_rows())
        assert not long, (
            f"§10 の役割の欄が {MAX_ROLE_CHARS} 字を超えた行がある。役割は1〜2文に縮め、詳しい説明は "
            "PLUGIN_REFERENCE.md（plugins/）・SCRIPTS_REFERENCE.md（scripts/）・MODULE_REFERENCE.md（それ以外）の"
            "節へ移して行からリンクする: " + "、".join(f"{name}（{n}字）" for name, n in long)
        )


class TestParser:
    def test_escaped_pipe_does_not_split(self):
        assert split_cells(r"| `a.py` | 種別 | 0.5×\|平均\| を満たす | dep |") == [
            "`a.py`", "種別", r"0.5×\|平均\| を満たす", "dep"]

    def test_bare_pipe_splits_even_inside_code(self):
        # GitHub はコードの中の素の `|` でもセルを切る
        assert len(split_cells("| `a.py` | 種別 | `x|y` を返す | dep |")) == 5

    def test_bare_pipe_in_text_is_reported(self):
        rows = ["| `backtest.py` | バックエンド | 値がある指標の |w| 和で割る | database.py |"]
        assert bad_cell_counts(rows) == [("`backtest.py`", 6)]

    def test_header_and_separator_are_skipped(self):
        lines = ["| ファイル | 種別 | 役割 | 主な依存先 |", "|---|---|---|---|", "| `a.py` | x | y | z |"]
        assert body_rows(lines) == ["| `a.py` | x | y | z |"]

    def test_limit_is_inclusive(self):
        ok = "| `a.py` | x | " + "あ" * MAX_ROLE_CHARS + " | z |"
        over = "| `b.py` | x | " + "あ" * (MAX_ROLE_CHARS + 1) + " | z |"
        assert long_roles([ok, over]) == [("`b.py`", MAX_ROLE_CHARS + 1)]

    def test_section_stops_at_the_next_heading(self):
        text = "## 9. 前\n| a | b | c | d |\n## 10. 表\n| e | f | g | h |\n## 11. 後\n| i | j | k | l |"
        assert section_lines(text) == ["| e | f | g | h |"]

    def test_missing_section_yields_nothing(self):
        assert section_lines("# 見出しだけ") == []
