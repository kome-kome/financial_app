"""ドキュメントが実体から静かに乖離するのを守る（Issue #472・#575）。

本ファイルは3つの照合を持つ。いずれも**乖離しても失敗が出ない**型の穴で、
気づく手段が人間の定期点検（`/tidy`）しか無かったものを機械化している。

1. `docs/MODELS.md` の章 ⇔ 初心者向け副読本の章立て（#472・#1129・下記の `GUIDES`）
2. `docs/SKILLS_AND_AGENTS.md` ⇔ ディスク上のスキル／エージェント実体（#575・後半のクラス）
3. `docs/MODELS.md` の章 ⇔ `templates/models.html` の節（#712・末尾のクラス）

## 1. 副読本の章立て同期（#472・#1129）

副読本（M-1 は §9・M-2 は §11・M-3 は §12 を噛み砕いた読み物）は、
更新頻度が正本と桁で違う（M-1 の副読本は 98 コミット vs 4 コミット・2026-06-27 で停止していた）。
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


class Guide:
    """副読本 1 冊。`section` は追随する MODELS.md の章番号、`source_of_truth` は本文が
    名前を挙げなければならない系列一覧の正本（書き写さずにリンクする宣言の照合用）。"""

    def __init__(self, filename: str, section: str, source_of_truth: str):
        self.path = DOCS / filename
        self.section = section
        self.source_of_truth = source_of_truth

    def __repr__(self) -> str:      # pytest の ID に出る
        return self.path.name


GUIDES = (
    Guide("M1_MACRO_MODEL_GUIDE.md", "9", "_MACRO_MAP"),       # #472
    Guide("M2_MACRO_GBDT_GUIDE.md", "11", "_MACRO_MAP"),       # #1129
    Guide("M3_STATE_SPACE_GUIDE.md", "12", "_DLM_MACRO_MAP"),  # #1129
)

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


def read_marker(guide: Guide) -> re.Match:
    text = guide.path.read_text(encoding="utf-8")
    m = MARKER_RE.search(text)
    assert m, (
        f"{guide.path.name} に models-sync マーカーが無い。冒頭へ "
        f"`<!-- models-sync: section={guide.section} headings=<12桁> -->` を置くこと"
        "（値は本テストの失敗メッセージが教える）"
    )
    return m


def test_every_guide_exists():
    """副読本の表（`GUIDES`）が空振りしていないこと（ファイル名の打ち間違いで照合が消えない）。"""
    missing = [g.path.name for g in GUIDES if not g.path.is_file()]
    assert not missing, f"GUIDES にあるのに docs/ に無い副読本: {missing}"


@pytest.mark.parametrize("guide", GUIDES, ids=repr)
class TestGuideTracksModels:
    """副読本が MODELS.md の対応する章の章立てに追随しているか。"""

    def test_marker_points_at_its_own_section(self, guide):
        """マーカーの章番号が表の章番号と一致すること（M-2 の副読本が §9 を指していても気づける）。"""
        assert read_marker(guide).group("section") == guide.section, (
            f"{guide.path.name} のマーカーが §{read_marker(guide).group('section')} を指している"
            f"（追随するのは §{guide.section}）"
        )

    def test_marker_points_at_an_existing_section(self, guide):
        section = read_marker(guide).group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        assert headings, (
            f"models-sync マーカーが指す MODELS.md §{section} が見つからない。"
            "章番号を振り直したならマーカー側も直すこと"
        )

    def test_section_has_subheadings(self, guide):
        """`### N.x` を1つも拾えないなら抽出ロジックが壊れている（検査が空回りする）。"""
        section = read_marker(guide).group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        assert len(headings) >= 2, (
            f"§{section} の小見出しを抽出できていない: {headings}"
        )

    def test_guide_digest_matches_models_headings(self, guide):
        marker = read_marker(guide)
        section = marker.group("section")
        headings = section_headings(MODELS_MD.read_text(encoding="utf-8"), section)
        expected = headings_digest(headings)
        assert marker.group("digest") == expected, (
            f"MODELS.md §{section} の章立てが変わっている（副読本は追随していない）。\n"
            f"  現在の章立て: {headings}\n"
            f"  {guide.path.name} を読み直して噛み砕きの過不足を直したうえで、"
            f"冒頭マーカーを headings={expected} へ更新すること。\n"
            "  （系列の増減・既定値・実測値は副読本の追随対象外＝正本へのリンクに留める）"
        )


@pytest.mark.parametrize("guide", GUIDES, ids=repr)
class TestGuideDeclaresItsScope:
    """副読本が「何を追随し、何を正本へ委ねるか」を本文で宣言しているか。

    宣言が無いと、次の書き手が良かれと思って系列一覧や実測値を書き写し、
    同じ乖離が再生産される（Issue #472 で実際に起きたのがマクロ系列表）。
    """

    def test_guide_links_to_the_source_of_truth(self, guide):
        text = guide.path.read_text(encoding="utf-8")
        assert guide.source_of_truth in text, (
            f"{guide.path.name} にマクロ系列の正本（{guide.source_of_truth}）への言及が無い。"
            "系列一覧を本書へ書き写すと必ず陳腐化する"
        )
        assert "MODELS.md" in text, f"{guide.path.name} から正本 MODELS.md への参照が消えている"


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

# Claude Code のクラウドの作業環境（claude.ai/code）が設定する環境変数。
# そこにも `~/.claude/skills/` は在るが、中身は同期された組み込みだけで利用者の個人スキル
# （`next-issue` など）は無いので、照合すると変更と無関係に毎回落ちる。ローカルの CLI には無い。
CLOUD_SESSION_ENV_MARKERS = (
    "CLAUDE_CODE_REMOTE_SESSION_ID",
    "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE",
)

# 組み込み／プラグイン由来でディスクに実体を持たないもの。
# **「呼べることを人が確認した印」**であって、在否を証明できないものの掃きだめではない。
# 2026-09-01 に、セッションが提示するスキル一覧へ実際に載っていることを確認して並べた。
# （`/review` は一覧にも `~/.claude/{skills,commands}` にも無かったので索引から削除した）
# `deep-research` は `anthropic-skills` プラグインの同期スキル（`~/.claude/skills/synced/<id>/` 配下で
# 1段の走査に掛からない）。2026-10-19 にセッションのスキル一覧へ載っていることを確認して足した。
BUILTIN_SKILLS = frozenset(
    {
        "claude-api",
        "code-review",
        "deep-research",
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


def cloud_session_marker() -> str | None:
    """Claude Code のクラウドの作業環境（claude.ai/code）なら、その印の環境変数名を返す。

    値は使わず、在るかだけを見る。
    """
    return next((name for name in CLOUD_SESSION_ENV_MARKERS if name in os.environ), None)


def require_user_claude_dir() -> Path:
    # 差し替えは明示の指示なので、クラウドでも最優先で効かせる（skip 判定の前に見る）
    if os.environ.get(USER_CLAUDE_HOME_ENV) is None:
        marker = cloud_session_marker()
        if marker:
            pytest.skip(
                f"Claude Code のクラウドの作業環境（{marker} が在る）。"
                "~/.claude/skills はあっても利用者の個人スキルが無いので照合できない"
                "（この層を照合できるのはローカルの pytest だけ）"
            )
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

    索引はユーザーレベルの実体も案内している（genshijin 系・`security-auditor` など）ため、
    ここを照合しないと #575 の実害の主軸——「`/genshijin` は常時 ON なのに索引には英語版
    `/caveman` しか無く実運用と真逆の案内だった」——を拾えない。CI の checkout には
    `~/.claude/` が無いので、この層はローカルの pytest でしか走らない。Claude Code の
    クラウドの作業環境（claude.ai/code）も、`~/.claude/skills` は在るが個人スキルが無いので skip する。
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
    クラウドの印での skip がローカルにまで及ぶと唯一の砦が消えるので、その逆側も縛る。
    """

    def test_guard_skips_when_user_level_is_absent(self, monkeypatch, tmp_path):
        monkeypatch.setenv(USER_CLAUDE_HOME_ENV, str(tmp_path / "absent"))
        with pytest.raises(pytest.skip.Exception):
            require_user_claude_dir()

    def test_guard_returns_dir_when_user_level_is_present(self, monkeypatch, tmp_path):
        (tmp_path / "skills").mkdir()
        monkeypatch.setenv(USER_CLAUDE_HOME_ENV, str(tmp_path))
        assert require_user_claude_dir() == tmp_path

    @staticmethod
    def _home_with_user_skills(monkeypatch, tmp_path) -> Path:
        """`~/.claude/skills` が在るホームを作り、差し替えとクラウドの印を消す（＝ローカル相当）。"""
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv(USER_CLAUDE_HOME_ENV, raising=False)
        for name in CLOUD_SESSION_ENV_MARKERS:
            monkeypatch.delenv(name, raising=False)
        return tmp_path / ".claude"

    def test_guard_runs_locally_when_home_has_skills(self, monkeypatch, tmp_path):
        """ローカル（クラウドの印が無い）では今までどおり照合が走る。ここで skip されると砦が消える。"""
        expected = self._home_with_user_skills(monkeypatch, tmp_path)
        assert require_user_claude_dir() == expected

    @pytest.mark.parametrize("marker", CLOUD_SESSION_ENV_MARKERS)
    def test_guard_skips_in_cloud_session_even_if_skills_dir_exists(self, monkeypatch, tmp_path, marker):
        """クラウドには `~/.claude/skills` が在っても個人スキルが無いので skip する（値は見ない）。"""
        self._home_with_user_skills(monkeypatch, tmp_path)
        monkeypatch.setenv(marker, "")
        with pytest.raises(pytest.skip.Exception, match="クラウド"):
            require_user_claude_dir()

    def test_override_still_wins_in_cloud_session(self, monkeypatch, tmp_path):
        """差し替え（`FINAPP_TEST_CLAUDE_HOME`）は明示の指示なので、クラウドでも効く。"""
        self._home_with_user_skills(monkeypatch, tmp_path)
        override = tmp_path / "override"
        (override / "skills").mkdir(parents=True)
        monkeypatch.setenv(CLOUD_SESSION_ENV_MARKERS[0], "session_x")
        monkeypatch.setenv(USER_CLAUDE_HOME_ENV, str(override))
        assert require_user_claude_dir() == override

    def test_disk_scanners_are_empty_for_a_bare_dir(self, tmp_path):
        """走査対象が無いディレクトリで例外を出さず空を返すこと（CI 相当の経路）。"""
        assert skills_on_disk(tmp_path) == set()
        assert commands_on_disk(tmp_path) == set()
        assert agents_on_disk(tmp_path) == set()


