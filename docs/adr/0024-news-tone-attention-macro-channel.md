# ニューストーン／関心度チャネル（GDELT・Wikimedia Pageviews）をマクロ集約系列として追加する

## Status

accepted（2026-07-31）。Issue #406 の設計決定（親 #404 のトリアージで分割された子）。ADR-0023
（EPU チャネル＋昇格ゲート手順）の続編。

## Context

既存のマクロチャネルは価格（FX・株価指数・金利・コモディティ）・実体経済（GDP・失業率・IIP）・
サーベイ（短観）・見通し（IMF WEO）・政策不確実性（EPU・#404）で構成される。EPU は新聞記事の
**出現量**から政策不確実性を測るが、記事の**極性（トーン）**と一般大衆の**関心度**は未カバー。
GDELT・Wikimedia Pageviews はどちらも無料・認証不要で日次の該当データを配信している。

制約は容量。#404 の原案は銘柄別（約4,000社 × 日次）だったが、2026-07-30 の本番実測で

| 項目 | 実測値 |
|---|---|
| DB 合計 | 326MB / 500MB（ヘッドルーム 174MB） |
| `macro_data` | 22MB / 62,367行 / 49系列 ≈ 370 bytes/行 |
| 銘柄別日次の想定 | 4,000社 × 250営業日 = 100万行/年 ≈ **370MB/年** |

となり、無料枠を初年度で超過するため**銘柄別は採れない**。一方でマクロ集約（数系列）なら既存
`macro_data`（`series_code` 縦持ち）へ 1系列 ≈ 1.5MB/10年で収まる。

実 API 検証（2026-07-31・いずれも認証不要）:

- **GDELT DOC 2.0**（`api.gdeltproject.org/api/v2/doc/doc`）: `mode=timelinetone` / `timelinevol`
  が日次の集計系列を JSON で返す。配信開始は **2017-01-01**（それ以前を指定すると
  `Invalid query start date`）。2017-01-01〜今日を**1リクエスト**で投げても間引かれず日次のまま
  3,473 点が返る＝系列あたり1リクエストで全履歴が揃う（ページング・チャンク分割は不要）。
  レート制限は「1リクエスト/5秒」で、**超過時も HTTP 200 のままプレーンテキストの警告本文**を
  返す（ステータスコードでは検知できない）。
- **Wikimedia Pageviews**（`wikimedia.org/api/rest_v1/metrics/pageviews/per-article`）: 記事別
  日次閲覧数。配信開始 **2015-07-01**、全期間が1リクエスト（4,047点）で返る。**User-Agent に
  連絡先（URL かメール）が無いと 403**（robot policy）。

## Decision

1. **GDELT から3系列をマクロ集約で収集する**（`collector_prices.py::GDELT_SERIES`）:

   | `series_code` | 内容 | mode | query |
   |---|---|---|---|
   | `JP_NEWS_TONE` | 日本ニュース全体の平均トーン | `timelinetone` | `sourcecountry:japan` |
   | `JP_NEWS_ECON_TONE` | 株式市場関連ニュースの平均トーン | `timelinetone` | `sourcecountry:japan theme:ECON_STOCKMARKET` |
   | `JP_NEWS_ECON_VOL` | 株式市場関連ニュースの報道量（全記事比%） | `timelinevol` | 同上 |

   レート制限対策は「本文が JSON か」で判定し、`GDELT_RATE_SLEEP=6.0` 秒 × 試行回数の線形
   バックオフで再試行する（`GDELT_RETRIES=4`＝最大 60 秒/系列）。取れなければ空リストで
   graceful skip＝既存コネクタと同型。

