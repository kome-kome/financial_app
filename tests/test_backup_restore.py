"""Storage から落として戻す経路の不変条件（Issue #503 検証8）。

**取れていることと戻せることは別物で、後者だけが本体。** 2026-09-04 時点で「Storage から
落として復元する」は一度も通っておらず、原因は手順を踏んでいなかったからではなく
**経路がコードに無かったから**だった（`Storage` は upload / list / remove の3つしか持って
いなかった）。ここで縛るのは、その経路が持つべき3つの性質:

1. **落としたものをローカル世代と混ぜない**。`backup_push` の保持ポリシーは `.backups/`
   直下の世代を数えるので、複製が枠を食うと**本物の世代が消える**
2. **転送の欠けを復元の前に落とす**。途中で切れたダンプを `pg_restore` へ渡すと
   「復元に失敗した」としか見えず、転送の問題かダンプの問題かの切り分けが後ろへ倒れる
3. **ドライランは 37MB を落とさない**。確認のたびに全量を引くと、確認自体を避けるようになる
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import backup_push as bp
from scripts import backup_restore as br


class _FakeStorage:
    """`Storage.download` だけを差し替えた偽物（HTTP へ出ない）。"""

    def __init__(self, objects: dict):
        self.objects = objects
        self.fetched: list[str] = []

    def download(self, remote_path: str) -> bytes:
        self.fetched.append(remote_path)
        if remote_path not in self.objects:
            raise SystemExit(f"中止: ダウンロード失敗 {remote_path}")
        return self.objects[remote_path]


def _generation(stamp: str, tables: dict) -> _FakeStorage:
    manifest = {
        "stamp": stamp,
        "created_at": "2026-08-21T01:38:18+00:00",
        "source": "postgresql://edinet:***@localhost:5432/financial_db",
        "compress": 9,
        "tables": [{"table": t, "bytes": len(b), "rows": 1} for t, b in tables.items()],
        "total_bytes": sum(len(b) for b in tables.values()),
    }
    objects = {f"{stamp}/{bp.MANIFEST_NAME}": json.dumps(manifest).encode("utf-8")}
    objects.update({f"{stamp}/{t}.dump": b for t, b in tables.items()})
    return _FakeStorage(objects)


class TestDownloadPlacement:
    """落とした世代はローカル世代の数えに入らないこと（保持ポリシーとの衝突）。"""

    def test_downloads_go_under_a_separate_directory(self):
        assert br.FROM_STORAGE_STORE.parent == bp.LOCAL_STORE
        assert br.FROM_STORAGE_STORE.name.startswith("_")

    def test_download_dir_is_not_counted_as_a_local_generation(self, tmp_path, monkeypatch):
        """`local_generations()` は直下に manifest.json を持つディレクトリだけを世代とみなす。

        `_from_storage/` は世代を1階層下に置くので、**構造的に**数えられない。
        「命名で避ける」ではなく「形で入れない」ことを縛る。
        """
        monkeypatch.setattr(bp, "LOCAL_STORE", tmp_path)
        real = tmp_path / "20260821T013818Z"
        real.mkdir()
        (real / bp.MANIFEST_NAME).write_text("{}", encoding="utf-8")
        downloaded = tmp_path / "_from_storage" / "20260820T111003Z"
        downloaded.mkdir(parents=True)
        (downloaded / bp.MANIFEST_NAME).write_text("{}", encoding="utf-8")

        assert bp.local_generations() == ["20260821T013818Z"]

    def test_downloaded_generation_never_reaches_the_retention_policy(self, tmp_path, monkeypatch):
        """保持ポリシーへ渡る世代名に `_from_storage` が混じらないこと。

        混じると `stamp_date()`（先頭8桁を日付として読む）が例外を投げるか、最悪の場合
        **本物の世代が「消す側」へ回る**。
        """
        monkeypatch.setattr(bp, "LOCAL_STORE", tmp_path)
        for name in ("20260801T000000Z", "20260815T000000Z", "20260829T000000Z"):
            d = tmp_path / name
            d.mkdir()
            (d / bp.MANIFEST_NAME).write_text("{}", encoding="utf-8")
        (tmp_path / "_from_storage").mkdir()

        stamps = bp.local_generations()
        assert "_from_storage" not in stamps
        bp.generations_to_delete(stamps)          # 例外を出さない


class TestPullIntegrity:
    """転送の欠けは復元より手前で落とす。"""

    def test_size_mismatch_stops_before_restore(self, tmp_path):
        store = _generation("20260821T013818Z", {"companies": b"x" * 100})
        # マニフェストは 100 バイトと言っているのに、実体を 40 バイトへ差し替える
        store.objects["20260821T013818Z/companies.dump"] = b"x" * 40

        with pytest.raises(SystemExit) as e:
            br.pull_generation(store, "20260821T013818Z", tmp_path, echo=lambda *_: None)
        msg = str(e.value)
        assert "companies" in msg and "100" in msg and "40" in msg

    def test_matching_sizes_pass_and_land_on_disk(self, tmp_path):
        tables = {"companies": b"c" * 32, "app_settings": b"a" * 16}
        store = _generation("20260821T013818Z", tables)

        out = br.pull_generation(store, "20260821T013818Z", tmp_path, echo=lambda *_: None)

        assert out == tmp_path / "20260821T013818Z"
        for t, b in tables.items():
            assert (out / f"{t}.dump").read_bytes() == b
        assert (out / bp.MANIFEST_NAME).is_file()

    def test_dry_run_fetches_only_the_manifest(self, tmp_path):
        """ドライランで 37MB を引かない。確認が高いと、確認しなくなる。"""
        store = _generation("20260821T013818Z", {"companies": b"c" * 32})

        br.pull_generation(store, "20260821T013818Z", tmp_path, manifest_only=True,
                           echo=lambda *_: None)

        assert store.fetched == [f"20260821T013818Z/{bp.MANIFEST_NAME}"]
        assert not (tmp_path / "20260821T013818Z" / "companies.dump").exists()


class TestRestoreReadsTheChosenStore:
    """`load_manifest` / `restore_order` が取得元ディレクトリを引数で受けること。

    ここが `LOCAL_STORE` 直書きへ戻ると、`--source storage` が**黙ってローカル世代を
    復元する**（引数は受け取るのに効かない、という最も気づきにくい壊れ方をする）。
    """

    def test_load_manifest_honours_the_store_argument(self, tmp_path):
        stamp = "20260821T013818Z"
        (tmp_path / stamp).mkdir()
        (tmp_path / stamp / bp.MANIFEST_NAME).write_text(
            json.dumps({"stamp": stamp, "tables": [], "total_bytes": 0}), encoding="utf-8")

        assert br.load_manifest(stamp, tmp_path)["stamp"] == stamp

    def test_latest_generation_honours_the_store_argument(self, tmp_path):
        for name in ("20260820T111003Z", "20260821T013818Z"):
            (tmp_path / name).mkdir()
            (tmp_path / name / bp.MANIFEST_NAME).write_text("{}", encoding="utf-8")

        assert br.latest_generation(tmp_path) == "20260821T013818Z"


class TestStorageDownload:
    """`download` は上げる側と同じ `_fail` を通ること（エラーの読み方を2つに分けない）。"""

    class _Resp:
        def __init__(self, status_code, text="", content=b""):
            self.status_code = status_code
            self.text = text
            self.content = content

    def _store(self, resp):
        store = object.__new__(bp.Storage)
        store.bucket = bp.BUCKET
        store.base = "https://x.supabase.co/storage/v1"
        store.client = type("C", (), {"get": lambda _self, url: resp})()
        return store

    def test_missing_bucket_is_named(self):
        with pytest.raises(SystemExit) as e:
            self._store(self._Resp(404, '{"error":"Bucket not found"}')).download("g/x.dump")
        assert bp.BUCKET in str(e.value)

    def test_anon_key_is_called_out(self):
        with pytest.raises(SystemExit, match="service_role"):
            self._store(self._Resp(403, "unauthorized")).download("g/x.dump")

    def test_success_returns_the_bytes(self):
        assert self._store(self._Resp(200, content=b"dump")).download("g/x.dump") == b"dump"
