# 日本株財務分析ツール

ファンダメンタル分析を IT・統計の力で増強し、財務データと市場環境データをエビデンスとした **投資判断モデルを自分で構築・改善し続けるためのプラットフォーム**。

感覚や経験に頼った投資判断から、データドリブンな投資判断へシフトすることがゴール。分析手法・評価手法を自作・改善できる自由度がこのツールの核心。

> 想定ユーザーは **作者自身のみ**（限定公開・認証あり）。不特定多数への公開は想定していません。

---

## 主な機能

### データ収集
- **財務データ**: 金融庁 EDINET API から有価証券報告書 (XBRL) を取得し、BS/PL/CF を再分類して保存
- **株価データ**: stooq から現在株価、J-Quants から日次 OHLCV 履歴
- **業種データ**: JPX 公式の上場会社一覧 Excel から TSE 33 業種コードを補完
- **マクロデータ**: 為替・金利・指数・コモディティの 9 系列を stooq から日次取得

### 分析
- **OLS 回帰**: per-share 財務金額（EPS / BPS / DPS）から理論株価を推定（次元整合性を担保）
- **乖離分析**: 実勢価格と予測価格のギャップから割安・割高ランキング、AR(1) MLE による収束予測
- **Zスコア正規化**: 年度内で業種を跨いだ相対順位
- **業種固定効果**: 業種ダミー変数を入れた回帰で業種別の P/E・P/B 構造差を吸収
- **バックテスト**: 過去スコア上位 N 社の実績リターンを `stock_price_history` から検証
- **プラグイン方式**: `plugins/` 配下に関数を追加すれば自動で API・UI に表示される

### 運用
- **収集ジョブの SSE 進捗配信**: 長時間処理をリアルタイムでブラウザに通知
- **データ鮮度可視化**: ダッシュボードの「データ鮮度」カードと警告バナーで最終更新からの経過日数を表示
- **DB ビューア**: スキーマ・プレビュー・統計・リレーション・ドリルダウンをブラウザから直接確認
- **HMAC-SHA256 署名トークン認証**: タイミング攻撃対策・パスワードリセット用回復キー対応

---

## アーキテクチャ概要

```
┌────────────┐    HTTP/REST/SSE    ┌──────────────┐    HTTPS    ┌─────────┐
│ ブラウザ    │ ─────────────────→ │ FastAPI       │ ─────────→ │ EDINET  │
│ (6 画面)    │                    │ api.py        │            │ stooq   │
└────────────┘                    │ collector.py  │            │ JPX     │
                                  │ plugins/      │            │ J-Quants│
                                  └───────┬──────┘            └─────────┘
                                          │ SQLAlchemy
                                          ▼
                                  ┌──────────────────┐
                                  │ Supabase         │
                                  │ PostgreSQL       │
                                  └──────────────────┘
```

コンポーネント図・ER 図・シーケンス図・全 API エンドポイント一覧・ファイル役割表は
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) に集約。

---

## デプロイ環境

**本番稼働中**: [Render](https://render.com/)（Free Web Service）+ [Supabase](https://supabase.com/)（PostgreSQL）

| 項目 | 値 |
|---|---|
| ホスティング | Render Free（メモリ 512 MB・スピンダウン 15 分） |
| データベース | Supabase PostgreSQL |
| 起動コマンド | `uvicorn api:app --host 0.0.0.0 --port $PORT` |
| 自動デプロイ | `main` への push でトリガ |
| 自動収集 | GitHub Actions に統一。差分収集は `daily-incremental.yml`（UTC 18:00 / JST 03:00）、全件収集は `full-pipeline.yml` の `workflow_dispatch` |

運用ガイド・制約・既知の落とし穴は [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) に集約。

---

## ローカルセットアップ

```powershell
# 仮想環境
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# .env を作成（UTF-8 BOMなし。.env.example を参照）
#   EDINET_API_KEY=...
#   DATABASE_URL=postgresql://postgres:[PASSWORD]@db.[PROJECT].supabase.co:5432/postgres?sslmode=require
#   APP_PASSWORD=...
#   APP_SECRET_KEY=...
#   APP_RECOVERY_KEY=...
#   ALLOWED_ORIGIN=http://localhost:8000
#   JQUANTS_API_KEY=...  (任意)

# 起動
uvicorn api:app --reload
```

ブラウザで `http://localhost:8000/` を開く。初回は `/collection` から「全件収集」を実行してデータベースを構築する。

> **DB は Supabase に一本化済み**。Render 本番もローカル開発も同じ Supabase インスタンスを参照する設計に統一されている（移行設計の経緯は [`docs/archive/REFACTORING.md`](docs/archive/REFACTORING.md) を参照）。

### よく使うコマンド

```powershell
python collector.py --years 5            # 全件収集（5年分）
python collector.py --years 1 --max 10   # 動作確認用（10 社）
python collector.py --company E000001    # 特定企業のみ更新
python collector.py --market             # 株価のみ更新
python collector.py --incremental        # 差分収集（収集済みをスキップ）
python check.py                          # EDINET API 接続確認
pytest                                   # テスト実行
```

---

## ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/VISION.md`](docs/VISION.md) | プロジェクト方針・ロードマップ・サードパーティライブラリ採用基準 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | コンポーネント図・ER 図・シーケンス図・API エンドポイント一覧 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Render + Supabase 運用ガイド・既知の制約 |
| [`docs/MODELS.md`](docs/MODELS.md) | 分析モデルの数式・パラメータ・参考文献（DOI 付き） |
| [`docs/archive/IMPROVEMENTS.md`](docs/archive/IMPROVEMENTS.md) | これまでの改善履歴と検証ノート（archive） |
| [`docs/FUTURE_TASKS.md`](docs/FUTURE_TASKS.md) | 未実装の課題・改善案（Tier 別） |
| [`CLAUDE.md`](CLAUDE.md) | Claude Code（AI コーディングエージェント）向けの動作指示 |

---

## ライセンス

私的利用目的のため、ライセンスは設定していません。
