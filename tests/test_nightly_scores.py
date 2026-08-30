"""nightly_scores.py と nightly-scores.yml の不変条件ガード（Issue #432/#443・親 #423）。

守るのは2系統:

CLI（nightly_scores.py）
  1. 登録モデルが実在の producer であり、NIGHTLY_PARAMS がパラメータ契約を通ること
  2. 実行後に「DB へ本当に書かれたか」を直接クエリで確認すること
     （例外が出なかったことを永続化の証明にしない）
  3. 1モデルの失敗が他モデルを巻き込まず、最後に非ゼロ終了すること
  3b. モデル間で重い共有ロード（load_data 等）を再計算しないこと（#443・Egress）

ワークフロー（nightly-scores.yml）
  4. `workflows:` は `["**"]` 固定（列挙は "[定常] …" の角括弧で startup_failure）
  5. daily-incremental の name と `success` で絞ること（株価が前進していない日に
     スコアだけ更新しない）
  6. `tee` するステップに `set -o pipefail` があること（exit code が化ける・#352）

heavy の自動実行契約（ADR-0031・Issue #423 子6）
  7. `heavy=True` のプラグインは `HEAVY_AUTOMATION` へ必ず登録されていること。
     未登録の heavy は「実行手段が無いまま存在する」状態で、failure が出ないため
     notify-failure でも検知できない（#432/#443/#423 子5 で3回起きた）。
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nightly_scores import (  # noqa: E402
    EXEMPT_PREFIX,
    HEAVY_AUTOMATION,
    LOCAL_PREFIX,
    NIGHTLY_MODELS,
    NIGHTLY_PARAMS,
    VERIFIERS,
    VerificationError,
    _summarize,
    _verify_macro_enet,
    _verify_sector_ols,
    run_models,
)
from tests.test_sector_ols import _seed_sector  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOW_DIR / "nightly-scores.yml"
DAILY = WORKFLOW_DIR / "daily-incremental.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """`on:` は YAML 1.1 で bool True にパースされるため両方を見る。"""
    return doc.get("on", doc.get(True)) or {}


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


# ── CLI: 登録内容がパラメータ契約と producer 契約に整合しているか ──────────────

class TestRegistration:
    def test_models_exist_and_are_heavy_producers(self):
        from plugins import get_plugin

        for name in NIGHTLY_MODELS:
            plugin = get_plugin(name)
            assert plugin is not None, f"{name} は登録済みプラグインではない"
            # 夜間バッチへ載せる理由が「Render で動かせない heavy」であること（#423 の原則）
            assert plugin.heavy is True, f"{name} は heavy ではない＝夜間バッチの対象外"
            assert hasattr(plugin, "produced_output")

    def test_nightly_params_pass_the_parameter_contract(self):
        """NIGHTLY_PARAMS が params_schema に無いキーや範囲外の値を持たないこと。"""
        from plugins import get_plugin
        from plugins.utils import coerce_params

        for name, raw in NIGHTLY_PARAMS.items():
            plugin = get_plugin(name)
            assert plugin is not None
            schema = plugin.params_schema()
            for key in raw:
                assert key in schema, f"{name}: '{key}' は params_schema に存在しない"
            coerce_params(schema, dict(raw))   # 違反があれば ValueError

    def test_params_keys_are_registered_models(self):
        for name in NIGHTLY_PARAMS:
            assert name in NIGHTLY_MODELS, f"{name} は NIGHTLY_MODELS に無い"

    def test_sector_ols_uses_ridge(self):
        """既定 features 10項目は VIF>10 が頻発するため ridge 固定（本番の既存行も ridge）。"""
        assert NIGHTLY_PARAMS["sector_ols"]["regularization"] == "ridge"

    def test_default_mu_source_producer_is_registered(self):
        """`sell_ranking` の既定 mu_source を出す producer が夜間バッチに載っていること。

        既定 μ̂ の生成元が自動実行されていないと、朝の画面には「いつのものか分からない μ̂」
        が出る（#443）。既定を切り替えたら（#402 で M-2→M-6 のように）ここも追随させる。
        """
        from plugins import get_plugin

        schema = get_plugin("sell_ranking").params_schema()
        default_source = schema["mu_source"]["default"]
        assert default_source in NIGHTLY_MODELS, (
            f"sell_ranking の既定 mu_source '{default_source}' を更新する producer が "
            f"NIGHTLY_MODELS（{NIGHTLY_MODELS}）に無い"
        )

    def test_macro_enet_uses_schema_defaults(self):
        """M-6 は既定構成のまま回す（ADR-0021/0022 の実測と同一のパラメータ）。

        ここでパラメータを足すと、本番の μ̂ が「昇格ゲートで評価していない構成」で
        生成される。M-6 は tune-hyperparameters.yml の matrix にも入っていない。
        """
        assert "macro_enet" not in NIGHTLY_PARAMS

    def test_every_model_has_a_verifier(self):
        """永続化検証の無いモデルを黙って追加させない（#432 の設計上の約束）。"""
        for name in NIGHTLY_MODELS:
            assert name in VERIFIERS, f"{name} の永続化検証（VERIFIERS）が無い"