# ---------------------------------------------------------------------------
# 3. MODELS.md の章 ⇔ models.html の節（Issue #712）
# ---------------------------------------------------------------------------
#
# `docs/MODELS.md`（理論の正本）と `templates/models.html`（画面の解説）は、人が手で
# 同期させる約束しか持たなかった。実装が変わってドキュメントだけ取り残されても
# **失敗として現れない**ため、#712 では乖離4件（最長3ヶ月）が同時に見つかっている。
#
# ここで縛るのは**構造**だけ——「章があるのに節が無い／節があるのに章が無い」と
# 「`#mN` リンクが実在しない節を指している」の2つ。本文の主張が古いこと（#712 の
# 乖離1＝廃止済みの交差項が現役仕様として3ヶ月残った）は**この検査では拾えない**。
# 本文まで機械化するには説明文をプラグインから生成するしかないが、MODELS.md は
# 数式と参考文献を含む理論文書でその性質を持たない。拾えない範囲は `/tidy` に残す。
#
# アンカー `mN` の `N` は MODELS.md の章番号に由来する（ARCHITECTURE.md の画面一覧）。
# #712 で M-6 だけが §16 に対し `id="m15"` とずれていた——§15 が後から差し込まれた
# ときにページ側が追随しなかった跡で、まさにこの検査が拾うべき型だった。

