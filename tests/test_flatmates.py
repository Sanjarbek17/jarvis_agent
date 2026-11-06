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
    monkeypatch.setattr(f, "_viewing_topic", 0)
    monkeypatch.setattr(f, "CHAT", tmp_path / "chat.jsonl")
    monkeypatch.setattr(f, "POSTS", tmp_path / "posts.json")
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
    assert asyncio.run(f.answer("Ali", "qancha?")) == ("Panjob, $240.", 0)
    assert "2 rooms, Panjob, $240" in seen["prompt"] and "Ali: qancha?" in seen["prompt"] and "Free now." in seen["prompt"]


def test_other_chat_and_failures_are_silence(monkeypatch):
    answered(monkeypatch, {"reply": ""})
    assert asyncio.run(f.answer("Ali", "tushlik qildingmi?")) == ("", 0)
    answered(monkeypatch, {"reply": "NO_REPLY"})
    assert asyncio.run(f.answer("Ali", "hello")) == ("", 0)
    answered(monkeypatch, {}, fail=True)
    assert asyncio.run(f.answer("Ali", "qancha?")) == ("", 0)


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

    async def nothing():
        return None

    asyncio.run(f.on_message(NS(get_sender=sender, raw_text="qayerda?", reply=reply, get_reply_message=nothing), "Ali"))
    assert replies == ["Panjob."] and "Jarvis: Panjob." in f._history


def test_viewing_posts_use_a_viewing_topic_if_there_is_one():
    tg = Tg(topics=("New flat options", "Viewings"))
    asyncio.run(f.post(tg, {"text": "📅", "viewing": True}))
    asyncio.run(f.post(tg, {"text": "🏠"}))
    assert tg.sent[0][3]["reply_to"] == 11 and tg.sent[1][3]["reply_to"] == 10


def test_viewing_posts_stay_in_new_flat_options_otherwise():
    tg = Tg(topics=("General", "new flat options"))
    asyncio.run(f.post(tg, {"text": "📅", "viewing": True}))
    assert tg.sent[0][3]["reply_to"] == 11


def test_what_members_say_is_kept_for_the_search(monkeypatch):
    f.remember("Ali", "ertaga 5 da bo'sh man")
    rows = f.recent(0)
    assert rows[0]["name"] == "Ali" and "ertaga" in rows[0]["text"] and f.recent(rows[0]["ts"] + 1) == []


def test_the_chat_file_stays_small(monkeypatch):
    for _ in range(1500):
        f.remember("Ali", "x" * 200)
    assert len(f.CHAT.read_text().splitlines()) < 1500


def test_booked_viewings_are_in_the_flat_list():
    f.FLATMATES_VIEW.write_text(json.dumps([{"place": "2 rooms", "status": "open", "viewing": "Sat 7 Nov, 17:00, Gagarin 12"}]))
    assert "Viewing booked: Sat 7 Nov, 17:00, Gagarin 12" in f.flats()


# --- replies to a flat's post ------------------------------------------------------------------------------------

class Group(Tg):
    def __init__(self, history=(), fail_reply_to=0, **kw):
        super().__init__(**kw)
        self.history, self.fail_reply_to, self.next_id = list(history), fail_reply_to, 500

    async def send_message(self, to, text, **kw):
        if kw.get("reply_to") == self.fail_reply_to:
            raise ValueError("REPLY_MESSAGE_ID_INVALID")
        await super().send_message(to, text, **kw)
        self.next_id += 1
        return NS(id=self.next_id)

    async def send_file(self, to, files, caption="", **kw):
        await super().send_file(to, files, caption=caption, **kw)
        self.next_id += 1
        return [NS(id=self.next_id), NS(id=self.next_id + 1)]

    async def iter_messages(self, chat, limit=0):
        for m in self.history:
            yield m


def test_a_follow_up_replies_to_the_flats_post():
    tg = Group()
    asyncio.run(f.post(tg, {"text": "🏠 flat", "key": "@o", "link": "https://t.me/g/1", "first": True}))
    asyncio.run(f.post(tg, {"text": "Skip this one", "key": "@o", "link": "https://t.me/g/1"}))
    assert tg.sent[0][3]["reply_to"] == 11 and tg.sent[1][3]["reply_to"] == 501


def test_an_album_is_remembered_by_its_first_message(tmp_path):
    photo = tmp_path / "1.jpg"
    photo.write_bytes(b"x")
    tg = Group()
    asyncio.run(f.post(tg, {"text": "🏠", "photos": [str(photo)], "key": "@o", "first": True}))
    assert f._posts() == {"@o": 501}