2. **Wikimedia から2系列を「記事バスケットの合算」で収集する**（`WIKIMEDIA_SERIES`）:

   | `series_code` | 記事（ja.wikipedia） |
   |---|---|
   | `JP_WIKI_MARKET_ATTN` | 日経平均株価・東京証券取引所 |
   | `JP_WIKI_MACRO_ATTN` | 景気後退・インフレーション・日本銀行・金融政策 |

   単一記事はニュース以外の流入（リンク元の変化・編集）でも跳ねるため**複数記事を合算**する。
   欠測日（API が項目ごと落とす）は **0 埋めせず合算から除外**する（記事の増減で水準に段差を
   作らないため）。存在しない記事（404）はその記事だけ落として残りで合算を続ける。
   User-Agent は連絡先付き定数 `WIKIMEDIA_UA`（個人メールではなくリポジトリ URL）。

3. **変換規約は5系列とも zscore**（`plugins/macro_snapshots.py::_MACRO_MAP`）。トーンは正負を
   跨ぐため yoy（除算）が発散する。報道量(%)・閲覧数は常に正だが、「平時比でどれだけ注目されて
   いるか」がレジーム情報であり yoy にするとその水準の高低が消えるため、EPU/VIX/CLI と同じ規約に
   揃える。5系列とも**日次**なので低頻度系列の変換窓（#379/#382）にも strict 律速（#381）にも
   触れない。

4. **既定には入れない**。ADR-0023 で定式化した「収集 → 保留枠 → 実測 → 昇格」に従って本番
   `macro_data` へ蓄積し昇格ゲート（2モデル × 2指標の4検定・Bonferroni α=0.0125）を通したが、
   **4検定すべて非有意**（下の実測節）。よって選択肢としては残しつつ `DEFAULT_MACRO_FEATURES`
   からは外す。実測前の保留枠 `_PENDING_EVAL_FEATURES` と、実測して棄却された枠
   `_GATE_REJECTED_FEATURES` は**別の集合として区別する**（後から「未評価なのか評価済みで
   落ちたのか」を取り違えないため）。

5. **昇格ゲートのランナーを一般化する**。`scripts/epu_feature_bakeoff.py` を
   `scripts/macro_feature_bakeoff.py` へ改名し、候補を `--preset`（`PRESETS`）／`--features` で
   受け取る形にした（`--preset epu` が旧スクリプトと等価）。2件目の候補が出た時点で複製を作らない。

6. **銘柄別センチメントは採らない**（今後も再提案しない）。容量が構造的に入らないため、
   `theme:` 絞り込みによるマクロ集約が無料枠での上限。

容量見積り: 5系列 × 約3,500〜4,000行 ≈ 1.9万行 × 370 bytes ≈ **約7MB**（ヘッドルーム 174MB の
約4%）。収集時間の増分は GDELT 3系列 × 6秒 ＋ Wikimedia 6記事 × 1秒 ≈ **30秒/回**
（GDELT のレート制限に当たった場合はリトライで最大 3 分程度）。

## 実測（2026-07-31・`scripts/macro_feature_bakeoff.py --preset attention`・本番蓄積後）

パネル: 3,979社 / 43ヶ月 / 57,955サンプル / 9 fold（honest embargo=12）。base は現行
`DEFAULT_MACRO_FEATURES`（71特徴量）、with_cand は base + 本 ADR の5系列（76特徴量）。

| モデル | 指標 | base | with_cand | 差 | p | 判定 |
|---|---|---|---|---|---|---|
| M-2（XGBoost） | rank-IC | +0.1449 | +0.1390 | **−0.0060** | 0.140 | ns |
| M-2（XGBoost） | 売り側 spread | +0.0503 | +0.0482 | −0.0021 | 0.495 | ns |
| M-6（ElasticNet） | rank-IC | +0.1713 | +0.1723 | +0.0010 | 0.214 | ns |
| M-6（ElasticNet） | 売り側 spread | +0.0684 | +0.0682 | −0.0002 | 0.623 | ns |

strict 母集団は 43ヶ月・57,955サンプルで**不変**（2017-01 開始でも既存の律速より新しくない＝
学習窓は縮まない）。**VERDICT: keep as option only**（Bonferroni α=0.0125 で通過ゼロ）。

