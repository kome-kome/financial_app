"""朝の推奨（/morning）の 1 画面目の約束（#1214）。

主役は買い推奨ランキング。データに問題が無い朝に、鮮度カード 5 枚（「夜間バッチ」は 8 行）が
開いたままだと、毎朝スクロールが 1 回要る。読み込み中の枠が注意色だと、毎朝 5〜8 秒
「注意？」に見える。どちらも**破っても画面は壊れず、失敗として現れない**ので、ここで縛る。

API 側（判定そのもの）は `tests/test_morning.py`。ここは画面のファイルだけを読む。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "templates" / "morning.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "morning.js").read_text(encoding="utf-8")
CSS = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", HTML, flags=re.S))


class TestLoadingVerdictIsNeutral:
    def test_initial_box_is_not_a_status_colour(self):
        box = re.search(r'<div class="([^"]*)" id="verdict">', HTML)
        assert box, "判定の枠（#verdict）を拾えない"
        classes = box.group(1).split()
        assert "verdict-loading" in classes, classes
        assert not {"verdict-warn", "verdict-alert", "verdict-fresh"} & set(classes), (
            f"読み込み中の枠に状態色を付けない（毎朝「注意？」に見える）: {classes}")

    def test_loading_style_uses_neutral_tokens(self):
        rule = re.search(r"\.verdict-loading\s*\{([^}]*)\}", CSS)
        assert rule, ".verdict-loading の CSS が無い"
        assert "--bg-elevated" in rule.group(1), rule.group(1)
        assert "--status-" not in rule.group(1), "中立色に状態色のトークンを使わない"
        head = re.search(r"\.verdict-loading\s+\.verdict-head\s*\{([^}]*)\}", CSS)
        assert head and "--status-" not in head.group(1)

    def test_a_failed_load_does_not_stay_neutral(self):
        """取得に失敗したら赤にする（判定を出せなかった＝発注の根拠が無い）。"""
        catch = JS[JS.index("} catch (e) {"):]
        assert "verdict verdict-alert" in catch


class TestFreshnessFoldFollowsTheVerdict:
    def test_details_start_closed(self):
        tag = re.search(r'<details class="fresh-details" id="fresh-details"([^>]*)>', HTML)
        assert tag, "鮮度の詳細（#fresh-details）を拾えない"
        assert not re.search(r"\bopen\b", tag.group(1)), (
            "既定は閉じておく（読み込み中に空の枠を見せない。開くのは注意・警告のときだけ）")

    def test_fold_is_not_decided_by_width(self):
        assert "matchMedia" not in JS, (
            "鮮度の開閉を幅で決めない（デスクトップ幅でも、問題なしの朝はランキングを 1 画面目に入れる）")

    def test_fold_is_decided_by_the_verdict(self):
        fn = re.search(r"function syncFreshFold\(verdict\)\s*\{(.*?)\n\}", JS, flags=re.S)
        assert fn, "syncFreshFold が無い"
        body = fn.group(1)
        assert re.search(r"\.open\s*=\s*verdict\s*!==\s*'fresh'", body), body
        # 判定が前回と同じなら触らない（プリセットを選び直すたびに、人が開いた詳細を閉じない）
        assert re.search(r"if\s*\(\s*verdict\s*===\s*lastVerdict\s*\)\s*return", body), body
        assert "syncFreshFold(d.freshness.overall_verdict)" in JS

    def test_the_one_line_summary_stays_visible_when_folded(self):
        """畳んでも状態が分かるのは、<summary> に 1 行の要約（✓ / ! / ×）を描いているから。"""
        assert re.search(r'<summary[^>]*id="fresh-summary"', HTML)
        assert "getElementById('fresh-summary')" in JS and "freshChip(" in JS
