"""heavy プラグイン実行の進捗（plugins/progress.py・Issue #545）の単体テスト。

縛るのは3つ:
  1. **カバレッジ表と実体の照合**（`TestProgressCoverageRegistry`）——「heavy を足したが
     進捗が無い」は画面が沈黙するだけで例外もログも出ず、実行時には失敗として現れない
     （ADR-0031 の `HEAVY_AUTOMATION` / #515 の `WATCHED` と同型）。
  2. **sink 未設定で完全な no-op**——月次バッチ（`scripts/run_monthly*.py`）や
     `/api/recommend` 経路が進捗機構に触られないことを構造的に担保する。
  3. **開始待ちストリーム**——heavy の POST は完了まで返らないため画面は応答を待たずに
     SSE を開く。素の `_sse_stream` は running=False で即切れるので、待ち合わせが無いと
     進捗が1件も届かないまま「沈黙」へ戻る。
"""
import asyncio
import inspect
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# import 時の APP_SECRET_KEY 未設定警告を避けるため、import 前にダミーを設定
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")

import api  # noqa: E402,F401  （routers.analysis を単独 import すると循環になる）
import plugins as plugin_registry  # noqa: E402
import routers.analysis as analysis  # noqa: E402
from collection_jobs import JobState, _sse_stream_awaiting_start  # noqa: E402
from plugins import progress  # noqa: E402

# "common" を名乗るプラグインが通っていなければならない共通骨格の関数名。
# （macro_snapshots のロード/構築段。M-3 は build_snapshots を通らないが
#   load_weekly_prices_chunked を共有する。）
_COMMON_MARKERS = (
    "build_snapshots",
    "load_data",
    "load_weekly_prices_chunked",
    "load_prices",
)


def _heavy_plugins() -> list:
    return [p for p in plugin_registry.list_plugins() if getattr(p, "heavy", False)]


def _sources_of(plugin) -> str:
    """プラグインクラスの MRO を辿って定義モジュールのソースを連結して返す。

    継承で execute を借りているプラグイン（macro_gbdt_rank → macro_gbdt）は自分の
    モジュールに共通骨格の呼び出しが現れないため、**自分のソースだけを見ると誤って
    落ちる**。
    """
    texts = []
    for klass in type(plugin).__mro__:
        module = sys.modules.get(klass.__module__)
        if module is None or not getattr(module, "__file__", None):
            continue
        try:
            texts.append(inspect.getsource(module))
        except OSError:      # pragma: no cover - ソースが無い環境向けの保険
            continue
    return "\n".join(texts)


class TestProgressCoverageRegistry:
    """PROGRESS_COVERAGE（表）と heavy プラグイン（実体）を照合する。"""

    def test_every_heavy_plugin_is_registered(self):
        missing = [p.name for p in _heavy_plugins()
                   if p.name not in progress.PROGRESS_COVERAGE]
        assert not missing, (
            f"heavy=True なのに進捗カバレッジ表に無いプラグイン: {missing}。"
            "plugins/progress.py の PROGRESS_COVERAGE へ "
            "'common' / 'own' / 'exempt: <理由>' を追加してください（Issue #545）"
        )

    def test_registry_has_no_phantom_entries(self):
        known = {p.name for p in plugin_registry.list_plugins()}
        phantom = [n for n in progress.PROGRESS_COVERAGE if n not in known]
        assert not phantom, f"実在しないプラグインが表に残っています: {phantom}"

    def test_registered_plugins_are_still_heavy(self):
        heavy = {p.name for p in _heavy_plugins()}
        stale = [n for n in progress.PROGRESS_COVERAGE if n not in heavy]
        assert not stale, (
            f"heavy=False になったのに表へ残っているプラグイン: {stale}"
        )

    def test_values_use_known_vocabulary(self):
        for name, value in progress.PROGRESS_COVERAGE.items():
            assert value in ("common", "own") or value.startswith("exempt:"), (
                f"{name}: 未知の値 '{value}'（'common' / 'own' / 'exempt: <理由>'）"
            )

    def test_exempt_requires_a_reason(self):
        for name, value in progress.PROGRESS_COVERAGE.items():
            if value.startswith("exempt:"):
                assert value[len("exempt:"):].strip(), (
                    f"{name}: exempt には理由が必要です（空理由は棚上げと区別できない）"
                )

    def test_common_plugins_go_through_the_shared_pipeline(self):
        for plugin in _heavy_plugins():
            if progress.PROGRESS_COVERAGE.get(plugin.name) != "common":
                continue
            src = _sources_of(plugin)
            assert any(marker in src for marker in _COMMON_MARKERS), (
                f"{plugin.name} は 'common' だが macro_snapshots の共通骨格"
                f"（{'/'.join(_COMMON_MARKERS)}）を参照していません。"
                "自前で進捗を出すなら 'own' へ変更してください"
            )

    def test_own_plugins_emit_progress_themselves(self):
        for plugin in _heavy_plugins():
            if progress.PROGRESS_COVERAGE.get(plugin.name) != "own":
                continue
            src = _sources_of(plugin)
            assert "progress.emit(" in src, (
                f"{plugin.name} は 'own' だが progress.emit を呼んでいません"
            )


