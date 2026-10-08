"""分析プラグイン・バックテスト API ルーター。

/api/plugins/*, /api/gap-analysis, /api/recommend, /api/backtest を担当。
"""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import api
import backtest
import batch_activity
import model_comparison
import plugins as plugin_registry
from collection_jobs import jobs
from plugins import progress

router = APIRouter()
log = logging.getLogger(__name__)

# 画面から回した heavy の所要（分）を残す `app_settings` のキー接頭辞。実行前の確認ダイアログに
# 「前回 約 N 分」と出すためだけの値で、バッチの所要（`run_daytime.JOBS[*].measured_min`）とは別物。
LAST_RUN_KEY_PREFIX = "plugin_last_run_min:"

# 取消した実行の HTTP 応答（409）と画面のログの最後の1行（#849）。画面は 409 の detail が
# この文で始まるかで「失敗」と「取消」を見分ける（`analysis.js::_isCancelled`）。
CANCELLED_MESSAGE = "取消しました。保存は始めていないので、保存済みの結果は前回のままです"

# いま画面から走っている heavy の取消状態（`_progress_job` 名 → Cancellation）。
# `_run_with_progress` が実行の間だけ置き、`cancel_plugin` が読む。
_cancellations: dict[str, progress.Cancellation] = {}


def _jst(iso: Optional[str]) -> Optional[str]:
    """`batch_activity` の UTC ISO を画面用の JST 文字列へ（読めなければそのまま）。"""
    from datetime import datetime
    try:
        return api._utc_to_jst_str(datetime.fromisoformat(iso)) if iso else None
    except ValueError:
        return iso


def _batch_running_message(running: list[dict]) -> str:
    parts = [f"{r['label']}（開始 {_jst(r['started_at']) or '不明'}"
             + (f"・ステップ {r['step']}" if r.get("step") else "") + "）"
             for r in running]
    return ("ローカルバッチが実行中です: " + "、".join(parts)
            + "。同じ正本 DB を並行して読み書きすると計算結果そのものが変わるため、"
              "終わってから実行してください")


def _refuse_heavy_while_unsafe(job_name: str, label: str) -> None:
    """heavy を始めてよいかの関所（409）。**`_run_with_progress` を呼ぶ直前に、await を挟まずに**呼ぶ。

    1. ローカルバッチ（夜間・日中・月次…）の実行中マーカーがあれば断る。並走は所要ではなく
       結果を変える（seed 固定でも MCMC の発散が 0→344）。マーカーは `batch_activity` が唯一の源。
    2. 同じ heavy がこのアプリ内で既に走っていれば断る。2タブで押すと JobState も保存先の表も
       取り合いになる。チェックと `reset_for_run` の間に await が無いので、イベントループ上で
       2つの要求がすり抜けることはない。

    逆向き（画面の heavy 実行中にバッチが起動する）は防げない——バッチ側はアプリを見ていない。
    """
    running = batch_activity.read_activity()["running"]
    if running:
        raise HTTPException(409, _batch_running_message(running))
    if jobs.is_running(_progress_job(job_name)):
        raise HTTPException(409, f"「{label}」は既に実行中です（別のタブで開始されています）。"
                                 "終わるまで待ってください")


def _heavy_special_names() -> set:
    """`SPECIAL_ANALYSES` のうち heavy なもの（#593）。

    進捗 SSE は `AnalysisPlugin` だけでなくここも通す。**名前を書き写さず
    `SPECIAL_ANALYSES` から引く**——特例を足したときに片方だけ直す事故を防ぐ。
    """
    return {e["name"] for e in SPECIAL_ANALYSES if e.get("heavy")}


def _progress_job(plugin_name: str) -> str:
    """進捗 JobState のスロット名。収集ジョブと同じ registry へ相乗りする（#545）。

    プラグイン名でスロットを分けるのは「種別ごとに独立スロット」という registry の
    設計に合わせるため（別モデルを続けて回しても互いのログを踏まない）。
    """
    return f"plugin:{plugin_name}"


