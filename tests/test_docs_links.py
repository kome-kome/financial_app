"""Markdown の相対リンクが実在するかを照合する（/tidy 2026-09-12）。

**リンクが切れても失敗として現れない**。テストは落ちず、アプリは動き、notify-failure も
鳴らない。気づく手段が人間の定期点検（`/tidy`）しか無かった穴で、`tests/test_docs_sync.py`
が副読本の章立てとスキル索引に対してやっているのと同じ機械化をリンクへ適用する。

実際に取り逃していた3件（2026-09-12 の `/tidy` で検出）:

| リンク元 | 書かれていたリンク先 | 実体 |
|---|---|---|
| `docs/DEPLOYMENT.md` | `adr/0047-persisted-scores-need-their-panel.md` | `adr/0047-tuning-gate-compares-on-one-panel.md` |
| `docs/adr/0050-…` | `0019-conservative-feature-gate.md` | `0019-m2-monotone-constraints-economic-sign-priors.md` |
| `docs/adr/0050-…` | `0041-preset-promotion-gate-has-a-cli.md` | `0041-preset-weight-gate-has-an-implementation.md` |

3件とも **ADR 番号は正しく、リネーム後のファイル名に追随しなかった**だけだった。だから
失敗メッセージは「壊れている」で終わらせず、同じ番号・同じベース名のファイルを候補として
出す（候補が出れば修正先がその場で決まる）。

外部 URL（http/https/mailto）と同一ファイル内アンカー（`#…`）は対象外。前者はネットワーク
に依存して CI を不安定にし、後者は見出しの表記ゆれで偽陽性を量産するため。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 走査から外すディレクトリ。venv は依存パッケージ同梱の .md が数百本あり、
# こちらの責任範囲ではない。
EXCLUDED_DIRS = {
    ".git",
    ".logs",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "venv",
}

EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:")

# `[label](target)` — target に空白は含めず、末尾の `"title"` は捨てる。
LINK_RE = re.compile(r"\[([^\]\[]*)\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")

# 空振り検知の下限。正規表現や走査が壊れて 0 件になったとき、「リンク切れゼロ」
# として静かに通るのを防ぐ（"空を返すハンドラは全件失敗を隠す" 型）。
MIN_MARKDOWN_FILES = 40
MIN_RELATIVE_LINKS = 200

ADR_NUM_RE = re.compile(r"^(\d{4})-")


def markdown_files() -> list[Path]:
    """リポジトリ配下の .md を走査対象順に返す。"""
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.md"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        out.append(path)
    return sorted(out)


def relative_links(path: Path) -> list[tuple[int, str, str]]:
    """`path` 内の相対リンクを `(行番号, リンクテキスト, リンク先)` で返す。"""
    out: list[tuple[int, str, str]] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, target in LINK_RE.findall(line):
            if target.startswith(EXTERNAL_PREFIXES) or target.startswith("#"):
                continue
            body = target.split("#", 1)[0]
            if not body:  # `[x](#anchor)` は上で弾いているが念のため
                continue
            out.append((lineno, label, target))
    return out


def rename_candidates(target: str) -> list[str]:
    """リネーム追随漏れを直せるように、同名・同 ADR 番号のファイルを探す。"""
    name = Path(target.split("#", 1)[0]).name
    candidates: list[str] = []
    num = ADR_NUM_RE.match(name)
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        if path.name == name:
            candidates.append(path.relative_to(REPO_ROOT).as_posix())
        elif num and path.suffix == ".md" and path.name.startswith(num.group(1) + "-"):
            candidates.append(path.relative_to(REPO_ROOT).as_posix())
    return sorted(set(candidates))


def broken_links(files: list[Path] | None = None) -> list[tuple[Path, int, str, str]]:
    """壊れた相対リンクを `(リンク元, 行番号, リンクテキスト, リンク先)` で返す。"""
    out: list[tuple[Path, int, str, str]] = []
    for path in files if files is not None else markdown_files():
        for lineno, label, target in relative_links(path):
            body = target.split("#", 1)[0]
            if not (path.parent / body).exists():
                out.append((path, lineno, label, target))
    return out


class TestMarkdownLinksResolve:
    """.md の相対リンクが実在すること。"""

    def test_no_broken_relative_links(self):
        broken = broken_links()
        if not broken:
            return
        lines = []
        for path, lineno, label, target in broken:
            rel = path.relative_to(REPO_ROOT).as_posix()
            cands = rename_candidates(target)
            hint = f"  → 候補: {', '.join(cands)}" if cands else "  → 同名候補なし"
            lines.append(f"  {rel}:{lineno}  [{label}]({target})\n{hint}")
        assert False, (
            f"実在しないリンク先が {len(broken)} 件ある。"
            "リンク先ファイルをリネームしたときは、そのファイルを指す全リンクを直すこと。\n"
            + "\n".join(lines)
        )

    def test_scan_is_not_vacuous(self):
        """走査が空振りしていないこと（0 件抽出は「全部 OK」に化ける）。"""
        files = markdown_files()
        assert len(files) >= MIN_MARKDOWN_FILES, (
            f".md の走査が {len(files)} 本しか拾えていない（下限 {MIN_MARKDOWN_FILES}）。"
            f"EXCLUDED_DIRS か走査ロジックが壊れている疑い"
        )
        total = sum(len(relative_links(p)) for p in files)
        assert total >= MIN_RELATIVE_LINKS, (
            f"相対リンクが {total} 件しか抽出できていない（下限 {MIN_RELATIVE_LINKS}）。"
            f"LINK_RE が壊れると壊れたリンクも抽出されず、検査が静かに無効化される"
        )

    def test_core_docs_are_scanned(self):
        """索引の中心となる文書が走査対象に入っていること。"""
        scanned = {p.relative_to(REPO_ROOT).as_posix() for p in markdown_files()}
        for required in (
            "CLAUDE.md",
            "docs/ARCHITECTURE.md",
            "docs/DEPLOYMENT.md",
            "docs/adr/README.md",
        ):
            assert required in scanned, f"{required} が走査対象から外れている"


class TestCheckerActuallyDetects:
    """検出器が本当に検出すること（実ファイルを壊さず合成ケースで確かめる）。"""

    def test_missing_target_is_reported(self, tmp_path):
        doc = tmp_path / "a.md"
        doc.write_text("[ADR-0099](adr/0099-nope.md)\n", encoding="utf-8")
        assert broken_links([doc]) == [(doc, 1, "ADR-0099", "adr/0099-nope.md")]

    def test_existing_target_is_not_reported(self, tmp_path):
        (tmp_path / "b.md").write_text("ok\n", encoding="utf-8")
        doc = tmp_path / "a.md"
        doc.write_text("[B](b.md)\n", encoding="utf-8")
        assert broken_links([doc]) == []

    def test_anchor_suffix_is_stripped_before_resolving(self, tmp_path):
        (tmp_path / "b.md").write_text("# H\n", encoding="utf-8")
        doc = tmp_path / "a.md"
        doc.write_text("[B](b.md#h)\n", encoding="utf-8")
        assert broken_links([doc]) == []

    def test_external_and_self_anchor_links_are_skipped(self, tmp_path):
        doc = tmp_path / "a.md"
        doc.write_text(
            "[x](https://example.com/nope.md)\n"
            "[y](mailto:a@example.com)\n"
            "[z](#section)\n",
            encoding="utf-8",
        )
        assert relative_links(doc) == []

    def test_link_title_is_not_part_of_the_target(self, tmp_path):
        (tmp_path / "b.md").write_text("ok\n", encoding="utf-8")
        doc = tmp_path / "a.md"
        doc.write_text('[B](b.md "タイトル")\n', encoding="utf-8")
        assert broken_links([doc]) == []

    def test_rename_candidate_is_offered_by_adr_number(self):
        """ADR 番号が同じ実ファイルを候補として出す（今回の3件の直し方そのもの）。"""
        cands = rename_candidates("adr/0047-persisted-scores-need-their-panel.md")
        assert any("0047-" in c for c in cands), cands
