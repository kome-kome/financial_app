"""`scripts/resolve_price_suffix.py` の判定・対象選定・ドライラン（#555）。

一度きりの解決スクリプトなので、壊れても**次に走らせるまで誰も気づかない**。
実際に守る価値があるのは3点:

1. **採用ガード** — `.F` が Frankfurt を掴む誤爆を棄却できること
2. **再開可能性** — 途中で落ちても、書けたぶんが次回の対象から外れること
3. **ドライランが書かないこと** — `--apply` を付け忘れた回で本番を書き換えない

HTTP は一切張らない（`decide_suffix` は純関数、対象選定は SQL のみ）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.resolve_price_suffix import (
    decide_suffix, _targets, reject_bucket, REJECT_BUCKET_NOTE,
    REJECT_BUCKET_ORDER, YAHOO_PLACEHOLDER_EXCHANGE,
    PROBE_SUFFIXES, DEFAULT_PROBE_DAYS,
)


def _bars(n: int, volume=1000.0) -> list:
    """`fetch_yahoo_chart` の行の形。既定は約定あり（採用ガードの3条件目・#769）。"""
    return [{"trade_date": f"2026-01-{i % 28 + 1:02d}", "close": 100.0 + i, "volume": volume}
            for i in range(n)]


# E00306（1734.S 北弘電社）の実測形（#769）: 2024-04-11 に上場廃止済みなのに、Yahoo は
# SAP/JPY/実名とともに 2026-07-17 付け・終値657・出来高0 のバーを1本返した。
PHANTOM_1734 = [{"trade_date": "2026-07-17", "open": 657.0, "high": 657.0, "low": 657.0,
                 "close": 657.0, "volume": 0.0}]


FKA_JPY = {"exchangeName": "FKA", "currency": "JPY"}
SAP_JPY = {"exchangeName": "SAP", "currency": "JPY"}
FRA_EUR = {"exchangeName": "FRA", "currency": "EUR"}


