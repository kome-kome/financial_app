"""M-7 の JAX 実装（`plugins/seq_transformer.py`）のテスト。

jax が要るテストは `jax_ok` を使う。jax の扱い（#792 の skip 化を繰り返さない）:
- 未導入: CI（環境変数 `CI`）なら fail（requirements-dev.txt に入っているはず）、ローカルなら skip
- 導入済みで読めない（Smart App Control の DLL 遮断を含む）: fail。ローカルで遮断中は該当テストだけ
  `--deselect` して全件を回し、CI（ubuntu）で確かめる（docs/GOTCHAS.md）

守ること:
- テストラベルだけ差し替えても予測がビット一致する（リーク検査・ADR-0021 Decision 4 の作法）
- 同じ入力で 2 回回すとビット一致する（種の固定）
- 月末より後の株価を全社書き換えても、その月末の予測がビット一致する（系列〜モデルの端から端）
- 合成データ（系列に信号）で OOF の rank-IC が出て（`n > 0`・ADR-0014）、系列なしの対照を上回る
- fold の中の例外は CV の後で送出される（`walk_forward_cv_monthly` は握りつぶす）
- `import plugins` で jax を読まない（Render の自動検出）
"""
from __future__ import annotations

import importlib.util
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from plugins import seq_transformer as st
from plugins import weekly_sequences as ws
from plugins.macro_snapshots import oof_backtest
from plugins.utils import walk_forward_cv_monthly

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def jax_ok():
    if importlib.util.find_spec("jax") is None:
        if os.environ.get("CI"):
            pytest.fail("CI に jax が無い（requirements-dev.txt に jax・jaxlib を入れる）", pytrace=False)
        pytest.skip("jax 未導入")
    try:
        st.require_jax()
    except st.JaxUnavailable as e:
        pytest.fail(str(e), pytrace=False)


def _tiny_cfg(**kw) -> st.TFConfig:
    base = dict(n_static=3, d_model=16, n_layers=1, dropout=0.0, learning_rate=3e-3,
                max_epochs=30, patience=5, batch_size=128)
    base.update(kw)
    return st.TFConfig(**base)


def _ym(i: int) -> str:
    return f"{2020 + i // 12}-{i % 12 + 1:02d}"


def _synthetic(n_months: int = 30, n_per_month: int = 300, seed: int = 0):
    """系列に信号を持たせた合成パネル。y = −直近4週の ret_z の和/2 ＋ 0.3·財務0 ＋ 雑音。"""
    rng = np.random.default_rng(seed)
    n = n_months * n_per_month
    windows = np.zeros((n, ws.SEQ_WEEKS, ws.N_CHANNELS), dtype=np.float32)
    windows[:, :, 0] = rng.normal(size=(n, ws.SEQ_WEEKS))
    windows[:, :, 1] = rng.normal(size=(n, ws.SEQ_WEEKS))
    windows[:, :, ws.OBS_CHANNEL] = 1.0
    fin = rng.normal(size=(n, 3))
    sig = -windows[:, -4:, 0].sum(axis=1) / 2.0
    y = sig + 0.3 * fin[:, 0] + 0.5 * rng.normal(size=n)
    patches, pmask = ws.to_patches(windows)
    samples_by_ym: dict = {}
    codes = np.empty(n, dtype=np.int64)
    for i in range(n):
        m = i // n_per_month
        ym = _ym(m)
        codes[i] = st.ym_code(ym)
        samples_by_ym.setdefault(ym, []).append((fin[i].tolist(), float(y[i]), i))
    return samples_by_ym, st.SampleBank(ym_codes=codes, patches=patches, pmask=pmask)


def _run_cv(cfg, samples_by_ym, bank, seed=42):
    errors: list = []
    diag: dict = {}
    fp = st.make_fit_predict(cfg, bank, seed, errors, diag)
    _folds, res = walk_forward_cv_monthly(
        samples_by_ym, ["f0", "f1", "f2"], min_train_months=6, step_months=3,
        return_residuals=True, fit_predict=fp, embargo_months=12, pass_train_groups=True)
    st.raise_fold_errors(errors)
    return res, diag


# ── jax 不要 ─────────────────────────────────────────────────────────────────

