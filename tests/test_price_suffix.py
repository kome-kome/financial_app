"""Yahoo ティッカーのサフィックス解決（#555）— 器の部分のテスト。

背景: `collector_prices.py` の Yahoo ティッカー生成は4箇所すべてが `f"{sec_code}.T"`
固定で、**東証以外の単独上場銘柄は原理的に取得できない**という欠測が「例外を出さずに
落ちる」形で常設化していた（札証/福証の単独上場38社が全モデルの母集団から脱落）。

ここで固定するのは3つ:

1. **集約ヘルパの意味論** — NULL は「未解決＝.T」の1つの意味しか持たない
2. **誤爆ガード** — `.F` は Frankfurt と衝突し、HTTP200 で61バーを返しながら
   `exchangeName=FRA`＝別会社（454社中2社が実測で誤爆）。件数だけ見ていると
   気づかずに別会社の株価を書き込む
3. **直書きが復活しないこと** — ヘルパを作っただけでは次に5箇所目を書く人を止められない

外部 HTTP は httpx 組み込みの MockTransport で擬似（新規依存なし）。
"""
import os
import re
import sys
import asyncio

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector_prices as cp
from collector_utils import (
    YAHOO_SUFFIX_TSE, YAHOO_LOCAL_EXCHANGES, YAHOO_EXPECT_CURRENCY,
    yahoo_ticker, yahoo_expect_exchanges, yahoo_guard_kwargs,
)


# ── Yahoo v8 chart レスポンスの組み立て（meta 付き）─────────────────────────
# test_collect_macro.py の `_yahoo_json` は **meta を持たない**。あちらを触らずに
# ここでローカル定義することで、「meta 無しペイロードでも従来どおり解析できる」ことが
# あちら側の回帰テストとして生き続ける。
TS0 = 1672617600  # 2023-01-02 00:00 UTC 付近


def _chart_json(n_bars: int, meta: dict = None, with_timestamp: bool = True) -> dict:
    closes = [float(100 + i) for i in range(n_bars)]
    result = {}
    if meta is not None:
        result["meta"] = meta
    if with_timestamp:
        result["timestamp"] = [TS0 + 86400 * i for i in range(n_bars)]
        result["indicators"] = {"quote": [{
            "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": [1000] * n_bars,
        }]}
    return {"chart": {"result": [result]}}


def _client(payload=None, status: int = 200):
    """MockTransport 付きの AsyncClient。payload が None なら status だけ返す。"""
    def handler(request):
        if payload is None:
            return httpx.Response(status)
        return httpx.Response(status, json=payload)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fetch_chart(payload=None, status: int = 200, ticker: str = "1001.F"):
    async def _go():
        async with _client(payload, status) as s:
            return await cp.fetch_yahoo_chart(s, ticker, "20260101", "20260301")
    return asyncio.run(_go())


def _fetch_history(payload=None, status: int = 200, ticker: str = "1001.F", **kw):
    async def _go():
        async with _client(payload, status) as s:
            return await cp.fetch_yahoo_history(s, ticker, "20260101", "20260301", **kw)
    return asyncio.run(_go())


