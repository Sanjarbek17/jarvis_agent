"""Ticket 50b087e6: the Jarvis app updates itself from the newest published APK."""
import hashlib
import json

import pytest
from starlette.testclient import TestClient

from core import appserver

AUTH = {"Authorization": "Bearer home-token"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "RELEASES", tmp_path / "rel")
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setenv("APP_TOKEN", "home-token")
    (tmp_path / "rel").mkdir()
    return TestClient(appserver.app), tmp_path / "rel"


def publish(rel, data=b"PK-apk-bytes", **over):
    m = {"version_code": 23, "version_name": "0.23", "file": "jarvis-app-0.23+23.apk",
         "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "notes": "faster"} | over
    (rel / m["file"]).write_bytes(data)
    (rel / "latest.json").write_text(json.dumps(m))
    return m


def test_nothing_published(app):
    client, _ = app
    assert client.get("/api/app/latest", headers=AUTH).status_code == 204
    assert client.get("/api/app/apk", headers=AUTH).status_code == 404


def test_latest_and_download(app):
    client, rel = app
    m = publish(rel)
    assert client.get("/api/app/latest").status_code == 401                  # the APK has APP_TOKEN inside
    got = client.get("/api/app/latest", headers=AUTH).json()
    assert got == {"version_code": 23, "version_name": "0.23", "sha256": m["sha256"], "size_bytes": 12, "notes": "faster"}
    r = client.get("/api/app/apk", headers=AUTH)
    assert r.content == b"PK-apk-bytes" and r.headers["content-type"] == "application/vnd.android.package-archive"
    assert client.get("/api/app/apk").status_code == 401


@pytest.mark.parametrize("over", [{"file": "../../.env"}, {"file": "evil.apk"}, {"size_bytes": 999}, {"sha256": "x"},
                                  {"version_code": "23"}, {"version_code": 0}])
def test_bad_manifest_offers_nothing(app, over):
    client, rel = app
    publish(rel, **over) if "file" not in over else (rel / "latest.json").write_text(json.dumps(
        {"version_code": 23, "version_name": "0.23", "file": over["file"], "sha256": "0" * 64, "size_bytes": 1}))
    assert client.get("/api/app/latest", headers=AUTH).status_code == 204
    assert client.get("/api/app/apk", headers=AUTH).status_code == 404


def test_broken_json(app):
    client, rel = app
    (rel / "latest.json").write_text("{nope")
    assert client.get("/api/app/latest", headers=AUTH).status_code == 204


def test_publish_script(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, "scripts")
    import publish_app
    monkeypatch.setattr(publish_app, "RELEASES", tmp_path / "rel")
    versions = iter([(5, "0.5", "com.sanjar.jarvis"), (5, "0.5", "com.sanjar.jarvis"), (6, "0.6", "com.sanjar.jarvis"),
                     (7, "0.7", "com.other.app")])
    monkeypatch.setattr(publish_app, "apk_version", lambda apk: next(versions))
    apk = tmp_path / "app-release.apk"
    apk.write_bytes(b"one")
    r = publish_app.publish(apk, "first")
    assert r["file"] == "jarvis-app-0.5+5.apk" and r["sha256"] == hashlib.sha256(b"one").hexdigest()
    with pytest.raises(SystemExit):                                         # not newer: refused
        publish_app.publish(apk)
    apk.write_bytes(b"two!")
    assert publish_app.publish(apk)["size_bytes"] == 4
    assert json.loads((tmp_path / "rel" / "latest.json").read_text())["version_code"] == 6
    with pytest.raises(SystemExit):                                         # another app's APK
        publish_app.publish(apk)
    monkeypatch.setattr(appserver, "RELEASES", tmp_path / "rel")
    assert appserver.latest_release()["version_name"] == "0.6"
