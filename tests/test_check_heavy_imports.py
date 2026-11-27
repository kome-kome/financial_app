"""check_heavy_imports のプロファイル（バッチが使う依存だけを確かめる・#789）。

2026-10-03 の月次 M-1 は、M-1 が一度も import しない jaxlib の部品（`cpu/_sparse.pyd`）を
Smart App Control が遮断しただけで `deps_smoke` が exit=1 になった。守る境界は2つ:
**base は推論系に触れない**ことと、**既定は推論系まで確かめる**こと（指定を忘れたバッチが
macro_beta 直前の確認を失わない）。
"""
from pathlib import Path

import pytest

import jax_import_guard
from scripts import check_heavy_imports as chi

INFERENCE_ONLY = {"pymc", "pytensor", "arviz", "jax", "numpyro"}


def _names(profile: str) -> set[str]:
    return {name for name, _ in chi.PROFILES[profile]}


class TestProfiles:
    def test_base_never_touches_the_inference_stack(self):
        assert not _names("base") & INFERENCE_ONLY

    def test_inference_includes_base_and_the_whole_stack(self):
        assert _names("base") <= _names("inference")
        assert INFERENCE_ONLY <= _names("inference")

    def test_default_is_the_strict_side(self):
        assert chi.DEFAULT_PROFILE == "inference"


class TestMain:
    @pytest.fixture
    def probed(self, monkeypatch):
        """probe / warm_jax を記録係に差し替える（本物の import をしない）。"""
        calls: list[str] = []

        def probe(name):
            calls.append(name)
            return "ok", "1.0"

        def warm_jax():
            calls.append("jax.devices")
            return "ok", "devices=['cpu:0']"

        monkeypatch.setattr(jax_import_guard, "install", lambda: None)
        monkeypatch.setattr(jax_import_guard, "substituted", lambda: ())
        monkeypatch.setattr(chi, "probe", probe)
        monkeypatch.setattr(chi, "warm_jax", warm_jax)
        return calls

    def test_base_probes_only_the_base_and_skips_jax_devices(self, probed, capsys):
        assert chi.main(["--profile", "base"]) == 0
        assert probed == [name for name, _ in chi.BASE_IMPORTS]
        assert "jax" not in capsys.readouterr().out

    def test_no_argument_checks_everything(self, probed):
        assert chi.main() == 0
        assert probed == [name for name, _ in chi.PROFILES["inference"]] + ["jax.devices"]

    def test_base_ignores_a_blocked_jax(self, monkeypatch, probed):
        """#789 の再現: jax が遮断されていても base は通る（使わない部品で落ちない）。"""
        monkeypatch.setattr(chi, "probe",
                            lambda name: ("error", "ImportError: blocked") if name == "jax"
                            else ("ok", "1.0"))
        assert chi.main(["--profile", "base"]) == 0
        assert chi.main([]) == 1

    def test_unknown_profile_is_rejected(self, probed):
        with pytest.raises(SystemExit):
            chi.main(["--profile", "everything"])


# ── 版の照合（`--pins`・#1324）────────────────────────────────────────────────
#
# WSL2 の venv は Windows とは別に入れてある。依存の版を上げても import は通るので、照合しないと
# M-7 の計測が古い版で黙って走る。守る境界: `-r` をたどる・名前を正規化する・ずれと未導入を失敗にする・
# 固定値が取れないファイルを「ずれ 0 件」で通さない。