# ── 1. 集約ヘルパ ──────────────────────────────────────────────────────────
class TestYahooTickerHelper:

    def test_default_is_tokyo(self):
        assert yahoo_ticker("1001") == "1001.T"

    def test_none_means_tokyo(self):
        """NULL は「未解決＝.T」の1つの意味しか持たない（3状態目を作らない）。"""
        assert yahoo_ticker("1001", None) == yahoo_ticker("1001") == "1001.T"

    def test_empty_string_means_tokyo(self):
        """空文字も .T へ倒す（DB から "" が来ても "1001" という壊れたティッカーにしない）。"""
        assert yahoo_ticker("1001", "") == "1001.T"

    @pytest.mark.parametrize("suffix,expected", [(".S", "1001.S"), (".F", "1001.F")])
    def test_local_exchange_suffixes(self, suffix, expected):
        assert yahoo_ticker("1001", suffix) == expected

    def test_alphanumeric_sec_code_is_preserved(self):
        """実データには `589A` のような英数字コードがある（4桁数字だけではない）。"""
        assert yahoo_ticker("589A", ".F") == "589A.F"
        assert yahoo_ticker("377A") == "377A.T"

    def test_registry_is_exactly_the_measured_pair(self):
        """レジストリ照合。実測していない取引所を黙って足させない（#555 の範囲の固定）。

        名証（`.NG`）は HTTP200・timestamp 0行・`exchangeName=YHD` で取得手段が無く、
        `.T` の exchangeName は #555 で測っていない。**未実測の定数を置かない。**
        """
        assert YAHOO_LOCAL_EXCHANGES == {".S": "SAP", ".F": "FKA"}
        assert YAHOO_SUFFIX_TSE == ".T"
        assert YAHOO_EXPECT_CURRENCY == "JPY"

    def test_expect_exchanges_for_local(self):
        assert yahoo_expect_exchanges(".S") == frozenset({"SAP"})
        assert yahoo_expect_exchanges(".F") == frozenset({"FKA"})

    def test_expect_exchanges_is_none_for_tokyo(self):
        """`.T`（＝未解決を含む）は検証しない＝従来の4,000社経路の意味論が不変。"""
        assert yahoo_expect_exchanges(None) is None
        assert yahoo_expect_exchanges(".T") is None
        assert yahoo_expect_exchanges("") is None

    def test_guard_kwargs_is_empty_for_tokyo(self):
        """**東証経路はキーワードを1つも渡さない。**

        既存の偽関数（`async def _fake_fetch(http, ticker, d_from, d_to)`）が
        そのまま通ることの根拠であり、「新しい引数が既定で全社に効き始める」事故を
        構造的に防ぐ意図の表明でもある。
        """
        assert yahoo_guard_kwargs(None) == {}
        assert yahoo_guard_kwargs(".T") == {}
        assert yahoo_guard_kwargs("") == {}

    def test_guard_kwargs_for_local_exchange(self):
        assert yahoo_guard_kwargs(".F") == {
            "expect_exchanges": frozenset({"FKA"}), "expect_currency": "JPY"}
        assert yahoo_guard_kwargs(".S") == {
            "expect_exchanges": frozenset({"SAP"}), "expect_currency": "JPY"}

    def test_guard_kwargs_are_accepted_by_fetch(self):
        """返した dict がそのまま `fetch_yahoo_history` の実シグネチャに嵌ること。

        キーワード名の typo は本番で「常に空リスト」ではなく TypeError になるが、
        地方取引所38社しか通らない経路なので気づくのが遅れる。ここで縛る。
        """
        payload = _chart_json(3, {"exchangeName": "FKA", "currency": "JPY"})
        assert len(_fetch_history(payload, **yahoo_guard_kwargs(".F"))) == 3


# ── 2. 誤爆ガード（純関数）──────────────────────────────────────────────────
class TestExchangeGuard:

    LOCAL = frozenset({"SAP", "FKA"})

    def test_frankfurt_is_rejected(self):
        """`377A.F` / `6461.F` の実測ケース。HTTP200・61バーでも別会社。"""
        meta = {"exchangeName": "FRA", "currency": "EUR"}
        assert cp._meta_matches(meta, self.LOCAL, "JPY") is False

    def test_fukuoka_is_accepted(self):
        meta = {"exchangeName": "FKA", "currency": "JPY"}
        assert cp._meta_matches(meta, self.LOCAL, "JPY") is True

    def test_currency_alone_can_reject(self):
        """取引所名が合っていても通貨が違えば弾く（ガードが2軸で効くことの確認）。"""
        meta = {"exchangeName": "FKA", "currency": "EUR"}
        assert cp._meta_matches(meta, self.LOCAL, "JPY") is False

    def test_empty_meta_is_rejected_when_checking(self):
        """検証を要求した以上、素性が分からないものは採らない。"""
        assert cp._meta_matches({}, self.LOCAL, "JPY") is False
        assert cp._meta_matches(None, self.LOCAL, "JPY") is False

    def test_no_expectation_accepts_anything(self):
        """期待値が None＝検証しない。`.T` 経路が1ビットも変わらないことの根拠。"""
        assert cp._meta_matches({}, None, None) is True
        assert cp._meta_matches({"exchangeName": "FRA", "currency": "EUR"}, None, None) is True

    def test_nagoya_yhd_is_not_in_registry(self):
        """名証（YHD）はスコープ外＝採用集合に入っていないので棄却される。"""
        meta = {"exchangeName": "YHD", "currency": "JPY"}
        assert cp._meta_matches(meta, self.LOCAL, "JPY") is False


