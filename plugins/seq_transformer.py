"""M-7（時系列 Transformer）の JAX 実装：encoder・AdamW・学習・walk-forward への fit_predict。

構造（小型・約 2 万パラメータ）
------------------------------
- 系列の枝: 4 週パッチ（`weekly_sequences.PATCH_DIM`）を Linear で d_model へ → 先頭に要約用の
  トークン（CLS）→ 学習する位置埋め込み → pre-LN の encoder を `n_layers` 層（head は `N_HEADS`、
  FFN は 2·d_model・GELU）→ CLS の出力。観測の無いパッチは注意の対象から外す（mask）。CLS は常に
  見えるので、全部欠けても注意の対象は空にならない
- 財務の枝: 財務列（fold 内で補完・winsorize・z）→ Linear(d_model) → GELU
- 出力: 結合 → Linear(d_model) → GELU → Linear(1)
- `use_sequence=False` は系列の枝を丸ごと外した対照（同じ学習手順・同じ行）

浅い NN が最良だった Gu, Kelly & Xiu (2020, RFS, doi:10.1093/rfs/hhaa009) を小型の根拠にする。

学習
----
- 損失は二乗誤差（順位で学習した M-5 が MSE に負けた・ADR-0017）
- 答えは fold 内で p1-p99 winsorize → 学習月ごとに平均を引く（`DEMEAN_TARGET`）→ 全体の sd で z。
  M-7 はマクロを入れず、系列も週内の横断 z で市場中立なので、月に共通の成分は説明できない雑音になる。
  評価は月内の順位だけ（rank-IC）なので水準は飾り（M-6 と同じく「水準は検証の外」）
- 最適化は AdamW（Loshchilov & Hutter 2019）を手書き。global norm で clip
- 学習回数は early stopping で決める: 学習期間の末尾の月を塊で検証に回し、学習との間に最大
  `LABEL_HORIZON_MONTHS` の空白を置く（52 週先の答えが時間で重なる分だけ検証が甘くなるのを防ぐ。
  柔軟なモデルほど未来の混入を吸う＝M-2 は除去で rank-IC 0.348→0.141）。決めた回数で
  **学習期間の全体を学習し直してから**予想する（空白と検証の分だけ古いモデルで予想しないため）
- 種は呼び出し側が渡す（プラグインは `tuning.seeded`）。fold の種は test 月から作るので、落ちた
  fold があっても同じ月は同じ種。shape を固定して再コンパイルを起こさない

jax の読み方
------------
jax はモジュールの import 時には読まない（`plugins` の自動検出は Render でも走り、Render には jax が
無い）。使う関数の中で `require_jax()` を呼ぶ。その前に `jax_import_guard.install()` を挿し、Smart App
Control が GPU 専用の拡張を止めても CPU の jax を起動できるようにする。読めなければ `JaxUnavailable`。

失敗を隠さない
--------------
`walk_forward_cv_monthly` は fit_predict の例外を握って fold を黙って捨てる（`plugins/utils.py`）。
`make_fit_predict` は例外を `errors` へ積んでから送出し、呼び出し側は CV の後で
`raise_fold_errors(errors)` を呼ぶ——OOF が黙って縮むのを防ぐ。
"""
from __future__ import annotations

import functools
import math
import statistics
from dataclasses import dataclass, field

import numpy as np

from .macro_snapshots import LABEL_HORIZON_MONTHS
from .utils import fit_feature_columns, transform_feature_row, winsorize
from .weekly_sequences import N_PATCHES, PATCH_DIM

# ── 固定の定数（変えるなら事前登録をやり直す・docs/prereg/m7-transformer.md）──────────
N_HEADS = 4
BATCH_SIZE = 512
PREDICT_BATCH = 4096
WEIGHT_DECAY = 1e-4
ADAM_B1, ADAM_B2, ADAM_EPS = 0.9, 0.999, 1e-8
CLIP_NORM = 1.0
LN_EPS = 1e-5
NEG_INF = -1e9
VALID_FRAC = 0.2                   # early stopping の検証に回す行の割合（月の塊で近似・M-2 と同じ）
ES_GAP_MAX_MONTHS = LABEL_HORIZON_MONTHS
ES_MIN_FIT_MONTHS = 3              # 学習側に最低限残す月数（足りなければ空白を縮める）
FALLBACK_EPOCHS = 10               # early stopping を組めないときの固定回数
DEMEAN_TARGET = True
MAX_LAYERS = 8