ROOT = Path(__file__).resolve().parents[1]


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestParsePins:
    def test_follows_nested_requirements_relative_to_the_file(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        _write(tmp_path / "base.txt", "numpy==2.4.5\n# コメント\n\nPyYAML==6.0.3  # 行末のコメント\n")
        top = _write(sub / "inference.txt", "-r ../base.txt\njax==0.10.2\n")
        pins = chi.parse_pins(top)
        assert pins == {"numpy": ("numpy", "2.4.5"), "pyyaml": ("PyYAML", "6.0.3"), "jax": ("jax", "0.10.2")}

    def test_later_lines_win_over_included_ones(self, tmp_path):
        _write(tmp_path / "base.txt", "numpy==2.4.5\n")
        top = _write(tmp_path / "top.txt", "-r base.txt\nnumpy==2.5.0\n")
        assert chi.parse_pins(top)["numpy"] == ("numpy", "2.5.0")

    def test_extras_markers_and_unpinned_lines(self, tmp_path):
        req = _write(tmp_path / "r.txt",
                     "uvicorn[standard]==0.46.0\n"
                     "colorama==0.4.6 ; sys_platform == 'win32'\n"
                     "requests>=2.0\n"
                     "-e .\n"
                     "--index-url https://example.invalid/simple\n")
        pins = chi.parse_pins(req)
        assert pins == {"uvicorn": ("uvicorn", "0.46.0"), "colorama": ("colorama", "0.4.6")}

    def test_a_cycle_of_includes_terminates(self, tmp_path):
        _write(tmp_path / "a.txt", "-r b.txt\nnumpy==1\n")
        _write(tmp_path / "b.txt", "--requirement=a.txt\nscipy==2\n")
        assert set(chi.parse_pins(tmp_path / "a.txt")) == {"numpy", "scipy"}

    def test_names_are_normalized(self):
        assert chi.normalize_name("typing_extensions") == chi.normalize_name("Typing-Extensions")
        assert chi.normalize_name("zope.interface") == "zope-interface"

    def test_the_real_inference_requirements_include_both_layers(self):
        """実物の requirements-inference.txt から、直書き（jax）と `-r` の先（numpy）の両方が取れる。"""
        pins = chi.parse_pins(ROOT / "requirements-inference.txt")
        assert "jax" in pins and "numpyro" in pins
        assert "numpy" in pins and "psycopg2-binary" in pins


class TestCheckPins:
    PINS = {"numpy": ("numpy", "2.4.5"), "jax": ("jax", "0.10.2"), "pymc": ("pymc", "5.28.5")}

    def test_matching_versions_report_nothing(self):
        installed = {"numpy": "2.4.5", "jax": "0.10.2", "pymc": "5.28.5"}
        assert chi.check_pins(self.PINS, version_of=installed.get) == []

    def test_mismatch_and_missing_are_both_reported_with_the_versions(self):
        installed = {"numpy": "2.3.0", "jax": "0.10.2"}
        problems = chi.check_pins(self.PINS, version_of=installed.get)
        assert problems == ["numpy: 固定値 2.4.5 / 実際 2.3.0", "pymc: 固定値 5.28.5 / 未導入"]


class TestMainWithPins:
    @pytest.fixture
    def quiet(self, monkeypatch):
        monkeypatch.setattr(jax_import_guard, "install", lambda: None)
        monkeypatch.setattr(jax_import_guard, "substituted", lambda: ())
        monkeypatch.setattr(chi, "probe", lambda name: ("ok", "1.0"))
        monkeypatch.setattr(chi, "warm_jax", lambda: ("ok", "devices=['cpu:0']"))

    def test_a_drifted_version_fails_even_when_every_import_works(self, quiet, monkeypatch, tmp_path, capsys):
        req = _write(tmp_path / "r.txt", "numpy==2.4.5\njax==0.10.2\n")
        monkeypatch.setattr(chi, "installed_version", {"numpy": "2.4.5", "jax": "0.9.0"}.get)
        assert chi.main(["--pins", str(req)]) == 1
        out = capsys.readouterr().out
        assert "jax: 固定値 0.10.2 / 実際 0.9.0" in out
        assert "pip install -r" in out

    def test_matching_versions_pass(self, quiet, monkeypatch, tmp_path, capsys):
        req = _write(tmp_path / "r.txt", "numpy==2.4.5\n")
        monkeypatch.setattr(chi, "installed_version", {"numpy": "2.4.5"}.get)
        assert chi.main(["--pins", str(req)]) == 0
        assert "版も固定値どおり" in capsys.readouterr().out

    def test_a_file_without_pins_is_a_failure_not_a_pass(self, quiet, tmp_path):
        """取り違えたファイルを「ずれ 0 件」で通さない（空を返すと全件失敗が隠れるのと同型）。"""
        req = _write(tmp_path / "r.txt", "# 空\nrequests>=2\n")
        assert chi.main(["--pins", str(req)]) == 1

    def test_a_missing_file_is_a_failure(self, quiet, tmp_path):
        assert chi.main(["--pins", str(tmp_path / "nope.txt")]) == 1

    def test_without_pins_versions_are_not_compared(self, quiet, monkeypatch):
        """Windows の deps_smoke と月次は `--pins` を付けない＝従来どおり import だけを見る。"""
        monkeypatch.setattr(chi, "installed_version", lambda name: pytest.fail("照合していないのに版を読んだ"))
        assert chi.main([]) == 0
