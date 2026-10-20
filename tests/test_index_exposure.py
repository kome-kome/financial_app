"""露出τの道具（scripts/index_exposure.py・#1006）のテスト。

純関数は合成の点数と群で手計算と突き合わせる。CLI は台帳と DB の読み出しを差し替え、共通の
`db` fixture（in-memory SQLite）だけを使う（本物の DB・JPX の一覧には触れない）。
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import index_events as IE  # noqa: E402
from scripts import index_exposure as X  # noqa: E402

# 10 社。点数は E01=10 … E10=1。移行措置は上位 2 社と最下位 1 社、新規採用は 5 位の 1 社。
SCORES = {f"E{i:02d}": float(11 - i) for i in range(1, 11)}
GROUPS = {"E01": "transition", "E02": "transition", "E10": "transition", "E05": "new_add",
          "E03": "other", "E04": "other"}


class TestExposureTable:
    def test_matches_hand_calculation(self):
        t = X.exposure_table(SCORES, GROUPS)
        assert (t["n"], t["k"]) == (10, 2)
        tr = t["groups"]["transition"]
        assert (tr["n"], tr["base_rate"], tr["top_n"], tr["top_share"]) == (3, 0.3, 2, 1.0)
        assert (tr["bottom_n"], tr["bottom_share"]) == (1, 0.5)
        assert tr["top_lift"] == pytest.approx(1.0 / 0.3)
        assert tr["mean_rank_pct"] == pytest.approx((0.95 + 0.85 + 0.05) / 3)
        na = t["groups"]["new_add"]
        assert (na["n"], na["top_n"], na["bottom_n"]) == (1, 0, 0)
        assert na["mean_rank_pct"] == pytest.approx(0.55)

    def test_groups_sum_to_the_population_and_missing_is_unknown(self):
        t = X.exposure_table(SCORES, GROUPS)
        assert sum(g["n"] for g in t["groups"].values()) == t["n"]
        assert t["groups"]["unknown"]["n"] == 4          # 群の無い 4 社

    def test_missing_scores_are_left_out(self):
        scores = {**SCORES, "E11": None, "E12": float("nan")}
        assert X.exposure_table(scores, GROUPS)["n"] == 10

    def test_uniform_rank_averages_one_half(self):
        t = X.exposure_table(SCORES, {ec: "other" for ec in SCORES})
        assert t["groups"]["other"]["mean_rank_pct"] == pytest.approx(0.5)

    def test_empty(self):
        t = X.exposure_table({}, {})
        assert (t["n"], t["k"]) == (0, 0)
        assert t["groups"]["transition"]["top_share"] is None


def test_overlap_counts():
    members = {"transition": frozenset({"E01", "E02", "E99"}), "new_add": frozenset({"E05"})}
    got = X.overlap_counts(members, {"M-6": {"E01", "E05"}, "tradable": {"E01", "E02", "E05"}})
    assert got == {"transition": {"total": 3, "M-6": 1, "tradable": 2},
                   "new_add": {"total": 1, "M-6": 1, "tradable": 1}}


class TestMain:
    @pytest.fixture
    def wired(self, db, monkeypatch):
        """DB の読み出しと台帳を差し替える。台帳は transition=E01/E02/E10・new_add=E05。"""
        import database

        ledger = IE.IndexLedger(
            ok=True,
            mentions={"E01": (("topix-2026-10", "transition"),), "E02": (("topix-2026-10", "transition"),),
                      "E10": (("topix-2026-10", "transition"),), "E05": (("topix-2026-10", "new_add"),)},
            groups={("topix-2026-10", "transition"): frozenset({"E01", "E02", "E10"}),
                    ("topix-2026-10", "new_add"): frozenset({"E05"})})
        monkeypatch.setattr(database, "SessionLocal", lambda: contextlib.nullcontext(db))
        monkeypatch.setattr(IE, "load_ledger", lambda db, root=None: ledger)
        monkeypatch.setattr(X, "model_scores", lambda db, snapshot_date=None: {"M-6": dict(SCORES)})
        monkeypatch.setattr(X, "preset_scores", lambda db: {"preset:バランス型": dict(SCORES)})
        monkeypatch.setattr(X, "tradable_codes", lambda db: set(SCORES))
        return ledger

    def test_writes_the_table(self, wired, tmp_path, capsys):
        import json

        out = tmp_path / "x.json"
        assert X.main(["--asof", "2026-11-02", "--out", str(out)]) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["overlap"]["transition"] == {"total": 3, "M-6": 3, "preset:バランス型": 3, "tradable": 3}
        tr = data["exposure"]["M-6"]["groups"]["transition"]
        assert (tr["n"], tr["top_n"], tr["bottom_n"]) == (3, 2, 1)
        assert "露出τ" in capsys.readouterr().out

    def test_before_the_announcement_everyone_is_unknown(self, wired, tmp_path):
        """群は `known_at` で決まる（先読みしない）。公表前の日付では移行措置の群が空になる。"""
        import json

        out = tmp_path / "x.json"
        assert X.main(["--asof", "2026-10-06", "--out", str(out)]) == 0
        groups = json.loads(out.read_text(encoding="utf-8"))["exposure"]["M-6"]["groups"]
        assert groups["transition"]["n"] == 0 and groups["unknown"]["n"] == 10

    def test_snapshot_mode_skips_presets(self, wired, monkeypatch, tmp_path):
        import json

        def no_presets(db):
            raise AssertionError("写しのときはプリセットを採点しない")

        monkeypatch.setattr(X, "preset_scores", no_presets)
        out = tmp_path / "x.json"
        assert X.main(["--snapshot-date", "2026-10-21", "--out", str(out)]) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["snapshot_date"] == "2026-10-21" and data["asof"] == "2026-10-21"
        assert set(data["exposure"]) == {"M-6"}

    def test_ledger_not_ok_fails(self, wired, monkeypatch):
        monkeypatch.setattr(IE, "load_ledger", lambda db, root=None: IE.IndexLedger(ok=False, reason="一覧が無い"))
        assert X.main([]) == X.EXIT_FAILED

    def test_no_overlap_fails(self, wired, monkeypatch):
        other = {f"F{i:02d}": float(i) for i in range(10)}
        monkeypatch.setattr(X, "model_scores", lambda db, snapshot_date=None: {"M-6": other})
        monkeypatch.setattr(X, "preset_scores", lambda db: {})
        monkeypatch.setattr(X, "tradable_codes", lambda db: set(other))
        assert X.main(["--asof", "2026-11-02"]) == X.EXIT_FAILED


def test_model_scores_reads_the_snapshot_table(db):
    """`--snapshot-date` は写しの表から、源ごとの値の列（μ̂ / gap）を読む。"""
    from database import PredictionSnapshot

    db.add_all([
        PredictionSnapshot(snapshot_date="2026-10-21", source="macro_enet", edinet_code="E01", mu=0.1),
        PredictionSnapshot(snapshot_date="2026-10-21", source="sector_ols", edinet_code="E01", gap_ratio=12.0),
        PredictionSnapshot(snapshot_date="2026-11-01", source="macro_enet", edinet_code="E01", mu=0.9),
    ])
    db.commit()
    got = X.model_scores(db, "2026-10-21")
    assert got["M-6"] == {"E01": 0.1} and got["gap"] == {"E01": 12.0} and got["M-2"] == {}
