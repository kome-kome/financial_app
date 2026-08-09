"""正本 `docs/MODELS.md` と初心者向け副読本の章立て同期を CI で守る（Issue #472）。

副読本 `docs/M1_MACRO_MODEL_GUIDE.md` は MODELS.md §9 を噛み砕いた読み物だが、
更新頻度が正本と桁で違う（98 コミット vs 4 コミット・副読本は 2026-06-27 で停止）。
**乖離しても失敗が出ない**ため notify-failure でも `/tidy` のリンク検査でも拾えず、
人間が気づくまで古い説明が残り続ける——ADR-0031 で `heavy` の自動実行登録を CI 必須に
したのと同型の穴。

そこで「章立てが変わったら副読本を見直す」だけを機械化する。本文の細かい変更では
落とさない（副読本は設計思想だけを追随し、系列一覧・既定値・実測値は正本へのリンクに
留める設計のため）。章立てが変わるとき＝噛み砕き直しが要るときだけ落ちる。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
MODELS_MD = DOCS / "MODELS.md"
GUIDE_MD = DOCS / "M1_MACRO_MODEL_GUIDE.md"

MARKER_RE = re.compile(
    r"<!--\s*models-sync:\s*section=(?P<section>[0-9]+)\s+"
    r"headings=(?P<digest>[0-9a-f]{12})\s*-->"
)

DIGEST_LEN = 12


def section_headings(text: str, section: str) -> list[str]:
    """`## <section>. …` から次の `## ` 直前までの見出し行を順序どおり返す。"""
    head_pat = re.compile(rf"^##\s+{re.escape(section)}\.\s")
    out: list[str] = []
    started = False
    for line in text.splitlines():
        if not started:
            if head_pat.match(line):
                started = True
                out.append(line.strip())
            continue
        if line.startswith("## "):
            break
        if line.startswith("#"):
            out.append(line.strip())
    return out


def headings_digest(headings: list[str]) -> str:
    joined = "\n".join(headings)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:DIGEST_LEN]


def read_marker() -> re.Match:
    text = GUIDE_MD.read_text(encoding="utf-8")
    m = MARKER_RE.search(text)
    assert m, (
        f"{GUIDE_MD.name} に models-sync マーカーが無い。冒頭へ "
        "`<!-- models-sync: section=9 headings=<12桁> -->` を置くこと"
        "（値は本テストの失敗メッセージが教える）"
    )
    return m


class TestM1GuideTracksModels:
    """M-1 副読本が MODELS.md §9 の章立てに追随しているか。"""

    def test_marker_points_at_an_existing_section(self):
        section = read_marker().group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        assert headings, (
            f"models-sync マーカーが指す MODELS.md §{section} が見つからない。"
            "章番号を振り直したならマーカー側も直すこと"
        )

    def test_section_has_subheadings(self):
        """`### 9.x` を1つも拾えないなら抽出ロジックが壊れている（検査が空回りする）。"""
        section = read_marker().group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        assert len(headings) >= 2, (
            f"§{section} の小見出しを抽出できていない: {headings}"
        )

    def test_guide_digest_matches_models_headings(self):
        marker = read_marker()
        section = marker.group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        expected = headings_digest(headings)
        assert marker.group("digest") == expected, (
            f"MODELS.md §{section} の章立てが変わっている（副読本は追随していない）。\n"
            f"  現在の章立て: {headings}\n"
            f"  {GUIDE_MD.name} を読み直して噛み砕きの過不足を直したうえで、"
            f"冒頭マーカーを headings={expected} へ更新すること。\n"
            "  （系列の増減・既定値・実測値は副読本の追随対象外＝正本へのリンクに留める）"
        )


class TestGuideDeclaresItsScope:
    """副読本が「何を追随し、何を正本へ委ねるか」を本文で宣言しているか。

    宣言が無いと、次の書き手が良かれと思って系列一覧や実測値を書き写し、
    同じ乖離が再生産される（Issue #472 で実際に起きたのがマクロ系列表）。
    """

    def test_guide_links_to_the_source_of_truth(self):
        text = GUIDE_MD.read_text(encoding="utf-8")
        assert "_MACRO_MAP" in text, (
            "副読本にマクロ系列の正本（plugins/macro_snapshots.py の _MACRO_MAP）への"
            "言及が無い。系列一覧を本書へ書き写すと必ず陳腐化する"
        )
        assert "MODELS.md" in text, "副読本から正本 MODELS.md への参照が消えている"
