"""scripts/nightly_diag_report.py の不変条件（Issue #726・ADR-0061）。

守るもの:
  1. timeline は追っている値が動いた夜だけを出し、同時に何が変わっていたかを添える
     （code / code? / preprocess / data / no-context-change）
  2. edges は α が候補の端に張り付いた夜だけを出す
  3. 出力は Windows cp932 のリダイレクトで落ちない（全角文字以外の非 ASCII 記号を使わない）
"""
import unicodedata

import pytest

from scripts import nightly_diag_report as rep

SHA_A = "a" * 40
SHA_B = "b" * 40


def _ols_row(run_id, alphas, *, code=SHA_A, snap=None, n_total=100, pp="winsor_z_v1"):
    sectors = [{"industry": ind, "n": 10, "alpha": a,
                "alpha_edge": ("low" if a == 0.001 else "high" if a == 1000.0 else None)}
               for ind, a in alphas.items()]
    return {
        "run_id": run_id, "model": "sector_ols", "snapshot_date": snap,
        "code_version": code, "preprocess_version": pp, "created_at": run_id,
        "diagnostics": {
            "n_total": n_total, "n_sectors": len(sectors),
            "n_alpha_at_low_edge": sum(1 for s in sectors if s["alpha_edge"] == "low"),
            "n_alpha_at_high_edge": sum(1 for s in sectors if s["alpha_edge"] == "high"),
            "sectors": sectors,
        },
    }


def _enet_row(run_id, *, rank_ic=0.16, alpha=0.05, at_min=False, code=SHA_A,
              snap="2026-09-18", n_train=90000, cv_min=0.0):
    return {
        "run_id": run_id, "model": "macro_enet", "snapshot_date": snap,
        "code_version": code, "preprocess_version": "winsor_z_v1", "created_at": run_id,
        "diagnostics": {
            "n_train_samples": n_train,
            "final_model": {"alpha": alpha, "l1_ratio": 0.5, "n_nonzero": 40,
                            "alpha_at_path_min": at_min, "alpha_at_path_max": False,
                            "l1_ratio_at_grid_edge": False, "l1_ratio_grid": [0.1, 0.5, 0.9]},
            "cv_diagnostics": {"alpha_at_path_min": cv_min, "alpha_at_path_max": 0.0},
            "oof": {"rank_ic": {"mean": rank_ic}, "short_side_spread": 0.07},
        },
    }


class TestTimeline:
    def test_unchanged_nights_are_skipped(self):
        rows = [_ols_row("n1", {"機械": 0.001}), _ols_row("n2", {"機械": 0.001}),
                _ols_row("n3", {"機械": 10.0})]
        items = rep.timeline(rows)
        assert [i["run_id"] for i in items] == ["n1", "n3"]
        assert items[0]["changed"] == ["(baseline)"]
        assert "alpha_by_sector" in items[1]["changed"]

    def test_moved_without_context_change_is_flagged(self):
        """件数も断面日もコードも同じなのに動いた夜＝#697 型の候補として出す。"""
        rows = [_ols_row("n1", {"機械": 0.001}), _ols_row("n2", {"機械": 10.0})]
        assert rep.timeline(rows)[1]["causes"] == ["no-context-change"]

    def test_code_change_is_attributed(self):
        rows = [_ols_row("n1", {"機械": 0.001}), _ols_row("n2", {"機械": 10.0}, code=SHA_B)]
        assert rep.timeline(rows)[1]["causes"] == ["code"]

    @pytest.mark.parametrize("code", ["unknown", SHA_A + "+dirty"])
    def test_undecidable_code_is_not_called_unchanged(self, code):
        """unknown や未コミット変更ありは「コードは同じ」と言えない（code? を付ける）。"""
        rows = [_ols_row("n1", {"機械": 0.001}, code=code),
                _ols_row("n2", {"機械": 10.0}, code=code)]
        assert rep.timeline(rows)[1]["causes"] == ["code?"]

    def test_data_and_preprocess_changes_are_attributed(self):
        rows = [_ols_row("n1", {"機械": 0.001}),
                _ols_row("n2", {"機械": 10.0}, n_total=101, pp="winsor_z_v2")]
        assert rep.timeline(rows)[1]["causes"] == ["preprocess", "data"]

    def test_models_are_compared_with_their_own_previous_night(self):
        rows = [_ols_row("n1", {"機械": 0.001}), _enet_row("n1"),
                _ols_row("n2", {"機械": 0.001}), _enet_row("n2", rank_ic=0.17, snap="2026-09-25")]
        items = rep.timeline(rows)
        moved = [(i["run_id"], i["model"]) for i in items if i["changed"] != ["(baseline)"]]
        assert moved == [("n2", "macro_enet")]
        assert items[-1]["changed"] == ["rank_ic"]
        assert items[-1]["causes"] == ["data"]

    def test_json_text_is_accepted(self):
        """JSON 列が文字列で返る経路（SQLite・写し）でも読める。"""
        import json

        row = _ols_row("n1", {"機械": 0.001})
        row["diagnostics"] = json.dumps(row["diagnostics"])
        assert rep.timeline([row])[0]["values"]["n_alpha_at_low_edge"] == 1


