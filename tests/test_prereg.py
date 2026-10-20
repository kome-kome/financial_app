"""事前登録文書の凍結と照合（#1007・`scripts/prereg.py`）のテスト。

後半の 2 クラスは本物の文書 `docs/prereg/` を読む:
- `TestFrozenDocuments`: 本文の sha256 が追記節に記録した凍結値と一致する＝本文を変えた PR は CI で落ちる
- `TestBoundaryTable`: 文書に凍結した O'Brien-Fleming 型の境界表が `model_stats` の出力と一致する
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import model_stats as ms
from scripts import prereg

ROOT = Path(__file__).resolve().parents[1]
PREREG_DIR = ROOT / "docs" / "prereg"
INDEX_EVENT_DOC = PREREG_DIR / "index-event-2026.md"

ISSUE = "https://github.com/kome-kome/financial_app/issues/1007"
BODY = (
    "# 事前登録: テスト\n"
    "\n"
    f"- 事前登録の Issue: {ISSUE}\n"
    "\n"
    "## 判定規則\n"
    "- R1: p < 0.05\n"
)
APPENDIX = "\n## 追記節\n\n### stage A\n未記入\n"


def _doc(body: str = BODY, appendix: str = APPENDIX) -> str:
    return body + "\n" + prereg.APPEND_MARKER + "\n" + appendix


def _write(tmp_path: Path, text: str, name: str = "doc.md") -> Path:
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return p


@pytest.fixture(autouse=True)
def _fixed_head_sha(monkeypatch):
    # git を呼ばない（作業ツリーの状態でテストの結果が変わらないように）
    monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)


class TestBodyHash:
    def test_appendix_changes_do_not_move_the_hash(self):
        assert prereg.body_sha256(_doc()) == prereg.body_sha256(_doc(appendix=APPENDIX + "σ = 0.012\n"))

    def test_one_character_in_the_body_moves_the_hash(self):
        assert prereg.body_sha256(_doc()) != prereg.body_sha256(_doc(body=BODY.replace("0.05", "0.06")))

    def test_line_endings_and_bom_do_not_move_the_hash(self):
        lf = _doc()
        crlf = "﻿" + lf.replace("\n", "\r\n")
        assert prereg.body_sha256(lf) == prereg.body_sha256(crlf)

    def test_hash_covers_exactly_the_text_above_the_marker(self):
        import hashlib
        assert prereg.body_sha256(_doc()) == hashlib.sha256(BODY.encode("utf-8")).hexdigest()

    @pytest.mark.parametrize("text", [
        BODY,                                                  # マーカーが無い
        _doc() + prereg.APPEND_MARKER + "\n",                  # 2 行ある
    ])
    def test_marker_must_appear_exactly_once(self, text):
        with pytest.raises(prereg.PreregError):
            prereg.body_sha256(text)

    def test_marker_inside_a_line_does_not_count(self):
        text = BODY + f"文中の {prereg.APPEND_MARKER} は区切りではない\n"
        with pytest.raises(prereg.PreregError):
            prereg.body_sha256(text)


class TestVerify:
    def test_match_returns_the_stamp(self, tmp_path):
        p = _write(tmp_path, _doc())
        sha = prereg.body_sha256(_doc())
        stamp = prereg.verify(p, sha)
        assert stamp == {
            "prereg_path": p.resolve().as_posix(),
            "prereg_sha256": sha,
            "prereg_issue": ISSUE,
            "head_sha": "a" * 40,
        }

    def test_uppercase_hex_is_accepted(self, tmp_path):
        p = _write(tmp_path, _doc())
        assert prereg.verify(p, prereg.body_sha256(_doc()).upper())["prereg_sha256"]

    def test_body_edit_after_freeze_is_rejected(self, tmp_path):
        frozen = prereg.body_sha256(_doc())
        p = _write(tmp_path, _doc(body=BODY.replace("R1", "R1'")))
        with pytest.raises(prereg.PreregError, match="一致しない"):
            prereg.verify(p, frozen)

    def test_appendix_edit_after_freeze_is_accepted(self, tmp_path):
        frozen = prereg.body_sha256(_doc())
        p = _write(tmp_path, _doc(appendix=APPENDIX + "MDE = 0.031\n"))
        assert prereg.verify(p, frozen)["prereg_sha256"] == frozen

    @pytest.mark.parametrize("sha", ["", "abc", "g" * 64, "a" * 63])
    def test_malformed_sha_is_rejected(self, tmp_path, sha):
        p = _write(tmp_path, _doc())
        with pytest.raises(prereg.PreregError, match="64"):
            prereg.verify(p, sha)

    def test_missing_document_is_rejected(self, tmp_path):
        with pytest.raises(prereg.PreregError, match="無い"):
            prereg.verify(tmp_path / "nope.md", "a" * 64)

    def test_missing_issue_line_is_rejected(self, tmp_path):
        body = BODY.replace(f"- 事前登録の Issue: {ISSUE}\n", "")
        p = _write(tmp_path, _doc(body=body))
        with pytest.raises(prereg.PreregError, match="Issue"):
            prereg.verify(p, prereg.body_sha256(_doc(body=body)))


class TestToolArguments:
    """計測道具が使う形: 引数の欠落・不一致は終了コード 2 で止まる。"""

    def _parser(self):
        ap = argparse.ArgumentParser()
        prereg.add_arguments(ap)
        return ap

    def test_both_arguments_are_required(self):
        with pytest.raises(SystemExit) as e:
            self._parser().parse_args([])
        assert e.value.code == 2
        with pytest.raises(SystemExit) as e:
            self._parser().parse_args(["--prereg", "x.md"])
        assert e.value.code == 2

    def test_mismatch_stops_with_exit_code_2(self, tmp_path, capsys):
        p = _write(tmp_path, _doc())
        args = self._parser().parse_args(["--prereg", str(p), "--prereg-sha", "0" * 64])
        with pytest.raises(SystemExit) as e:
            prereg.stamp_from_args(args)
        assert e.value.code == prereg.EXIT_MISMATCH == 2
        assert "一致しない" in capsys.readouterr().err

    def test_match_returns_the_stamp(self, tmp_path):
        p = _write(tmp_path, _doc())
        sha = prereg.body_sha256(_doc())
        args = self._parser().parse_args(["--prereg", str(p), "--prereg-sha", sha])
        assert prereg.stamp_from_args(args)["prereg_sha256"] == sha


class TestCli:
    def test_prints_the_body_hash(self, tmp_path, capsys):
        p = _write(tmp_path, _doc())
        assert prereg.main([str(p)]) == 0
        assert prereg.body_sha256(_doc()) in capsys.readouterr().out

    def test_check_exit_codes(self, tmp_path):
        p = _write(tmp_path, _doc())
        assert prereg.main([str(p), "--check", prereg.body_sha256(_doc())]) == 0
        assert prereg.main([str(p), "--check", "0" * 64]) == 2

    def test_recorded_value_that_disagrees_with_the_body_fails(self, tmp_path):
        stale = "凍結値（本文の sha256）: `" + "0" * 64 + "`\n"
        p = _write(tmp_path, _doc(appendix=APPENDIX + stale))
        assert prereg.main([str(p)]) == 2

    def test_recorded_value_must_appear_at_most_once(self, tmp_path):
        line = "凍結値（本文の sha256）: `" + prereg.body_sha256(_doc()) + "`\n"
        p = _write(tmp_path, _doc(appendix=APPENDIX + line + line))
        assert prereg.main([str(p)]) == 2


class TestFrozenDocuments:
    """凍結ガード: 本文を変えると CI で落ちる。凍結し直すなら追記節の凍結値も同じ PR で変える。"""

    DOCS = sorted(PREREG_DIR.glob("*.md"))

    def test_there_is_a_document(self):
        assert INDEX_EVENT_DOC in self.DOCS

    @pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
    def test_body_matches_the_recorded_value(self, path):
        text = path.read_text(encoding="utf-8")
        recorded = prereg.recorded_sha(text)
        assert recorded is not None, f"{path.name}: 追記節に凍結値の行が無い"
        assert prereg.body_sha256(text) == recorded, (
            f"{path.name}: 本文が凍結値と一致しない。開封前の正当な修正なら、"
            "`python -m scripts.prereg <文書>` の値で追記節の凍結値も更新する")

    @pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
    def test_issue_line_is_in_the_body(self, path):
        assert prereg.issue_url(path.read_text(encoding="utf-8")).startswith("https://github.com/")


class TestBoundaryTable:
    """文書の §6.3 の境界表＝`model_stats` の出力（表の数値を書き写さずに照合する）。"""

    ROW = re.compile(r"^\| (\d) \| (\d\.\d{3}) \| (\d\.\d{3}) \| (\d\.\d{6}) \|$", re.M)

    def test_table_matches_model_stats(self):
        rows = self.ROW.findall(INDEX_EVENT_DOC.read_text(encoding="utf-8"))
        n = 8
        assert [int(r[0]) for r in rows] == list(range(1, n + 1))
        z = ms.ld_obf_boundaries(n, 0.05)
        expected = [(f"{k / n:.3f}", f"{z[k - 1]:.3f}", f"{ms.obf_spending(k / n, 0.05):.6f}")
                    for k in range(1, n + 1)]
        assert [tuple(r[1:]) for r in rows] == expected
