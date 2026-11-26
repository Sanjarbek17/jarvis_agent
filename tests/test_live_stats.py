"""Ticket 0960b065: a status question about the Instagram projects gets numbers read right now, in code."""
import asyncio
import time

from core import appserver, livestats


def test_spots_status_questions_in_three_languages():
    for t in ("status", "holati qanday?", "nechta ko'rish bor", "сколько просмотров", "how many followers"):
        assert livestats.is_status(t), t
    assert not livestats.is_status("make the next reel about roundabouts")


def test_which_project_a_message_is_about():
    assert livestats.projects_of("haydovchi status") == ["Haydovchi darslari"]
    assert livestats.projects_of("rust channel views") == ["Instagram Channels"]
    assert livestats.projects_of("instagram status") == list(livestats.PROJECTS)
    assert livestats.projects_of("taxi app status") == []


def test_labels_say_live_or_how_old(monkeypatch):
    monkeypatch.setattr(livestats, "last_check", lambda: None)
    acc = [{"account": "haydovchi_darslari", "followers": 272, "reels": [{"url": "u1", "views": 57}]}]
    fresh = livestats.text({"project": "Haydovchi darslari", "at": time.time() - 60, "accounts": acc})
    old = livestats.text({"project": "Haydovchi darslari", "at": time.time() - 3600 * 10, "accounts": acc})
    assert "LIVE from Instagram" in fresh and "272 followers" in fresh and "57" in fresh
    assert "NOT LIVE" in old and "10 h" in old


def test_a_reading_is_reused_for_15_minutes_then_read_again(monkeypatch, tmp_path):
    monkeypatch.setattr(livestats, "CACHE", tmp_path / "live.json")
    monkeypatch.setattr(livestats, "accounts", lambda p: [{"account": "a", "profile": "x", "own": True}])
    runs = []

    async def one(acc, sem):
        runs.append(1)
        return {"account": "a", "followers": len(runs), "reels": []}

    monkeypatch.setattr(livestats, "_one", one)
    asyncio.run(livestats.fetch("Instagram Channels"))
    asyncio.run(livestats.fetch("Instagram Channels"))
    assert len(runs) == 1
    data = livestats._load()
    data["Instagram Channels"]["at"] -= livestats.MAX_AGE + 1
    livestats.CACHE.write_text(__import__("json").dumps(data))
    assert asyncio.run(livestats.fetch("Instagram Channels"))["accounts"][0]["followers"] == 2


def test_app_status_question_reaches_the_brain_with_live_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "_brains", {})
    asked, fetched = [], []

    class Brain:
        session_id = "s"

        async def reply(self, text):
            asked.append(text)
            return "ok"

    async def fetch(project, max_age=livestats.MAX_AGE):
        fetched.append(project)
        return {"project": project, "at": time.time(), "accounts": [{"account": "haydovchi_darslari", "followers": 300, "reels": []}]}

    monkeypatch.setattr(appserver, "_brain", lambda c: Brain())
    monkeypatch.setattr(livestats, "fetch", fetch)
    monkeypatch.setattr(livestats, "last_check", lambda: None)
    chat = appserver.new_chat("Haydovchi darslari", "s")
    asyncio.run(appserver.ask(chat, "status"))
    assert fetched == ["Haydovchi darslari"] and "[LIVE STATS" in asked[0] and "300 followers" in asked[0]
    asyncio.run(appserver.ask(appserver.load_chat(chat["id"]), "make tomorrow's reel about roundabouts"))
    assert fetched == ["Haydovchi darslari"] and "[LIVE STATS" not in asked[1]     # not a status question


def test_an_old_check_file_is_only_shown_with_its_age(monkeypatch):
    monkeypatch.setattr(livestats, "last_check", lambda: {"at": time.time() - 3600 * 9, "data": {"reels": [
        {"first_line": "Svetofor", "likes": 101, "saves": 18, "shares": 12}]}})
    t = livestats.text({"project": "Haydovchi darslari", "at": time.time(), "accounts": []})
    assert "Older, only for likes" in t and "9 h" in t
