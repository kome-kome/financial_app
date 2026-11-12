"""画面のアクセシビリティ規則の静的検証（#850）。

ここで縛る規則は、どれも破っても画面は壊れず、失敗として現れない:

- 補足文字（`--text-muted`）が背景に対して WCAG AA（4.5:1）に届かない
  （#850 以前はライト約 2.5:1・ダーク約 4.0:1 で、鮮度カードのラベルや表の見出しが読みにくかった）
- `outline:none` でフォーカス枠を消す（重みのスライダーはフォーカスが全く見えなかった）
- 11px 未満の文字（9〜10.5px が 62 箇所あった）
- 用語ツールチップ（分析画面の `.gloss`・/guide の `.term`）がホバー専用で、キーボードでは説明に届かない
  （#850 は `.gloss` だけを照合していて、同じ作りの `.term` が外れていた・#893）
- `showNotif` を呼ぶのにトーストの CSS が無い（#850 まで一度も無く、エラーはページ最下部に
  装飾なしで足されて 4 秒で消えていた＝誰の目にも入っていなかった）

テンプレートと静的 JS をファイルとして読むだけで、アプリも DB も起動しない。
"""
import glob
import os
import re

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = sorted(glob.glob(os.path.join(BASE_DIR, "templates", "*.html")))
STATIC_JS = sorted(glob.glob(os.path.join(BASE_DIR, "static", "js", "*.js")))

AA_TEXT = 4.5          # WCAG 2.x AA・通常サイズの文字
MIN_FONT_PX = 11.0     # 意味を持つ文字の下限
# `--text-muted` が載りうる背景（カード・地・表の行ホバー・入力欄）
MUTED_BACKGROUNDS = ("--bg", "--bg-elevated", "--bg-sunken", "--bg-hover")
NOTIF_CSS_SELECTORS = (".notif-stack{", ".notif{", ".notif-error{", ".notif-close{")
# ホバーで ::after の吹き出しを出す用語のクラス（分析画面・/guide）
TOOLTIP_CLASSES = ("gloss", "term")

_ROOT_RE = re.compile(r'(:root(?:\[data-theme="light"\])?)\{(.*?)\}', re.S)
_TOKEN_RE = re.compile(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;")
_FONT_RE = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)px")
_OUTLINE_OFF_RE = re.compile(r"outline\s*:\s*(?:none|0)\b")
_FOCUS_VISIBLE_RE = re.compile(r":focus-visible\{outline:\s*2px solid var\(--accent-text\)")
_SCRIPT_RE = re.compile(r'<script src="/static/js/([\w-]+\.js)')
_SELECTOR_LIST_RE = re.compile(r"([^{}]+)\{")
_NOTIF_CALL_RE = re.compile(r"(?<!function )\bshowNotif\(")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _name(path):
    return os.path.basename(path)