class JaxUnavailable(RuntimeError):
    """jax を読めない（未導入・Smart App Control の遮断を含む）。"""


def require_jax():
    """(jax, jax.numpy) を返す。読めなければ JaxUnavailable（理由と確かめ方を添える）。"""
    import jax_import_guard

    jax_import_guard.install()
    try:
        import jax
        import jax.numpy as jnp
    except Exception as e:   # DLL の遮断は ImportError、二度目の import は AttributeError になりうる
        raise JaxUnavailable(
            f"jax を読めない（{type(e).__name__}: {e}）。M-7 は jax で学習する。"
            "`python -m scripts.check_heavy_imports` で止まっている部品を確かめる"
            "（Windows の Smart App Control が jaxlib の DLL を止めることがある・docs/GOTCHAS.md）。"
            "未導入なら requirements-inference.txt を入れる") from e
    return jax, jnp


@dataclass(frozen=True)
class TFConfig:
    """学習の設定（hash 可能＝コンパイル済みの関数をこの値ごとに1回だけ作る）。"""
    n_static: int
    use_sequence: bool = True
    d_model: int = 32
    n_layers: int = 2
    dropout: float = 0.1
    learning_rate: float = 1e-3
    max_epochs: int = 30
    patience: int = 5
    batch_size: int = BATCH_SIZE
    n_heads: int = N_HEADS
    weight_decay: float = WEIGHT_DECAY

    def __post_init__(self):
        if self.n_static < 1:
            raise ValueError("財務列が 1 つも無い（n_static >= 1）")
        if self.d_model < self.n_heads or self.d_model % self.n_heads:
            raise ValueError(f"d_model={self.d_model} は head 数 {self.n_heads} の倍数にする")
        if not (1 <= self.n_layers <= MAX_LAYERS):
            raise ValueError(f"n_layers は 1〜{MAX_LAYERS}")
        if not (0.0 <= self.dropout < 1.0):
            raise ValueError("dropout は 0 以上 1 未満")
        if self.learning_rate <= 0 or self.max_epochs < 1 or self.patience < 1 or self.batch_size < 1:
            raise ValueError("learning_rate・max_epochs・patience・batch_size は正にする")


# ── 純関数（jax 不要）───────────────────────────────────────────────────────

def es_month_split(train_groups, *, valid_frac: float = VALID_FRAC,
                   gap_max: int = ES_GAP_MAX_MONTHS,
                   min_fit: int = ES_MIN_FIT_MONTHS) -> tuple[int, int] | None:
    """early stopping の (学習側の行数, 検証の開始行) を返す。組めなければ None。

    `train_groups` は月の昇順に連結した学習行の各月の行数（`walk_forward_cv_monthly(pass_train_groups=True)`）。
    末尾の月を丸ごと検証に回し（検証行数が全体の `valid_frac` に最も近い月数・同点なら少ない側）、
    その手前に最大 `gap_max` か月の空白を置く。学習側に `min_fit` か月残すために空白を縮める。
    月を割らない（同じ月の行が学習と検証に分かれない）。
    """
    groups = [int(g) for g in train_groups]
    m, n = len(groups), sum(groups)
    if m < min_fit + 1 or n <= 0:
        return None
    target = n * valid_frac
    best_k, best_d, cum = 1, None, 0
    for k in range(1, m - min_fit + 1):
        cum += groups[-k]
        d = abs(cum - target)
        if best_d is None or d < best_d:
            best_k, best_d = k, d
    before = m - best_k
    gap = max(0, min(gap_max, before - min_fit))
    fit_months = before - gap
    if fit_months < min_fit:
        return None
    return sum(groups[:fit_months]), sum(groups[:before])


def demean_by_month(y: np.ndarray, train_groups) -> np.ndarray:
    """学習月ごと（`train_groups` の塊ごと）に平均を引く。"""
    out = np.asarray(y, dtype=float).copy()
    start = 0
    for g in train_groups:
        g = int(g)
        if g > 0:
            out[start:start + g] -= out[start:start + g].mean()
        start += g
    if start != len(out):
        raise ValueError(f"train_groups の合計 {start} が行数 {len(out)} と合わない")
    return out


