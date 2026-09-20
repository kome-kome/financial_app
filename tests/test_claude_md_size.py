"""`CLAUDE.md` が「索引＋必須ルール」から肥大していないかを照合する（#705）。

**伸びたことは失敗として現れない**。テストは落ちず、アプリは動き、notify-failure も
鳴らない。`CLAUDE.md` は冒頭で「索引＋必須ルールに限る（1項目 120字目安）」と宣言して
いるのに、書き足すたびに少しずつ伸びて宣言から離れていく——そして**毎セッション読み
込まれる**ので、伸びた分はそのまま入力トークンの常時コストになる。

2026-09-20 の `/tidy` で「設計制約」節の2項目が 2,023字・1,286字（120字目安の17倍・
10.7倍）まで育っていたのが見つかり、#705 で 1,163字・975字へ圧縮した。気づく手段が
人間の定期点検しか無かった穴なので、`tests/test_docs_sync.py`（副読本の章立て・スキル
索引）や `tests/test_docs_links.py`（相対リンク）と同じ機械化をサイズへ適用する。

**上限は「守れる約束」に置く**。120字は目安であって上限ではない——設計制約の項目には
1つで複数の禁止を束ねているものがあり、120字で切ると命令が落ちる。ここで止めたいのは
「宣言から離れ続けること」なので、上限は**圧縮直後の実測に余裕を足した値**にしてある
（実測から逆算した閾値ではなく、「これ以上は背景を移す」という約束の側から決めた値）。
超えたときに削るのではなく、**背景・実測値・経緯を `docs/` 側へ移す**のが正しい直し方。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# 「設計制約」節の見出し。節を切り出せなくなったら（改名・削除）テストは失敗する。
SECTION_PREFIX = "## 設計制約"

# 箇条書きの項目の始まり。この節の項目はすべて `- **…**` で始まる。
ITEM_PREFIX = "- **"

# 上限。2026-09-20 の #705 圧縮直後の実測（全体 21,024字 / 最大項目 1,163字）に
# 余裕を足した値。**圧縮前の 22,195字 / 2,023字 より小さい**ことが要点で、
# 元の状態へ戻ったら必ず鳴る。
MAX_TOTAL_CHARS = 22_000
MAX_ITEM_CHARS = 1_300

# 空振り検知の下限。抽出が壊れて 0 件になると「上限内」として静かに通る
# （"空を返すハンドラは全件失敗を隠す" 型）。
MIN_ITEMS = 10

HOW_TO_FIX = (
    "CLAUDE.md は索引＋必須ルール。削るのではなく、背景・実測値・経緯を "
    "docs/ARCHITECTURE.md（仕組みと経緯）か docs/GOTCHAS.md（再現条件と回避手順）へ移し、"
    "命令形の1行とリンクだけを残すこと"
)


def section_lines(text: str) -> list[str]:
    """「設計制約」節の行を、次の `## ` 直前まで返す。"""
    out: list[str] = []
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith(SECTION_PREFIX):
                started = True
            continue
        if line.startswith("## "):
            break
        out.append(line)
    return out


def constraint_items(text: str) -> list[tuple[int, str]]:
    """「設計制約」節の項目を `(節内の連番, 本文)` で返す。"""
    return [
        (i, line)
        for i, line in enumerate(section_lines(text), 1)
        if line.startswith(ITEM_PREFIX)
    ]


def item_label(item: str, width: int = 34) -> str:
    """失敗メッセージ用に、項目の先頭を強調記号なしで短く返す。"""
    return item[len(ITEM_PREFIX):].replace("**", "")[:width]


class TestClaudeMdStaysAnIndex:
    """`CLAUDE.md` が索引＋必須ルールの分量に留まっていること。"""

    def test_total_size_is_within_budget(self):
        total = len(CLAUDE_MD.read_text(encoding="utf-8"))
        assert total <= MAX_TOTAL_CHARS, (
            f"CLAUDE.md が {total:,}字（上限 {MAX_TOTAL_CHARS:,}字）。{HOW_TO_FIX}"
        )

    def test_no_single_constraint_is_oversized(self):
        items = constraint_items(CLAUDE_MD.read_text(encoding="utf-8"))
        over = [(n, it) for n, it in items if len(it) > MAX_ITEM_CHARS]
        assert not over, (
            f"「設計制約」節に上限 {MAX_ITEM_CHARS:,}字を超える項目が {len(over)} 件ある。\n"
            + "\n".join(f"  第{n}項 {len(it):,}字: {item_label(it)}…" for n, it in over)
            + f"\n{HOW_TO_FIX}"
        )

    def test_extraction_is_not_vacuous(self):
        """節と項目を拾えていること（0 件抽出は「上限内」に化ける）。"""
        text = CLAUDE_MD.read_text(encoding="utf-8")
        assert section_lines(text), (
            f"CLAUDE.md から「{SECTION_PREFIX}」節を切り出せない。"
            "見出しを変えたならこのテストの SECTION_PREFIX も直すこと"
        )
        items = constraint_items(text)
        assert len(items) >= MIN_ITEMS, (
            f"「設計制約」節から項目を {len(items)} 件しか拾えていない（下限 {MIN_ITEMS}）。"
            f"箇条書きの書式（{ITEM_PREFIX}…）か抽出ロジックが壊れている疑い"
        )


class TestCheckerActuallyDetects:
    """検出器が本当に検出すること（実ファイルを触らず合成ケースで確かめる）。"""

    def _doc(self, *items: str) -> str:
        body = "\n".join(items)
        return f"# T\n\n## 設計制約（変えてはいけないこと）\n\n{body}\n\n## 次の節\n\nx\n"

    def test_oversized_item_is_reported(self):
        text = self._doc(ITEM_PREFIX + "あ**" + "い" * (MAX_ITEM_CHARS + 1))
        over = [it for _, it in constraint_items(text) if len(it) > MAX_ITEM_CHARS]
        assert len(over) == 1

    def test_item_at_the_limit_is_not_reported(self):
        head = ITEM_PREFIX + "あ**"
        text = self._doc(head + "い" * (MAX_ITEM_CHARS - len(head)))
        over = [it for _, it in constraint_items(text) if len(it) > MAX_ITEM_CHARS]
        assert over == []

    def test_section_stops_at_the_next_heading(self):
        """次の節の箇条書きを「設計制約」の項目として数えないこと。"""
        text = (
            "# T\n\n## 設計制約（変えてはいけないこと）\n\n"
            + ITEM_PREFIX + "中**x\n\n"
            "## 別の節\n\n" + ITEM_PREFIX + "外**y\n"
        )
        assert [it for _, it in constraint_items(text)] == [ITEM_PREFIX + "中**x"]

    def test_missing_section_yields_no_lines(self):
        assert section_lines("# T\n\n## 別の節\n\n- **x**\n") == []

    def test_sub_bullets_are_not_counted_as_items(self):
        """字下げした補足は独立した項目として数えない（先頭一致で弾く）。"""
        text = self._doc(ITEM_PREFIX + "親**x", "  " + ITEM_PREFIX + "子**y")
        assert len(constraint_items(text)) == 1