# ── heavy の自動実行契約（ADR-0031・Issue #423 子6）──────────────────────────

class TestHeavyAutomationRegistry:
    """`heavy=True` を足したのに自動実行経路が無い、を CI で落とす。

    これは3回起きている: `sector_ols`（自動経路ゼロで `gap_ratio` が33〜36日前・#432）、
    M-6 `macro_enet`（`sell_ranking` の既定 mu_source なのに tune の matrix に無く、
    ローカル手動実行が唯一の更新経路・#443）、`recommend_factor_premia`（GHA 実行履歴
    ゼロで 37期の重みのまま固着・#423 子5）。いずれも**失敗ではなく無実行**なので
    notify-failure（#414）では検知できない＝人間が気づくまで古い値が出続ける。
    """

    @staticmethod
    def _heavy_plugins() -> list[str]:
        from plugins import list_plugins

        return sorted(p.name for p in list_plugins() if p.heavy)

    def test_every_heavy_plugin_is_registered(self):
        missing = set(self._heavy_plugins()) - set(HEAVY_AUTOMATION)
        assert not missing, (
            f"heavy なのに自動実行が未登録: {sorted(missing)}。"
            "回すワークフロー名か 'exempt: <理由>' を nightly_scores.HEAVY_AUTOMATION へ"
            "書くこと（exempt でも構わないが、理由をコードに残す）"
        )

    def test_registry_has_no_stale_entries(self):
        """heavy でなくなった／削除されたプラグインの残骸を残さない。"""
        stale = set(HEAVY_AUTOMATION) - set(self._heavy_plugins())
        assert not stale, f"heavy でないのに登録されている: {sorted(stale)}"

    def test_hidden_heavy_plugins_are_exempt(self):
        """退役（hidden・ADR-0044）したモデルを自動実行し続けない。

        hidden は「選択肢として勧めない」という評価の結論なので、夜間・月次が回し続けて
        いるのに UI から消えている状態は矛盾する（消えたモデルの μ̂ だけが更新され続け、
        誰も見ないまま計算資源と Egress を食う）。逆向き（exempt なら hidden）は縛らない
        ——M-4/M-5 以前から exempt な heavy は普通にありうる。
        """
        from plugins import list_plugins

        for p in list_plugins():
            if not (p.hidden and p.heavy):
                continue
            entry = HEAVY_AUTOMATION.get(p.name, "")
            assert entry.startswith(EXEMPT_PREFIX), (
                f"{p.name} は退役（hidden）なのに自動実行が登録されている: {entry!r}。"
                "HEAVY_AUTOMATION を 'exempt: <理由>' へ変えること"
            )

    def test_workflow_entries_point_at_a_file_that_runs_the_model(self):
        """GHA ワークフロー名を書いたら、実在し**そのモデルを実際に回す**ことまで確かめる。

        存在しないファイル名や、別のモデルしか回さないワークフローを書いても通って
        しまうと、レジストリが「登録した気になる」だけの飾りになる。

        **さらに schedule が生きていることまで見る**。#503 で正本がローカルへ移り、
        月次3本の cron を止めたとき、レジストリの値は yml を指したまま残った＝
        「登録はあるが動かない」という嘘を CI が素通しした（#504）。yml を指すなら
        その cron が実際に回っていることが登録の意味である。
        """
        for name, automation in HEAVY_AUTOMATION.items():
            if automation.startswith((EXEMPT_PREFIX, LOCAL_PREFIX)):
                continue
            path = WORKFLOW_DIR / automation
            assert path.is_file(), f"{name}: ワークフロー {automation} が存在しない"
            assert name in path.read_text(encoding="utf-8"), (
                f"{name}: {automation} の中にモデル名が現れない＝実際には回していない"
            )
            assert _triggers(_load(path)).get("schedule"), (
                f"{name}: {automation} の schedule が止まっている＝登録が実体を指していない。"
                f"ローカルへ移したなら 'local:<スクリプト>' へ書き換えること"
            )

    def test_local_entries_point_at_a_batch_that_runs_the_model(self):
        """`local:` を書いたら、そのモジュールが実際にこのモデルを回すことを確かめる。

        照合先は各バッチの `heavy_models()`。**列挙を書き写した定数ではなく実体**
        （`steps_for()` の argv や `NIGHTLY_MODELS`）から導かれるので、ステップを
        入れ替えればここが追随する。
        """
        import importlib

        for name, automation in HEAVY_AUTOMATION.items():
            if not automation.startswith(LOCAL_PREFIX):
                continue
            rel = automation[len(LOCAL_PREFIX):].strip()
            path = ROOT / rel
            assert path.is_file(), f"{name}: ローカルバッチ {rel} が存在しない"
            module = importlib.import_module(rel.removesuffix(".py").replace("/", "."))
            fn = getattr(module, "heavy_models", None)
            assert callable(fn), (
                f"{name}: {rel} に heavy_models() が無い＝何を回すか機械的に確かめられない"
            )
            assert name in fn(), (
                f"{name}: {rel} の heavy_models() に現れない＝実際には回していない"
            )

    def test_local_batches_have_a_task_installer(self):
        """ローカル駆動には**タスクスケジューラ登録**という CI から見えない一段がある。

        「登録があること ≠ 動いていること」の距離が GHA より1段長い。せめて登録手順が
        再現可能な形で存在することは縛る——手順が人の記憶にしか無いと、PC を入れ替えた
        時点で黙って消える（そして失敗は出ない）。
        """
        for name, automation in HEAVY_AUTOMATION.items():
            if not automation.startswith(LOCAL_PREFIX):
                continue
            stem = Path(automation[len(LOCAL_PREFIX):].strip()).stem   # run_nightly
            installer = ROOT / "scripts" / f"install_{stem.removeprefix('run_')}_task.ps1"
            assert installer.is_file(), (
                f"{name}: 登録スクリプト {installer.name} が無い＝起動手順が再現できない"
            )

    def test_nightly_models_are_registered_as_nightly(self):
        """逆向きの整合。夜間バッチに載せたらレジストリ側も夜間バッチを指していること。"""
        from scripts.run_nightly import heavy_models

        for name in NIGHTLY_MODELS:
            entry = HEAVY_AUTOMATION.get(name, "")
            assert entry.startswith(LOCAL_PREFIX) and name in heavy_models(), (
                f"{name} は NIGHTLY_MODELS だが HEAVY_AUTOMATION では {entry!r} になっている"
            )

    def test_exemptions_state_a_reason(self):
        """'exempt:' だけ書いて理由を省けないようにする（CLAUDE.md のメタ検証網羅性）。"""
        for name, automation in HEAVY_AUTOMATION.items():
            if not automation.startswith(EXEMPT_PREFIX):
                continue
            reason = automation[len(EXEMPT_PREFIX):].strip()
            assert len(reason) >= 20, (
                f"{name}: exempt の理由が短すぎる（{reason!r}）。"
                "なぜ自動実行しないかを、後から読む人が判断できる粒度で書くこと"
            )


