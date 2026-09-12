---
name: batch-ops
description: >-
  financial_app のローカル起動・収集・バッチ実行コマンド集。uvicorn / launch.py の起動、
  夜間バッチ（run_nightly）、月次・macro_beta・M-1 の各バッチ、平日日中キュー（run_daytime）、
  バッチ鮮度 watchdog、バックアップ（run_backup）、collector の各種オプション、
  株価修復スクリプト（repair_splits_from_jquants / repair_scale_mixture / resolve_price_suffix）を
  叩くときに参照する。CLAUDE.md から移設した正本。
---

# 起動・実行コマンド

`CLAUDE.md` の同名節から移設した。コマンド文字列・コメント・Issue 番号・実測値は移設時点のまま。

```powershell
# ローカル（Windows）— 正本はローカル PostgreSQL（#503・ADR-0038）
./venv/Scripts/Activate.ps1
uvicorn api:app --reload                 # → http://localhost:8000/
python launch.py                         # GUI ランチャー（既定=ローカル正本）
./run_local.ps1                          # 接続先をローカルに固定して起動
./run_local.ps1 -Console -Port 8010      # 同上・ランチャー無しでコンソール起動

# 夜間バッチ（収集 → スコア更新）。GHA の cron は #503 で全停止した
./run_nightly.ps1                        # 手動で1回
./run_nightly.ps1 -DryRun                # 実行計画だけ
./scripts/install_nightly_task.ps1       # タスクスケジューラへ登録（毎日 JST 17:20）

# バッチ鮮度 watchdog（走らなかったことを検知して起票）。#515・ADR-0042
python -m scripts.check_batch_freshness             # 判定（停止なら起票 + exit 2）
./run_watchdog.ps1 -DryRun                          # 起票せず本文だけ見る
./run_watchdog.ps1 -DryRun -Now 2026-08-28T00:00:00+00:00   # 欠落を再現（DB を汚さない）
./scripts/install_watchdog_task.ps1                 # タスクスケジューラへ登録（毎日 JST 20:00）

# 夜間バッチの収集ログを晩ごとに並べて読む（#556 の並行フェッチ・#620 のスケール選別）。DB に触らない
python -m scripts.check_nightly_collect             # 直近3晩・警告があれば exit 2
python -m scripts.check_nightly_collect --nights 5

# 月次バッチ（Fama-MacBeth 重み → M-1 マクロβ推論 → M-1/M-2/M-3 探索）。#504
./run_monthly.ps1                        # 手動で1回
./run_monthly.ps1 -DryRun                # 実行計画だけ
./run_monthly.ps1 -Steps factor_premia   # 一部だけ
./scripts/install_monthly_task.ps1       # タスクスケジューラへ登録（毎月1日 JST 01:00・上限16h）

# macro_beta は別タスク（毎月2日 JST 01:00）。実測 360分で月次本体の窓に入らない（#579）
./run_monthly_beta.ps1                   # 手動で1回
./run_monthly_beta.ps1 -DryRun           # 実行計画だけ
./run_monthly_beta.ps1 -Force            # 収束ゲートを無視して live で persist（人手で精査した1回だけ）
./scripts/install_monthly_beta_task.ps1  # 登録（毎月2日 JST 01:00・上限16h）。**登録後に1回手動実行して足跡を入れる**

# macro_beta の収束ゲートの余裕を run 横断で読む（#612）。読み取り専用・DB へ書かない
# **DB を手で引かない**（判定は本番の gate_verdict と共有される）。div=N / old-gate の印が付いた run は推移へ混ぜない
python -m scripts.macro_beta_gate_history            # 全変数の p99 と閾値までの余裕を古い順に
python -m scripts.macro_beta_gate_history --limit 10 # 読む run 数（既定 20）
python -m scripts.macro_beta_gate_history --threshold 1.01  # strict 基準で見直す

# M-1 探索も別タスク（毎月3日 JST 01:00）。実測 約752分で月次本体の窓に入らない（#584・ADR-0046）
./run_monthly_m1.ps1                     # 手動で1回
./run_monthly_m1.ps1 -DryRun             # 実行計画だけ
./scripts/install_monthly_m1_task.ps1    # 登録（毎月3日 JST 01:00・上限16h）。**登録後に1回手動実行して足跡を入れる**

# 平日日中バッチ（重い計算をキューから1日1件）。#618・平日 JST 08:00・窓8h
# **人が PC を触らない時間帯で回すこと自体が再現性の条件**（並走すると NUTS の発散が 0→344 に増えた実測）
./run_daytime.ps1 -Queue                 # キューの中身
./run_daytime.ps1 -Enqueue beta          # 積む（beta / tune:macro_gbdt / tune:macro_dlm / gate:interactions / interim / disclosures）
./run_daytime.ps1 -DryRun                # 実行計画だけ（キューは減らさない）
./run_daytime.ps1 -Now                   # 枠を待たず次の1件（休暇等）。**タスク経由＝セッション0で走る**
./run_daytime.ps1 -Now -Force            # 並走に敏感な仕事（beta / tune:* / gate:*）も叩く。**叩いたら PC を触らない**
./scripts/install_daytime_task.ps1       # 登録（平日 JST 08:00・上限8h）。**登録後に1回手動実行して足跡を入れる**

# バックアップ（週次バッチ・毎週日曜 JST 21:00。Storage は 50MB/ファイル・1GB。実測 38.1MB/世代）
./run_backup.ps1                                      # 手動で1回（ダンプ → Storage へ push）
./run_backup.ps1 -DryRun                              # 実行計画だけ
./scripts/install_backup_task.ps1                     # タスクスケジューラへ登録（毎週日曜 JST 21:00・上限2h）。**登録後に1回手動実行して足跡を入れる**
python -m scripts.backup_push --apply                 # ローカルに世代を作る
python -m scripts.backup_push --apply --dest storage  # Storage へ push
python -m scripts.backup_restore --apply --create-schema --dest-url <local-url>
python -m scripts.backup_restore --source storage --apply --create-schema --dest-url <local-url>  # Storage から

python _pipeline_incremental.py         # 差分収集（XBRL＋マクロ＋株価）＝鮮度の担い手
python collector.py --years 5           # 全件収集（5年分）
python collector.py --years 1 --max 10  # テスト用（10社）
python collector.py --company E02167    # 特定企業更新
python collector.py --market            # 株価のみ更新
python collector.py --incremental       # XBRL 差分のみ（**株価は更新しない**）
python collector.py --macro                              # マクロ全系列
python collector.py --macro --macro-series JP10Y_FRED    # 指定系列のみ（定義是正後の再収集用）
python collector.py --repair-price-breaks                # 週次株価の分割段差を検出（dry-run）
python collector.py --repair-price-breaks --persist      # 同上＋該当銘柄をYahooで取り直し検算

# Yahoo が遡及反映しない分割を公式(J-Quants AdjFactor)の裏付け付きで直す（#466）。既定はドライラン
python -m scripts.repair_splits_from_jquants                   # 検出→判定（書かない）
python -m scripts.repair_splits_from_jquants --only E03137     # 1社だけ（検出を省く）
python -m scripts.repair_splits_from_jquants --apply

# 1つの価格列に2つのスケールが混ざった帯を Yahoo で取り直して均す（#620）。既定はドライラン
# （株価は書かないが、突合で非該当と決まった帯は記録し、夜間の往復段差の警告から除く・#644）
python -m scripts.repair_scale_mixture                          # 候補（多いときは突合せず止まる）
python -m scripts.repair_scale_mixture --only E01332,E01717     # 夜間ログが警告した社を突合→判定を記録
python -m scripts.repair_scale_mixture --only E32779 --apply    # 公式突合で確定してから取り直す

# 地方取引所の単独上場を拾う（#555）。既定はドライラン＝棄却理由まで出す
python -m scripts.resolve_price_suffix                            # 何も書かない
python -m scripts.resolve_price_suffix --apply --backfill-weekly  # 採用＋5年weekly
python -m scripts.resolve_price_suffix --reprobe                  # 解決済みも測り直す
python -m scripts.resolve_price_suffix --apply --bucket empty     # 取引所判明・バー0本の5社だけ（月次が回す・#560）
python edinet_ping.py                    # EDINET API接続テスト
```

```bash
pytest                      # テスト全件
pytest tests/test_utils.py  # 単一ファイル
```
