"""The website is served by the app server, so the app and the site share one backend."""
from starlette.testclient import TestClient

from core import appserver


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setenv("APP_TOKEN", "t0k")
    appserver._code.clear()
    appserver._code_times.clear()
    return TestClient(appserver.app)


def test_every_site_file_is_served_without_login_and_locked_down(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    types = {"": "text/html", "app.js": "javascript", "style.css": "text/css",
             "mic-worklet.js": "javascript", "icon.svg": "image/svg+xml"}
    assert set(types) == set(appserver.WEB_FILES)
    for path, kind in types.items():
        res = client.get("/" + path)
        assert res.status_code == 200 and kind in res.headers["content-type"], path
        assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
        assert res.headers["x-content-type-options"] == "nosniff"


def test_only_the_listed_files_are_served(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for path in (".env", "../.env", "core/appserver.py", "web/app.js", "index.html", "..%2f.env", "api/nope"):
        res = client.get("/" + path)
        assert res.status_code == 404, path
        assert "APP_TOKEN" not in res.text


def test_the_site_holds_no_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("APP_TOKEN", "s3cret-token")
    for path in appserver.WEB_FILES:
        assert "s3cret-token" not in client.get("/" + path).text


def test_api_still_needs_login_and_app_token_is_refused_from_outside(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer t0k"}).status_code == 200
    outside = {"Authorization": "Bearer t0k", "Cf-Connecting-IP": "1.2.3.4"}
    assert client.get("/api/projects", headers=outside).status_code == 401


def test_site_logs_in_like_the_app(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    sent = []

    async def send(text):
        sent.append(text)

    monkeypatch.setattr(appserver, "send_code", send)
    device = "Website: Chrome on Mac"
    assert client.post("/api/login/start", json={"device": device}).status_code == 200
    assert device in sent[0]
    code = sent[0].split("code: ")[1][:6]
    session = client.post("/api/login", json={"code": code, "device": device}).json()["token"]
    outside = {"Authorization": f"Bearer {session}", "Cf-Connecting-IP": "1.2.3.4"}
    assert client.get("/api/projects", headers=outside).status_code == 200
