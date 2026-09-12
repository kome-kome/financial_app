"""
GitHub Actions 用・差分収集パイプライン（毎日自動実行向け）。

対象: 過去1年・収集済みスキップ（XBRL）＋成長率/Zスコア再計算
     ＋市場データ（株価）更新 ＋マクロデータ（為替・金利等）更新

全件収集は _pipeline_gh.py で workflow_dispatch 手動実行。
"""
import asyncio, sys, time
from typing import Optional
from datetime import datetime, date, timedelta
from functools import partial
from dotenv import load_dotenv
load_dotenv()

from collector import (
    run_full_collection, collect_macro_data,
    collect_stock_price_history_jquants, update_market_data_from_history,
    rebuild_split_adjustment_factors,
    fill_recent_stock_price_gap_yahoo, detect_roundtrip_scale_bands,
    load_judged_scale_bands, exclude_judged_bands, roundtrip_log_line,
)
from collector_prices import format_yahoo_http_stats
from collector_utils import EdinetAccessError
from database import SessionLocal, init_db, price_freshness
import _pipeline_utils
from macro_health import check_macro_freshness, format_report

LOG_FILE = "logs/pipeline_incremental.log"

log = _pipeline_utils.make_logger(LOG_FILE)
_is_readonly_error = _pipeline_utils._is_readonly_error
# 差分パイプラインは指数バックオフ（backoff_base=2）で従来挙動を維持する。
_run_with_retry = partial(_pipeline_utils._run_with_retry, log_fn=log, backoff_base=2)


