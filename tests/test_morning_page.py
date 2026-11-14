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


class TestTradeCandidates:
    """今日の売買候補（#1228）。実際の売買に使う一覧を、財務指標のランキングの上に並べる。

    配線が外れても画面は壊れず「読み込み中…」のまま残るだけ＝失敗として現れない。
    """

    BLOCK = JS[JS.index("// ── 今日の売買候補"):JS.index("// data-change 属性を持つコントロール")]

    def test_candidates_sit_above_the_preset_ranking(self):
        """判定・鮮度のすぐ下に置く（2026-11-14 に決定。ランキングの下では送らないと見えない）。"""
        order = [HTML.index(s) for s in (
            'id="fresh-details"', ">今日の売買候補<", ">買い推奨ランキング<")]
        assert order == sorted(order), order

    def test_every_element_the_script_fills_exists(self):
        ids = set(re.findall(r"getElementById\('(cand-[\w-]+)'\)", self.BLOCK))
        for key in ("enet", "gbdt"):
            ids |= {f"cand-{key}-asof", f"cand-{key}-body"}
        missing = [i for i in sorted(ids) if f'id="{i}"' not in HTML]
        assert len(ids) >= 6 and not missing, missing

    def test_reads_the_saved_scores_of_both_models_and_the_ledger_holdings(self):
        assert "name: 'macro_enet'" in self.BLOCK and "name: 'macro_gbdt'" in self.BLOCK
        assert "/saved?limit=${CAND_TOP_K}" in self.BLOCK
        assert "'/api/portfolio/sell-candidates'" in self.BLOCK
        assert re.search(r"const CAND_TOP_K = 10;", self.BLOCK), "運用の単位は 1 モデルの上位 10 社（ADR-0070）"

    def test_one_failed_request_does_not_blank_the_others(self):
        assert "Promise.allSettled(" in self.BLOCK

    def test_candidates_load_once_and_not_on_preset_change(self):
        """別の並びなので、プリセット・件数を選び直しても読み直さない。"""
        assert JS.count("loadCandidates()") == 2          # 定義と、最後の 1 回の呼び出し
        assert JS.rstrip().endswith("loadCandidates();")

    def test_mu_is_shown_beside_the_ranking_not_mixed_into_it(self):
        """μ̂ を財務指標の点数へ混ぜない（混ぜるなら昇格ゲートが要る・ADR-0030）。"""
        call = re.search(r"apiFetch\(`/api/morning\?([^`]*)`\)", JS)
        assert call and "mu" not in call.group(1) and "weights" not in call.group(1), call.group(0)

    def test_stale_m6_scores_are_flagged_where_the_list_is(self):
        assert "markMuAsof((d.freshness.mu || {}).level)" in JS
        assert re.search(r"\.cand-asof\.stale-asof\s*\{[^}]*--status-warn-text", CSS)

    def test_sell_card_links_to_where_trades_are_recorded(self):
        assert 'class="cand-more" href="/analysis?tab=sell_ranking"' in HTML
