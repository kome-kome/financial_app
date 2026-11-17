"""業務画面の文言に、利用者に意味の無い Issue 番号・ADR 番号を出さない（#1264）。

経緯はコードのコメントと ADR に残す。画面の説明文に「（#815）」「ADR-0045」と書くと、ヘルプと用語を
頼りに読む初見の利用者（数か月後の自分を含む）には意味の無い記号が並ぶだけになる。技術版の
/models と /guide の「もっと詳しく」は開発の経緯を読む場所なので対象外。

破っても画面は壊れず失敗として現れないので、ここで縛る。
"""
import json
import os
import re

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 業務画面（/guide・/models・ログインを除く）
BUSINESS_TEMPLATES = ("analysis", "morning", "dashboard", "collection", "company", "db")
BUSINESS_JS = ("analysis", "morning", "dashboard", "collection", "company", "db", "common")

_NUMBER_RE = re.compile(r"(?:Issue\s*)?#\d{3,4}\b|ADR-\d{4}")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _visible_template_text(text):
    for pat in (r"<!--.*?-->", r"<style\b.*?</style>", r"<script\b.*?</script>"):
        text = re.sub(pat, "", text, flags=re.S)
    return text


def _js_without_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        lines.append(re.sub(r"\s//\s.*$", "", line))   # 行末のコメント（文字列の中の URL の // は前後が空白でない）
    return "\n".join(lines)


def _hits(text):
    return [m.group(0) for m in _NUMBER_RE.finditer(text)]


@pytest.mark.parametrize("page", BUSINESS_TEMPLATES)
def test_templates_show_no_issue_numbers(page):
    text = _visible_template_text(_read(os.path.join(BASE_DIR, "templates", f"{page}.html")))
    assert not _hits(text), f"{page}.html の画面の文言に番号: {_hits(text)}"


@pytest.mark.parametrize("name", BUSINESS_JS)
def test_js_strings_show_no_issue_numbers(name):
    text = _js_without_comments(_read(os.path.join(BASE_DIR, "static", "js", f"{name}.js")))
    assert not _hits(text), f"{name}.js の表示文字列に番号: {_hits(text)}"


def test_plugin_descriptions_show_no_issue_numbers():
    """分析画面のフォーム・説明・選択肢はプラグインの `to_meta()` がそのまま画面へ渡る。"""
    import plugins
    bad = {}
    for p in plugins.list_plugins():
        if p.hidden:
            continue
        hits = _hits(json.dumps(p.to_meta(), ensure_ascii=False))
        if hits:
            bad[p.name] = hits
    assert not bad, f"プラグインの画面向けの文言に番号（経緯はコードのコメントへ）: {bad}"


def test_detector_catches_the_old_forms():
    # 測定器の較正: #1264 で外した形を拾い、コメントの中の番号は拾わない
    assert _hits("ありませんでしたが（#815）、既定を 0 にしました。")
    assert _hits("詳細は ADR-0045。") and _hits("売り判定の質を測れない(Issue #402)。")
    assert not _hits(_js_without_comments("// 経緯は #1264\nconst a = 1;  // #815\n/* ADR-0045 */"))
    assert not _hits(_visible_template_text("<!-- #423 --><style>.x{}/* #888 */</style><p>本文</p>"))