def test_a_flat_posted_before_this_is_found_by_its_link():
    history = [NS(id=90, out=True, message="📅 Viewing https://t.me/g/1"), NS(id=70, out=True, message="🏠 flat https://t.me/g/1"),
               NS(id=60, out=False, message="look https://t.me/g/1"), NS(id=50, out=True, message="🏠 other")]
    tg = Group(history)
    asyncio.run(f.post(tg, {"text": "Skip this one", "key": "@o", "link": "https://t.me/g/1"}))
    assert tg.sent[0][3]["reply_to"] == 70 and f._posts() == {"@o": 70}


def test_with_no_post_found_it_goes_to_the_topic():
    tg = Group()
    asyncio.run(f.post(tg, {"text": "Skip this one", "key": "@o", "link": "https://t.me/g/1"}))
    assert tg.sent[0][3]["reply_to"] == 11


def test_a_deleted_post_doesnt_block_the_queue():
    f._keep_post("@o", 70)
    tg = Group(fail_reply_to=70)
    asyncio.run(f.post(tg, {"text": "Skip this one", "key": "@o"}))
    assert tg.sent[0][3]["reply_to"] == 11


def event(raw, earlier=None, client=None, replies=None):
    async def sender():
        return NS(bot=False)

    async def reply(text, **kw):
        replies.append(text)

    async def get_reply():
        return earlier

    return NS(get_sender=sender, raw_text=raw, reply=reply, get_reply_message=get_reply, client=client, chat_id=f.FLATMATES_GROUP_ID)


def test_the_model_sees_what_a_member_replied_to(monkeypatch):
    seen = answered(monkeypatch, {"reply": "The owner told us."})
    replies = []
    asyncio.run(f.on_message(event("How do you know?", NS(message="Skip this one, they don't rent to boys", out=True), replies=replies), "Ikrom"))
    assert "replies to your message" in seen["prompt"] and "don't rent to boys" in seen["prompt"]
    assert "Ikrom: How do you know?" in seen["prompt"] and replies == ["The owner told us."]


def test_a_topics_first_message_isnt_a_reply(monkeypatch):
    seen = answered(monkeypatch, {"reply": ""})
    asyncio.run(f.on_message(event("salom", NS(message="", out=False), replies=[]), "Ali"))
    assert "replies to" not in seen["prompt"].split("--- end ---")[1].split("Look at")[0]


def test_a_question_about_one_flat_is_answered_under_its_post(monkeypatch):
    f.FLATMATES_VIEW.write_text(json.dumps([{"place": "2 rooms", "key": "@o", "link": "https://t.me/g/1"}]))
    f._keep_post("@o", 70)
    answered(monkeypatch, {"reply": "$240.", "flat": 1})
    tg, replies = Group(), []
    asyncio.run(f.on_message(event("Panjob qancha?", client=tg, replies=replies), "Ali"))
    assert replies == [] and tg.sent[0][2] == "$240." and tg.sent[0][3]["reply_to"] == 70


def test_a_member_who_replied_gets_the_answer_under_their_message(monkeypatch):
    f.FLATMATES_VIEW.write_text(json.dumps([{"place": "2 rooms", "key": "@o"}]))
    f._keep_post("@o", 70)
    answered(monkeypatch, {"reply": "$240.", "flat": 1})
    tg, replies = Group(), []
    asyncio.run(f.on_message(event("qancha?", NS(message="🏠 2 rooms", out=True), client=tg, replies=replies), "Ali"))
    assert replies == ["$240."] and tg.sent == []


def test_nonsense_flat_numbers_are_ignored(monkeypatch):
    answered(monkeypatch, {"reply": "Hm.", "flat": 9})
    replies = []
    asyncio.run(f.on_message(event("nima?", client=Group(), replies=replies), "Ali"))
    assert replies == ["Hm."]


def test_a_message_meant_for_jarvis_must_get_an_answer(monkeypatch):
    seen = answered(monkeypatch, {"reply": "x"})
    asyncio.run(f.answer("Ali", "How do you know?", "Skip this one", True))
    assert f.TO_YOU in seen["prompt"] and "or empty" not in seen["prompt"]
    asyncio.run(f.answer("Ali", "Jarvis, qaysi biri yaxshi?"))
    assert f.TO_YOU in seen["prompt"]
    asyncio.run(f.answer("Ali", "tushlik qildingmi?"))
    assert f.TO_ANYONE in seen["prompt"] and f.TO_YOU not in seen["prompt"]