class TestDecideSuffix:
    """採用ガードの真理値表。**件数だけを見ない**ための中核。"""

    def test_frankfurt_collision_is_rejected(self):
        """`377A.F` / `6461.F` の実測形。HTTP200・61バーでも別会社なので採らない。

        ガードが無ければ「英数字コードが1社だけ救えた」という**最ももっともらしい誤り**
        として通り、別会社の株価が `stock_price_weekly` へ入る。
        """
        got, reason = decide_suffix([(".S", [], {}), (".F", _bars(61), FRA_EUR)])
        assert got is None
        assert "exchange_mismatch:FRA" in reason

    def test_fukuoka_is_adopted(self):
        got, reason = decide_suffix([(".S", [], {}), (".F", _bars(61), FKA_JPY)])
        assert (got, reason) == (".F", "adopted")

    def test_single_bar_is_adopted(self):
        """バー数の下限は設けない——**約定のある**1本なら採る。

        **バー数で足切りすると、この Issue が救おうとしている低流動銘柄をまさに落とす。**
        （#555 が例に挙げた 1734 北弘電社は、実際には 2024 年に廃止済みで、その1本は
        出来高0の幽霊だった＝下の `test_phantom_bar_without_trades_is_rejected`・#769）
        """
        got, reason = decide_suffix([(".S", _bars(1), SAP_JPY)])
        assert (got, reason) == (".S", "adopted")

    def test_phantom_bar_without_trades_is_rejected(self):
        """#769 の実測形。取引所名・通貨・実名が揃っていても出来高0の1本は採らない。

        採用した結果、2026-07-17 付けの 657円が 2023-03 期の財務行へ押し込まれ、
        Yahoo が記号を消したあとは毎晩「解決済みなのに空」が鳴り続けた。
        """
        got, reason = decide_suffix([(".S", PHANTOM_1734, SAP_JPY)])
        assert got is None
        assert reason == ".S:empty:SAP:no_trades"
        # 期待した取引所の meta は返っている＝`empty` の棚（月次の再プローブで拾えるまま）
        assert reject_bucket(reason) == "empty"

    def test_bars_with_unknown_volume_are_not_evidence(self):
        """出来高が None のバーは約定の証拠として数えない（誤った株価を書かない側へ倒す）。"""
        got, reason = decide_suffix([(".F", _bars(5, volume=None), FKA_JPY)])
        assert got is None
        assert "no_trades" in reason

    def test_one_trade_among_idle_days_is_enough(self):
        """薄商いの現役銘柄は約定の無い日（出来高0）のバーを持つ。1本でも約定があれば採る。"""
        bars = _bars(10, volume=0.0)
        bars[3]["volume"] = 100.0
        got, reason = decide_suffix([(".S", bars, SAP_JPY)])
        assert (got, reason) == (".S", "adopted")

    def test_currency_mismatch_is_rejected(self):
        """取引所名が合っていても通貨が違えば棄却＝ガードが2軸で効く。"""
        got, reason = decide_suffix([(".F", _bars(61), {"exchangeName": "FKA",
                                                        "currency": "USD"})])
        assert got is None
        assert "currency_mismatch:USD" in reason

    def test_nagoya_yhd_is_rejected(self):
        """名証はスコープ外。`.NG` は HTTP200・timestamp 0行・YHD を返す。"""
        got, reason = decide_suffix([(".F", _bars(3), {"exchangeName": "YHD",
                                                       "currency": "JPY"})])
        assert got is None
        assert "exchange_mismatch:YHD" in reason

    def test_both_empty_is_not_found(self):
        got, reason = decide_suffix([(".S", [], {}), (".F", [], {})])
        assert got is None
        assert reason == ".S:not_found,.F:not_found"

    def test_empty_on_expected_exchange_is_marked_empty(self):
        """`231A.F` 等の実測形（2026-08-27）: 期待した取引所・実名を返しながらバーが0本。

        再プローブの価値がある側。`fetch_yahoo_chart` が meta を timestamp より先に読む
        ことの意味がここに出る。（同日の `1734.S` も同じ形だったが、こちらは 2024 年に
        廃止済みの記号だった＝meta だけでは上場の証拠にならない・#769）
        """
        got, reason = decide_suffix([(".S", [], SAP_JPY)])
        assert got is None
        assert reason == ".S:empty:SAP"

    def test_placeholder_shell_is_not_called_empty(self):
        """`9062.F`（日本通運）ほか27社の実測形: `YHD`・currency なし・名前なし。

        Yahoo が知らない記号へ返す空箱で**実在する上場ではない**。これを `empty` に畳むと、
        東証を廃止された大型株が「地方取引所に実在する」かのようにレポートへ並ぶ
        ——`.F`＝Frankfurt と同じ「外部識別子の名前空間の衝突」を棄却側で踏むことになる。
        """
        got, reason = decide_suffix([(".F", [], {"exchangeName": "YHD"})])
        assert got is None
        assert reason == ".F:placeholder:YHD"
        assert reject_bucket(reason) == "placeholder"

    def test_placeholder_constant_matches_observation(self):
        assert YAHOO_PLACEHOLDER_EXCHANGE == "YHD"

    def test_sapporo_wins_when_both_match(self):
        """両方一致（実際には起こらない）でも決定的に `.S` を採る。"""
        got, _ = decide_suffix([(".S", _bars(5), SAP_JPY), (".F", _bars(5), FKA_JPY)])
        assert got == ".S"

    def test_no_probes_is_not_found(self):
        assert decide_suffix([]) == (None, "not_found")

    def test_probe_order_is_sapporo_first(self):
        """順序はレジストリとして固定する（`.S` 採用時に `.F` を叩かない前提）。"""
        assert PROBE_SUFFIXES == (".S", ".F")

    def test_probe_window_is_wide_enough_for_thin_names(self):
        """窓は1年。1リクエストのコストは窓幅に依らないので狭める理由が無い

        （#555 の実測 61バーは約90日窓相当で、薄い銘柄を取りこぼしやすい）。
        """
        assert DEFAULT_PROBE_DAYS >= 365


