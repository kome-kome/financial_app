"""database.py のユニットテスト（in-memory SQLite）。

対象: pack/unpack・upsert_company・upsert_financial（辞書フラット化）・
calc_zscore_normalization（年度別Zスコア）。
calc_growth_rates は PostgreSQL 専用 SQL（LAG OVER・::numeric）のため SQLite では検証不可（対象外）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (
    Company,
    FinancialRecord,
    RegressionResult,
    pack_elements,
    unpack_elements,
    upsert_company,
    upsert_financial,
    upsert_regression_result,
)


class TestPackUnpack:
    def test_roundtrip(self):
        rows = [
            {"element": "NetSales", "context": "CurrentYear", "value": "1000"},
            {"element": "Assets", "context": "CurrentYear", "value": "5000"},
        ]
        assert unpack_elements(pack_elements(rows)) == rows

    def test_pack_returns_bytes(self):
        assert isinstance(pack_elements([{"a": 1}]), (bytes, bytearray))


class TestUpsertCompany:
    def test_insert_new(self, db):
        upsert_company(db, {"edinet_code": "E00001", "name": "テストA", "industry": "化学"})
        db.commit()
        obj = db.query(Company).filter_by(edinet_code="E00001").one()
        assert obj.name == "テストA"
        assert obj.industry == "化学"
        assert obj.updated_at is not None

    def test_empty_value_does_not_overwrite(self, db):
        upsert_company(db, {"edinet_code": "E00001", "name": "元名", "industry": "化学"})
        db.commit()
        # 空文字は実値を潰さない／実値は更新する
        upsert_company(db, {"edinet_code": "E00001", "name": "", "industry": "医薬品"})
        db.commit()
        obj = db.query(Company).filter_by(edinet_code="E00001").one()
        assert obj.name == "元名"
        assert obj.industry == "医薬品"

    def test_no_duplicate_on_upsert(self, db):
        upsert_company(db, {"edinet_code": "E00001", "name": "A"})
        db.commit()
        upsert_company(db, {"edinet_code": "E00001", "name": "B"})
        db.commit()
        assert db.query(Company).count() == 1
        assert db.query(Company).one().name == "B"


class TestUpsertFinancial:
    def _data(self, **over):
        d = {
            "edinet_code": "E00001", "year": 2023, "period_end": "2023-03-31",
            "bs": {"total_assets": 5000.0, "bps": 100.0},
            "pl": {"revenue": 1000.0, "eps": 50.0},
            "cf": {"operating_cf": 150.0},
            # derived（計算結果）は financial_records には保存しない（VIEW で都度算出）
            "derived": {"roe": 8.0},
            "val": {"per": 15.0, "dps": 10.0},
        }
        d.update(over)
        return d

    def test_flattens_sections(self, db):
        obj = upsert_financial(db, self._data())
        db.commit()
        assert obj.bs_total_assets == 5000.0   # bs_ プレフィックス
        assert obj.bs_bps == 100.0
        assert obj.pl_revenue == 1000.0         # pl_ プレフィックス
        assert obj.pl_eps == 50.0
        assert obj.cf_operating_cf == 150.0     # cf_ プレフィックス
        assert obj.per == 15.0                   # val もそのまま
        assert obj.dps == 10.0

    def test_c2_fields_and_nonfin_section(self, db):
        # 網羅性追加（C2）: bs/pl 新列＋ nonfin セクション（プレフィックス無しの直接列）
        obj = upsert_financial(db, self._data(
            bs={"ppe_total": 31778.0, "investments_other_assets": 12066.0},
            pl={"rd_expenses": 3241.0, "depreciation": 2757.0,
                "extraordinary_income": 445.0, "extraordinary_loss": 41.0},
            nonfin={"employees": 19967.0, "issued_shares": 15794987460.0},
        ))
        db.commit()
        assert obj.bs_ppe_total == 31778.0
        assert obj.bs_investments_other_assets == 12066.0
        assert obj.pl_rd_expenses == 3241.0
        assert obj.pl_depreciation == 2757.0
        assert obj.pl_extraordinary_income == 445.0
        assert obj.pl_extraordinary_loss == 41.0
        assert obj.employees == 19967.0             # nonfin → 直接列
        assert obj.issued_shares == 15794987460.0   # 数十億株も float で保持

    def test_derived_not_persisted(self, db):
        # 計算結果（derived）は financial_records に永続化されない（financial_metrics VIEW が担う）。
        # 計算列は ORM から削除済みのため、そもそも属性として存在しない。
        obj = upsert_financial(db, self._data(derived={"roe": 8.0, "op_margin": 12.3}))
        db.commit()
        assert obj.bs_total_assets == 5000.0          # ソースは保存される
        assert not hasattr(FinancialRecord, "roe")
        assert not hasattr(FinancialRecord, "op_margin")
        assert not hasattr(FinancialRecord, "gap_ratio")

    def test_raw_xbrl_json_populated(self, db):
        obj = upsert_financial(db, self._data())
        db.commit()
        assert obj.raw_xbrl_json["bs"]["total_assets"] == 5000.0
        assert set(obj.raw_xbrl_json) == {"bs", "pl", "cf"}

    def test_update_existing_no_duplicate(self, db):
        upsert_financial(db, self._data())
        db.commit()
        upsert_financial(db, self._data(pl={"revenue": 2000.0}))
        db.commit()
        rows = db.query(FinancialRecord).filter_by(edinet_code="E00001", year=2023).all()
        assert len(rows) == 1
        assert rows[0].pl_revenue == 2000.0


class TestUpsertRegressionResult:
    def _args(self, **over):
        d = dict(edinet_code="E00001", year=2023, period_end="2023-03-31",
                 predicted_market_cap=12000.0, gap_ratio=-15.0,
                 model="ols", sector="情報・通信業")
        d.update(over)
        return d

    def test_insert_new(self, db):
        upsert_regression_result(db, **self._args())
        db.commit()
        rr = db.query(RegressionResult).one()
        assert rr.predicted_market_cap == 12000.0
        assert rr.gap_ratio == -15.0
        assert rr.model == "ols"

    def test_upsert_updates_in_place(self, db):
        # 同一キー (edinet_code, year, period_end) は merge で上書きされ重複しない
        upsert_regression_result(db, **self._args(gap_ratio=-15.0))
        db.commit()
        upsert_regression_result(db, **self._args(gap_ratio=30.0, model="ridge"))
        db.commit()
        rows = db.query(RegressionResult).all()
        assert len(rows) == 1
        assert rows[0].gap_ratio == 30.0
        assert rows[0].model == "ridge"

    def test_empty_period_end_normalized(self, db):
        # period_end が None の場合は NULL として保存される（DATE 型移行後）
        upsert_regression_result(db, **self._args(period_end=None))
        db.commit()
        rr = db.query(RegressionResult).one()
        assert rr.period_end is None


# Zスコア正規化は financial_metrics VIEW（PostgreSQL window function）へ移行した。
# 旧 calc_zscore_normalization は廃止済み。VIEW の算出値が旧実装と一致することは
# Postgres 上で別途検証している（年度内・標本SD・sd=0→1.0・n>=2・丸め桁の一致）。
# SQLite には STDDEV/WINDOW が無いため本ファイルでは検証しない。


class TestTunedParams:
    """plugin_tuned_params の upsert/get（Issue #264・ハイパーパラメータ自動探索の永続化）。"""

    def test_get_none_when_unset(self, db):
        from database import get_tuned_params
        assert get_tuned_params(db, "macro_gbdt") is None

    def test_upsert_and_get_round_trip(self, db):
        from database import get_tuned_params, upsert_tuned_params
        upsert_tuned_params(
            db, "macro_gbdt", {"max_depth": 4, "learning_rate": 0.05}, "rank_ic", 0.083,
            [{"params": {"max_depth": 4}, "score": 0.083}] * 25,  # 20件超のリーダーボード
            n_combos=200, data_fingerprint="fp1",
        )
        got = get_tuned_params(db, "macro_gbdt")
        assert got["params"] == {"max_depth": 4, "learning_rate": 0.05}
        assert got["objective_name"] == "rank_ic"
        assert got["objective_value"] == pytest.approx(0.083)
        assert got["n_combos"] == 200
        assert got["data_fingerprint"] == "fp1"

    def test_upsert_is_idempotent_per_plugin_name(self, db):
        """同一 plugin_name の再 upsert は上書き（複数行にならない）。"""
        from database import PluginTunedParams, upsert_tuned_params
        upsert_tuned_params(db, "macro_gbdt", {"max_depth": 4}, "rank_ic", 0.1, [], 10, None)
        upsert_tuned_params(db, "macro_gbdt", {"max_depth": 6}, "rank_ic", 0.2, [], 20, None)
        rows = db.query(PluginTunedParams).filter_by(plugin_name="macro_gbdt").all()
        assert len(rows) == 1
        assert rows[0].params_json == {"max_depth": 6}

    def test_leaderboard_truncated_to_top_20(self, db):
        from database import PluginTunedParams, upsert_tuned_params
        leaderboard = [{"params": {"x": i}, "score": i} for i in range(30)]
        upsert_tuned_params(db, "macro_dlm", {"x": 1}, "rank_ic", 0.5, leaderboard, 30, None)
        row = db.query(PluginTunedParams).filter_by(plugin_name="macro_dlm").one()
        assert len(row.leaderboard_json) == 20

    def test_different_plugins_independent(self, db):
        from database import get_tuned_params, upsert_tuned_params
        upsert_tuned_params(db, "macro_gbdt", {"a": 1}, "rank_ic", 0.1, [], 1, None)
        assert get_tuned_params(db, "macro_risk_return") is None
        assert get_tuned_params(db, "macro_gbdt")["params"] == {"a": 1}


