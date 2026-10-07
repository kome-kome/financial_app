"""ローカル駆動バッチの「いま走っているか」——実行中マーカーの唯一の源。

## なぜ要るか

足跡（`app_settings` の `*_last_run`）は**終わったときにしか書かれない**ので、「走ったか」は
分かっても「いま走っているか」は分からない（`batch_freshness.status_of` の docstring どおり、
あちらは実行中に鳴らない作り）。一方で、画面から重い分析（sector_ols・M 系）を回すと、
夜間・日中・月次のバッチと同じ正本 DB を同時に読み書きする。並走は所要ではなく**結果そのもの**
を変える（seed 固定でも MCMC の発散が 0→344 に増えた実測がある）ので、画面側が「いまバッチが
走っている」を知って止める必要がある。

## 書き手と読み手

- 書き手は `scripts/batch_common.run_batch` だけ（全ローカルバッチが通る唯一の骨格）。
  開始時に書き、ステップの開始と heartbeat ごとに更新し、終了時に消す（`RunningMarker`）。
  手で `python -m scripts.run_*` を叩いた分も同じ経路を通るので拾える。
- 読み手は API（`routers/analysis.py`）。`batch_freshness.WATCHED` の `log_prefix` を回して読む。

## 残骸の扱い

OS ごと落ちると `finally` が走らずマーカーが残る。**残ったマーカーで重い分析を永久に止めない**
ために、マーカー自身が書いた `heartbeat_sec` から導出した期限（`2 × heartbeat_sec + 猶予`）を
過ぎたものは「実行中」と見なさず、`stale`（異常終了の残骸）として別に返す。期限を書き写さず
マーカーに載せた値から導出するので、`batch_common.HEARTBEAT_SEC` を変えても追随する。

## 守ること

- **書けなくてもバッチは落とさない**（`record_footprint` と同じ扱い）。失敗は呼び出し側の
  `warn` へ1回だけ渡す。
- 本モジュールは `scripts/` を import しない（API から import するため。`batch_freshness` は
  読むときだけ遅延 import する）。
- 置き場所は `batch_common.LOG_DIR` と同じ `.logs/`（`tests/test_batch_activity.py` が照合する）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / ".logs"

# heartbeat が1回遅れても「実行中」のまま扱うための余白（秒）。期限は
# `2 × heartbeat_sec + STALE_GRACE_SEC`＝既定で 15分。heartbeat は子を待つ親が刻むので、
# 親が生きている限り 5分ごとに必ず更新される。
STALE_GRACE_SEC = 300.0


def marker_path(log_prefix: str, log_dir: Optional[Path] = None) -> Path:
    """`.logs/<log_prefix>.running.json`。ログ本体（`<prefix>_YYYYMMDD.log`）と並べる。

    既定の置き場所はモジュール属性 `LOG_DIR` を**呼ぶたびに**読む——テストは conftest で
    これを一時フォルダへ差し替える（`run_batch` を呼ぶテストが本物の `.logs` に書いたり、
    走っている本物のバッチのマーカーを消したりしないように）。
    """
    return (log_dir or LOG_DIR) / f"{log_prefix}.running.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunningMarker:
    """1回のバッチ実行ぶんの実行中マーカー（書き手側）。

    `start` → `beat`（ステップ開始と heartbeat ごと）→ `clear` の順に呼ぶ。どれも
    **例外を外へ出さない**——マーカーは画面の安全柵であって、バッチ本体の成否には関係しない。
    書けなかったら `warn` へ1回だけ伝える（毎回出すとログが警告で埋まる）。
    """

    def __init__(self, path: Path, heartbeat_sec: float,
                 warn: Optional[Callable[[str], None]] = None):
        self.path = path
        self.heartbeat_sec = heartbeat_sec
        self.warn = warn
        self._data: dict = {}
        self._warned = False

    def _write(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)       # 読み手が書きかけを読まないよう置き換えで書く
        except OSError as e:
            if not self._warned and self.warn is not None:
                self._warned = True
                self.warn(f"実行中マーカーを書けない（画面の重い分析を止められない）: {e}")

    def start(self) -> None:
        now = _now_iso()
        self._data = {"pid": os.getpid(), "started_at": now, "heartbeat_at": now,
                      "heartbeat_sec": self.heartbeat_sec, "step": None}
        self._write()

    def beat(self, step: Optional[str] = None) -> None:
        if not self._data:
            return
        self._data["heartbeat_at"] = _now_iso()
        if step is not None:
            self._data["step"] = step
        self._write()

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as e:
            if self.warn is not None:
                self.warn(f"実行中マーカーを消せない（{self.heartbeat_sec * 2:.0f}秒強で残骸扱いになる）: {e}")


def _parse(ts) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_activity(now: Optional[datetime] = None, log_dir: Optional[Path] = None) -> dict:
    """いま走っているローカルバッチ（`running`）と、期限切れで残ったマーカー（`stale`）。

    各要素は `label` / `log_prefix` / `started_at` / `heartbeat_at` / `step` / `age_sec`（ISO は UTC）。
    **読めないマーカーは `stale` へ入れる**（実行中とは言えないが、黙って捨てると壊れた
    書き手に気づけない）。
    """
    from batch_freshness import WATCHED            # 遅延 import（モジュール冒頭で scripts を引かない）

    now = now or datetime.now(timezone.utc)
    running: list[dict] = []
    stale: list[dict] = []
    for w in WATCHED:
        path = marker_path(w.log_prefix, log_dir)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            stale.append({"label": w.label, "log_prefix": w.log_prefix, "started_at": None,
                          "heartbeat_at": None, "step": None, "age_sec": None,
                          "note": "マーカーを読めない"})
            continue
        beat = _parse(data.get("heartbeat_at"))
        try:
            hb_sec = float(data.get("heartbeat_sec") or 0)
        except (TypeError, ValueError):
            hb_sec = 0.0
        age = (now - beat).total_seconds() if beat else None
        entry = {"label": w.label, "log_prefix": w.log_prefix,
                 "started_at": data.get("started_at"), "heartbeat_at": data.get("heartbeat_at"),
                 "step": data.get("step"), "age_sec": None if age is None else round(age)}
        if age is not None and hb_sec > 0 and age <= 2 * hb_sec + STALE_GRACE_SEC:
            running.append(entry)
        else:
            stale.append(entry)
    return {"running": running, "stale": stale}