def prepare_target(y_raw, train_groups) -> tuple[np.ndarray, float, float]:
    """fold 内の答えの前処理。(z 化した答え float32, 予想を戻す水準, 予想を戻す sd)。"""
    y_w, _, _ = winsorize([float(v) for v in y_raw])
    y = np.asarray(y_w, dtype=float)
    level = float(y.mean())
    if DEMEAN_TARGET:
        y = demean_by_month(y, train_groups)
    centre = float(y.mean())
    sd = float(y.std())
    if not math.isfinite(sd) or sd <= 0.0:
        sd = 1.0
    return ((y - centre) / sd).astype(np.float32), level, sd


def fit_static(rows: list, n_static: int) -> tuple[np.ndarray, tuple]:
    """財務列の fold 内前処理（列平均で補完 → p1-p99 winsorize → z）。(行列 float32, 変換パラメータ)。"""
    X, win_p, norm_p = fit_feature_columns([list(r) for r in rows], n_static)
    return np.asarray(X, dtype=np.float32)[:, 1:], (win_p, norm_p)


def transform_static(rows: list, prep: tuple) -> np.ndarray:
    """学習側のパラメータで test 側の財務列を変換する（リークしない）。"""
    win_p, norm_p = prep
    if not rows:
        return np.zeros((0, len(win_p)), dtype=np.float32)
    return np.asarray([transform_feature_row(list(r), win_p, norm_p)[1:] for r in rows],
                      dtype=np.float32)


def fold_seed(seed: int, ym_code: int) -> int:
    """fold の種（test 月 YYYYMM から作る＝落ちた fold があっても同じ月は同じ種）。"""
    return (int(seed) * 1_000_003 + int(ym_code)) % (2 ** 31)


def ym_code(ym: str) -> int:
    """'YYYY-MM' → YYYYMM。"""
    return int(ym[:4]) * 100 + int(ym[5:7])


def median_epochs(epochs: list) -> int:
    """fold ごとの学習回数の中央値（最終モデルの学習回数・M-2 の n_est_final と同型）。"""
    if not epochs:
        return FALLBACK_EPOCHS
    return max(1, int(round(statistics.median(epochs))))


def raise_fold_errors(errors: list) -> None:
    """fit_predict の中で起きた例外を CV の後で送出する（先頭をそのまま＝取消は取消として届く）。"""
    if errors:
        raise errors[0]


# ── JAX のプログラム（cfg ごとに 1 回だけ作る）──────────────────────────────────

@dataclass(frozen=True)
class _Program:
    init: object
    opt_init: object
    train_step: object
    predict: object


