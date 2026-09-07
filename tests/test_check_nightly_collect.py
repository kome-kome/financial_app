"""`scripts/check_nightly_collect.py` のパースと判定（#556・#620）。

ログ文字列は**実ログから写した**もの（`.logs/nightly_2026090*.log`）。`_pipeline_incremental.py`
の書式を変えたらここが落ちる——それが狙いで、書式変更は「読めなくなった」ことを静かな
劣化ではなくテストの失敗として現す。
"""
import pytest

from scripts._textwidth import display_width, pad
from scripts.check_nightly_collect import (
    parse_nightly_log, seconds_per_company, warnings_for,
)

# #622／#620 の適用前（2026-09-07 の実ログ）。並行度・HTTP失敗・往復段差の行がまだ無い。
LOG_BEFORE = """\
2026-09-07 17:38:52,357 INFO fill_recent_stock_price_gap_yahoo: 4078/4440社を補完（基準セッション 2026-09-07 / JST 2026-09-07 17:38 ・最古起点 20260308 〜 20260907 ・価格ゼロ 416社（うち解決済み 0社））
[17:44:16] [Yahoo gap-fill 500/4078]
2026-09-07 18:22:56,659 INFO fill_recent_stock_price_gap_yahoo: 14888件を株価テーブルへ集約保存（うち新規日付 3665件）
[18:22:56]   Yahoo Finance gap-fill: 14888件 投入（うち新規日付 3665件・20260308 〜 20260907・4078社・基準セッション 2026-09-07）
[18:22:56]   価格ゼロ 416社（うち解決済み 0社）
[18:23:10]   J-Quants catchup (2026-06-09〜2026-06-19): 18470件 upsert
[18:24:41]   株価鮮度: p50=2026-09-07 / p05=2026-09-07 / max=2026-09-07 / level=fresh（3823銘柄・5営業日超の遅れ 88銘柄）
"""

# #622／#620 の適用後（今夜以降に出るはずの形）。
LOG_AFTER = """\
2026-09-08 17:30:00,000 INFO fill_recent_stock_price_gap_yahoo: 4080/4440社を補完（基準セッション 2026-09-08）
2026-09-08 17:45:30,000 INFO fill_recent_stock_price_gap_yahoo: 15000件を株価テーブルへ集約保存（うち新規日付 3700件）
[17:45:30]   Yahoo Finance gap-fill: 15000件 投入（うち新規日付 3700件・20260309 〜 20260908・4080社・基準セッション 2026-09-08）
[17:45:30]   価格ゼロ 416社（うち解決済み 0社）
[17:45:31]   Yahoo 並行度 4・HTTP失敗 429=0 5xx=0 4xx=2 その他=0
[17:46:00]   J-Quants catchup (2026-06-10〜2026-06-20): 18400件 upsert・スケール不一致で不採用 37行（2社）
[17:47:00]   株価鮮度: p50=2026-09-08 / p05=2026-09-08 / max=2026-09-08 / level=fresh（3823銘柄・5営業日超の遅れ 88銘柄）
[17:47:05]   往復段差: なし（調整差のある 2社を検査）
"""


class TestParse:
    def test_gap_fill_and_freshness(self):
        r = parse_nightly_log(LOG_BEFORE)
        assert (r["gap_target"], r["gap_universe"]) == (4078, 4440)
        assert (r["upserted"], r["new_rows"]) == (14888, 3665)
        assert r["gap_minutes"] == pytest.approx(44.1, abs=0.1)
        assert (r["priceless"], r["priceless_resolved"]) == (416, 0)
        assert r["fresh_p50"] == "2026-09-07" and r["fresh_level"] == "fresh"
        assert r["fresh_stale5d"] == 88

    def test_seconds_per_company(self):
        assert seconds_per_company(parse_nightly_log(LOG_BEFORE)) == pytest.approx(0.649, abs=1e-3)

    def test_concurrency_and_http(self):
        r = parse_nightly_log(LOG_AFTER)
        assert r["concurrency"] == 4
        assert (r["http_429"], r["http_5xx"], r["http_4xx"]) == (0, 0, 2)

    def test_scale_mismatch_and_roundtrip(self):
        r = parse_nightly_log(LOG_AFTER)
        assert (r["scale_mismatch_rows"], r["scale_mismatch_companies"]) == (37, 2)
        assert r["roundtrip"] == "なし" and r["roundtrip_companies"] == 0