# ── 3. fetch_yahoo_chart / fetch_yahoo_history の meta 経路 ────────────────
class TestFetchYahooChartMeta:

    LOCAL = frozenset({"SAP", "FKA"})

    def test_chart_returns_rows_and_meta(self):
        meta = {"exchangeName": "FKA", "currency": "JPY"}
        rows, got = _fetch_chart(_chart_json(61, meta))
        assert len(rows) == 61
        assert got == meta

    def test_history_rejects_frankfurt(self):
        """誤爆は空リストへ畳む（呼び出し側の分岐を増やさない）。"""
        payload = _chart_json(61, {"exchangeName": "FRA", "currency": "EUR"})
        rows = _fetch_history(payload, expect_exchanges=self.LOCAL, expect_currency="JPY")
        assert rows == []

    def test_chart_still_surfaces_rejected_rows(self):
        """棄却されたケースでも `fetch_yahoo_chart` は中身を返す。

        解決スクリプトのレポートが「404 だった」と「FRA を掴んだ」を区別できるために要る。
        """
        payload = _chart_json(61, {"exchangeName": "FRA", "currency": "EUR"})
        rows, meta = _fetch_chart(payload)
        assert len(rows) == 61
        assert meta["exchangeName"] == "FRA"

    def test_history_accepts_matching_exchange(self):
        payload = _chart_json(61, {"exchangeName": "FKA", "currency": "JPY"})
        rows = _fetch_history(payload, expect_exchanges=self.LOCAL, expect_currency="JPY")
        assert len(rows) == 61

    def test_single_bar_is_kept(self):
        """低流動を落とさない。1734（北弘電社・札証）は61営業日中1日しか約定しない。"""
        payload = _chart_json(1, {"exchangeName": "SAP", "currency": "JPY"})
        rows = _fetch_history(payload, expect_exchanges=self.LOCAL, expect_currency="JPY")
        assert len(rows) == 1

    def test_meta_survives_missing_timestamp(self):
        """名証（`.NG`）型: HTTP200・timestamp 0行・exchangeName=YHD。

        meta を timestamp より**先に**読んでいないと、これが 404 と区別できなくなる。
        """
        payload = _chart_json(0, {"exchangeName": "YHD", "currency": "JPY"},
                              with_timestamp=False)
        rows, meta = _fetch_chart(payload)
        assert rows == []
        assert meta["exchangeName"] == "YHD"

    def test_http_error_returns_empty_pair(self):
        rows, meta = _fetch_chart(None, status=404)
        assert (rows, meta) == ([], {})

    def test_bad_date_returns_empty_pair(self):
        async def _go():
            async with _client(_chart_json(3, {})) as s:
                return await cp.fetch_yahoo_chart(s, "1001.T", "not-a-date", "20260301")
        assert asyncio.run(_go()) == ([], {})

    def test_no_expectation_keeps_legacy_behaviour(self):
        """**PR1 が無挙動変更である証拠。**

        `expect_*` を渡さなければ FRA だろうと meta 欠落だろうと従来どおり行が返る。
        既存4呼び出しは全社 yahoo_suffix=NULL の間これらを渡さない。
        """
        payload = _chart_json(61, {"exchangeName": "FRA", "currency": "EUR"})
        assert len(_fetch_history(payload)) == 61
        assert len(_fetch_history(_chart_json(61))) == 61   # meta キー自体が無い


