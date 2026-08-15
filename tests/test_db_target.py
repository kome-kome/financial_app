"""接続先スイッチ `FINAPP_DB_TARGET` の解決とガード（Issue #481 B-1）。

Supabase が Egress 超過で restricted になったときにローカル読取レプリカへ逃げるための
切替口。**既定は prod**＝環境変数を触らなければ従来どおり `DATABASE_URL` を見る。

`resolve_database_url()` は副作用の無い純関数なので、辞書を渡すだけで全分岐を検証できる。
`importlib.reload(database)` を使わずに済むのが要点——reload すると engine が作り直され、
`db_egress` のリスナが死んだ engine ごとに積み上がる。

ガードの強さを2種に分けている点をここで固定する:

- **local 指定なのにリモート → RuntimeError**（明示した人しか踏まないので CI に無害）
- **prod 指定で `DATABASE_URL` 未設定 → 警告どまり**（`ci.yml` は `DATABASE_URL` を渡さずに
  走るため、raise にすると CI が全滅する）
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
    def test_no_env_falls_back_to_local_default(self):
        """後方互換: 何も設定しなければ従来どおりローカル既定へ落ちる。"""
        assert resolve_database_url({}) == ("prod", _LOCAL_DEFAULT_URL)

    def test_prod_uses_database_url(self):
        assert resolve_database_url({"DATABASE_URL": REMOTE}) == ("prod", REMOTE)

    def test_empty_string_is_treated_as_unset(self):
        """空文字の env var（PowerShell で消し損ねた等）を URL として採用しない。"""
        assert resolve_database_url({"FINAPP_DB_TARGET": "", "DATABASE_URL": ""}) == (
            "prod", _LOCAL_DEFAULT_URL)


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
        """**CI 保護の回帰テスト。** ci.yml は DATABASE_URL を渡さずに走るので、
        ここを raise にすると全テストが import 時点で落ちる。警告どまりに固定する。"""
        with caplog.at_level(logging.WARNING, logger="database"):
            target, url = resolve_database_url({})
        assert (target, url) == ("prod", _LOCAL_DEFAULT_URL)
        assert any("DATABASE_URL" in r.message for r in caplog.records), "警告が出ていない"

    def test_prod_with_database_url_is_silent(self, caplog):
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
