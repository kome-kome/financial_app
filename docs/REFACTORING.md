# リファクタリング設計書 — DB 一本化と XBRL 生データ保存

> **ステータス**: ドラフト（実装前のレビュー用）
> **作成日**: 2026-05-22
> **対象ブランチ**: `claude/net-cash-analysis-metrics-LkubW`
>
> 本書は以下の 2 つの構造改善の設計提案である。実装は本書のレビュー承認後に着手する。
>
> 1. **DB 一本化** — ローカル PostgreSQL を廃止し、Supabase に統合する
> 2. **XBRL 生データ保存** — 新指標追加時の EDINET 再ダウンロードを不要にする中間テーブルを追加する

---

## 目次

1. [課題の整理](#1-課題の整理)
2. [改善方針](#2-改善方針)
3. [DB 一本化（Supabase）の詳細設計](#3-db-一本化supabaseの詳細設計)
4. [XBRL 生データ中間テーブルの詳細設計](#4-xbrl-生データ中間テーブルの詳細設計)
5. [段階的な実装計画](#5-段階的な実装計画)
6. [リスクと対策](#6-リスクと対策)
7. [採択しなかった代替案](#7-採択しなかった代替案)

---

## 1. 課題の整理

### 1.1 デプロイ用とローカル用の混在

調査結果: **コアコードは共通、起動方法のみ別**で混在は最小限。

| ファイル | 用途 | 区分 |
|---|---|---|
| `render.yaml`, `Procfile` | Render デプロイ | クラウド専用 |
| `launch.py` | Windows ローカル起動（`venv\Scripts\python.exe`・tkinter） | ローカル専用 |
| `api.py`, `collector.py`, `database.py`, `plugins/*` | アプリ本体 | **共通**（環境変数で切替）|

切り替えポイントは `DATABASE_URL` のみ:
- `database.py:31` `_is_local = "localhost" in DATABASE_URL` → SSL とコネクションプールサイズを分岐
- それ以外（CORS の `ALLOWED_ORIGIN`、シークレットキー等）も全て環境変数経由

→ **構造的な混在問題は無い**。混乱の元は次節（1.2）の DB 二重管理である。

### 1.2 ローカル DB と Supabase の並存（整合性リスク）

`CLAUDE.md` は「永続化はすべて Supabase」と謳う一方、`.env.example` のテンプレは `postgresql://edinet:edinet@localhost:5432/financial_db`。実態として **2 つの DB が並存**している。

#### 順序依存ロジック（DB ごとに結果が変わりうる箇所）

| 箇所 | 順序依存の中身 | 影響 |
|---|---|---|
| `collector.py:163-196` `fetch_edinet_code_list` | 直近 400 日を逆順スキャンしながら `companies[code] = {…}` で **dict 上書き** | 同じ企業の名称・証券コードが直近書類で上書きされる。連結子会社化等で名称が変わった企業の表示名が、収集タイミングで違いうる |
| `collector.py:278-310` `collect_doc_ids_for_period` | `max_companies` 指定時、`seen_order` の **rank < N で先着 N 社に絞り込み** | 同じ期間スキャンでも、別 DB で `max_companies` を別タイミングで指定すると採用銘柄が違う |
| `database.py:_calc_zscore_for_year` | 母集団の社数で μ・σ が変わる | DB 間で **全 Z スコアが微妙に違う** → `recommend.py` のランキング、`gap_ratio` まで連鎖して違う |
| `sector_ols.py:194-262` | 業種ごとに `winsorize → z-score → OLS` を回す。母集団が違えば係数も違う | DB 間で `predicted_market_cap` が違う |

→ 2 つの DB は **「同じデータを違うタイミングで集めた近似コピー」** であって、厳密な同期コピーではない。ユーザーの危惧は正しい。

### 1.3 XBRL 生データの非保存

`database.py:179` の `raw_xbrl_json` カラムは名前に反して **既に `XBRL_MAP` で抽出済みの BS/PL/CF dict のみ**を保存している。

```python
# database.py:319-324（現状）
flat["raw_xbrl_json"] = {
    "bs": data.get("bs", {}),   # ← 抽出済みの値のみ
    "pl": data.get("pl", {}),
    "cf": data.get("cf", {}),
}
```

EDINET から取得した ZIP・XBRL CSV は `collector.py:fetch_xbrl_csv` 内で `pd.DataFrame` に展開した後、`parse_xbrl_csv` で `XBRL_MAP` のキーだけ抽出してメモリから破棄される。**全行は永続化されていない**。

#### 影響

- 新指標を追加するたびに（前回の `bs_investment_securities` がまさに該当）、全企業 × 全年度分の XBRL ZIP を **EDINET から再ダウンロード**しなければならない
- EDINET API は無料だがレート制限あり。仮に 4,000 社 × 5 年 = 20,000 書類を再収集すると、`RATE_SLEEP=1秒` でも約 5.5 時間
- Render Free プランの「15 分アイドルでスピンダウン」と相性が悪い（途中で止まる）
- ユーザー視点では「指標を増やすたびに 1 日仕事」になる

---

## 2. 改善方針

ユーザーの選択に基づき以下 2 軸で進める:

| 軸 | 採用案 | 理由 |
|---|---|---|
| DB 二重管理 | **Supabase に一本化** | データ整合性問題が根本的に消える。Supabase の容量（無料 500MB）も日本株 4,000 社の構造化データなら十分 |
| XBRL 生データ保存 | **中間テーブル追加** | 新指標追加で再 fetch 不要にする。ZIP 丸ごと保存はサイズ過大 / `raw_xbrl_json` の単純拡張は context 情報が落ちて連結/非連結が区別できない |

両者は独立して実装可能。`Phase 1 → Phase 2` の順で段階導入する（詳細は §5）。

---

## 3. DB 一本化（Supabase）の詳細設計

### 3.1 目標状態

```
[開発者の手元 PC] ─── DATABASE_URL=postgresql://...supabase.co:6543/... ──→ [Supabase PostgreSQL]
                                                                                ↑
[Render 本番]    ─── DATABASE_URL=postgresql://...supabase.co:6543/... ──→ (同じインスタンス)
```

- **DB は 1 つだけ**。Render と開発者 PC は同じ Supabase インスタンスを参照する
- 開発・実験は Supabase の **branch DB**（無料プラン 1 ブランチまで）で行う想定
- **現状ローカル DB の方がデータが多いため、まずローカル→Supabase へ同期アップロードしてから**
  ローカルを停止する（詳細は §3.2.3）

### 3.2 やること

#### 3.2.1 設定変更

- `.env.example` のテンプレを `localhost` から Supabase 形式に変更
  ```
  # Before
  DATABASE_URL=postgresql://edinet:edinet@localhost:5432/financial_db
  # After
  DATABASE_URL=postgresql://postgres:[YOUR_PASSWORD]@db.[YOUR_PROJECT].supabase.co:5432/postgres?sslmode=require
  ```
- `database.py:30-33` の `_is_local` 分岐を残す（CI のテスト DB は localhost 想定）。本番は常に非ローカル

#### 3.2.2 ドキュメント整備

- `CLAUDE.md` — 「ローカル開発時の DB 接続手順」セクションを追加
- `docs/DEPLOYMENT.md` — 「開発者 PC からの Supabase 接続」と「ローカル DB を廃止する移行手順」を追加
- `README.md` — セットアップ手順を Supabase 前提に書き換え

#### 3.2.3 ローカル DB を「マスター」として Supabase へ昇格させる移行手順

**前提の見直し**: 当初は「Supabase が現マスター・ローカルは捨ててよい」前提だったが、実態としては
**ローカル DB の方が件数が多く、より完全なデータセット**である。よってこちらをマスターとして
Supabase へ取り込む方針に変更する。EDINET 再収集（5〜10 時間）を回避できるため運用上も合理的。

##### Step 1: 件数比較（移行前の現状把握）

ローカル / Supabase 双方で以下のクエリを叩き、テーブル別の件数を表で記録する:

```sql
SELECT 'companies'           AS tbl, COUNT(*) FROM companies
UNION ALL SELECT 'financial_records',     COUNT(*) FROM financial_records
UNION ALL SELECT 'stock_price_history',   COUNT(*) FROM stock_price_history
UNION ALL SELECT 'collection_logs',       COUNT(*) FROM collection_logs
UNION ALL SELECT 'macro_data',            COUNT(*) FROM macro_data;
```

期待される結果イメージ:

| テーブル | ローカル | Supabase | 採用 |
|---|---|---|---|
| companies | 4,000 | 3,800 | ローカル |
| financial_records | 20,000 | 14,000 | ローカル |
| stock_price_history | 2,000,000 | 1,500,000 | ローカル |
| macro_data | 30,000 | 28,000 | ローカル |

差分の出ている箇所を見て、本当にローカル優位か（あるいは Supabase だけにある新規データが
無いか）を確認する。

##### Step 2: マージ戦略の選択

| 戦略 | 内容 | 推奨度 |
|---|---|---|
| **A. 全置換** | Supabase のテーブルを truncate → ローカル全 dump を restore | ★★★（推奨）|
| B. upsert マージ | `ON CONFLICT DO UPDATE` で重複キーは上書き、新規のみ insert | ★★ |
| C. 差分のみ追加 | Supabase に無いレコードだけ insert | ★ |

**推奨は A**（全置換）。理由:
- ローカルが「より新しく完全」と確認できているなら、Supabase の現データに保護価値は薄い
- upsert は集計テーブル（Zスコア・成長率）の整合性確保が複雑になりがち
- 全置換なら DB の状態を「ローカルのスナップショット時点」に確定でき、デバッグも容易

ただし以下の場合は B / C を検討:
- Supabase 側にだけ存在する最近の `market_data`（Render の自動収集分）がある
- ローカル側でテスト用に投入したダミーデータが含まれている可能性がある

##### Step 3: pg_dump → psql restore（戦略 A の場合）

```bash
# ローカルからフルダンプ（--column-inserts で順序耐性を高める）
pg_dump --no-owner --no-acl --data-only --column-inserts \
  --table=companies --table=financial_records \
  --table=stock_price_history --table=collection_logs --table=macro_data \
  postgresql://edinet:edinet@localhost:5432/financial_db \
  > local_dump.sql

# Supabase へ投入（先に対象テーブルを truncate）
psql "$SUPABASE_DATABASE_URL" <<SQL
TRUNCATE companies, financial_records, stock_price_history,
         collection_logs, macro_data
         RESTART IDENTITY CASCADE;
SQL

psql "$SUPABASE_DATABASE_URL" < local_dump.sql
```

注意点:
- `--data-only`: schema は `init_db()` で作られているため二重定義を避ける
- `--no-owner --no-acl`: Supabase は権限管理が独自なので owner / GRANT を持ち込まない
- `xbrl_raw_documents`（Phase 2 追加予定）が完成後の本番移行では、これも dump 対象に加える

##### Step 4: 整合性チェック

```sql
-- Step 1 と同じ件数クエリを Supabase で叩いて、ローカルと一致するか確認
-- 加えて以下の sanity check:
SELECT MAX(period_end), MIN(period_end), COUNT(DISTINCT edinet_code)
  FROM financial_records;
SELECT MAX(date), MIN(date), COUNT(DISTINCT edinet_code) FROM stock_price_history;
SELECT MAX(date), MIN(date), COUNT(DISTINCT series)      FROM macro_data;
```

##### Step 5: Zスコア・成長率の再計算

DB の母集団が変わった以上、`calc_zscore_normalization` / 成長率計算は **必ず再実行**する
（CLAUDE.md の「Zスコアは年度別」原則に従い、年度ごとに）。Web UI の「再計算」ボタン
または `database.py:_calc_zscore_for_year` を直接呼ぶスクリプトで実施。

##### Step 6: Render の挙動確認

Supabase に直接書き込んだ後、Render の本番アプリを再デプロイ（あるいは `/health` を叩いて
スピンアップ）してダッシュボードが正常表示されるか確認。

##### Step 7: ローカル DB の停止・削除

整合性確認後、開発者 PC のローカル PostgreSQL サービスを停止し、`.env` の `DATABASE_URL` を
Supabase に切り替える。データボリュームの削除は **1〜2 週間運用して問題が無いことを
確認してから**にする（即削除しない・ロールバック余地を残す）。

---

これらをワンショットで実行するスクリプトを `scripts/migrate_local_to_supabase.py` として
用意する（dry-run モード・件数比較レポート出力・確認プロンプト付き）。

##### 実行環境について

Web 版 Claude Code（claude.ai/code 上のリモートコンテナ）は**ローカル PostgreSQL にも
本番 Supabase にも IP 到達できない**ため、上記スクリプトの実行は次のいずれかで行う:

- **VS Code 版 / デスクトップ版 Claude Code** — 手元 PC 上で動く Claude Code から
  `.env` を読み、`python scripts/compare_db_counts.py` や
  `python scripts/migrate_local_to_supabase.py --dry-run` を起動できる
- **ターミナル手動実行** — 同じスクリプトを `venv\Scripts\python.exe` で直接実行する

Web 版 Claude Code（このセッション）の役割は **コード・ドキュメント・スクリプト雛形を
ブランチに push するところまで**。実 DB に触れる段階は必ず手元側で実行する。

### 3.3 やらないこと（スコープ外）

- マルチテナント対応（個人ツールなので単一ユーザー前提を維持）
- 読み取り専用レプリカ・分散構成
- DB スイッチング（環境変数 1 本で十分）

---

## 4. XBRL 生データ中間テーブルの詳細設計

### 4.1 目標状態

新しい指標を追加するときの流れ:

```
【現状】
  XBRL_MAP 追加 → EDINET から全企業ZIP再取得（数時間〜） → parse → DB

【目標】
  XBRL_MAP 追加 → ローカルの中間テーブルから re-parse（数分） → DB
                  ↑ EDINETへの追加リクエスト 0 件
```

### 4.2 新テーブル `xbrl_raw_elements`

EDINET から取得した XBRL CSV の **全行**を構造化 JSON で保持する。1 書類につき 1 レコード（小サイズ）。

```python
class XbrlRawDocument(Base):
    """EDINET XBRL CSV の生データ。新指標追加時に再 parse する用。
    1 書類につき 1 レコード。doc_id は EDINET 書類管理番号で UNIQUE。
    """
    __tablename__ = "xbrl_raw_documents"
    __table_args__ = (
        UniqueConstraint("doc_id", name="uq_xbrl_raw_doc_id"),
        Index("ix_xbrl_raw_edinet_period", "edinet_code", "period_end"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    doc_id      = Column(String(20), nullable=False, index=True)   # EDINET 書類管理番号
    edinet_code = Column(String(10), nullable=False, index=True)
    period_end  = Column(String(20))                                # 決算期末
    fetched_at  = Column(DateTime, default=datetime.utcnow)

    # 構造化 JSON: [{"element": str, "context": str, "value": float|str, "unit": str|None}, ...]
    elements    = Column(JSON, nullable=False)

    # 元の XBRL CSV の行数（健全性チェック用）
    n_rows      = Column(Integer)
```

#### サイズ見積もり

- 1 書類あたり XBRL 要素: 約 1,000〜5,000 行（実測必要）
- 1 要素あたり JSON: `{"element": "OperatingProfit", "context": "CurrentYearDuration_jpcrp_…_Member", "value": 1234567890, "unit": "JPY"}` ≈ 150 バイト
- 1 書類あたり: 150 KB〜750 KB（圧縮前）
- PostgreSQL JSONB は圧縮が効き、平均 30〜50 KB と推定
- 4,000 社 × 5 年 = 20,000 書類 → **600 MB〜1 GB**

→ Supabase 無料枠（500 MB）を超える可能性が高い。**圧縮戦略が必要**。

### 4.3 圧縮戦略

選択肢:

| 案 | 内容 | サイズ削減 | 実装難度 |
|---|---|---|---|
| A | JSONB のまま（PG が自動圧縮）| 30〜50% | 楽 |
| B | gzip した bytes で保存（`BYTEA`）| 70〜85% | 中（透過アクセスのため `cf_compression` ヘルパが必要）|
| C | context を別テーブル化（正規化）| 50%（重複削減）| 中（join が増える）|
| D | A + 古い書類の自動アーカイブ（5年超を別 schema）| 設計次第 | 高 |

**推奨**: **B（gzip + BYTEA）**。サイズ削減効果が高く、re-parse 時のメモリ展開コストも低い。実装は `gzip.compress(json.dumps(elements).encode())` の 1 行。

修正後のスキーマ:

```python
class XbrlRawDocument(Base):
    __tablename__ = "xbrl_raw_documents"
    ...
    # gzip 圧縮済み JSON（取り出し時に gzip.decompress → json.loads）
    elements_gz = Column(LargeBinary, nullable=False)
    elements_format = Column(String(10), default="gzip+json")   # 将来の方式変更用
```

ヘルパ:

```python
def pack_elements(rows: list[dict]) -> bytes:
    """[{element, context, value, unit}, ...] を gzip(JSON) に圧縮"""
    return gzip.compress(json.dumps(rows, ensure_ascii=False).encode("utf-8"))

def unpack_elements(blob: bytes) -> list[dict]:
    return json.loads(gzip.decompress(blob).decode("utf-8"))
```

### 4.4 collector.py の改修

`fetch_xbrl_csv` の戻り値（pandas DataFrame）を「構造化 dict のリスト」に変換し、**parse_xbrl_csv の前**で `xbrl_raw_documents` に保存する。

```python
# 追加: parse 前に raw 保存
def df_to_raw_rows(df, doc_id, edinet_code, period_end) -> list[dict]:
    """XBRL CSV DataFrame を [{element, context, value, unit}, ...] に変換"""
    rows = []
    col_map = _detect_columns(df)  # 既存ロジックを抽出
    for _, row in df.iterrows():
        rows.append({
            "element": str(row[col_map["element"]]).split(":")[-1],
            "context": str(row.get(col_map.get("context", ""), "")),
            "value":   str(row[col_map["value"]]),   # 文字列のまま保持（型推論は parse 時）
            "unit":    str(row.get(col_map.get("unit", ""), "")) or None,
        })
    return rows

# upsert ヘルパ
def upsert_xbrl_raw(db, doc_id, edinet_code, period_end, rows):
    obj = db.query(XbrlRawDocument).filter_by(doc_id=doc_id).first()
    blob = pack_elements(rows)
    if obj is None:
        obj = XbrlRawDocument(
            doc_id=doc_id, edinet_code=edinet_code, period_end=period_end,
            elements_gz=blob, n_rows=len(rows),
        )
        db.add(obj)
    else:
        obj.elements_gz = blob
        obj.n_rows = len(rows)
        obj.fetched_at = datetime.utcnow()
```

### 4.5 再 parse 用 CLI / API

新指標を追加した後、EDINET から再 fetch せずに DB 上のデータで `financial_records` を再構築する:

```
python collector.py --reparse                # 全書類を再 parse
python collector.py --reparse --year 2024    # 特定年度のみ
python collector.py --reparse --company E000001
```

実装:

```python
def reparse_from_raw(db, year=None, edinet_code=None, on_progress=None):
    """xbrl_raw_documents から financial_records を再構築する"""
    q = db.query(XbrlRawDocument)
    if year:
        q = q.filter(XbrlRawDocument.period_end.like(f"{year}%"))
    if edinet_code:
        q = q.filter(XbrlRawDocument.edinet_code == edinet_code)
    docs = q.all()
    for i, doc in enumerate(docs, 1):
        rows = unpack_elements(doc.elements_gz)
        parsed = parse_raw_rows(rows)            # 既存 parse_xbrl_csv の中核ロジックを切り出し
        rec = calc_derived({"bs": parsed["bs"], "pl": parsed["pl"], "cf": parsed["cf"], ...})
        upsert_financial(db, {...rec, doc_id: doc.doc_id, edinet_code: doc.edinet_code, ...})
        if on_progress:
            on_progress(i, len(docs), f"re-parse: {doc.edinet_code} {doc.period_end}")
```

#### Web UI 連携

`templates/collection.html` の収集タブに「**再 parse のみ実行**」ボタンを追加。EDINET には叩かないため Render Free でも 30 秒以内に終わる規模で完結する想定（年度単位の進捗を SSE 配信）。

### 4.6 既存 `raw_xbrl_json` カラムの扱い

`financial_records.raw_xbrl_json`（既に抽出済みの dict のみ）は重複情報になる。

**段階的廃止**:
1. Phase 2 で `xbrl_raw_documents` を追加（既存カラムはそのまま）
2. 動作確認・全件再収集後、Phase 3 で `raw_xbrl_json` カラムを drop するマイグレーション

### 4.7 やらないこと（スコープ外）

- ZIP 生バイナリの保存（サイズ過大）
- 1 要素 = 1 レコードへの正規化（pivot 効率は良いが Supabase 行数制限 50K/月 を圧迫する）
- 他社の財務 API との統合（J-Quants 等は別仕組み）

---

## 5. 段階的な実装計画

| Phase | 内容 | 後方互換 | 想定工数 |
|---|---|---|---|
| **P1** | DB 一本化のドキュメント整備（`CLAUDE.md` / `DEPLOYMENT.md` / `README.md`）+ 件数比較レポート出力 | 完全互換 | 半日 |
| **P2** | `migrate_local_to_supabase.py` 実装（dry-run・件数比較・全置換戦略）→ **ローカル → Supabase へ同期アップロード実行** | データ追加あり | 1 日 |
| **P3** | 同期後の Zスコア・成長率の再計算 + 整合性チェック + Render での動作確認 | 完全互換 | 半日 |
| **P4** | `XbrlRawDocument` テーブル追加 + `collector.py` で raw 保存（**ここから新規収集分に raw が蓄積**）| **完全互換**（既存 financial_records も同時に書く）| 1 日 |
| **P5** | `reparse_from_raw` の CLI + `templates/collection.html` に「再 parse」ボタン追加 | 完全互換 | 1 日 |
| **P6** | 1〜2 週間運用観察後、ローカル PostgreSQL 停止 + `.env` から削除（**データボリュームは即削除しない**）| 互換破壊（ローカル DB が使えなくなる）| 半日 |
| **P7** | `financial_records.raw_xbrl_json` カラム drop（drop migration、`xbrl_raw_documents` で代替可能になってから）| 互換破壊（小） | 半日 |

各 Phase は独立した PR とし、PR 単位で動作確認 → マージする。

#### この順序の理由

- **P2 を最優先**にしたのは、ローカル DB の方が件数が多く完全なため。先に Supabase を
  ローカル相当に底上げすれば、以降は「Supabase = マスター」として安心して扱える
- 旧計画にあった「P5: 全件再収集」は不要になった。ローカルの既存データをそのまま昇格できるため
  EDINET 再ダウンロード（5〜10 時間）を回避できる
- P4 の XBRL raw 蓄積は、移行が終わってから始めても問題ない。**新規収集分から徐々に貯まり、
  古い書類は必要に応じて re-fetch で埋める**運用にする
- P6 のローカル削除は最後。**1〜2 週間の観察期間**を挟んで、Supabase だけで運用上の問題が
  無いことを確認してから実施する（ロールバック余地を残す）

---

## 6. リスクと対策

### 6.1 Supabase 容量超過

| リスク | 対策 |
|---|---|
| XBRL 中間テーブルで無料 500MB を超える | gzip 圧縮で 70〜85% 削減（§4.3）。それでも超えたら過去 5 年超の書類を別 schema にアーカイブ |
| Supabase の API レート制限超過 | コネクションプール（既に `pool_size=3, max_overflow=5`）+ 同時実行を 1 ジョブに制限 |

### 6.2 移行中のデータ整合性

- **P2 → P5** の間は raw が部分的にしか無い → reparse は **データがある書類のみ**実行する設計（`docs = db.query(XbrlRawDocument).all()` で対象を限定）
- **P5** の全件再収集は 5〜10 時間かかる想定 → Render 上では `BackgroundTasks` + SSE で実行できるが、Render Free のスピンダウン制限（15 分）に注意。実用上は開発者 PC から `python collector.py --years 5` を一晩走らせる方が現実的

### 6.3 EDINET API の仕様変更

- XBRL 要素名や CSV エンコーディングが将来変わると raw の構造もずれる
- 対策: `xbrl_raw_documents.elements_format` カラムでバージョン管理。互換性が壊れたら新フォーマットに切り替え、旧データは古いコードで読む

### 6.4 開発者 PC が複数ある場合

- 全員が同じ Supabase を参照すると、誰かのテスト書き込みが他に波及する
- 対策: Supabase の **branch DB**（PR 単位のスナップショット）を活用するか、テスト時は `DATABASE_URL` を `sqlite:///:memory:` に切り替える（既存テストはこの方式）

---

## 7. 採択しなかった代替案

### 7.1 ZIP 生バイナリの保存（XBRL 案 B）

- メリット: 完全な生データ。EDINET の仕様変更があっても全て遡れる
- デメリット: 1 書類 1〜10 MB、合計 50〜200 GB。Supabase 無料枠を 2 桁超過
- → 却下

### 7.2 raw_xbrl_json の軽量拡張（XBRL 案 C）

- メリット: スキーマ追加不要
- デメリット: context 情報が落ちて連結 / 非連結 / セグメント別が区別できない。priority resolution（`parse_xbrl_csv:354-373`）が再現できない
- → 却下

### 7.3 ローカル DB を残し、Supabase と双方向同期

- メリット: オフライン開発可能
- デメリット: 同期競合の解決ロジックが必要。マスターはどちらか問題が常に発生
- → 「マスターは Supabase、ローカルは廃止」の方針で却下

### 7.4 DB を SQLite ファイル化

- メリット: 環境依存ゼロ
- デメリット: Render Free の永続ディスクなし問題が再発。Supabase の利点（JSONB・全文検索・ダッシュボード）を失う
- → 却下

---

## 改訂履歴

| 日付 | 内容 |
|---|---|
| 2026-05-22 | 初版作成（ドラフト・レビュー待ち）|
