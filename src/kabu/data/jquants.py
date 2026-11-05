"""J-Quants API（日本取引所グループの公式 API、V2）を使うための部品。

V2 は、J-Quants のダッシュボードで発行する「APIキー」を x-api-key ヘッダーに付けて使う。
（2025年12月22日以降の登録者は V2 しか使えない。V1 のメールアドレス・パスワードでのログインは使えない）

APIキーの置き場所（上から順に探す）:
  1. 環境変数 JQUANTS_API_KEY
  2. config/local/jquants_api_key.txt（.gitignore で GitHub に上げないようにしてある）
  3. どちらもなければ画面で聞いて、2 に保存する（入力は画面に表示しない）

無料プランは1分あたり5回までしか問い合わせできないので、問い合わせの間に pause_seconds（既定13秒）あける。
"""

import getpass
import os
import time

import requests

from kabu.config import resolve_path

BASE_URL = "https://api.jquants.com/v2"
KEY_FILE = "config/local/jquants_api_key.txt"
DEFAULT_PAUSE_SECONDS = 13.0   # 無料プランは毎分5回まで → 12秒より長くあける


class JQuantsError(Exception):
    pass


def _describe(response: requests.Response) -> str:
    hint = ""
    if response.status_code in (401, 403):
        hint = "（APIキーが違うか、今のプランでは使えないデータの可能性があります）"
    elif response.status_code == 429:
        hint = "（問い合わせが多すぎます。少し待ってからやり直してください）"
    return f"HTTP {response.status_code}{hint}: {response.text[:300]}"


def get_api_key(ask: bool = True) -> str:
    """APIキーを返す。なければ画面で聞いて保存する（ask=False なら聞かずにエラー）。"""
    key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if key:
        return key
    path = resolve_path(KEY_FILE)
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    if not ask:
        raise JQuantsError(f"APIキーがありません。{KEY_FILE} に保存するか、環境変数 JQUANTS_API_KEY を設定してください")
    print("J-Quants のダッシュボードで発行した APIキー を貼り付けて Enter を押してください")
    key = getpass.getpass("  APIキー（貼り付けても表示されません）: ").strip()
    if not key:
        raise JQuantsError("APIキーが入力されませんでした")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    print(f"  APIキーを {path} に保存しました（GitHub には上がりません）")
    return key


def forget_api_key() -> None:
    """保存したAPIキーを消す（キーが違っていたときに、次回また聞き直すため）。"""
    path = resolve_path(KEY_FILE)
    if path.exists():
        path.unlink()


def _list_in(data: dict, key: str | None) -> list:
    """応答の中のデータの一覧を取り出す。キー名が分からないときは、最初に見つかった一覧を使う。"""
    if key and isinstance(data.get(key), list):
        return data[key]
    for value in data.values():
        if isinstance(value, list):
            return value
    return []


def get_all(path: str, params: dict, api_key: str, key: str | None = None,
            pause: float = DEFAULT_PAUSE_SECONDS, sleep=time.sleep) -> tuple[list[dict], dict]:
    """データを取る。件数が多いと pagination_key で分割されて返ってくるので、全部つなげる。
    戻り値: (データの一覧, 最初の応答そのもの（一覧以外のキーを確かめるため）)"""
    rows, params, first, calls = [], dict(params), None, 0
    while True:
        if calls > 0:
            sleep(pause)
        calls += 1
        response = requests.get(f"{BASE_URL}{path}", params=params, headers={"x-api-key": api_key}, timeout=60)
        if response.status_code != 200:
            raise JQuantsError(f"{path} を取得できませんでした。{_describe(response)}")
        data = response.json()
        first = first if first is not None else data
        rows += _list_in(data, key)
        if not data.get("pagination_key"):
            return rows, first
        params["pagination_key"] = data["pagination_key"]