# ── CLI: 実行と永続化検証 ────────────────────────────────────────────────────

class TestRunModels:
    def test_runs_sector_ols_and_verifies_persistence(self, db, make_fin):
        from database import RegressionResult

        _seed_sector(db, make_fin, n=12)
        entries = asyncio.run(run_models(["sector_ols"], db))

        assert len(entries) == 1
        e = entries[0]
        assert e["ok"] is True, e["error"]
        assert e["error"] is None
        assert "n_sectors=" in e["summary"]
        assert "gap_ratio 非NULL 12件" in e["verified"]
        assert db.query(RegressionResult).count() == 12
        # ridge 指定が execute まで届いていること（params が素通りしていない証明）
        assert {r.model for r in db.query(RegressionResult).all()} == {"ridge"}

    def test_failure_is_captured_and_other_models_continue(self, db, make_fin):
        _seed_sector(db, make_fin, n=12)
        entries = asyncio.run(run_models(["no_such_plugin", "sector_ols"], db))

        assert [e["model"] for e in entries] == ["no_such_plugin", "sector_ols"]
        assert entries[0]["ok"] is False
        assert "見つかりません" in entries[0]["error"]
        # 先行モデルの失敗が後続を止めない（fail-fast: false と同じ思想）
        assert entries[1]["ok"] is True, entries[1]["error"]

    def test_execute_failure_does_not_raise(self, db):
        """空 DB で sector_ols は ValueError を投げるが、バッチは握って結果へ落とす。"""
        entries = asyncio.run(run_models(["sector_ols"], db))
        assert entries[0]["ok"] is False
        assert entries[0]["error"].startswith("ValueError")

    def test_elapsed_is_recorded(self, db, make_fin):
        _seed_sector(db, make_fin, n=12)
        entries = asyncio.run(run_models(["sector_ols"], db))
        assert entries[0]["elapsed_min"] >= 0


