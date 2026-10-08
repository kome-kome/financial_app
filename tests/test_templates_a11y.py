"""画面のアクセシビリティ規則の静的検証（#850）。

ここで縛る規則は、どれも破っても画面は壊れず、失敗として現れない:

- 補足文字（`--text-muted`）が背景に対して WCAG AA（4.5:1）に届かない
  （#850 以前はライト約 2.5:1・ダーク約 4.0:1 で、鮮度カードのラベルや表の見出しが読みにくかった）
- `outline:none` でフォーカス枠を消す（重みのスライダーはフォーカスが全く見えなかった）
- 11px 未満の文字（9〜10.5px が 62 箇所あった）
- 用語ツールチップ（`.gloss`）がホバー専用で、キーボードでは説明に届かない
- `showNotif` を呼ぶのにトーストの CSS が無い（#850 まで一度も無く、エラーはページ最下部に
  装飾なしで足されて 4 秒で消えていた＝誰の目にも入っていなかった）

テンプレートと静的 JS をファイルとして読むだけで、アプリも DB も起動しない。
"""
import glob
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = sorted(glob.glob(os.path.join(BASE_DIR, "templates", "*.html")))
STATIC_JS = sorted(glob.glob(os.path.join(BASE_DIR, "static", "js", "*.js")))

AA_TEXT = 4.5          # WCAG 2.x AA・通常サイズの文字
MIN_FONT_PX = 11.0     # 意味を持つ文字の下限
# `--text-muted` が載りうる背景（カード・地・表の行ホバー・入力欄）
MUTED_BACKGROUNDS = ("--bg", "--bg-elevated", "--bg-sunken", "--bg-hover")
NOTIF_CSS_SELECTORS = (".notif-stack{", ".notif{", ".notif-error{", ".notif-close{")

_ROOT_RE = re.compile(r'(:root(?:\[data-theme="light"\])?)\{(.*?)\}', re.S)
_TOKEN_RE = re.compile(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;")
_FONT_RE = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)px")
_OUTLINE_OFF_RE = re.compile(r"outline\s*:\s*(?:none|0)\b")
_FOCUS_VISIBLE_RE = re.compile(r":focus-visible\{outline:\s*2px solid var\(--accent-text\)")
_GLOSS_TAG_RE = re.compile(r'<[a-z]+\b[^>]*\bclass="[^"]*\bgloss\b[^"]*"[^>]*>')
_SCRIPT_RE = re.compile(r'<script src="/static/js/([\w-]+\.js)')
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


def test_glossary_tooltips_are_keyboard_reachable():
    tags = [
        (_name(p), tag)
        for p in TEMPLATES + STATIC_JS
        for tag in _GLOSS_TAG_RE.findall(_read(p))
    ]
    assert tags, "`.gloss` を1つも拾えない（検出の正規表現が実体とずれた）"
    unreachable = [f"{name}: {tag[:80]}" for name, tag in tags if 'tabindex="0"' not in tag]
    assert not unreachable, "\n".join(unreachable)
    for path in TEMPLATES:
        text = _read(path)
        if ".gloss::after{" in text:
            assert ".gloss:focus::after" in text, f"{_name(path)}: フォーカスでもツールチップを出す"


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