@functools.lru_cache(maxsize=16)
def _program(cfg: TFConfig) -> _Program:
    jax, jnp = require_jax()
    f32 = jnp.float32
    D, H = cfg.d_model, cfg.n_heads
    dh = D // H
    T = N_PATCHES + 1
    tree_map = jax.tree_util.tree_map

    def init(key):
        keys = iter(jax.random.split(key, 8 + 8 * cfg.n_layers))

        def dense(n_in, n_out):
            w = jax.random.normal(next(keys), (n_in, n_out), f32) * f32(1.0 / math.sqrt(n_in))
            return {"w": w, "b": jnp.zeros((n_out,), f32)}

        p = {"static": dense(cfg.n_static, D)}
        if cfg.use_sequence:
            p["embed"] = dense(PATCH_DIM, D)
            p["cls"] = jax.random.normal(next(keys), (D,), f32) * f32(0.02)
            p["pos"] = jax.random.normal(next(keys), (T, D), f32) * f32(0.02)
            p["layers"] = [{
                "ln1_g": jnp.ones((D,), f32), "ln1_b": jnp.zeros((D,), f32),
                "q": dense(D, D), "k": dense(D, D), "v": dense(D, D), "o": dense(D, D),
                "ln2_g": jnp.ones((D,), f32), "ln2_b": jnp.zeros((D,), f32),
                "ff1": dense(D, 2 * D), "ff2": dense(2 * D, D),
            } for _ in range(cfg.n_layers)]
            p["lnf_g"] = jnp.ones((D,), f32)
            p["lnf_b"] = jnp.zeros((D,), f32)
            head_in = 2 * D
        else:
            head_in = D
        p["head1"] = dense(head_in, D)
        p["head2"] = dense(D, 1)
        return p

    def ln(x, g, b):
        mu = jnp.mean(x, axis=-1, keepdims=True)
        var = jnp.mean(jnp.square(x - mu), axis=-1, keepdims=True)
        return (x - mu) * jax.lax.rsqrt(var + f32(LN_EPS)) * g + b

    def lin(x, d):
        return x @ d["w"] + d["b"]

    def forward(p, P, M, S, key, train: bool):
        n_drop = 2 + 2 * cfg.n_layers
        dk = jax.random.split(key, n_drop)

        def drop(x, k):
            if not train or cfg.dropout <= 0.0:
                return x
            keep = jax.random.bernoulli(k, 1.0 - cfg.dropout, x.shape)
            return jnp.where(keep, x / f32(1.0 - cfg.dropout), f32(0.0))

        s = jax.nn.gelu(lin(S, p["static"]))
        if cfg.use_sequence:
            B = P.shape[0]
            x = lin(P, p["embed"])
            cls = jnp.broadcast_to(p["cls"], (B, 1, D))
            x = jnp.concatenate([cls, x], axis=1) + p["pos"][None, :, :]
            x = drop(x, dk[0])
            keep_tok = jnp.concatenate([jnp.ones((B, 1), dtype=bool), M], axis=1)
            bias = jnp.where(keep_tok, f32(0.0), f32(NEG_INF))[:, None, None, :]
            for li, L in enumerate(p["layers"]):
                h = ln(x, L["ln1_g"], L["ln1_b"])
                q = lin(h, L["q"]).reshape(B, T, H, dh)
                k = lin(h, L["k"]).reshape(B, T, H, dh)
                v = lin(h, L["v"]).reshape(B, T, H, dh)
                sc = jnp.einsum("bqhd,bkhd->bhqk", q, k) * f32(1.0 / math.sqrt(dh)) + bias
                a = jax.nn.softmax(sc, axis=-1)
                o = jnp.einsum("bhqk,bkhd->bqhd", a, v).reshape(B, T, D)
                x = x + drop(lin(o, L["o"]), dk[1 + 2 * li])
                h = ln(x, L["ln2_g"], L["ln2_b"])
                x = x + drop(lin(jax.nn.gelu(lin(h, L["ff1"])), L["ff2"]), dk[2 + 2 * li])
            c = ln(x[:, 0, :], p["lnf_g"], p["lnf_b"])
            z = jnp.concatenate([c, s], axis=-1)
        else:
            z = s
        z = drop(jax.nn.gelu(lin(z, p["head1"])), dk[-1])
        return lin(z, p["head2"])[:, 0]

    def loss_fn(p, P, M, S, y, w, key):
        pred = forward(p, P, M, S, key, True)
        return jnp.sum(w * jnp.square(pred - y)) / jnp.maximum(jnp.sum(w), f32(1.0))

    def opt_init(p):
        zeros = tree_map(jnp.zeros_like, p)
        return {"t": jnp.zeros((), jnp.int32), "m": zeros, "v": tree_map(jnp.zeros_like, p)}

    def train_step(p, opt, P, M, S, y, w, key):
        loss, g = jax.value_and_grad(loss_fn)(p, P, M, S, y, w, key)
        gn = jnp.sqrt(sum(jnp.sum(jnp.square(x)) for x in jax.tree_util.tree_leaves(g)))
        scale = jnp.minimum(f32(1.0), f32(CLIP_NORM) / (gn + f32(1e-6)))
        g = tree_map(lambda x: x * scale, g)
        t = opt["t"] + 1
        tf = t.astype(f32)
        m = tree_map(lambda m_, g_: f32(ADAM_B1) * m_ + f32(1.0 - ADAM_B1) * g_, opt["m"], g)
        v = tree_map(lambda v_, g_: f32(ADAM_B2) * v_ + f32(1.0 - ADAM_B2) * g_ * g_, opt["v"], g)
        bc1 = f32(1.0) - jnp.power(f32(ADAM_B1), tf)
        bc2 = f32(1.0) - jnp.power(f32(ADAM_B2), tf)

        def upd(p_, m_, v_):
            step = (m_ / bc1) / (jnp.sqrt(v_ / bc2) + f32(ADAM_EPS))
            if p_.ndim >= 2:            # 重み行列だけ減衰（bias・LN・CLS は減衰しない）
                step = step + f32(cfg.weight_decay) * p_
            return (p_ - f32(cfg.learning_rate) * step).astype(f32)

        p = tree_map(upd, p, m, v)
        return p, {"t": t, "m": m, "v": v}, loss

    def predict(p, P, M, S):
        return forward(p, P, M, S, jax.random.PRNGKey(0), False)

    return _Program(init=jax.jit(init), opt_init=jax.jit(opt_init),
                    train_step=jax.jit(train_step), predict=jax.jit(predict))


