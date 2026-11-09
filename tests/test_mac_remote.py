"""Ticket 436ccc83: control the Mac from the app and website. Whoever has this has the whole Mac."""
import hashlib
import json
import re

import pytest
from starlette.testclient import TestClient

from core import appserver, macremote

SESSION = {"Authorization": "Bearer my-session"}
BUILT_IN = {"Authorization": "Bearer home-token"}
PUBLIC = {"Cf-Connecting-IP": "203.0.113.9"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    appserver.SESSIONS.write_text(json.dumps({hashlib.sha256(b"my-session").hexdigest(): {"device": "Huawei", "used": 9e12}}))
    monkeypatch.setenv("APP_TOKEN", "home-token")
    monkeypatch.setattr(appserver, "_mac_used", 0.0)
    done, told = [], []

    async def do_input(body):
        macremote.input_command(body)                # the real checks; never the real mouse
        done.append(body)
        return ""

    async def screen(width=1280):
        return b"\xff\xd8\xff-frame", 1280, 800

    async def wake():
        done.append({"action": "wake"})

    async def to_telegram(text):
        told.append(text)

    monkeypatch.setattr(macremote, "do_input", do_input)
    monkeypatch.setattr(macremote, "screen", screen)
    monkeypatch.setattr(macremote, "wake", wake)
    monkeypatch.setattr(appserver, "_to_telegram", to_telegram)
    return TestClient(appserver.app), done, told


@pytest.mark.parametrize("headers,code", [({}, 401), (PUBLIC, 401), ({"Authorization": "Bearer wrong"}, 401),
                                          (BUILT_IN, 403), (BUILT_IN | PUBLIC, 401)])
def test_only_a_login_session_can_touch_the_mac(app, headers, code):
    client, done, told = app
    assert client.get("/api/mac/screen", headers=headers).status_code == code
    assert client.post("/api/mac/input", json={"action": "click", "x": 1, "y": 1}, headers=headers).status_code == code
    assert client.post("/api/mac/input", json={"action": "wake"}, headers=headers).status_code == code
    assert done == [] and told == []


def test_screen_with_a_session(app):
    client, _, _ = app
    r = client.get("/api/mac/screen?w=800", headers=SESSION | PUBLIC)
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8\xff")
    assert r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
    assert (r.headers["x-mac-width"], r.headers["x-mac-height"]) == ("1280", "800")
    assert client.get("/api/mac/screen?w=junk", headers=SESSION).status_code == 200


def test_input_with_a_session_and_bad_input_refused(app):
    client, done, _ = app
    assert client.post("/api/mac/input", json={"action": "click", "x": 10, "y": 20}, headers=SESSION).json() == {"ok": True}
    assert client.post("/api/mac/input", json={"action": "wake"}, headers=SESSION).json() == {"ok": True}
    assert len(done) == 2
    for bad in ({"action": "rm"}, {"action": "click", "x": "1; ls", "y": 2}, {"action": "key", "key": "$(reboot)"}):
        assert client.post("/api/mac/input", json=bad, headers=SESSION).status_code == 400
    assert len(done) == 2


def test_alert_when_control_starts_then_quiet(app, monkeypatch):
    client, _, told = app
    client.get("/api/mac/screen", headers=SESSION | PUBLIC)
    assert len(told) == 1 and "Huawei" in told[0] and "over the internet" in told[0] and "203.0.113.9" in told[0]
    assert "/logoutall" in told[0]
    client.get("/api/mac/screen", headers=SESSION | PUBLIC)
    client.post("/api/mac/input", json={"action": "move", "x": 1, "y": 1}, headers=SESSION | PUBLIC)
    assert len(told) == 1                            # one alert for one sitting
    monkeypatch.setattr(appserver, "_mac_used", 1.0)  # long ago
    client.get("/api/mac/screen", headers=SESSION)
    assert len(told) == 2 and "home network" in told[1]


def test_logoutall_cuts_off_control(app):
    client, _, _ = app
    assert client.get("/api/mac/screen", headers=SESSION).status_code == 200
    assert appserver.end_all_sessions() == 1
    assert client.get("/api/mac/screen", headers=SESSION).status_code == 401
    assert client.get("/api/mac/screen", headers=SESSION | PUBLIC).status_code == 401
    assert appserver.end_all_sessions() == 0


def test_input_command_arguments():
    c = macremote.input_command
    assert c({"action": "click", "x": 10, "y": 20.5}) == (["click", "10", "20.5"], "")
    assert c({"action": "click", "x": 1, "y": 2, "right": True, "double": True}) == (["click", "1", "2", "--right", "--double"], "")
    assert c({"action": "drag", "x1": 1, "y1": 2, "x2": 3, "y2": 4}) == (["drag", "1", "2", "3", "4"], "")
    assert c({"action": "scroll", "dy": -5}) == (["scroll", "-5"], "")
    assert c({"action": "scroll", "dy": 3, "x": 7, "y": 8}) == (["scroll", "3", "7", "8"], "")
    assert c({"action": "key", "key": "CMD+Shift+T"}) == (["key", "cmd+shift+t"], "")


def test_typed_text_goes_by_stdin_never_arguments():
    args, stdin = macremote.input_command({"action": "type", "text": "my password"})
    assert args == ["type", "-"] and stdin == "my password"


@pytest.mark.parametrize("body", [
    {"action": "click", "x": -1, "y": 2}, {"action": "click", "x": 1e9, "y": 2}, {"action": "click", "x": True, "y": 2},
    {"action": "click", "x": float("nan"), "y": 2}, {"action": "click", "x": 1}, {"action": "scroll", "dy": 1000},
    {"action": "type", "text": ""}, {"action": "type", "text": 5}, {"action": "type", "text": "x" * 5001},
    {"action": "key", "key": "cmd+tab; rm"}, {"action": "key", "key": "--right"}, {"action": "key"}, {},
])
def test_input_command_refuses(body):
    with pytest.raises(ValueError):
        macremote.input_command(body)
