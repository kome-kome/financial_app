"""plugins/sector_ols.py の execute() テスト"""
import pytest

from plugins.sector_ols import SectorOLSPlugin, DEFAULT_FEATURES_MKTCAP
from database import FinancialRecord
from tests.conftest import make_company, make_record


@pytest.fixture
def plugin():
    return SectorOLSPlugin()


def _seed_industry_records(db, industry: str, n: int, prefix: str = "E"):
    """同一業種で n 社のレコードを作成。市場規模・財務指標を線形に変化させる。"""
    for i in range(n):
        ec = f"{prefix}{i+1:06d}"
        sec = f"{1000 + i}"
        make_company(db, edinet_code=ec, sec_code=sec,
                     name=f"テスト{prefix}{i}", industry=industry)
        # market_cap が pl_revenue や bs_total_equity と線形相関するよう設計
        make_record(
            db, edinet_code=ec, sec_code=sec,
            year=2024, period_end="2024-03-31", industry=industry,
            pl_revenue=1000.0 * (i + 1),
            pl_operating_profit=100.0 * (i + 1),
            pl_net_income=80.0 * (i + 1),
            bs_total_equity=2000.0 * (i + 1),
            cf_operating_cf=150.0 * (i + 1),
            stock_price=1000.0 + i * 10,
            pl_eps=50.0 + i,
            bs_bps=500.0 + i * 10,
            market_cap=5000.0 * (i + 1) + 100.0,  # 線形関係
        )


class TestSectorOLS:
    async def test_raises_when_no_data(self, plugin, db):
        with pytest.raises(ValueError, match="データがありません"):
            await plugin.execute({}, db)

    async def test_raises_when_no_features(self, plugin, db):
        # `features=[]` だと `or DEFAULT_FEATURES_MKTCAP` でフォールバックされるため、
        # CSV 形式の空文字列（カンマのみ）を渡して split 後に空 list になるパスを通す
        _seed_industry_records(db, "建設業", n=10)
        with pytest.raises(ValueError, match="説明変数を1つ以上"):
            await plugin.execute({"features": ","}, db)

    async def test_raises_when_no_sector_meets_min_samples(self, plugin, db):
        # 3社のみ → min_samples=5 を満たさない
        _seed_industry_records(db, "建設業", n=3)
        with pytest.raises(ValueError, match="分析可能な業種がありません"):
            await plugin.execute({"min_samples": 10}, db)

    async def test_writes_predicted_market_cap_and_gap(self, plugin, db):
        """実行後、DBに predicted_market_cap と gap_ratio が書き込まれる"""
        _seed_industry_records(db, "建設業", n=10)
        result = await plugin.execute(
            {"features": DEFAULT_FEATURES_MKTCAP, "min_samples": 5}, db
        )
        assert result["n_sectors"] == 1
        assert result["n_total"] == 10
        # 業種統計が含まれる
        assert any(s["industry"] == "建設業" for s in result["sector_stats"])

        # DB に gap_ratio が書き込まれているか
        recs = db.query(FinancialRecord).all()
        with_gap = [r for r in recs if r.gap_ratio is not None]
        assert len(with_gap) == 10
        # 全て predicted_market_cap も埋まる
        assert all(r.predicted_market_cap is not None for r in with_gap)

    async def test_high_r2_for_linear_data(self, plugin, db):
        """線形に並べたデータでは R² が高い"""
        _seed_industry_records(db, "建設業", n=15)
        result = await plugin.execute(
            {"features": ["pl_revenue", "bs_total_equity"], "min_samples": 5}, db
        )
        stats = result["sector_stats"][0]
        # 完全線形相関なら r2 ≈ 1.0
        assert stats["r2"] >= 0.95

    async def test_sector_ranks_assigned(self, plugin, db):
        """予測結果に sector_rank が付く（1〜N）"""
        _seed_industry_records(db, "建設業", n=8)
        result = await plugin.execute(
            {"features": DEFAULT_FEATURES_MKTCAP, "min_samples": 5}, db
        )
        ranks = sorted(r["sector_rank"] for r in result["results"])
        assert ranks == list(range(1, 9))

    async def test_skips_sector_below_min_samples(self, plugin, db):
        """min_samples 未満の業種は n_skipped_sectors にカウントされる"""
        _seed_industry_records(db, "建設業", n=10)        # OK
        _seed_industry_records(db, "建材",   n=3, prefix="X")  # スキップ
        result = await plugin.execute(
            {"features": DEFAULT_FEATURES_MKTCAP, "min_samples": 5}, db
        )
        assert result["n_skipped_sectors"] == 1
        assert result["n_sectors"] == 1

    async def test_skips_record_without_industry(self, plugin, db):
        """industry が None / 空文字のレコードは集計対象外"""
        _seed_industry_records(db, "建設業", n=10)
        make_company(db, edinet_code="E999999", sec_code="9999")
        make_record(db, edinet_code="E999999", sec_code="9999",
                    year=2024, period_end="2024-03-31",
                    industry=None,
                    pl_revenue=1000.0, pl_operating_profit=100.0,
                    pl_net_income=80.0, bs_total_equity=2000.0,
                    cf_operating_cf=150.0, market_cap=5000.0)
        result = await plugin.execute(
            {"features": DEFAULT_FEATURES_MKTCAP, "min_samples": 5}, db
        )
        # 11社いるが業種ありは 10 のみカウント
        assert result["n_total"] == 10

    async def test_skips_record_with_nonpositive_target(self, plugin, db):
        """market_cap <= 0 のレコードは除外（log 正規化のため）"""
        _seed_industry_records(db, "建設業", n=10)
        # 負の market_cap を持つ追加レコード
        make_company(db, edinet_code="E_NEG", sec_code="9000")
        make_record(db, edinet_code="E_NEG", sec_code="9000", year=2024,
                    period_end="2024-03-31", industry="建設業",
                    pl_revenue=100.0, pl_operating_profit=10.0,
                    pl_net_income=5.0, bs_total_equity=200.0,
                    cf_operating_cf=15.0,
                    market_cap=-100.0)
        result = await plugin.execute(
            {"features": DEFAULT_FEATURES_MKTCAP, "min_samples": 5}, db
        )
        assert result["n_total"] == 10  # 負値は除外
