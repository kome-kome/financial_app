"""バックアップ `scripts/backup_push.py` の不変条件（Issue #503 Phase 3）。

**復元したことのないバックアップはバックアップではない。** ここで縛るのは、復元に必要な
前提が黙って崩れないようにするための4点:

1. **バックアップ元は正本（ローカル）**。リモートを引いたものは「Supabase の自己複製」で、
   正本が失われたときに役に立たない（`mirror_common.guard_dest_local` と対になるガード）
2. **保持ポリシーが決定的**。消す順が実行ごとに変わると、どの世代が残るか予測できない
3. **Free プランの上限（50MB/ファイル）を超えたら止める**。413 で落ちる前に気づきたい
4. **圧縮する**。ミラーの pull は `--compress=0` が正しく、バックアップは逆。用途で正解が
   反転するので、取り違えをここで落とす
"""
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import backup_push as bp
from scripts import mirror_common as mc


class TestRetention:
    """`generations_to_delete` は純関数。世代名を渡すだけで全分岐を検証できる。"""

    def test_keeps_recent_generations(self):
        stamps = [f"2026081{d}T000000Z" for d in range(1, 9)]
        drop = bp.generations_to_delete(stamps, keep_recent=3, keep_monthly=0)
        assert set(stamps) - set(drop) == set(stamps[-3:])

    def test_keeps_first_generation_of_each_month(self):
        stamps = [
            "20260601T000000Z", "20260615T000000Z",     # 6月（最初は 06-01）
            "20260702T000000Z", "20260728T000000Z",     # 7月（最初は 07-02）
            "20260805T000000Z",                          # 8月
        ]
        drop = bp.generations_to_delete(stamps, keep_recent=1, keep_monthly=3)
        kept = set(stamps) - set(drop)
        assert "20260601T000000Z" in kept and "20260702T000000Z" in kept
        assert "20260615T000000Z" in drop and "20260728T000000Z" in drop

    def test_monthly_window_is_limited(self):
        stamps = [f"2026{m:02d}01T000000Z" for m in range(1, 9)]
        drop = bp.generations_to_delete(stamps, keep_recent=0, keep_monthly=2)
        assert set(stamps) - set(drop) == {"20260801T000000Z", "20260701T000000Z"}

    def test_empty_input_is_safe(self):
        assert bp.generations_to_delete([]) == []

    def test_is_deterministic(self):
        """同じ入力なら同じ結果。順序に依存すると「消える世代」が実行ごとに変わる。"""
        stamps = ["20260701T000000Z", "20260601T000000Z", "20260805T000000Z",
                  "20260615T000000Z", "20260728T000000Z"]
        first = bp.generations_to_delete(stamps, 2, 2)
        for _ in range(5):
            assert bp.generations_to_delete(list(reversed(stamps)), 2, 2) == first

    def test_nothing_is_dropped_when_policy_covers_all(self):
        stamps = ["20260801T000000Z", "20260802T000000Z"]
        assert bp.generations_to_delete(stamps, keep_recent=5, keep_monthly=5) == []


class TestSourceGuard:
    def test_remote_source_is_refused(self, monkeypatch):
        """リモートを引いたものはバックアップではない（正本の複製にならない）。"""
        monkeypatch.setattr(bp.database, "_is_local", False)
        monkeypatch.setattr(bp.database, "DATABASE_URL",
                            "postgresql://postgres.abc:pw@aws-1.pooler.supabase.com:5432/postgres")
        with pytest.raises(SystemExit, match="正本"):
            bp.guard_source_is_primary()

    def test_local_source_passes(self, monkeypatch):
        monkeypatch.setattr(bp.database, "_is_local", True)
        bp.guard_source_is_primary()

    def test_error_message_masks_the_password(self, monkeypatch):
        monkeypatch.setattr(bp.database, "_is_local", False)
        monkeypatch.setattr(bp.database, "DATABASE_URL",
                            "postgresql://postgres.abc:sup3rsecret@aws-1.pooler.supabase.com:5432/postgres")
        with pytest.raises(SystemExit) as e:
            bp.guard_source_is_primary()
        assert "sup3rsecret" not in str(e.value) and "***" in str(e.value)


class TestDumpFlags:
    def test_backup_compresses_unlike_the_mirror_pull(self):
        """**用途で正解が反転する**: pull は compress=0、backup は最大圧縮。

        pull で圧縮すると出来上がりサイズが実 Egress を過小申告する（ワイヤ上は非圧縮）。
        backup で圧縮しないと Storage の 50MB/1GB 枠にすぐ当たる。取り違えると
        「動くが目的を果たさない」ので、両方の既定をここで固定する。
        """
        conn = mc.PgConn.from_url("postgresql://u:p@localhost:5432/db")
        assert "--compress=0" in mc.pg_dump_argv(conn, ["companies"], "x.dump")
        assert f"--compress={bp.COMPRESS}" in mc.pg_dump_argv(
            conn, ["companies"], "x.dump", compress=bp.COMPRESS)
        assert bp.COMPRESS > 0

    def test_invalid_compress_is_rejected(self):
        conn = mc.PgConn.from_url("postgresql://u:p@localhost:5432/db")
        with pytest.raises(ValueError):
            mc.pg_dump_argv(conn, ["companies"], "x.dump", compress=10)

    def test_strict_names_survives_in_backup_path(self):
        """typo が exit 0 で通り「その表だけ入っていないダンプ」になるのを防ぐ（実測済み）。"""
        conn = mc.PgConn.from_url("postgresql://u:p@localhost:5432/db")
        assert "--strict-names" in mc.pg_dump_argv(conn, ["companies"], "x.dump", compress=9)


