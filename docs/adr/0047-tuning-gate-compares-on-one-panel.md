# 探索の品質ゲートは同一パネル上でしか比較しない

- **状態**: 採用
- **日付**: 2026-09-01
- **関連**: #590 / #291 / #532 / #592（真因の追及）、[ADR-0045](0045-momentum-gains-vanish-when-the-population-is-aligned.md)（母集団を揃える）、[ADR-0039](0039-persisted-weights-carry-their-preprocess-generation.md)（永続値は前処理世代を背負う）、[ADR-0041](0041-preset-weight-gate-has-an-implementation.md)（rank-IC の測り方の正本）、[ADR-0010](0010-hyperparameter-tuning-github-actions-automation.md)（ゲートの導入元）

## Context

2026-09-01 の月次実走で `tune:macro_gbdt` は **150件を完走した**（`n_failed=0`・176.3分）のに
`exit=1` で終わり、μ̂ は更新されなかった。#291 の品質ゲートが働いた結果である:

```
WARNING:hyperparameter_search:前回スコア0.5068を下回ったため persist をスキップしました（今回=0.2359）
```

設計どおりの挙動だが、**保存されていた過去値が比較対象として成立していない**。
`plugin_tuned_params.leaderboard_json` を展開すると原因が出た:

| model | 保存値 | fold 数 | `n_oof_samples` | 保存日 |
|---|---|---|---|---|
| `macro_dlm` | 0.0221 | **55** | 830,155 | 2026-07-10 |
| `macro_risk_return` | 0.2614 | **11** | 31,859 | 2026-07-10 |
| `macro_gbdt` | 0.5068 | **10** | 15,228 | 2026-07-19 |

株式リターン予測の rank-IC は 0.02〜0.05 が良好とされる水準で、`macro_dlm` の 0.0221（55 fold）
だけがそこに収まっている。上位20件まで展開すると、**スコアはハイパーパラメータでほとんど
動かず、fold 数でだけ動いている**ことが分かる:

- `macro_gbdt`: 上位20件すべて 0.4358〜0.5068。`momentum_window=18`（n=10）が上位を独占し、
  `mw=12`（n=12）・`mw=3`（n=13）が下に並ぶ。`use_momentum=False` でも 0.4385 出る
- `macro_risk_return`: `mw=12`(n=11) 0.2614 > `mw=6`(n=13) 0.2596 > `mw=3`(n=14) 0.2563。
  `min_coverage` を 0.3→0.9 に振っても値が1桁も動かない（0.2614 が4通りで完全同値）

**ADR-0045 の「母集団が縮む側は必ず有利に見える」が、チューナの目的関数側にそのまま残って
いる。** そのうえでゲートは「過去のパネルで測った値」と「今日のパネルで測った値」を単純比較
していた:

```python
prev_score = prev["objective_value"] if prev is not None else None
if prev_score is not None and result["best_score"] < prev_score:
    # persist をスキップして非ゼロ終了
```

`_data_fingerprint(db)` は persist 時に保存されるが**比較には使われていない**。パネルは毎晩
伸びるので OOF の対象期間は毎月変わるのに、比較対象は固定された過去値のままである。したがって
**一度たまたま高い値が入ると、それを超えるまで永久に更新されない**。ゲートが守っていたのは
本番値ではなく、**たまたま最初に入った値**だった。

## Decision

**ゲートは同一パネル上で測った2値しか比較しない。手段は保存値との比較ではなく、候補プールへの
champion 投入に置き換える。**

1. **champion 投入**: 本番稼働中の `params` を今回の探索の候補プール先頭へ入れる
   （`plugins.tuning.search(champion_params=...)`）。`best >= champion` が構造的に成立するので、
   **本番より悪い params を選ぶことは起こりえない**。追加コストは最大1候補
   （M-2 約1.2分 / M-3 約1.0分 / M-1 約2.6分。それぞれ 240 / 400 / 900分の予算に対し誤差）で、
   grid の2モデルでは champion が既に combos にあるため**コストゼロ**（3モデルとも実測で
   投影が grid 内に落ちることを確認済み）