# サイドバーIA用の「特例エントリ」。AnalysisPlugin ではない分析（スクリーニング・バックテスト）を
# プラグインと同じメタ形(name/label/category/ui_order)で /api/plugins に並べ、フロントの統一サイドバーへ載せる。
# 完全プラグイン化はしない（backtest は GET・params_schema 非使用・マルチピリオドで契約に馴染まないため）。
# href を持つエントリはタブを持たず、サイドバーで別ページへのリンクとして描画される。
SPECIAL_ANALYSES = [
    {
        "name": "screen",
        "label": "スクリーニング",
        "description": "ROE・PER・自己資本比率などの財務条件で銘柄を絞り込みます",
        "depends_on": [],
        "heavy": False,
        "category": "① 銘柄を探す",
        "ui_order": 130,
        "params_schema": {},
        "href": "/collection",  # 既存UIは収集ページ。分析ハブへの統合は後続PRで対応
    },
    {
        "name": "backtest",
        "label": "バックテスト",
        "description": "過去時点でのスコアリング（おすすめ／バリュエーション／ネットキャッシュ）の期待リターン（その後の株価変化）を検証します",
        "depends_on": [],
        "heavy": False,
        "category": "④ 戦略を検証",
        "ui_order": 410,
        "params_schema": {},  # 専用UI（既存タブ）を使用するため空
    },
    {
        "name": "model_comparison",
        "label": "モデル比較（OOF）",
        "description": "将来リターン予測モデル M-1〜M-6 の予測力（rank-IC・ロングショート spread・hit-rate）を無リーク OOF で横並び比較します（/api/backtest の as-of 上位N とは別手法）。退役した M-4/M-5 もここには並びます",
        "depends_on": [],
        "heavy": True,   # 全モデル（heavy）を実行するため。Render では各モデルがスキップされる
        "writes": [],    # 各モデルを tuning_dry_run で回す＝保存済みの表は変えない（model_comparison.py）
        "category": "④ 戦略を検証",
        "ui_order": 420,
        "params_schema": {},  # 専用UI（静的タブ）を使用するため空
    },
]


@router.get("/api/plugins")
async def list_plugins():
    """分析メタ一覧。プラグイン + 特例エントリ(screen/backtest)を ui_order 昇順で返す。

    `hidden=True` のプラグインは除外する（ADR-0044 の退役＝サイドバーに出さない）。除外は
    ここだけで、レジストリ・`/api/plugins/{name}/run`・`model_comparison` には残る。
    """
    metas = [p.to_meta() for p in plugin_registry.list_plugins() if not p.hidden]
    metas.extend(SPECIAL_ANALYSES)
    metas.sort(key=lambda m: m.get("ui_order", 999))
    return {"plugins": metas}


@router.get("/api/model/status")
async def model_status(db: Session = Depends(api.get_db)):
    """業種別OLSモデルの鮮度情報。鮮度バーUI用。"""
    import datetime
    from sqlalchemy import func
    from database import FinancialRecord, RegressionResult

    rq = db.query(RegressionResult).filter(RegressionResult.gap_ratio.isnot(None))
    computed_at = rq.with_entities(func.max(RegressionResult.computed_at)).scalar()
    n_results = rq.count()
    data_updated_at = db.query(func.max(FinancialRecord.updated_at)).scalar()

    staleness_days = None
    is_stale = False
    if computed_at:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        staleness_days = (now - computed_at).days
        if data_updated_at:
            is_stale = computed_at < data_updated_at

    return {
        "computed_at": computed_at.isoformat() if computed_at else None,
        "staleness_days": staleness_days,
        "n_results": n_results,
        "is_stale": is_stale,
    }


@router.post("/api/plugins/{plugin_name}/run", response_model=None)
@api.limiter.limit(api.RATELIMIT_ANALYSIS)
async def run_plugin(
    request: Request, plugin_name: str, params: dict,
    db: Session = Depends(api.get_db),
):
    p = plugin_registry.get_plugin(plugin_name)
    if p is None:
        raise HTTPException(404, f"プラグイン '{plugin_name}' が見つかりません")
    # 旗（RENDER_LIGHT_MODE）だけでなく接続先 prod でも止める＝旗が反映されない Render で
    # sector_ols が断面の regression_results へ書くのを防ぐ（#733）
    if api.writes_blocked() and getattr(p, "heavy", False):
        raise HTTPException(403, f"「{p.label}」は計算が重いためローカル環境で実行してください"
                                 "（Render Free プラン制限。Render は閲覧専用で、ローカルで実行した結果はここには反映されません）")
    if getattr(p, "heavy", False):
        # try の外で断る（中の `except Exception` に 409 を 500 へ化けさせない）
        _refuse_heavy_while_unsafe(plugin_name, p.label)
    try:
        if getattr(p, "heavy", False):
            return await _execute_with_progress(p, plugin_name, params, db)
        return await plugin_registry.execute_plugin(p, params, db)
    except progress.AnalysisCancelled:
        # 下の `except Exception` に 500 へ化けさせない（取消は失敗ではない・#849）
        raise HTTPException(409, CANCELLED_MESSAGE)
    except (plugin_registry.DependencyError, ValueError) as e:
        # パラメータ契約違反・依存不足のドメイン検証メッセージ（内部実装は露出しない）。
        # 可観測性のためログにも残す（#346）。
        log.info("プラグイン '%s' の実行を検証で拒否: %s", plugin_name, e)
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Plugin '%s' error: %s", plugin_name, e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


