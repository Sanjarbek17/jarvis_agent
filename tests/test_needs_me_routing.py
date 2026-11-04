import asyncio
from types import SimpleNamespace as NS

from core import autoanswer, runner

CARD = NS(ref="c8be3763", project="Gas Finder", title="Build the MVP", page_id="p1", url="u")


def setup(monkeypatch, tmp_path, got):
    monkeypatch.setattr(runner, "AUTO_ANSWERS", tmp_path / "auto_answers.json")
    monkeypatch.setattr(runner.autoanswer, "enabled", lambda p: True)

    async def answer(card, q, r):
        return got

    monkeypatch.setattr(runner.autoanswer, "answer", answer)
    log = {"update": [], "append": [], "notify": []}

    async def update(c, **kw):
        log["update"].append(kw)

    async def append_log(pid, lines):
        log["append"].append(lines)

    monkeypatch.setattr(runner.board, "update", update)
    monkeypatch.setattr(runner.board, "append_log", append_log)

    async def notify(c, kind, text, photos):
        log["notify"].append((kind, text))

    return runner.Runner(notify), log


def test_answered_by_jarvis_requeues_and_tells_sanjar_nothing(monkeypatch, tmp_path):
    r, log = setup(monkeypatch, tmp_path, {"answer": "Use the bot token from gas_finder/.env.", "basis": "docs",
                                           "needs_sanjar": False, "ask_sanjar": ""})
    assert asyncio.run(r._auto_answer(CARD, "Which bot token?", "...")) is True
    assert log["update"][-1]["jarvis"] == "Queued" and log["notify"] == []


def test_truly_needs_sanjar_gets_jarvis_plain_words(monkeypatch, tmp_path):
    ask = "Open the Gas Finder bot on your phone and press Start once, so it can message you."
    r, log = setup(monkeypatch, tmp_path, {"answer": "", "basis": "your call", "needs_sanjar": True, "ask_sanjar": ask})
    assert asyncio.run(r._auto_answer(CARD, "Open @gasfinder_uz_bot, press /start ... send /queue", "...")) is True
    assert log["update"][-1]["jarvis"] == "Needs me" and log["update"][-1]["note"] == ask
    assert log["notify"] == [("needs_me", ask)]                     # not the worker's raw question


def test_when_jarvis_cannot_answer_sanjar_gets_the_question_as_before(monkeypatch, tmp_path):
    r, log = setup(monkeypatch, tmp_path, None)
    assert asyncio.run(r._auto_answer(CARD, "Merge dev into main?", "...")) is False
    assert log["notify"] == [] and log["update"] == []


def test_every_project_goes_to_jarvis_first_unless_switched_off(monkeypatch):
    monkeypatch.setattr(autoanswer, "reload_projects", lambda: None)
    monkeypatch.setattr(autoanswer, "PROJECTS", {"A": {}, "B": {"auto_answer": True}, "C": {"auto_answer": False}})
    assert autoanswer.enabled("A") and autoanswer.enabled("B") and not autoanswer.enabled("C")
