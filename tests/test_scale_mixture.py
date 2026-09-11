"""1つの価格列に2つのスケールが混ざる事故を防ぐ仕組みのテスト（#620）。

この壊れ方は**エラーとして現れない**——どちらの値も妥当な株価で、upsert は成功し、
行数も鮮度も正常に見える。したがって「書かなかったこと」と「往復の帯を見つけること」を
テストで押さえるしかない。
"""
import asyncio
import os
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import database as D
import scripts.repair_scale_mixture as rsm
from collector_prices import (
    _pair_roundtrip_steps, _steps_from_closes, collect_stock_price_history_jquants,
    exclude_judged_bands, load_judged_scale_bands, record_scale_band_verdicts,
    scale_band_key,
)
from collector_utils import rounding_tolerance, same_price_scale
from scripts.repair_scale_mixture import (
    CONFIRMED, REJECTED, UNDETERMINED, confirm_official_scale, judge_official_scale,
)


class TestSamePriceScale:
    """丸め差と本物のスケール差を分ける（判定は収集側と修復側で共有する）。"""

    def test_rounding_difference_is_same_scale(self):
        assert same_price_scale(2861.0, 2861.4) is True

    def test_adjustment_difference_is_not(self):
        # #620 の実測: E32779 の 2026-05-29（Yahoo=C=2861.0・公式 AdjC=2384.2）
        assert same_price_scale(2861.0, 2384.2) is False

    def test_low_priced_stock_gets_a_wider_tolerance(self):
        # 丸め幅 1円は株価 21円では 4.8% にあたる（#466 の E01300）
        assert rounding_tolerance(21.0, 24.7) == pytest.approx(1.0 / 21.0)
        assert same_price_scale(21.0, 21.9) is True

    def test_non_positive_is_never_the_same_scale(self):
        assert same_price_scale(0.0, 100.0) is False
        assert same_price_scale(None, 100.0) is False


class TestRoundtripBands:
    """「飛んで数日で戻る」帯を、実際の値動きと分けて拾う。"""

    # #620 の実測（E32779 3482 ロードスターキャピタル）。帯の中は公式 AdjC が入っており、
    # 両端の段差は企業イベントでは説明できない（11営業日で戻る分割は存在しない）。
    _MIXED = [
        ("2026-05-28", 2845.0), ("2026-05-29", 2861.0),
        ("2026-06-01", 2306.7), ("2026-06-02", 2283.3), ("2026-06-03", 2285.0),
        ("2026-06-04", 2247.5), ("2026-06-05", 2284.2), ("2026-06-08", 2265.0),
        ("2026-06-09", 2296.7), ("2026-06-10", 2346.7), ("2026-06-11", 2355.0),
        ("2026-06-12", 2449.2), ("2026-06-15", 2587.5),
        ("2026-06-16", 2938.0), ("2026-06-17", 2960.0),
    ]

    def _bands(self, rows):
        return _pair_roundtrip_steps(_steps_from_closes(rows))

    def test_finds_the_mixed_band(self):
        bands = self._bands(self._MIXED)
        assert len(bands) == 1
        assert (bands[0]["start"], bands[0]["end"]) == ("2026-06-01", "2026-06-15")
        assert bands[0]["ratio_out"] < 1.0 < bands[0]["ratio_back"]

    def test_ignores_a_one_way_move(self):
        # #620 の E02293（6966 三井ハイテック）の形。上げて戻らない＝本物の値動き。
        assert self._bands([("2026-06-15", 808.0), ("2026-06-16", 1108.0),
                            ("2026-06-17", 1120.0), ("2026-06-18", 1130.0)]) == []

    def test_ignores_a_one_day_round_trip(self):
        # 1日下げて翌日戻す形は値動き。catchup が作る帯は窓ぶんの長さを必ず持つ。
        assert self._bands([("2026-06-15", 1000.0), ("2026-06-16", 800.0),
                            ("2026-06-17", 1000.0)]) == []

    def test_ignores_a_round_trip_that_is_too_far_apart(self):
        assert self._bands([("2026-05-01", 1000.0), ("2026-05-04", 800.0),
                            ("2026-06-02", 1000.0)]) == []

    def test_ignores_a_move_that_does_not_come_back(self):
        # 下げた 20% のうち 5% しか戻らない＝往復ではない（積が 1.0 から離れる）
        assert self._bands([("2026-06-01", 1000.0), ("2026-06-04", 800.0),
                            ("2026-06-12", 840.0)]) == []

    def test_rounding_noise_is_not_a_step(self):
        assert _steps_from_closes([("2026-06-01", 1000.0), ("2026-06-02", 1005.0)]) == []

    def test_picks_the_best_partner_not_the_first(self):
        """相手が複数成立するとき、最も戻り切っている方（積が 1.0 に近い方）と組む。"""
        rows = [("2026-06-01", 1000.0), ("2026-06-05", 800.0),
                ("2026-06-10", 880.0),    # ここで組むと積 0.88（先着ならこちら）
                ("2026-06-15", 1010.0)]   # ここで組むと積 0.918（こちらが最良）
        bands = self._bands(rows)
        assert len(bands) == 1
        assert (bands[0]["start"], bands[0]["end"]) == ("2026-06-05", "2026-06-10")


