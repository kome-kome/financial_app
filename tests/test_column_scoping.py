"""クエリ列スコープのメタ検査（Issue #482・ADR-0034 の系）。

**目的**: `db.query(Model)` の全列 ORM ロードが新しく増えたら CI で落とす。

全列ロードは failure を出さない。Supabase の Egress 枠（5GB/月）を静かに食うだけなので、
`notify-failure.yml`（#414）では原理的に拾えず、気づく経路が人間の目視しかない
（ADR-0031「登録≠実行」と同型の、失敗として現れない劣化）。実際 #459 で
`financial_metrics` VIEW の全列転送が 22.5MB/回 と判明するまで誰も気づかなかった。

**なぜ AST か**: `db.query(FinancialMetric)`（全列）と `db.query(FinancialMetric.roe)`
（列指定）を確実に判別するため。正規表現だと改行をまたぐ書き方を取りこぼすうえ、
docstring 内のコード例（`database.py` の `latest_year_subq` にある）まで拾ってしまう。
リポジトリ初の AST テストだが、判定ロジックは `TestHeavyAutomationRegistry`
（tests/test_nightly_scores.py）と同じ「レジストリとの双方向差分」に載せている。

**db_egress は使わない**: `db_egress._Bucket` は n_cols を保持せず、SQLite では
`cursor.rowcount = -1` のため `unknown_calls` にしか積まれない（tests/test_db_egress.py
参照）。「このクエリが何列引いたか」を実行時に測る手段が無いので、静的解析で完結させる。
"""
import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Base, ViewBase

ROOT = Path(__file__).resolve().parent.parent

# スキャン対象（非再帰の *.py）。
#  - ルート直下: database.py / backtest.py / collector_*.py / feature_disclosure.py が居る
#  - plugins/  : 分析モデル本体。夜間バッチとユーザー操作の両方で走る
#  - routers/  : Render のリクエスト経路（CSV/スクリーニング等が VIEW を広く引く）
# 除外:
#  - tests/    : 本番 Egress を使わない
#  - scripts/  : 手動起動の検証 CLI。反復 pull は scripts/_cache.py の pickle キャッシュ
#                （#355）という別の制御下にある。ここを含めると「手動だから exempt」だけの
#                行が増えてレジストリが読めなくなる。含めるならキャッシュ層の契約テストと
#                セットで（フォローアップ）。
SCAN_DIRS = ("", "plugins", "routers")

# 行を積まない終端演算。これらがチェーンに現れ、かつ .all() が無ければ列指定の利得が無い。
SINGLE_ROW_TERMINALS = frozenset({
    "first", "one", "one_or_none", "scalar", "count",
    "delete", "update", "exists", "get",
})
# 列を選び直す演算。これがあれば全列は転送されない。
COLUMN_REBIND = frozenset({"with_entities", "with_only_columns"})

EXEMPT_PREFIX = "exempt:"
_MIN_REASON_LEN = 20   # tests/test_nightly_scores.py の HEAVY_AUTOMATION と同値


def _orm_model_names() -> frozenset:
    """database.py が定義する ORM クラス名（字面のヒューリスティックにしない）。"""
    return frozenset(
        {m.class_.__name__ for m in Base.registry.mappers} |
        {m.class_.__name__ for m in ViewBase.registry.mappers}
    )


def _annotate_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node          # type: ignore[attr-defined]


def _chain_methods(node: ast.AST) -> set:
    """node を含むメソッドチェーンに現れるメソッド名。

    `db.query(M).filter(...).all()` なら {"filter", "all"}。変数へ束縛してから後段で
    呼ぶ形（`q = db.query(M)` → `q.filter(...).all()`）は追わない——データフロー追跡を
    入れると偽陰性（＝穴の見逃し）が生まれ、このテストの目的に反する。追えないものは
    「要登録」に倒し、正当な理由は FULL_ROW_LOADS の理由文で明示する。
    """
    names = set()
    cur = node
    while True:
        parent = getattr(cur, "_parent", None)
        if parent is None:
            break
        if isinstance(parent, ast.Attribute):
            names.add(parent.attr)
            cur = parent
        elif isinstance(parent, ast.Call):
            cur = parent
        else:
            break
    return names


