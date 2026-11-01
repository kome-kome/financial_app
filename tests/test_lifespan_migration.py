"""起動時（lifespan）の書き込みが移行用の接続を通ること（#1100・ADR-0038 追補）。

Render の常用接続を SELECT 中心のロールへ絞ったとき、起動時の書き込み3つ——`init_db()` の DDL・
`APP_PASSWORD` の平文→ハッシュ移行・`running` のまま残ったジョブの回収——が常用の接続へ戻ると、
権限不足で起動が落ちる（DDL・パスワード移行）か、黙って回収されない。ここでは3つとも
`migration_engine()` の engine を通ることを、SQLite の engine を差し込んで確かめる。
"""
import asyncio
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")

import api  # noqa: E402
from database import AppSetting, Base, CollectionLog  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


def _seeded_engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(AppSetting.__table__.insert().values(key="APP_PASSWORD", value="plain-pw"))
        conn.execute(CollectionLog.__table__.insert().values(job_type="incremental", status="running"))
    return eng


def test_startup_writes_go_through_the_migration_engine(monkeypatch):
    mig = _seeded_engine()
    seen = []

    @contextmanager
    def fake_migration_engine():
        yield mig

    monkeypatch.setattr(api, "migration_engine", fake_migration_engine)
    monkeypatch.setattr(api, "init_db", lambda bind=None: seen.append(bind))
    monkeypatch.setattr(api, "APP_PASSWORD", api.APP_PASSWORD)   # lifespan が書き換えるので戻す

    async def run():
        async with api.lifespan(api.app):
            pass

    asyncio.run(run())

    assert seen == [mig], "init_db に移行用の engine が渡っていない"
    with mig.connect() as conn:
        stored = conn.execute(
            select(AppSetting.value).where(AppSetting.key == "APP_PASSWORD")).scalar()
        status = conn.execute(select(CollectionLog.status)).scalar()
    assert api._is_password_hashed(stored), "パスワード移行が移行用の engine の DB に書かれていない"
    assert status == "error", "スタックジョブの回収が移行用の engine の DB に書かれていない"