class TestCatchupSkipsMismatchedScale:
    """J-Quants catchup は AdjC≠C の行を書かない（本丸）。"""

    _MON = date(2024, 1, 8)

    def _row(self, *, c: float, adjc: float) -> dict:
        return {"Code": "10010", "Date": "2024-01-08",
                "O": c, "H": c, "L": c, "C": c, "Vo": 5000.0,
                "AdjO": adjc, "AdjH": adjc, "AdjL": adjc, "AdjC": adjc, "AdjVo": 6000.0,
                "AdjFactor": 1.0}

    def _run(self, db, row):
        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[row]):
            with patch("collector_prices.record_prices_batch", return_value=1) as batch:
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                            result = asyncio.run(collect_stock_price_history_jquants(
                                db, date_from=self._MON, date_to=self._MON))
        return result, batch

    def _add_company(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.commit()

    def test_writes_when_the_adjustment_matches(self, db, make_company):
        self._add_company(db, make_company)
        result, batch = self._run(db, self._row(c=1005.0, adjc=1005.0))
        batch.assert_called_once()
        assert result["scale_mismatch"] == 0
        assert result["scale_mismatch_companies"] == []

    def test_does_not_write_when_the_adjustment_differs(self, db, make_company):
        self._add_company(db, make_company)
        result, batch = self._run(db, self._row(c=2768.0, adjc=2306.7))
        batch.assert_not_called()
        assert result["scale_mismatch"] == 1
        assert result["scale_mismatch_companies"] == ["E00001"]

    def test_rounding_difference_still_gets_written(self, db, make_company):
        """1円の丸め差で公式値を捨てない（捨てると是正の機会を全社で失う）。"""
        self._add_company(db, make_company)
        result, batch = self._run(db, self._row(c=1005.0, adjc=1005.4))
        batch.assert_called_once()
        assert result["scale_mismatch"] == 0


class TestConfirmOfficialScale:
    """候補を確定させる突合（形だけでは本物を選べない・実測 234社中2社）。"""

    _BANDS = [{"start": "2026-06-01", "end": "2026-06-15"}]

    def test_confirms_when_db_equals_official_adjusted(self):
        rows = [{"Date": "2026-06-01", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(rows, {"2026-06-01": 2306.7}, self._BANDS)
        assert ok is True
        assert "1/1" in why

    def test_rejects_when_there_is_no_adjustment_difference(self):
        """E02293 型: 調整差が無い＝誰が書いても同じ値＝往復は本物の値動き。"""
        rows = [{"Date": "2026-06-01", "C": 808.0, "AdjC": 808.0}]
        ok, why = confirm_official_scale(rows, {"2026-06-01": 808.0}, self._BANDS)
        assert ok is False
        assert "AdjC≠C の日が無い" in why

    def test_rejects_when_db_does_not_hold_the_official_value(self):
        rows = [{"Date": "2026-06-01", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(rows, {"2026-06-01": 2768.0}, self._BANDS)
        assert ok is False
        assert "AdjC と一致しない" in why

    def test_ignores_days_outside_the_band(self):
        rows = [{"Date": "2026-05-20", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(rows, {"2026-05-20": 2306.7}, self._BANDS)
        assert ok is False

    def test_rejects_when_official_returned_nothing(self):
        ok, why = confirm_official_scale([], {}, self._BANDS)
        assert ok is False
        assert "1行も取得できなかった" in why


class TestYahooCrossCheck:
    """条件3（Yahoo 値が AdjC と食い違うこと）。**これを落とすと分割のあった
    高ボラ銘柄を必ず誤検知する**——実測 E01717（6834 日本工機）。"""

    _BANDS = [{"start": "2026-06-10", "end": "2026-06-18"}]

    def test_rejects_when_yahoo_also_matches_official(self):
        """E01717 型: 分割はあるが Yahoo もその分割を遡及調整済み。

        `AdjC != C` は「分割がある」としか言っておらず、Yahoo が正しく調整していれば
        Yahoo 値 = AdjC になる。すると DB が Yahoo 由来でも「DB 値 == AdjC」が
        成り立ってしまい、**実際の値動きの往復を混在と誤断する**。
        """
        rows = [{"Date": "2026-06-10", "C": 9972.0, "AdjC": 4986.0}]
        ok, why = confirm_official_scale(
            rows, {"2026-06-10": 4986.0}, self._BANDS, {"2026-06-10": 4986.0})
        assert ok is False
        assert "Yahoo と公式が同じスケール" in why

    def test_confirms_when_yahoo_disagrees_with_official(self):
        """E32779 型: Yahoo が無償割当を落としており、公式と恒常的にずれる（#466）。"""
        rows = [{"Date": "2026-06-10", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(
            rows, {"2026-06-10": 2306.7}, self._BANDS, {"2026-06-10": 2768.0})
        assert ok is True
        assert "Yahoo 値と食い違う" in why

    def test_rejects_when_yahoo_could_not_be_fetched(self):
        """**取れないことを「一致しない」と読むと誤検知になる。** 判定不能は棄却側へ。"""
        rows = [{"Date": "2026-06-10", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(
            rows, {"2026-06-10": 2306.7}, self._BANDS, {})
        assert ok is False
        assert "判定できない" in why

    def test_skipping_the_yahoo_check_is_visible_in_the_reason(self):
        """`None` は「突合を省いた」。確定はするが理由にそう書く（黙って緩めない）。"""
        rows = [{"Date": "2026-06-10", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(rows, {"2026-06-10": 2306.7}, self._BANDS, None)
        assert ok is True
        assert "Yahoo 突合は省略" in why


# ── #644: 判定を3値にし、非該当の帯を記録して夜間の警告から除く ─────────────────

# 2026-09-11 の夜間が警告した3社の帯（`repair_scale_mixture --only` の実出力から写した・
# 比は表示の4桁）。3社とも [棄却] だったのに毎晩警告されていた。
_REAL_0911 = {
    "E01332": [
        {"start": "2026-06-19", "end": "2026-06-22", "days": 4,
         "ratio_out": 1.1510, "ratio_back": 0.8448},
        {"start": "2026-07-31", "end": "2026-08-18", "days": 19,
         "ratio_out": 1.1323, "ratio_back": 0.8631},
    ],
    "E01717": [
        {"start": "2026-05-15", "end": "2026-05-21", "days": 7,
         "ratio_out": 0.8255, "ratio_back": 1.1413},
        {"start": "2026-06-10", "end": "2026-06-18", "days": 9,
         "ratio_out": 0.8692, "ratio_back": 1.1795},
    ],
    "E34165": [
        {"start": "2026-07-06", "end": "2026-07-17", "days": 15,
         "ratio_out": 1.1656, "ratio_back": 0.8346},
    ],
}
_TODAY = date(2026, 9, 11)


def _found(bands_by_ec: dict) -> dict:
    """`detect_roundtrip_scale_bands` の戻りと同じ形。"""
    return {"companies": [{"edinet_code": ec, "bands": [dict(b) for b in bs]}
                          for ec, bs in sorted(bands_by_ec.items())],
            "steps": 99}


def _rejected(bands_by_ec: dict) -> list:
    return [(ec, b, "テスト") for ec, bs in bands_by_ec.items() for b in bs]


class TestJudgeThreeWay:
    """非該当（記録してよい）と判定不能（記録してはいけない）を分ける。"""

    _BAND = [{"start": "2026-06-10", "end": "2026-06-18"}]

    def test_each_branch_maps_to_its_status(self):
        adj = [{"Date": "2026-06-10", "C": 9972.0, "AdjC": 4986.0}]
        flat = [{"Date": "2026-06-10", "C": 808.0, "AdjC": 808.0}]
        cases = [
            ((adj, {"2026-06-10": 4986.0}, {"2026-06-10": 9972.0}), CONFIRMED),
            ((adj, {"2026-06-10": 4986.0}, None), CONFIRMED),      # Yahoo 突合を省いた
            ((flat, {"2026-06-10": 808.0}, {}), REJECTED),          # 調整差なし
            ((adj, {"2026-06-10": 9972.0}, {}), REJECTED),          # DB が公式値ではない
            ((adj, {"2026-06-10": 4986.0}, {"2026-06-10": 4986.0}), REJECTED),  # E01717 型
            ((adj, {"2026-06-10": 4986.0}, {}), UNDETERMINED),      # Yahoo が取れない
            (([], {}, {}), UNDETERMINED),                            # 公式値が取れない
        ]
        for (rows, closes, yahoo), want in cases:
            status, _ = judge_official_scale(rows, closes, self._BAND, yahoo)
            assert status == want, (rows, closes, yahoo)

    def test_band_newer_than_the_coverage_is_rejected(self):
        """E34165 型: 帯が契約窓より新しい＝catchup が一度も書いていない＝混ざりようがない。

        理由は「AdjC≠C の日が無い」ではなく窓の外であることを書く（公式値を見ていないのに
        調整差が無いと書くと、読み手が根拠を取り違える）。
        """
        rows = [{"Date": "2026-06-19", "C": 1000.0, "AdjC": 500.0}]
        status, why = judge_official_scale(
            rows, {}, _REAL_0911["E34165"], {}, cover_to="2026-06-19")
        assert status == REJECTED
        assert "契約窓" in why and "AdjC≠C" not in why

    def test_missing_official_rows_inside_the_coverage_is_undetermined(self):
        rows = [{"Date": "2026-06-01", "C": 1000.0, "AdjC": 500.0}]
        status, why = judge_official_scale(rows, {}, self._BAND, {}, cover_to="2026-06-30")
        assert status == UNDETERMINED
        assert "欠落" in why

    def test_missing_official_rows_without_coverage_is_undetermined(self):
        rows = [{"Date": "2026-06-01", "C": 1000.0, "AdjC": 500.0}]
        assert judge_official_scale(rows, {}, self._BAND, {})[0] == UNDETERMINED

    def test_wrapper_only_confirms_confirmed(self):
        rows = [{"Date": "2026-06-10", "C": 9972.0, "AdjC": 4986.0}]
        ok, _ = confirm_official_scale(rows, {"2026-06-10": 4986.0}, self._BAND, {})
        assert ok is False           # 判定不能は確定させない


class TestJudgedBandRecord:
    """判定済みの帯を記録し、夜間の検知から**帯単位で**除く。"""

    def test_nothing_recorded_means_nothing_excluded(self, db):
        assert load_judged_scale_bands(db) == set()
        found, n = exclude_judged_bands(_found(_REAL_0911), set())
        assert n == 0 and len(found["companies"]) == 3

    def test_the_0911_bands_are_silenced(self, db):
        """完了条件1: 9/11 の3社・5本の帯は、記録後は夜間の警告に出ない。"""
        rep = record_scale_band_verdicts(db, _rejected(_REAL_0911), today=_TODAY)
        assert rep == {"recorded": 5, "cleared": 0, "pruned": 0, "total": 5}
        found, n = exclude_judged_bands(_found(_REAL_0911), load_judged_scale_bands(db))
        assert found["companies"] == [] and n == 5

    def test_a_new_band_of_a_judged_company_is_still_reported(self, db):
        """完了条件2: 社単位で黙らせない。同じ社でも新しい帯は警告される。"""
        record_scale_band_verdicts(db, _rejected(_REAL_0911), today=_TODAY)
        new = {"start": "2026-08-20", "end": "2026-08-28", "days": 8,
               "ratio_out": 0.85, "ratio_back": 1.17}
        tonight = dict(_REAL_0911, E01717=_REAL_0911["E01717"] + [new])
        found, n = exclude_judged_bands(_found(tonight), load_judged_scale_bands(db))
        assert n == 5
        assert [c["edinet_code"] for c in found["companies"]] == ["E01717"]
        assert found["companies"][0]["bands"] == [new]

    def test_rewritten_values_on_the_same_dates_are_reported_again(self, db):
        """帯の端の値が書き換われば（再取得・分割修復）判定をやり直させる。"""
        record_scale_band_verdicts(db, _rejected(_REAL_0911), today=_TODAY)
        moved = [dict(_REAL_0911["E34165"][0], ratio_out=1.2500)]
        found, n = exclude_judged_bands(_found({"E34165": moved}), load_judged_scale_bands(db))
        assert n == 0 and len(found["companies"]) == 1

    def test_key_is_stable_under_display_rounding(self):
        b = dict(_REAL_0911["E34165"][0], ratio_out=1.16561234)
        assert scale_band_key("E34165", b) == scale_band_key("E34165", _REAL_0911["E34165"][0])

    def test_confirmed_band_is_cleared(self, db):
        record_scale_band_verdicts(db, _rejected(_REAL_0911), today=_TODAY)
        band = _REAL_0911["E34165"][0]
        rep = record_scale_band_verdicts(db, [], [("E34165", band)], today=_TODAY)
        assert rep["cleared"] == 1 and rep["total"] == 4
        assert scale_band_key("E34165", band) not in load_judged_scale_bands(db)

    def test_bands_outside_the_retention_window_are_pruned(self, db):
        record_scale_band_verdicts(db, _rejected(_REAL_0911), today=_TODAY)
        rep = record_scale_band_verdicts(db, [], today=date(2027, 6, 1))
        assert rep["pruned"] == 5 and rep["total"] == 0

    def test_corrupt_record_raises_instead_of_reading_as_empty(self, db):
        """空扱いにすると「読めない」が「判定済みが無い」に化ける。夜間は既存の except が
        「往復段差の検知に失敗」行にし、check_nightly_collect が警告する。"""
        D.upsert_setting(db, D.KEY_SCALE_BAND_VERDICTS, "{not json")
        with pytest.raises(ValueError):
            load_judged_scale_bands(db)
        D.upsert_setting(db, D.KEY_SCALE_BAND_VERDICTS, '{"version": 1}')
        with pytest.raises(ValueError):
            load_judged_scale_bands(db)


class TestVerifyTargetsPerBand:
    """`verify_targets` の帯ごとの判定と、Yahoo の取得失敗の扱い。"""

    def _verify(self, monkeypatch, *, rows, closes, yahoo, bands):
        monkeypatch.setenv("JQUANTS_API_KEY", "test-key")
        monkeypatch.setattr(rsm, "YAHOO_STOCK_RATE_SLEEP", 0)
        monkeypatch.setattr(rsm, "_learn_jquants_coverage",
                            AsyncMock(return_value=("2024-06-19", "2026-06-19")))
        monkeypatch.setattr(rsm, "_jquants_fetch_code", AsyncMock(return_value=rows))
        monkeypatch.setattr(rsm, "fetch_yahoo_closes", AsyncMock(return_value=yahoo))
        monkeypatch.setattr(rsm, "load_tickers", lambda db, ecs: {"E01332": ("5801", None)})
        monkeypatch.setattr(rsm, "load_daily_closes", lambda db, ec: closes)
        return asyncio.run(rsm.verify_targets(None, {"E01332": bands}))["E01332"]

    def test_yahoo_fetch_failure_is_undetermined_not_confirmed(self, monkeypatch):
        """取得失敗（None）を「突合を省いた」と読むと確定になり、非該当の記録まで消す。"""
        ok, why, per_band = self._verify(
            monkeypatch, bands=[_REAL_0911["E01332"][0]], yahoo=None,
            rows=[{"Date": "2026-06-19", "C": 5000.0, "AdjC": 2500.0}],
            closes={"2026-06-19": 2500.0})
        assert ok is False and "判定できない" in why
        assert [st for _, st, _ in per_band] == [UNDETERMINED]

    def test_each_band_gets_its_own_verdict(self, monkeypatch):
        """E01332 型: 1本目は Yahoo も公式と同じスケール、2本目は契約窓より新しい。"""
        ok, _, per_band = self._verify(
            monkeypatch, bands=_REAL_0911["E01332"],
            rows=[{"Date": "2026-06-19", "C": 5000.0, "AdjC": 2500.0}],
            closes={"2026-06-19": 2500.0}, yahoo={"2026-06-19": 2500.0})
        assert ok is False
        assert [st for _, st, _ in per_band] == [REJECTED, REJECTED]
        assert "契約窓" in per_band[1][2]
