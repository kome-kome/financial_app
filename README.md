# 日本株財務分析ツール

ファンダメンタル分析を IT・統計の力で増強し、財務データと市場環境データをエビデンスとした **投資判断モデルを自分で構築・改善し続けるためのプラットフォーム**。

感覚や経験に頼った投資判断から、データドリブンな投資判断へシフトすることがゴール。分析手法・評価手法を自作・改善できる自由度がこのツールの核心。

> 想定ユーザーは **作者自身のみ**（限定公開・認証あり）。不特定多数への公開は想定していません。

---

## 何ができるか

- **集める**: 金融庁 EDINET の有価証券報告書（XBRL）・株価・マクロ指標を毎晩集め、自前の DB に蓄積する
- **分析する**: 集めたデータで自作の分析モデルを回し、理論株価との乖離（割安・割高）やリターン予測の順位を出す
- **検証する**: モデルの成績を過去データで検証し、昇格ゲートを通ったものだけを既定に採用する

データの取得元と処理の流れは [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)、各モデルの数式・前提・参考文献は [`docs/MODELS.md`](docs/MODELS.md) が正本です。README には書き写しません（取得元・系列の本数・画面の数は実装とともに変わり、書き写すと黙って古くなるため）。

---

## 構成の要点

- **正本（唯一の正しいデータ）はローカル PostgreSQL**（#503・[ADR-0038](docs/adr/0038-local-postgres-is-the-primary.md)）。収集・重い計算・バックアップは、すべてローカルのバッチ（夜間・平日日中・月次・週次）が回す
- **Render は閲覧専用の窓**。Supabase に残した断面（2026-08-07 時点）を読むだけで、正本の更新は反映されない
- **Supabase は「Render が読む断面」と「Storage のバックアップ置き場」**。ローカルから Supabase の Postgres へ書き戻す経路は持たない
- **接続先は `FINAPP_DB_TARGET` で切り替える**（既定 `local`）。`.env` に `DATABASE_URL` があるだけでは Supabase へは行かない。`prod` を明示するのは Render（`render.yaml`）だけ

```
┌──────────┐  HTTP   ┌────────────────┐  SQL   ┌──────────────────────┐
│ ブラウザ │ ──────→ │ ローカル       │ ─────→ │ ローカル PostgreSQL  │
│          │         │ FastAPI        │        │ （正本）             │
└──────────┘         └────────────────┘        └──────────▲───────────┘
     │                                                    │ 書き込み
     │               ┌────────────────┐  HTTPS  ┌─────────┴──────────┐
     │               │ 外部 API       │ ←────── │ ローカルのバッチ   │
     │               │ EDINET 等      │         │ 夜間・日中・月次   │
     │               └────────────────┘         └─────────┬──────────┘
     │                                                    │ 週次バックアップ
     │               ┌────────────────┐  SQL   ┌──────────▼───────────┐
     └─────────────→ │ Render         │ ─────→ │ Supabase             │
         HTTPS       │ （閲覧専用）   │  読取  │ 断面 ＋ Storage      │
                     └────────────────┘        └──────────────────────┘
```

- バッチの暦（起動日・時刻・窓・所要の実測）と外部サービスの無料プラン制約: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- コンポーネント図・ER 図・API エンドポイント一覧・ファイル役割表: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

---

## ローカルセットアップ

```powershell
# 1. 仮想環境
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. .env を作る（初回のみ・UTF-8 BOM なし）。キーの意味は .env.example に書いてある
Copy-Item .env.example .env

# 3. ローカル PostgreSQL にスキーマを作る（先にドライランで接続先を確かめる）
python -m scripts.setup_local_db            # ドライラン（何も変更しない）
python -m scripts.setup_local_db --apply    # 実行

# 4. 起動
uvicorn api:app --reload
```

- ローカル PostgreSQL の接続先は、未設定なら [`database.py`](database.py) の既定（`_LOCAL_DEFAULT_URL`）を使う。別の URL を使うときは `.env` に `DATABASE_URL_LOCAL` を書く（ローカル以外のホストを書くと import 時に `RuntimeError` で止まる）
- PostgreSQL のインストールと初期化の詳細は [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) の「ローカル PostgreSQL」節
- `scripts/` 配下は `python -m scripts.<名前>` で起動する（直接パスで実行すると import が解決しない）

ブラウザで `http://localhost:8000/` を開く。

### よく使うコマンド

- 夜間・日中・月次バッチ、収集、株価修復の全コマンド: [`.claude/skills/batch-ops/SKILL.md`](.claude/skills/batch-ops/SKILL.md)
- テスト: `pytest`（`pytest.ini` で `testpaths=tests` に固定）

---

## ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/VISION.md`](docs/VISION.md) | プロジェクト方針・ロードマップ・サードパーティライブラリ採用基準 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | コンポーネント図・ER 図・処理フロー・API エンドポイント一覧・ファイル役割表 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | バッチの暦・Render 運用・外部サービスの制約 |
| [`docs/MODELS.md`](docs/MODELS.md) | 分析モデルの数式・パラメータ・参考文献（DOI 付き） |
| [`docs/GOTCHAS.md`](docs/GOTCHAS.md) | 既知のハマりどころ（再現条件と回避手順） |
| [`CONTEXT.md`](CONTEXT.md) | ドメイン用語集 |
| [`docs/adr/`](docs/adr/README.md) | 設計判断の記録（ADR） |
| [`docs/FUTURE_TASKS.md`](docs/FUTURE_TASKS.md) | Issue 運用ガイドと設計制約（残タスクの正本は GitHub Issues） |
| [`docs/archive/IMPROVEMENTS.md`](docs/archive/IMPROVEMENTS.md) | これまでの改善履歴と検証ノート（archive） |
| [`CLAUDE.md`](CLAUDE.md) | Claude Code（AI コーディングエージェント）向けの動作指示 |

---

## ライセンス

私的利用目的のため、ライセンスは設定していません。
