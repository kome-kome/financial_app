from abc import ABC, abstractmethod
from typing import Any


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

    def to_meta(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "depends_on": self.depends_on,
            "heavy": self.heavy,
            "params_schema": self.params_schema(),
        }
