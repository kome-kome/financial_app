"""
M-5 マクロ×財務 ランク学習（learning-to-rank）推奨プラグイン（Issue #362 / ADR-0017）

M-2（macro_gbdt）の rank-IC 整合版。M-2 が **MSE 最小化**（reg:squarederror）で学習する一方、
評価・ハイパラ探索・VISION 比較はすべて **期内クロスセクション Spearman rank-IC**。この
「学習目的 ≠ 評価指標」不一致（ADR-0007 が auto_hyperparams を撤去した理由と同型）を、
XGBoost の learning-to-rank 目的（rank:pairwise 既定）で解消する。**各 test 月を1クエリ
グループ**として期内順位を直接最適化し、外れリターンに MSE が引きずられて順位が歪む問題を
避ける。

設計（Issue #362 の実装注意に対応）:
  - M-2 を無改変ベースラインとして残すため **新兄弟モデル（M-5）** として追加。execute() 本体は
    MacroGbdtPlugin から丸ごと継承し、4フック（_objective / _make_cv_callback / _fit_final_model /
    _persist_producer）＋ _model_type / params_schema のみ override（DRY・同一 fold/特徴量で純比較）。
  - walk_forward_cv_monthly は月境界を落として train_samples を flat 化するため、
    `pass_train_groups=True`（utils.py・#362 の最小拡張）で各学習月のサンプル数配列を
    fit_predict へ渡し、XGBRanker.fit(group=...) の各月=1クエリグループを復元する。
  - 予測は**順位スコア**（リターン単位でない）。初版は OOF 比較専用とし producer 永続化
    （macro_gbdt_scores）と下流 sell_ranking（mu_source）統合は見送る（produced_output=False /
    read_producer_scores={} / _persist_producer=no-op）。「順位→分位期待リターン写像」を別途
    定義するまで統合しない。
  - ラベル: rank:pairwise は順序のみ使うため生の対数リターンをそのまま渡す（負値可）。
    rank:ndcg/map は非負の段階的関連度を要求し 2^rel ゲインがオーバーフローするため、各クエリ
    グループ内で `_NDCG_GRADES` 段の分位グレード（0..K-1）へ変換する。
  - 検証: model_comparison（POST /api/backtest/model-comparison）の COMPARISON_MODELS に M-5 を
    1行追加。M-2(MSE) と同一 fold・同一特徴量で OOF rank-IC を純比較する。

参考: Burges 2010 "From RankNet to LambdaRank to LambdaMART"; XGBoost Learning-to-Rank docs。
"""
from typing import Any

import numpy as np

from .macro_gbdt import MacroGbdtPlugin

# rank:ndcg/map 用の段階的関連度グレード数（2^rel ゲインのオーバーフロー回避・上限）。
# 期内銘柄数（数百）をそのまま関連度にすると 2^rel が発散するため分位でバケット化する。
_NDCG_GRADES = 16

# XGBRanker が受け付けないハイパーパラメータ（fit_predict/final で除外する）。
_RANKER_DROP_KEYS = ("n_estimators", "early_stopping_rounds")


def _prep_rank_labels(y: np.ndarray, groups: list[int], objective: str) -> np.ndarray:
    """クエリグループごとに XGBRanker 用のラベルを整える。

    rank:pairwise → 生リターンをそのまま返す（順序のみ使用・負値可）。
    rank:ndcg / rank:map → 各グループ内で _NDCG_GRADES 段の分位グレード（0..K-1・非負整数）へ
    変換する（2^rel ゲインのオーバーフロー回避）。順位スコアの学習目的は「期内順位」なので、
    グレード化しても最適化対象の順序情報は保たれる。
    """
    if not objective.startswith(("rank:ndcg", "rank:map")):
        return np.asarray(y, dtype=float)

    out = np.empty(len(y), dtype=float)
    start = 0
    for g in groups:
        seg = np.asarray(y[start:start + g], dtype=float)
        k = min(_NDCG_GRADES, g)
        if k <= 1:
            out[start:start + g] = 0.0
        else:
            order = seg.argsort()               # 昇順インデックス
            ranks = np.empty(g, dtype=float)
            ranks[order] = np.arange(g)          # 0..g-1 の密順位
            # 0..g-1 → 0..k-1 の分位グレードへ圧縮
            out[start:start + g] = np.floor(ranks / g * k).clip(0, k - 1)
        start += g
    return out