class TestTargetSelection:
    """対象選定＝再開可能性の本体。"""

    def _seed(self, db, make_company, make_price):
        # 価格ゼロ・未解決（対象）
        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=False))
        # 価格ゼロ・解決済み（既定では対象外＝再開したとき二度手間にならない）
        db.add(make_company(edinet_code="E00002", sec_code="1002",
                            is_active=False, yahoo_suffix=".F"))
        # 価格あり（対象外）
        db.add(make_company(edinet_code="E00003", sec_code="1003", is_active=True))
        db.add(make_price(edinet_code="E00003", trade_date="2026-08-01"))
        # sec_code なし（ティッカーを組めない＝対象外）
        db.add(make_company(edinet_code="E00004", sec_code=None, is_active=False))
        db.commit()

    def test_picks_only_priceless_and_unresolved(self, db, make_company, make_price):
        self._seed(db, make_company, make_price)
        rows = _targets(db, reprobe=False, only=None, limit=None)
        assert [r[1] for r in rows] == ["1001"]

    def test_reprobe_includes_resolved(self, db, make_company, make_price):
        """`--reprobe` は解決済みも測り直す（Yahoo が記号を張り替えた疑いのとき）。"""
        self._seed(db, make_company, make_price)
        rows = _targets(db, reprobe=True, only=None, limit=None)
        assert sorted(r[1] for r in rows) == ["1001", "1002"]

    def test_resume_needs_no_state_file(self, db, make_company, make_price):
        """書けたぶんが次回の対象から自動で外れる＝途中中断からそのまま再開できる。"""
        from database import Company

        self._seed(db, make_company, make_price)
        assert [r[1] for r in _targets(db, False, None, None)] == ["1001"]

        # 1社ぶん書けた状態で中断した、という想定
        db.query(Company).filter(Company.edinet_code == "E00001").update(
            {"yahoo_suffix": ".S"})
        db.commit()
        assert _targets(db, False, None, None) == [], \
            "解決済みの社が再開時の対象に残っている（＝二度手間になる）"

    def test_only_filters_by_sec_code(self, db, make_company, make_price):
        self._seed(db, make_company, make_price)
        assert _targets(db, True, ["1002"], None)[0][1] == "1002"
        assert _targets(db, True, ["9999"], None) == []

    def test_limit_truncates(self, db, make_company, make_price):
        self._seed(db, make_company, make_price)
        assert len(_targets(db, reprobe=True, only=None, limit=1)) == 1

    def test_order_is_deterministic(self, db, make_company, make_price):
        """部分実行が再現するよう sec_code 順に固定する。"""
        self._seed(db, make_company, make_price)
        rows = _targets(db, reprobe=True, only=None, limit=None)
        assert [r[1] for r in rows] == sorted(r[1] for r in rows)

    def test_reprobe_reaches_resolved_with_prices(self, db, make_company, make_price,
                                                  make_weekly):
        """#769: 解決できた社には翌晩から株価が入る。株価ゼロに限ると永久に測り直せない。

        夜間の「解決済みなのに空」が案内する `--reprobe` が、警告の対象に届かなかった。
        日次だけ・週次だけの社も含む（daily は保持窓で trim され weekly だけが残る）。
        """
        self._seed(db, make_company, make_price)
        db.add(make_company(edinet_code="E00005", sec_code="1734",
                            is_active=False, yahoo_suffix=".S"))
        db.add(make_price(edinet_code="E00005", trade_date="2026-07-17"))
        db.add(make_company(edinet_code="E00006", sec_code="1006",
                            is_active=False, yahoo_suffix=".F"))
        db.add(make_weekly(edinet_code="E00006", trade_date="2026-01-09"))
        db.commit()

        assert sorted(r[1] for r in _targets(db, True, None, None)) == \
            ["1001", "1002", "1006", "1734"]
        # 既定（再開可能性）は従来どおり株価ゼロ・未解決だけ
        assert [r[1] for r in _targets(db, False, None, None)] == ["1001"]

    def test_reprobe_skips_unresolved_with_prices(self, db, make_company, make_price):
        """株価を持つ未解決の社（東証の銘柄）は `--reprobe` でも入れない。

        入れると東証と福証の重複上場が `.F` に切り替わる。E00003 が `_seed` のその形。
        """
        self._seed(db, make_company, make_price)
        assert "1003" not in [r[1] for r in _targets(db, True, None, None)]

    def test_targets_carry_the_current_suffix(self, db, make_company, make_price):
        """5列目は現在の接尾辞＝`_resolve` が「解決済みだった社」を見分ける材料。"""
        self._seed(db, make_company, make_price)
        by_sec = {r[1]: r[4] for r in _targets(db, True, None, None)}
        assert by_sec == {"1001": None, "1002": ".F"}