class TestSharedSnapshotCache:
    """モデル間で load_data 等を再計算しない（#443・Supabase Egress 5GB/月）。

    夜間バッチは1プロセスで複数の producer を回すため、包まないとモデル数だけ
    週次127万行を再ロードする。ContextVar なので execute_plugin の
    asyncio.to_thread オフロード先（別スレッド）まで伝播する必要がある。
    """

    @staticmethod
    def _fake_plugin(compute_calls: list):
        from plugins.base import AnalysisPlugin
        from plugins.macro_snapshots import shared_cache_get_or_compute

        class _Fake(AnalysisPlugin):
            name = "fake_heavy"
            label = "fake"
            description = "test"
            heavy = True

            def params_schema(self) -> dict:
                return {}

            def execute(self, params, db) -> dict:
                # 実プラグインの load_data と同じ経路でキャッシュを引く
                val = shared_cache_get_or_compute(
                    "load_data", "key", lambda: compute_calls.append(1) or "loaded")
                return {"loaded": val is not None}

        return _Fake()

    def test_heavy_load_is_computed_once_across_models(self, db, monkeypatch):
        import plugins

        calls: list = []
        monkeypatch.setattr(plugins, "get_plugin", lambda name: self._fake_plugin(calls))

        entries = asyncio.run(run_models(["fake_a", "fake_b"], db))

        assert all(e["ok"] for e in entries), [e["error"] for e in entries]
        assert len(calls) == 1, (
            "2モデルで load_data 相当が2回走っている＝shared_snapshot_cache が"
            "ワーカースレッドへ伝播していない（Egress がモデル数に比例する）"
        )

    def test_cache_does_not_leak_outside_run_models(self, db, monkeypatch):
        """バッチを抜けたらキャッシュは破棄される（通常の API 実行へ漏らさない）。"""
        import plugins
        from plugins import macro_snapshots as ms

        monkeypatch.setattr(plugins, "get_plugin", lambda name: self._fake_plugin([]))
        asyncio.run(run_models(["fake_a"], db))

        assert ms._shared_cache.get() is None


