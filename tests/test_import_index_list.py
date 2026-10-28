"""指数イベント台帳の取り込み CLI（scripts/import_index_list.py・#1003）のテスト。

検体は `pdftotext -enc UTF-8 -table`（Xpdf 4.00）の実出力から**見出しと書式だけ**を写したもの
（2026-10-21 に写した。元資料の sha256: 2026 一覧 7c8f87a3…d13d / 2023 一覧 cce5a965…9bfb /
2022 一覧 b3005c8d…c9c2）。JPX の資料は無断転載不可なので、銘柄のコードと名前はすべて架空にして
並びを写さない。実ファイルが要るテストは、ファイルか `pdftotext` が無ければ skip する（CI には無い）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import index_events as IE  # noqa: E402
from scripts import import_index_list as M  # noqa: E402

TEXT_2026 = """     TOPIX初回定期入替における選定結果(2026年10月30日実施）及び構成銘柄一覧

                                                                                 2026年10月7日公表

（１）新規追加銘柄

No.        コード                                 銘柄名                                          市場区分

1          9901         架空電機                                                     スタンダード

2          990A         架空ホールディングス                                                          グロース

（２）移行措置銘柄

No.        コード                                 銘柄名                                          市場区分

1          9902         架空水産                                                                  プライム

\f     Copyright © 2026 by JPX Market Innovation & Research, Inc. All rights reserved.
（３）構成銘柄（2026年10月30日時点）

No.  コード                銘柄名                                                               市場区分

1    9901  架空電機                                                                             スタンダード

2    9902  架空水産                                                                             プライム

3    9903  架空商事                                                                             プライム

4    990A  架空ホールディングス                                                                   グロース

（注）　銘柄コード順に掲載。本資料は公表日の情報に基づくもので、今後変動する可能性があります。
"""
SECTIONS_2026 = {"1": "new_add", "2": "transition", "3": "constituent"}

TEXT_2023 = """           TOPIXにおける段階的ウエイト低減銘柄一覧（再評価後の取扱い）

                                               2023年10月6日公表

(1) 段階的ウエイト低減銘柄から除外となる銘柄　（銘柄コード順）

　　 2023年10月最終営業日以降、四半期ごとに移行係数を「1」となるまで0.1幅で逓増

No.  コード                           銘柄名

1    9901  架空電機

(2) 段階的ウエイト低減を継続する銘柄　（銘柄コード順）

　　 2023年10月最終営業日以降も、移行係数の逓減を継続  （2025年1月最終営業日にTOPIX構成銘柄から除外）

No.  コード                           銘柄名

1    9902  架空水産

2    9903  架空商事
"""

TEXT_2022 = """           TOPIXにおける段階的ウエイト低減銘柄一覧

                                                                     2022年10月7日公表

段階的ウエイト低減銘柄（銘柄コード順）

No.  コード                     銘柄名

1    9901  架空電機

2    9902  架空水産

（注）　銘柄数は、上場廃止等に伴い、今後変動する可能性があります。
"""


class TestParse:
    def test_three_sections_with_markets(self):
        got = M.parse_list_text(TEXT_2026, SECTIONS_2026)
        assert got == [("9901", "new_add", "スタンダード"), ("990A", "new_add", "グロース"),
                       ("9902", "transition", "プライム"),
                       ("9901", "constituent", "スタンダード"), ("9902", "constituent", "プライム"),
                       ("9903", "constituent", "プライム"), ("990A", "constituent", "グロース")]

    def test_half_width_headings(self):
        got = M.parse_list_text(TEXT_2023, {"1": "released", "2": "transition"})
        assert [(c, k) for c, k, _ in got] == [("9901", "released"), ("9902", "transition"),
                                               ("9903", "transition")]

    def test_single_section_without_heading(self):
        got = M.parse_list_text(TEXT_2022, {"0": "transition"})
        assert [(c, k, m) for c, k, m in got] == [("9901", "transition", ""), ("9902", "transition", "")]

    def test_gap_in_numbering_raises(self):
        text = TEXT_2022.replace("2    9902", "3    9902")
        with pytest.raises(M.IndexListError, match="通し番号"):
            M.parse_list_text(text, {"0": "transition"})

    def test_duplicate_code_raises(self):
        text = TEXT_2022.replace("2    9902", "2    9901")
        with pytest.raises(M.IndexListError, match="重複"):
            M.parse_list_text(text, {"0": "transition"})

    def test_unknown_or_missing_section_raises(self):
        with pytest.raises(M.IndexListError, match="節"):
            M.parse_list_text(TEXT_2026, {"1": "new_add", "2": "transition"})
        with pytest.raises(M.IndexListError, match="節"):
            M.parse_list_text(TEXT_2022, {"1": "transition"})


# 2 列の一覧（#1065）の検体。架空のコードで、見出し・列の位置・注記の印の付き方だけを写す
TWO_COL = """
         「架空の指数」の定期選定結果
             追加（3）                       除外（4）

   コード       銘柄名                    コード              銘柄名

   9001      架空Ａ                         9101  架空Ｋ

*  901A      架空Ｂ                         9102             架空Ｌ

   9003      架空Ｃ                         9103  架空Ｍ

                                            9104  架空Ｎ

   *新規上場に伴い、2026年10月30日に追加される銘柄です。
   2.指数構成銘柄
No.  コード   銘柄名
1    9999   構成だけの社
"""
TWO_SECTIONS = {"left": "x_add", "right": "x_remove"}


class TestTwoColumn:
    def test_left_and_right_columns_are_split_at_the_second_header(self):
        got = M.parse_two_column_text(TWO_COL, TWO_SECTIONS)
        assert [(c, k) for c, k, _ in got] == [("9001", "x_add"), ("901A", "x_add"), ("9003", "x_add"),
                                                ("9101", "x_remove"), ("9102", "x_remove"),
                                                ("9103", "x_remove"), ("9104", "x_remove")]

    def test_reading_stops_at_the_note_not_at_a_marked_row(self):
        """新規上場の印 `*  901A` の行は読み、`*新規上場…` の注記と次の節（構成銘柄）は読まない。"""
        codes = {c for c, _, _ in M.parse_two_column_text(TWO_COL, TWO_SECTIONS)}
        assert "901A" in codes and "9999" not in codes and "2026" not in codes

    def test_three_codes_on_a_line_raise(self):
        text = TWO_COL.replace("9103  架空Ｍ", "9103  架空Ｍ  9105  架空Ｏ")
        with pytest.raises(M.IndexListError):
            M.parse_two_column_text(text, TWO_SECTIONS)

    def test_duplicate_in_a_column_raises(self):
        with pytest.raises(M.IndexListError):
            M.parse_two_column_text(TWO_COL.replace("9102", "9101"), TWO_SECTIONS)

    def test_missing_header_raises(self):
        with pytest.raises(M.IndexListError):
            M.parse_two_column_text("9001 架空Ａ\n", TWO_SECTIONS)

    def test_registry_of_routes_confounder_keys(self):
        assert M.registry_of("growth250-2026-10") is IE.CONFOUNDER_LISTS
        assert M.registry_of("topix-2026-10") is IE.REVIEWS


class TestCheckCounts:
    REVIEW = IE.REVIEWS["topix-2026-10"]._replace(
        counts={"new_add": 2, "transition": 1, "constituent": 4},
        market_counts={"new_add": {"プライム": 0, "スタンダード": 1, "グロース": 1}})

    def test_match(self):
        M.check_counts("k", M.parse_list_text(TEXT_2026, SECTIONS_2026), self.REVIEW)

    def test_count_mismatch_raises(self):
        review = self.REVIEW._replace(counts={"new_add": 2, "transition": 1, "constituent": 5})
        with pytest.raises(M.IndexListError, match="件数"):
            M.check_counts("k", M.parse_list_text(TEXT_2026, SECTIONS_2026), review)

    def test_market_mismatch_raises(self):
        review = self.REVIEW._replace(market_counts={"new_add": {"プライム": 1, "スタンダード": 1, "グロース": 0}})
        with pytest.raises(M.IndexListError, match="市場区分"):
            M.check_counts("k", M.parse_list_text(TEXT_2026, SECTIONS_2026), review)


class TestImportReview:
    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(M.IndexListError, match="元資料が無い"):
            M.import_review("topix-2022-10", tmp_path, Path("pdftotext"))

    def test_source_sha_mismatch_raises(self, tmp_path):
        src = tmp_path / IE.REVIEWS["topix-2022-10"].source
        src.parent.mkdir(parents=True)
        src.write_bytes(b"%PDF-1.4 not the list")
        with pytest.raises(M.IndexListError, match="sha256"):
            M.import_review("topix-2022-10", tmp_path, Path("pdftotext"))

    def test_unknown_key_raises(self, tmp_path):
        with pytest.raises(M.IndexListError, match="未登録"):
            M.import_review("topix-2099-10", tmp_path, Path("pdftotext"))


def test_missing_pdftotext_is_a_plain_error(tmp_path):
    with pytest.raises(M.IndexListError, match="起動できない"):
        M.extract_text(tmp_path / "x.pdf", tmp_path / "no-such-pdftotext.exe")


def test_refuses_to_run_inside_the_lists_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert M.main(["--all", "--root", str(tmp_path)]) == M.EXIT_MISMATCH


def test_runs_under_isolated_mode():
    """`python -I`（カレントフォルダを sys.path に入れない）でも index_events を import できる。"""
    proc = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "import_index_list.py"), "--help"],
                          capture_output=True, cwd=str(ROOT.parent), timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def _real_inputs():
    try:
        pdftotext = M.find_pdftotext()
    except M.IndexListError:
        return None
    root = IE.DEFAULT_LISTS_DIR
    if not all((root / r.source).is_file() for r in IE.REVIEWS.values()):
        return None
    return root, pdftotext


@pytest.mark.parametrize("key", list(IE.CONFOUNDER_LISTS))
def test_real_confounder_lists_match_the_registry(key):
    """交絡の記述用の一覧（#1065）も、実ファイルの取り込み結果が登録表に一致する（手元だけ）。"""
    try:
        pdftotext = M.find_pdftotext()
    except M.IndexListError:
        pytest.skip("pdftotext が無い")
    review = IE.CONFOUNDER_LISTS[key]
    if not (IE.DEFAULT_LISTS_DIR / review.source).is_file():
        pytest.skip("JPX の一覧（.index_lists/raw）が無い")
    _data, sha = M.import_review(key, IE.DEFAULT_LISTS_DIR, pdftotext)
    assert sha == review.normalized_sha256


@pytest.mark.parametrize("key", list(IE.REVIEWS))
def test_real_lists_match_the_registry(key):
    """実ファイルの取り込み結果が登録表の件数と正規化 CSV の sha256 に一致する（手元だけ）。"""
    inputs = _real_inputs()
    if inputs is None:
        pytest.skip("JPX の一覧（.index_lists/raw）か pdftotext が無い")
    data, sha = M.import_review(key, *inputs)
    assert sha == IE.REVIEWS[key].normalized_sha256