class TestUnknownIsNotZero:
    """**取れなかった項目は 0 ではなく `None`。** 0 と混ぜると、書式が変わった日に
    「健全」へ静かに倒れる（`except: return []` と同型の失敗）。"""

    def test_missing_lines_are_none(self):
        r = parse_nightly_log(LOG_BEFORE)
        # #622 適用前なので並行フェッチの行が無い＝0件成功ではなく「不明」
        assert r["concurrency"] is None
        assert r["http_429"] is None and r["http_5xx"] is None
        # #620 適用前なので往復段差の検知そのものが無い
        assert r["roundtrip"] is None and r["roundtrip_companies"] is None

    def test_scale_mismatch_unknown_before_the_guard_shipped(self):
        """catchup 行はあるが `スケール不一致…` は 0 件のとき**行ごと出ない**。

        catchup 行の存在だけで 0 と決めると、#620 以前の晩（選別が存在しない）まで
        「0件で健全」に見える。往復段差の行——同じ PR で入った1組——を手掛かりにする。
        """
        assert parse_nightly_log(LOG_BEFORE)["catchup_upserted"] == 18470
        assert parse_nightly_log(LOG_BEFORE)["scale_mismatch_rows"] is None

    def test_scale_mismatch_zero_after_the_guard_shipped(self):
        log = LOG_AFTER.replace("・スケール不一致で不採用 37行（2社）", "")
        r = parse_nightly_log(log)
        assert r["roundtrip"] == "なし"            # #620 が入っている晩である証拠
        assert r["scale_mismatch_rows"] == 0        # 記載が無い＝本物の 0
        assert r["scale_mismatch_companies"] == 0

    def test_empty_log_yields_all_none(self):
        r = parse_nightly_log("")
        assert r["gap_target"] is None and r["new_rows"] is None
        assert r["fresh_level"] is None
        assert seconds_per_company(r) is None


class TestWarnings:
    def test_healthy_night_has_no_warning(self):
        assert warnings_for(parse_nightly_log(LOG_AFTER)) == []

    def test_missing_gap_fill_is_flagged(self):
        assert any("収集が終わっていない" in w for w in warnings_for(parse_nightly_log("")))

    def test_skipped_gap_fill_is_not_flagged(self):
        """週末・祝日明けの全社追いつき（#474）はスキップが正常。"""
        log = "[17:30:00]   Yahoo Finance gap-fill: スキップ（全社が最新セッションに追いついている・基準セッション 2026-09-06）\n"
        assert warnings_for(parse_nightly_log(log)) == []

    def test_rate_limited_is_flagged(self):
        r = parse_nightly_log(LOG_AFTER.replace("429=0", "429=17"))
        assert any("429" in w for w in warnings_for(r))

    def test_roundtrip_band_is_flagged(self):
        log = LOG_AFTER.replace(
            "往復段差: なし（調整差のある 2社を検査）",
            "**往復段差 2社**（例: E32779, E05716）＝調整差のある社の日次に")
        r = parse_nightly_log(log)
        assert r["roundtrip_companies"] == 2
        assert any("repair_scale_mixture" in w for w in warnings_for(r))

    def test_stale_freshness_is_flagged(self):
        r = parse_nightly_log(LOG_AFTER.replace("level=fresh", "level=stale"))
        assert any("stale" in w for w in warnings_for(r))

    def test_volume_change_is_not_a_warning(self):
        """社数や所要の増減は警告にしない——夜ごとに ±45% 振れた前例がある（#556）。"""
        r = parse_nightly_log(LOG_AFTER.replace("4080/4440", "120/4440"))
        assert warnings_for(r) == []


class TestTextWidth:
    def test_fullwidth_counts_as_two(self):
        assert display_width("abc") == 3
        assert display_width("日本語") == 6

    def test_ambiguous_is_caller_decided(self):
        """Ambiguous は端末依存。cp932 コンソール向け（watchdog）は 2 幅で数える。"""
        assert display_width("×") == 1
        assert display_width("×", ambiguous=2) == 2

    def test_pad_never_truncates(self):
        assert pad("日本語", 4) == "日本語"
        assert pad("ab", 5) == "ab   "