async def _run_with_progress(job_name: str, label: str, run, db=None):
    """重い分析を進捗 sink で包んで実行する（Issue #545・#593 で特例エントリへも拡張）。

    包むのは **heavy だけ**。軽い分析まで JobState を回すと、画面が開いていない
    実行（`/api/recommend` 等）でも状態が残り続けて意味が無い。sink は ContextVar 経由で
    execute の内側（`asyncio.to_thread` の先）まで伝播する。

    `run` は引数なしのコルーチン関数。**プラグインと特例エントリで実体を2本に増やさない**
    ——`model_comparison` は `AnalysisPlugin` ではなく `SPECIAL_ANALYSES` の特例で
    `/api/plugins/{name}/run` を通らないが、進捗の配線だけはここを共有する（#593）。

    例外は握らず送出する（呼び出し側の except が HTTP ステータスへマップする契約を保つ）
    が、**閉じる前に最後の1行として画面へ残す**——ここで黙って落ちると、画面は
    ストリームが切れただけになり「終わった」と区別できない。

    `db` を渡すと、成功したときの所要を `app_settings` に残す（`_record_last_run`）。

    画面からの取消（#849）は `Cancellation` を実行の間だけ `_cancellations` に置いて受ける。
    取消で止まったときは `AnalysisCancelled` をそのまま送出する（呼び出し側が 409 へ写す）。
    """
    job = _progress_job(job_name)
    st = jobs.state(job)
    st.reset_for_run()
    st.append_log(f"「{label}」を開始しました")
    started = time.monotonic()
    cancel = progress.Cancellation()
    _cancellations[job] = cancel

    def sink(step: str, current: int, total: int) -> None:
        st.progress, st.total = current, total
        st.append_log(f"{step} {current}/{total}" if total else step)

    try:
        with progress.progress_sink(sink, cancel=cancel):
            result = await run()
        if db is not None:
            _record_last_run(db, job_name, (time.monotonic() - started) / 60.0)
        return result
    except progress.AnalysisCancelled:
        st.append_log(CANCELLED_MESSAGE)
        raise
    except Exception as e:
        st.append_log(f"[エラー] {e}")
        raise
    finally:
        if _cancellations.get(job) is cancel:
            del _cancellations[job]
        # running=False が SSE の終端。append_log より後（最後の1件に載せるため）。
        st.running = False


def _record_last_run(db, job_name: str, minutes: float) -> None:
    """画面から回した heavy の所要を残す（次の確認ダイアログの「前回 約 N 分」）。

    **失敗しても実行結果は返す**——所要の記録は表示用の補助で、計算の成否と関係ない。
    閲覧専用の環境（Render の断面）には書かない。
    """
    if api.writes_blocked():
        return
    try:
        from database import upsert_setting
        upsert_setting(db, LAST_RUN_KEY_PREFIX + job_name, f"{minutes:.1f}")
    except Exception:                                  # noqa: BLE001 — 補助の記録で実行を落とさない
        log.warning("「%s」の所要を記録できなかった", job_name, exc_info=True)
        try:
            db.rollback()
        except Exception:                              # noqa: BLE001
            pass


async def _execute_with_progress(p, plugin_name: str, params: dict, db):
    """heavy プラグインを進捗つきで実行する（`_run_with_progress` の薄い入口）。"""
    return await _run_with_progress(
        plugin_name, p.label, lambda: plugin_registry.execute_plugin(p, params, db), db=db)


def _heavy_entry(name: str) -> Optional[dict]:
    """heavy な分析のメタ（プラグインと `SPECIAL_ANALYSES` の特例の両方）。heavy でなければ None。"""
    p = plugin_registry.get_plugin(name)
    if p is not None:
        return p.to_meta() if getattr(p, "heavy", False) else None
    return next((e for e in SPECIAL_ANALYSES if e["name"] == name and e.get("heavy")), None)