class TestConfig:
    def test_defaults_are_valid(self):
        cfg = st.TFConfig(n_static=19)
        assert (cfg.d_model, cfg.n_layers, cfg.n_heads, cfg.batch_size) == (32, 2, 4, 512)

    @pytest.mark.parametrize("kw", [
        {"n_static": 0}, {"d_model": 30}, {"d_model": 2}, {"n_layers": 0},
        {"n_layers": st.MAX_LAYERS + 1}, {"dropout": 1.0}, {"learning_rate": 0.0},
        {"max_epochs": 0}, {"patience": 0},
    ])
    def test_invalid_rejected(self, kw):
        base = {"n_static": 3}
        base.update(kw)
        with pytest.raises(ValueError):
            st.TFConfig(**base)

    def test_hashable(self):
        assert hash(_tiny_cfg()) == hash(_tiny_cfg())


class TestEsMonthSplit:
    def test_short_history_keeps_min_fit(self):
        # 6 か月 × 160 行: 検証 1 か月・空白 2 か月（学習に 3 か月残す）
        assert st.es_month_split([160] * 6) == (480, 800)

    def test_long_history_uses_full_gap(self):
        # 40 か月 × 100 行: 検証 8 か月（=20%）・空白 12 か月・学習 20 か月
        assert st.es_month_split([100] * 40) == (2000, 3200)

    def test_too_few_months(self):
        assert st.es_month_split([100] * st.ES_MIN_FIT_MONTHS) is None
        assert st.es_month_split([]) is None

    def test_boundaries_fall_on_month_edges(self):
        groups = [37, 52, 41, 60, 45, 58, 49, 63, 40, 55, 47, 61, 44, 50, 39, 66, 42, 53]
        n_fit, v0 = st.es_month_split(groups)
        edges = set(np.cumsum([0] + groups).tolist())
        assert n_fit in edges and v0 in edges
        assert n_fit <= v0 < sum(groups)


class TestTarget:
    def test_demean_by_month(self):
        y = np.array([1.0, 2.0, 3.0, 10.0, 20.0])
        out = st.demean_by_month(y, [3, 2])
        assert np.allclose(out, [-1.0, 0.0, 1.0, -5.0, 5.0])

    def test_demean_rejects_mismatch(self):
        with pytest.raises(ValueError):
            st.demean_by_month(np.zeros(4), [3, 2])

    def test_prepare_target(self):
        rng = np.random.default_rng(1)
        y = rng.normal(0.05, 0.3, size=200)
        z, level, sd = st.prepare_target(y.tolist(), [100, 100])
        assert z.dtype == np.float32
        assert abs(float(z.mean())) < 1e-5 and abs(float(z.std()) - 1.0) < 1e-4
        assert sd > 0 and abs(level - 0.05) < 0.1
        # 月内の平均は 0（市場の成分を学習しない）
        assert abs(float(z[:100].mean())) < 1e-5 and abs(float(z[100:].mean())) < 1e-5

    def test_constant_target_does_not_divide_by_zero(self):
        z, _level, sd = st.prepare_target([0.1] * 10, [5, 5])
        assert sd == 1.0 and np.all(np.isfinite(z))


class TestHelpers:
    def test_fold_seed_depends_on_month(self):
        assert st.fold_seed(42, 202401) == st.fold_seed(42, 202401)
        assert st.fold_seed(42, 202401) != st.fold_seed(42, 202404)
        assert 0 <= st.fold_seed(10 ** 9, 209912) < 2 ** 31

    def test_ym_code(self):
        assert st.ym_code("2024-07") == 202407

    def test_median_epochs(self):
        assert st.median_epochs([3, 9, 5]) == 5
        assert st.median_epochs([]) == st.FALLBACK_EPOCHS
        assert st.median_epochs([0]) == 1

    def test_static_transform_uses_train_stats(self):
        X, prep = st.fit_static([[1.0, 10.0], [3.0, float("nan")], [5.0, 30.0], [7.0, 40.0]], 2)
        assert X.shape == (4, 2) and X.dtype == np.float32
        out = st.transform_static([[4.0, 25.0]], prep)
        assert out.shape == (1, 2) and np.all(np.isfinite(out))
        assert st.transform_static([], prep).shape == (0, 2)


