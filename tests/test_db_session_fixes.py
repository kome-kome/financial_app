"""セッション設定が「接続に付いてくる」ことのメタ検査（#565・ADR-0043）。**DB は要らない。**

守る不変条件:

1. **`database.SESSION_FIXES` が TimeZone を UTC へ固定している** — naive な DateTime 列へ
   aware UTC を書くと、PG は**セッション TZ**でローカル時刻へ変換して tz を落とす。
   ローカル PG は実測 `TimeZone=Asia/Tokyo` なので、固定しないと JST naive が入り、
   表示側（`api._utc_to_jst_str`）が更に +9h して**9時間先の時刻**を出す（#565）。
2. **engine を作った時点で connect フックが張られている** — 「呼ぶ人が思い出す」形にすると
   経路ごとに正しさが違う状態へ戻る（`mirror_common` の float8 丸めが実例）。
3. **`mirror_common._SESSION_FIXES` が `database.SESSION_FIXES` を包含する** — 二重定義に
   戻ると、また片方だけが正しい状態になる。**これは失敗として現れない**（両者とも
   「接続できて書き込みも成功する」）ので、CI で照合するしかない。

実行: pytest tests/test_db_session_fixes.py -q
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from scripts import mirror_common as mc  # noqa: E402


class TestSessionFixesContent:
    def test_timezone_is_pinned_to_utc(self):
        assert any("TimeZone" in s and "UTC" in s for s in database.SESSION_FIXES), (
            "TimeZone を UTC へ固定していないと、接続先の既定値（ローカル PG は "
            "Asia/Tokyo）次第で naive DateTime 列が JST で保存される（#565）"
        )

    def test_datestyle_is_pinned(self):
        assert any("DateStyle" in s for s in database.SESSION_FIXES)

    def test_extra_float_digits_is_not_pinned_here(self):
        """float の text 表現を変えるのはミラー固有の要件（#565 の範囲外）。"""
        assert not any("extra_float_digits" in s for s in database.SESSION_FIXES)


class TestSessionFixesAreAttachedToTheEngine:
    def test_engine_registers_a_connect_hook(self):
        """engine を作った時点でフックが張られていること（呼び忘れを構造的に消す）。"""
        src = inspect.getsource(database)
        assert 'listens_for' in src and '"connect"' in src
        assert "SESSION_FIXES" in src

    def test_hook_executes_every_fix(self):
        """フック本体が `SESSION_FIXES` を全件流し、cursor を閉じること。"""
        executed: list[str] = []
        closed: list[bool] = []

        class _Cur:
            def execute(self, sql):
                executed.append(sql)

            def close(self):
                closed.append(True)

        class _Conn:
            def cursor(self):
                return _Cur()

        database._apply_session_fixes(_Conn(), None)
        assert executed == list(database.SESSION_FIXES)
        assert closed == [True]


class TestMirrorReusesTheSameSource:
    def test_mirror_includes_every_shared_fix(self):
        """二重定義に戻したら落ちる（片方だけ正しい状態は沈黙する）。"""
        assert set(database.SESSION_FIXES) <= set(mc._SESSION_FIXES)

    def test_mirror_adds_its_own_float_precision(self):
        """ミラーは float8 の完全往復（17桁）に extra_float_digits >= 1 が要る。"""
        assert any("extra_float_digits" in s for s in mc._SESSION_FIXES)
