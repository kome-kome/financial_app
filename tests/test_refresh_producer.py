"""`scripts/refresh_producer.py`（日中キューの `model:*`・#1317）のテスト。

守ること:
- 夜間の `nightly_scores.run_models` を共有し、予測の写し（`snapshot_predictions`）はしない
- 回せるのは永続化の検証があり、夜間で回さないモデルだけ（夜間のモデルは写しを飛ばすので拒む）
- 失敗したら非 0 で終わる
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import nightly_scores
from scripts import refresh_producer as rp


def test_allowed_models_are_verified_and_not_nightly():
    allowed = rp.allowed_models()
    assert "macro_transformer" in allowed
    assert not set(allowed) & set(nightly_scores.NIGHTLY_MODELS)
    assert set(allowed) <= set(nightly_scores.VERIFIERS)


@pytest.mark.parametrize("name", ["macro_enet", "sector_ols", "no_such_model"])
def test_refuses_models_it_must_not_run(name):
    with pytest.raises(SystemExit) as e:
        rp.main(["--model", name])
    assert e.value.code == 2


def _fake_entries(ok: bool) -> list[dict]:
    return [{"model": "macro_transformer", "ok": ok, "summary": "s", "verified": "v" if ok else None,
             "error": None if ok else "RuntimeError: x", "elapsed_min": 0.1,
             "diagnostics_error": None, "side_output_error": None}]


@pytest.fixture
def fake_session(monkeypatch):
    import database
    session = MagicMock()
    monkeypatch.setattr(database, "SessionLocal", lambda: session)
    monkeypatch.setattr(nightly_scores, "snapshot_predictions",
                        MagicMock(side_effect=AssertionError("写しをしてはいけない")))
    return session


def test_runs_one_model_through_run_models(monkeypatch, fake_session):
    calls = []

    async def fake_run_models(models, db, **kw):
        calls.append((list(models), db))
        return _fake_entries(True)
    monkeypatch.setattr(nightly_scores, "run_models", fake_run_models)
    entries = asyncio.run(rp.run("macro_transformer"))
    assert calls == [(["macro_transformer"], fake_session)]
    assert entries[0]["ok"] is True
    fake_session.close.assert_called_once()


def test_failure_exits_non_zero(monkeypatch, fake_session):
    async def fake_run_models(models, db, **kw):
        return _fake_entries(False)
    monkeypatch.setattr(nightly_scores, "run_models", fake_run_models)
    with pytest.raises(SystemExit) as e:
        asyncio.run(rp.run("macro_transformer"))
    assert "macro_transformer" in str(e.value.code)
    fake_session.close.assert_called_once()


def test_daytime_job_runs_this_cli_with_the_deps_smoke():
    from scripts import run_daytime as rd
    job = rd.JOBS["model:macro_transformer"]
    assert job.argv[1:] == ("-m", "scripts.refresh_producer", "--model", "macro_transformer")
    assert job.parallel_sensitive is True and job.needs_deps_smoke is True
    # jax を使うので WSL2 で起動し、依存の確認も Linux 側で行う（#1316・ADR-0080）
    assert [s.name for s in rd.steps_for("py", "model:macro_transformer")] == [rd.DEPS_SMOKE_WSL, job.name]
    assert "macro_transformer" in rd.heavy_models()
