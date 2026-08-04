"""macro_health.py と macro-health.yml の不変条件ガード（Issue #420）。

守るのは2系統:

判定ロジック（macro_health.py）
  1. 期待更新頻度ごとの許容遅延で stale / missing を分類すること
  2. 既定モデルが使う系列（critical）だけが非ゼロ終了の根拠になること
     ＝昇格ゲートで棄却済みの GDELT/Wikimedia が落ちても毎日 failure にしない
  3. 除外系列は判定から外すが**レポートには必ず出す**（黙って消さない）
  4. 系列一覧は collector_prices の定義から生成する（テストに列挙を二重管理しない）

ワークフロー（macro-health.yml）
  5. `workflows:` は `["**"]` 固定（列挙は "[定常] …" の角括弧で startup_failure・#414）
  6. 収集ワークフローの name と `success` で絞ること（実在の name と一致するか）
  7. `tee` するステップに `set -o pipefail` があること（exit code が化ける・#352）
  8. 収集側と同じ API キーを渡すこと（キー有無で判定対象が変わるため）
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import macro_health as mh  # noqa: E402
from database import MacroData  # noqa: E402

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "macro-health.yml"
DAILY = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily-incremental.yml"
FULL = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "full-pipeline.yml"

AS_OF = date(2026, 8, 4)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """`on:` は YAML 1.1 で bool True にパースされるため両方を見る。"""
    return doc.get("on", doc.get(True)) or {}


def _seed(db, code: str, last: date):
    db.add(MacroData(series_code=code, series_name=code, category="test",
                     trade_date=last.isoformat(), close=1.0))
    db.commit()


def _seed_all_fresh(db, as_of: date = AS_OF):
    """除外系列を除く全ての収集対象を「当日更新済み」で埋める。"""
    for s in mh.expected_series():
        if s["code"] not in mh.EXCLUDED_SERIES:
            _seed(db, s["code"], as_of)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


# ── 判定ロジック ──────────────────────────────────────────────────────────

class TestFreshnessCheck:
    def test_all_fresh_is_healthy(self, db):
        _seed_all_fresh(db)
        r = mh.check_macro_freshness(db, as_of=AS_OF)
        assert r["stale"] == []
        assert r["missing"] == []
        assert r["n_critical_bad"] == 0
        assert r["checked"] == len(mh.expected_series()) - len(r["excluded"])

    def test_stale_critical_series_is_flagged(self, db):
        _seed_all_fresh(db)
        # USDJPY は日次（許容 14日）かつ M-1/M-2/M-3 の既定特徴量が参照する critical 系列
        db.query(MacroData).filter(MacroData.series_code == "USDJPY").delete()
        _seed(db, "USDJPY", AS_OF - timedelta(days=30))

        r = mh.check_macro_freshness(db, as_of=AS_OF)
        stale = {e["code"]: e for e in r["stale"]}
        assert "USDJPY" in stale
        assert stale["USDJPY"]["lag_days"] == 30
        assert stale["USDJPY"]["limit"] == mh.FREQ_STALE_DAYS["daily"]
        assert stale["USDJPY"]["critical"] is True
        assert r["n_critical_bad"] == 1

    def test_within_limit_is_not_flagged(self, db):
        """FRED 日次の公表ラグ（実測最大7日）＋週末で落ちないこと。"""
        _seed_all_fresh(db)
        db.query(MacroData).filter(MacroData.series_code == "USDJPY").delete()
        _seed(db, "USDJPY", AS_OF - timedelta(days=mh.FREQ_STALE_DAYS["daily"]))

        r = mh.check_macro_freshness(db, as_of=AS_OF)
        assert [e["code"] for e in r["stale"]] == []

    def test_missing_series_is_reported_separately(self, db):
        _seed_all_fresh(db)
        db.query(MacroData).filter(MacroData.series_code == "USDJPY").delete()
        db.commit()

        r = mh.check_macro_freshness(db, as_of=AS_OF)
        assert [e["code"] for e in r["missing"]] == ["USDJPY"]
        assert r["missing"][0]["lag_days"] is None
        assert r["n_critical_bad"] == 1

    def test_low_frequency_series_use_wider_limit(self, db):
        """月次・四半期は公表ラグが大きい（JP_IIP 実測 96日）ので日次基準で落とさない。"""
        _seed_all_fresh(db)
        db.query(MacroData).filter(MacroData.series_code == "JP_M2").delete()
        _seed(db, "JP_M2", AS_OF - timedelta(days=60))   # 月次: 許容 130日

        r = mh.check_macro_freshness(db, as_of=AS_OF)
        assert "JP_M2" not in {e["code"] for e in r["stale"]}

    def test_series_stale_days_overrides_freq_default(self, db, monkeypatch):
        """lag_days でシフトした系列は個別 stale_days で検知力を保つ（#444）。

        JP10Y_FRED は lag_days=70 で trade_date が後ろへずれる＝last が最大70日ぶん
        「新しく」見える。freq 既定（monthly=130日）のままでは配信停止の検知がその分
        遅れるため、系列定義の stale_days=60 を優先する。
        """
        import collector_prices as cp
        monkeypatch.setattr(cp, "FRED_API_KEY", "dummy")
        _seed_all_fresh(db)
        db.query(MacroData).filter(MacroData.series_code == "JP10Y_FRED").delete()
        # monthly 既定（130日）なら通る遅れだが、個別 stale_days=60 を超えるので stale
        _seed(db, "JP10Y_FRED", AS_OF - timedelta(days=100))

        r = mh.check_macro_freshness(db, as_of=AS_OF)
        stale = {e["code"]: e for e in r["stale"]}
        assert "JP10Y_FRED" in stale
        assert stale["JP10Y_FRED"]["limit"] == 60
        assert stale["JP10Y_FRED"]["limit"] < mh.FREQ_STALE_DAYS["monthly"]

    def test_lag_days_alone_does_not_tighten_limit(self, db, monkeypatch):
        """freq 既定から `lag_days` を一律に差し引いてはいけない（#444 の回帰防止）。

        `lag_days` は先読み防止の保守的シフト量であって実配信ラグの推定値ではない。
        一律差し引きにすると、2026-08-04 の本番実測 lag で以下4系列が即 CRITICAL になる:
        JP_REAL_GDP / JP_TRADE_BAL（実測80日 > 210-135=75）、
        JP_IIP / JP_IIP_INVENTORY（実測96日 > 130-60=70）。
        """
        import collector_prices as cp
        monkeypatch.setattr(cp, "FRED_API_KEY", "dummy")
        monkeypatch.setattr(cp, "ESTAT_API_KEY", "dummy")
        _seed_all_fresh(db)
        for code, lag in [("JP_REAL_GDP", 80), ("JP_TRADE_BAL", 80),
                          ("JP_IIP", 96), ("JP_IIP_INVENTORY", 96)]:
            db.query(MacroData).filter(MacroData.series_code == code).delete()
            _seed(db, code, AS_OF - timedelta(days=lag))

        r = mh.check_macro_freshness(db, as_of=AS_OF)
        assert {e["code"] for e in r["stale"]} == set()

    def test_excluded_series_never_counted_but_always_reported(self, db):
        """除外系列は判定対象外。ただしレポートには必ず出す（黙って消さない）。"""
        _seed_all_fresh(db)
        r = mh.check_macro_freshness(db, as_of=AS_OF)

        # 収集定義にまだ残っている除外系列が excluded に出る（JP_IP のように収集対象から
        # 既に外れた系列は expected_series に現れないため、部分集合であることを見る）
        excluded = {e["code"] for e in r["excluded"]}
        assert excluded <= set(mh.EXCLUDED_SERIES)
        assert {"JP10Y", "BCOM"} <= excluded
        assert excluded & {e["code"] for e in r["stale"] + r["missing"]} == set()
        report = "\n".join(mh.format_report(r))
        for code in excluded:
            assert code in report

    def test_excluded_series_have_reasons(self):
        """除外の理由は必須（後から「なぜ外したか」を辿れるようにする）。"""
        for code, reason in mh.EXCLUDED_SERIES.items():
            assert reason.strip(), code

    def test_gate_rejected_features_are_not_critical(self, db):
        """昇格ゲートで棄却済みの系列（GDELT/Wikimedia）は critical ではない。

        既定モデルが使わないものの一時失敗で毎日 failure にしない（#406/#409）。
        """
        critical = mh.critical_series_codes()
        assert "JP_NEWS_TONE" not in critical
        assert "JP_WIKI_MARKET_ATTN" not in critical
        # 既定 ON の系列は critical
        assert {"USDJPY", "VIX", "BAA_SPREAD"} <= critical

    def test_non_critical_stale_does_not_trigger_exit(self, db):
        _seed_all_fresh(db)
        db.query(MacroData).filter(MacroData.series_code == "JP_NEWS_TONE").delete()
        _seed(db, "JP_NEWS_TONE", AS_OF - timedelta(days=90))

        r = mh.check_macro_freshness(db, as_of=AS_OF)
        assert "JP_NEWS_TONE" in {e["code"] for e in r["stale"]}
        assert r["n_critical_bad"] == 0

    def test_expected_series_come_from_collector_definitions(self):
        """系列一覧は collector_prices の定義が唯一の情報源（列挙を二重管理しない）。"""
        import collector_prices as cp
        codes = {s["code"] for s in mh.expected_series()}
        assert {s["code"] for s in cp.MACRO_SERIES} <= codes
        assert {s["code"] for s in cp.BOJ_SERIES} <= codes
        # freq は全系列で許容遅延表に載っている値
        assert all(s["freq"] in mh.FREQ_STALE_DAYS for s in mh.expected_series())
        # stale_days は常にキーとして存在する（未指定は None＝freq 既定を使う・#444）
        assert all("stale_days" in s for s in mh.expected_series())

    def test_api_key_gated_groups_are_skipped_when_unset(self, monkeypatch):
        """キー未設定で収集自体が走らないグループは判定対象から外す（誤検知防止）。"""
        import collector_prices as cp
        monkeypatch.setattr(cp, "FRED_API_KEY", "")
        monkeypatch.setattr(cp, "ESTAT_API_KEY", "")
        codes = {s["code"] for s in mh.expected_series()}
        assert "HY_OAS" not in codes and "JP_CPI_CORE" not in codes

        monkeypatch.setattr(cp, "FRED_API_KEY", "dummy")
        monkeypatch.setattr(cp, "ESTAT_API_KEY", "dummy")
        codes = {s["code"] for s in mh.expected_series()}
        assert "HY_OAS" in codes and "JP_CPI_CORE" in codes

    def test_report_is_ascii_safe(self, db):
        """Windows cp932 リダイレクトで落ちない記号だけを使う。"""
        _seed_all_fresh(db)
        db.query(MacroData).filter(MacroData.series_code == "USDJPY").delete()
        _seed(db, "USDJPY", AS_OF - timedelta(days=30))
        report = "\n".join(mh.format_report(mh.check_macro_freshness(db, as_of=AS_OF)))
        report.encode("cp932")   # 例外が出ないこと自体が検証対象


# ── ワークフロー ──────────────────────────────────────────────────────────

class TestWorkflow:
    def test_workflows_filter_is_wildcard(self, workflow):
        """`workflows:` の列挙は "[定常] …" の角括弧で startup_failure になる（#414）。"""
        assert _triggers(workflow)["workflow_run"]["workflows"] == ["**"]

    def test_chained_on_collection_workflow_success(self, workflow):
        cond = workflow["jobs"]["check"]["if"]
        daily_name = _load(DAILY)["name"]
        full_name = _load(FULL)["name"]
        assert daily_name in cond, "daily-incremental の name とズレている"
        assert full_name in cond, "full-pipeline の name とズレている"
        assert "conclusion == 'success'" in cond

    def test_tee_step_sets_pipefail(self, workflow):
        for step in workflow["jobs"]["check"]["steps"]:
            run = step.get("run", "")
            if "tee" in run:
                assert "set -o pipefail" in run, step.get("name")

    def test_passes_same_api_keys_as_collection(self, workflow):
        """キーの有無で判定対象が変わるため、収集ジョブと同じキーを渡す。"""
        env = next(s for s in workflow["jobs"]["check"]["steps"]
                   if "check_macro_health" in s.get("run", ""))["env"]
        assert {"DATABASE_URL", "FRED_API_KEY", "ESTAT_API_KEY"} <= set(env)

    def test_collection_pipelines_do_not_exit_on_macro_health(self):
        """収集本体は健全性で非ゼロ終了しない（sector_ols 夜間更新の巻き添え防止・#425/#432）。"""
        root = Path(__file__).resolve().parents[1]
        for name in ("_pipeline_incremental.py", "_pipeline_gh.py"):
            src = (root / name).read_text(encoding="utf-8")
            assert "format_report(check_macro_freshness" in src, name
            assert "n_critical_bad" not in src, f"{name} が終了コードを判定している"
