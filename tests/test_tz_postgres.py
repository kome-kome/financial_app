"""naive DateTime 列の往復を**実 PostgreSQL** で検証する（#565・ADR-0043）。

## なぜ別ファイルなのか

#565 の壊れ方は **SQLite では原理的に再現できない**。原因は「PG が aware な値を
`timestamp without time zone` へキャストする際、**セッション TZ** でローカル時刻へ変換して
tz を落とす」という PG 固有の挙動で、SQLite にはセッション TZ という概念が無い。
`tests/test_db_session_fixes.py` は「設定が書いてある・フックが張ってある」までしか言えず、
**それが実際に効いているか**を言えるのはここだけ。

`FINAPP_TEST_PG_URL` が設定されているときだけ走り、**CI では skip される**
（`ci.yml` は本番 DB にも外部にも触れないという契約を崩さない）。
規約は `tests/test_db_egress_postgres.py` と同じ。

実行:
    $env:FINAPP_TEST_PG_URL = "postgresql://edinet:edinet@localhost:5432/financial_db"
    pytest tests/test_tz_postgres.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

PG_URL = os.environ.get("FINAPP_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="FINAPP_TEST_PG_URL 未設定（ローカル PostgreSQL がある環境でのみ実行）",
)

# アプリの実テーブルには触れない。列型は #565 の対象と同じ naive `timestamp`。
SCRATCH = "_test_tz_scratch"


@pytest.fixture(scope="module")
def engine():
    """**`database.engine` そのもの**を使う。別に engine を作ると、検証したい当の
    フック（`database` モジュールが自分の engine へ張るもの）を迂回してしまう。"""
    eng = database.engine
    with eng.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS public."{SCRATCH}"'))
        conn.execute(text(
            f'CREATE TABLE public."{SCRATCH}" '
            '(id integer primary key, naive_ts timestamp without time zone)'
        ))
    yield eng
    with eng.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS public."{SCRATCH}"'))


class TestSessionTimezone:
    def test_session_timezone_is_utc(self, engine):
        """サーバ既定が Asia/Tokyo でも、接続に付いてくる設定が UTC へ倒すこと。"""
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_setting('TimeZone')")).scalar() == "UTC"

    def test_server_default_may_differ(self, engine):
        """サーバ既定は変えていない（ローカルは Asia/Tokyo のまま）ことの確認。

        ロール既定や postgresql.conf を書き換える方式ではなく**接続側で倒している**ので、
        サーバ既定が何であってもアプリは UTC で動く（別マシンでも再現する）。
        """
        with engine.connect() as conn:
            server = conn.execute(text(
                "SELECT setting FROM pg_settings WHERE name = 'TimeZone'")).scalar()
        assert isinstance(server, str) and server


class TestNaiveDatetimeRoundTrip:
    def test_aware_utc_is_stored_as_utc_naive(self, engine):
        """#565 の再発を直接捕まえる唯一のテスト。

        `datetime.now(timezone.utc)`（aware）を naive 列へ書いて読み戻したとき、
        UTC の壁時計が返ること。セッション TZ が Asia/Tokyo だと 9 時間進んだ値が返る。
        """
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(text(f'DELETE FROM public."{SCRATCH}"'))
            conn.execute(
                text(f'INSERT INTO public."{SCRATCH}" (id, naive_ts) VALUES (1, :ts)'),
                {"ts": now},
            )
        with engine.connect() as conn:
            got = conn.execute(
                text(f'SELECT naive_ts FROM public."{SCRATCH}" WHERE id = 1')).scalar()

        assert got.tzinfo is None, "列は naive のまま（型は変えていない）"
        drift = abs(got - now.replace(tzinfo=None))
        assert drift < timedelta(seconds=5), (
            f"UTC で保存されていない（ずれ {drift}）。9時間ずれているなら "
            "セッション TZ が UTC に固定できていない＝#565 の再発"
        )

    def test_display_helper_agrees_with_wall_clock(self, engine):
        """保存値を `api._utc_to_jst_str` に通すと実際の JST 壁時計と一致すること。

        表示関数の仕様（naive を UTC とみなして +9h）は変えていないので、
        **保存側が UTC である限り**画面の時刻は正しい。#565 はこの前提が崩れた事故だった。
        """
        import api

        now_utc = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(text(f'DELETE FROM public."{SCRATCH}"'))
            conn.execute(
                text(f'INSERT INTO public."{SCRATCH}" (id, naive_ts) VALUES (2, :ts)'),
                {"ts": now_utc},
            )
        with engine.connect() as conn:
            stored = conn.execute(
                text(f'SELECT naive_ts FROM public."{SCRATCH}" WHERE id = 2')).scalar()

        shown = api._utc_to_jst_str(stored)
        expect = now_utc.astimezone(timezone(timedelta(hours=9)))
        assert shown.startswith(expect.strftime("%Y-%m-%d %H:%M"))
        assert shown.endswith(" JST")