class _QueryScanner(ast.NodeVisitor):
    def __init__(self, rel_path: str, model_names: frozenset):
        self.rel_path = rel_path
        self.model_names = model_names
        self.stack: list = []
        self.found: dict = {}
        self.query_sites: set = set()   # "<path>::<qualname>"（全列/列指定を問わない）

    def _push(self, node, name):
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._push(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._push(node, node.name)

    def visit_Lambda(self, node):
        self._push(node, "<lambda>")

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "query":
            qual = ".".join(self.stack) if self.stack else "<module>"
            self.query_sites.add(f"{self.rel_path}::{qual}")
        if (isinstance(func, ast.Attribute) and func.attr == "query"
                and len(node.args) == 1 and not node.keywords
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in self.model_names):
            terms = _chain_methods(node)
            transfers_rows = not (
                (terms & COLUMN_REBIND) or
                ((terms & SINGLE_ROW_TERMINALS) and "all" not in terms)
            )
            if transfers_rows:
                model = node.args[0].id
                qual = ".".join(self.stack) if self.stack else "<module>"
                key = f"{self.rel_path}::{qual}::{model}"
                # 同一関数・同一モデルの複数分岐は1エントリに畳む（year 有無の2本など）
                self.found.setdefault(key, node.lineno)
        self.generic_visit(node)


def scan() -> tuple[dict, set]:
    """(全列ロード {key: lineno}, 全 query 呼び出し箇所 {"<path>::<qualname>"}) を返す。

    key は "<path>::<qualname>::<Model>"。行番号はキーに含めない（行がずれるだけで
    レジストリが壊れるのを避ける）。
    """
    model_names = _orm_model_names()
    full: dict = {}
    sites: set = set()
    for sub in SCAN_DIRS:
        directory = ROOT / sub if sub else ROOT
        for path in sorted(directory.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            _annotate_parents(tree)
            scanner = _QueryScanner(path.relative_to(ROOT).as_posix(), model_names)
            scanner.visit(tree)
            for key, lineno in scanner.found.items():
                full.setdefault(key, lineno)
            sites |= scanner.query_sites
    return full, sites


# ── 全列ロードの許可リスト ──────────────────────────────────────────────────
# 新しく `db.query(Model)` を書いたら、列指定へ直すか、ここへ "exempt: <理由>" を足す。
# 理由は「なぜ列を絞っても意味が無いか／絞れないか」を書く（20文字以上）。
FULL_ROW_LOADS: dict[str, str] = {
    # ── A. 表示・CSV が VIEW を広く要求する（#482 のスコープ外・消費列の棚卸しが先）──
    "routers/market.py::export_csv::FinancialMetric":
        "exempt: CSV は全97列引いて21列書く。絞るには出力仕様の確定が要る（#489）。行数は limit 10000 で上限あり",
    "routers/market.py::screening::FinancialMetric":
        "exempt: スクリーニング条件が VIEW の広い範囲を参照し結果も record_to_dict で返す。行数は limit で上限あり",
    "routers/market.py::get_financials::FinancialMetric":
        "exempt: serializers.record_to_dict が VIEW 全列を返す API 契約。1社分の年度数（数十行）で行数が小さい",
    "routers/market.py::list_companies::FinancialMetric":
        "exempt: 同じく record_to_dict 経由。1ページ分の edinet_code へ IN 絞り済みで行数はページサイズ（最大500）",
    "routers/market.py::get_macro_data::MacroData":
        "exempt: /api/macro/data は OHLCV を返す契約で 11列中6列を実際に返す。行数は limit days で上限あり",
    "routers/market.py::db_company_drilldown::FinancialRecord":
        "exempt: DB ビューアの1社ドリルダウンで全列表示そのものが目的。行数は1社の年度数どまり",
    "routers/market.py::db_company_drilldown::StockPriceDaily":
        "exempt: 同ドリルダウンの直近30行（limit 30）。4列しかなく絞る余地が無い",
    "plugins/gap_analysis.py::GapAnalysisPlugin.execute::FinancialMetric":
        "exempt: #482 のスコープ外。結果テーブルと CSV が VIEW を広く出すため消費列の棚卸しが先（#489）",
    "plugins/net_cash_analysis.py::NetCashAnalysisPlugin._build_query::FinancialMetric":
        "exempt: #482 のスコープ外。清原式の結果表が BS 内訳を広く出すため消費列の棚卸しが先（#489）",
    "backtest.py::run::FinancialMetric":
        "exempt: #482 のスコープ外。resolve_weights の動的プリセットが任意の METRICS を要求しうるため列集合が実行時決定",

    # ── B. companies 全列（#446 の実測で「絞らない」と決定済み）──
    "plugins/macro_snapshots.py::_load_data_impl::Company":
        "exempt: companies 全列は実測 0.5MB / 4,437行で #446 が明示的に絞らないと決めた。消費側が業種・sec_code 等を広く使う",
    "plugins/macro_dlm.py::_load_prices_impl::Company":
        "exempt: 同上。M-3 も load_data と同じ companies 全列を共有する（列を分けると2経路の乖離を生む）",
    "routers/market.py::list_companies::Company":
        "exempt: companies は列数が少なく実測 0.5MB/全件。さらに offset/limit（最大500）でページング済み",

    # ── C. 書き込み・再構築経路（ORM インスタンスが要る）──
    "collector_prices.py::_update_issued_shares::Company":
        "exempt: 取得した ORM 行の issued_shares へ書き戻す更新経路。Row タプルでは永続化できない",
    "collector_prices.py::backfill_historical_stock_prices_yahoo::FinancialRecord":
        "exempt: 取得した ORM 行の stock_price へ書き戻す更新経路。Row タプルでは永続化できない",
    "collector_financials.py::refill_cf_from_xbrl._target_q::FinancialRecord":
        "exempt: XBRL 再取得で CF 列を書き戻す補完バッチ。ORM インスタンスが必要で workflow_dispatch の手動起動のみ",
    "collector_financials.py::refill_pl_bs_from_xbrl._target_q::FinancialRecord":
        "exempt: 同上（PL/BS 列の補完・refill-pl-bs.yml の手動起動のみ）。書き戻しに ORM インスタンスが要る",
    "collector_financials.py::refill_c2_from_xbrl._target_q::FinancialRecord":
        "exempt: 同上（C2 列の補完・手動起動のみ）。書き戻しに ORM インスタンスが要る",
    "collector_financials.py::refill_machinery_from_xbrl._target_q::FinancialRecord":
        "exempt: 同上（bs_machinery の補完・手動起動のみ）。書き戻しに ORM インスタンスが要る",
    "collector_financials.py::diagnose_cf_labels::FinancialRecord":
        "exempt: CF ラベル診断の手動 CLI。limit で件数上限があり定常の夜間経路には載らない",
    "collector_financials.py::reparse_from_raw::XbrlRawDocument":
        "exempt: 生 XBRL（elements_gz）を読み直して financial_records を再構築する経路で blob 列そのものが目的。手動起動のみ",

    # ── D. 台帳・ジョブ状態（行数が企業数にも時間にも比例しない）──
    "api.py::lifespan::CollectionLog":
        "exempt: 起動時に running のまま残ったジョブを回収する更新経路。件数は同時実行ジョブ数で一桁",
    "routers/collect.py::_reset_stuck_jobs::CollectionLog":
        "exempt: 同上（running のみを対象にした ORM 更新）。行数が一桁で列指定の効果が無い",
    "routers/collect.py::collection_status::CollectionLog":
        "exempt: 直近5件（limit 5）を画面へ全列表示する。行数が定数で上限されている",

    # ── E. 変数束縛（静的には追えないが実際は行を持ってこない）──
    "plugins/gap_analysis.py::GapAnalysisPlugin._regression_meta::RegressionResult":
        "exempt: 変数へ束縛した後 with_entities(max)/with_entities(model).distinct() にしか使わず行は転送しない",
    "routers/analysis.py::model_status::RegressionResult":
        "exempt: 同じく with_entities(max(computed_at)) と count() のみ。行そのものは持ってこない",

    # ── F. producer スコア（行数は銘柄数どまり・#489 でまとめて扱う）──
    "database.py::get_macro_gbdt_scores::MacroGbdtScore":
        "exempt: #482 のスコープ外。7列×銘柄数で 0.37MB。読むのは edinet_code/mu だけなので絞る余地はあり #489 へ",
    "database.py::get_macro_gbdt_producer::MacroGbdtScore":
        "exempt: 同上（読むのは edinet_code/mu/r1_prime の3列）。producer 5系統をまとめて絞る #489 で扱う",
    "database.py::get_macro_dlm_scores::MacroDlmScore":
        "exempt: 同上（M-3・6列×銘柄数）。producer 5系統をまとめて絞る #489 で扱う",
    "database.py::get_macro_ensemble_scores::MacroEnsembleScore":
        "exempt: 同上（M-4 の合成スコア・6列×銘柄数）。producer 5系統をまとめて絞る #489 で扱う",
    "database.py::get_macro_enet_scores::MacroEnetScore":
        "exempt: 同上（M-6・既定 mu_source・7列×銘柄数）。producer 5系統をまとめて絞る #489 で扱う",
    "database.py::get_macro_enet_producer::MacroEnetScore":
        "exempt: 同上（M-6 の r1_prime 付き版）。producer 5系統をまとめて絞る #489 で扱う",
    "database.py::get_latest_factor_premia::RecommendFactorPremium":
        "exempt: 1ラン＝ファクタ数（10行程度）。行数が銘柄数にも時間にも比例せず絞る利得が無い",

    # ── G. 消費列が定義上の全列 ──
    "feature_disclosure.py::load_disclosure_features::StatementDisclosure":
        "exempt: 直後に __table__.columns を回して全列を dict 化する＝消費列が定義上の全列。1社分へ絞り済み",
}


# ── テスト ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def scan_result():
    return scan()


@pytest.fixture(scope="module")
def detected(scan_result):
    return scan_result[0]


@pytest.fixture(scope="module")
def query_sites(scan_result):
    return scan_result[1]


# #482 で列指定へ直した経路。ここは「全列ロードとして検出されないこと」に加えて
# 「その関数が現に query を呼んでいること」も見る。関数がリネーム・削除されると
# 前者だけでは黙って通る（空振りするメタ検査になる）。
SCOPED_SITES = {
    "plugins/recommend.py::RecommendPlugin.execute": "FinancialMetric",
    "plugins/macro_snapshots.py::_load_data_impl": "FinancialMetric",
    "plugins/sell_ranking.py::SellRankingPlugin.execute": "FinancialMetric",
    "plugins/sector_ols.py::SectorOLSPlugin._load_records": "FinancialRecord",
    "plugins/utils.py::get_macro_features": "MacroData",
    "database.py::get_macro_beta": "MacroBetaLoading",
}


class TestFullRowLoadRegistry:
    def test_every_full_row_load_is_registered(self, detected):
        missing = sorted(set(detected) - set(FULL_ROW_LOADS))
        assert not missing, (
            "全列 ORM ロードが未登録です。列指定（db.query(Model.col, ...)）へ直すか、"
            "FULL_ROW_LOADS へ 'exempt: <理由>' を追加してください:\n  "
            + "\n  ".join(f"{k}  (line {detected[k]})" for k in missing)
        )

    def test_registry_has_no_stale_entries(self, detected):
        stale = sorted(set(FULL_ROW_LOADS) - set(detected))
        assert not stale, (
            "FULL_ROW_LOADS に実体の無いエントリがあります（列指定へ直した／関数をリネームした）。"
            "登録を消してください:\n  " + "\n  ".join(stale)
        )

    def test_exemptions_state_a_reason(self):
        for key, reason in FULL_ROW_LOADS.items():
            assert reason.startswith(EXEMPT_PREFIX), f"{key} の値は 'exempt:' で始めること"
            body = reason[len(EXEMPT_PREFIX):].strip()
            assert len(body) >= _MIN_REASON_LEN, (
                f"{key} の理由が短すぎます（{_MIN_REASON_LEN}文字以上）: {body!r}")

    def test_scanner_is_not_vacuously_green(self, detected):
        """スキャナ自体が壊れて 0 件になると全テストが緑になる（メタ検査の自己無効化）。"""
        assert detected, "スキャナが1件も検出していない＝判定ロジックが壊れている"

    def test_scoped_paths_stay_scoped(self, detected, query_sites):
        """#482 で直した経路が全列ロードへ戻っていないこと。

        「detected に無い」だけでは、関数がリネーム・削除されても通ってしまう。
        query 呼び出しが現にその関数に在ることを併せて確認し、空振りを防ぐ。
        """
        for site, model in SCOPED_SITES.items():
            # ネストした内部関数（sector_ols の _base_query 等）で引く形も許す
            assert any(s == site or s.startswith(site + ".") for s in query_sites), \
                f"{site} が query を呼んでいない（リネーム／削除？）"
            assert f"{site}::{model}" not in detected, f"{site} が全列ロードへ戻っている"
        # 週次も同じ関数内で列指定のまま
        assert ("plugins/sell_ranking.py::SellRankingPlugin.execute::StockPriceWeekly"
                not in detected)


class TestScannerBehaviour:
    """判定ロジック自体の単体テスト（実ファイルに依存しない）。"""

    def _scan_src(self, src: str) -> dict:
        tree = ast.parse(src)
        _annotate_parents(tree)
        scanner = _QueryScanner("x.py", frozenset({"Company"}))
        scanner.visit(tree)
        return scanner.found

    def test_detects_plain_full_row_load(self):
        assert self._scan_src("def f(db):\n    return db.query(Company).all()\n")

    def test_column_selection_is_not_flagged(self):
        assert not self._scan_src(
            "def f(db):\n    return db.query(Company.edinet_code).all()\n")

    def test_star_args_are_not_flagged(self):
        assert not self._scan_src("def f(db, cols):\n    return db.query(*cols).all()\n")

    def test_single_row_terminal_is_not_flagged(self):
        assert not self._scan_src(
            "def f(db):\n    return db.query(Company).filter_by(x=1).first()\n")

    def test_count_only_is_not_flagged(self):
        assert not self._scan_src("def f(db):\n    return db.query(Company).count()\n")

    def test_with_entities_is_not_flagged(self):
        assert not self._scan_src(
            "def f(db):\n    return db.query(Company).with_entities(Company.id).all()\n")

    def test_docstring_code_sample_is_not_flagged(self):
        # 正規表現なら拾ってしまう偽陽性（database.py の latest_year_subq に実在）
        assert not self._scan_src('def f(db):\n    """例: db.query(Company).all()"""\n    return 1\n')

    def test_receiver_name_does_not_matter(self):
        assert self._scan_src("def f(self):\n    return self.session.query(Company).all()\n")

    def test_non_model_argument_is_not_flagged(self):
        assert not self._scan_src("def f(db, subq):\n    return db.query(subq).all()\n")

    def test_same_function_branches_collapse_to_one_key(self):
        found = self._scan_src(
            "def f(db, y):\n"
            "    q = db.query(Company).filter_by(a=1)\n"
            "    if y:\n"
            "        q = db.query(Company).filter_by(b=2)\n"
            "    return q.all()\n"
        )
        assert list(found) == ["x.py::f::Company"]
