"""接続先スイッチ `FINAPP_DB_TARGET` の解決とガード（Issue #481 B-1・#503 で既定を反転）。

**既定は local**（2026-08-20・#503・ADR-0038）＝正本であるローカル PostgreSQL を見る。
反転前の既定は prod だったが、正本が移った以上「環境変数を触らなければ Supabase」は
危険側の既定になる（収集も分析も既定で正本の外を叩き、内容が分岐する）。prod を踏むのは
明示した人だけで、実質 Render 1箇所である。

`resolve_database_url()` は副作用の無い純関数なので、辞書を渡すだけで全分岐を検証できる。
`importlib.reload(database)` を使わずに済むのが要点——reload すると engine が作り直され、
`db_egress` のリスナが死んだ engine ごとに積み上がる。

ガードの強さを2種に分けている点をここで固定する:

- **local 指定なのにリモート → RuntimeError**（`DATABASE_URL_LOCAL` にリモートを入れた事故）
- **prod 指定で `DATABASE_URL` 未設定 → 警告どまり**（raise にすると Render の設定漏れが
  起動失敗として出ず…ではなく逆で、**過去に ci.yml がこの経路を踏んでいた**ため緩くしてある。
  反転後の CI は local 経路を通るが、緩さ自体は Render 側で生きている）
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from database import _LOCAL_DEFAULT_URL, mask_url, resolve_database_url

REMOTE = "postgresql://postgres.abc:secret@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres"


class TestDefaults:
    def test_no_env_resolves_to_local(self):
        """既定は local＝正本を見る（#503 で prod から反転）。"""
        assert resolve_database_url({}) == ("local", _LOCAL_DEFAULT_URL)

    def test_database_url_alone_does_not_reach_supabase(self):
        """**反転の要**: `.env` に `DATABASE_URL` があるだけでは Supabase へ行かない。

        反転前はこれが `("prod", REMOTE)` だった。正本がローカルへ移った後に同じ挙動を
        残すと、収集も分析も既定で Supabase を叩き続けて正本が分岐する。prod は
        明示した人（＝Render）だけが踏む。
        """
        assert resolve_database_url({"DATABASE_URL": REMOTE}) == ("local", _LOCAL_DEFAULT_URL)

    def test_prod_uses_database_url(self):
        assert resolve_database_url(
            {"FINAPP_DB_TARGET": "prod", "DATABASE_URL": REMOTE}) == ("prod", REMOTE)

    def test_empty_string_is_treated_as_unset(self):
        """空文字の env var（PowerShell で消し損ねた等）を URL として採用しない。"""
        assert resolve_database_url({"FINAPP_DB_TARGET": "", "DATABASE_URL": ""}) == (
            "local", _LOCAL_DEFAULT_URL)

    def test_render_falls_back_to_prod_without_explicit_target(self):
        """**Render だけは既定を反転させない。**

        `render.yaml` の `FINAPP_DB_TARGET: prod` は、既存サービスが Blueprint 管理下に
        ない場合に反映されない。反映漏れのまま既定の local へ落ちると、Render は localhost を
        見て「接続失敗」ではなく**空の DB に繋がって0件**になる（#481 B-0 と同型）。
        `RENDER` は Render が必ず設定するので、設定漏れの保険として使う。
        """
        assert resolve_database_url({"RENDER": "true", "DATABASE_URL": REMOTE}) == ("prod", REMOTE)

    def test_explicit_target_beats_render_detection(self):
        """明示指定が最優先。Render 上でローカルを見たい場面（検証）を塞がない。"""
        assert resolve_database_url(
            {"RENDER": "true", "FINAPP_DB_TARGET": "local"})[0] == "local"

    def test_render_detection_does_not_leak_into_local_dev(self):
        """`RENDER` が無い環境（開発機・CI）は既定どおり local。"""
        assert resolve_database_url({"DATABASE_URL": REMOTE})[0] == "local"

    def test_launcher_default_matches_database_default(self):
        """`launch.py` が写している既定値が `database._DEFAULT_TARGET` とずれないこと。

        ランチャーは engine 生成の副作用を避けるため database を import せず、既定値を
        文字列で写している。二重定義は黙って乖離する（ADR-0031 と同型）ので CI で縛る。
        """
        import re
        from pathlib import Path
        from database import _DEFAULT_TARGET

        src = (Path(__file__).resolve().parents[1] / "launch.py").read_text(encoding="utf-8")
        found = re.findall(r'os\.environ\.get\("FINAPP_DB_TARGET"\)\s*or\s*"(\w+)"', src)
        assert found, "launch.py の初期 target を読み取れない（式の形が変わった？）"
        assert set(found) == {_DEFAULT_TARGET}, (
            f"launch.py の既定 {found} が database._DEFAULT_TARGET={_DEFAULT_TARGET!r} と違う"
        )


