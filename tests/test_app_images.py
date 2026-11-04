"""Ticket 546d965e: screenshots and photos show up in the Jarvis app chat, not only in Telegram."""
import asyncio
import subprocess
from types import SimpleNamespace as NS

from starlette.testclient import TestClient

from core import appserver, tools


def _png(path, w=2400, h=1600):
    icon = "/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/GenericApplicationIcon.icns"
    subprocess.run(["sips", "-s", "format", "png", "-z", str(h), str(w), icon, "--out", str(path)],
                   capture_output=True, check=True)
    return path


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "IMAGES", tmp_path / "images")
    monkeypatch.setenv("APP_TOKEN", "t0k")
    return TestClient(appserver.app), {"Authorization": "Bearer t0k"}


def test_images_are_shrunk_stored_and_served(tmp_path, monkeypatch):
    client, auth = _setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("Jarvis")
    big = _png(tmp_path / "shot.png")
    msg = appserver.add_images(chat["id"], [big, tmp_path / "missing.png"], "Mac")
    assert msg and len(msg["images"]) == 1 and msg["text"] == "Mac"
    name = msg["images"][0]
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(appserver.IMAGES / name)],
                         capture_output=True, text=True).stdout
    assert name.endswith(".jpg") and "pixelWidth: 1280" in out            # longest side shrunk for the phone
    shown = client.get(f"/api/chats/{chat['id']}/messages", headers=auth).json()
    assert shown[-1]["images"] == [name]
    r = client.get(f"/api/images/{name}", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert client.get(f"/api/images/{name}").status_code == 401             # needs the login
    assert client.get("/api/images/..%2F..%2F.env", headers=auth).status_code == 404
    assert client.get("/api/images/0123456789abcdef.jpg", headers=auth).status_code == 404


def test_push_with_photos_reaches_chat_and_feed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("Jarvis")
    shot = _png(tmp_path / "s.png", 400, 300)
    msg = appserver.push("Reels are scheduled.", chat["id"], [shot])
    assert len(msg["images"]) == 1
    assert appserver.load_chat(chat["id"])["messages"][-1]["images"]
    assert appserver.updates_since(0)[-1]["images"]
    assert appserver.push("", "", [shot])["text"] == ""                  # images alone are fine
    assert appserver.push("", "", []) is None and appserver.push("NO_REPLY", "", [shot]) is None


def test_mac_screenshots_go_to_the_app_chat_he_asked_in(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General")
    shot = _png(tmp_path / "s.png", 400, 300)
    telegram = []

    async def send_photos(p, label):
        telegram.append(len(p))

    monkeypatch.setattr(tools.hooks, "send_photos", send_photos, raising=False)
    app_ctx = NS(request_context=NS(request=NS(headers={"x-jarvis-origin": f"app:{chat['id']}"})))
    assert asyncio.run(tools.show_photos([shot], "Mac", app_ctx)) == "shown in this app chat"
    assert telegram == [] and appserver.load_chat(chat["id"])["messages"][-1]["images"]
    assert asyncio.run(tools.show_photos([shot], "Mac", None)) == "sent to Telegram" and telegram == [1]
    gone = NS(request_context=NS(request=NS(headers={"x-jarvis-origin": "app:deleted"})))
    assert asyncio.run(tools.show_photos([shot], "Mac", gone)) == "sent to Telegram"     # chat gone: Telegram