# ── 4. 直書きの復活を止めるメタテスト ──────────────────────────────────────
class TestNoRawTickerLiteralsRemain:
    """`.T` 直書きが4箇所に散っていたことがこの Issue の根本原因。

    ヘルパを作っただけでは次に5箇所目を書く人を止められないので、
    レジストリ照合で機械的に縛る（test_column_scoping の FULL_ROW_LOADS と同型）。
    """

    # 許可する直書き。値は理由（20文字以上を必須にして「とりあえず登録」を防ぐ）。
    ALLOWED = {
        '"1306.T"': "MACRO_SERIES の TOPIX 代理 ETF。銘柄マスタ由来ではない静的な系列定義なので "
                    "sec_code からの動的生成には該当しない",
    }

    def _hits(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "collector_prices.py")
        with open(path, encoding="utf-8") as f:
            return [(i, ln.rstrip("\n")) for i, ln in enumerate(f, 1)
                    if re.search(r'\.T"', ln)]

    def test_every_reason_is_substantive(self):
        for token, reason in self.ALLOWED.items():
            assert len(reason) >= 20, f"{token} の理由が短すぎる: {reason}"

    def test_only_allowlisted_literals_remain(self):
        unexpected = [
            (i, ln.strip()) for i, ln in self._hits()
            if not any(tok in ln for tok in self.ALLOWED)
        ]
        assert not unexpected, (
            "collector_prices.py に Yahoo ティッカーの直書きが増えている。\n"
            "sec_code から組むなら yahoo_ticker(sec_code, suffix) を使うこと"
            "（.T 決め打ちは地方取引所の単独上場を静かに落とす・#555）。\n"
            "静的な系列定義など例外なら TestNoRawTickerLiteralsRemain.ALLOWED へ理由付きで足す。\n"
            f"該当行: {unexpected}"
        )

    def test_allowlist_entries_are_still_present(self):
        """許可リストが陳腐化して形骸化していないこと（消えた行を守り続けない）。"""
        lines = [ln for _, ln in self._hits()]
        for token in self.ALLOWED:
            assert any(token in ln for ln in lines), \
                f"{token} は collector_prices.py に存在しない。ALLOWED から外すこと"

    def test_company_ticker_sites_go_through_helper(self):
        """4箇所すべてが集約ヘルパを通っていること（呼び出し側の実数で担保）。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "collector_prices.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert src.count("yahoo_ticker(") >= 4, \
            "yahoo_ticker() の利用が4箇所未満＝どこかが直書きへ戻っている可能性がある"


# ── 5. gap-fill が解決済みサフィックスを使う（#555 PR2）─────────────────────
from datetime import date, datetime, timedelta, time as dtime      # noqa: E402
from unittest.mock import patch                                     # noqa: E402

from collector_prices import fill_recent_stock_price_gap_yahoo, JST  # noqa: E402


def _monday_anchor():
    """「直近の月曜 03:00 JST」相当の (now_jst, session)。実行日に依存させない。

    test_cf_and_yahoo.py と同じアンカー（実 today の2週間手前に置き floor_d に触れない）。
    """
    base = date.today() - timedelta(days=14)
    monday = base + timedelta(days=(7 - base.weekday()) % 7)
    return datetime.combine(monday, dtime(3, 0), JST), monday - timedelta(days=3)


class TestGapFillUsesResolvedSuffix:
    """毎晩の gap-fill が `companies.yahoo_suffix` を引くこと。

    捕捉するのは「叩いたティッカー列」と「渡された kwargs」の2つ。
    ここが壊れると **38社が黙って priceless へ戻る**（例外は出ない）。

    どのテストも「基準セッション当日の価格を持つアンカー社」を1社置く。
    株価テーブルが完全に空だと `fill_recent_stock_price_gap_yahoo` は
    `reason="empty"` で早期 return する（異常時に全社取得へ暴走させないためのガード）ので、
    それを踏まずに本経路へ入るために要る。アンカーは `last_d >= session` で
    スキップされるので、観測する `seen` には現れない。
    """

    def _seed_anchor(self, db, make_company, make_price, session):
        db.add(make_company(edinet_code="E09999", sec_code="9999", is_active=True))
        db.add(make_price(edinet_code="E09999", trade_date=session.isoformat()))

    def _run(self, db, now_jst):
        seen, kwargs_seen = [], []

        async def fake(http, ticker, d_from, d_to, **kw):
            seen.append(ticker)
            kwargs_seen.append(kw)
            return []

        with patch("collector_prices.fetch_yahoo_history", new=fake):
            res = asyncio.run(fill_recent_stock_price_gap_yahoo(
                db, gap_days=0, now_jst=now_jst))
        return res, seen, kwargs_seen

    def test_anchor_alone_is_not_fetched(self, db, make_company, make_price):
        """前提の確認: アンカーは最新セッションに追いついているので叩かれない。"""
        now_jst, session = _monday_anchor()
        self._seed_anchor(db, make_company, make_price, session)
        db.commit()

        res, seen, _kw = self._run(db, now_jst)
        assert seen == []
        assert res.get("reason") != "empty", "早期 return を踏んでいる（アンカーが効いていない）"

    def test_resolved_company_uses_its_suffix(self, db, make_company, make_price):
        now_jst, session = _monday_anchor()
        self._seed_anchor(db, make_company, make_price, session)
        db.add(make_company(edinet_code="E00001", sec_code="1001",
                            is_active=False, yahoo_suffix=".S"))
        db.commit()

        _res, seen, kw = self._run(db, now_jst)
        assert seen == ["1001.S"]
        assert kw[0] == {"expect_exchanges": frozenset({"SAP"}), "expect_currency": "JPY"}

    def test_unresolved_company_uses_tokyo_without_kwargs(self, db, make_company,
                                                          make_price):
        """未解決は `.T` かつ**キーワード無し**＝従来経路そのまま。"""
        now_jst, session = _monday_anchor()
        self._seed_anchor(db, make_company, make_price, session)
        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=True))
        db.add(make_price(edinet_code="E00001",
                          trade_date=(session - timedelta(days=3)).isoformat()))
        db.commit()

        _res, seen, kw = self._run(db, now_jst)
        assert seen == ["1001.T"]
        assert kw[0] == {}

    def test_resolved_company_bypasses_the_backoff(self, db, make_company, make_price,
                                                   monkeypatch):
        """**解決済みはバックオフの非該当日でも叩く**（#555 の要件）。"""
        now_jst, session = _monday_anchor()
        monkeypatch.setattr("collector_prices.should_retry_priceless_delisted",
                            lambda ec, today: False)
        self._seed_anchor(db, make_company, make_price, session)
        db.add(make_company(edinet_code="E00002", sec_code="1002",
                            is_active=False, yahoo_suffix=".F"))
        db.commit()

        _res, seen, _kw = self._run(db, now_jst)
        assert seen == ["1002.F"], f"解決済みなのに見送られた: {seen}"

    def test_unresolved_priceless_still_backs_off(self, db, make_company, make_price,
                                                  monkeypatch):
        """未解決は従来どおり7日に1回＝#475 の資産を壊していない。"""
        now_jst, session = _monday_anchor()
        monkeypatch.setattr("collector_prices.should_retry_priceless_delisted",
                            lambda ec, today: False)
        self._seed_anchor(db, make_company, make_price, session)
        db.add(make_company(edinet_code="E00002", sec_code="1002", is_active=False))
        db.commit()

        _res, seen, _kw = self._run(db, now_jst)
        assert seen == [], f"見送るべき社を叩いている: {seen}"

    def test_priceless_counters_are_reported(self, db, make_company, make_price,
                                             monkeypatch):
        """母数が戻り値に載ること（454→416 を run ログから追えるようにする）。"""
        now_jst, session = _monday_anchor()
        monkeypatch.setattr("collector_prices.should_retry_priceless_delisted",
                            lambda ec, today: True)
        self._seed_anchor(db, make_company, make_price, session)
        db.add(make_company(edinet_code="E00001", sec_code="1001",
                            is_active=False, yahoo_suffix=".S"))
        db.add(make_company(edinet_code="E00002", sec_code="1002", is_active=False))
        db.commit()

        res, seen, _kw = self._run(db, now_jst)
        assert res["priceless"] == 2, "アンカーは価格を持つので母数へ入れない"
        assert res["priceless_resolved"] == 1
        assert sorted(seen) == ["1001.S", "1002.T"]

    def test_exchange_rejected_is_counted(self, db, make_company, make_price):
        """解決済みなのに空が返ったら数える＝静かな脱落を黙らせない。"""
        now_jst, session = _monday_anchor()
        self._seed_anchor(db, make_company, make_price, session)
        db.add(make_company(edinet_code="E00001", sec_code="1001",
                            is_active=False, yahoo_suffix=".S"))
        db.commit()

        res, _seen, _kw = self._run(db, now_jst)   # fake は常に [] を返す
        assert res["exchange_rejected"] == 1

    def test_unresolved_empty_is_not_counted_as_rejected(self, db, make_company,
                                                         make_price, monkeypatch):
        """未解決の空振りは `exchange_rejected` に混ぜない（意味が違う）。"""
        now_jst, session = _monday_anchor()
        monkeypatch.setattr("collector_prices.should_retry_priceless_delisted",
                            lambda ec, today: True)
        self._seed_anchor(db, make_company, make_price, session)
        db.add(make_company(edinet_code="E00002", sec_code="1002", is_active=False))
        db.commit()

        res, _seen, _kw = self._run(db, now_jst)
        assert res["exchange_rejected"] == 0


