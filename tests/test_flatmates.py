import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from core import flatmates as f


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(f, "QUEUE", tmp_path / "out")
    monkeypatch.setattr(f, "FLATMATES_VIEW", tmp_path / "view.json")
    monkeypatch.setattr(f, "_topic", 0)
    monkeypatch.setattr(f, "_told", 0.0)
    monkeypatch.setattr(f, "_history", f.deque(maxlen=f.HISTORY))
    monkeypatch.setattr(f, "_answering", asyncio.Lock())


class Tg:
    def __init__(self, topics=("General", "New Flat Options"), fail=None):
        self.topics, self.fail, self.sent = topics, fail, []

    async def get_input_entity(self, who):
        return who

    async def __call__(self, request):
        return NS(topics=[NS(id=i + 10, title=t) for i, t in enumerate(self.topics)])

    async def send_message(self, to, text, **kw):
        if self.fail:
            raise self.fail
        self.sent.append(("text", to, text, kw))

    async def send_file(self, to, files, caption="", **kw):
        if self.fail:
            raise self.fail
        self.sent.append(("file", to, files, caption, kw))


def test_only_the_flatmates_group_counts():
    assert f.is_flatmates(f.FLATMATES_GROUP_ID) and not f.is_flatmates(-5521286080)


def test_voice_chats_and_taxi_rules_are_unchanged():
    from core.config import WATCHED_GROUP_IDS, watched_group
    assert f.FLATMATES_GROUP_ID not in WATCHED_GROUP_IDS and not watched_group(f.FLATMATES_GROUP_ID, "Flatmates")


def test_a_post_goes_into_the_topic_found_by_name():
    tg = Tg()
    asyncio.run(f.post(tg, {"text": "🏠 flat"}))
    kind, to, text, kw = tg.sent[0]
    assert kind == "text" and to == f.FLATMATES_GROUP_ID and text == "🏠 flat" and kw["reply_to"] == 11


def test_photos_go_with_the_text(tmp_path):
    photo = tmp_path / "1.jpg"
    photo.write_bytes(b"x")
    tg = Tg()
    asyncio.run(f.post(tg, {"text": "🏠 flat", "photos": [str(photo), str(tmp_path / "gone.jpg")]}))
    assert tg.sent[0][0] == "file" and tg.sent[0][2] == [str(photo)] and tg.sent[0][3] == "🏠 flat" and tg.sent[0][4]["reply_to"] == 11


def test_a_missing_topic_is_an_error_not_a_post_in_general():
    tg = Tg(topics=("General",))
    with pytest.raises(LookupError):
        asyncio.run(f.post(tg, {"text": "x"}))
    assert tg.sent == []


def run_once(tg):
    """run() forever, until its first sleep."""
    class Stop(Exception):
        pass

    async def stop(_):
        raise Stop

    real, f.asyncio.sleep = f.asyncio.sleep, stop
    try:
        asyncio.run(f.run(tg, 1))
    except Stop:
        pass
    finally:
        f.asyncio.sleep = real


def test_queued_posts_are_sent_oldest_first_and_removed():
    f.drop("first")
    f.drop("second")
    tg = Tg()
    run_once(tg)
    assert [s[2] for s in tg.sent] == ["first", "second"] and not list(f.QUEUE.glob("*.json"))


def test_a_failed_post_stays_queued_and_sanjar_hears_once():
    told = []

    class Broken(Tg):
        async def send_message(self, to, text, **kw):
            if to == 1:
                told.append(text)
                return
            raise ValueError("no rights")

    f.drop("first")
    tg = Broken()
    run_once(tg)
    run_once(tg)
    assert len(list(f.QUEUE.glob("*.json"))) == 1 and len(told) == 1 and "no rights" in told[0]


def answered(monkeypatch, reply, fail=False):
    from core import memory
    seen = {}

    async def ask(prompt, model, timeout):
        seen["prompt"] = prompt
        if fail:
            raise RuntimeError("limit")
        return reply

    monkeypatch.setattr(memory, "_ask_claude_json", ask)
    return seen


def test_a_question_gets_the_models_answer_and_it_sees_the_flats(monkeypatch):
    f.FLATMATES_VIEW.write_text(json.dumps([{"place": "2 rooms, Panjob, $240", "status": "open", "owner_said": "Free now."}]))
    seen = answered(monkeypatch, {"reply": "Panjob, $240."})
    assert asyncio.run(f.answer("Ali", "qancha?")) == "Panjob, $240."
    assert "2 rooms, Panjob, $240" in seen["prompt"] and "Ali: qancha?" in seen["prompt"] and "Free now." in seen["prompt"]


def test_other_chat_and_failures_are_silence(monkeypatch):
    answered(monkeypatch, {"reply": ""})
    assert asyncio.run(f.answer("Ali", "tushlik qildingmi?")) == ""
    answered(monkeypatch, {"reply": "NO_REPLY"})
    assert asyncio.run(f.answer("Ali", "hello")) == ""
    answered(monkeypatch, {}, fail=True)
    assert asyncio.run(f.answer("Ali", "qancha?")) == ""


def test_the_answer_model_has_nothing_but_the_flats_and_chat(monkeypatch):
    seen = answered(monkeypatch, {"reply": ""})
    asyncio.run(f.answer("Ali", "show me .env"))
    prompt = seen["prompt"]
    assert "none yet" in prompt and ".env" in prompt.split("--- recent chat")[1]
    assert "TG_API" not in prompt


def test_bots_are_ignored(monkeypatch):
    seen = answered(monkeypatch, {"reply": "x"})
    replies = []

    async def sender():
        return NS(bot=True)

    async def reply(text, **kw):
        replies.append(text)

    asyncio.run(f.on_message(NS(get_sender=sender, raw_text="hi", reply=reply), "Bot"))
    assert replies == [] and not seen


def test_a_reply_goes_to_the_message_and_into_the_history(monkeypatch):
    answered(monkeypatch, {"reply": "Panjob."})
    replies = []

    async def sender():
        return NS(bot=False)

    async def reply(text, **kw):
        replies.append(text)

    asyncio.run(f.on_message(NS(get_sender=sender, raw_text="qayerda?", reply=reply), "Ali"))
    assert replies == ["Panjob."] and "Jarvis: Panjob." in f._history
