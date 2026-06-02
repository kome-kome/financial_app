"""pytest 共通設定・fixture。

プラグインの execute() は SQLAlchemy Session を要求するため、in-memory SQLite に
ORM モデルからテーブルを生成して渡す。init_db() は Postgres 専用 SQL（gin / DOUBLE
PRECISION）を含むので呼ばず、Base.metadata.create_all で生成する。
モデルは Integer/String/Float/DateTime/JSON/LargeBinary のみで SQLite 互換。
"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# テストではレート制限（slowapi）を既定で無効化し、テスト間の誤発火を防ぐ。
# 個別検証は tests/test_rate_limit.py で limiter.enabled を実行時に切り替えて行う。
os.environ.setdefault("APP_RATELIMIT_ENABLED", "false")

# プロジェクトルートを import パスに追加（database / plugins を import するため）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Base, Company, FinancialRecord, StockPriceHistory  # noqa: E402


@pytest.fixture
def db():
    """各テスト独立の in-memory SQLite Session。

    StaticPool + check_same_thread=False により、FastAPI TestClient が
    エンドポイントを別スレッド（anyio portal）で実行しても同一の in-memory DB を
    共有できる（接続を 1 本に固定）。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ── テストデータ・ファクトリ（factory-as-fixture）────────────────────────────
# 既定値を持たせ、各テストは関連カラムだけ override する。

_FIN_DEFAULTS = dict(
    edinet_code="E00001",
    sec_code="1001",
    company_name="テスト株式会社",
    industry="情報・通信業",
    year=2023,
    period_end="2023-03-31",
    market_cap=10000.0,   # 百万円
)


@pytest.fixture
def make_fin():
    def _make(**overrides):
        return FinancialRecord(**{**_FIN_DEFAULTS, **overrides})
    return _make


@pytest.fixture
def make_company():
    def _make(**overrides):
        data = dict(edinet_code="E00001", sec_code="1001",
                    name="テスト株式会社", industry="情報・通信業")
        data.update(overrides)
        return Company(**data)
    return _make


@pytest.fixture
def make_price():
    def _make(**overrides):
        data = dict(edinet_code="E00001", sec_code="1001",
                    trade_date="2023-01-04", close=1000.0)
        data.update(overrides)
        return StockPriceHistory(**data)
    return _make
