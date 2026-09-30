"""`scripts/check_nightly_collect.py` のパースと判定（#556・#620）。

ログ文字列は**実ログから写した**もの（`.logs/nightly_2026090*.log`）。`_pipeline_incremental.py`
の書式を変えたらここが落ちる——それが狙いで、書式変更は「読めなくなった」ことを静かな
劣化ではなくテストの失敗として現す。
"""
import pytest

from collector_prices import (
    format_yahoo_http_stats, roundtrip_log_line, scale_rejection_log_line,
    scale_step_log_line,
)
from scripts._textwidth import display_width, pad
from scripts import run_nightly
from scripts.check_nightly_collect import (
    WARNING_KINDS, latest_night, parse_nightly_log, seconds_per_company, warning_items,
    warnings_for,
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

# #622／#620 の適用後（**2026-09-08 の実ログから写した**）。
# 初版は #622 の適用前に「出るはずの形」を推測で書いており、実際とは3点ずれていた——
# 並行度・HTTP失敗は独立行ではなく集約保存の行に**併記**され、そのぶん `新規日付 N件` の
# 後ろに閉じ括弧が来ない。**推測で書いた検体は本物のログを読めないことを検出できない。**
LOG_AFTER = """\n2026-09-08 17:36:10,507 INFO fill_recent_stock_price_gap_yahoo: 4105/4441社を補完（基準セッション 2026-09-08 / JST 2026-09-08 17:36 ・最古起点 20260309 〜 20260908 ・価格ゼロ 417社（うち解決済み 0社） ・廃止済み価格ゼロ 332社は今夜は見送り（7日に1回試す・#475） ・地方取引所に実在 4社は .T を叩かない（月次が正しい取引所で再プローブ・#560））
[17:37:32] [Yahoo gap-fill 500/4105]
2026-09-08 17:47:20,603 INFO fill_recent_stock_price_gap_yahoo: 7621件を株価テーブルへ集約保存（うち新規日付 3720件・並行度 4・HTTP失敗 429=0 5xx=0 4xx=350 その他=0）
[17:47:20]   Yahoo Finance gap-fill: 7621件 投入（うち新規日付 3720件・20260309 〜 20260908・4105社・基準セッション 2026-09-08）
[17:47:20]   価格ゼロ 417社（うち解決済み 0社）
[17:47:20]   Yahoo 並行度 4・HTTP失敗 429=0 5xx=0 4xx=350 その他=0
[17:49:02]   J-Quants catchup (2026-06-10〜2026-06-20): 18265件 upsert・契約窓外 3日・スケール不一致で不採用 218行（44社）
[17:49:05]   株価鮮度: p50=2026-09-08 / p05=2026-09-08 / max=2026-09-08 / level=fresh（3823銘柄・5営業日超の遅れ 88銘柄）
[17:49:05]   往復段差: なし（調整差のある 44社を検査）
"""

# 2026-09-11 の実ログ（`.logs/nightly_20260911.log`）の往復段差の行。判定済みの非該当が
# 毎晩ここに出続けていた（#644）。
LINE_RT_HIT_0911 = "[17:50:19]   **往復段差 3社**（例: E01332, E01717, E34165）＝調整差のある社の日次に「飛んで戻る」帯がある。1つの列に2つのスケールが混ざった疑い。`python -m scripts.repair_scale_mixture` で確認する（#620）"


def _night_with(line: str) -> str:
    """LOG_AFTER の往復段差の行を差し替える。行は `_pipeline_incremental.py` と同じく
    `roundtrip_log_line` の**実出力**を渡す——推測で写した検体は、書式が変わったときに
    読めなくなったことを検出できない。"""
    return LOG_AFTER.replace("往復段差: なし（調整差のある 44社を検査）", line)


_BAND = {"start": "2026-08-20", "end": "2026-08-28", "days": 8,
         "ratio_out": 0.85, "ratio_back": 1.17}


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

    def test_gap_fill_parsed_after_concurrency_shipped(self):
        """#622 で `新規日付 N件` の後ろに「・並行度 …」が続くようになった。

        閉じ括弧まで要求していた旧パターンはここを黙って読み落とし、投入行数・所要が
        丸ごと `-` になったうえ「収集が終わっていない可能性」という偽の警告を出していた。
        """
        r = parse_nightly_log(LOG_AFTER)
        assert (r["upserted"], r["new_rows"]) == (7621, 3720)
        assert r["gap_minutes"] == pytest.approx(11.2, abs=0.1)

    def test_concurrency_and_http(self):
        r = parse_nightly_log(LOG_AFTER)
        assert r["concurrency"] == 4
        assert (r["http_429"], r["http_5xx"], r["http_4xx"]) == (0, 0, 350)

    def test_scale_mismatch_and_roundtrip(self):
        r = parse_nightly_log(LOG_AFTER)
        assert (r["scale_mismatch_rows"], r["scale_mismatch_companies"]) == (218, 44)
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
        log = LOG_AFTER.replace("・スケール不一致で不採用 218行（44社）", "")
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
            "往復段差: なし（調整差のある 44社を検査）",
            "**往復段差 2社**（例: E01332, E01717）＝調整差のある社の日次に")
        r = parse_nightly_log(log)
        assert r["roundtrip_companies"] == 2
        assert any("repair_scale_mixture" in w for w in warnings_for(r))

    def test_roundtrip_hit_line_from_a_real_night(self):
        """2026-09-11 の実ログの検出行（#644 の前の書式）。除外数は書式に無い＝不明。"""
        log = LOG_AFTER.replace(
            "[17:49:05]   往復段差: なし（調整差のある 44社を検査）", LINE_RT_HIT_0911)
        r = parse_nightly_log(log)
        assert r["roundtrip"] == "検出" and r["roundtrip_companies"] == 3
        assert r["roundtrip_excluded"] is None

    def test_stale_freshness_is_flagged(self):
        r = parse_nightly_log(LOG_AFTER.replace("level=fresh", "level=stale"))
        assert any("stale" in w for w in warnings_for(r))

    def test_volume_change_is_not_a_warning(self):
        """社数や所要の増減は警告にしない——夜ごとに ±45% 振れた前例がある（#556）。"""
        r = parse_nightly_log(LOG_AFTER.replace("4105/4441", "120/4441"))
        assert warnings_for(r) == []


