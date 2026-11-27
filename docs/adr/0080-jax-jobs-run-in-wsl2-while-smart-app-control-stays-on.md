# jax を使う M-7 の仕事は WSL2 で回し、Smart App Control はオンのままにする

## Status

**accepted（2026-11-27）**。#1316（M-7 の計測と判定）。利用者の選択（2026-11-27・代案 1〜3 から 1）。

## Context

ローカルの Windows では Smart App Control（SAC：署名や評判の無いプログラム部品の読み込みを止める保護機能）が、
pip で入れた未署名の部品（jaxlib・lightgbm・psycopg2 の `.pyd`）をクラウドの評判で止めることがある。止まる部品は
毎回変わり、数時間で戻ることもある（9/1 から記録・詳細は [GOTCHAS.md「ローカルバッチの実行環境」](../GOTCHAS.md#ローカルバッチの実行環境windowsタスクスケジューラ)）。

2026-11-23 の午前から jaxlib が止まったまま戻らず、M-7（[ADR-0079](0079-transformer-sequence-model-joins-as-comparison.md)）の
事前登録の計測 `measure:m7-transformer` を積めない状態が 5 日続いた（11/27 07:44〜07:46 も `jaxlib\mlir\_mlir_libs\_sdyMpmd.pyd`）。

- SAC には個別の例外（特定のファイルだけ許可）が無い。Microsoft の FAQ にも「個別アプリのバイパス手段は無い」とある
- 2026-11-22 に「SAC をオフ／WSL2／App Control for Business を自前で組む／現状維持」を比べ、現状維持を選んでいた
- 新しい Windows（Insider 26220.7070 以降）では SAC をクリーンインストール無しでオンへ戻せると公表されたが、段階配信で確実ではない

## Considered Options

1. **jax を使う仕事だけ WSL2（Ubuntu 24.04）の venv で回す（採用）**: SAC は Windows の部品を見る仕組みで、Linux の中の
   部品は止めない（2026-11-27 に WSL では `check_heavy_imports` が exit 0）。DB は Windows 側のまま、mirrored の
   ネットワークで `127.0.0.1` から読む。SAC はオンのまま
2. 必要な期間だけ SAC をオフにする: 即日で済むが、その間は署名の無いプログラムも動く。オンへ戻せない版なら恒久的なオフになる
3. 期限を決めて待つ: 12/2 の月次 macro_beta（jax を使う）も落ちうる。12 月は日中枠が混む
4. M-7 を PyTorch 等で書き直す: PyTorch も pip の未署名部品で、同じ遮断を受けない保証が無い。事前登録の凍結し直しも要る
5. GitHub Actions で回す: 正本の DB はローカルにしか無く、計算のデータをリポジトリの外へ出すことになる
6. jax の版を変える: 評判はファイルごとに付くので、新しい版は評判ゼロから始まる

## Decision

- `run_daytime.Job` に `runner`（`native` 既定・`wsl`）を足し、`measure:m7-transformer` と `model:macro_transformer` を `wsl` にする
- WSL の仕事は `batch_common.wsl_argv` で `wsl.exe -d Ubuntu-24.04 --cd <リポジトリの /mnt/c パス> --exec sh -c 'exec "$HOME/finapp-venv/bin/python" "$@"'` として起動する。
  **コードは元のフォルダ（main）をそのまま読み**、venv だけ Linux のホーム（`~/finapp-venv`）に置く。ユーザー名はリポジトリへ書かない
- 依存の確認は Linux 側の `deps_smoke_wsl` で行う。Windows の仕事と並ぶ日は `DEPS_SMOKE_MIN` を 2 本で等分する（窓から引いた枠を増やさない）
- 日中枠はセッション0（S4U）で走るので、対話の画面で WSL が動いてもタスクから動くとは限らない。経路そのものを確かめる
  軽い仕事 `check:wsl` を置き、WSL・venv を作り直したら単独で積んで `-Now` で叩く
- 子へ `TZ=Asia/Tokyo` を足し、`FINAPP_*`・`PYTHONUNBUFFERED`・`PYTHONIOENCODING`・`TZ` を `WSLENV` で渡す（締切が届かないと子が自分で畳めない・ADR-0054）
- 予算超過の打ち切りでは `kill_tree` の後に `wsl --terminate Ubuntu-24.04` を呼ぶ（`wsl.exe` を落としても Linux 側の計算は残りうる）
- Python は render.yaml・CI と同じ 3.13.7 を python.org のソース（署名を照合）からビルドし、`requirements-inference.txt` の固定版を入れる

## Consequences

- 良くなること: SAC の評判の揺れに左右されずに M-7 を回せる。SAC はオンのまま
- 環境が 2 つになる。依存の版を上げたら WSL の venv も入れ直す必要がある（ずれを止める照合は別 Issue）
- Windows と Linux で数値の結果がわずかに変わりうる。M-7 は実データの結果がまだ無いので比べる相手が変わらないが、
  過去の結果と比べる macro_beta 等は**差を測ってから**移す（別 Issue）。それまで 12/2 の月次 macro_beta は Windows のまま
- `wsl --terminate` は Linux 側の全プロセスを止める＝打ち切りの日は人が開いた WSL の画面も閉じる
- heartbeat のメモリ表示は WSL の仕事では出さない（Windows 側のツリーには中継しか見えない）。Linux 側の上限は
  `%USERPROFILE%\.wslconfig` の `memory=5GB`（PC は 7.9GB）
- WSL・Ubuntu・venv のどれかが壊れると `deps_smoke_wsl` が先頭で落ち、日中枠の失敗の起票に乗る