class TestTuningDryRun:
    """database.tuning_dry_run() — 探索中の producer 永続化抑止（Issue #264）。"""

    def test_replace_macro_gbdt_scores_is_noop_during_dry_run(self, db):
        from database import get_macro_gbdt_scores, replace_macro_gbdt_scores, tuning_dry_run
        with tuning_dry_run():
            n = replace_macro_gbdt_scores(db, [{"edinet_code": "E1", "mu": 0.1}], "d")
        assert n == 0
        assert get_macro_gbdt_scores(db) == {}

    def test_replace_macro_dlm_scores_is_noop_during_dry_run(self, db):
        from database import get_macro_dlm_scores, replace_macro_dlm_scores, tuning_dry_run
        with tuning_dry_run():
            n = replace_macro_dlm_scores(db, [{"edinet_code": "E1", "mu": 0.1}], "d")
        assert n == 0
        assert get_macro_dlm_scores(db) == {}

    def test_replace_works_normally_outside_dry_run(self, db):
        """dry_run コンテキストを抜けた後は通常どおり永続化される。"""
        from database import get_macro_gbdt_scores, replace_macro_gbdt_scores, tuning_dry_run
        with tuning_dry_run():
            replace_macro_gbdt_scores(db, [{"edinet_code": "E1", "mu": 0.1}], "d")
        replace_macro_gbdt_scores(db, [{"edinet_code": "E2", "mu": 0.2}], "d")
        assert get_macro_gbdt_scores(db) == {"E2": pytest.approx(0.2)}

    def test_dry_run_resets_on_exception(self, db):
        """with ブロック内で例外が出ても _tuning_dry_run は必ず解除される。"""
        import database as database_module
        with pytest.raises(ValueError):
            with database_module.tuning_dry_run():
                raise ValueError("boom")
        assert database_module._tuning_dry_run.get() is False