def _luminance(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _root_blocks(text):
    return {sel: dict(_TOKEN_RE.findall(body)) for sel, body in _ROOT_RE.findall(text)}


def test_contrast_formula_matches_wcag_reference_values():
    # 測定器の較正: 白黒は 21:1、同色は 1:1（式が崩れると全テンプレートが素通りする）
    assert round(contrast("#000000", "#ffffff"), 2) == 21.0
    assert contrast("#777777", "#777777") == 1.0


def test_every_template_defines_both_themes():
    for path in TEMPLATES:
        blocks = _root_blocks(_read(path))
        assert set(blocks) == {":root", ':root[data-theme="light"]'}, _name(path)


# 2026-11 時点のテンプレートの枚数。**これより少なく拾えたら空振り**（glob やパターンが外れても、
# 比べる相手が 1 枚だけなら同一性の照合は必ず通る）。増えるのは構わない。
MIN_TEMPLATES_WITH_TOKENS = 9


def _raw_root_blocks(text):
    """`:root` と `:root[data-theme="light"]` の本文を、空白を除いた生の文字列で返す。

    `_root_blocks` は `#rrggbb` の値しか拾わないので、rgba や var() のトークンの食い違いを見落とす。
    """
    return {sel: re.sub(r"\s+", "", body) for sel, body in _ROOT_RE.findall(text)}


def test_token_blocks_are_identical_in_every_template():
    """#1132: 9 枚のトークン 2 ブロックは同一のコピー（DESIGN.md「トークン」）。1 枚だけ値を変えても
    画面は壊れず、テーマを切り替えた先でだけ色がずれる。"""
    blocks = {_name(p): _raw_root_blocks(_read(p)) for p in TEMPLATES}
    blocks = {name: b for name, b in blocks.items() if len(b) == 2}
    assert len(blocks) >= MIN_TEMPLATES_WITH_TOKENS, sorted(blocks)
    (ref_name, ref), *rest = sorted(blocks.items())
    failures = []
    for name, b in rest:
        for sel in ref:
            if b.get(sel) != ref[sel]:
                theirs = dict(_TOKEN_RE.findall(b.get(sel, "")))
                mine = dict(_TOKEN_RE.findall(ref[sel]))
                diff = sorted(k for k in set(mine) | set(theirs) if mine.get(k) != theirs.get(k))
                failures.append(f"{name} {sel} が {ref_name} と違う（#rrggbb の差: {diff or 'それ以外の値'}）")
    assert not failures, "\n".join(failures)


def test_muted_text_meets_aa_on_every_background():
    failures = []
    for path in TEMPLATES:
        for sel, tokens in _root_blocks(_read(path)).items():
            muted = tokens["--text-muted"]
            for bg in MUTED_BACKGROUNDS:
                ratio = contrast(muted, tokens[bg])
                if ratio < AA_TEXT:
                    failures.append(f"{_name(path)} {sel} --text-muted {muted} on {bg} {tokens[bg]}: {ratio:.2f}")
    assert not failures, "\n".join(failures)


def test_no_focus_outline_is_removed():
    hits = [
        f"{_name(p)}:{text.count(chr(10), 0, m.start()) + 1}"
        for p in TEMPLATES + STATIC_JS
        for text in [_read(p)]
        for m in _OUTLINE_OFF_RE.finditer(text)
    ]
    assert not hits, f"outline:none / outline:0 はフォーカス位置を消す（:focus-visible の枠に任せる）: {hits}"


def test_every_template_draws_a_focus_ring():
    missing = [_name(p) for p in TEMPLATES if not _FOCUS_VISIBLE_RE.search(_read(p))]
    assert not missing, f":focus-visible の枠が無い: {missing}"


def test_no_text_below_minimum_size():
    small = [
        f"{_name(p)}:{text.count(chr(10), 0, m.start()) + 1} {m.group(0)}"
        for p in TEMPLATES + STATIC_JS
        for text in [_read(p)]
        for m in _FONT_RE.finditer(text)
        if float(m.group(1)) < MIN_FONT_PX
    ]
    assert not small, "\n".join(small)


def _tooltip_tag_re(cls):
    # class 属性の中の単語として照合する（`term-xxx` のような別のクラスを拾わない）
    return re.compile(rf'<[a-z]+\b[^>]*\bclass="(?:[^"]*\s)?{cls}(?:\s[^"]*)?"[^>]*>')


def test_glossary_tooltips_are_keyboard_reachable():
    for cls in TOOLTIP_CLASSES:
        tags = [
            (_name(p), tag)
            for p in TEMPLATES + STATIC_JS
            for tag in _tooltip_tag_re(cls).findall(_read(p))
        ]
        assert tags, f"`.{cls}` を1つも拾えない（検出の正規表現が実体とずれた）"
        unreachable = [f"{name}: {tag[:80]}" for name, tag in tags if 'tabindex="0"' not in tag]
        assert not unreachable, "\n".join(unreachable)
        # ホバーで開く規則ごとにフォーカスを並べる。ファイルのどこかに `:focus::after` があるかだけを見ると、
        # @starting-style（フェードの開始）側に残った1つで、開く規則から外しても通ってしまう（#893 で実測）
        for path in TEMPLATES:
            text = _read(path)
            if f".{cls}::after{{" not in text:
                continue
            hover_rules = [s.strip() for s in _SELECTOR_LIST_RE.findall(text) if f".{cls}:hover::after" in s]
            assert hover_rules, f"{_name(path)}: `.{cls}:hover::after` の規則を拾えない（検出の正規表現が実体とずれた）"
            no_focus = [s for s in hover_rules if f".{cls}:focus::after" not in s]
            assert not no_focus, f"{_name(path)}: `.{cls}` はフォーカスでもツールチップを出す: {no_focus}"


def test_glossary_tooltips_flip_inside_the_page():
    """右端の用語の吹き出しが画面の外へはみ出さない（#888・#893・#1137）。

    位置合わせは common.js の placeTip が 1 つで受け持つ。どのツールチップのクラスも
    (1) common.js の対象に入っていて、(2) CSS が --tip-room で外寸を抑え、.tip-flip で左へ開く。
    片方だけでは効かない（JS が測っても CSS が受けなければ動かず、CSS だけでは誰も印を付けない）。
    """
    common = _read(next(p for p in STATIC_JS if _name(p) == "common.js"))
    m = re.search(r"const TIP_SELECTOR = '([^']+)'", common)
    assert m, "common.js の TIP_SELECTOR を拾えない（検出の正規表現が実体とずれた）"
    selectors = {s.strip() for s in m.group(1).split(",")}
    # 複製に戻さない: placeTip は common.js だけに置く
    dup = [_name(p) for p in STATIC_JS if _name(p) != "common.js" and "function placeTip" in _read(p)]
    assert not dup, f"placeTip が common.js 以外にもある（複製は片方だけ直って食い違う）: {dup}"
    for cls in TOOLTIP_CLASSES:
        assert f".{cls}" in selectors, f"`.{cls}` が common.js の TIP_SELECTOR に無い"
        styled = [p for p in TEMPLATES if f".{cls}::after{{" in _read(p)]
        assert styled, f"`.{cls}::after` を持つ画面を拾えない"
        for path in styled:
            text = _read(path)
            assert "max-width:min(280px,var(--tip-room,280px))" in text, \
                f"{_name(path)}: `.{cls}::after` の max-width が --tip-room を受けない"
            assert f".{cls}.tip-flip::after{{left:auto;right:calc(0px - var(--tip-shift,0px))}}" in text, \
                f"{_name(path)}: `.{cls}.tip-flip::after` の規則が無い（左へ開けない）"
            script = re.search(r'<script src="/static/js/common\.js[^"]*"></script>', text)
            assert script, f"{_name(path)}: common.js を読み込んでいない（placeTip が動かない）"


def test_pages_that_raise_toasts_style_them():
    js = {_name(p): _read(p) for p in STATIC_JS}
    callers = []
    for path in TEMPLATES:
        text = _read(path)
        if any(_NOTIF_CALL_RE.search(js.get(src, "")) for src in _SCRIPT_RE.findall(text)):
            callers.append(_name(path))
            missing = [s for s in NOTIF_CSS_SELECTORS if s not in text]
            assert not missing, f"{_name(path)} は showNotif を呼ぶがトーストの CSS が無い: {missing}"
    assert callers, "showNotif を呼ぶ画面を1つも拾えない（検出の正規表現が実体とずれた）"


# ── 分析画面のキーボード・読み上げ・ライトテーマの対比（#1176）────────────────────────────
# どれも破っても画面は壊れず、マウスと目で使う人には違いが見えない。

_ANALYSIS_HTML = os.path.join(BASE_DIR, "templates", "analysis.html")
_ANALYSIS_JS = os.path.join(BASE_DIR, "static", "js", "analysis.js")

# JS が組み立てる inline style の中の直書きの色。トークン（var(--…)）ならテーマに追随するが、
# 直書きはライトテーマの白地で読めなくなる（#c084fc は白地で約 2.6:1 だった）。
_JS_INLINE_HEX_RE = re.compile(
    r"\b(?:color|background(?:-color)?|border(?:-[a-z]+)*|fill|stroke)\s*:\s*#[0-9a-fA-F]{3,8}\b")
# 色トークンの表などに置いた引用符付きの色の値
_QUOTED_HEX_RE = re.compile(r"""['"]#[0-9a-fA-F]{3,8}['"]""")
_LABEL_RE = re.compile(r"<label\b([^>]*)>(.*?)</label>", re.S)
_HEADING_DIV_RE = re.compile(r'<div class="section-title"')


def test_js_inline_styles_use_color_tokens():
    hits = [
        f"{_name(p)}:{text.count(chr(10), 0, m.start()) + 1} {m.group(0)}"
        for p in STATIC_JS
        for text in [_read(p)]
        for m in _JS_INLINE_HEX_RE.finditer(text)
    ]
    assert not hits, "JS の inline style は var(--…) のトークンで書く（DESIGN.md）:\n" + "\n".join(hits)


def test_js_inline_hex_detector_catches_the_old_form():
    # 測定器の較正: #1176 で直した形を必ず拾う（正規表現が外れると上のテストが素通りする）
    assert _JS_INLINE_HEX_RE.search('<div style="font-size:15px;font-weight:700;color:#c084fc">')
    assert _JS_INLINE_HEX_RE.search("background:#c084fc;border-radius:0 3px 3px 0")
    assert not _JS_INLINE_HEX_RE.search("color:var(--accent-text)")


def test_analysis_js_has_no_hex_color_values():
    # 色の表（例: MRR_COEF_COLOR_TOKENS）もトークン名で持つ。cssVar() で描く直前に解決する。
    # 企業詳細（company.js）のチャートの系列パレットは未移行のため、ここでは分析画面だけを縛る。
    text = _read(_ANALYSIS_JS)
    hits = [f"analysis.js:{text.count(chr(10), 0, m.start()) + 1} {m.group(0)}"
            for m in _QUOTED_HEX_RE.finditer(text)]
    assert not hits, "\n".join(hits)


# ラベルを照合する画面 → `<label>` を持つファイル（先頭がテンプレート）。収集画面は #1186 で足した
# （collection.js は `<label>` を組み立てない）
_LABEL_SCREENS = {
    "analysis": (_ANALYSIS_HTML, _ANALYSIS_JS),
    "collection": (os.path.join(BASE_DIR, "templates", "collection.html"),),
}


@pytest.mark.parametrize("screen", sorted(_LABEL_SCREENS))
def test_labels_name_their_field(screen):
    """`<label>` は for で入力欄と結ぶか、入力欄を中に含む（無いと読み上げで「textbox」としか読まれない）。"""
    unbound = []
    for path in _LABEL_SCREENS[screen]:
        text = _read(path)
        labels = _LABEL_RE.findall(text)
        assert labels, f"{_name(path)}: <label> を1つも拾えない（検出の正規表現が実体とずれた）"
        for attrs, inner in labels:
            if "for=" in attrs or re.search(r"<(?:input|select|textarea)\b", inner):
                continue
            unbound.append(f"{_name(path)}: <label{attrs}>{inner[:40]}")
    assert not unbound, "\n".join(unbound)


@pytest.mark.parametrize("screen", sorted(_LABEL_SCREENS))
def test_label_for_targets_exist(screen):
    """静的な `<label for>` の行き先の id が同じテンプレートにある（id の書き換えで黙って外れる）。"""
    text = _read(_LABEL_SCREENS[screen][0])
    targets = re.findall(r'<label\b[^>]*\bfor="([^"]+)"', text)
    assert targets, "テンプレートの <label for> を拾えない"
    missing = [t for t in targets if f'id="{t}"' not in text]
    assert not missing, missing


def test_analysis_section_titles_are_headings():
    """カードの見出しは h2（小見出しは h3）。div のままだと見出しで移動できない（h1 しか無かった）。"""
    for path in (_ANALYSIS_HTML, _ANALYSIS_JS):
        text = _read(path)
        assert not _HEADING_DIV_RE.search(text), f"{_name(path)}: section-title が div のまま"
        assert re.search(r'<h2 class="section-title"', text), f"{_name(path)}: h2 の見出しを拾えない"


def test_analysis_skip_link_reaches_the_main_content():
    text = _read(_ANALYSIS_HTML)
    m = re.search(r'<a class="skip-link" href="#([\w-]+)">', text)
    assert m, "本文へのスキップリンクが無い（ナビ 8・サイドバー 12 を Tab で抜ける必要がある）"
    assert re.search(rf'<main\b[^>]*\bid="{m.group(1)}"', text), "スキップリンクの行き先が main に無い"
    assert ".skip-link:focus{" in text, "スキップリンクがフォーカスで現れない"
    # 本文より前（ナビより前）に置く。後ろに置くと Tab で先にナビを抜けることになる
    assert text.index('class="skip-link"') < text.index('<nav class="gnav">')


def test_analysis_sidebar_marks_the_current_tab():
    js = _read(_ANALYSIS_JS)
    body = js[js.index("function showTab("):]
    body = body[:body.index("\n}\n")]
    assert "setAttribute('aria-current', 'true')" in body, "現在のタブが色だけで示されている"
    assert "removeAttribute('aria-current')" in body, "前のタブの aria-current が残る"


def test_analysis_result_rows_have_keyboard_entry_and_company_links():
    """M-2/M-3/M-6 の結果の行: 詳細はボタンで開け、銘柄名は企業詳細へのリンク（M-1 の表と同じ）。"""
    js = _read(_ANALYSIS_JS)
    for click in ("_rrSelectRow", "_dlmSelectRow"):
        assert re.search(rf'_rowDetailBtn\([^;]*data-click="{click}"', js, re.S), \
            f"{click} を開くボタンが無い（行のクリックはマウス専用）"
    assert 'class="row-detail"' in js and ".row-detail{" in _read(_ANALYSIS_HTML)
    assert js.count("_companyLink(r.edinet_code, name)") >= 2, "M-2/M-6 と M-3 の銘柄名がリンクになっていない"
    # 行の中のリンクは行の data-click を起こさない（企業詳細を開くたびに詳細パネルも開いてしまう）
    delegate = js[js.index("function _wireDelegate("):]
    assert "closest('a[href]')" in delegate[:800]
