"""ドキュメントが実体から静かに乖離するのを守る（Issue #472・#575）。

本ファイルは2つの照合を持つ。どちらも**乖離しても失敗が出ない**型の穴で、
気づく手段が人間の定期点検（`/tidy`）しか無かったものを機械化している。

1. `docs/MODELS.md` §9 ⇔ 副読本 `docs/M1_MACRO_MODEL_GUIDE.md` の章立て（#472・下記）
2. `docs/SKILLS_AND_AGENTS.md` ⇔ ディスク上のスキル／エージェント実体（#575・後半のクラス）

## 1. 副読本の章立て同期（#472）

副読本 `docs/M1_MACRO_MODEL_GUIDE.md` は MODELS.md §9 を噛み砕いた読み物だが、
更新頻度が正本と桁で違う（98 コミット vs 4 コミット・副読本は 2026-06-27 で停止）。
**乖離しても失敗が出ない**ため notify-failure でも `/tidy` のリンク検査でも拾えず、
人間が気づくまで古い説明が残り続ける——ADR-0031 で `heavy` の自動実行登録を CI 必須に
したのと同型の穴。

そこで「章立てが変わったら副読本を見直す」だけを機械化する。本文の細かい変更では
落とさない（副読本は設計思想だけを追随し、系列一覧・既定値・実測値は正本へのリンクに
留める設計のため）。章立てが変わるとき＝噛み砕き直しが要るときだけ落ちる。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / "docs"
MODELS_MD = DOCS / "MODELS.md"
GUIDE_MD = DOCS / "M1_MACRO_MODEL_GUIDE.md"

MARKER_RE = re.compile(
    r"<!--\s*models-sync:\s*section=(?P<section>[0-9]+)\s+"
    r"headings=(?P<digest>[0-9a-f]{12})\s*-->"
)

DIGEST_LEN = 12


def section_headings(text: str, section: str) -> list[str]:
    """`## <section>. …` から次の `## ` 直前までの見出し行を順序どおり返す。"""
    head_pat = re.compile(rf"^##\s+{re.escape(section)}\.\s")
    out: list[str] = []
    started = False
    for line in text.splitlines():
        if not started:
            if head_pat.match(line):
                started = True
                out.append(line.strip())
            continue
        if line.startswith("## "):
            break
        if line.startswith("#"):
            out.append(line.strip())
    return out


def headings_digest(headings: list[str]) -> str:
    joined = "\n".join(headings)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:DIGEST_LEN]


def read_marker() -> re.Match:
    text = GUIDE_MD.read_text(encoding="utf-8")
    m = MARKER_RE.search(text)
    assert m, (
        f"{GUIDE_MD.name} に models-sync マーカーが無い。冒頭へ "
        "`<!-- models-sync: section=9 headings=<12桁> -->` を置くこと"
        "（値は本テストの失敗メッセージが教える）"
    )
    return m


class TestM1GuideTracksModels:
    """M-1 副読本が MODELS.md §9 の章立てに追随しているか。"""

    def test_marker_points_at_an_existing_section(self):
        section = read_marker().group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        assert headings, (
            f"models-sync マーカーが指す MODELS.md §{section} が見つからない。"
            "章番号を振り直したならマーカー側も直すこと"
        )

    def test_section_has_subheadings(self):
        """`### 9.x` を1つも拾えないなら抽出ロジックが壊れている（検査が空回りする）。"""
        section = read_marker().group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        assert len(headings) >= 2, (
            f"§{section} の小見出しを抽出できていない: {headings}"
        )

    def test_guide_digest_matches_models_headings(self):
        marker = read_marker()
        section = marker.group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        expected = headings_digest(headings)
        assert marker.group("digest") == expected, (
            f"MODELS.md §{section} の章立てが変わっている（副読本は追随していない）。\n"
            f"  現在の章立て: {headings}\n"
            f"  {GUIDE_MD.name} を読み直して噛み砕きの過不足を直したうえで、"
            f"冒頭マーカーを headings={expected} へ更新すること。\n"
            "  （系列の増減・既定値・実測値は副読本の追随対象外＝正本へのリンクに留める）"
        )


class TestGuideDeclaresItsScope:
    """副読本が「何を追随し、何を正本へ委ねるか」を本文で宣言しているか。

    宣言が無いと、次の書き手が良かれと思って系列一覧や実測値を書き写し、
    同じ乖離が再生産される（Issue #472 で実際に起きたのがマクロ系列表）。
    """

    def test_guide_links_to_the_source_of_truth(self):
        text = GUIDE_MD.read_text(encoding="utf-8")
        assert "_MACRO_MAP" in text, (
            "副読本にマクロ系列の正本（plugins/macro_snapshots.py の _MACRO_MAP）への"
            "言及が無い。系列一覧を本書へ書き写すと必ず陳腐化する"
        )
        assert "MODELS.md" in text, "副読本から正本 MODELS.md への参照が消えている"


# ---------------------------------------------------------------------------
# 2. スキル／エージェント索引 ⇔ ディスク実体（Issue #575）
# ---------------------------------------------------------------------------

INDEX_MD = DOCS / "SKILLS_AND_AGENTS.md"
REPO_CLAUDE = Path(__file__).resolve().parents[1] / ".claude"

SKILL_SECTION = "2"
AGENT_SECTION = "3"

DAGGER = "†"

# 表の1列目からだけ拾う（説明文が別コマンドに言及しているのを「記載あり」と誤認しないため）
SKILL_IN_CELL_RE = re.compile(r"`/([a-z0-9][a-z0-9-]*)`")
AGENT_IN_CELL_RE = re.compile(r"\*\*([A-Za-z][A-Za-z0-9_-]*)\*\*")

USER_CLAUDE_HOME_ENV = "FINAPP_TEST_CLAUDE_HOME"

# 組み込み／プラグイン由来でディスクに実体を持たないもの。
# **「呼べることを人が確認した印」**であって、在否を証明できないものの掃きだめではない。
# 2026-09-01 に、セッションが提示するスキル一覧へ実際に載っていることを確認して並べた。
# （`/review` は一覧にも `~/.claude/{skills,commands}` にも無かったので索引から削除した）
BUILTIN_SKILLS = frozenset(
    {
        "claude-api",
        "code-review",
        "fewer-permission-prompts",
        "init",
        "keybindings-help",
        "loop",
        "run",
        "schedule",
        "security-review",
        "simplify",
        "update-config",
    }
)

BUILTIN_AGENTS = frozenset(
    {
        "claude",
        "claude-code-guide",
        "Explore",
        "general-purpose",
        "Plan",
        "statusline-setup",
    }
)


def section_rows(text: str, section: str) -> list[str]:
    """`## <section>. …` から次の `## ` 直前までの、表の行だけを返す。"""
    head_pat = re.compile(rf"^##\s+{re.escape(section)}\.\s")
    out: list[str] = []
    started = False
    for line in text.splitlines():
        if not started:
            if head_pat.match(line):
                started = True
            continue
        if line.startswith("## "):
            break
        if line.startswith("|"):
            out.append(line)
    return out


def first_cell(row: str) -> str:
    return row.strip().strip("|").split("|")[0].strip()


def indexed_skills() -> dict[str, bool]:
    """索引 §2 のスキル名 → † が付いているか（＝この環境に未収録と宣言している）。"""
    out: dict[str, bool] = {}
    for row in section_rows(INDEX_MD.read_text(encoding="utf-8"), SKILL_SECTION):
        cell = first_cell(row)
        m = SKILL_IN_CELL_RE.search(cell)
        if m:
            out[m.group(1)] = DAGGER in cell
    return out


def indexed_agents() -> dict[str, bool]:
    """索引 §3 のエージェント名 → † が付いているか。"""
    out: dict[str, bool] = {}
    for row in section_rows(INDEX_MD.read_text(encoding="utf-8"), AGENT_SECTION):
        cell = first_cell(row)
        m = AGENT_IN_CELL_RE.search(cell)
        if m:
            out[m.group(1)] = DAGGER in cell
    return out


def skills_on_disk(base: Path) -> set[str]:
    """`<base>/skills/<name>/SKILL.md` の <name>。"""
    d = base / "skills"
    return {p.parent.name for p in d.glob("*/SKILL.md")} if d.is_dir() else set()


def commands_on_disk(base: Path) -> set[str]:
    """`<base>/commands/<name>.md`＝ユーザー定義のスラッシュコマンド（スキルと同じ呼び方をする）。"""
    d = base / "commands"
    return {p.stem for p in d.glob("*.md")} if d.is_dir() else set()


def agents_on_disk(base: Path) -> set[str]:
    """`<base>/agents/<name>.md` の <name>。"""
    d = base / "agents"
    return {p.stem for p in d.glob("*.md")} if d.is_dir() else set()


def user_claude_dir() -> Path:
    """ユーザーレベルの Claude 設定ディレクトリ（環境変数で差し替え可＝CI 相当をローカルで再現できる）。"""
    override = os.environ.get(USER_CLAUDE_HOME_ENV)
    return Path(override) if override else Path.home() / ".claude"


def require_user_claude_dir() -> Path:
    base = user_claude_dir()
    if not (base / "skills").is_dir():
        pytest.skip(
            f"ユーザーレベル {base} が無い（CI では常にここで skip される。"
            "この層を照合できるのはローカルの pytest だけ）"
        )
    return base


class TestRepoSkillsAndAgentsAreIndexed:
    """リポジトリに入っているスキル／エージェントが索引に載っているか（**CI で走る唯一の層**）。

    `docs/SKILLS_AND_AGENTS.md` は「何が使えるか」の唯一の早見表なのに、実体と乖離しても
    **失敗として現れない**（載っていなければ存在を知らないまま使われず、載っていて実体が
    無ければ呼んで空振りする）。2026-08-30 の `/tidy` で `/verify` の空振りと genshijin 系
    4本の欠落が偶然見つかったのが #575 の発端で、ADR-0031（heavy の自動実行登録を CI 必須に
    した）と同型の穴。

    ただし CI で縛れるのは **git 管理下の `.claude/` だけ**。`~/.claude/` 配下は checkout に
    含まれないため、下の `TestUserLevelMatchesIndex` は CI では必ず skip される
    （`tests/test_tz_postgres.py` が実 PostgreSQL を要求して CI では skip されるのと同型の割り切り）。
    """

    def test_extraction_is_not_vacuous(self):
        """抽出が壊れて空回りしていないか（空集合同士の照合は常に緑になる）。"""
        skills, agents = indexed_skills(), indexed_agents()
        assert len(skills) >= 20, f"§{SKILL_SECTION} からスキルを拾えていない: {sorted(skills)}"
        assert len(agents) >= 10, f"§{AGENT_SECTION} からエージェントを拾えていない: {sorted(agents)}"

    def test_repo_skills_are_indexed(self):
        indexed = indexed_skills()
        missing = sorted(skills_on_disk(REPO_CLAUDE) - set(indexed))
        assert not missing, (
            f".claude/skills/ の {missing} が {INDEX_MD.name} §{SKILL_SECTION} に無い。"
            "表へ1行足すこと（載っていないスキルは存在を知られないまま使われない）"
        )

    def test_repo_agents_are_indexed(self):
        indexed = indexed_agents()
        missing = sorted(agents_on_disk(REPO_CLAUDE) - set(indexed))
        assert not missing, (
            f".claude/agents/ の {missing} が {INDEX_MD.name} §{AGENT_SECTION} に無い。"
            "表へ1行足すこと"
        )

    def test_repo_entries_are_not_marked_missing(self):
        """リポジトリに実体があるのに † （未収録）扱いになっていないか。"""
        skills, agents = indexed_skills(), indexed_agents()
        wrong = sorted(
            [n for n in skills_on_disk(REPO_CLAUDE) if skills.get(n)]
            + [n for n in agents_on_disk(REPO_CLAUDE) if agents.get(n)]
        )
        assert not wrong, (
            f"{wrong} は .claude/ に実体があるのに索引で {DAGGER}（未収録）扱い。{DAGGER} を外すこと"
        )


class TestUserLevelMatchesIndex:
    """`~/.claude/` のスキル／コマンド／エージェントと索引の両方向照合（**CI では skip**）。

    索引はユーザーレベルの実体も案内している（genshijin 系・`implementer` など）ため、
    ここを照合しないと #575 の実害の主軸——「`/genshijin` は常時 ON なのに索引には英語版
    `/caveman` しか無く実運用と真逆の案内だった」——を拾えない。CI の checkout には
    `~/.claude/` が無いので、この層はローカルの pytest でしか走らない。
    """

    def test_user_entities_are_indexed(self):
        base = require_user_claude_dir()
        skills, agents = indexed_skills(), indexed_agents()
        missing = sorted(
            (skills_on_disk(base) | commands_on_disk(base)) - set(skills)
        ) + sorted(agents_on_disk(base) - set(agents))
        assert not missing, (
            f"ユーザーレベル（{base}）の {missing} が {INDEX_MD.name} に無い。"
            f"§{SKILL_SECTION}／§{AGENT_SECTION} の表へ足すこと"
            "（この検査は CI では skip されるので、ローカルの pytest が唯一の砦）"
        )

    def test_indexed_entries_exist_somewhere(self):
        """† 無しで載っているものが本当に呼べるか（`/verify` 型の空振りを落とす）。"""
        base = require_user_claude_dir()
        on_disk_skills = (
            skills_on_disk(base)
            | commands_on_disk(base)
            | skills_on_disk(REPO_CLAUDE)
            | BUILTIN_SKILLS
        )
        on_disk_agents = agents_on_disk(base) | agents_on_disk(REPO_CLAUDE) | BUILTIN_AGENTS
        ghosts = sorted(
            [n for n, dag in indexed_skills().items() if not dag and n not in on_disk_skills]
        ) + sorted(
            [n for n, dag in indexed_agents().items() if not dag and n not in on_disk_agents]
        )
        assert not ghosts, (
            f"索引に {DAGGER} 無しで載っている {ghosts} の実体が見つからない"
            f"（{base} と .claude/ とホワイトリストを探した）。"
            f"呼んでも空振りするので削除するか {DAGGER} を付けること。"
            "組み込み／プラグイン由来で実在するなら BUILTIN_SKILLS / BUILTIN_AGENTS へ足す"
            "（＝呼べることを人が確認した印）"
        )

    def test_dagger_entries_are_really_absent(self):
        """† は「未収録」の宣言。実体が入ったのに † のままだと読み手が使わずに損をする。"""
        base = require_user_claude_dir()
        present = skills_on_disk(base) | commands_on_disk(base) | skills_on_disk(REPO_CLAUDE)
        wrong = sorted([n for n, dag in indexed_skills().items() if dag and n in present])
        assert not wrong, f"{wrong} は実体があるのに {DAGGER} が付いている。{DAGGER} を外すこと"

    def test_whitelist_holds_only_entries_without_a_file(self):
        """ホワイトリストが実体持ちを覆い隠していないか（覆えばそこだけ照合が死ぬ）。"""
        base = require_user_claude_dir()
        overlap = sorted(
            BUILTIN_SKILLS & (skills_on_disk(base) | commands_on_disk(base) | skills_on_disk(REPO_CLAUDE))
        ) + sorted(BUILTIN_AGENTS & (agents_on_disk(base) | agents_on_disk(REPO_CLAUDE)))
        assert not overlap, (
            f"{overlap} はディスクに実体があるのに BUILTIN_* に載っている。"
            "ホワイトリストから外すこと（実体側の照合が効かなくなる）"
        )


class TestUserLevelGuard:
    """skip の分岐そのものを縛る。

    skip 条件が事故で常時成立していても「緑」に見えるだけなので、ここが無いと
    `TestUserLevelMatchesIndex` が黙って空振りし続けても誰も気づけない。
    """

    def test_guard_skips_when_user_level_is_absent(self, monkeypatch, tmp_path):
        monkeypatch.setenv(USER_CLAUDE_HOME_ENV, str(tmp_path / "absent"))
        with pytest.raises(pytest.skip.Exception):
            require_user_claude_dir()

    def test_guard_returns_dir_when_user_level_is_present(self, monkeypatch, tmp_path):
        (tmp_path / "skills").mkdir()
        monkeypatch.setenv(USER_CLAUDE_HOME_ENV, str(tmp_path))
        assert require_user_claude_dir() == tmp_path

    def test_disk_scanners_are_empty_for_a_bare_dir(self, tmp_path):
        """走査対象が無いディレクトリで例外を出さず空を返すこと（CI 相当の経路）。"""
        assert skills_on_disk(tmp_path) == set()
        assert commands_on_disk(tmp_path) == set()
        assert agents_on_disk(tmp_path) == set()
