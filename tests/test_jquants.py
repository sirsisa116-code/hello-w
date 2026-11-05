"""J-Quants の部品のテスト（ネットにはつながない。応答は作り物）。"""

import json
from datetime import datetime, timedelta

import pytest

import kabu.data.jquants as jq


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def token_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jq, "resolve_path", lambda p: tmp_path / p)
    monkeypatch.setattr("builtins.input", lambda _: "me@example.com")
    monkeypatch.setattr(jq.getpass, "getpass", lambda _: "secret")
    return tmp_path


def test_login_saves_only_refresh_token(token_dir, monkeypatch):
    posts = []

    def fake_post(url, json=None, params=None, timeout=None):
        posts.append(url)
        if url.endswith("auth_user"):
            return FakeResponse(200, {"refreshToken": "R1"})
        return FakeResponse(200, {"idToken": "I1"})

    monkeypatch.setattr(jq.requests, "post", fake_post)
    assert jq.get_id_token() == "I1"
    saved = (token_dir / jq.TOKEN_FILE).read_text(encoding="utf-8")
    assert "R1" in saved and "secret" not in saved and "me@example.com" not in saved

    # 2回目は保存したトークンを使い、ログインし直さない
    posts.clear()
    assert jq.get_id_token() == "I1"
    assert not any(u.endswith("auth_user") for u in posts)


def test_relogin_when_token_expired_or_rejected(token_dir, monkeypatch):
    path = token_dir / jq.TOKEN_FILE
    path.parent.mkdir(parents=True)
    old = (datetime.now() - timedelta(days=10)).isoformat()
    path.write_text(json.dumps({"refresh_token": "OLD", "saved_at": old}), encoding="utf-8")
    calls = []

    def fake_post(url, json=None, params=None, timeout=None):
        calls.append((url.rsplit("/", 1)[-1], (params or {}).get("refreshtoken")))
        if url.endswith("auth_user"):
            return FakeResponse(200, {"refreshToken": "NEW"})
        if params["refreshtoken"] == "NEW":
            return FakeResponse(200, {"idToken": "I2"})
        return FakeResponse(400, {"message": "invalid"})

    monkeypatch.setattr(jq.requests, "post", fake_post)
    assert jq.get_id_token() == "I2"
    assert calls[0][0] == "auth_user"   # 期限切れのトークンは使わず、最初からログインし直す


def test_login_failure_message(token_dir, monkeypatch):
    monkeypatch.setattr(jq.requests, "post", lambda *a, **k: FakeResponse(403, {"message": "bad password"}))
    with pytest.raises(jq.JQuantsError, match="ログインできませんでした"):
        jq.get_id_token()


def test_get_all_follows_pagination(monkeypatch):
    pages = {None: {"statements": [{"a": 1}], "pagination_key": "p2"}, "p2": {"statements": [{"a": 2}]}}
    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append(params.get("pagination_key"))
        assert headers["Authorization"] == "Bearer T"
        return FakeResponse(200, pages[params.get("pagination_key")])

    monkeypatch.setattr(jq.requests, "get", fake_get)
    assert jq.get_all("/fins/statements", {"code": "72030"}, "T", "statements") == [{"a": 1}, {"a": 2}]
    assert seen == [None, "p2"]