@router.get("/api/batch/activity")
async def batch_activity_status():
    """いま走っているローカルバッチ（`running`）と、期限切れで残ったマーカー（`stale`）。

    分析画面が読み込み時に読み、走っていれば heavy のボタンを止めて帯で理由を出す。
    時刻は画面用に JST へ整形して返す。
    """
    act = batch_activity.read_activity()
    for key in ("running", "stale"):
        for r in act[key]:
            r["started_at_jst"] = _jst(r.get("started_at"))
            r["heartbeat_at_jst"] = _jst(r.get("heartbeat_at"))
    if act["running"]:
        act["message"] = _batch_running_message(act["running"])
    return act


@router.get("/api/plugins/{plugin_name}/preflight")
async def plugin_preflight(plugin_name: str, db: Session = Depends(api.get_db)):
    """heavy を押す直前の確認材料（読取専用）。画面はこれで確認ダイアログを組み立てる。

    - `blocked_reason`: 今は始められない理由（バッチ実行中・同じ分析が実行中）。None なら始められる
    - `writes`: 置き換わる保存済みの表（`AnalysisPlugin.writes`）
    - `last_run_min`: このアプリから前回回したときの所要（記録が無ければ None）

    サーバ側の 409（`_refuse_heavy_while_unsafe`）と同じ判定を使う。こちらは押す前に見せる
    ための写しで、守りは 409 の側にある。
    """
    entry = _heavy_entry(plugin_name)
    if entry is None:
        raise HTTPException(404, f"heavy な分析 '{plugin_name}' が見つかりません")
    try:
        _refuse_heavy_while_unsafe(plugin_name, entry["label"])
        blocked = None
    except HTTPException as e:
        blocked = e.detail
    last = None
    try:
        from database import get_setting
        raw = get_setting(db, LAST_RUN_KEY_PREFIX + plugin_name)
        last = float(raw) if raw not in (None, "") else None
    except Exception:                                  # noqa: BLE001 — 表示の補助で画面を殺さない
        log.warning("「%s」の前回所要を読めなかった", plugin_name, exc_info=True)
    return {"name": plugin_name, "label": entry["label"], "writes": entry.get("writes"),
            "last_run_min": last, "blocked_reason": blocked}


@router.get("/api/plugins/{plugin_name}/progress")
async def stream_plugin_progress(plugin_name: str):
    """heavy プラグイン実行の進捗を SSE で流す（Issue #545）。

    画面は POST の応答を待たずにこれを開く（POST は完了まで返らないため）。開始前に
    開かれても `stream_awaiting_start` が数秒待ち合わせる。
    """
    if plugin_registry.get_plugin(plugin_name) is None and plugin_name not in _heavy_special_names():
        raise HTTPException(404, f"プラグイン '{plugin_name}' が見つかりません")
    return jobs.stream_awaiting_start(_progress_job(plugin_name))


@router.post("/api/plugins/{plugin_name}/cancel")
async def cancel_plugin(plugin_name: str):
    """画面から回した heavy の取消を求める（協調型・#849）。

    止まるのは計算が次に進捗を送ったときで、すぐではない。**保存を始めた後は受け付けない**
    （`progress.persisting`）——sector_ols は業種ごとに commit するので、途中で止めると
    `regression_results` に新旧が混ざる。入口の判定は進捗 SSE と同じ規則にする。

    返すのは `{accepted, running, saving, message}`。受付の可否はプロセス内の旗を見るだけで、
    DB へは書かない。
    """
    if plugin_registry.get_plugin(plugin_name) is None and plugin_name not in _heavy_special_names():
        raise HTTPException(404, f"プラグイン '{plugin_name}' が見つかりません")
    job = _progress_job(plugin_name)
    cancel = _cancellations.get(job)
    st = jobs.state(job)
    if cancel is None or not st.running:
        return {"accepted": False, "running": False, "saving": False,
                "message": "実行中ではありません（もう終わっています）"}
    if not cancel.request():
        msg = "保存を始めた後は取消できません。このまま最後まで実行します"
        st.append_log(msg)
        return {"accepted": False, "running": True, "saving": True, "message": msg}
    msg = "取消を受け付けました。次の区切りで止まります"
    st.append_log(msg)
    return {"accepted": True, "running": True, "saving": False, "message": msg}


