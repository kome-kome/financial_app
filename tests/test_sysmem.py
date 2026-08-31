r"""sysmem（プロセス常駐メモリと物理メモリの実測）。

**測れているつもりで測れていない**を防ぐのがここの役目。2026-09-01 に実際に踏んだのは、
`venv\Scripts\python.exe` がランチャースタブなので `Popen` の pid を単体で測ると、実体が
300MB 常駐していても **4MB** と返る形だった。エラーにならず「静かに正しく見える」ので、
子の自己申告と親の観測を突き合わせる回帰をここに置く。
"""
import os
import subprocess
import sys

import pytest

import sysmem


class TestMachineMemory:
    def test_total_is_positive(self):
        total = sysmem.total_mb()
        assert total is not None and total > 0

    def test_available_is_positive_and_below_total(self):
        avail, total = sysmem.available_mb(), sysmem.total_mb()
        assert avail is not None and avail > 0
        assert avail <= total


class TestSelfMeasurement:
    def test_rss_is_positive(self):
        rss = sysmem.rss_mb()
        assert rss is not None and rss > 0

    def test_peak_is_at_least_current(self):
        rss, peak = sysmem.rss_mb(), sysmem.peak_rss_mb()
        assert rss is not None and peak is not None
        assert peak >= rss

    def test_snapshot_has_every_key(self):
        snap = sysmem.snapshot()
        assert set(snap) == {"rss_mb", "peak_rss_mb", "avail_mb", "total_mb"}


class TestProcessTree:
    def test_tree_contains_the_root(self):
        assert os.getpid() in sysmem.process_tree(os.getpid())

    def test_unknown_pid_degrades_to_itself(self):
        """存在しない pid でも例外を投げない（計測の失敗が本業を止めない）。"""
        assert sysmem.process_tree(2 ** 31 - 1) == [2 ** 31 - 1]

    def test_unknown_pid_measures_none(self):
        """測れないことを 0.0 ではなく None で表す（「食っていない」と混ぜない）。"""
        assert sysmem.rss_mb(2 ** 31 - 1) is None

    def test_tree_rss_reports_none_when_nothing_measurable(self):
        total, counted = sysmem.tree_rss_mb(2 ** 31 - 1)
        assert total is None and counted == 0


class TestChildMeasurement:
    """**この回帰が本体**——親から見た子ツリーの常駐量が、子の自己申告と一致すること。"""

    def test_tree_total_matches_child_self_report(self, tmp_path):
        marker = tmp_path / "child_rss.txt"
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import sysmem\n"
            "buf = bytearray(200 * 1024 * 1024)\n"
            "buf[::4096] = b'\x01' * (len(buf) // 4096)\n"
            "open(%r, 'w').write(str(sysmem.rss_mb()))\n"
            "import time; time.sleep(30)\n"
        ) % (os.getcwd(), str(marker))
        proc = subprocess.Popen([sys.executable, "-c", code])
        try:
            deadline = 60
            while deadline and not marker.exists():
                import time as _t
                _t.sleep(0.5)
                deadline -= 1
            if not marker.exists():
                pytest.skip("子プロセスが自己申告を書けなかった（環境依存）")
            child_says = float(marker.read_text())
            parent_sees, counted = sysmem.tree_rss_mb(proc.pid)
            assert parent_sees is not None and counted >= 1
            # スタブだけを測ると桁で外れる（実測 4MB 対 313MB）。**下限を子の自己申告に取る**
            # ——ツリーには親スタブぶんが乗るので上振れは正常、下振れだけが症状。
            assert parent_sees >= child_says * 0.9, (
                f"親が見た合計 {parent_sees:.0f}MB が子の自己申告 {child_says:.0f}MB を"
                "大きく下回る（ランチャースタブだけを測っている疑い）"
            )
        finally:
            proc.kill()
            proc.wait()


class TestFormatting:
    def test_line_mentions_both_usage_and_headroom(self):
        line = sysmem.format_line()
        assert "rss=" in line and "avail=" in line

    def test_unmeasurable_values_show_as_question_mark(self, monkeypatch):
        """欠測を黙って消さない（`?` として出す）。"""
        monkeypatch.setattr(sysmem, "rss_mb", lambda *a, **k: None)
        monkeypatch.setattr(sysmem, "available_mb", lambda: None)
        monkeypatch.setattr(sysmem, "total_mb", lambda: None)
        assert "?" in sysmem.format_line()