2. **persist は止めない**（`exit 0`）。「保存値を下回った」は失敗ではなくパネル世代の移動なので、
   バッチを失敗させない
3. **軸の追加は default で補い、値域の縮小では投入しない。** 保存された params には
   「その時点の探索空間」しか入っておらず、後から足された軸のキーは無い（実測: `macro_gbdt` の
   2026-07-19 の行には #366/#402 で足された `use_monotone_constraints` /
   `use_sector_features` が無い）。本番の `execute_plugin` も `coerce_params` で default を
   補うので、**補完後の姿が「いま本番で動いている設定」**である＝ここで諦めると、軸を1本
   足しただけで champion 再測定が黙って止まる（#590 が直したのと同じ「失敗として現れない」形）。
   一方、値が `d.values` から外れている場合は投入しない——`dims` を狭めるのは退役の手段でもあり
   （ADR-0045 のモメンタム既定・#583）、投入すると「前回勝ったから」で毎月復活し続ける。
   投入しないときは WARNING を出し、今回の best を persist する
4. **比較可能性の根拠を列で持つ**。`plugin_tuned_params` へ `prev_objective_value` /
   `champion_objective_value` / `n_periods` / `n_oof_samples` を追加する

## Consequences

**ゲートは実質発火しなくなる。** champion がプールに居る以上 `best >= champion` なので、
persist がブロックされる経路は無い。**劣化は WARNING ログと DB 列でしか現れない**——
月次バッチの Issue 起票（`batch_common.notify`）はもう鳴らない。これは意図した交換で、
旧実装の「鳴りっぱなしで μ̂ が固着する」より「鳴らないが値は前進する」を選んでいる。
水準の移動を追うには `plugin_tuned_params` の
`objective_value` / `prev_objective_value` / `n_periods` を SQL で見る。

**汚染済みの3行は手動でリセットしない。** 次の探索実行で自然に上書きされる。手術すると
`params_json`（本番の μ̂）も失う。既存行の新列は NULL のままで、これは「fold 数が記録されて
いない世代」を意味する。

**rank-IC の水準そのものは直っていない。** M-1 の 0.2614・M-2 の 0.5068 が M-3 の 0.0221 の
10〜20倍になる理由（リーク・ホライズン・母集団定義のいずれか）は未解明で、本 ADR の範囲外。
ゲートを直したことで**探索が「fold が少ない候補」を選び続ける性質はそのまま残る**——
`momentum_window=18` が毎月勝ち続ける可能性がある。真因の追及は #592 が持つ。

**`--persist` を付けない試し撃ちでは champion を混ぜない。** 本番値を巻き込まずに空間だけを
見たい用途で、1候補ぶんの時間を余分に使わない。

## Alternatives considered

- **世代（`data_fingerprint`）が違えば比較しない。** 実装は最小だが、パネルは毎晩伸びるので
  指紋は常に不一致＝**ゲートの実質廃止**になる。#291 の劣化防止が丸ごと消える。
- **許容幅を持たせる**（相対 -N% までは許す）。実装は軽いが、今回の 0.5068→0.2359（-53%）は
  どんな幅でも弾かれるので**現に起きている固着は解けない**。パネル差にも無力。
- **champion を空間外でも投入する。** 本番で今動いているのは champion なので実力比較としては
  こちらが正確。だが退役させたはずの設定が勝ち続けて復活しうる。`coerce_params` の options
  検証で弾かれれば失敗候補として記録されるだけなので、明示的に投入しない方を選んだ。
- **比較専用に1回だけ champion を評価し、下回ったら従来どおり非ゼロ終了。** 起票される
  アラームは残るが、grid の2モデルでは champion が空間内にある以上どのみち発火しない
  （`best >= champion`）。random の M-2 だけがサンプリング運で鳴る＝**モデルによって意味の
  違うアラーム**になる。