def project_tuned_params(plugin, params: dict) -> tuple[dict, list[str]]:
    """保存済みの調整値を**現在の探索空間へ射影**する（#604）。

    `plugin_tuned_params` は「そのとき探索した空間」の記録なので、**軸を外すと古い値が
    残り続ける**。画面はこれを「🔧 自動調整済み」としてフォームへプリフィルするため、
    探索をやめた設定が推奨値として出続ける——実際 M-1 は 2026-09-04 に
    `use_momentum`/`momentum_window` を軸から外したが、保存値は
    `use_momentum=True, momentum_window=18`（2026-09-02 の探索）のままで、
    次の月次探索（毎月3日・#579 で2日から移動）まで画面に出続ける状態だった。

    射影の規則:
      - `base_params` にあるキー … **その値で上書き**（探索が固定した測定条件そのもの）
      - dims にある軸           … 保存値をそのまま使う（探索で選ばれた値）
      - どちらにも無いキー       … 落とす（`coerce_params` が `params_schema` の既定を補完）

    第2要素は「保存値と違う扱いになったキー」。画面はこれを注記に使う。**黙って値を
    変えると、今度は「調整済みと言いながら別の値」という逆の混乱になる。**

    `tuning_search_space()` を持たないプラグインは射影せずそのまま返す。
    """
    space_fn = getattr(plugin, "tuning_search_space", None)
    if space_fn is None:
        return dict(params), []
    try:
        base, dims = space_fn()
    except Exception:                                  # 探索空間が壊れていても表示は殺さない
        log.warning("tuning_search_space() の取得に失敗（射影せず生値を返す）", exc_info=True)
        return dict(params), []

    dim_names = {d.name for d in dims}
    out: dict = {}
    changed: list[str] = []
    for k, v in (params or {}).items():
        if k in base:
            out[k] = base[k]
            if base[k] != v:
                changed.append(k)
        elif k in dim_names:
            out[k] = v
        else:
            changed.append(k)                          # 空間から消えた＝落として既定へ
    for k, v in base.items():                          # 保存値に無い固定値も足す
        out.setdefault(k, v)
    return out, sorted(changed)


# ── 測ったパネルの照合（#711・ADR-0047）──────────────────────────────────────
# 保存値は「そのとき存在したパネルで測った結果」で、パネルが動けば根拠は消える。射影
# （`project_tuned_params`）が見るのは**探索空間の形だけ**なので、いま探索中の軸に残った
# 古い値は素通りする——2026-09-02 の探索が選んだ `max_features=5` は #615 で `use_macro`
# が base へ落ちた後も画面のプリフィルに出続け、9/20 の実測では rank-IC +0.0052・
# fold 間 std 0（予測値が月内で全銘柄同じ）という最悪の条件だった。**パネルの世代は
# 空間とは別の目で見る。**
#
# 指紋の規則は `hyperparameter_search._data_fingerprint()` が唯一の源で、ここへ書き写さない
# （`weekly_price_cache.fingerprint()` の「規則が2つ同居すると次に触る人が古い方をコピー
# する」と同じ理由）。
#
# **パネルは毎晩伸びるので指紋は探索当日しか一致しない＝自動プリフィルは事実上ほぼ常に
# 止まる。** これは意図した交換で、消えるのは「黙って推す」ことだけ——バッジと
# 「調整済みの値に戻す」ボタンは残るので手動適用の導線は生きている。ADR-0047 は同じ判定を
# **品質ゲートでは棄却した**が、あちらは比較そのものが消えて劣化防止が丸ごと無くなるため
# で、理由が逆になっている。
PANEL_FP_TTL_SEC = 60.0

# 画面は1回の読込で3モデルぶん叩く。指紋は `stock_price_weekly`（実測 1,284,465行・195MB）へ
# `count(*)` を打つので、TTL で3回を1回へまとめる。このエンドポイントは「読取専用・軽量
# （重い計算は起こさない）」を約束しているので、約束の側を守る。
_panel_fp_cache: dict = {}