class TestJudgedBandsExcluded:
    """#644: 夜間は判定済みの非該当の帯を除いて数え、除いた数を行に残す。"""

    def test_none_line_with_exclusions_is_read(self):
        line = roundtrip_log_line(45, {"companies": [], "steps": 9}, 5)
        r = parse_nightly_log(_night_with(line))
        assert r["roundtrip"] == "なし" and r["roundtrip_companies"] == 0
        assert r["roundtrip_excluded"] == 5
        assert warnings_for(r) == []      # 判定済みしか無い晩は警告しない（完了条件1）

    def test_hit_line_with_exclusions_is_read_and_flagged(self):
        found = {"companies": [{"edinet_code": "E01717", "bands": [_BAND]}], "steps": 9}
        r = parse_nightly_log(_night_with(roundtrip_log_line(45, found, 5)))
        assert r["roundtrip"] == "検出" and r["roundtrip_companies"] == 1
        assert r["roundtrip_excluded"] == 5
        assert any("repair_scale_mixture" in w for w in warnings_for(r))   # 完了条件2

    def test_zero_exclusions_is_zero_not_unknown(self):
        """0 のときも行に出す＝記載の無い晩（旧書式）だけが `None` になる。"""
        r = parse_nightly_log(_night_with(
            roundtrip_log_line(45, {"companies": [], "steps": 9}, 0)))
        assert r["roundtrip_excluded"] == 0

    def test_old_format_is_unknown(self):
        assert parse_nightly_log(LOG_AFTER)["roundtrip_excluded"] is None
        assert parse_nightly_log(LOG_BEFORE)["roundtrip_excluded"] is None

    def test_scale_mismatch_zero_inference_still_works_on_the_new_format(self):
        """スケール不一致の「0 と不明」の区別は往復段差の行を手掛かりにしている。"""
        log = _night_with(roundtrip_log_line(45, {"companies": [], "steps": 9}, 5)) \
            .replace("・スケール不一致で不採用 218行（44社）", "")
        assert parse_nightly_log(log)["scale_mismatch_rows"] == 0