async def main():
    t0 = time.time()
    log("=" * 60)
    log("差分収集パイプライン 開始")
    log("=" * 60)

    log("[init] init_db() でスキーマ冪等マイグレーションを実行")
    init_db()

    # ─── Phase 1: XBRL 差分収集（過去1年・収集済みスキップ）───────────────
    # **EDINET の障害で株価・マクロを巻き添えにしない**（#425 と同じ原則・#580）。
    # #577 で「全件失敗を失敗として現す」ために EdinetAccessError を送出するようにしたが、
    # ここで素通りさせると Phase 3（マクロ）と Phase 4（株価）が**丸ごと走らなくなる**。
    # 2026-08-30 は EDINET が全滅していてもマクロ 41,306件・Yahoo gap-fill 341社・鮮度 fresh を
    # 収集できており、その半分を落とすのは直した穴より高くつく。10連続失敗＝わずか6秒の断で
    # 発火するので、これは理論上の話ではない。
    # したがって**握って続行し、最後に非ゼロで抜ける**——検知（#577）と非巻き添え（#425）を両立させる。
    log("[1/4] XBRL 差分収集 開始（過去1年・skip_existing=True）")
    xbrl_failure: Optional[str] = None
    db1 = SessionLocal()
    try:
        cancelled = await _run_with_retry(
            lambda: run_full_collection(
                db1,
                years_back=1,
                skip_existing=True,
                on_progress=lambda c, t, m: log(m) if c % 50 == 0 or "[完了]" in m or "[企業マスタ" in m else None,
            ),
            label="XBRL差分収集",
        )
    except EdinetAccessError as e:
        # 収集経路が構造的に壊れている。**ここでは return しない**（後続が本体の鮮度を担う）。
        cancelled = False
        xbrl_failure = str(e)
        log(f"[1/4] XBRL 差分収集 失敗（株価・マクロは継続する）: {xbrl_failure}")
    finally:
        db1.close()
    if cancelled:
        log("[1/4] 収集が停止されました")
        return
    if not xbrl_failure:
        log(f"[1/4] XBRL 差分収集 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 2: 成長率・Zスコアは financial_metrics VIEW が都度算出するため事前計算は不要 ───
    log("[2/4] 成長率・Zスコアは financial_metrics VIEW で都度算出（事前計算スキップ）")

    # ─── Phase 3: マクロデータ収集 ───────────────────────────────────────────
    log("[3/4] マクロデータ収集 開始")
    db = SessionLocal()
    try:
        n = await _run_with_retry(
            lambda: collect_macro_data(
                db, years_back=5,
                on_progress=lambda c, t, m: log(m) if c % 10 == 0 or "完了" in m else None,
            ),
            label="マクロデータ収集",
        )
        log(f"  マクロデータ {n} 件更新")

        # 健全性レポート（#420）: collect_macro_data は 1 系列が取れなくても continue する
        # ため、部分失敗は exit 0 で通り #414 の失敗通知にも載らない。その時点の鮮度を
        # run ログへ残しておく（後から「いつ欠け始めたか」を遡れるようにする）。
        # **ここでは非ゼロ終了しない**。マクロの不調でこのジョブを failure にすると、
        # マクロを一切使わない sector_ols の夜間更新（nightly-scores の workflow_run
        # チェーンは conclusion=success 条件・#432）まで巻き添えで止まるため。
        # 終了コードによる通知は独立した macro-health.yml が担う。
        for line in format_report(check_macro_freshness(db)):
            log(line)
    finally:
        db.close()
    log(f"[3/4] マクロデータ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 4: 市場データ更新（Yahoo で鮮度確保 → J-Quants で公式値へ置換）────
    # 直近の鮮度は Yahoo ギャップ補完が担う。J-Quants 無料は直近84日（12週）を配信しないため、
    # かつては days_back=14 でも取得していたが、この窓はエンバーゴ内で**構造的に常に0件**であり
    # （毎日 JQUANTS_RATE_SLEEP=20s × 14日 ≒ 4.7分の空振り）、しかも全日403となって
    # 中断ガードを誤発火させ Yahoo 補完まで巻き添えで止めていた（#419 / #425）。
    log("[4/4] 市場データ更新 開始（Yahoo で鮮度確保 → J-Quants catchup で公式値へ置換）")
    db4 = SessionLocal()
    try:
        # 鮮度を先に確保する（gap_days=0: steady-state でも毎日 Yahoo が直近を補完）。
        # J-Quants より先に置くのは、片方の収集元の失敗がもう片方を巻き添えにしないため（#425）。
        gap_result = await fill_recent_stock_price_gap_yahoo(
            db4, gap_days=0,
            on_progress=lambda c, t, m: log(m) if c % 500 == 0 or "完了" in m else None,
        )
        if gap_result.get("skipped"):
            # 週末・祝日明けはここに来る（全社が最新セッションに追いついている＝#474）。
            # 黙って通すと「2時間かけて空振りしていた」時代と区別がつかない。
            log(f"  Yahoo Finance gap-fill: スキップ（{gap_result.get('reason')}"
                f"・基準セッション {gap_result.get('session')}）")
        else:
            # upserted は**投入行数**（ON CONFLICT DO UPDATE）。正味の増分は new_rows（#474）。
            log(f"  Yahoo Finance gap-fill: {gap_result.get('upserted', 0)}件 投入"
                f"（うち新規日付 {gap_result.get('new_rows', 0)}件・"
                f"{gap_result.get('from')} 〜 {gap_result.get('to')}・"
                f"{gap_result.get('companies')}社・基準セッション {gap_result.get('session')}）")
        # 価格ゼロの母数を毎晩残す（#555）。従来はスキップ数しか出ておらず、
        # 「454 → 416 に減ったか」をアドホック SQL 無しに追えなかった。
        # exchange_rejected は通常0で、0以外は解決済み社が静かに脱落し始めた合図。
        log(f"  価格ゼロ {gap_result.get('priceless', 0)}社"
            f"（うち解決済み {gap_result.get('priceless_resolved', 0)}社）"
            + (f"・**解決済みなのに空 {gap_result.get('exchange_rejected')}社**"
               if gap_result.get("exchange_rejected") else ""))
        # 並行フェッチの安全弁（#556）。Yahoo は絞ると「200 OK・close が全 null」を返し
        # **例外を出さない**ので、429/5xx の件数を毎晩残す。0 以外が続いたら
        # `FINAPP_YAHOO_CONCURRENCY` を下げる（1 で逐次に戻る）。
        # 書式は `format_yahoo_http_stats` が唯一の源（点検 CLI の正規表現が読む形）。
        # ここで書き写すと、片方だけ変えた日に CLI が黙って読み落とす（#622 の前例）。
        _he = gap_result.get("http_errors") or {}
        if not gap_result.get("skipped"):
            log(f"  Yahoo 並行度 {gap_result.get('concurrency')}・"
                f"{format_yahoo_http_stats(_he)}")

        # J-Quants catchup: 12週境界を過ぎた直後（today-90〜today-80日）を再取得し、
        # Yahoo 暫定値を J-Quants 公式値で自動上書きする（毎日走ることで徐々に置換）。
        # J-Quants 側の障害で鮮度更新（上の Yahoo）と PER/PBR 反映（下の market_data）を
        # 落とさないよう、この呼び出しだけは失敗を握って継続する。
        _catchup_to   = date.today() - timedelta(days=80)
        _catchup_from = date.today() - timedelta(days=90)
        catchup_result: dict = {}   # 失敗経路でも下の往復段差の検知が参照する
        try:
            catchup_result = await collect_stock_price_history_jquants(
                db4, date_from=_catchup_from, date_to=_catchup_to,
                on_progress=lambda c, t, m: log(m) if c % 3 == 0 or "完了" in m else None,
            )
            log(f"  J-Quants catchup ({_catchup_from}〜{_catchup_to}): "
                f"{catchup_result.get('upserted', 0)}件 upsert"
                + (f"・契約窓外 {catchup_result['out_of_coverage']}日"
                   if catchup_result.get("out_of_coverage") else "")
                # スケール不一致で**書かなかった**行（#620）。0 が続いていたのに増えたら、
                # Yahoo が別の社の調整を落とし始めた合図。
                + (f"・スケール不一致で不採用 {catchup_result['scale_mismatch']}行"
                   f"（{len(catchup_result.get('scale_mismatch_companies') or [])}社）"
                   if catchup_result.get("scale_mismatch") else "")
                # 登録済みスピンオフの権利落ち前として**書かなかった**行（#651）。
                # `AdjC == C` のまま上の選別を素通りする行なので別に数える
                + (f"・スピンオフ権利落ち前で不採用 {catchup_result['spinoff_unadjusted']}行"
                   f"（{len(catchup_result.get('spinoff_unadjusted_companies') or [])}社）"
                   if catchup_result.get("spinoff_unadjusted") else "")
                # 403 は契約失効／プラン対象外／URL 不在。**カバレッジ境界ではない**（#462）
                + ("（全日403＝要確認）" if catchup_result.get("all_forbidden") else ""))
        except Exception as e:
            log(f"  J-Quants catchup 失敗（継続します）: {type(e).__name__}: {e}")

        # 開始も残す（#470）。完了ログしか無かったため、2026-08-08 の失敗は
        # 「catchup 完了の 2分16秒後に落ちた」から**推定**するしかなかった。
        log("  financial_records へ株価・バリュエーションを反映 開始")
        n_updated = update_market_data_from_history(db4)
        log(f"  financial_records.stock_price: {n_updated}社 更新")

        # 分割補正係数の作り直し（#655・ADR-0055）。**ここに置くのは入力の近さ**——係数は
        # `issued_shares` と `bs_bps` から復元するので、直前の工程がそれを更新した直後が
        # 唯一ずれない位置。別ステップへ切り出すと「収集は成功したが係数だけ古い」状態が
        # 作れてしまう。例外は握らない（上の株価反映と同じ扱い）＝この工程の自己検証だけが
        # 係数表の固着を検知する仕組みなので、黙って続けると #504 と同型の穴になる。
        # 検出0件のときは既存の表に触らず raise するため、失敗しても VIEW は前夜の係数で動く。
        n_factors = rebuild_split_adjustment_factors(db4)
        log(f"  split_adjustment_factors: {n_factors}行 全置換")

        # 正味の鮮度を run 間で比較できる形で残す（#474）。gap-fill の「投入行数」は
        # 取り直しを含むため鮮度の指標にならない。p50 は DB 側集約だけで出る（Egress 数行）。
        fr = price_freshness(db4)
        log(f"  株価鮮度: p50={fr.get('price_asof_p50')} / p05={fr.get('price_asof_p05')}"
            f" / max={fr.get('price_asof_max')} / level={fr.get('level')}"
            f"（{fr.get('n_codes')}銘柄・5営業日超の遅れ {fr.get('n_stale_over_5d')}銘柄）")

        # 「飛んで数日で戻る」帯の検知（#620）。**この壊れ方は例外を出さない**——
        # どちらの値も妥当な株価で、upsert は成功し、行数も鮮度も上の指標も正常に見える。
        #
        # **今夜 J-Quants が調整差を報告した社に絞って見る。** 形だけで全社を走査すると
        # 実際の値動きの往復まで拾って意味を失う（実測 283社中、本物は2社）。調整差の無い
        # 社では誰が書いても同じ値になる＝混ざりようがないので、この交差が過不足のない網。
        #
        # ただし交差は**社単位**で、ADR-0053 の確定条件1・3（帯の中に AdjC≠C の日があるか・
        # Yahoo が AdjC と食い違うか）は見ていない。分割のある社で実際の値動きが往復すると
        # 毎晩鳴り続けるので、`repair_scale_mixture` が非該当と判定済みの**帯**は除く（#644）。
        _susp = (catchup_result or {}).get("scale_mismatch_companies") or []
        try:
            if _susp:
                rt = detect_roundtrip_scale_bands(db4, only_ecs=_susp)
                rt, n_judged = exclude_judged_bands(rt, load_judged_scale_bands(db4))
                log(f"  {roundtrip_log_line(len(_susp), rt, n_judged)}")
            else:
                log("  往復段差: 検査対象なし（今夜は AdjC≠C の社が無かった）")
        except Exception as e:
            # 検知は収集の付随物。ここで落として株価収集を失敗にはしない。
            log(f"  往復段差の検知に失敗（継続します）: {type(e).__name__}: {e}")
    finally:
        db4.close()
    log(f"[4/4] 市場データ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    log("=" * 60)
    log(f"差分収集パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
    log("=" * 60)

    # 巻き添えを避けるために握った失敗を、**ここで初めて終了コードにする**（#580）。
    # 握りっぱなしにすると #577 で塞いだ「全件失敗が exit=0」がそのまま戻る。
    # 非ゼロで抜ければ `batch_common` が pipeline を失敗と記録し（`nightly_last_success` は
    # 進めない）`gh issue create` する＝**株価は取れているが XBRL は死んでいる**が読み取れる。
    if xbrl_failure:
        log(f"[FAIL] XBRL 差分収集が失敗している: {xbrl_failure}")
        raise SystemExit(1)

if __name__ == "__main__":
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"差分収集パイプライン開始: {datetime.now()}\n")
    asyncio.run(main())