def current_panel_fingerprint(db) -> Optional[str]:
    """いま動いているパネルの指紋（読めなければ None）。

    **例外を外へ出さない。** 指紋が取れないのは表示を殺す理由にならず、呼び出し側が
    「同じだと言えない」として扱えば安全側へ倒れる（`project_tuned_params` が探索空間の
    取得に失敗しても生値を返すのと同じ方針）。
    """
    now = time.monotonic()
    hit = _panel_fp_cache.get("v")
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        from hyperparameter_search import _data_fingerprint
        fp = _data_fingerprint(db)
    except Exception:                                  # noqa: BLE001 — 表示は殺さない
        log.warning("パネル指紋の取得に失敗（自動適用しない側へ倒す）", exc_info=True)
        fp = None
    _panel_fp_cache["v"] = (now + PANEL_FP_TTL_SEC, fp)
    return fp


def panel_changed(tuned_fp: Optional[str], current_fp: Optional[str]) -> Optional[bool]:
    """保存時のパネルと現在のパネルが違うか。True=違う / False=同じ / None=判定不能。

    **`stale_params` へ混ぜない。** 「探索空間から軸が消えた」と「パネルが動いた」は別の
    事実で、同じ顔にすると読む人が原因を選べない（`batch_freshness.status_of` が missing と
    stale を分けているのと同じ）。

    どちらかが欠けていれば None ＝**「同じ」と積極的に言えたときだけ False** を返す。
    指紋を持たない古い行と、指紋を読めなかった回を、「一致した」と同じ扱いにしない
    （`check_batch_freshness.recovered()` が ok を積極的に言えた対象だけ返すのと同じ向き）。
    """
    if not tuned_fp or not current_fp:
        return None
    return tuned_fp != current_fp


@router.get("/api/plugins/{plugin_name}/tuned")
async def get_plugin_tuned(plugin_name: str, db: Session = Depends(api.get_db)):
    """自動調整済みハイパーパラメータ（Issue #264・hyperparameter_search.py --persist が
    書き込む）を読む。読取専用・軽量（重い計算は起こさない）。未調整なら404。

    `params` は**現在の探索空間へ射影した値**を返す（#604・`project_tuned_params`）。
    生の保存値は `params_as_tuned`、射影で扱いが変わったキーは `stale_params` に載せる
    ——画面はこの2つで「保存された値」と「いま推奨できる値」を区別して見せられる。

    加えて**測ったパネルが現在と同じか**を `panel_changed` で返す（#711・ADR-0047）。
    射影は空間の形しか見ないので、いま探索中の軸に残った古い値はこちらでしか捉えられない。
    画面は `panel_changed === false`（同じだと言えた）ときだけ自動プリフィルする。
    """
    from database import get_tuned_params

    tuned = get_tuned_params(db, plugin_name)
    if tuned is None:
        raise HTTPException(404, f"'{plugin_name}' は自動調整されていません")
    plugin = plugin_registry.get_plugin(plugin_name)
    if plugin is not None:
        raw = tuned.get("params") or {}
        projected, changed = project_tuned_params(plugin, raw)
        tuned = {**tuned, "params": projected,
                 "params_as_tuned": raw, "stale_params": changed}
    current_fp = current_panel_fingerprint(db)
    return {**tuned, "panel_fingerprint": current_fp,
            "panel_changed": panel_changed(tuned.get("data_fingerprint"), current_fp)}