class TestHttp404Breakdown:
    """#556: 4xx の内訳として 404 を読み、404 以外の 4xx だけを警告する。

    404 は上場廃止社が毎晩一定数返す値（実測 約320件）で、一緒くたに数えると 401/403 の
    ような拒否が埋もれる。行は `_pipeline_incremental.py` と同じく `format_yahoo_http_stats`
    の**実出力**で組み立てる——推測で写した検体は、書式が変わったときに読めなくなったことを
    検出できない。
    """

    OLD_LINE = "[17:47:20]   Yahoo 並行度 4・HTTP失敗 429=0 5xx=0 4xx=350 その他=0"

    def _night(self, stats: dict) -> str:
        line = f"[17:47:20]   Yahoo 並行度 4・{format_yahoo_http_stats(stats)}"
        assert self.OLD_LINE in LOG_AFTER      # 差し替えが空振りしていないこと
        return LOG_AFTER.replace(self.OLD_LINE, line)

    def test_breakdown_is_read(self):
        r = parse_nightly_log(self._night(
            {"429": 0, "5xx": 0, "4xx": 320, "404": 320, "other": 0}))
        assert (r["http_4xx"], r["http_404"], r["http_other"]) == (320, 320, 0)
        assert r["concurrency"] == 4

    def test_404_only_is_not_flagged(self):
        """上場廃止社の 404 だけの晩は健全。"""
        r = parse_nightly_log(self._night(
            {"429": 0, "5xx": 0, "4xx": 320, "404": 320, "other": 0}))
        assert warnings_for(r) == []

    def test_non_404_4xx_is_flagged(self):
        r = parse_nightly_log(self._night(
            {"429": 0, "5xx": 0, "4xx": 325, "404": 320, "other": 0}))
        w = [x for x in warnings_for(r) if "404以外" in x]
        assert len(w) == 1 and "5件" in w[0]

    def test_old_format_breakdown_is_unknown_and_not_flagged(self):
        """内訳の無い晩（9/8〜9/11）は「不明」。0 と読んで 350件すべてを拒否扱いにしない。"""
        r = parse_nightly_log(LOG_AFTER)
        assert r["http_4xx"] == 350 and r["http_404"] is None
        assert warnings_for(r) == []

    def test_table_marks_unknown_breakdown(self):
        from scripts.check_nightly_collect import ROWS

        get = dict(ROWS)["HTTP 4xx（404）/ その他"]
        assert get(parse_nightly_log(LOG_AFTER)) == "350（-） / 0"
        assert get(parse_nightly_log(self._night(
            {"429": 0, "5xx": 0, "4xx": 320, "404": 318, "other": 0}))) == "320（318） / 0"


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


