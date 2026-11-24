"""The website is served by the app server, so the app and the site share one backend."""
import asyncio
import json
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


def _files_setup(tmp_path, monkeypatch):
    client, auth = _move_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(appserver, "FILES", tmp_path / "files")
    return client, auth


def test_a_sent_file_lands_in_the_asking_chat_and_downloads_with_login_only(tmp_path, monkeypatch):
    client, auth = _files_setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("Jarvis", "Video")
    src = tmp_path / "gas finder.mp4"
    src.write_bytes(b"movie")
    asyncio.run(appserver.push_file(src, "Your video", chat["id"]))

    msg = appserver.load_chat(chat["id"])["messages"][-1]
    file = msg["files"][0]
    assert msg["text"] == "Your video" and file["title"] == "gas finder.mp4" and file["size"] == 5
    assert appserver.updates_since(0)[-1]["files"] == [file]            # the Updates chat has it too
    url = f"/api/files/{file['name']}"
    assert client.get(url).status_code == 401
    res = client.get(url, headers=auth)
    assert res.content == b"movie" and "attachment" in res.headers["content-disposition"]
    assert res.headers["content-security-policy"] == "sandbox"
    for bad in ("nope", "..%2f.env", "0123456789abcdef"):
        assert client.get(f"/api/files/{bad}", headers=auth).status_code == 404


def test_file_without_a_chat_goes_to_updates_and_big_ones_stay_on_telegram(tmp_path, monkeypatch):
    _files_setup(tmp_path, monkeypatch)
    src = tmp_path / "report.pdf"
    src.write_bytes(b"pdf")
    asyncio.run(appserver.push_file(src))
    assert appserver.updates_since(0)[-1]["text"] == "File: report.pdf"

    monkeypatch.setattr(appserver, "FILE_MAX", 1)
    asyncio.run(appserver.push_file(src, "Report"))
    last = appserver.updates_since(0)[-1]
    assert "files" not in last and "on Telegram: report.pdf" in last["text"]


def test_only_send_file_notes_reach_the_app(tmp_path, monkeypatch):
    from core import outbox
    monkeypatch.setattr(outbox, "OUTBOX", tmp_path / "out")
    outbox.OUTBOX.mkdir()
    (outbox.OUTBOX / "1.json").write_text(json.dumps({"file": "/x/a.apk", "caption": "app", "app": True, "chat": "c1"}))
    (outbox.OUTBOX / "2.json").write_text(json.dumps({"file": "/x/qr.png", "caption": "login"}))
    seen = []

    async def on_file(*args):
        seen.append(args)

    class Tg:
        async def send_file(self, *a, **k):
            return NS(id=1)

    async def go():
        job = asyncio.create_task(outbox.run(Tg(), 1, on_file))
        await asyncio.sleep(0.1)
        job.cancel()

    asyncio.run(go())
    assert seen == [("/x/a.apk", "app", "c1")]


def test_projects_with_new_activity_and_new_projects_come_first(tmp_path, monkeypatch):
    client, auth = _move_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(appserver, "PROJECTS", {"Old": {}, "Quiet": {}, "Newest": {}})
    names = lambda: [p["name"] for p in client.get("/api/projects", headers=auth).json()]
    assert names() == ["General", "Newest", "Quiet", "Old"]
    appserver.new_chat("Old", "Back at it")
    assert names() == ["General", "Old", "Newest", "Quiet"]


def test_telegram_and_the_website_are_one_conversation(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    sent, asked = [], []

    class Main:
        session_id = "main"

        async def reply(self, text):
            asked.append(text)
            return "Answer"

    async def tell(text):
        sent.append(text)

    monkeypatch.setattr(appserver, "main_brain", Main())
    monkeypatch.setattr(appserver, "tell_telegram", tell)
    monkeypatch.setattr(appserver, "_brains", {})

    appserver.telegram_said("me", "hello from Telegram")
    appserver.telegram_said("jarvis", "hello back")
    chat = appserver.load_chat(appserver.UPDATES)
    assert [m["role"] for m in chat["messages"]] == ["me", "jarvis"]
    assert appserver.updates_since(0) == []                 # the conversation is not news

    asyncio.run(appserver.ask(chat, "hello from the site"))
    assert "hello from the site" in asked[0]                # the same brain as Telegram
    assert sent == ["From the website: hello from the site", "Answer"]
    assert appserver._brains == {}                          # so the idle check never stops it
    assert [m["text"] for m in appserver.load_chat(appserver.UPDATES)["messages"]][-2:] == ["hello from the site", "Answer"]


def test_website_chat_thinking_and_stop_with_session(tmp_path, monkeypatch):
    import time
    from core.brain import Brain
    from core import answering
    client = _client(tmp_path, monkeypatch)
    sent = []

    async def send(text):
        sent.append(text)

    monkeypatch.setattr(appserver, "send_code", send)
    monkeypatch.setattr(answering, "DIR", tmp_path / "answering")
    answering.clear()

    # Log in as a website session
    device = "Website: Chrome on Mac"
    assert client.post("/api/login/start", json={"device": device}).status_code == 200
    code = sent[0].split("code: ")[1][:6]
    session = client.post("/api/login", json={"code": code, "device": device}).json()["token"]
    auth = {"Authorization": f"Bearer {session}", "Cf-Connecting-IP": "1.2.3.4"}

    chat = appserver.new_chat("General", "Thinking test")
    cid = chat["id"]

    # Initially not answering
    res = client.get(f"/api/chats/{cid}/thinking", headers=auth)
    assert res.status_code == 200
    assert res.json()["answering"] is False

    # Simulate live thinking
    appserver._thinking[cid] = {
        "started": time.time() - 1.5,
        "step": "reading Api.kt",
        "steps": ["searching the code", "reading Api.kt"],
    }
    with answering.mark(cid):
        res = client.get(f"/api/chats/{cid}/thinking", headers=auth)
        assert res.status_code == 200
        data = res.json()
        assert data["answering"] is True
        assert data["step"] == "reading Api.kt"
        assert data["steps"] == ["searching the code", "reading Api.kt"]

        # Check messages headers
        msg_res = client.get(f"/api/chats/{cid}/messages", headers=auth)
        assert msg_res.status_code == 200
        assert msg_res.headers.get("X-Jarvis-Answering") == "true"
        assert msg_res.headers.get("X-Jarvis-Step") == "reading Api.kt"

        # Mock brain to test stop
        mock_brain = Brain("chat")
        mock_brain.was_stopped = False
        appserver._brains[cid] = (mock_brain, time.monotonic())

        # Stop thinking from website
        stop_res = client.post(f"/api/chats/{cid}/stop", headers=auth)
        assert stop_res.status_code == 200
        assert stop_res.json()["ok"] is True
        assert stop_res.json()["stopped"] is True
        assert mock_brain.was_stopped is True
        assert cid not in appserver._thinking
        assert not answering.is_answering(cid)

    appserver._brains.pop(cid, None)
    appserver._thinking.pop(cid, None)
    answering.clear()


def test_website_files_contain_thinking_and_stop_button(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    js = client.get("/app.js").text
    assert "thinking" in js
    assert "thinkingCard" in js
    assert "stopJarvis" in js
    assert "Stop thinking" in js
    assert "/api/chats/" in js
    assert "/thinking" in js
    assert "/stop" in js

    css = client.get("/style.css").text
    assert ".bubble.thinking-card" in css
    assert ".thinking-stop-btn" in css
    assert ".thinking-step" in css
    assert ".thinking-toggle" in css

