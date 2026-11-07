"""Ticket 5d5a2c64: Sanjar attaches photos in the app / website chat; Jarvis gets them to look at."""
import asyncio
import struct
import zlib

import pytest
from starlette.testclient import TestClient

from core import appserver

AUTH = {"Authorization": "Bearer home-token"}


def png(w=40, h=30) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    rows = b"".join(b"\x00" + b"\x80\x20\x20" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "IMAGES", tmp_path / "images")
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setenv("APP_TOKEN", "home-token")
    seen = []

    class FakeBrain:
        session_id = "s"
        lock = asyncio.Lock()

        async def reply(self, text):
            seen.append(text)
            return "a red square"

    monkeypatch.setattr(appserver, "_brain", lambda chat: FakeBrain())
    return TestClient(appserver.app), seen


def test_upload_makes_a_jpeg(app):
    client, _ = app
    assert client.post("/api/uploads", content=png()).status_code == 401
    name = client.post("/api/uploads", content=png(), headers=AUTH).json()["name"]
    assert name.endswith(".jpg")
    saved = (appserver.IMAGES / name).read_bytes()
    assert saved[:3] == b"\xff\xd8\xff"
    assert client.get(f"/api/images/{name}", headers=AUTH).status_code == 200
    assert not list(appserver.IMAGES.glob(".upload-*"))          # temp file gone


def test_upload_refuses_non_images(app):
    client, _ = app
    for body in (b"", b"#!/bin/sh\nrm -rf /\n", b"\x89PNG\r\n\x1a\n" + b"garbage" * 10):
        r = client.post("/api/uploads", content=body, headers=AUTH)
        assert r.status_code == 400, body[:10]
    assert not [p for p in appserver.IMAGES.glob("*") if p.is_file()] if appserver.IMAGES.exists() else True


def test_message_with_image_reaches_the_brain(app):
    client, seen = app
    chat = client.post("/api/chats", json={"project": "General"}, headers=AUTH).json()
    name = client.post("/api/uploads", content=png(), headers=AUTH).json()["name"]
    reply = client.post(f"/api/chats/{chat['id']}/messages", json={"text": "", "images": [name]}, headers=AUTH).json()
    assert reply["text"] == "a red square"
    assert str(appserver.IMAGES / name) in seen[0] and "Read" in seen[0]
    msgs = client.get(f"/api/chats/{chat['id']}/messages", headers=AUTH).json()
    assert msgs[0]["role"] == "me" and msgs[0]["images"] == [name]
    assert client.get("/api/chats?project=General", headers=AUTH).json()[0]["title"] == "Image"


def test_message_refuses_unknown_or_bad_image_names(app):
    client, seen = app
    chat = client.post("/api/chats", json={"project": "General"}, headers=AUTH).json()
    url = f"/api/chats/{chat['id']}/messages"
    for images in (["0123456789abcdef.jpg"], ["../../.env"], "x.jpg", ["a"] * 11):
        assert client.post(url, json={"text": "hi", "images": images}, headers=AUTH).status_code == 400
    assert client.post(url, json={"text": ""}, headers=AUTH).status_code == 400
    assert seen == []