class TestVerifier:
    def test_empty_table_raises(self, db):
        with pytest.raises(VerificationError, match="空です"):
            _verify_sector_ols(db, datetime.now(timezone.utc))

    def test_stale_rows_raise(self, db, make_fin):
        """既存行があっても、今回の実行より古ければ「書けていない」と判定する。"""
        _seed_sector(db, make_fin, n=12)
        asyncio.run(run_models(["sector_ols"], db))
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        with pytest.raises(VerificationError, match="より古い"):
            _verify_sector_ols(db, future)

    def test_macro_enet_empty_table_raises(self, db):
        with pytest.raises(VerificationError, match="空です"):
            _verify_macro_enet(db, datetime.now(timezone.utc))

    def test_macro_enet_reports_rows_and_asof(self, db):
        """件数だけでなく as-of（代表値・最古・古い銘柄数）をログへ残す（#417）。"""
        from database import replace_macro_enet_scores

        replace_macro_enet_scores(
            db,
            [{"edinet_code": "E1", "mu": 0.10, "r1_prime": 0.3},
             {"edinet_code": "E2", "mu": 0.20, "r1_prime": None}],
            "2026-08-03", snapshot_date_min="2026-07-27", n_stale=1)

        msg = _verify_macro_enet(db, datetime.now(timezone.utc) - timedelta(minutes=5))
        assert "2社" in msg
        assert "snapshot_date=2026-08-03" in msg
        assert "2026-07-27" in msg and "1社" in msg

    def test_macro_enet_stale_rows_raise(self, db):
        """全置換なので、今回の実行より古い created_at は「書けていない」と判定する。"""
        from database import replace_macro_enet_scores

        replace_macro_enet_scores(db, [{"edinet_code": "E1", "mu": 0.1}], "2026-08-03")
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        with pytest.raises(VerificationError, match="より古い"):
            _verify_macro_enet(db, future)

    def test_summarize_keeps_scalars_and_short_string_lists(self):
        """採用特徴量は残し、巨大配列は件数へ畳む（自動ドロップの追跡に必要）。"""
        s = _summarize({
            "n_sectors": 3,
            "features_used": ["pl_eps", "bs_bps"],
            "results": [{"a": 1}] * 100,
            "sector_stats": [{"industry": "x"}] * 30,
        })
        assert s == "n_sectors=3, features_used=[pl_eps,bs_bps], results(n=100), sector_stats(n=30)"

    def test_summarize_truncates_long_string_lists(self):
        s = _summarize({"features_used": [f"f{i}" for i in range(21)]})
        assert s == "features_used(n=21)"

    def test_summarize_handles_empty_result(self):
        assert _summarize({}) == "(要約できる項目なし)"


# ── ワークフロー: 起動条件の不変条件 ────────────────────────────────────────

class TestWorkflow:
    def test_workflows_is_match_all_pattern(self, workflow):
        wr = _triggers(workflow)["workflow_run"] or {}
        assert wr.get("workflows") == ["**"], (
            "workflows: は ['**'] 固定。'[定常] …' を列挙すると角括弧がフィルタパターンの"
            "文字クラスとして解釈され startup_failure になる（notify-failure.yml と同じ罠）"
        )
        assert wr.get("types") == ["completed"]

    def test_manual_dispatch_is_available(self, workflow):
        assert "workflow_dispatch" in _triggers(workflow)

    def test_chained_on_daily_incremental_success(self, workflow):
        """daily-incremental が success のときだけ起動する（鮮度が先・#423 の依存）。"""
        condition = workflow["jobs"]["scores"]["if"]
        daily_name = _load(DAILY)["name"]
        assert f"== '{daily_name}'" in condition, (
            f"daily-incremental の name（{daily_name!r}）と一致していない。"
            "改名したなら if の文字列も追随させること"
        )
        assert "conclusion == 'success'" in condition, (
            "success 以外でも起動すると、株価が前進していない日に古い株価由来の"
            "スコアを『今日のランキング』として出してしまう"
        )
        assert "workflow_dispatch" in condition, "手動実行の口が if で塞がれている"

    def test_permissions_are_minimal(self, workflow):
        perms = workflow["permissions"]
        assert perms == {"contents": "read"}

    def test_piped_steps_set_pipefail(self, workflow):
        """tee すると exit code が上書きされ、失敗が success に化ける（#352）。"""
        for step in workflow["jobs"]["scores"]["steps"]:
            script = step.get("run", "")
            if "| tee" in script:
                assert "set -o pipefail" in script, (
                    f"'{step.get('name')}' が pipefail 無しで tee している"
                )

    def test_runs_the_cli(self, workflow):
        scripts = " ".join(s.get("run", "") for s in workflow["jobs"]["scores"]["steps"])
        assert "python nightly_scores.py" in scripts

    def test_timeout_is_set(self, workflow):
        assert workflow["jobs"]["scores"]["timeout-minutes"] > 0


