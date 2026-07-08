# OECD Composite Leading Indicator を先行指標チャネルとして追加する

## Status

accepted（2026-07-09）。Issue #283（ref #280）の設計決定。

## Context

現行の全マクロチャネル（FX・金利・株式・コモディティ・VIX・信用・インフレ期待・JP実体経済・物価・マネー・サーベイ）は同時性〜遅行性の指標のみで、先行指標（leading indicator）チャネルが皆無だった。

また JP_IP 凍結（#253・#281）の根本原因は「FRED が OECD 原系列の再配布を停止したこと」であり、OECD 原典に直接アクセスすればこの種の凍結リスクを避けられる。

Issue #283 は実装着手前に「OECD API はアカウント登録ゲートが無く、FRED/e-Stat 導入時に効いていた自然な安全弁が働かない」ことへの go/no-go 人判断を求めていた。実 API 検証（2026-07-09）の結果:

- OECD SDMX API（`sdmx.oecd.org/public/rest/data`）は匿名クエリのみサポート・APIキー不要（OECD 公式ドキュメントに明記）。
- レート制限は具体的な閾値が非公開（「responsive experience 維持のため導入」とのみ記載）。
- CSV 形式（`format=csvfilewithlabels`）で `TIME_PERIOD`/`OBS_VALUE` 列を返し、既存フェッチャー（`fetch_fred_series` 等）と同型の実装で完結する。
- 日本の CLI（Composite Leading Indicator・振幅調整済）は `OECD.SDD.STES,DSD_STES@DF_CLI,4.1` / `JPN.M.LI.IX._Z.AA.IX._Z.H` で取得可能。対象月から2か月遅れで公表。

## Decision

1. **OECD SDMX API から日本の CLI（Composite Leading Indicator・振幅調整済）を1系列のみ追加**。`series_code=JP_CLI`。消費者信頼感指数・企業景況感指数は短観と重複度が高いため見送り（Issue #283 の改善案どおり）。

2. **APIキー不要のため常時収集**（BOJ コネクタと同じゲート無し方式）。`OECD_RATE_SLEEP=1.0` 秒で保守的にレート制限に配慮。

3. **配置は `collector_prices.py`**。既存フェッチャーと同型のシグネチャ（`session, ..., date_from, lag_days=0 -> list[dict]`）で実装（`fetch_oecd_series`）。

4. **series_key は9次元を明示指定**。OECD Data Explorer が生成するサンプル URL は一部次元を空欄（ワイルドカード）にしているが、将来別系列を追加した際に空欄次元が複数系列にマッチするリスクを避けるため、`REF_AREA.FREQ.MEASURE.UNIT_MEASURE.ACTIVITY.ADJUSTMENT.TRANSFORMATION.TIME_HORIZ.METHODOLOGY` の全9次元を明示した完全キーを使う。

5. **`_MACRO_MAP` では zscore 変換を採用**。CLI は100を中心とした振幅調整済み指数で、水準（100からの乖離）自体がトレンド転換点シグナルのため、yoy（前年比）ではなく zscore が適切。

6. **公表ラグ補正は `lag_days=60`**（e-Stat 鉱工業指数 `JP_IIP` と同水準・2か月ラグの安全側丸め）。

## Considered Options

- **消費者信頼感指数・企業景況感指数も同時実装**（却下）：短観と重複する粒度が大きく、pooled BIC で直交性が確認できるまでは1系列に絞り検証コストを抑える。
- **series_key を空欄（ワイルドカード）のまま使う**（却下）：現状は唯一解に解決されるが、将来 OECD が同一次元に別系列を追加した場合に静かに誤ったデータへ切り替わるリスクがある。明示キーの方がコストがほぼゼロで安全。
- **yoy 変換を採用**（却下）：CLI は水準自体が景気循環シグナルであり、前年比を取るとトレンド転換点の情報が薄れる。既存の金利・DI 系チャネル（zscore 採用）と整合させる。
- **GitHub Actions Azure IP からの疎通を事前に本番検証してから実装**（見送り）：ローカル検証で200 OK・実データ取得を確認済み。stooq のような明示的ブロック事例は現時点で報告が無いため、本番初回実行時に確認する運用とする（DEPLOYMENT.md に記載）。

## Consequences

- **新規収集サーフェス**：`fetch_oecd_series`（`collector_prices.py`）・`OECD_SERIES`/`OECD_BASE_URL`/`OECD_RATE_SLEEP` 定数・`collect_macro_data` への結線・`_MACRO_MAP`/`MACRO_FEATURE_OPTIONS`（`plugins/macro_snapshots.py`）へ `macro_jp_cli_zscore` 追記。
- **新規 env var なし**：認証不要のため Render / GitHub Actions Secrets への追加作業は発生しない。
- **容量影響は軽微**：月次1系列の追加で数百KB程度。
- **検証**：本番初回実行（GitHub Actions・Azure IP）で疎通確認、pooled BIC で既存チャネル（短観DI等）との直交性を確認。
- **ドキュメント更新**：DEPLOYMENT.md（外部サービス制約に OECD 節を追加）・GOTCHAS.md（SDMX 次元明示指定・404挙動の注意）・ARCHITECTURE.md（マクロ収集チャネル）・MODELS.md（特徴量追加）。
- **残る #284（IMF WEO）・#286（GDP需要項目）は本 ADR のスコープ外**。#284 は年2回更新の疎データ・vintage バイアス設計が必要、#286 は statsDataId 特定自体が難航中で、それぞれ個別の go/no-go 判断を要する。