class TestScaleGuardRules:
    """#765 の2行を読む。検体は `_pipeline_incremental.py` と同じく**書式関数の実出力**。

    Yahoo が株式併合を split として持った上場廃止社は比率倍の値を返し続けるので、同じ
    基準日のまま弾き続けている社（既知）だけの晩は警告しない。株価表に残った段差は
    直すまで毎晩警告する。
    """

    EX = {"edinet_code": "E35289", "from_date": "2026-09-25", "from_close": 1406.0,
          "to_date": "2026-09-28", "to_close": 1688467584.0}

    def _night(self, *, rejected=0, new=0, steps=None, scan_failed=False):
        res = {"scale_rejected": rejected, "scale_rejected_new": new,
               "scale_dropped_bars": rejected, "scale_rejected_examples": [self.EX] if rejected else []}
        lines = [f"[17:47:20]   {scale_rejection_log_line(res)}"]
        if scan_failed:
            lines.append("[17:49:05]   株価スケール段差の走査に失敗（継続します）: RuntimeError: boom")
        elif steps is not None:
            lines.append("[17:49:05]   " + scale_step_log_line(steps))
        # collector 自身の WARNING 行（文言が違う＝点検の正規表現には掛からない）
        lines.append("2026-09-30 18:18:08,899 WARNING fill_recent_stock_price_gap_yahoo: DB の直前値と"
                     "100倍以上離れた Yahoo の値を書かなかった 9社・9本（新規 9社）（例: …）")
        return LOG_AFTER + "\n".join(lines) + "\n"

    @staticmethod
    def _steps(n):
        hits = [{"table": "daily", "edinet_code": f"E{i:05d}", "from_date": "2026-09-08",
                 "from_close": 3700.0, "to_date": "2026-09-09", "to_close": 16278046720.0}
                for i in range(n)]
        return {"companies": sorted({h["edinet_code"] for h in hits}),
                "daily_steps": n, "weekly_steps": 0, "hits": hits}

    def test_lines_are_read(self):
        r = parse_nightly_log(self._night(rejected=4, new=1, steps=self._steps(2)))
        assert (r["scale_rejected"], r["scale_rejected_new"], r["scale_dropped_bars"]) == (4, 1, 4)
        assert (r["scale_steps"], r["scale_steps_daily"], r["scale_steps_weekly"]) == (2, 2, 0)
        assert r["scale_steps_failed"] is False

    def test_new_rejection_and_remaining_steps_are_flagged(self):
        w = warnings_for(parse_nightly_log(self._night(rejected=4, new=1, steps=self._steps(2))))
        assert any("新たに書かなかった社 1" in x for x in w)
        assert any("100倍以上の段差が残っている 2社" in x for x in w)

    def test_known_only_night_is_quiet(self):
        w = warnings_for(parse_nightly_log(self._night(rejected=4, new=0, steps=self._steps(0))))
        assert w == []

    def test_scan_failure_is_flagged(self):
        r = parse_nightly_log(self._night(scan_failed=True))
        assert r["scale_steps_failed"] is True and r["scale_steps"] is None
        assert any("走査が例外で落ちた" in x for x in warnings_for(r))

    def test_old_format_is_unknown_not_zero(self):
        r = parse_nightly_log(LOG_AFTER)
        assert r["scale_rejected"] is None and r["scale_rejected_new"] is None
        assert r["scale_steps"] is None and r["scale_steps_failed"] is None


# 2026-09-29 の実ログ（`.logs/nightly_20260929.log`）から写した。往復段差 4社・解決済みなのに空
# 1社が出ていたが `END pipeline: exit=0` で何も起票されなかった晩（#767 の発端）。
LOG_0929 = """\
[2026-09-29T08:48:43+00:00] 夜間バッチ開始（正本=ローカル・#503）
2026-09-29 18:07:30,954 INFO fill_recent_stock_price_gap_yahoo: 3878/4446社を補完（基準セッション 2026-09-29 / JST 2026-09-29 18:07 ・最古起点 20260330 〜 20260929 ・価格ゼロ 418社（うち解決済み 0社））
2026-09-29 18:18:08,899 INFO fill_recent_stock_price_gap_yahoo: 7530件を株価テーブルへ集約保存（うち新規日付 3714件・並行度 4・HTTP失敗 429=0 5xx=0 4xx=128（うち404=128） その他=0）
[18:18:08]   価格ゼロ 418社（うち解決済み 0社）・**解決済みなのに空 1社**
[18:18:08]   Yahoo 並行度 4・HTTP失敗 429=0 5xx=0 4xx=128（うち404=128） その他=0
[18:19:51]   J-Quants catchup (2026-07-01〜2026-07-11): 18106件 upsert・契約窓外 3日・スケール不一致で不採用 339行（68社）
[18:20:38]   株価鮮度: p50=2026-09-29 / p05=2026-09-29 / max=2026-09-29 / level=fresh（3804銘柄・5営業日超の遅れ 68銘柄）
[18:20:38]   **往復段差 4社**（例: E00024, E04980, E35948, E40060）＝調整差のある社の日次に「飛んで戻る」帯がある。1つの列に2つのスケールが混ざった疑い。`python -m scripts.repair_scale_mixture` で確認する（#620）・判定済みの非該当 3帯を除外
[2026-09-29T09:25:28+00:00] 夜間バッチ終了
"""