class TestEmit:
    def test_noop_without_sink(self):
        """sink 未設定でも例外にならない（バッチ経路の非破壊）。"""
        progress.emit("なにか", 1, 10)
        assert progress.active() is False

    def test_emits_to_sink(self):
        seen = []
        with progress.progress_sink(lambda s, c, t: seen.append((s, c, t))):
            assert progress.active() is True
            progress.emit("ロード", 3, 10)
        assert seen == [("ロード", 3, 10)]

    def test_sink_is_restored_after_context(self):
        with progress.progress_sink(lambda s, c, t: None):
            pass
        assert progress.active() is False

    def test_nested_sinks_do_not_leak(self):
        outer, inner = [], []
        with progress.progress_sink(lambda s, c, t: outer.append(s)):
            with progress.progress_sink(lambda s, c, t: inner.append(s)):
                progress.emit("内側")
            progress.emit("外側")
        assert inner == ["内側"] and outer == ["外側"]

    def test_every_thins_out_intermediate_calls(self):
        seen = []
        with progress.progress_sink(lambda s, c, t: seen.append(c)):
            for i in range(10):
                progress.emit("構築", i, 10, every=5)
        assert seen == [0, 5]

    def test_every_always_keeps_first_and_last(self):
        """終端を間引くと『4300/4400 のまま完了』に見えて止まったのか終わったのか分からない。"""
        seen = []
        with progress.progress_sink(lambda s, c, t: seen.append(c)):
            progress.emit("構築", 0, 7, every=100)
            progress.emit("構築", 3, 7, every=100)
            progress.emit("構築", 7, 7, every=100)
        assert seen == [0, 7]

    def test_sink_receives_step_without_counts(self):
        seen = []
        with progress.progress_sink(lambda s, c, t: seen.append((s, c, t))):
            progress.emit("キャッシュから復元")
        assert seen == [("キャッシュから復元", 0, 0)]


class TestSharedPipelineEmits:
    """共通骨格が実際に進捗を出すこと（表の 'common' が空手形でないことの裏取り）。"""

    def test_build_snapshots_emits_company_progress(self):
        from plugins.macro_snapshots import _build_snapshots_impl

        seen = []
        with progress.progress_sink(lambda s, c, t: seen.append((s, c, t))):
            _build_snapshots_impl(
                prices_by_co={}, fin_by_co={}, companies={}, macro_cache={},
                fin_features=[], macro_names=[], use_momentum=False, mom_window=12,
                min_coverage=0.0, build_interactions=False, macro_nan_ok=False,
                return_stock_ids=False, price_features=[],
            )
        assert any("スナップショット" in step for step, _c, _t in seen)


class TestExecuteWithProgress:
    """routers.analysis が JobState へ配線されていること。"""

    @staticmethod
    def _fake_plugin(execute):
        class _P:
            name = "fake_heavy"
            label = "偽の重い分析"
            heavy = True
            depends_on: list = []

            def params_schema(self):
                return {}

        p = _P()
        p.execute = execute
        return p

    def test_progress_reaches_job_state(self):


        def execute(params, db):
            progress.emit("構築", 2, 4)
            return {"ok": True}

        p = self._fake_plugin(execute)
        result = asyncio.run(analysis._execute_with_progress(p, p.name, {}, None))

        st = analysis.jobs.state(analysis._progress_job(p.name))
        assert result == {"ok": True}
        assert st.running is False
        assert st.progress == 2 and st.total == 4
        assert any("構築 2/4" == line for line in st.log)

    def test_failure_is_left_in_the_log_before_closing(self):
        """黙って落ちるとストリームが切れただけになり『終わった』と区別できない。"""


        def execute(params, db):
            raise ValueError("説明変数を1つ以上選択してください")

        p = self._fake_plugin(execute)
        with pytest.raises(ValueError):
            asyncio.run(analysis._execute_with_progress(p, p.name, {}, None))

        st = analysis.jobs.state(analysis._progress_job(p.name))
        assert st.running is False
        assert st.log[-1].startswith("[エラー]")

    def test_sink_is_not_installed_for_light_plugins(self):
        """軽いプラグインは包まない＝ contextvar が漏れていないこと。"""
        assert progress.active() is False


