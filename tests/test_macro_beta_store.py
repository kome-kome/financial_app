"""tests/test_macro_beta_store.py — #214 per-stock 階層ベイズ推論結果の永続化層。

macro_beta_loadings / macro_beta_meta への upsert・読み出し（producer 用）と、
macro_beta_inference.persist() の InferenceResult → DB 結線を検証する。
MCMC 本体（PyMC）は不要＝ローカルで実行可能。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import upsert_macro_beta, get_macro_beta, MacroBetaLoading


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