MODELS_HTML = Path(__file__).resolve().parents[1] / "templates" / "models.html"
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

CHAPTER_RE = re.compile(r"^##\s+(?P<num>[0-9]+)\.\s+(?P<title>.+)$")
SECTION_RE = re.compile(r'<div\s+class="model-section"\s+id="m(?P<num>[0-9]+)"\s*>')
ANCHOR_RE = re.compile(r'href="(?:/models)?#m(?P<num>[0-9]+)"')

# MODELS.md の章のうち、models.html に節を持たないもの。**空の理由は不可**
# （理由を書かせないと、次の書き手が「載せ忘れ」をここへ足して検査を殺せる）。
CHAPTERS_WITHOUT_A_SECTION = {
    "1": "exempt: バリュエーション分析（§3）へ統合した墓標節。旧アンカー #m1 は削除済み（ADR-0001）",
    "4": "exempt: M-1（§9）へ集約した墓標節",
    "15": "exempt: 兄弟モデル候補メニューは探索枠でプラグイン実体を持たない（ARCHITECTURE.md の画面一覧）",
}


def models_chapters() -> dict[str, str]:
    """`docs/MODELS.md` の `## N. <題名>` から {章番号: 題名}。"""
    out: dict[str, str] = {}
    for line in MODELS_MD.read_text(encoding="utf-8").splitlines():
        m = CHAPTER_RE.match(line)
        if m:
            out[m.group("num")] = m.group("title").strip()
    return out


