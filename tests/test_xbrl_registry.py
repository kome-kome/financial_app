"""再分類項目レジストリ（FinancialRecord 列 info → XBRL_MAP 射影）の drift テスト。

候補1「再分類項目レジストリを1つに畳む」の構造不変量を固定する:
  - 生タグの多対一の一意性（同一タグが2列に現れない）
  - 全 (section, field) に対応列が在る（parse 出力の silent-drop が構造的に不能）
  - collector.XBRL_MAP は build_xbrl_map() の射影と一致
  - upsert_financial は未知キーで fail fast（silent-drop の反対）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import FinancialRecord, build_xbrl_map, upsert_financial

_PREFIX_SECTIONS = ("bs", "pl", "cf")


def _column_name(section: str, field: str) -> str:
    """(section, field) → FinancialRecord の列名。bs/pl/cf は接頭辞付き、val/nonfin は素。"""
    return f"{section}_{field}" if section in _PREFIX_SECTIONS else field


class TestXbrlMapProjection:
    def test_no_duplicate_tag(self):
        # 同一生タグが2列の info["xbrl"] に現れない（多対一の一意性）。build_xbrl_map の
        # import 時ガードとは独立に、列定義そのものを直接検査して回帰を防ぐ。
        seen: dict[str, str] = {}
        for col in FinancialRecord.__table__.columns:
            for tag in col.info.get("xbrl", ()):
                assert tag not in seen, f"生タグ '{tag}' が {seen[tag]} と {col.name} に重複"
                seen[tag] = col.name

    def test_build_xbrl_map_count_matches_tags(self):
        # 射影の件数 == 全列の生タグ総数（dedup による黙った取りこぼしが無い）。
        total_tags = sum(len(c.info.get("xbrl", ())) for c in FinancialRecord.__table__.columns)
        assert len(build_xbrl_map()) == total_tags

    def test_every_target_has_column(self):
        # 全 (section, field) に対応列が在る → parse が emit する field は必ず列を持つ
        #（upsert の silent-drop が構造的に起こり得ない）。
        for section, field in build_xbrl_map().values():
            col = _column_name(section, field)
            assert hasattr(FinancialRecord, col), f"{section}.{field} に対応列 {col} が無い"

    def test_sections_are_known(self):
        for section, _ in build_xbrl_map().values():
            assert section in ("bs", "pl", "cf", "val", "nonfin")

    def test_collector_xbrl_map_is_projection(self):
        import collector
        assert collector.XBRL_MAP == build_xbrl_map()

    def test_derived_columns_have_no_tags(self):
        # 派生列（計算値）は生タグを持たない＝parse 対象外であることを固定。
        for name in ("cf_free_cf", "pl_ebitda", "pl_nonoperating_income"):
            col = FinancialRecord.__table__.columns[name]
            assert not col.info.get("xbrl"), f"{name} は派生列だが生タグを持っている"

    def test_negative_space_preserved(self):
        # 意図的に未登録の生タグ（金融持株会社で誤採用する生 OperatingRevenue1 本体）は
        # 射影に現れない。Summary 変種のみ採用される。
        m = build_xbrl_map()
        assert "OperatingRevenue1" not in m
        assert m.get("OperatingRevenue1SummaryOfBusinessResults") == ("pl", "revenue")


class TestUpsertFailFast:
    def _data(self, **over):
        d = {
            "edinet_code": "E09999", "year": 2023, "period_end": "2023-03-31",
            "bs": {"total_assets": 5000.0},
            "pl": {"revenue": 1000.0},
            "val": {"per": 15.0},
        }
        d.update(over)
        return d

    def test_raises_on_unknown_val_key(self, db):
        # val の typo（列に存在しないキー）は silent-drop せず ValueError。
        with pytest.raises(ValueError):
            upsert_financial(db, self._data(val={"per": 15.0, "nonexistent_metric": 1.0}))

    def test_raises_on_unknown_nonfin_key(self, db):
        with pytest.raises(ValueError):
            upsert_financial(db, self._data(nonfin={"headcount_typo": 100.0}))

    def test_valid_keys_do_not_raise(self, db):
        # 正規キー（derived は flat に入らないため検証対象外）は従来どおり通る。
        obj = upsert_financial(db, self._data(derived={"roe": 8.0}, nonfin={"employees": 100.0}))
        db.commit()
        assert obj.bs_total_assets == 5000.0
        assert obj.employees == 100.0
