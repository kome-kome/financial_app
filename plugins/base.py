from abc import ABC, abstractmethod
from typing import Any


class DependencyError(Exception):
    """plugin の depends_on の producer が未実行（produced_output=False）のときに送出する。

    呼び出し側（plugins.ensure_dependencies 経由の runner / 専用エンドポイント）が
    HTTP ステータスへマップする（gap-analysis→404・汎用 runner→400）。
    """
    def __init__(self, plugin_name: str, unsatisfied: list[str], message: str):
        self.plugin_name = plugin_name
        self.unsatisfied = unsatisfied   # 未充足の producer plugin 名
        super().__init__(message)


class AnalysisPlugin(ABC):
    name: str
    label: str
    description: str = ""
    depends_on: list[str] = []  # 先に実行が必要なプラグイン名
    heavy: bool = False         # True: 重い計算。Render 軽量モードではブロックしローカル実行を促す
    # UIサイドバーの目的別グルーピング（投資フロー順）。
    # category = サイドバーのグループ見出し。ui_order = 並び順（十の位=カテゴリ帯, 一の位=カテゴリ内順）。
    # カテゴリの表示順は所属エントリの ui_order 昇順で決まる。未設定は末尾。
    category: str = ""
    ui_order: int = 999
    # True: サイドバーへ出さない＝**退役したモデルの置き場**（ADR-0044）。`heavy` とは別軸で、
    # heavy が「Render 軽量モードでブロックする（＝実行環境の制約）」なのに対し、hidden は
    # 「選択肢として勧めない（＝評価の結論）」を表す。レジストリ登録・`get_plugin` /
    # `execute_plugin` / `POST /api/plugins/{name}/run` / `model_comparison` の比較行は**残る**
    # ので、いつでも再評価・復帰できる。削除ではなく hidden にするのは、比較ファミリーの
    # 一員としての役割（ADR-0021「並置して実データで決める」）が退役後も残るため。
    # フィルタは消費側（`routers/analysis.py` の `/api/plugins`）が行う。
    hidden: bool = False

    @abstractmethod
    def params_schema(self) -> dict:
        """UIフォーム定義を返す。各フィールドの type: select/multiselect/slider/number"""
        ...

    @abstractmethod
    def execute(self, params: dict, db: Any) -> dict:
        """分析を実行して結果を返す。

        **同期（def）で実装すること**。イベントループ上では plugins.execute_plugin が
        asyncio.to_thread でワーカースレッドへオフロードするため、CPU-bound な実装でも
        ループを塞がない（heartbeat watchdog の誤停止防止・Issue #357）。`async def` で
        実装すると to_thread が未 await のコルーチンを返して壊れるため禁止
        （ABC の abstractmethod は sync/async を強制できない＝規約で守る）。
        """
        ...

    def produced_output(self, db: Any) -> bool:
        """この plugin が共有DBへ出力を書き終えているか（depends_on の充足判定に使う）。

        他 plugin から depends_on で指される producer が override する。デフォルトは
        True（前提条件を持たない＝常に充足扱い）。例: sector_ols は regression_results に
        書き込み済みかを返し、gap_analysis（depends_on=["sector_ols"]）の前提として使われる。
        """
        return True

    def to_meta(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "depends_on": self.depends_on,
            "heavy": self.heavy,
            "category": self.category,
            "ui_order": self.ui_order,
            "hidden": self.hidden,
            "params_schema": self.params_schema(),
        }