確定知見:

- **ニュースのトーン・報道量・Wikipedia 関心度は、既存マクロ71本の上に情報を足さない**。5系列を
  足しても M-6 の rank-IC は +0.0010（p=0.214）、M-2 はむしろ −0.0060 と悪化方向。EPU（#404）が
  売り側 spread で有意に効いた（+0.0032・p=0.001）のと対照的で、「新聞記事ベースなら何でも効く」
  わけではない。EPU は**政策不確実性という構造化された指数**、GDELT トーンは**未構造の平均極性**で、
  後者は既に価格・VIX・EPU に織り込まれていると解釈できる。
- 月次スナップショット（月末の値を1点だけ使う）では日次の関心度スパイクがほぼ落ちる。日次シグナルを
  活かすなら週次の M-3（ADR-0012）側での利用が筋で、月次3兄弟への投入は分が悪い。
- 昇格ゲートは**棄却も含めて記録に残す**。以降「GDELT を入れれば効くのでは」という再提案は、本節の
  実測を上書きする根拠（別の集約軸・別頻度）を伴わない限り採らない。

## Considered Options

- **銘柄別 GDELT／Wikimedia（#404 原案）**: 却下。370MB/年で無料枠に入らない（上表）。
- **GDELT GKG の生ファイル（15分ごとの CSV/ZIP）をダウンロードして自前集計**: 却下。日次数百MB
  規模のダウンロードが必要で、Render 無料プラン・GitHub Actions の実行時間に見合わない。DOC 2.0
  の timeline モードが同じ集計を1リクエストで返す。
- **Wikimedia を記事1本＝1系列で持つ**: 却下。単一記事は編集・リンク流入で跳ね、ニュース由来の
  関心度と分離できない。またテーマあたり系列数が増え、pooled BIC / ElasticNet の共線ブロックを
  無駄に膨らませる。
- **Google Trends（#404 で検討済み）**: 却下済み。公式 API は申請ゲート（alpha）で無料枠が無い。
- **欠測日を 0 で埋める**: 却下。閲覧数 0 と「その日のデータが無い」は別物で、0 埋めすると
  zscore の分布が歪む。

## Consequences

- **良い点**: 既存チャネルと直交する2軸（記事の極性・大衆の関心度）が加わる。追加コストは
  容量 +7MB・収集 +30秒/回のみで、認証情報の新規管理はゼロ（どちらも認証不要）。
- **悪い点 / リスク**:
  - GDELT のレート制限は間隔（1req/5s）だけでなく**短時間の累積クエリ数**にも効く。開発中に数十回
    叩いた直後は 60 秒の線形バックオフでも3系列中2系列が全滅し、3 分放置したら即復帰した
    （2026-07-31 実測）。リトライ上限に達した系列はその回だけスキップされ、次回収集で埋まる
    （upsert は冪等）。通常運用（3系列 × 日1回）はこの上限に触れない。
  - GDELT は 2017-01-01 開始・Wikimedia は 2015-07-01 開始で、既存の日次市場系列（1990年代〜）
    より短い。**既定へ昇格させる場合は strict 母集団が縮まないかを実測で確認する**（#381 の教訓・
    ゲートスクリプトが自動でチェックする）。
  - ja.wikipedia の記事が改名・統合されると 404 になり、その記事の寄与が黙って消える（合算のため
    水準は下がるが収集は止まらない）。記事名は定数として `WIKIMEDIA_SERIES` に明示してある。
- **既定に入らないため、実行時コストは選択したユーザーだけが払う**。収集は続けるので、将来
  ボラティリティ・レジームが変わったときに再判定できる（`--features` で個別に再実測可能）。

## 参考

- GDELT DOC 2.0 API ドキュメント: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- Wikimedia REST API（Pageviews）: https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/
- Wikimedia robot policy（User-Agent 要件）: https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy
