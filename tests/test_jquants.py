"""J-Quants（V2）の部品のテスト（ネットにはつながない。応答は作り物）。"""

import json

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
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jq, "resolve_path", lambda p: tmp_path / p)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    return tmp_path


def test_api_key_asked_once_and_saved(key_dir, monkeypatch):
    monkeypatch.setattr(jq.getpass, "getpass", lambda _: "  KEY123  ")
    assert jq.get_api_key() == "KEY123"
    assert (key_dir / jq.KEY_FILE).read_text(encoding="utf-8") == "KEY123"
    # 2回目は聞かない
    monkeypatch.setattr(jq.getpass, "getpass", lambda _: pytest.fail("聞き直してはいけない"))
    assert jq.get_api_key() == "KEY123"
    jq.forget_api_key()
    assert not (key_dir / jq.KEY_FILE).exists()


def test_api_key_from_environment(key_dir, monkeypatch):
    monkeypatch.setenv("JQUANTS_API_KEY", "ENVKEY")
    assert jq.get_api_key(ask=False) == "ENVKEY"
    monkeypatch.delenv("JQUANTS_API_KEY")
    with pytest.raises(jq.JQuantsError, match="APIキーがありません"):
        jq.get_api_key(ask=False)


def test_get_all_follows_pagination_and_waits(monkeypatch):
    pages = {None: {"data": [{"a": 1}], "pagination_key": "p2"}, "p2": {"data": [{"a": 2}]}}
    seen, waits = [], []

    def fake_get(url, params=None, headers=None, timeout=None):
        assert url.endswith("/v2/fins/summary") and headers == {"x-api-key": "K"}
        seen.append(params.get("pagination_key"))
        return FakeResponse(200, pages[params.get("pagination_key")])

    monkeypatch.setattr(jq.requests, "get", fake_get)
    rows, first = jq.get_all("/fins/summary", {"code": "72030"}, "K", sleep=waits.append)
    assert rows == [{"a": 1}, {"a": 2}] and seen == [None, "p2"]
    assert waits == [jq.DEFAULT_PAUSE_SECONDS]       # 2回目の前に、回数制限のぶん待つ
    assert "pagination_key" in first


def test_get_all_error_hint(monkeypatch):
    monkeypatch.setattr(jq.requests, "get", lambda *a, **k: FakeResponse(403, {"message": "Forbidden"}))
    with pytest.raises(jq.JQuantsError, match="今のプランでは使えない"):
        jq.get_all("/fins/summary", {"code": "72030"}, "K")
