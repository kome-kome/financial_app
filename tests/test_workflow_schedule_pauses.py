"""schedule を止めたワークフローは、理由・復旧条件・代替経路を yml に残す（Issue #503）。

## なぜ CI で強制するのか

**無実行は failure を出さない。** notify-failure（#414）は run の conclusion を見るので
起動しなかったものは対象外だし、macro-health（#420）が見るのはデータ鮮度であって
「誰が回すはずだったか」ではない。ADR-0031 の「heavy を足したが自動実行が無い」と
同型の穴で、気づく経路が人間の目視しかない。

実例が3つある: sector_ols が自動経路ゼロで gap_ratio が33〜36日前になった（#432）／
M-6 が既定 mu_source なのに tune の matrix に無く手動が唯一の更新経路だった（#443）／
recommend-factor-premia が実行履歴ゼロのまま 37 期の重みで固着していた（#423 子5）。

2026-08-20 の正本反転（#503・ADR-0038）では、Supabase への自動書込を断つために
**4本を同時に止めた**。まとめて止めた直後こそ「なぜ止まっているか」が失われやすいので、
止め方そのものに書式を課す。

## 課す書式

有効な cron を持たず、かつ **コメントアウトされた schedule/cron がテキストに残っている**
ワークフローを「停止中」とみなし、次の3点を要求する:

- **停止理由**（`⛔` マーカー ＋ 参照 Issue 番号）
- **復旧条件** … いつ戻すのか。戻す条件を書けないなら、それは削除すべき設定である
- **代替経路** … 止めている間どこで回すのか。「どこでも回さない」なら鮮度が止まることを明示する

`workflow_dispatch` だけを持ち **もともと cron を持たない**ワークフロー（`collect-macro` など）は
対象外。「止めた」のではなく「最初から手動」なので、書くべき復旧条件が存在しない。
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# コメントアウトされた schedule / cron 行。「かつて cron を持っていた」ことの印。
_COMMENTED_SCHEDULE = re.compile(r"^\s*#\s*(schedule:|-\s*cron:)", re.MULTILINE)

REQUIRED_MARKERS = ("⛔", "復旧条件", "代替経路")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """`on:` は YAML 1.1 で bool True にパースされるため両方を見る。"""
    return doc.get("on", doc.get(True)) or {}


def _active_crons(doc: dict) -> list[str]:
    schedule = _triggers(doc).get("schedule") or []
    return [entry["cron"] for entry in schedule if "cron" in entry]


def paused_workflows() -> list[Path]:
    """有効な cron が無く、コメントアウトされた schedule が残っているもの。"""
    out = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if _active_crons(_load(path)):
            continue
        if _COMMENTED_SCHEDULE.search(text):
            out.append(path)
    return out


class TestPausedSchedulesAreDocumented:
    def test_at_least_one_paused_workflow_is_detected(self):
        """検出ロジック自体が壊れていないこと。

        正規表現を壊すと対象ゼロになり、**全件パスして緑になる**（検査が消えたことに
        気づけない）。2026-08-20 時点で停止中は4本あるので、ゼロなら壊れている。
        正本を Supabase へ戻して全部復帰させたら、この検査ごと畳んでよい。
        """
        assert paused_workflows(), (
            "停止中のワークフローが1本も検出されない。"
            f"検出パターン {_COMMENTED_SCHEDULE.pattern!r} が実ファイルと合っているか確認すること"
        )

    @pytest.mark.parametrize("path", paused_workflows(), ids=lambda p: p.name)
    def test_pause_states_reason_recovery_and_replacement(self, path: Path):
        text = path.read_text(encoding="utf-8")
        missing = [m for m in REQUIRED_MARKERS if m not in text]
        assert not missing, (
            f"{path.name}: schedule を止めているのに {missing} が書かれていない。"
            "止めたこと自体は failure を出さないので、通知でも鮮度ゲートでも拾えない"
            "（#503・ADR-0031 と同型）。なぜ止めたか・いつ戻すか・その間どこで回すかを"
            "同じファイルに書くこと"
        )

    @pytest.mark.parametrize("path", paused_workflows(), ids=lambda p: p.name)
    def test_pause_references_an_issue(self, path: Path):
        """停止の経緯を追える先（Issue 番号）を必ず残す。"""
        text = path.read_text(encoding="utf-8")
        head = text[: text.index("jobs:")] if "jobs:" in text else text
        assert re.search(r"#\d{3,}", head), (
            f"{path.name}: 停止理由に Issue 番号が無い。"
            "後から「なぜ止まっているのか」を辿れる先を残すこと"
        )
