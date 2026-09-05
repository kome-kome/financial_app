"""tests/test_macro_beta_store.py — #214 per-stock 階層ベイズ推論結果の永続化層。

macro_beta_loadings / macro_beta_meta への upsert・読み出し（producer 用）と、
macro_beta_inference.persist() の InferenceResult → DB 結線を検証する。
MCMC 本体（PyMC）は不要＝ローカルで実行可能。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (MACRO_BETA_STATUS_LIVE, MACRO_BETA_STATUS_QUARANTINED,
                      MacroBetaLoading, get_macro_beta, upsert_macro_beta)


def _meta(run_id="run1"):
    return {
        "run_id": run_id,
        "snapshot_date": "2026-06-01",
        "selected_factors": ["macro_usdjpy_yoy", "macro_vix_zscore"],
        "factor_cov": [[1.0, 0.1], [0.1, 1.0]],
        "hyperparams": {"draws": 1000, "tune": 1000},
    }


def _loadings(run_id="run1"):
    return [
        {"run_id": run_id, "edinet_code": "E001", "factor_name": "macro_usdjpy_yoy",
         "loading_mean": 0.5, "loading_se": 0.1},
        {"run_id": run_id, "edinet_code": "E001", "factor_name": "macro_vix_zscore",
         "loading_mean": -0.3, "loading_se": 0.2},
        {"run_id": run_id, "edinet_code": "E001", "factor_name": "_intercept",
         "loading_mean": 0.02, "loading_se": 0.01},
    ]


class TestMacroBetaStore:
    def test_roundtrip(self, db):
        n = upsert_macro_beta(db, _meta(), _loadings())
        assert n == 3
        meta, loadings = get_macro_beta(db)
        assert meta["run_id"] == "run1"
        assert meta["selected_factors"] == ["macro_usdjpy_yoy", "macro_vix_zscore"]
        assert meta["factor_cov"] == [[1.0, 0.1], [0.1, 1.0]]      # Σ_macro（R_macro 用）
        assert loadings["E001"]["macro_usdjpy_yoy"] == (0.5, 0.1)
        assert loadings["E001"]["_intercept"][0] == 0.02            # 切片が _intercept 行で復元可

    def test_idempotent_overwrite(self, db):
        upsert_macro_beta(db, _meta(), _loadings())
        changed = _loadings()
        changed[0]["loading_mean"] = 0.9                            # 同 run_id で値だけ変更
        upsert_macro_beta(db, _meta(), changed)
        rows = db.query(MacroBetaLoading).filter_by(run_id="run1").all()
        assert len(rows) == 3                                       # 重複行が増えない（冪等）
        _, loadings = get_macro_beta(db)
        assert loadings["E001"]["macro_usdjpy_yoy"][0] == 0.9       # 上書きされている

    def test_get_latest_and_explicit_run(self, db):
        upsert_macro_beta(db, _meta("runA"), _loadings("runA"))
        upsert_macro_beta(db, _meta("runB"), _loadings("runB"))
        meta, _ = get_macro_beta(db)                               # 既定＝最新（後挿入の runB）
        assert meta["run_id"] == "runB"
        meta_a, load_a = get_macro_beta(db, "runA")                # 明示 run_id
        assert meta_a["run_id"] == "runA"
        assert "E001" in load_a

    def test_missing_returns_none(self, db):
        meta, loadings = get_macro_beta(db)
        assert meta is None and loadings == {}

    def test_with_loadings_false_skips_the_loading_table(self, db):
        """meta だけ要る呼び出し（存在確認・selected_factors）は loadings を引かない（#482）。

        macro_beta_loadings は約4,400社 × 因子数の行を持ち、4呼び出しのうち3つが
        戻り値の loadings を捨てている。転送そのものを止める。
        """
        upsert_macro_beta(db, _meta(), _loadings())
        meta, loadings = get_macro_beta(db, with_loadings=False)
        assert meta["run_id"] == "run1"
        assert meta["selected_factors"] == ["macro_usdjpy_yoy", "macro_vix_zscore"]
        assert loadings == {}
        # 既定は従来どおり loadings 付き（macro_snapshots の producer 経路）
        _, full = get_macro_beta(db)
        assert full["E001"]["macro_usdjpy_yoy"] == (0.5, 0.1)

    def test_with_loadings_false_on_empty_db(self, db):
        meta, loadings = get_macro_beta(db, with_loadings=False)
        assert meta is None and loadings == {}

    def test_missing_run_id_raises(self, db):
        with pytest.raises(ValueError):
            upsert_macro_beta(db, {"snapshot_date": "2026-06-01"}, [])

    def test_persist_from_inference_result(self, db):
        from macro_beta_inference import InferenceResult, persist
        res = InferenceResult(
            run_id="mb_test", snapshot_date="2026-06-01",
            selected_factors=["macro_usdjpy_yoy"],
            loadings={"E001": {"macro_usdjpy_yoy": (0.4, 0.05)}},
            alpha={"E001": (0.01, 0.002)}, mu_pred={"E001": 0.03},
            factor_cov=[[1.0]],
        )
        persist(db, res)
        meta, loadings = get_macro_beta(db, "mb_test")
        assert meta["selected_factors"] == ["macro_usdjpy_yoy"]
        assert loadings["E001"]["macro_usdjpy_yoy"] == (0.4, 0.05)
        assert loadings["E001"]["_intercept"] == (0.01, 0.002)     # 切片が格納される


class TestQuarantine:
    """収束ゲートに落ちた run を**捨てずに隔離する**（#609）。

    2026-09-03 は 6時間かけて完走した run が `r_hat_max=1.1179 > 1.05` で reject され、
    loadings ごと消えて測り直しになった。ゲートの役目は「品質の悪い結果が即ライブ反映
    されるのを防ぐ」ことであって、計算を捨てることではない。
    """

    def test_quarantined_run_is_invisible_to_the_producer(self, db):
        upsert_macro_beta(db, dict(_meta("live1"), status=MACRO_BETA_STATUS_LIVE),
                          _loadings("live1"))
        upsert_macro_beta(db, dict(_meta("bad1"), status=MACRO_BETA_STATUS_QUARANTINED),
                          _loadings("bad1"))
        meta, loadings = get_macro_beta(db)
        assert meta["run_id"] == "live1", "隔離した run が最新として producer に返っている"
        assert loadings, "live な loadings まで消えている"

    def test_quarantined_run_is_still_readable_by_run_id(self, db):
        """**保全されていること自体**が隔離の目的。読めなければ捨てたのと同じ。"""
        upsert_macro_beta(db, dict(_meta("bad1"), status=MACRO_BETA_STATUS_QUARANTINED),
                          _loadings("bad1"))
        meta, loadings = get_macro_beta(db, "bad1")
        assert meta["status"] == MACRO_BETA_STATUS_QUARANTINED
        assert loadings["E001"]["macro_usdjpy_yoy"] == (0.5, 0.1)

    def test_null_status_is_treated_as_live(self, db):
        """既存2件（2026-07-04 / 08-01）は status 列が無かった時代の run。

        ここを quarantined 側へ倒すと、**列を足した瞬間に M-1 の入力が消える**。
        """
        upsert_macro_beta(db, _meta("old1"), _loadings("old1"))   # status を渡さない
        meta, _ = get_macro_beta(db)
        assert meta["run_id"] == "old1"

    def test_all_quarantined_means_no_producer_input(self, db):
        """隔離しか無いときは「未蓄積」と同じ扱い（producer は graceful degrade）。"""
        upsert_macro_beta(db, dict(_meta("bad1"), status=MACRO_BETA_STATUS_QUARANTINED),
                          _loadings("bad1"))
        meta, loadings = get_macro_beta(db)
        assert meta is None and loadings == {}

    def test_status_can_be_promoted_by_reupsert(self, db):
        """人が精査して昇格させる経路（同じ run_id を live で書き直す）。"""
        upsert_macro_beta(db, dict(_meta("bad1"), status=MACRO_BETA_STATUS_QUARANTINED),
                          _loadings("bad1"))
        assert get_macro_beta(db)[0] is None
        upsert_macro_beta(db, dict(_meta("bad1"), status=MACRO_BETA_STATUS_LIVE),
                          _loadings("bad1"))
        assert get_macro_beta(db)[0]["run_id"] == "bad1"

    def test_persist_defaults_to_live(self, db):
        """`persist` の status 省略は live（テストや手動呼び出しが黙って隔離されない）。"""
        from macro_beta_inference import InferenceResult, persist

        res = InferenceResult(
            run_id="mb_live", snapshot_date="2026-06-01",
            selected_factors=["macro_usdjpy_yoy"],
            loadings={"E001": {"macro_usdjpy_yoy": (0.4, 0.05)}},
            alpha={"E001": (0.01, 0.002)}, mu_pred={"E001": 0.03},
            factor_cov=[[1.0]],
        )
        persist(db, res)
        assert get_macro_beta(db)[0]["status"] == MACRO_BETA_STATUS_LIVE

    def test_persist_can_quarantine(self, db):
        from macro_beta_inference import InferenceResult, persist

        res = InferenceResult(
            run_id="mb_bad", snapshot_date="2026-06-01",
            selected_factors=["macro_usdjpy_yoy"],
            loadings={"E001": {"macro_usdjpy_yoy": (0.4, 0.05)}},
            alpha={"E001": (0.01, 0.002)}, mu_pred={"E001": 0.03},
            factor_cov=[[1.0]],
        )
        persist(db, res, status=MACRO_BETA_STATUS_QUARANTINED)
        assert get_macro_beta(db)[0] is None                      # producer からは見えない
        assert get_macro_beta(db, "mb_bad")[0]["status"] == MACRO_BETA_STATUS_QUARANTINED


class TestSchemaIsCheckedBeforeTheLongRun:
    """**6時間走ってから「列が無い」で落ちない**こと（#609）。

    `status` を足したとき、DDL を打つのは `init_db()`（API 起動時）だけでバッチは呼ばない。
    列が無い DB に対してもバッチは走り切ってしまい、persist の瞬間に落ちて結果が消える。
    """

    def test_passes_when_the_columns_exist(self, db):
        from macro_beta_inference import assert_persist_schema

        assert_persist_schema(db)      # 例外が出ないこと

    def test_fails_fast_when_a_column_is_missing(self, db, monkeypatch):
        import macro_beta_inference as mbi

        real_inspect = None

        class _FakeInspector:
            def get_columns(self, table):
                return [{"name": c} for c in ("run_id", "snapshot_date", "hyperparams")]

        monkeypatch.setattr("sqlalchemy.inspect", lambda bind: _FakeInspector())
        with pytest.raises(SystemExit) as exc:
            mbi.assert_persist_schema(db)
        assert "status" in str(exc.value) and "init_db" in str(exc.value)
        assert real_inspect is None

    def test_checked_columns_match_what_persist_writes(self):
        """チェックする列と `persist` が実際に書く列がずれたら意味が無い。"""
        import inspect as _inspect

        import macro_beta_inference as mbi

        src = _inspect.getsource(mbi.persist)
        for col in mbi.PERSIST_META_COLUMNS:
            assert f'"{col}":' in src, f"persist が書かない列 {col} をチェックしている"