# ── 学習と予想（numpy の配列を受け取る）─────────────────────────────────────────

def _take(arr, idx):
    return None if arr is None else arr[idx]


def _padded_index(order: np.ndarray, start: int, size: int) -> tuple[np.ndarray, np.ndarray]:
    """shape を固定するため、末尾の不足分を 0 番の行で埋め、重み 0 を付ける。"""
    idx = order[start:start + size]
    w = np.ones(size, dtype=np.float32)
    if len(idx) < size:
        w[len(idx):] = 0.0
        idx = np.concatenate([idx, np.zeros(size - len(idx), dtype=idx.dtype)])
    return idx, w


def _predict_z(cfg: TFConfig, params, P, M, S) -> np.ndarray:
    prog = _program(cfg)
    n = len(S)
    out = np.empty(n, dtype=np.float64)
    order = np.arange(n)
    for start in range(0, n, PREDICT_BATCH):
        idx, w = _padded_index(order, start, PREDICT_BATCH)
        pred = np.asarray(prog.predict(params, _take(P, idx), _take(M, idx), S[idx]))
        k = int(w.sum())
        out[start:start + k] = pred[:k]
    return out


def _train(cfg: TFConfig, data: dict, seed: int, *, epochs: int | None = None,
           valid: dict | None = None) -> tuple:
    """学習して (params, 学習回数, 検証損失の履歴) を返す。

    `epochs` を渡せばその回数だけ回す。渡さず `valid` があれば early stopping（`patience` 回続けて
    検証損失が下がらなければ止め、最良の回の params に戻す）。
    """
    jax, _jnp = require_jax()
    prog = _program(cfg)
    n = len(data["y"])
    if n == 0:
        raise ValueError("学習行が 0")
    key = jax.random.PRNGKey(int(seed) % (2 ** 31))
    k_init, k_drop = jax.random.split(key)
    params = prog.init(k_init)
    opt = prog.opt_init(params)
    rng = np.random.default_rng(int(seed))
    use_es = epochs is None and valid is not None and len(valid["y"]) > 0
    n_epochs = epochs if epochs is not None else (cfg.max_epochs if use_es else FALLBACK_EPOCHS)
    best_loss, best_params, best_ep, bad = math.inf, params, 0, 0
    history: list[float] = []
    step = 0
    for ep in range(1, n_epochs + 1):
        order = rng.permutation(n)
        for start in range(0, n, cfg.batch_size):
            idx, w = _padded_index(order, start, cfg.batch_size)
            kb = jax.random.fold_in(k_drop, step)
            step += 1
            params, opt, _ = prog.train_step(
                params, opt, _take(data["P"], idx), _take(data["M"], idx),
                data["S"][idx], data["y"][idx], w, kb)
        if use_es:
            pred = _predict_z(cfg, params, valid["P"], valid["M"], valid["S"])
            vl = float(np.mean(np.square(pred - valid["y"].astype(np.float64))))
            history.append(vl)
            if vl < best_loss:
                best_loss, best_params, best_ep, bad = vl, params, ep, 0
            else:
                bad += 1
                if bad >= cfg.patience:
                    break
    if use_es:
        return best_params, best_ep, history
    return params, n_epochs, history


def _slice(data: dict, start: int, stop: int | None) -> dict:
    return {k: (None if v is None else v[start:stop]) for k, v in data.items()}