ROOT = Path(__file__).resolve().parent.parent


def _plugin_tab_map() -> dict:
    """analysis.js の PLUGIN_TAB_MAP（プラグイン名 → 静的タブ id）を読む。"""
    src = (ROOT / "static" / "js" / "analysis.js").read_text(encoding="utf-8")
    block = re.search(r"const PLUGIN_TAB_MAP = \{(.*?)\n\};", src, re.S)
    assert block, "PLUGIN_TAB_MAP の定義が見つかりません"
    return dict(re.findall(r"'([^']+)':\s*'([^']+)'", block.group(1)))


class TestProgressDomIsWiredForEveryHeavyTab:
    """進捗の受け皿（DOM）が heavy の全タブに在ること。

    分析画面のタブは2系統ある——`PLUGIN_TAB_MAP` に載る**静的タブ**（analysis.html に
    手書き）と、載っていないプラグインへ `_createDynamicTab` が生成する**動的タブ**。
    実行ボタンはどちらも `runDynamicPlugin` を通るため、**静的タブ側に DOM を貼り忘れても
    JS は静かに進捗を諦める**（`_startPluginProgress` が要素を見つけられず null を返す）＝
    エラーもログも出ず、症状は「進捗が出ない」＝直す前と同じ沈黙。gnav の貼り忘れ
    （`tests/test_templates_nav.py`）と同型なので、同じくテストで縛る。
    """

    def test_static_tabs_have_a_progress_box(self):
        html = (ROOT / "templates" / "analysis.html").read_text(encoding="utf-8")
        tab_map = _plugin_tab_map()
        missing = []
        for plugin in _heavy_plugins():
            tab_id = tab_map.get(plugin.name)
            if tab_id is None:
                continue        # 動的タブ側は _createDynamicTab が生成する
            if f'id="dynprogress-{tab_id}"' not in html:
                missing.append(f"{plugin.name}（#tab-{tab_id}）")
        assert not missing, (
            f"静的タブに進捗ボックスがない heavy プラグイン: {missing}。"
            "templates/analysis.html の実行ボタン直後へ dynprogress-{tabId} 一式を"
            "追加してください（Issue #545）"
        )

    def test_dynamic_tabs_generate_a_progress_box(self):
        js = (ROOT / "static" / "js" / "analysis.js").read_text(encoding="utf-8")
        assert "dynprogress-${esc(tabId)}" in js, (
            "_createDynamicTab が進捗ボックスを生成しなくなっています"
        )


class TestStreamAwaitingStart:
    def test_waits_for_start_then_streams(self):
        st = JobState()
        frames = []

        async def scenario():
            async def consume():
                async for chunk in _sse_stream_awaiting_start(st, grace=5.0):
                    frames.append(chunk)

            task = asyncio.ensure_future(consume())
            await asyncio.sleep(0.5)
            assert not frames, "開始前に配信して閉じてはいけない"
            st.reset_for_run()
            st.append_log("開始しました")
            await asyncio.sleep(0.1)
            st.running = False
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(scenario())
        assert frames, "開始後は配信されること"
        assert "開始しました" in frames[0]

    def test_gives_up_after_grace(self):
        """開始しないまま待ち続けない（実行が弾かれたときストリームだけ生き残る）。"""
        st = JobState()
        frames = []

        async def consume():
            async for chunk in _sse_stream_awaiting_start(st, grace=0.4):
                frames.append(chunk)

        asyncio.run(asyncio.wait_for(consume(), timeout=5))
        assert len(frames) == 1
        assert '"running": false' in frames[0]