# ── 週次株価の run 間キャッシュ（#480・ADR-0036）──────────────────────────────

def _steps(workflow) -> list:
    return workflow["jobs"]["scores"]["steps"]


def _cache_steps(workflow, action: str) -> list:
    return [s for s in _steps(workflow) if str(s.get("uses", "")).startswith(action)]


class TestWeeklyCacheStep:
    """yml と Python 側の定数が食い違うと、キャッシュは**黙って効かないだけ**で
    failure は出ない（毎晩フルロードに戻り Egress だけ元に戻る）。ADR-0031 の
    「登録≠実行」と同型なので、CI で縛る。"""

    def test_cache_path_matches_the_python_constant(self, workflow):
        import weekly_price_cache as wpc
        paths = [s["with"]["path"].strip()
                 for s in _cache_steps(workflow, "actions/cache")]
        assert paths, "週次キャッシュの restore/save ステップが無い（#480 が無効化されている）"
        assert all(p == wpc.CACHE_DIR_NAME for p in paths), (
            f"yml のパスと weekly_price_cache.CACHE_DIR_NAME（{wpc.CACHE_DIR_NAME}）が不一致"
        )

    def test_key_is_unique_per_run_and_restore_key_is_its_prefix(self, workflow):
        """固定キーだと初日以降 save が起きず、基準が古いまま凍って毎晩フルロードへ退化する。"""
        restore = _cache_steps(workflow, "actions/cache/restore")
        assert len(restore) == 1
        key = restore[0]["with"]["key"]
        rkeys = restore[0]["with"]["restore-keys"].split()
        assert "github.run_id" in key, "key が run ごとにユニークでないと save が起きない"
        assert any(key.startswith(rk) for rk in rkeys), (
            "restore-keys が key の前方一致になっていない（直近世代を拾えない）"
        )

    def test_save_runs_even_when_the_batch_failed(self, workflow):
        """1モデルが落ちてもキャッシュは保存する（nightly_scores が続行する設計と揃える）。"""
        save = _cache_steps(workflow, "actions/cache/save")
        assert len(save) == 1
        assert save[0]["if"] == "always()"
        assert save[0]["with"]["key"] == \
            _cache_steps(workflow, "actions/cache/restore")[0]["with"]["key"]

    def test_only_nightly_scores_uses_actions_cache(self):
        """月次3本は**意図的にスコープ外**（#480・ADR-0036 決定7）＝入れ忘れではない。

        tune-hyperparameters は matrix 3並列で同一キーへ同時 save が競合する。
        入れるなら競合の扱いを決めてからにすること。
        """
        for p in sorted(WORKFLOW_DIR.glob("*.yml")):
            if p.name == WORKFLOW.name:
                continue
            assert "actions/cache@" not in p.read_text(encoding="utf-8"), (
                f"{p.name} が actions/cache を使っている。ADR-0036 決定7 を読み直すこと"
            )

    def test_egress_ledger_is_captured_as_an_artifact(self, workflow):
        """削減量を run ログの grep でなく構造化データで追えること（#478）。

        2026-08-19（ADR-0037 決定7）以降、書き先は `db_egress` の既定
        （`.egress/ledger.jsonl`）で **yml に `FINAPP_EGRESS_LEDGER` は書かない**
        ——人が環境変数を立てる運用だったせいで、過去2回の超過の主因だった
        ローカル実行の記録が空だった。artifact 側は `.egress/*.jsonl` で拾う。
        全ワークフローぶんの網羅は `tests/test_db_egress.py` のメタ検査が持つ。
        """
        artifact = next(s for s in _steps(workflow)
                        if str(s.get("uses", "")).startswith("actions/upload-artifact"))
        assert ".egress" in artifact["with"]["path"], (
            "台帳を artifact に含めていない＝実測が run ログからしか取れない"
        )

    def test_workflow_does_not_disable_the_cache(self, workflow):
        """テスト側の既定 OFF（conftest）が本番へ写経されていないこと。"""
        assert "FINAPP_WEEKLY_CACHE" not in yaml.dump(workflow)