@router.get("/api/gap-analysis")
@api.limiter.limit(api.RATELIMIT_ANALYSIS)
async def gap_analysis(
    request: Request,
    year: Optional[int] = None,
    sort: str = "asc",
    db: Session = Depends(api.get_db),
):
    p = plugin_registry.get_plugin("gap_analysis")
    try:
        return await plugin_registry.execute_plugin(p, {"year": year, "sort": sort}, db)
    except (plugin_registry.DependencyError, ValueError) as e:
        # sector_ols 未実行等の依存/検証エラー（内部実装は露出しない）。可観測性のためログにも残す（#346）。
        log.info("gap-analysis を依存/検証で拒否: %s", e)
        raise HTTPException(404, str(e))
    except Exception as e:
        log.error("Gap-analysis error: %s", e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


@router.get("/api/recommend/presets")
async def get_recommend_presets(db: Session = Depends(api.get_db)):
    from plugins.recommend import METRICS, get_all_presets
    return {"presets": get_all_presets(db), "metrics": METRICS}


@router.post("/api/recommend")
async def recommend_stocks(req: dict, db: Session = Depends(api.get_db)):
    p = plugin_registry.get_plugin("recommend")
    try:
        return await plugin_registry.execute_plugin(p, req, db)
    except (plugin_registry.DependencyError, ValueError) as e:
        # パラメータ契約違反・依存不足のドメイン検証メッセージ（内部実装は露出しない）。可観測性のためログにも残す（#346）。
        log.info("recommend を依存/検証で拒否: %s", e)
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Recommend error: %s", e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


@router.get("/api/backtest")
async def run_backtest(
    preset: str = "バランス型",
    months_ago: int = 6,
    top_n: int = 20,
    industry: Optional[str] = None,
    min_market_cap: Optional[float] = None,
    source: str = "recommend",
    cost_bps: float = 0.0,
    db: Session = Depends(api.get_db),
):
    if not (1 <= months_ago <= 60):
        raise HTTPException(400, "months_ago は 1〜60 の範囲で指定してください")
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    if source not in backtest.SCORING_SOURCES:
        raise HTTPException(400, f"source は {', '.join(backtest.SCORING_SOURCES)} のいずれか")
    if not (0 <= cost_bps <= 100):
        raise HTTPException(400, "cost_bps は 0〜100 の範囲で指定してください")
    try:
        return backtest.run(db, preset, months_ago, top_n, industry, min_market_cap, source, cost_bps)
    except ValueError as e:
        # 非対応の重み（mu＝μ̂・Issue #423 子4）等のドメイン検証。500 にすると
        # 「壊れている」と読めてしまうため 400 で理由を返す。
        log.info("backtest を検証で拒否: %s", e)
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Backtest error: %s", e, exc_info=True)
        raise HTTPException(500, "バックテスト実行エラーが発生しました。")


@router.get("/api/backtest/multi")
async def backtest_multi(
    preset: str = "バランス型",
    top_n: int = 20,
    industry: Optional[str] = None,
    min_market_cap: Optional[float] = None,
    source: str = "recommend",
    cost_bps: float = 0.0,
    db: Session = Depends(api.get_db),
):
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    if source not in backtest.SCORING_SOURCES:
        raise HTTPException(400, f"source は {', '.join(backtest.SCORING_SOURCES)} のいずれか")
    if not (0 <= cost_bps <= 100):
        raise HTTPException(400, "cost_bps は 0〜100 の範囲で指定してください")
    periods = []
    for m in backtest.MULTI_PERIODS:
        try:
            periods.append(backtest.run(db, preset, m, top_n, industry, min_market_cap, source, cost_bps))
        except ValueError as e:
            # 重み由来の非対応（mu）はどの期間でも同じ結論なので、期間ごとに「計算エラー」を
            # 並べず即 400 で理由を返す（Issue #423 子4）。
            log.info("backtest multi を検証で拒否: %s", e)
            raise HTTPException(400, str(e))
        except Exception as e:
            log.error("Backtest multi error (months=%d): %s", m, e, exc_info=True)
            periods.append({"holding_months": m, "summary": None, "results": [],
                            "total_candidates": 0, "error": "計算エラー"})
    return {"periods": periods, "preset": preset, "top_n": top_n, "source": source, "cost_bps": cost_bps}


@router.post("/api/backtest/model-comparison", response_model=None)
@api.limiter.limit(api.RATELIMIT_ANALYSIS)
async def backtest_model_comparison(request: Request, db: Session = Depends(api.get_db)):
    """将来リターン予測モデル M-1/M-2/M-3 の OOF バックテストを横並び比較する。

    3モデルとも heavy=True のため、Render 軽量モードでは各モデルが reason="heavy_render" で
    スキップされる（ローカル実行専用）。個々のモデル失敗は per-model で握り、比較全体は継続する。
    """
    # 進捗つきで包む（#593）。内部で heavy 3本を順に回すので**実行が最も長いのがここ**
    # なのに、#545 の配線は `/api/plugins/{name}/run` 経路にしか通っていなかった。
    label = next((e["label"] for e in SPECIAL_ANALYSES if e["name"] == "model_comparison"),
                 "モデル比較（OOF）")
    # try の外で断る（下の `except Exception` に 409 を 500 へ化けさせない）
    _refuse_heavy_while_unsafe("model_comparison", label)
    try:
        return await _run_with_progress(
            "model_comparison", label,
            lambda: model_comparison.run_comparison(
                db, render_light_mode=api.writes_blocked()), db=db)
    except progress.AnalysisCancelled:
        raise HTTPException(409, CANCELLED_MESSAGE)
    except Exception as e:
        log.error("Model comparison error: %s", e, exc_info=True)
        raise HTTPException(500, "モデル比較の実行エラーが発生しました。")