@dataclass
class FittedModel:
    """学習済みのモデルと、予想を元の尺度へ戻すための fold 内の前処理。"""
    cfg: TFConfig
    params: object
    static_prep: tuple
    y_level: float
    y_sd: float
    epochs: int
    es_used: bool
    history: list = field(default_factory=list)

    def predict(self, static_rows: list, patches, pmask) -> np.ndarray:
        S = transform_static(static_rows, self.static_prep)
        if self.cfg.use_sequence and (patches is None or pmask is None):
            raise ValueError("use_sequence=True なのに系列が渡されていない")
        P = patches if self.cfg.use_sequence else None
        M = pmask if self.cfg.use_sequence else None
        return _predict_z(self.cfg, self.params, P, M, S) * self.y_sd + self.y_level


def fit_model(cfg: TFConfig, static_rows: list, y_raw, train_groups, patches, pmask,
              seed: int, *, epochs: int | None = None) -> FittedModel:
    """1 回分の学習。`epochs` が None なら early stopping で回数を決めて、全行で学習し直す。"""
    if cfg.use_sequence and (patches is None or pmask is None):
        raise ValueError("use_sequence=True なのに系列が渡されていない")
    S, prep = fit_static(static_rows, cfg.n_static)
    y_z, level, sd = prepare_target(y_raw, train_groups)
    full = {"P": patches if cfg.use_sequence else None,
            "M": pmask if cfg.use_sequence else None, "S": S, "y": y_z}
    history: list = []
    es_used = False
    if epochs is None:
        split = es_month_split(train_groups)
        if split is None:
            epochs = min(FALLBACK_EPOCHS, cfg.max_epochs)
        else:
            n_fit, v0 = split
            _p, epochs, history = _train(cfg, _slice(full, 0, n_fit), seed,
                                         valid=_slice(full, v0, None))
            es_used = True
    params, epochs, _ = _train(cfg, full, seed, epochs=epochs)
    return FittedModel(cfg=cfg, params=params, static_prep=prep, y_level=level, y_sd=sd,
                       epochs=int(epochs), es_used=es_used, history=history)


# ── walk-forward へ差し込む ───────────────────────────────────────────────────

@dataclass(frozen=True)
class SampleBank:
    """CV のサンプルの 3 番目の要素（行番号）から引く系列と月。

    `patches` (N, 13, 12) float32・`pmask` (N, 13) bool は `use_sequence=False` なら None。
    `ym_codes` (N,) は各行の月（YYYYMM）＝fold の種に使う。
    """
    ym_codes: np.ndarray
    patches: np.ndarray | None = None
    pmask: np.ndarray | None = None

    def take(self, idx: np.ndarray):
        return _take(self.patches, idx), _take(self.pmask, idx)


def make_fit_predict(cfg: TFConfig, bank: SampleBank, seed: int, errors: list,
                     diag: dict | None = None):
    """`walk_forward_cv_monthly(..., pass_train_groups=True)` へ渡す fit_predict。

    サンプルは `(財務列, 答え, 行番号)`。例外は `errors` へ積んでから送出する（CV は例外を握るので、
    呼び出し側が CV の後で `raise_fold_errors(errors)` を呼ぶ）。`diag` には fold ごとの学習回数・
    early stopping を使えたか・学習行数を積む。
    """
    def fit_predict(train_samples, test_samples, train_groups=None):
        try:
            if train_groups is None:
                raise ValueError("M-7 の fit_predict には各学習月の行数（train_groups）が要る"
                                 "（walk_forward_cv_monthly(pass_train_groups=True)）")
            idx_tr = np.fromiter((s[2] for s in train_samples), dtype=np.int64,
                                 count=len(train_samples))
            idx_te = np.fromiter((s[2] for s in test_samples), dtype=np.int64,
                                 count=len(test_samples))
            P_tr, M_tr = bank.take(idx_tr)
            P_te, M_te = bank.take(idx_te)
            fm = fit_model(cfg, [s[0] for s in train_samples], [s[1] for s in train_samples],
                           train_groups, P_tr, M_tr, fold_seed(seed, bank.ym_codes[idx_te[0]]))
            yhat = fm.predict([s[0] for s in test_samples], P_te, M_te)
            if diag is not None:
                diag.setdefault("epochs", []).append(fm.epochs)
                diag.setdefault("es_used", []).append(fm.es_used)
                diag.setdefault("n_train", []).append(len(train_samples))
            return yhat.tolist(), [float(s[1]) for s in test_samples]
        except Exception as e:
            errors.append(e)
            raise
    return fit_predict
