"""Ticket 31ed52a7: reach Jarvis over a public tunnel, safely: Telegram-code login, no static token from outside."""
import re

import pytest
from starlette.testclient import TestClient

from core import appserver

PUBLIC = {"Cf-Connecting-IP": "203.0.113.9"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setenv("APP_TOKEN", "home-token")
    appserver._code.clear()
    appserver._code_times.clear()
    sent = []

    async def send(text):
        sent.append(text)

    monkeypatch.setattr(appserver, "send_code", send)
    return TestClient(appserver.app), sent


def _code(sent):
    return re.search(r"\b(\d{6})\b", sent[-1]).group(1)


def test_static_token_only_at_home(app):
    client, _ = app
    home = {"Authorization": "Bearer home-token"}
    assert client.get("/api/projects", headers=home).status_code == 200
    r = client.get("/api/projects", headers=home | PUBLIC)
    assert r.status_code == 401 and r.json()["login"] is True
    assert client.get("/api/projects", headers=PUBLIC).status_code == 401
    assert client.get("/api/health", headers=PUBLIC).json()["public"] is True


def test_login_with_telegram_code_then_works_everywhere(app):
    client, sent = app
    assert client.post("/api/login/start", json={"device": "Redmi"}, headers=PUBLIC).json()["ok"]
    assert "over the internet" in sent[-1] and "Redmi" in sent[-1]
    token = client.post("/api/login", json={"code": _code(sent), "device": "Redmi"}, headers=PUBLIC).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/projects", headers=auth | PUBLIC).status_code == 200
    assert client.get("/api/projects", headers=auth).status_code == 200          # at home too
    stored = (appserver.SESSIONS).read_text()
    assert token not in stored                                                    # only its hash is kept
    # the code is single-use
    assert client.post("/api/login", json={"code": _code(sent)}, headers=PUBLIC).status_code == 400
    # logout ends it
    assert client.post("/api/logout", headers=auth | PUBLIC).json()["ok"]
    assert client.get("/api/projects", headers=auth | PUBLIC).status_code == 401


def test_wrong_codes_kill_the_code(app):
    client, sent = app
    client.post("/api/login/start", headers=PUBLIC)
    good = _code(sent)
    bad = "000000" if good != "000000" else "111111"
    for i in range(appserver.CODE_TRIES - 1):
        assert client.post("/api/login", json={"code": bad}, headers=PUBLIC).json()["error"] == "Wrong code."
    assert "Too many" in client.post("/api/login", json={"code": bad}, headers=PUBLIC).json()["error"]
    assert client.post("/api/login", json={"code": good}, headers=PUBLIC).status_code == 400


def test_code_expires_and_rate_limit(app, monkeypatch):
    client, sent = app
    client.post("/api/login/start", headers=PUBLIC)
    assert client.post("/api/login/start", headers=PUBLIC).status_code == 429     # one a minute
    appserver._code["until"] = 0                                                  # expired
    assert client.post("/api/login", json={"code": _code(sent)}, headers=PUBLIC).status_code == 400
    appserver._code_times[:] = [1e12] * 0
    import time
    appserver._code_times[:] = [time.time() - 3000 + i for i in range(appserver.CODES_PER_HOUR)]
    assert client.post("/api/login/start", headers=PUBLIC).status_code == 429     # five an hour


def test_call_socket_refuses_static_token_from_outside(app):
    client, _ = app
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/call?token=home-token", headers=PUBLIC) as ws:
            ws.receive_text()
    assert e.value.code == 4401
