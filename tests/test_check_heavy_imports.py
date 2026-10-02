"""check_heavy_imports のプロファイル（バッチが使う依存だけを確かめる・#789）。

2026-10-03 の月次 M-1 は、M-1 が一度も import しない jaxlib の部品（`cpu/_sparse.pyd`）を
Smart App Control が遮断しただけで `deps_smoke` が exit=1 になった。守る境界は2つ:
**base は推論系に触れない**ことと、**既定は推論系まで確かめる**こと（指定を忘れたバッチが
macro_beta 直前の確認を失わない）。
"""
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
