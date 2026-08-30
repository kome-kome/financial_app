# 劣後したモデルは削除せず hidden で退役する（評価の結論を UI に反映し、比較の土俵は残す）

## Status

accepted（2026-08-30）。Issue [#570](https://github.com/kome-kome/financial_app/issues/570)。
[ADR-0015](0015-m4-ensemble-stacking.md)（M-4 の「統合が単体最良を上回らなければ単体で十分」）と
[ADR-0017](0017-m5-learning-to-rank.md)（M-5 の「上回らなければ MSE で十分」）が定めた判定を、
**UI と運用へ反映する手続き**を決める。[ADR-0021](0021-sibling-model-candidate-menu.md)
（並置して実データで決める）の逆向き＝並置をやめる側の作法。

## Context

「③ 将来リターンを予測」に M-1〜M-6 の6本が並んだ。増えた経路そのものは設計どおりで、
ADR-0021 が「候補は探索枠（`model_candidates`）に置き、昇格ゲートを通ったものだけ正式兄弟に
する」と定めたことで**入口は締まっていた**。締まっていなかったのは**出口**である。

昇格ゲートは「足すか否か」の検定しか持たず、いったん兄弟になったモデルを**降ろす手続きが
無かった**。結果、実測で決着がついた後もモデルはサイドバーに残り続けた:

| モデル | 決着 | それでも残っていた |
|---|---|---|
| M-4 `macro_ensemble` | 統合は M-6 単体を上回らない（rank-IC +0.0006・p=0.810／売り側 spread p=0.655・ADR-0015 の実測・2026-07-30） | サイドバー・`mu_source` 選択肢 |
| M-5 `macro_gbdt_rank` | producer なし（`produced_output=False`）で下流参照ゼロ。ADR-0017 が約束した実測は**未実施のまま**だった | サイドバー |

これは「壊れた」のではなく「決着が UI に届いていない」状態で、[ADR-0031](0031-heavy-plugins-require-registered-automation.md)
の「登録はあるが誰も回さない」と同型の非対称である——**失敗として現れないので通知では拾えない**。
実際、この6本のうち μ̂ が日次更新されているのは M-6 のみ（`nightly_scores.NIGHTLY_MODELS`）で、
M-4/M-5 は `HEAVY_AUTOMATION` で exempt＝**そもそも自動更新経路が無いまま UI に並んでいた**。

素直な対処は削除だが、それは 2 つの資産を捨てる:

- **比較ファミリーの一員としての役割**。ADR-0021 の思想は「並置して実データで決める」であり、
  `model_comparison` の横並びは新モデルを評価するときの基準線そのものになる。M-2 が
  「無改変のベースライン」として価値を持つのと同じ理由で、退役したモデルにも基準線の役割が残る。
- **決着の再現性**。前提（マクロ系列・学習窓・前処理世代）が変われば結論は動く。実際 #447 の
  `lag_days` 是正だけで M-2 の rank-IC は 0.1332→0.1285 動いた。削除すると、再燃したときに
  同じ実装を書き直すことになる。

## Decision

1. **退役は `AnalysisPlugin.hidden = True` で行う**（`plugins/base.py`）。フィルタは消費側＝
   `routers/analysis.py` の `/api/plugins` が `if not p.hidden` で除外する1箇所だけに置く。
   `plugins/__init__.py` の自動検出は**無改変**とし、レジストリ登録・`get_plugin` /
   `execute_plugin` / `POST /api/plugins/{name}/run` / `model_comparison.COMPARISON_MODELS` /
   テストは**すべて残す**。退役は削除ではない。
2. **`hidden` と `heavy` は別軸**として扱う。`heavy` は「Render 軽量モードでブロックする」＝
   実行環境の制約、`hidden` は「選択肢として勧めない」＝評価の結論。混ぜない。
3. **下流の選択肢からも同時に外す**。`sell_ranking` / `recommend` の `mu_source` options と、
   `templates/analysis.html` の静的 select（`params_schema` から動的注入されない**二重管理**）
   の両方。片方だけ直すと、選んだ瞬間に `coerce_params` の membership 検証が reject する。
4. **退役の根拠は実測に置く**。基準は2つあり、どちらかを満たすこと:
   - 昇格ゲートと同じ土俵での劣後（ADR-0018 の定常ブートストラップで、補正後 α を通る差）。
     [ADR-0028](0028-freshness-limits-from-measured-release-lag.md) が「既定を減らす向きにも
     同じ基準を課す」と定めたのと同じ理由で、**点推定の大小では降ろさない**。
   - producer も下流参照も持たない（＝運用に接続されていない）こと。
5. **未実測のまま退役させない**。ADR が「実測を追記する」と書いたなら、退役の前に測って
   その ADR を埋める。測定は `python -m scripts.model_comparison_run --models a,b`
   （`model_comparison.run_comparison(only_models=...)` を薄く包むだけ）で行い、
   **測る手続きを書き起こさない**——[ADR-0041](0041-preset-weight-gate-has-an-implementation.md)
   と同じ理由で、アドホックに書くと本番と別物を測る。
6. **CI で縛る**（`tests/test_analysis_meta.py::TestHiddenPlugins`・
   `tests/test_nightly_scores.py::TestHeavyAutomationRegistry`）:
   hidden は `/api/plugins` に出ない／レジストリと `COMPARISON_MODELS` には残る／
   `mu_source` 選択肢に出ない／静的 select と `params_schema` が一致する／
   hidden かつ heavy なら `HEAVY_AUTOMATION` は `exempt:`。
   最後の1本は「UI から消えたのに夜間バッチだけが回し続ける」矛盾を止める。

## Considered Options

- **プラグインごと削除する**: サイドバーは最も綺麗になるが、比較の基準線と決着の再現性を失う
  （上記 Context）。→ 却下。ファイルを消すのは、前提が変わっても二度と評価しないと決めたとき。
- **`plugins/__init__.py` の自動検出から外す**: API からも消えるため `model_comparison` が
  `not_registered` を返し、比較行が死ぬ。`HEAVY_AUTOMATION` の exempt エントリも stale 判定で
  CI が落ち、退役のたびにレジストリを削る作業が要る。→ 却下（消費側フィルタのほうが可逆）。
- **`category` を空にしてサイドバーから落とす**: `to_meta()` の契約（category は非空 str）を
  壊し、`tests/test_analysis_meta.py` の既存テストと衝突する。意図も読めない。→ 却下。
- **`ui_order` を末尾へ送るだけ**: 並びが変わるだけで選択肢は減らない。「増えすぎて困る」に
  対する答えになっていない。→ 却下。

## Consequences

- サイドバー「③ 将来リターンを予測」は **6本→4本**（M-1/M-2/M-3/M-6）になる。
  「④ 戦略を検証 → モデル比較（OOF）」は**6モデルのまま**で、退役した2本もそこで測り直せる。
- M-4/M-5 のフロント側変更は不要だった。両者は `PLUGIN_TAB_MAP` に無く
  `_createDynamicTab()` が `/api/plugins` のメタから丸ごと生成しているため、API から消えれば
  タブごと消える。逆に言えば、**静的タブを持つモデル（M-1/M-2/M-3）を退役させるときは
  `templates/analysis.html` からタブ本体を外す作業が別途要る**。
- `mu_source` はブラウザに永続化されていない（`localStorage` は保有銘柄のみ）ため、選択肢の
  除去で既存ユーザーが 400 を踏むことはない。API を直接叩けば退役モデルも引き続き実行できる。
- 退役の判断そのものは可逆だが、**戻すときも同じ手続きを踏む**（`hidden` を落とすだけでなく、
  実測を添えて ADR を追記する）。hidden の付け外しを気軽にやると、UI が評価の結論ではなく
  その日の気分を映すようになる。
- 残る非対称: M-2 は M-6 に有意劣後（p=0.002）しているが、M-4 の基底・SHAP 解釈・
  monotone constraints・分割コンフォーマル（ADR-0020）の実装土台であり今回は残した。
  M-4 を退役させた以上「M-2 は何のために残っているか」は別途明文化する（#570 の残タスク）。

関連 ADR: 0015（M-4 スタッキング・単体で十分の判定）, 0017（M-5 learning-to-rank）,
0018（rank-IC 差の有意性検定）, 0021（候補メニューと昇格ゲート）, 0022（既定 mu_source の切替）,
0028（減らす向きにも同じ基準）, 0031（heavy には自動実行の登録が要る）, 0041（測る手続きは実装として残す）。