class TestDryRunWritesNothing:
    """`--apply` を付け忘れた回で本番を書き換えない。"""

    def test_resolve_without_apply_does_not_write(self, db, make_company, make_price,
                                                  monkeypatch):
        import asyncio
        from database import Company
        import scripts.resolve_price_suffix as rps

        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=False))
        db.add(make_company(edinet_code="E00003", sec_code="1003", is_active=True))
        db.add(make_price(edinet_code="E00003", trade_date="2026-08-01"))
        db.commit()

        async def fake_chart(http, ticker, d_from, d_to):
            return (_bars(10), SAP_JPY) if ticker.endswith(".S") else ([], {})

        monkeypatch.setattr(rps, "fetch_yahoo_chart", fake_chart)
        targets = _targets(db, False, None, None)
        res = asyncio.run(rps._resolve(db, targets, "20260101", "20260301",
                                       sleep=0, apply=False))

        assert [r["sec_code"] for r in res["adopted"]] == ["1001"]
        assert db.query(Company).filter(
            Company.edinet_code == "E00001").one().yahoo_suffix is None, \
            "ドライランなのに書き込んでいる"

    def test_apply_writes_the_suffix(self, db, make_company, make_price, monkeypatch):
        import asyncio
        from database import Company
        import scripts.resolve_price_suffix as rps

        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=False))
        db.add(make_company(edinet_code="E00003", sec_code="1003", is_active=True))
        db.add(make_price(edinet_code="E00003", trade_date="2026-08-01"))
        db.commit()

        async def fake_chart(http, ticker, d_from, d_to):
            return (_bars(10), FKA_JPY) if ticker.endswith(".F") else ([], {})

        monkeypatch.setattr(rps, "fetch_yahoo_chart", fake_chart)
        targets = _targets(db, False, None, None)
        asyncio.run(rps._resolve(db, targets, "20260101", "20260301",
                                 sleep=0, apply=True))

        assert db.query(Company).filter(
            Company.edinet_code == "E00001").one().yahoo_suffix == ".F"

    def test_frankfurt_is_not_written(self, db, make_company, make_price, monkeypatch):
        """**誤爆が永続化されないこと**——このテストが本 Issue の一番の保険。"""
        import asyncio
        from database import Company
        import scripts.resolve_price_suffix as rps

        db.add(make_company(edinet_code="E00001", sec_code="377A", is_active=False))
        db.add(make_company(edinet_code="E00003", sec_code="1003", is_active=True))
        db.add(make_price(edinet_code="E00003", trade_date="2026-08-01"))
        db.commit()

        async def fake_chart(http, ticker, d_from, d_to):
            # `.T` が404 の英数字コードは `.F` まで到達し Frankfurt を掴む
            return (_bars(61), FRA_EUR) if ticker.endswith(".F") else ([], {})

        monkeypatch.setattr(rps, "fetch_yahoo_chart", fake_chart)
        targets = _targets(db, False, None, None)
        res = asyncio.run(rps._resolve(db, targets, "20260101", "20260301",
                                       sleep=0, apply=True))

        assert res["adopted"] == []
        assert [r["sec_code"] for r in res["rejected"]] == ["377A"]
        assert "FRA" in res["rejected"][0]["reason"]
        assert db.query(Company).filter(
            Company.edinet_code == "E00001").one().yahoo_suffix is None


