"""Ticket f4f11c38: ticket messages and talk about a project land in that project's chat, not General."""
import ast
import asyncio
import logging
from types import SimpleNamespace as NS

import pytest

from core import appserver, origin, tools


@pytest.fixture
def chats(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "IMAGES", tmp_path / "images")
    monkeypatch.setattr(appserver, "find_project", lambda n: next(
        (p for p in ("General", "Code Learning", "Theater") if p.lower() == n.strip().lower()), ""))


def test_news_goes_to_the_projects_latest_chat(chats):
    general = appserver.new_chat("General", "lessons")
    old = appserver.new_chat("Code Learning", "old")
    new = appserver.new_chat("Code Learning", "newer")
    assert appserver.project_chat("Code Learning", general["id"]) == new["id"]       # asked in General
    assert appserver.project_chat("Code Learning", old["id"]) == old["id"]           # asked in one of its chats
    assert appserver.project_chat("Code Learning") == new["id"]                      # asked on Telegram
    assert appserver.project_chat("General", general["id"]) == general["id"]


def test_a_project_without_chats_gets_one(chats):
    cid = appserver.project_chat("Theater")
    chat = appserver.load_chat(cid)
    assert chat["project"] == "Theater" and chat["title"] == "Theater updates"
    assert appserver.project_chat("Theater") == cid                                 # reused next time


def test_unknown_project_keeps_the_asking_chat(chats):
    assert appserver.project_chat("Nope", "abc") == "abc" and appserver.project_chat("Nope") == ""


def test_a_general_chat_about_one_project_moves_there(chats):
    chat = appserver.new_chat("General", "lessons")
    assert appserver.follow_ticket(chat["id"], "Code Learning") == "Code Learning"
    assert appserver.load_chat(chat["id"])["project"] == "Code Learning"
    assert appserver.follow_ticket(chat["id"], "Code Learning") == ""               # already there


def test_a_general_chat_about_several_projects_stays(chats):
    chat = appserver.new_chat("General", "mixed")
    appserver.follow_ticket(chat["id"], "Theater")
    appserver.move_chat(chat["id"], "General")                                       # he moved it back
    assert appserver.follow_ticket(chat["id"], "Code Learning") == ""
    assert appserver.load_chat(chat["id"])["project"] == "General"


def test_updates_and_project_chats_never_move(chats):
    appserver.push("hello")                                                          # creates Updates
    assert appserver.follow_ticket(appserver.UPDATES, "Theater") == ""
    theater = appserver.new_chat("Theater", "t")
    assert appserver.follow_ticket(theater["id"], "Code Learning") == ""
    assert appserver.load_chat(theater["id"])["project"] == "Theater"


def test_tool_says_the_chat_moved(chats):
    chat = appserver.new_chat("General", "lessons")
    assert "moved under Code Learning" in tools._followed({"channel": "app", "chat": chat["id"]}, "Code Learning")
    assert tools._followed({}, "Code Learning") == ""                                # Telegram: nothing to move


def _jarvis(names, **g):
    src = open("jarvis.py").read()
    keep = [n for n in ast.parse(src).body if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names)
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("REPORT_PROMPT", "MUST_TELL"))]
    exec(compile(ast.Module(keep, []), "jarvis.py", "exec"), g)
    return g


def run_notify(monkeypatch, asked: str):
    telegram, app = [], []

    async def send(tg, to, text, **kw):
        telegram.append(text)

    async def reply(t):
        return "The lessons fix is done, sir."

    async def no_photos(*a):
        pass

    g = _jarvis({"relay", "notify"}, chat=NS(reply=reply), tg=None, OWNER_ID=1, is_silent=lambda t: False,
                appserver=appserver, origin=origin, call=None, runner=NS(from_sanjar=lambda r: False),
                tgfmt=NS(send=send, md=lambda t: t), app_push=lambda t, c="", ph=(): app.append(c),
                send_photos=no_photos, log=logging.getLogger(), asyncio=asyncio, Path=__import__("pathlib").Path, Ticket=object)
    origin.set("cl-1", {"channel": "app", "chat": asked} if asked else {})

    async def go():
        await g["notify"](NS(ref="cl-1", title="Fix lessons", project="Code Learning"), "done", "ok", [])
        await asyncio.sleep(0.05)

    asyncio.run(go())
    return telegram, app


def test_asked_in_general_the_result_goes_to_the_projects_chat(chats, monkeypatch):
    general = appserver.new_chat("General", "lessons")
    project = appserver.new_chat("Code Learning", "lessons")
    telegram, app = run_notify(monkeypatch, general["id"])
    assert app == [project["id"]] and telegram == []


def test_asked_on_telegram_it_goes_to_telegram_and_the_projects_chat(chats, monkeypatch):
    project = appserver.new_chat("Code Learning", "lessons")
    for asked in ("", appserver.UPDATES):                                            # Telegram, or Updates (= Telegram)
        telegram, app = run_notify(monkeypatch, asked)
        assert telegram == ["The lessons fix is done, sir."] and app == [project["id"]]


def test_asked_in_the_projects_own_chat_stays_there(chats, monkeypatch):
    appserver.new_chat("Code Learning", "newer one")
    asked = appserver.new_chat("Code Learning", "where he asked")
    appserver.new_chat("Code Learning", "newest")
    telegram, app = run_notify(monkeypatch, asked["id"])
    assert app == [asked["id"]] and telegram == []
