"""J-Quants API（日本取引所グループの公式 API）を使うための部品。

ログインの流れ:
  1. メールアドレスとパスワードで「リフレッシュトークン」をもらう（1週間有効）
  2. リフレッシュトークンで「ID トークン」をもらう（24時間有効）。データを取るときはこれを使う

パスワードは保存しない。リフレッシュトークンだけを config/local/jquants_token.json に保存する
（config/local/ は .gitignore で GitHub に上げないようにしてある）。
トークンが期限切れなら、画面でメールアドレスとパスワードを聞き直す。
"""

import getpass
import json
from datetime import datetime, timedelta

import requests

from kabu.config import resolve_path

BASE_URL = "https://api.jquants.com/v1"
TOKEN_FILE = "config/local/jquants_token.json"
REFRESH_TOKEN_DAYS = 6   # 有効期限は1週間だが、余裕をもって6日で取り直す


class JQuantsError(Exception):
    pass


def _describe(response: requests.Response) -> str:
    """失敗したときの説明。トークンが入っている可能性があるので、成功した応答の中身は出さない。"""
    return f"HTTP {response.status_code}: {response.text[:300]}"


def _load_refresh_token() -> str | None:
    path = resolve_path(TOKEN_FILE)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    saved_at = datetime.fromisoformat(data["saved_at"])
    if datetime.now() - saved_at > timedelta(days=REFRESH_TOKEN_DAYS):
        return None
    return data["refresh_token"]


def _save_refresh_token(token: str) -> None:
    path = resolve_path(TOKEN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"refresh_token": token, "saved_at": datetime.now().isoformat(timespec="seconds")}),
                    encoding="utf-8")


def login_interactively() -> str:
    """画面でメールアドレスとパスワードを聞いて、リフレッシュトークンをもらう。"""
    print("J-Quants にログインします（パスワードは保存しません）")
    mail = input("  メールアドレス: ").strip()
    password = getpass.getpass("  パスワード（入力しても表示されません）: ")
    response = requests.post(f"{BASE_URL}/token/auth_user",
                             json={"mailaddress": mail, "password": password}, timeout=30)
    if response.status_code != 200:
        raise JQuantsError(f"ログインできませんでした。{_describe(response)}")
    token = response.json().get("refreshToken")
    if not token:
        raise JQuantsError("ログインの応答に refreshToken がありませんでした（API の仕様が変わった可能性）")
    _save_refresh_token(token)
    return token


def get_id_token() -> str:
    """データを取るための ID トークンをもらう。必要なら画面でログインし直す。"""
    refresh = _load_refresh_token() or login_interactively()
    response = requests.post(f"{BASE_URL}/token/auth_refresh", params={"refreshtoken": refresh}, timeout=30)
    if response.status_code != 200:
        # 保存してあったトークンが使えなくなっていた → ログインし直して、もう一度だけ試す
        refresh = login_interactively()
        response = requests.post(f"{BASE_URL}/token/auth_refresh", params={"refreshtoken": refresh}, timeout=30)
        if response.status_code != 200:
            raise JQuantsError(f"ID トークンをもらえませんでした。{_describe(response)}")
    token = response.json().get("idToken")
    if not token:
        raise JQuantsError("応答に idToken がありませんでした（API の仕様が変わった可能性）")
    return token


def get_all(path: str, params: dict, id_token: str, key: str) -> list[dict]:
    """データを取る。件数が多いと pagination_key で分割されて返ってくるので、全部つなげる。"""
    rows, params = [], dict(params)
    while True:
        response = requests.get(f"{BASE_URL}{path}", params=params,
                                headers={"Authorization": f"Bearer {id_token}"}, timeout=60)
        if response.status_code != 200:
            raise JQuantsError(f"{path} を取得できませんでした。{_describe(response)}")
        data = response.json()
        rows += data.get(key, [])
        if not data.get("pagination_key"):
            return rows
        params["pagination_key"] = data["pagination_key"]