class TestProbeBucketIsPersisted:
    """棄却理由を DB へ残す（#560）。

    分類する `reject_bucket` は #555 からあったが **printf されて消えていた**ため、
    「取引所は分かっているのに絞り込めない」状態だった。永続化して初めて `--bucket` で
    バケットへ絞れる（#560 の月次は `empty` の数社だけを叩いていた。#841 からは全バケット）。
    """

    def _seed(self, db, make_company, make_price):
        db.add(make_company(edinet_code="E00001", sec_code="1734", is_active=False))
        db.add(make_company(edinet_code="E00003", sec_code="1003", is_active=True))
        db.add(make_price(edinet_code="E00003", trade_date="2026-08-01"))
        db.commit()

    def _run(self, db, monkeypatch, chart, apply=True):
        import asyncio
        import scripts.resolve_price_suffix as rps

        monkeypatch.setattr(rps, "fetch_yahoo_chart", chart)
        targets = _targets(db, False, None, None)
        return asyncio.run(rps._resolve(db, targets, "20260101", "20260301",
                                        sleep=0, apply=apply))

    def test_empty_bucket_is_written(self, db, make_company, make_price, monkeypatch):
        """`SAP` と実名は返るがバーが0本＝再プローブの価値がある社。"""
        from database import Company

        self._seed(db, make_company, make_price)

        async def chart(http, ticker, d_from, d_to):
            return ([], SAP_JPY) if ticker.endswith(".S") else ([], {})

        res = self._run(db, monkeypatch, chart)
        assert res["adopted"] == []
        assert db.query(Company).filter(
            Company.edinet_code == "E00001").one().yahoo_probe_bucket == "empty"

    def test_not_found_bucket_is_written(self, db, make_company, make_price, monkeypatch):
        from database import Company

        self._seed(db, make_company, make_price)

        async def chart(http, ticker, d_from, d_to):
            return ([], {})

        self._run(db, monkeypatch, chart)
        assert db.query(Company).filter(
            Company.edinet_code == "E00001").one().yahoo_probe_bucket == "not_found"

    def test_adoption_clears_the_bucket(self, db, make_company, make_price, monkeypatch):
        """採用できたら棄却理由は消す。**残すと解決済みなのに not_found という
        読めない状態になり、`--bucket` で絞ったときに解決済みを拾い続ける。**"""
        from database import Company

        self._seed(db, make_company, make_price)
        db.query(Company).filter(Company.edinet_code == "E00001").update(
            {"yahoo_probe_bucket": "empty"})
        db.commit()

        async def chart(http, ticker, d_from, d_to):
            return (_bars(10), SAP_JPY) if ticker.endswith(".S") else ([], {})

        self._run(db, monkeypatch, chart)
        row = db.query(Company).filter(Company.edinet_code == "E00001").one()
        assert row.yahoo_suffix == ".S"
        assert row.yahoo_probe_bucket is None

    def test_dry_run_writes_no_bucket(self, db, make_company, make_price, monkeypatch):
        from database import Company

        self._seed(db, make_company, make_price)

        async def chart(http, ticker, d_from, d_to):
            return ([], {})

        self._run(db, monkeypatch, chart, apply=False)
        assert db.query(Company).filter(
            Company.edinet_code == "E00001").one().yahoo_probe_bucket is None

    def test_bucket_filter_narrows_the_targets(self, db, make_company, make_price):
        """月次が5社だけを叩けること＝この Issue の目的そのもの。"""
        db.add(make_company(edinet_code="E00001", sec_code="1734",
                            is_active=False, yahoo_probe_bucket="empty"))
        db.add(make_company(edinet_code="E00002", sec_code="9062",
                            is_active=False, yahoo_probe_bucket="placeholder"))
        db.add(make_company(edinet_code="E00004", sec_code="1005", is_active=False))
        db.commit()

        assert [r[1] for r in _targets(db, False, None, None, bucket="empty")] == ["1734"]
        assert [r[1] for r in _targets(db, False, None, None,
                                       bucket="placeholder")] == ["9062"]
        # 絞らなければ従来どおり全件（未プローブを含む）
        assert len(_targets(db, False, None, None)) == 3