class TestWarningKinds:
    """#767: watchdog は警告を種類ごとの Issue にする。種類の表がここの唯一の源。"""

    def test_every_warning_has_a_registered_kind(self):
        """検体を全部並べて、出た種類キーが全部 `WARNING_KINDS` にあること。"""
        guard = TestScaleGuardRules()
        logs = (
            "", LOG_0929,
            LOG_AFTER.replace("429=0", "429=3").replace("level=fresh", "level=stale"),
            _night_with("[17:49:05]   往復段差の検知に失敗（継続します）: RuntimeError: x"),
            guard._night(rejected=4, new=1, steps=guard._steps(2)),
        )
        kinds = {k for t in logs for k, _ in warning_items(parse_nightly_log(t))}
        assert kinds <= set(WARNING_KINDS)
        assert {"collect_incomplete", "yahoo_http", "exchange_rejected", "roundtrip",
                "price_freshness", "scale_rejected", "scale_steps"} == kinds

    def test_warnings_for_is_the_text_of_warning_items(self):
        r = parse_nightly_log(LOG_0929)
        assert warnings_for(r) == [m for _, m in warning_items(r)]

    def test_the_0929_night_warns_on_two_kinds(self):
        assert [k for k, _ in warning_items(parse_nightly_log(LOG_0929))] \
            == ["exchange_rejected", "roundtrip"]

    def test_labels_have_no_digits(self):
        """ラベルは Issue タイトルになる。数字が入ると一致で既存の起票を探せない。"""
        for kind in WARNING_KINDS.values():
            assert not any(ch.isdigit() for ch in kind.label), kind.label

    def test_unknown_is_not_known(self):
        """行が無い晩は ok と言えない（閉じる根拠にしない）。"""
        r = parse_nightly_log(LOG_BEFORE)
        assert not WARNING_KINDS["roundtrip"].known(r)
        assert WARNING_KINDS["roundtrip"].known(parse_nightly_log(LOG_0929))


class TestCompleted:
    """書きかけのログを判定しない（20:00 に夜間がまだ走っていることがある・#767）。"""

    def test_a_finished_night_is_completed(self):
        assert parse_nightly_log(LOG_0929)["completed"] is True

    def test_a_night_in_flight_is_not_completed(self):
        head = LOG_0929.split("[2026-09-29T09:25:28+00:00]")[0]
        assert parse_nightly_log(head)["completed"] is False

    def test_a_second_run_in_the_same_file_resets_it(self):
        """手動の追いつき実行で同じ日のログに2回目が追記され、まだ走っている。"""
        log = LOG_0929 + "[2026-09-29T12:00:00+00:00] 夜間バッチ開始（正本=ローカル・#503）\n"
        assert parse_nightly_log(log)["completed"] is False

    def test_the_lines_match_what_the_runner_writes(self):
        """文言の源は `batch_common.run_batch` の `{spec.name}開始` / `{spec.name}終了`。"""
        name = run_nightly.SPEC.name
        log = f"[x] {name}開始（正本=ローカル・#503）\n[y] {name}終了\n"
        assert parse_nightly_log(log)["completed"] is True

    def test_latest_night_reads_only_the_newest_file(self, tmp_path):
        (tmp_path / "nightly_20260928.log").write_text("", encoding="utf-8")
        (tmp_path / "nightly_20260929.log").write_text(LOG_0929, encoding="utf-8")
        night = latest_night(tmp_path)
        assert night["log"] == "nightly_20260929.log" and night["completed"] is True

    def test_no_log_is_none(self, tmp_path):
        assert latest_night(tmp_path) is None
        assert latest_night(tmp_path / "absent") is None
