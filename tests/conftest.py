"""
pytest 共通設定・フィクスチャ。

DB は SQLite in-memory に差し替える。PostgreSQL 専用の SQL
（GIN インデックス・ADD COLUMN IF NOT EXISTS）は init_db ごと
パッチしてスキップする。
"""
import os
import sys
from pathlib import Path

# ─── 1. プロジェクトルートを sys.path に追加 ──────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ─── 2. 環境変数を差し替え（database/api の import より前に実行） ──
# database.py は import 時に create_engine() を呼ぶため、postgres 形式の
# ダミー URL を渡してエンジン生成だけ通過させる（実際は遅延接続なので問題なし）。
# その後、import 直後に SQLite engine へ差し替える。
os.environ["DATABASE_URL"] = "postgresql://localhost/dummy"
os.environ["APP_PASSWORD"] = ""              # 認証無し（テスト用）
os.environ["APP_SECRET_KEY"] = "test-secret"
os.environ["ALLOWED_ORIGIN"] = "http://testserver"
os.environ.setdefault("EDINET_API_KEY", "dummy")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ─── 3. database モジュールの engine/SessionLocal/init_db を差し替え ──
import database as _database

_test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_test_engine)

_database.engine = _test_engine
_database.SessionLocal = _TestSessionLocal


def _test_init_db():
    _database.Base.metadata.create_all(bind=_test_engine)


_database.init_db = _test_init_db
_test_init_db()  # 全テスト共通でテーブルを作成


# ─── 4. フィクスチャ ──────────────────────────────────────────────────

@pytest.fixture
def db():
    """関数ごとに新しいセッションを発行。終了時にテーブルをクリーンアップ。"""
    session = _TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
        # テーブル間の依存関係を考慮して逆順で全レコード削除
        with _test_engine.begin() as conn:
            for table in reversed(_database.Base.metadata.sorted_tables):
                conn.execute(table.delete())


@pytest.fixture
def client():
    """FastAPI TestClient。lifespan は呼ばない（init_db は既に実行済み）。"""
    from fastapi.testclient import TestClient
    import api as _api

    # api.SessionLocal が古い参照を持つので差し替え
    _api.SessionLocal = _TestSessionLocal

    with TestClient(_api.app) as c:
        yield c
    # クリーンアップ
    with _test_engine.begin() as conn:
        for table in reversed(_database.Base.metadata.sorted_tables):
            conn.execute(table.delete())


# ─── 5. テスト用ファクトリ ────────────────────────────────────────────

def make_company(db, edinet_code="E000001", sec_code="1301", name="テスト水産",
                 industry="水産・農林業", market="プライム"):
    """テスト用 Company レコードを作成・コミットして返す。"""
    from database import Company
    c = Company(edinet_code=edinet_code, sec_code=sec_code, name=name,
                industry=industry, market=market)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def make_record(db, edinet_code="E000001", year=2024, period_end="2024-03-31",
                **fields):
    """テスト用 FinancialRecord レコードを作成・コミットして返す。

    fields にはカラム名を直接指定（例: pl_revenue=1000.0, op_margin=10.0）。
    """
    from database import FinancialRecord
    defaults = {
        "edinet_code": edinet_code,
        "sec_code": "1301",
        "company_name": "テスト企業",
        "industry": "水産・農林業",
        "year": year,
        "period_end": period_end,
    }
    defaults.update(fields)
    r = FinancialRecord(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r
