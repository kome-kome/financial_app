# バリュエーション集約・OLSエンジン1本化・バックテスト一般化

## Status

accepted（2026-06-21）

## Context

分析手法を「一次分析 / 双対 / メタ検証」の3層モデル（CONTEXT.md「分析の階層」）で整理した結果、次の構造的な重複・非対称が判明した。

- **OLSエンジンの二重化**: `total_return`（総合リターン予測）と `sector_ols`（業種別OLS）が、いずれも per-share Ohlson 型 OLS で「理論株価 → 乖離」を算出していた。`total_return` は全市場プール回帰（EPS/BPS/CF/DPS 固定4特徴量＋任意の業種ダミー）、`sector_ols` は業種別個別回帰（リッチ特徴量・`regression_results` へ `gap_ratio` を永続化）。`total_return` の `upside_pct` と `sector_ols→gap_analysis` の `gap_ratio` は数式的に同一（(予測−実績)/実績）。
- **メタ層だけが未一般化**: バックテスト（`api.py`）は `recommend` のスコアをハードコードしていた。一方で双対層（`sell_ranking`）は既に recommend 品質因子＋バリュエーション `gap_ratio` の逆＋モメンタムを合成しており、横断的に一般化済みだった。「バックテスト＝おすすめ専用」「売り＝おすすめの逆」という当初認識のうち、後者はコードと食い違っていた。

## Decision

1. **OLSエンジンを `sector_ols` 1本へ集約**。`total_return` の独自OLSを廃止し、`sector_ols` の `gap_ratio` seam を消費する。
2. **`total_return` プラグインを廃止し、旧 `gap_analysis` を「バリュエーション分析」へ改名・拡張**して吸収する。バリュエーション分析は `gap_ratio`（割安度）・AR(1)半減期（平均回帰タイミング）・期待総リターン（gap＋配当利回り）・implied P/E・P/B（予測株価÷EPS・BPS）を一括で出すバリュエーション系の唯一のハブとする。
3. **バックテストを scoring source でパラメータ化**。`recommend / valuation / net_cash / sell` を同一土俵（rank→上位/下位N社→実現リターン）で検証できるようにする。ML系（`price_predictor` / `macro`）は WF-CV を内蔵するため対象外。
4. **`sell_ranking` に net_cash クッション消失を売り軸として追加**（清原式の逆観点）。

## Considered Options

- **両OLSを残し役割を明文化**（却下）: `total_return`=買いスクリーニング、`sector_ols`=割安度×タイミングと問いは違うが、エンジン二重保守のコストが上回ると判断。出力の主要部（gap）が同一で、配当・総リターン列を `gap_analysis` 側へ足せば `total_return` を完全再現できるため集約を選択。
- **`total_return` を `gap_analysis` の表示列に縮退**（一部採用）: ランキング機能は吸収するが、意思決定（買い候補ランキング）として独立した価値を持つため、バリュエーション分析の出力の一部（期待総リターンソート）として残す。

## Consequences

- バリュエーション分析が `sector_ols` の consumer になる（`depends_on=["sector_ols"]`）。`sector_ols` はローカル実行のため、サーバ単独でのスタンドアロン実行性を失う（既存の `gap_analysis` と同じ制約）。
- プール回帰の係数解釈（β ≈ 市場全体の implied 倍率）は失われる。業種別係数で代替（むしろ精緻化）。
- バリュエーション分析の責務が増える（gap＋半減期＋配当＋総リターン）。「乖離分析」名はこの責務に対し狭いため改名する。
- バックテストが「どのスクリーニング手法が実際に効いたか」を比較できる検証基盤になる（メタ層の本来価値）。

実装タスクは GitHub Issues（残タスクの正本）で追跡する。