class TestEdges:
    def test_only_nights_with_edges_are_listed(self):
        rows = [_ols_row("n1", {"機械": 0.001, "卸売業": 100.0}), _ols_row("n2", {"機械": 10.0}),
                _enet_row("n2", at_min=True, cv_min=0.25)]
        items = rep.edges(rows)
        assert [(i["run_id"], i["model"]) for i in items] == [("n1", "sector_ols"),
                                                               ("n2", "macro_enet")]
        assert items[0]["hits"] == ["機械 alpha=0.001 low (n=10)"]
        assert any("path min" in h for h in items[1]["hits"])
        assert any("25%" in h for h in items[1]["hits"])


class TestSectors:
    def test_only_changes_are_kept(self):
        rows = [_ols_row("n1", {"機械": 0.001}), _ols_row("n2", {"機械": 0.001}),
                _ols_row("n3", {"機械": 10.0})]
        assert rep.sector_alpha_changes(rows) == {"機械": [("n1", 0.001), ("n3", 10.0)]}


class TestOutputEncoding:
    @staticmethod
    def _assert_console_safe(text: str) -> None:
        text.encode("cp932")
        odd = {c for c in text if ord(c) > 127
               and unicodedata.east_asian_width(c) not in ("W", "F")}
        assert not odd, f"全角文字以外の非 ASCII 記号が出ている: {sorted(odd)}"

    @pytest.mark.parametrize("view", rep.VIEWS)
    def test_main_output_is_console_safe(self, view, monkeypatch, capsys):
        rows = [_ols_row("n1", {"機械": 0.001}), _enet_row("n1", at_min=True, cv_min=0.5),
                _ols_row("n2", {"機械": 10.0}, code=SHA_B), _enet_row("n2", rank_ic=0.2)]
        monkeypatch.setattr(rep, "fetch_rows", lambda model=None, since=None: rows)
        assert rep.main(["--view", view]) == 0
        out = capsys.readouterr().out
        assert out.strip()
        self._assert_console_safe(out)

    @pytest.mark.parametrize("view", rep.VIEWS)
    def test_empty_table_is_reported_not_crashed(self, view, monkeypatch, capsys):
        monkeypatch.setattr(rep, "fetch_rows", lambda model=None, since=None: [])
        assert rep.main(["--view", view]) == 0
        self._assert_console_safe(capsys.readouterr().out)

    def test_connection_string_is_never_printed(self, monkeypatch, capsys):
        """接続先は「ローカル／本番」の別だけを出す（URL・ホスト・ユーザー名を出さない）。"""
        monkeypatch.setattr(rep, "fetch_rows", lambda model=None, since=None: [])
        rep.main([])
        out = capsys.readouterr().out
        assert "://" not in out and "postgresql" not in out and "sqlite" not in out
        assert out.startswith("接続先=")