class _HttpError(Exception):
    """`collector_prices._count_yahoo_http_error` が読む形（`.response.status_code`）。"""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.response = type("Resp", (), {"status_code": status})()


def _chart_failing_with(status: int):
    """`fetch_yahoo_chart` の失敗経路の形: 失敗を数えてから ([], {}) を返す。"""
    import collector_prices

    async def chart(http, ticker, d_from, d_to):
        collector_prices._count_yahoo_http_error(_HttpError(status))
        return [], {}
    return chart


class TestReprobeUnresolves:
    """`--reprobe` で棄却された解決済みの社は接尾辞を外す（#769）。

    外さないと、Yahoo が記号を消した社を毎晩「解決済みなのに空」として叩き続ける
    （解決済みは廃止社の見送りを素通りする）。ただし**答えの出ていない失敗では外さない**
    ——外した社は株価を持つ未解決の社になり、`--reprobe` の対象にも戻らない。
    """

    def _seed(self, db, make_company, make_price):
        # E00306 の形: 解決済み `.S`・幽霊の株価1行
        db.add(make_company(edinet_code="E00306", sec_code="1734",
                            is_active=False, yahoo_suffix=".S"))
        db.add(make_price(edinet_code="E00306", trade_date="2026-07-17", close=657.0))
        db.commit()

    def _run(self, db, monkeypatch, chart, apply=True):
        import asyncio
        import scripts.resolve_price_suffix as rps

        monkeypatch.setattr(rps, "fetch_yahoo_chart", chart)
        targets = _targets(db, True, None, None)
        return asyncio.run(rps._resolve(db, targets, "20250101", "20260101",
                                        sleep=0, apply=apply))

    def _row(self, db):
        from database import Company
        db.expire_all()
        return db.query(Company).filter(Company.edinet_code == "E00306").one()

    def test_symbol_gone_clears_the_suffix(self, db, make_company, make_price, monkeypatch):
        """Yahoo が記号を消した（404）＝答えが出ている。接尾辞を外し棄却理由を残す。"""
        self._seed(db, make_company, make_price)
        res = self._run(db, monkeypatch, _chart_failing_with(404))

        row = self._row(db)
        assert row.yahoo_suffix is None
        assert row.yahoo_probe_bucket == "not_found"
        assert [r["sec_code"] for r in res["unresolved"]] == ["1734"]
        assert res["unresolved"][0]["previous_suffix"] == ".S"
        assert res["undecided"] == []

    def test_phantom_bar_clears_the_suffix(self, db, make_company, make_price, monkeypatch):
        """#769 の採用時の形がそのまま返ってきても、出来高0の1本では解決済みを保たない。"""
        self._seed(db, make_company, make_price)

        async def chart(http, ticker, d_from, d_to):
            return (PHANTOM_1734, SAP_JPY) if ticker.endswith(".S") else ([], {})

        res = self._run(db, monkeypatch, chart)
        row = self._row(db)
        assert row.yahoo_suffix is None
        assert row.yahoo_probe_bucket == "empty"
        assert res["adopted"] == []

    @pytest.mark.parametrize("status", [429, 503, 403])
    def test_transient_failure_keeps_the_suffix(self, db, make_company, make_price,
                                                monkeypatch, status):
        """429・5xx・404 以外の 4xx は答えではない。何も書かず判定不能として出す。"""
        self._seed(db, make_company, make_price)
        res = self._run(db, monkeypatch, _chart_failing_with(status))

        row = self._row(db)
        assert row.yahoo_suffix == ".S"
        assert row.yahoo_probe_bucket is None
        assert res["unresolved"] == []
        assert [r["sec_code"] for r in res["undecided"]] == ["1734"]

    def test_still_adoptable_keeps_the_suffix(self, db, make_company, make_price,
                                              monkeypatch):
        self._seed(db, make_company, make_price)

        async def chart(http, ticker, d_from, d_to):
            return (_bars(120), SAP_JPY) if ticker.endswith(".S") else ([], {})

        res = self._run(db, monkeypatch, chart)
        assert self._row(db).yahoo_suffix == ".S"
        assert res["unresolved"] == [] and res["undecided"] == []

    def test_dry_run_clears_nothing(self, db, make_company, make_price, monkeypatch):
        self._seed(db, make_company, make_price)
        res = self._run(db, monkeypatch, _chart_failing_with(404), apply=False)

        row = self._row(db)
        assert row.yahoo_suffix == ".S"
        assert row.yahoo_probe_bucket is None
        assert [r["sec_code"] for r in res["unresolved"]] == ["1734"], \
            "ドライランでも何が外れるかは報告する"

    def test_unresolved_rejection_is_unchanged(self, db, make_company, make_price,
                                               monkeypatch):
        """未解決の社の棄却は従来どおり（バケットだけ）。一時失敗でも書く挙動を変えない。"""
        from database import Company

        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=False))
        db.commit()
        res = self._run(db, monkeypatch, _chart_failing_with(429))

        row = db.query(Company).filter(Company.edinet_code == "E00001").one()
        assert row.yahoo_suffix is None
        assert row.yahoo_probe_bucket == "not_found"
        assert res["unresolved"] == [] and res["undecided"] == []