class TestFoldErrorsSurface:
    def test_errors_inside_folds_are_raised_after_cv(self, monkeypatch):
        samples, bank = _synthetic(n_months=24, n_per_month=10)

        def boom(*a, **k):
            raise RuntimeError("学習が落ちた")
        monkeypatch.setattr(st, "fit_model", boom)
        errors: list = []
        fp = st.make_fit_predict(_tiny_cfg(), bank, 42, errors)
        folds, res = walk_forward_cv_monthly(
            samples, ["f0", "f1", "f2"], min_train_months=6, step_months=3,
            return_residuals=True, fit_predict=fp, embargo_months=12, pass_train_groups=True)
        assert folds == [] and res == {}          # CV は黙って fold を捨てる
        assert len(errors) == 2                   # 18・21 か月目の 2 fold
        with pytest.raises(RuntimeError, match="学習が落ちた"):
            st.raise_fold_errors(errors)

    def test_missing_train_groups_is_an_error(self):
        samples, bank = _synthetic(n_months=2, n_per_month=5)
        errors: list = []
        fp = st.make_fit_predict(_tiny_cfg(), bank, 42, errors)
        with pytest.raises(ValueError, match="train_groups"):
            fp(samples[_ym(0)], samples[_ym(1)])
        assert len(errors) == 1

    def test_no_errors_is_silent(self):
        st.raise_fold_errors([])

    def test_sequence_required_when_enabled(self):
        with pytest.raises(ValueError, match="系列"):
            st.fit_model(_tiny_cfg(), [[0.0, 0.0, 0.0]] * 5, [0.0] * 5, [5], None, None, 1)


def test_import_plugins_does_not_load_jax():
    """`plugins` の自動検出（Render でも走る）が jax を読まない。jax の有無に関わらず確かめる。"""
    code = ("import sys; import plugins; "
            "bad = sorted(m for m in sys.modules if m == 'jax' or m.startswith('jax.') "
            "or m == 'jaxlib' or m.startswith('jaxlib.')); "
            "print('LOADED', bad)")
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "LOADED []" in result.stdout, result.stdout[-500:]


def _pin(path: Path, name: str) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^{re.escape(name)}==(\S+)", line.strip())
        if m:
            return m.group(1)
    return None


@pytest.mark.parametrize("name", ["jax", "jaxlib"])
def test_dev_and_inference_pin_the_same_jax(name):
    """CI で測る jax と、ローカルの推論・M-7 が使う jax を同じ版にする（片方だけ上げない）。"""
    dev = _pin(REPO_ROOT / "requirements-dev.txt", name)
    inf = _pin(REPO_ROOT / "requirements-inference.txt", name)
    assert dev is not None and dev == inf, (name, dev, inf)


# ── jax が要る ───────────────────────────────────────────────────────────────

def _small_fold(n_months=8, n_per_month=40, seed=3):
    samples, bank = _synthetic(n_months=n_months, n_per_month=n_per_month, seed=seed)
    yms = sorted(samples)
    train = [s for ym in yms[:-1] for s in samples[ym]]
    test = samples[yms[-1]]
    groups = [len(samples[ym]) for ym in yms[:-1]]
    return train, test, groups, bank