class TestLimits:
    def test_object_limit_matches_the_free_plan(self):
        assert bp.MAX_OBJECT_MB == 50.0
        assert bp.MAX_TOTAL_MB == 1024.0

    def test_retention_fits_in_the_free_quota(self):
        """保持世代 × 実測サイズが 1GB 枠に収まること（2026-08-20 実測 37.5MB/世代）。"""
        measured_mb_per_generation = 37.5
        worst_case = (bp.KEEP_RECENT + bp.KEEP_MONTHLY) * measured_mb_per_generation
        assert worst_case < bp.MAX_TOTAL_MB, (
            f"保持ポリシーで最大 {worst_case:.0f}MB になり 1GB 枠を超える"
        )

    def test_backup_covers_the_whole_mirror_scope(self):
        """正本にしか無いデータがバックアップから落ちないこと（#503 で daily を範囲へ入れた）。"""
        assert "stock_price_daily" in mc.mirror_tables()
        assert "stock_price_weekly" in mc.mirror_tables()
        assert "financial_records" in mc.mirror_tables()


class TestDumpOrder:
    """表ごとに別プロセスで取る＝**スナップショット時点が揃わない**ことへの対処。"""

    def test_dump_order_is_the_reverse_of_restore_order(self):
        tables = mc.mirror_tables()
        assert bp.dump_order(tables) == tuple(reversed(tables))

    def test_fk_parents_are_dumped_after_their_children(self):
        """親を先に取ると、その後に増えた会社を参照する子が「FK 先の無い行」として入る。

        子を先に取れば、後から取る親は子より新しい＝子が参照する親は必ず含まれる
        （余分な親が入るのは無害）。restore は逆に依存順で流す。
        """
        from database import Base

        order = {t: i for i, t in enumerate(bp.dump_order(mc.mirror_tables()))}
        for table in Base.metadata.tables.values():
            for col in table.columns:
                for fk in col.foreign_keys:
                    parent = fk.column.table.name
                    if table.name in order and parent in order:
                        assert order[parent] > order[table.name], (
                            f"親 {parent} が子 {table.name} より先にダンプされている"
                        )


class TestManifest:
    def test_manifest_records_rows_and_sizes(self, tmp_path):
        gen = bp.Generation(stamp="20260820T000000Z", dumps=[
            bp.TableDump("companies", tmp_path / "c.dump", 1234, 4438),
        ])
        m = gen.manifest()
        assert m["stamp"] == "20260820T000000Z"
        assert m["tables"][0] == {"table": "companies", "bytes": 1234, "rows": 4438}
        assert m["total_bytes"] == 1234
        assert m["compress"] == bp.COMPRESS
        json.dumps(m)      # 直列化できること

    def test_manifest_masks_the_source_url(self):
        gen = bp.Generation(stamp="x")
        assert "***" in gen.manifest()["source"] or "localhost" in gen.manifest()["source"]

    def test_stamp_round_trips_to_a_date(self):
        s = bp.new_stamp(datetime(2026, 8, 20, 12, 34, 56, tzinfo=timezone.utc))
        assert s == "20260820T123456Z"
        assert bp.stamp_date(s).isoformat() == "2026-08-20"


class TestStorageCredentials:
    def test_missing_credentials_name_what_is_missing(self):
        with pytest.raises(SystemExit) as e:
            bp.Storage.from_env({})
        msg = str(e.value)
        assert "SUPABASE_URL" in msg and "SUPABASE_SERVICE_ROLE_KEY" in msg

    def test_local_dest_needs_no_credentials(self, monkeypatch, capsys):
        """認証情報が無くても計画までは検証できる（ADR-0035 と同じ「Supabase 抜きの実証」）。"""
        monkeypatch.setattr(bp.database, "_is_local", True)
        assert bp.main([]) == 0
        assert "ドライラン" in capsys.readouterr().out


class TestStorageErrors:
    """**初回は必ずバケット未作成を踏む。** 生の 400 を出さず、手が動く形で伝える。"""

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    def _store(self):
        store = object.__new__(bp.Storage)
        store.bucket = bp.BUCKET
        store.base = "https://x.supabase.co/storage/v1"
        return store

    def test_missing_bucket_says_how_to_create_it(self):
        with pytest.raises(SystemExit) as e:
            self._store()._fail("一覧取得に失敗", self._Resp(404, '{"error":"Bucket not found"}'))
        msg = str(e.value)
        assert bp.BUCKET in msg and "private" in msg.lower()

    def test_anon_key_is_called_out(self):
        """private バケットへの書き込みは service_role が要る。403 の原因の筆頭。"""
        with pytest.raises(SystemExit, match="service_role"):
            self._store()._fail("アップロード失敗", self._Resp(403, "unauthorized"))

    def test_other_errors_keep_the_raw_detail(self):
        with pytest.raises(SystemExit, match="507"):
            self._store()._fail("アップロード失敗", self._Resp(507, "quota exceeded"))
