"""The website is served by the app server, so the app and the site share one backend."""
import asyncio
from types import SimpleNamespace as NS

from starlette.testclient import TestClient

from core import appserver, origin, tools


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


def test_news_from_a_chat_says_how_to_open_that_chat(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer t0k"}
    chat = appserver.new_chat("Jarvis", "Website plan")
    gone = appserver.new_chat("Jarvis", "Deleted later")
    appserver.push("From the chat", chat["id"])
    appserver.push("From a chat that went away", gone["id"])
    appserver.push("From nowhere")
    (appserver.CHATS / f"{gone['id']}.json").unlink()

    items = client.get("/api/notifications", headers=auth).json()["items"]
    assert (items[0]["chat"], items[0]["chat_project"]) == (chat["id"], "Jarvis")
    assert "chat_project" not in items[1] and "chat_project" not in items[2]
    shown = client.get(f"/api/chats/{appserver.UPDATES}/messages", headers=auth).json()
    assert shown[0]["chat_project"] == "Jarvis"


def _move_setup(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(appserver, "reload_projects", lambda: None)
    monkeypatch.setattr(appserver, "PROJECTS", {"Jarvis": {}})
    return client, {"Authorization": "Bearer t0k"}


def test_a_chat_moves_to_another_project_and_keeps_its_messages(tmp_path, monkeypatch):
    client, auth = _move_setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General", "Idea")
    chat["messages"].append({"role": "me", "text": "hi", "ts": 1.0})
    appserver.save_chat(chat)

    res = client.post(f"/api/chats/{chat['id']}/move", json={"project": "jarvis"}, headers=auth)
    assert res.status_code == 200 and res.json()["project"] == "Jarvis"
    assert [c["id"] for c in appserver.chats_of("Jarvis")] == [chat["id"]] and not appserver.chats_of("General")
    assert appserver.load_chat(chat["id"])["messages"][0]["text"] == "hi"
    assert "Jarvis" in appserver.context_line(appserver.load_chat(chat["id"])["project"])


def test_a_move_needs_login_and_a_real_target(tmp_path, monkeypatch):
    client, auth = _move_setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General")
    path = f"/api/chats/{chat['id']}/move"
    assert client.post(path, json={"project": "Jarvis"}).status_code == 401
    assert client.post(path, json={"project": "Nope"}, headers=auth).status_code == 400
    assert client.post("/api/chats/missing/move", json={"project": "Jarvis"}, headers=auth).status_code == 404
    appserver.push("news")
    assert client.post(f"/api/chats/{appserver.UPDATES}/move", json={"project": "Jarvis"}, headers=auth).status_code == 400
    assert appserver.load_chat(chat["id"])["project"] == "General"


def test_a_chat_waits_for_a_project_that_is_being_created(tmp_path, monkeypatch):
    _move_setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General")
    assert appserver.move_chat(chat["id"], "Code Learning", when_created=True)["move_to"] == "Code Learning"
    appserver.finish_moves()
    assert appserver.load_chat(chat["id"])["project"] == "General"            # not there yet

    appserver.PROJECTS["Code Learning"] = {}
    appserver.finish_moves()
    moved = appserver.load_chat(chat["id"])
    assert moved["project"] == "Code Learning" and "move_to" not in moved
    assert "Moved this chat to Code Learning." in moved["messages"][-1]["text"]
    appserver.finish_moves()                                                  # once only
    assert len(appserver.load_chat(chat["id"])["messages"]) == 1


def test_jarvis_moves_the_chat_it_is_in_with_its_tool(tmp_path, monkeypatch):
    _move_setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General")

    async def call(header, **args):
        ctx = NS(request_context=NS(request=NS(headers={origin.HEADER: header} if header else {})))
        tool = tools.make_server("Telegram")._tool_manager.get_tool("move_chat")
        return await tool.fn(ctx=ctx, **args)

    assert "Moved this chat to Jarvis" in asyncio.run(call(f"app:{chat['id']}", project="Jarvis"))
    assert appserver.load_chat(chat["id"])["project"] == "Jarvis"
    assert "as soon as" in asyncio.run(call(f"app:{chat['id']}", project="New One", when_created=True))
    assert "Couldn't" in asyncio.run(call(f"app:{chat['id']}", project="Nope"))
    assert "isn't one" in asyncio.run(call("", project="Jarvis"))              # Telegram


def test_a_reply_says_where_the_chat_is_now(tmp_path, monkeypatch):
    client, auth = _move_setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General")

    class Moves:
        session_id, lock = "", asyncio.Lock()

        async def reply(self, text):
            appserver.move_chat(chat["id"], "Jarvis")
            return "Moved it."

    monkeypatch.setattr(appserver, "_brain", lambda c: Moves())
    res = client.post(f"/api/chats/{chat['id']}/messages", json={"text": "move this"}, headers=auth).json()
    assert res["chat_project"] == "Jarvis" and appserver.load_chat(chat["id"])["project"] == "Jarvis"
