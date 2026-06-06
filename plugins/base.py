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

    @abstractmethod
    def params_schema(self) -> dict:
        """UIフォーム定義を返す。各フィールドの type: select/multiselect/slider/number"""
        ...

    @abstractmethod
    async def execute(self, params: dict, db: Any) -> dict:
        """分析を実行して結果を返す"""
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
            "params_schema": self.params_schema(),
        }