class TestWeeklyBackfillOnlyFilter:
    """`backfill_weekly_history_yahoo(only=...)` が対象を絞ること（#555）。

    これが効かないと、解決した37社のために**全社 5年ぶん**を取りに行って数十時間かかる。
    逆に絞りすぎると「解決したのに px_* が出ない」——daily 保持窓は183日＝約26週しかなく、
    `z_momentum` の52週にも `build_snapshots` の52週先ラベルにも届かないため。
    """

    def _run(self, db, only=None):
        from collector_prices import backfill_weekly_history_yahoo

        seen = []

        async def fake(session, ticker, d_from, d_to, **kw):
            seen.append(ticker)
            return []

        with patch("collector_prices.fetch_yahoo_history", new=fake), \
                patch("collector_prices.record_prices_batch", return_value=0), \
                patch("collector_prices.YAHOO_STOCK_RATE_SLEEP", 0):
            res = asyncio.run(backfill_weekly_history_yahoo(db, years_back=5, only=only))
        return res, seen

    def _seed(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001", yahoo_suffix=".S"))
        db.add(make_company(edinet_code="E00002", sec_code="1002", yahoo_suffix=".F"))
        db.add(make_company(edinet_code="E00003", sec_code="1003"))
        db.commit()

    def test_without_only_all_companies_are_targeted(self, db, make_company):
        self._seed(db, make_company)
        _res, seen = self._run(db)
        assert sorted(seen) == ["1001.S", "1002.F", "1003.T"]

    def test_only_restricts_to_the_listed_companies(self, db, make_company):
        self._seed(db, make_company)
        _res, seen = self._run(db, only=["E00001", "E00002"])
        assert sorted(seen) == ["1001.S", "1002.F"], "絞り込みが効いていない"

    def test_only_keeps_the_resolved_suffix(self, db, make_company):
        """絞り込んでもサフィックスは保たれる（`.T` へ戻らない）。"""
        self._seed(db, make_company)
        _res, seen = self._run(db, only=["E00002"])
        assert seen == ["1002.F"]

    def test_empty_only_targets_nothing(self, db, make_company):
        """空リストは「全社」ではなく「対象なし」。`if only:` の分岐を固定する。"""
        self._seed(db, make_company)
        res, seen = self._run(db, only=[])
        assert seen == []
        assert sorted(seen) == []
        assert res.get("companies") in (0, None)
