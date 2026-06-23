"""api.py の環境変数ロード順リグレッション。

api.py は APP_PASSWORD / APP_SECRET_KEY / APP_RECOVERY_KEY 等の認証設定を
モジュールトップで os.getenv(...) する。これらは database の import より前に
実行されるため、api.py 自身が load_dotenv() を先に呼ばないと、.env のみに
シークレットを置いた環境（ローカル等）で:
  - APP_PASSWORD="" → 認証が無効化
  - APP_SECRET_KEY が dev 既定値（既知のハードコード鍵）
という事故になる。本テストはその順序不変条件を固定する。
"""
import os
from pathlib import Path

API_SRC = (Path(__file__).resolve().parent.parent / "api.py").read_text(encoding="utf-8")


def test_api_calls_load_dotenv():
    assert "load_dotenv()" in API_SRC, "api.py は load_dotenv() を呼ぶ必要があります"


def test_load_dotenv_precedes_auth_env_reads():
    load_idx = API_SRC.find("load_dotenv()")
    pw_idx   = API_SRC.find('os.getenv("APP_PASSWORD"')
    sk_idx   = API_SRC.find('os.getenv("APP_SECRET_KEY"')
    assert pw_idx != -1 and sk_idx != -1, "認証設定の os.getenv 読み込みが見つかりません"
    assert load_idx != -1 and load_idx < pw_idx, \
        "load_dotenv() は APP_PASSWORD を env から読む前に実行する必要があります"
    assert load_idx < sk_idx, \
        "load_dotenv() は APP_SECRET_KEY を env から読む前に実行する必要があります"
