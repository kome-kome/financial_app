"""plugins/total_return.py の execute() テスト"""
import pytest

from plugins.total_return import TotalReturnPlugin
from tests.conftest import make_company, make_record


@pytest.fixture
def plugin():
    return TotalReturnPlugin()


def _seed_records(db, n: int, base_eps: float = 50.0, base_bps: float = 500.0,
                  pe_ratio: float = 15.0):
    """株価=EPS*PE が成り立つ n 社のレコードを作成。"""
    for i in range(n):
        ec = f"E{i+1:06d}"
        sec = f"{1000 + i}"
        make_company(db, edinet_code=ec, sec_code=sec,
                     name=f"会社{i}", industry="建設業")
        eps = base_eps + i * 2.0
        bps = base_bps + i * 20.0
        stock_price = eps * pe_ratio  # ≒ implied PER
        make_record(
            db, edinet_code=ec, sec_code=sec,
            year=2024, period_end="2024-03-31",
            company_name=f"会社{i}", industry="建設業",
            pl_eps=eps, bs_bps=bps,
            stock_price=stock_price,
            bs_total_equity=bps * 1000.0,  # 株数推計用
            cf_operating_cf=eps * 800.0,
            dps=eps * 0.3,
            market_cap=stock_price * 1000.0 / 1_000_000,  # 百万円換算
            div_yield=3.0,
        )


class TestTotalReturn:
    async def test_raises_when_insufficient_data(self, plugin, db):
        """20件未満で ValueError"""
        _seed_records(db, n=10)
        with pytest.raises(ValueError, match="データが不足"):
            await plugin.execute({}, db)

    async def test_returns_ranking_with_enough_data(self, plugin, db):
        _seed_records(db, n=30)
        result = await plugin.execute({"top_n": 10}, db)
        # 返却フィールドの確認
        assert "cv_metrics" in result
        assert "feature_weights" in result
        assert "ranking" in result
        assert result["n_total_samples"] == 30
        assert len(result["ranking"]) <= 10

    async def test_ranking_sorted_by_total_return(self, plugin, db):
        _seed_records(db, n=30)
        result = await plugin.execute({"top_n": 30}, db)
        returns = [r["total_return_pct"] for r in result["ranking"]]
        # 降順ソート
        assert returns == sorted(returns, reverse=True)

    async def test_rank_field_assigned(self, plugin, db):
        _seed_records(db, n=30)
        result = await plugin.execute({"top_n": 20}, db)
        for i, item in enumerate(result["ranking"]):
            assert item["rank"] == i + 1

    async def test_feature_weights_mece_groups(self, plugin, db):
        """feature_weights には pl/cf/bs/div の MECE グループラベルが付く"""
        _seed_records(db, n=30)
        result = await plugin.execute({"use_cf": True}, db)
        groups = {w["group"] for w in result["feature_weights"].values()}
        assert {"pl", "cf", "bs", "div"} == groups

    async def test_use_cf_false_excludes_cf_feature(self, plugin, db):
        _seed_records(db, n=30)
        result = await plugin.execute({"use_cf": False}, db)
        assert "cf_ops_ps" not in result["feature_weights"]
        assert "pl_eps" in result["feature_weights"]
        assert "bs_bps" in result["feature_weights"]

    async def test_min_div_yield_filter(self, plugin, db):
        _seed_records(db, n=30)
        # div_yield=3.0 で seed しているので 5.0 でフィルタすると全件 0 になる
        result = await plugin.execute({"min_div_yield": 5.0}, db)
        assert len(result["ranking"]) == 0

    async def test_implied_per_pbr_returned(self, plugin, db):
        _seed_records(db, n=30)
        result = await plugin.execute({"top_n": 5}, db)
        for item in result["ranking"]:
            # implied_per は予測価格÷EPS、 implied_pbr は予測価格÷BPS
            assert item["implied_per"] is None or item["implied_per"] > 0
            assert item["implied_pbr"] is None or item["implied_pbr"] > 0

    async def test_cv_metrics_has_folds(self, plugin, db):
        _seed_records(db, n=30)
        result = await plugin.execute({"n_folds": 3}, db)
        cv = result["cv_metrics"]
        assert "folds" in cv
        assert "n_samples" in cv
        assert cv["n_samples"] == 30