def page_sections() -> dict[str, int]:
    """`templates/models.html` の `<div class="model-section" id="mN">` から {章番号: 行番号}。"""
    out: dict[str, int] = {}
    for i, line in enumerate(MODELS_HTML.read_text(encoding="utf-8").splitlines(), start=1):
        m = SECTION_RE.search(line)
        if m:
            out[m.group("num")] = i
    return out


def anchor_refs() -> dict[str, list[str]]:
    """`templates/*.html` の `#mN` リンク → 参照元ファイル名の一覧。"""
    out: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        for m in ANCHOR_RE.finditer(path.read_text(encoding="utf-8")):
            out.setdefault(m.group("num"), []).append(path.name)
    return out


class TestModelsPageTracksModelsDoc:
    """MODELS.md の章と models.html の節が対応しているか（#712）。"""

    def test_extraction_is_not_vacuous(self):
        """抽出が壊れて空集合同士を比べていないか（空の照合は常に緑になる）。"""
        chapters, sections, anchors = models_chapters(), page_sections(), anchor_refs()
        assert len(chapters) >= 10, f"MODELS.md から章を拾えていない: {sorted(chapters)}"
        assert len(sections) >= 10, f"models.html から節を拾えていない: {sorted(sections)}"
        assert len(anchors) >= 10, f"templates から #mN リンクを拾えていない: {sorted(anchors)}"

    def test_every_chapter_has_a_section_or_a_reason(self):
        """MODELS.md にモデル章を足してページへ載せ忘れると落ちる。"""
        sections = page_sections()
        missing = [
            f"§{num} {title}"
            for num, title in sorted(models_chapters().items(), key=lambda kv: int(kv[0]))
            if num not in sections and not CHAPTERS_WITHOUT_A_SECTION.get(num, "").strip()
        ]
        assert not missing, (
            f"MODELS.md の {missing} に対応する節が {MODELS_HTML.name} に無い。"
            '`<div class="model-section" id="m<章番号>">` を足すか、'
            "本ファイルの CHAPTERS_WITHOUT_A_SECTION へ理由付き（`exempt: …`）で登録すること"
        )

    def test_every_section_has_a_chapter(self):
        """ページにだけ残った節・章番号とアンカー番号のずれで落ちる。"""
        chapters = models_chapters()
        orphans = [
            f"id=m{num}（{MODELS_HTML.name}:{line}）"
            for num, line in sorted(page_sections().items(), key=lambda kv: int(kv[0]))
            if num not in chapters
        ]
        assert not orphans, (
            f"{orphans} に対応する `## N.` が {MODELS_MD.name} に無い。"
            "アンカー番号は MODELS.md の章番号に由来する（章を差し込んだら節の id も動かす）"
        )

    def test_exemptions_are_not_stale(self):
        """除外表が実在しない章を指したり、節を持つ章を覆い隠したりしていないか。"""
        chapters, sections = models_chapters(), page_sections()
        unknown = sorted(set(CHAPTERS_WITHOUT_A_SECTION) - set(chapters), key=int)
        assert not unknown, (
            f"CHAPTERS_WITHOUT_A_SECTION の §{unknown} は {MODELS_MD.name} に無い章。"
            "章番号を振り直したなら除外表も直すこと"
        )
        covered = sorted(set(CHAPTERS_WITHOUT_A_SECTION) & set(sections), key=int)
        assert not covered, (
            f"§{covered} は節を持つのに CHAPTERS_WITHOUT_A_SECTION に載っている。"
            "除外したままだとその章の照合が死ぬので、行を外すこと"
        )
        blank = sorted([n for n, why in CHAPTERS_WITHOUT_A_SECTION.items() if not why.strip()], key=int)
        assert not blank, f"§{blank} の除外理由が空。なぜ載せないのかを書くこと"

    def test_anchor_links_resolve(self):
        """`templates/*.html` の `#mN` が実在する節を指しているか（節を改名すると落ちる）。"""
        sections = page_sections()
        broken = [
            f"#m{num}（参照元: {', '.join(sorted(set(srcs)))}）"
            for num, srcs in sorted(anchor_refs().items(), key=lambda kv: int(kv[0]))
            if num not in sections
        ]
        assert not broken, (
            f"{broken} が指す節が {MODELS_HTML.name} に無い。"
            "アンカーを改名したらリンク側も直すこと（切れても失敗として現れない）"
        )