class TestWithJax:
    def test_deterministic(self, jax_ok):
        train, test, groups, bank = _small_fold()
        cfg = _tiny_cfg(max_epochs=4, dropout=0.1)
        outs = []
        for _ in range(2):
            fp = st.make_fit_predict(cfg, bank, 42, [])
            outs.append(fp(train, test, groups)[0])
        assert np.array_equal(np.asarray(outs[0]), np.asarray(outs[1]))

    def test_test_labels_do_not_leak(self, jax_ok):
        train, test, groups, bank = _small_fold()
        cfg = _tiny_cfg(max_epochs=4)
        a, ya = st.make_fit_predict(cfg, bank, 42, [])(train, test, groups)
        perturbed = [(r, y * -3.0 + 1.234, i) for r, y, i in test]
        b, yb = st.make_fit_predict(cfg, bank, 42, [])(train, perturbed, groups)
        assert np.array_equal(np.asarray(a), np.asarray(b))
        assert ya != yb

    def test_no_sequence_variant_ignores_patches(self, jax_ok):
        train, test, groups, bank = _small_fold()
        cfg = _tiny_cfg(max_epochs=3, use_sequence=False)
        no_seq_bank = st.SampleBank(ym_codes=bank.ym_codes)
        a, _ = st.make_fit_predict(cfg, no_seq_bank, 42, [])(train, test, groups)
        b, _ = st.make_fit_predict(cfg, bank, 42, [])(train, test, groups)
        assert np.array_equal(np.asarray(a), np.asarray(b))

    def test_fixed_epochs_and_diag(self, jax_ok):
        train, test, groups, bank = _small_fold()
        fm = st.fit_model(_tiny_cfg(), [s[0] for s in train], [s[1] for s in train], groups,
                          *bank.take(np.array([s[2] for s in train])), 7, epochs=2)
        assert fm.epochs == 2 and not fm.es_used
        P, M = bank.take(np.array([s[2] for s in test]))
        pred = fm.predict([s[0] for s in test], P, M)
        assert pred.shape == (len(test),) and np.all(np.isfinite(pred))

    def test_future_prices_do_not_change_prediction(self, jax_ok):
        """週次株価 → 系列 → モデルの端から端で、月末より後の値を読まない。"""
        import datetime as dt
        from collections import namedtuple
        PX = namedtuple("PX", "trade_date close_last volume_sum")
        rng = np.random.default_rng(5)
        start = dt.date(2020, 1, 3)
        rows = {}
        for c in range(40):
            p = 100.0
            rr = []
            for i in range(110):
                p *= math.exp(rng.normal(0, 0.03))
                rr.append(PX((start + dt.timedelta(weeks=i)).isoformat(), p,
                             float(rng.integers(1000, 9000))))
            rows[f"E{c:05d}"] = rr
        cut = (start + dt.timedelta(weeks=80)).isoformat()
        train_dates = [(start + dt.timedelta(weeks=w)).isoformat() for w in (55, 60, 65, 70)]
        codes = sorted(rows)
        fin = rng.normal(size=(len(codes), 3)).tolist()
        y = rng.normal(size=len(codes) * len(train_dates)).tolist()

        def predict(rows_by_co):
            store = ws.build_weekly_store(rows_by_co)
            tr_codes = [c for _d in train_dates for c in codes]
            tr_dates = [d for d in train_dates for _c in codes]
            P_tr, M_tr = ws.to_patches(ws.gather_windows(store, tr_codes, tr_dates))
            P_te, M_te = ws.to_patches(ws.gather_windows(store, codes, [cut] * len(codes)))
            fm = st.fit_model(_tiny_cfg(), fin * len(train_dates), y,
                              [len(codes)] * len(train_dates), P_tr, M_tr, 11, epochs=2)
            return fm.predict(fin, P_te, M_te)

        mutated = {ec: [PX(r.trade_date, r.close_last * 5.0, r.volume_sum * 7.0)
                        if r.trade_date > cut else r for r in rr] for ec, rr in rows.items()}
        assert np.array_equal(predict(rows), predict(mutated))


@pytest.fixture(scope="module")
def synthetic_oof(jax_ok):
    samples, bank = _synthetic()
    res_seq, diag = _run_cv(_tiny_cfg(), samples, bank)
    res_flat, _ = _run_cv(_tiny_cfg(use_sequence=False), samples,
                          st.SampleBank(ym_codes=bank.ym_codes))
    return oof_backtest(res_seq), oof_backtest(res_flat), diag


class TestLearnsSyntheticSignal:
    def test_oof_is_not_empty(self, synthetic_oof):
        seq, _flat, diag = synthetic_oof
        assert seq["rank_ic"]["n"] > 0              # ADR-0014: OOF が空のまま緑にしない
        assert len(diag["epochs"]) == seq["rank_ic"]["n"] == 4
        assert all(diag["es_used"])

    def test_sequence_branch_beats_the_flat_control(self, synthetic_oof):
        seq, flat, _diag = synthetic_oof
        assert seq["rank_ic"]["mean"] > 0.3, seq["rank_ic"]
        assert seq["rank_ic"]["mean"] > flat["rank_ic"]["mean"] + 0.1, (seq["rank_ic"], flat["rank_ic"])