def _make_xgb_rank_fit_predict(xgb_params: dict, best_iterations: list) -> callable:
    """walk_forward_cv_monthly（pass_train_groups=True）注入用の XGBRanker fit_predict を返す。

    シグネチャは 3 引数 fit_predict(train_samples, test_samples, train_groups)。train_groups は
    各学習月のサンプル数配列（train_samples の連結順と一致）＝ XGBRanker の各クエリグループ。
    early_stopping は使わず固定 n_estimators で学習する（ランカーの eval_set は group 付き検証が
    必要で walk-forward の1テスト月では成立しにくいため・初版は単純化）。
    """
    import xgboost as xgb

    objective = xgb_params.get("objective", "rank:pairwise")
    n_estimators = xgb_params.get("n_estimators", 500)
    base_params = {k: v for k, v in xgb_params.items() if k not in _RANKER_DROP_KEYS}

    def fit_predict(train_samples, test_samples, train_groups):
        X_train = np.array([s[0] for s in train_samples], dtype=float)
        y_train = np.array([s[1] for s in train_samples], dtype=float)
        X_test = np.array([s[0] for s in test_samples], dtype=float)
        y_test_orig = [s[1] for s in test_samples]

        y_lab = _prep_rank_labels(y_train, train_groups, objective)

        model = xgb.XGBRanker(**base_params, n_estimators=n_estimators)
        model.fit(X_train, y_lab, group=train_groups, verbose=False)
        best_iterations.append(n_estimators)

        yhat = model.predict(X_test).tolist()
        return yhat, y_test_orig

    return fit_predict


class MacroGbdtRankPlugin(MacroGbdtPlugin):
    name = "macro_gbdt_rank"
    label = "M-5: マクロ×財務 ランク学習（learning-to-rank）"
    description = (
        "M-2 と同一データ・同一 fold で、学習目的を評価指標（期内クロスセクション rank-IC）へ"
        "整合させた兄弟モデル。XGBoost の learning-to-rank（rank:pairwise 既定）で各月を1クエリ"
        "グループとして期内順位を直接最適化します。予測は順位スコア（リターン単位でない）ため"
        "初版は OOF 比較専用です。【注意】株価週次履歴とマクロデータ5年分の蓄積が必要です。"
    )
    depends_on: list[str] = []
    heavy: bool = True
    category = "③ 将来リターンを予測"
    ui_order = 380                       # M-4=370 の後（M-1→M-2→M-3→M-4→M-5 順）

    def params_schema(self) -> dict:
        """M-2 のスキーマ＋ learning-to-rank 目的関数の選択軸。"""
        schema = super().params_schema()
        schema["objective"] = {
            "type": "select",
            "label": "ランク学習の目的関数",
            "description": (
                "rank:pairwise=ペアワイズ順序損失（既定・生リターンをそのまま使用）。"
                "rank:ndcg=NDCG最適化（期内分位グレードへ変換して学習）。"
            ),
            "options": [
                {"value": "rank:pairwise", "label": "rank:pairwise（ペアワイズ・既定）"},
                {"value": "rank:ndcg",     "label": "rank:ndcg（NDCG最適化）"},
            ],
            "default": "rank:pairwise",
        }
        return schema

    # ── MacroGbdtPlugin フックの override（execute 本体は継承・#362）──────────────
    def _objective(self, params: dict) -> str:
        return params["objective"]

    def _model_type(self) -> str:
        return "xgboost_ranker"

    def _make_cv_callback(self, xgb_params: dict, best_iterations: list) -> tuple:
        """XGBRanker コールバック＋ pass_train_groups=True（月クエリグループ境界の受渡）。"""
        return _make_xgb_rank_fit_predict(xgb_params, best_iterations), {"pass_train_groups": True}

    def _fit_final_model(self, final_params: dict, n_est_final: int,
                         X_all, y_all, samples_by_ym: dict, feat_names: list):
        """全データ再学習の最終ランカー。samples_by_ym を月ソート順に連結し group を復元する。

        （execute が渡す X_all/y_all は samples_by_ym.values() の dict 順・winsorize 済みで
        月境界を持たないため使わず、ここで sorted(ym) 順に組み直して各月=1クエリグループとする。）
        """
        import xgboost as xgb

        objective = final_params.get("objective", "rank:pairwise")
        X_parts: list = []
        y_parts: list = []
        groups: list[int] = []
        for ym in sorted(samples_by_ym.keys()):
            seg = samples_by_ym[ym]
            if not seg:
                continue
            X_parts.extend(s[0] for s in seg)
            y_parts.extend(s[1] for s in seg)
            groups.append(len(seg))

        X = np.array(X_parts, dtype=float)
        y = np.array(y_parts, dtype=float)
        y_lab = _prep_rank_labels(y, groups, objective)

        model = xgb.XGBRanker(**final_params, n_estimators=n_est_final)
        model.fit(X, y_lab, group=groups, verbose=False)
        return model

    def _persist_producer(self, db: Any, raw_items: list, rep_str: str | None) -> None:
        """no-op。M-5 のスコアは順位（リターン単位でない）ため producer を持たない（#362）。"""
        return None

    # ── producer を持たない（sell_ranking から mu_source として選ばれない）──────────
    def produced_output(self, db: Any) -> bool:
        return False

    def read_producer_scores(self, db: Any, macro_snapshot: dict | None = None) -> dict:
        return {}


plugin = MacroGbdtRankPlugin()
