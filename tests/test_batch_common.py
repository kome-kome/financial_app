"""scripts/batch_common.py の失敗通知（`notify`）— Issue #885。

ローカル駆動バッチ（夜間・月次・日中枠・バックアップ）が共有する起票の規則を縛る。
**同じタイトルの open Issue があればそこへ追記する**——鮮度ゲート（#876）のように直るまで毎晩同じ理由で
落ちる失敗で、晩ごとに Issue が増えないため。`gh` は `run` の差し替えで扱い、本物へは届かない。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import batch_common as bc  # noqa: E402

TITLE = "[ops] ローカル夜間バッチ失敗: {failed}"
FILED = "[ops] ローカル夜間バッチ失敗: scores"


class _FakeGh:
    """`gh` の差し替え。一覧には `issue_list` を返し、他の呼び出しは `returncode` で終わる。"""

    def __init__(self, issue_list="[]", list_returncode=0, returncode=0):
        self.issue_list, self.list_returncode, self.returncode = issue_list, list_returncode, returncode
        self.calls, self.kwargs = [], []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if argv[:3] == ["gh", "issue", "list"]:
            return subprocess.CompletedProcess(argv, self.list_returncode,
                                               stdout=self.issue_list, stderr="list boom")
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr="gh boom")


def _notify(gh, results=None):
    return bc.notify(results or {"pipeline": 0, "scores": 1}, None, TITLE, "本文", run=gh)


class TestNotifyAppendsToTheOpenIssue:
    def test_open_issue_with_the_same_title_gets_a_comment(self):
        gh = _FakeGh(issue_list=f'[{{"number": 7, "title": "無関係"}}, {{"number": 42, "title": "{FILED}"}}]')
        assert _notify(gh) is None
        assert [c[:3] for c in gh.calls] == [["gh", "issue", "list"], ["gh", "issue", "comment"]]
        assert gh.calls[-1][:4] == ["gh", "issue", "comment", "42"]
        assert gh.calls[-1][gh.calls[-1].index("--body") + 1] == "本文"

    def test_a_new_issue_is_created_when_no_title_matches(self):
        # 失敗したステップの組み合わせが違えばタイトルも違う＝別の Issue
        gh = _FakeGh(issue_list='[{"number": 42, "title": "[ops] ローカル夜間バッチ失敗: pipeline, scores"}]')
        assert _notify(gh) is None
        create = gh.calls[-1]
        assert create[:3] == ["gh", "issue", "create"]
        assert create[create.index("--title") + 1] == FILED
        labels = [create[i + 1] for i, a in enumerate(create) if a == "--label"]
        assert labels == list(bc.ISSUE_LABELS)

    def test_the_listing_is_not_filtered_by_label(self):
        """ラベルで絞ると、誰かが ops を外した瞬間に重複起票が始まる。"""
        gh = _FakeGh()
        _notify(gh)
        assert "--label" not in gh.calls[0]

    def test_a_failed_listing_falls_back_to_create(self):
        """重複より沈黙の方が悪い。倒した理由は戻り値でログへ残す。"""
        for gh in (_FakeGh(list_returncode=1), _FakeGh(issue_list="not json")):
            note = _notify(gh)
            assert gh.calls[-1][:3] == ["gh", "issue", "create"]
            assert note and "gh issue list" in note and "新規起票へ倒す" in note

    def test_a_failed_comment_is_reported_not_raised(self):
        gh = _FakeGh(issue_list=f'[{{"number": 42, "title": "{FILED}"}}]', returncode=1)
        note = _notify(gh)
        assert note and "gh issue comment #42 が失敗" in note

    def test_missing_gh_is_reported_not_raised(self):
        def boom(*_a, **_k):
            raise OSError("gh not found")

        assert "gh を起動できない" in _notify(boom)

    def test_nothing_failed_calls_no_gh(self):
        gh = _FakeGh()
        assert _notify(gh, results={"pipeline": 0, "scores": 0}) is None
        assert gh.calls == []

    def test_every_call_resolves_the_repository_from_root(self):
        """起動元の cwd に頼らない。一覧だけ別のフォルダで走ると、失敗して毎回新規起票へ倒れる。"""
        gh = _FakeGh(issue_list=f'[{{"number": 42, "title": "{FILED}"}}]')
        _notify(gh)
        assert [k.get("cwd") for k in gh.kwargs] == [str(bc.ROOT)] * 2