class TestTransientHttpFailures:
    """解除してよいかの判定＝「答えの出ていない失敗」の数。"""

    def test_not_found_is_an_answer(self):
        from scripts.resolve_price_suffix import transient_http_failures
        assert transient_http_failures(
            {"429": 0, "5xx": 0, "4xx": 2, "404": 2, "other": 0}) == 0

    @pytest.mark.parametrize("stats", [
        {"429": 1, "5xx": 0, "4xx": 0, "404": 0, "other": 0},
        {"429": 0, "5xx": 1, "4xx": 0, "404": 0, "other": 0},
        {"429": 0, "5xx": 0, "4xx": 1, "404": 0, "other": 0},   # 403 等の拒否
        {"429": 0, "5xx": 0, "4xx": 0, "404": 0, "other": 1},   # タイムアウト等
    ])
    def test_everything_else_is_not(self, stats):
        from scripts.resolve_price_suffix import transient_http_failures
        assert transient_http_failures(stats) == 1


class TestRejectBucket:
    """棄却の3分類。**強い信号を優先する**（mismatch > empty > not_found）。"""

    def test_mismatch_wins_over_not_found(self):
        assert reject_bucket(".S:not_found,.F:exchange_mismatch:FRA") == "mismatch"

    def test_empty_wins_over_not_found(self):
        """`.S` は exchangeName=SAP を返すが**バーが0本**、`.F` は知らない記号。

        素朴に "not_found" を含むかで分けると「Yahoo が知らない」群に紛れ、
        再プローブの候補から消える。
        """
        assert reject_bucket(".S:empty:SAP,.F:not_found") == "empty"

    def test_empty_wins_over_placeholder(self):
        """実在（SAP）と空箱（YHD）が混ざったら、実在の方を採る。"""
        assert reject_bucket(".S:empty:SAP,.F:placeholder:YHD") == "empty"

    def test_placeholder_wins_over_not_found(self):
        assert reject_bucket(".S:not_found,.F:placeholder:YHD") == "placeholder"

    def test_pure_not_found(self):
        assert reject_bucket(".S:not_found,.F:not_found") == "not_found"

    def test_currency_mismatch_is_a_mismatch(self):
        assert reject_bucket(".F:currency_mismatch:EUR") == "mismatch"

    def test_every_bucket_has_a_note(self):
        """分類には必ず「何を意味するか」を添える（レポートの見出しに出る）。"""
        for head in REJECT_BUCKET_ORDER:
            assert len(REJECT_BUCKET_NOTE[head]) >= 20

    def test_bucket_order_is_strongest_first(self):
        assert REJECT_BUCKET_ORDER == ("mismatch", "empty", "placeholder", "not_found")