class TestLocalTarget:
    def test_local_without_url_uses_local_default(self):
        """`DATABASE_URL_LOCAL` 未設定でも動く＝.env を編集せず切り替えられる。"""
        assert resolve_database_url({"FINAPP_DB_TARGET": "local"}) == ("local", _LOCAL_DEFAULT_URL)

    def test_local_uses_database_url_local(self):
        url = "postgresql://a:b@127.0.0.1:5432/mirror"
        assert resolve_database_url(
            {"FINAPP_DB_TARGET": "local", "DATABASE_URL_LOCAL": url}) == ("local", url)

    def test_local_ignores_database_url(self):
        """local のとき本番用の `DATABASE_URL` は一切参照しない（取り違えの芽を断つ）。"""
        target, url = resolve_database_url({"FINAPP_DB_TARGET": "local", "DATABASE_URL": REMOTE})
        assert (target, url) == ("local", _LOCAL_DEFAULT_URL)

    @pytest.mark.parametrize("raw", ["local", "LOCAL", " local ", "Local"])
    def test_target_is_case_and_space_tolerant(self, raw):
        assert resolve_database_url({"FINAPP_DB_TARGET": raw})[0] == "local"


class TestGuards:
    def test_local_pointing_at_remote_raises(self):
        """ミラーのつもりで本番へ書く事故を止める（**このガードが B-1 の本体**）。"""
        with pytest.raises(RuntimeError, match="ローカルではありません"):
            resolve_database_url({"FINAPP_DB_TARGET": "local", "DATABASE_URL_LOCAL": REMOTE})

    def test_guard_message_masks_the_password(self):
        with pytest.raises(RuntimeError) as exc:
            resolve_database_url({"FINAPP_DB_TARGET": "local", "DATABASE_URL_LOCAL": REMOTE})
        assert "secret" not in str(exc.value)
        assert "***" in str(exc.value)

    @pytest.mark.parametrize("bad", ["localhost", "production", "dev", "1"])
    def test_unknown_target_is_rejected(self, bad):
        """打ち間違いを黙って prod に落とさない（ローカルのつもりで本番を叩き続ける事故）。"""
        with pytest.raises(ValueError, match="FINAPP_DB_TARGET"):
            resolve_database_url({"FINAPP_DB_TARGET": bad})

    def test_prod_without_database_url_does_not_raise(self, caplog):
        """**Render 保護の回帰テスト。** prod を明示して `DATABASE_URL` を渡し忘れても
        raise せず警告どまりにする（反転前は ci.yml がこの経路を踏んでいた）。"""
        with caplog.at_level(logging.WARNING, logger="database"):
            target, url = resolve_database_url({"FINAPP_DB_TARGET": "prod"})
        assert (target, url) == ("prod", _LOCAL_DEFAULT_URL)
        assert any("DATABASE_URL" in r.message for r in caplog.records), "警告が出ていない"

    def test_prod_with_database_url_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="database"):
            resolve_database_url({"FINAPP_DB_TARGET": "prod", "DATABASE_URL": REMOTE})
        assert not caplog.records

    def test_default_local_is_silent(self, caplog):
        """既定（local）は警告を出さない＝平常運転がログを汚さない。"""
        with caplog.at_level(logging.WARNING, logger="database"):
            resolve_database_url({"DATABASE_URL": REMOTE})
        assert not caplog.records


class TestSchemeRewrite:
    """Supabase/Heroku は `postgres://` を返すが SQLAlchemy 2.x は `postgresql://` が必要。"""

    def test_rewrite_applies_for_prod(self):
        _, url = resolve_database_url({"DATABASE_URL": "postgres://u:p@db.example.com/x"})
        assert url.startswith("postgresql://")

    def test_rewrite_applies_for_local(self):
        _, url = resolve_database_url(
            {"FINAPP_DB_TARGET": "local", "DATABASE_URL_LOCAL": "postgres://u:p@127.0.0.1/x"})
        assert url.startswith("postgresql://")

    def test_rewrite_happens_before_the_local_guard(self):
        """`postgres://` のままでもローカル判定が効くこと（書き換え順の回帰）。"""
        target, url = resolve_database_url(
            {"FINAPP_DB_TARGET": "local", "DATABASE_URL_LOCAL": "postgres://u:p@localhost/x"})
        assert target == "local" and url == "postgresql://u:p@localhost/x"


class TestMaskUrl:
    @pytest.mark.parametrize("url,expected_hidden", [
        (REMOTE, "secret"),
        ("postgresql://edinet:edinet@localhost:5432/financial_db", "edinet:edinet"),
    ])
    def test_password_is_hidden(self, url, expected_hidden):
        masked = mask_url(url)
        assert expected_hidden not in masked
        assert "***" in masked

    def test_host_and_database_survive(self):
        masked = mask_url(REMOTE)
        assert "pooler.supabase.com" in masked and masked.endswith("/postgres")


class TestModuleState:
    def test_module_exposes_the_resolved_target(self):
        assert database.DB_TARGET in ("prod", "local")

    def test_db_target_info_shape(self):
        info = database.db_target_info()
        assert set(info) == {"db_target", "db_is_local", "db_label"}
        assert isinstance(info["db_is_local"], bool)

    def test_db_target_info_never_leaks_the_connection_string(self):
        """ブラウザへ渡る値なので、資格情報やホスト名を含めない。"""
        label = database.db_target_info()["db_label"]
        assert "://" not in label
        assert "@" not in label
