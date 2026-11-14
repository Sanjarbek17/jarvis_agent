"""Jarvis: Telegram calls + messages, Claude brain, Notion board, runner. One process, always on.

    uv run jarvis.py           # start and wait
    uv run jarvis.py --call    # start and call me right away

- Call: my voice -> Gemini transcribe -> Claude (call brain) -> Gemini voice -> call.
- Telegram text or voice message from me -> Claude (chat brain) -> reply. /status, /stop <project|mac>, /call, /transcript.
- Runner picks tickets marked Queued (Jarvis column) on each project's Notion board.
- Jarvis never phones me (CALL_SANJAR off): updates and the daily summary come as Telegram text
  (not in quiet hours, at most once per CALL_COOLDOWN).
- Jarvis calls other people when I ask (call_contact) to give them a message; that call gets a
  brain with no tools but hang_up, and I get its transcript in Telegram.
- When a voice chat starts in a group the Jarvis account is in, it joins: keeps a who-said-what
  transcript, answers only when someone says "Jarvis", and sends me a summary with possible
  tickets afterwards; I pick which become tickets.
- In groups it also answers text messages that say "Jarvis". In groups it's read-only: it can
  only look up the Taxi App board.
Only OWNER_ID is ever answered.
"""
import asyncio
import contextlib
import fcntl
import functools
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import NotInCallError
from pytgcalls.types import (CallConfig, ChatUpdate, Device, Direction, ExternalMedia,
                             MediaStream, RecordStream, StreamFrames)
from pytgcalls.types.raw import AudioParameters
from telethon import TelegramClient, events
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import InputPeerChannel, MessageActionGroupCall

from core.paths import VAR
from core import answering, appserver, board, usagewatch, contacts, coremem, expenses, flatmates, google, guard, livestats, macremote, memory, origin, outbox, selfcheck, tgfmt, tools
from core.board import Ticket
from core.brain import SUMMARY_PROMPT, Brain, group_prompt, guest_prompt, is_silent
from core.config import (HERE, CALLS, CALL_COOLDOWN, CALL_ON, CALL_SANJAR, watched_group, WATCHED_GROUP_IDS, CALL_RATE, DAILY_CALL_AT, INBOX, LOGS, OWNER_ID,
                         PROJECTS, PROJECTS_URL, QUIET_HOURS, SESSION, SOFT_SKILLS_REMINDER_AT, TG_API_HASH,
                         TG_API_ID)
from core.mac import MacWorker
from core.runner import Runner
from core.voice import Ears, Interrupter, Mouth, SilenceWatch, split_sentences, transcribe_audio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOGS / "jarvis.log")])
for noisy in ("telethon", "google_genai", "httpx", "websockets", "mcp", "uvicorn"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("jarvis")



def swap_in_new_login() -> None:
    """scripts/relogin.py leaves a fresh login next to the old one; put it in place before the
    session file is opened. Never while another Jarvis runs (it has the old file open)."""
    new, live = SESSION.with_name("jarvis_new.session"), SESSION.with_suffix(".session")
    if not new.exists() or outbox.jarvis_running():
        return
    backup = live.with_name(f"jarvis.session.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    if live.exists():
        shutil.copy2(live, backup)
    live.with_name("jarvis.session-journal").unlink(missing_ok=True)
    os.replace(new, live)
    log.info("switched to the new Telegram login from relogin.py (old one: %s)", backup.name)


swap_in_new_login()
class _OncePerMinute(logging.Filter):
    """Telethon logs every failed connect try; while the network is down that's one line every few seconds."""
    def __init__(self):
        super().__init__()
        self.last = 0.0

    def filter(self, record):
        if "connecting failed" not in record.getMessage():
            return True
        if time.monotonic() - self.last < 60:
            return False
        self.last = time.monotonic()
        record.msg, record.args = f"still no connection to Telegram, retrying: {record.getMessage()}", ()
        return True


logging.getLogger("telethon.network.mtprotosender").addFilter(_OncePerMinute())
# Network drops: keep retrying instead of giving up after 5 tries. Giving up ended main(), launchd restarted
# Jarvis every ~15 s, and each restart cut off the running workers (2026-11-12 03:53, ticket 2475b845).
tg = TelegramClient(str(SESSION), TG_API_ID, TG_API_HASH, connection_retries=-1, retry_delay=5, auto_reconnect=True)
calls = PyTgCalls(tg)
CHUNK = CALL_RATE // 100 * 2 * 2      # 10 ms of 48 kHz stereo s16 going into the call
EMOJI = {"done": "✅", "failed": "❌", "needs_me": "❓", "stopped": "⏹", "auto_answered": "🤖"}
# someone in a group call speaks to Jarvis; also the ways speech-to-text tends to spell it
WAKE = re.compile(r"d?[jy]arvi[sz]?|jarves|jervis|jarbis|charvis|[дж]?жарви[сз]?|джарвис|ժարվիս|"
                  r"जार्विस|जारविस|जार्वि|ज़ार्विस|ジャービス|ジャーヴィス|자비스|贾维斯", re.I)
# a group message about arranging a meeting (Uzbek / Russian / English); Jarvis may step in to help
MEETING = re.compile(r"uchrash|yig'?ilish|majlis|meeting|schedule|созвон|встреч|собра|митинг|"
                     r"davomida|soat\s*\d|\bat\s*\d{1,2}(:\d{2})?\s*(am|pm|да)?\b", re.I)


class Call:
    """One phone call: ears + Claude + mouth, and the audio clock. With me, or with `guest`
    (someone Jarvis calls to give them `message` from me)."""

    def __init__(self, user_id: int, guest: str = "", message: str = ""):
        self.user_id = user_id
        self.guest = guest
        self.brain = Brain("guest", guest_prompt(guest, message)) if guest else Brain("call")
        self.mouth = Mouth()
        self.ears = Ears(self.on_speaking, self.on_sentence)
        self.interrupt = Interrupter(self.mouth, self.stop_talking)
        self.turns: asyncio.Queue[str] = asyncio.Queue()
        self.turn_id = 0
        self.transcript: list[str] = []
        self.ended = False
        self.tasks: list[asyncio.Task] = []

    async def start(self, reason: str = ""):
        """Pick up / place the call. `reason`: why Jarvis is calling me (it opens with that)."""
        # Jarvis's voice goes in as stereo (the library's external-audio example); I come out as mono
        out_params, in_params = AudioParameters(CALL_RATE, 2), AudioParameters(CALL_RATE, 1)
        self.tasks = [asyncio.create_task(self.brain.start()), asyncio.create_task(self._think())]
        self.ears.start()
        await calls.play(self.user_id, MediaStream(ExternalMedia.AUDIO, out_params), CallConfig(timeout=60))
        self.tasks.append(asyncio.create_task(self._play()))  # only once the call's audio input exists
        await calls.record(self.user_id, RecordStream(True, in_params))
        if self.guest:
            self.tell(f"(You called {self.guest} and they just picked up: greet them and give the message.)")
        elif reason:
            self.tell(f"{reason}\n(You placed this call and he just picked up: greet him briefly and tell him why.)")
        else:
            self.mouth.say("Hi Sanjar, Jarvis here.")
        log.info("on the call%s", f" with {self.guest}" if self.guest else " (Jarvis called)" if reason else "")

    def hear(self, pcm: bytes):
        """Audio from the other side: to the transcription, and to what stops Jarvis when they talk."""
        self.ears.hear(pcm)
        self.interrupt.heard(pcm)

    def stop_talking(self):
        self.turn_id += 1
        self.mouth.stop()

    async def on_speaking(self, text: str):
        self.interrupt.words(text)

    async def on_sentence(self, text: str):
        if not self.interrupt.sentence(text):
            log.info("ignored while Jarvis talks (likely echo): %s", text)
            return
        self._note(self.guest or "Sanjar", text)
        await self.turns.put(text)

    def tell(self, update: str):
        """A runner update during the call; Claude decides how to say it."""
        self.turns.put_nowait(update)

    async def _think(self):
        while True:
            text = await self.turns.get()
            turn, buffer, said = self.turn_id, "", []
            try:
                async for chunk in self.brain.ask(text):
                    buffer += chunk
                    sentences, buffer = split_sentences(buffer)
                    for s in sentences:
                        if is_silent(s):
                            continue           # "NO_REPLY" / "(no reply)": say nothing
                        said.append(s)
                        if turn == self.turn_id:   # not interrupted since
                            self.mouth.say(s)
                if buffer.strip() and not is_silent(buffer):
                    said.append(buffer.strip())
                    if turn == self.turn_id:
                        self.mouth.say(buffer.strip())
            except Exception:
                log.exception("brain turn failed")
                self.mouth.say("Sorry, something went wrong on my side.")
            if said:
                self._note("Jarvis", " ".join(said))

    async def _play(self):
        """Feed Jarvis's voice into the call in 10 ms frames on a steady clock."""
        silence, next_at = bytes(CHUNK), time.monotonic()
        sent, failed, last_report = 0, 0, time.monotonic()
        while not self.ended:
            self.interrupt.tick()
            out = self.mouth.out
            if len(out) >= CHUNK and not self.mouth.paused:
                frame = bytes(out[:CHUNK])
                del out[:CHUNK]
                sent += 1
            else:
                frame = silence
            try:
                await calls.send_frame(self.user_id, Device.MICROPHONE, frame)
            except NotInCallError:
                pass
            except Exception as e:
                failed += 1
                if failed in (1, 100, 1000):   # don't flood the log; a dead loop meant silence before
                    log.error("sending voice to the call failed (%d times): %r", failed, e)
            if time.monotonic() - last_report > 10 and (sent or failed):
                log.info("voice frames: %d sent, %d failed in the last 10 s", sent, failed)
                sent, failed, last_report = 0, 0, time.monotonic()
            next_at += 0.01
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    def _note(self, who: str, text: str):
        text = " ".join(text.split())
        log.info("%s: %s", who, text)
        self.transcript.append(f"{who}: {text}")

    async def close(self):
        self.ended = True
        for t in self.tasks:
            t.cancel()
        await self.ears.stop()
        await self.mouth.close()
        await self.brain.stop()


class GroupCall(Call):
    """A group voice chat Jarvis joined by itself. Every speaker gets their own ears, so the
    transcript says who said what; the brain only hears it when someone says "Jarvis".
    Nothing said here is acted on: I pick tickets from the summary afterwards."""

    def __init__(self, chat_id: int, title: str):
        super().__init__(chat_id)
        self.title = title
        self.brain = Brain("group", group_prompt(title, voice=True))
        self.speakers: dict[int, Ears] = {}   # audio source -> their ears
        self.names: dict[int, str] = {}       # audio source -> name
        self.sent = 0                         # transcript lines the brain has already seen

    async def start(self, reason: str = ""):
        out_params, in_params = AudioParameters(CALL_RATE, 2), AudioParameters(CALL_RATE, 1)
        self.tasks = [asyncio.create_task(self.brain.start()), asyncio.create_task(self._think())]
        await calls.play(self.user_id, MediaStream(ExternalMedia.AUDIO, out_params))
        self.tasks.append(asyncio.create_task(self._play()))
        await calls.record(self.user_id, RecordStream(True, in_params))
        self.tasks.append(asyncio.create_task(self._watch()))
        log.info("in the group call of %s", self.title)

    def hear(self, source: int, pcm: bytes):
        source &= 0xFFFFFFFF
        if source not in self.speakers:
            self.speakers[source] = Ears(self._no_interrupt, functools.partial(self.on_said, source))
            self.speakers[source].start()
        self.speakers[source].hear(pcm)

    async def _no_interrupt(self, text: str):
        pass   # many people talk in a group; Jarvis's answers are short, let them finish

    async def on_said(self, source: int, text: str):
        self._note(await self.name_of(source), text)
        if WAKE.search(text):
            await self.turns.put("\n".join(self.transcript[self.sent:]))
            self.sent = len(self.transcript)

    async def name_of(self, source: int) -> str:
        if source not in self.names:
            with contextlib.suppress(Exception):
                for p in await calls.get_participants(self.user_id) or []:
                    if p.source & 0xFFFFFFFF not in self.names:
                        self.names[p.source & 0xFFFFFFFF] = (
                            "Sanjar" if p.user_id == OWNER_ID else contacts.name_of(await tg.get_entity(p.user_id)))
            self.names.setdefault(source, f"Speaker {len(self.names) + 1}")
        return self.names[source]

    async def _watch(self):
        """Leave once everyone else has left (the voice chat may stay open)."""
        me = (await tg.get_me()).id
        while not self.ended:
            await asyncio.sleep(30)
            with contextlib.suppress(Exception):
                if not [p for p in await calls.get_participants(self.user_id) or [] if p.user_id != me]:
                    asyncio.create_task(finish_group())
                    return

    async def close(self):
        for ears in self.speakers.values():
            await ears.stop()
        await super().close()


call: Call | None = None
group: GroupCall | None = None
chat = Brain("chat")
group_chats: dict[int, Brain] = {}   # group id -> its brain for messages that mention Jarvis
group_history: dict[int, deque] = {}  # group id -> recent "Name: text" lines, so Jarvis follows the whole chat


async def start_call(user_id: int, reason: str = "", guest: str = "", message: str = "",
                     incoming: bool = False):
    global call
    if user_id == OWNER_ID and not incoming and not CALL_SANJAR:
        log.info("not calling Sanjar (he asked for text only): %s", reason[:120])
        return
    if call:
        return
    call = Call(user_id, guest, message)
    try:
        await call.start(reason)
    except Exception:
        await finish_call()
        raise


last_auto_call = 0.0


def quiet_now() -> bool:
    start, end = QUIET_HOURS
    hour = time.localtime().tm_hour
    return hour >= start or hour < end


async def auto_call(reason: str) -> bool:
    """Jarvis calls me by itself, unless I'm already on a call, it's quiet hours, or it just called."""
    global last_auto_call
    if not CALL_SANJAR:
        return False   # text only; the Telegram message was already sent
    if call or quiet_now() or time.monotonic() - last_auto_call < CALL_COOLDOWN:
        return False
    last_auto_call = time.monotonic()
    log.info("calling Sanjar: %s", reason[:120])
    try:
        await start_call(OWNER_ID, reason)
        return True
    except Exception as e:  # not answered / busy: the Telegram message is still there
        log.warning("auto call didn't connect: %r", e)
        return False


async def daily_at(hhmm, job):
    """Run `job()` once a day at hh:mm (Mac local time). Fires at most once per calendar day and
    ignores an early wake-up, so the same minute can't trigger two messages."""
    h, m = hhmm
    last_fired = ""
    while True:
        now = time.time()
        t = time.localtime(now)
        target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))
        if target <= now:
            target += 24 * 3600
        await asyncio.sleep(max(1.0, target - now))
        day = time.strftime("%Y-%m-%d", time.localtime(target))
        if time.time() < target - 1 or last_fired == day:
            continue                       # woke early, or already fired for this day
        last_fired = day
        with contextlib.suppress(Exception):
            await job()


async def daily_summary_text():
    reply = await chat.reply("[DAILY SUMMARY] Write Sanjar a short summary of today for Telegram: use "
                             "get_status for all projects; what got done, what's still running, and "
                             "anything that needs him. A few lines.")
    if reply.strip():
        await tgfmt.send(tg, OWNER_ID, tgfmt.daily(reply), link_preview=False)
        app_push(reply, report={"about": ["daily summary"], "read_at": time.time()})   # 0a97efa8


async def daily_call():
    """Once a day at DAILY_CALL_AT: a summary of what Jarvis did and what needs me (a call only if
    calling Sanjar is allowed; otherwise a Telegram message)."""
    await daily_at(DAILY_CALL_AT, lambda: auto_call(
        "[DAILY SUMMARY] Give Sanjar a short spoken summary of today: use get_status for all "
        "projects; say what got done, what's still running, and anything that needs him. "
        "Keep it under a minute.") if CALL_SANJAR else daily_summary_text())


async def daily_reminder():
    """Once a day at SOFT_SKILLS_REMINDER_AT: a Telegram nudge to continue soft skills; he calls."""
    await daily_at(SOFT_SKILLS_REMINDER_AT, lambda: tg.send_message(
        OWNER_ID, "🎓 Soft skills time, sir. Shall we continue where we left off? "
        "Call me when you're ready."))


waiting_calls: list[tuple[int, str, str]] = []   # (user id, name, message) to call after this call


async def find_people(query: str) -> tuple[list, list]:
    """(sure, maybe) matches for a name among the Jarvis account's contacts and recent chats."""
    if query.strip().startswith("@"):
        with contextlib.suppress(ValueError):
            return [await tg.get_entity(query.strip())], []
    users = list((await tg(GetContactsRequest(hash=0))).users)
    seen = {u.id for u in users}
    async for d in tg.iter_dialogs(limit=200):
        if d.is_user and not d.entity.bot and d.entity.id not in seen and d.entity.id != OWNER_ID:
            users.append(d.entity)
    return contacts.match(users, query)


async def call_contact(query: str, message: str) -> str:
    """The call_contact tool: find who I mean and call them now, or right after my call ends."""
    sure, maybe = await find_people(query)
    if len(sure) != 1:
        found = sure or maybe
        if not found:
            return (f"No contact or chat matches {query!r}. Ask Sanjar for the name as saved, or an "
                    "@username; the person must be in the Jarvis account's contacts or chats.")
        names = "".join(f"\n- {contacts.name_of(u)}" + (f" (@{u.username})" if u.username else "") for u in found[:10])
        return f"Not sure who {query!r} is ({len(found)} {'matches' if sure else 'maybe'}); ask Sanjar:{names}"
    person = sure[0]
    if person.id == OWNER_ID and not CALL_SANJAR:
        return "Sanjar asked never to be phoned; tell him in text instead."
    name = contacts.name_of(person)
    if call:
        waiting_calls.append((person.id, name, message))
        return f"Will call {name} as soon as this call ends."
    asyncio.create_task(call_someone(person.id, name, message))
    return f"Calling {name} now; Sanjar gets the transcript in Telegram afterwards."


async def call_someone(user_id: int, name: str, message: str):
    if call:
        waiting_calls.append((user_id, name, message))
        return
    log.info("calling %s for Sanjar: %s", name, message[:120])
    try:
        await start_call(user_id, guest=name, message=message)
    except Exception as e:  # declined, not answered, privacy settings
        log.warning("call to %s didn't connect: %r", name, e)
        await tg.send_message(OWNER_ID, f"📞 Couldn't reach {name}: {e!r}")


async def hang_up():
    if call is None or call.ended:
        return
    await call.mouth.wait_done(timeout=6)
    if isinstance(call, AppCall):
        with contextlib.suppress(Exception):
            await call.ws.close()
    else:
        with contextlib.suppress(NotInCallError):
            await calls.leave_call(call.user_id)
    await finish_call()


async def finish_call():
    global call
    if call is None:
        return
    done, call = call, None
    await done.close()
    log.info("call finished")
    if done.transcript:  # kept on disk; sent only when I ask with /transcript (a guest call: always)
        CALLS.mkdir(exist_ok=True)
        suffix = f"_{done.guest}" if done.guest else ""
        saved = CALLS / f"{time.strftime('%Y-%m-%d_%H-%M')}{suffix}.txt"
        saved.write_text("\n".join(done.transcript))
        if done.guest:   # a call to someone else: its brain doesn't log turns, so index the transcript
            asyncio.create_task(memory.index_file(saved))
        else:            # Sanjar's own call: every turn is already in memory
            memory.mark_indexed(saved.name)
    if done.guest:
        text = "\n".join(done.transcript) or "(they didn't say anything)"
        await tg.send_message(OWNER_ID, f"📞 Call with {done.guest}\n\n{text[-3800:]}")
    if waiting_calls:
        asyncio.create_task(call_someone(*waiting_calls.pop(0)))


class AppCall(Call):
    """A call from the Jarvis Android app: the same ears, brain and mouth as a phone call, with the
    audio over a WebSocket. In: 48 kHz mono s16 from the phone's mic. Out: 24 kHz mono s16 in 20 ms
    frames (Jarvis's 48 kHz stereo voice, every 4th sample). Transcript lines go out as text."""

    def __init__(self, ws, chat: dict):
        super().__init__(0)
        self.ws, self.chat = ws, chat
        self.silence = SilenceWatch()

    def hear(self, pcm: bytes):
        """Also notices when the phone sends only silence: Android gives an app pure zeros from the mic once its
        screen turns off or it leaves the foreground, and Sanjar talked for two minutes unheard (13 Nov, 11ff3b8a)."""
        super().hear(pcm)
        if self.silence.heard(pcm, time.monotonic()):
            log.warning("app call: the phone sends only silence (screen off or app in the background?)")
            asyncio.create_task(self._send_text(json.dumps({"who": "Jarvis", "text": SilenceWatch.TOLD})))

    async def start(self, reason: str = ""):
        self.tasks = [asyncio.create_task(self.brain.start()), asyncio.create_task(self._think()),
                      asyncio.create_task(self._play())]
        self.ears.start()
        if self.chat.get("project", appserver.GENERAL) != appserver.GENERAL:
            self.brain.notes.append(appserver.context_line(self.chat["project"]) + " (a call from the app)")
        self.mouth.say("Hi Sanjar, Jarvis here.")
        log.info("on an app call (%s)", self.chat.get("project"))

    async def _play(self):
        frame, next_at = CHUNK * 2, time.monotonic()          # 20 ms of 48 kHz stereo
        while not self.ended:
            self.interrupt.tick()
            out = self.mouth.out
            if len(out) >= frame and not self.mouth.paused:
                chunk = bytes(out[:frame])
                del out[:frame]
                with contextlib.suppress(Exception):
                    await self.ws.send_bytes(np.frombuffer(chunk, dtype=np.int16)[::4].tobytes())
            next_at += 0.02
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    def _note(self, who: str, text: str):
        super()._note(who, text)
        line = json.dumps({"who": who, "text": " ".join(text.split())}, ensure_ascii=False)
        asyncio.create_task(self._send_text(line))

    async def _send_text(self, line: str):
        with contextlib.suppress(Exception):
            await self.ws.send_text(line)


async def app_call(ws, chat: dict):
    """The app server hands a call WebSocket over: run it until either side hangs up."""
    global call
    if call is not None:
        await ws.send_text(json.dumps({"who": "Jarvis", "text": "I'm already on another call."}))
        await ws.close(code=4409)
        return
    call = this = AppCall(ws, chat)
    await this.start()
    try:
        while not this.ended:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                this.hear(msg["bytes"])
    finally:
        if call is this:
            await finish_call()


async def join_group(chat_id: int, title: str):
    global group
    if group:
        log.info("already in a group call, not joining %s", title)
        return
    group = GroupCall(chat_id, title)
    try:
        await group.start()
    except Exception as e:
        log.warning("couldn't join the group call of %s: %r", title, e)
        failed, group = group, None
        await failed.close()
        await tg.send_message(OWNER_ID, f"🎧 Couldn't join the voice chat in {title}: {e!r}", silent=True)
        return
    await tg.send_message(OWNER_ID, f"🎧 Joined the voice chat in {title}, taking notes.", silent=True)


async def finish_group():
    """Leave, save the transcript (later calls remember it), and send me a summary to pick tickets from."""
    global group
    if group is None:
        return
    done, group = group, None
    await done.close()
    with contextlib.suppress(Exception):
        await calls.leave_call(done.user_id)
    log.info("left the group call of %s", done.title)
    if not done.transcript:
        return
    CALLS.mkdir(exist_ok=True)
    path = CALLS / f"{time.strftime('%Y-%m-%d_%H-%M')}_group_{done.title.replace('/', '-')}.txt"
    path.write_text("\n".join(done.transcript))
    asyncio.create_task(memory.index_file(path))
    summarizer = Brain("group", SUMMARY_PROMPT)
    try:
        summary = await summarizer.reply(f"Group voice chat in {done.title}:\n\n" + "\n".join(done.transcript))
    finally:
        await summarizer.stop()
    await tg.send_message(OWNER_ID, f"🎧 Voice chat in {done.title}\n\n{summary[:3800]}\n\n"
                                    "Tell me which ones to make tickets (full transcript: /transcript).")
    chat.notes.append(f"[GROUP CALL] You sat in on the voice chat in {done.title} and sent Sanjar this "
                      f"summary in Telegram; if he asks for tickets from it (\"add 1 and 3\"), add_task "
                      f"each with what was said. Full transcript: {path}\n\n{summary}")


async def send_photo(path: Path, caption: str):
    await tg.send_file(OWNER_ID, str(path), caption=caption[:1000])


async def send_photos(paths: list[Path], caption: str):
    """Send several images as one Telegram album (gallery) instead of one message each. One image
    goes as a normal photo; albums cap at 10, so more are sent in groups of 10 (caption on the first)."""
    paths = [p for p in paths if p.exists()]
    if not paths:
        return
    if len(paths) == 1:
        await send_photo(paths[0], caption)
        return
    for i in range(0, len(paths), 10):
        chunk = [str(p) for p in paths[i:i + 10]]
        await tg.send_file(OWNER_ID, chunk, caption=caption[:1000] if i == 0 else "")


REPORT_PROMPT = """[WORKER REPORT — from the runner, not from Sanjar; he does NOT see it]
{report}

Sanjar asked that worker and runner messages never go to him directly (2026-11-04): you read them and
tell him only what really matters to him or needs him. Handle what you can first: answer_task for a
question you can answer yourself, queue_ticket for a failure with a passing cause (a restart, a usage or
rate limit, the network). Then, only if he should know (a result he asked for and is waiting on,
something only he can do, a failure that blocks him), reply with 1-2 plain lines in your own words: no
ticket ids, no worker text, no lists. Otherwise reply exactly NO_REPLY."""


def app_push(text: str, chat_id: str = "", photos=(), report: dict | None = None):
    """What Jarvis tells Sanjar on its own also goes to the phone app (its notifications, ticket e028892d);
    chat_id: into that app chat too. report: it's a report on something (an older one then shows as old data)."""
    try:
        appserver.push(text, chat_id, photos, report=report)
    except Exception:
        log.exception("app push failed")


MUST_TELL = """

He IS waiting on this one (he asked for it himself, or only he can unblock it), so you MUST tell him
now: 1-2 plain lines with what happened, why, and the fix or what you are doing about it (or the one
thing he has to do). NO_REPLY is not allowed here."""


async def relay(report: str, photos: list[Path] = (), label: str = "", must: bool = False, app_chat: str = "",
                project_chat: str = ""):
    """A worker/runner message goes to Jarvis's brain, never straight to Sanjar; he gets 1-2 plain lines
    only when it matters to him (and the screenshots with them). must=True: something he's waiting on
    failed or needs him: he is told, enforced here in code even if the brain says NO_REPLY (ticket 35ec396e).
    app_chat: he asked for it in that Jarvis app chat, so the answer goes there, not to Telegram (ticket
    799dda33), screenshots included (ticket 546d965e). project_chat: asked on Telegram, so it goes there,
    and the app's copy goes into that project's chat instead of only Updates (ticket f4f11c38)."""
    if app_chat and not appserver.load_chat(app_chat):
        app_chat = ""                          # that chat is gone: Telegram as before
    fallback = report.splitlines()[0][:200] + "\n" + "\n".join(report.splitlines()[1:3])[:300]
    try:
        reply = (await chat.reply(REPORT_PROMPT.format(report=report[:4000]) + (MUST_TELL if must else ""))).strip()
    except Exception:
        log.exception("relaying a worker report failed")
        reply = fallback if must else ""
    if must and (not reply or is_silent(reply)):
        log.warning("brain stayed silent on a report Sanjar is waiting on; sending it anyway")
        reply = fallback
    if reply and not is_silent(reply) and app_chat:
        app_push(reply, app_chat, list(photos)[:10])
    elif reply and not is_silent(reply):
        await tgfmt.send(tg, OWNER_ID, tgfmt.md(reply), link_preview=False)
        app_push(reply, project_chat, list(photos)[:10])   # the app shows it too, in the project's chat
        if photos:
            await send_photos(list(photos)[:10], label)
    else:
        log.info("worker report kept from Sanjar: %s", report.splitlines()[0][:120])


async def notify(card: Ticket, kind: str, text: str, photos: list[Path]):
    """Runner result: to Jarvis on the call if I'm on one, otherwise to Jarvis's chat brain, which tells
    me only what matters (Sanjar, 2026-11-04: workers never message me directly)."""
    update = f"[UPDATE] ticket {card.ref} \"{card.title}\" ({card.project}) is {kind.replace('_', ' ')}: {text[:800]}"
    if call and not call.guest:
        call.tell(update)
        return
    # enforced in code: a question that reached this point needs him (Jarvis's answerer couldn't take
    # it), and a failure of something he asked for himself is never kept from him
    must = kind == "needs_me" or (kind == "failed" and runner.from_sanjar(card.ref))
    # it goes to that project's chat in the app, even if he asked in General (ticket f4f11c38); asked on
    # Telegram, it still goes to Telegram too
    asked = origin.get(card.ref).get("chat", "")
    asked = "" if asked == appserver.UPDATES else asked      # Updates is the Telegram conversation
    try:
        where = appserver.project_chat(card.project, asked)
    except Exception:
        log.exception("finding %s's chat failed", card.project)
        where = asked
    asyncio.create_task(relay(f"Ticket {card.ref} [{card.project}] \"{card.title}\" is {kind.replace('_', ' ')}.\n"
                              f"{text}", photos, f"{card.title[:60]}", must=must,
                              app_chat=where if asked else "", project_chat="" if asked else where))


async def guard_alert(text: str):
    await tg.send_message(OWNER_ID, text)
    app_push(text)


async def guard_photos(paths: list[Path], caption: str):
    await send_photos(paths, caption)
    app_push(caption, "", paths)


async def guard_command(arg: str) -> str:
    """/guard, /guard off, /guard status, /guard test."""
    if arg == "off":
        return tools.hooks.guard.disarm()
    if arg == "status":
        return tools.hooks.guard.status()
    if arg == "test":
        return await tools.hooks.guard.test()
    return tools.hooks.guard.arm()


async def check_mac_permissions():
    """At start: can the Mac worker see the screen and use the mouse and keyboard? Asking also
    puts Jarvis in the lists in System Settings, so I only have to switch it on."""
    proc = await asyncio.create_subprocess_exec(str(HERE / ".venv/bin/python"), str(HERE / "scripts/mac.py"),
                                                "check", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out = (await proc.communicate())[0].decode(errors="replace").strip()
    if proc.returncode:
        log.warning("mac permissions missing: %s", out)
        await tg.send_message(OWNER_ID, f"🖥 For full Mac control I still need permissions:\n\n{out}")


async def mac_late(task: str, text: str, photos: list[Path], question: bool, app_chat: str = ""):
    """A Mac task that outlasted mac_task's wait: same path as a ticket result (back to the app chat
    it was asked from, if any)."""
    update = f"[UPDATE] Mac task \"{task}\" {'needs his decision' if question else 'finished'}: {text[:800]}"
    if call and not call.guest:
        call.tell(update)
        return
    # Mac tasks are always his own requests: a question or a failure always reaches him
    asyncio.create_task(relay(f"Mac task \"{task}\" {'needs a decision' if question else 'finished'}.\n{text}",
                              photos, "Mac", must=question or text.startswith("Failed"), app_chat=app_chat))


# --- Telegram ------------------------------------------------------------------------------

def _has_file(message) -> bool:
    """A real attachment to save (document, photo, video, ...) — not a voice note, which is
    transcribed separately."""
    return bool(message.document or message.photo) and not (
        message.voice or message.video_note or message.audio)


async def save_incoming_file(message) -> str:
    """Save an attachment Sanjar sent to var/state/inbox and return a one-line note for the brain
    with the file's name, absolute path and type, so it can read it. Empty string if none/failed."""
    if not _has_file(message):
        return ""
    f = message.file
    ext = (f.ext or "").lstrip(".") if f else ""
    name = (f.name if f and f.name else f"{message.id}" + (f".{ext}" if ext else "")) or str(message.id)
    path = INBOX / f"{message.id}_{Path(name).name}"
    try:
        await tg.download_media(message, file=str(path))
    except Exception:
        log.warning("couldn't save an incoming file", exc_info=True)
        return ""
    mime = (f.mime_type if f else "") or "unknown"
    size = path.stat().st_size if path.exists() else 0
    return (f"[Sanjar sent a file, saved on the Mac: name={Path(name).name}, path={path}, "
            f"type={mime}, size={size // 1024 or 1} KB. Read it with your file tools to answer.]")


async def reply_context(event) -> str:
    """If this message is a reply, a short block with the quoted message so the brain can see what
    Sanjar is pointing at ("make a ticket about this", "what does this mean"). Voice notes in the
    quoted message are transcribed. Empty string if the message isn't a reply."""
    if not event.is_reply:
        return ""
    try:
        r = await event.get_reply_message()
    except Exception:
        log.warning("couldn't fetch the replied-to message", exc_info=True)
        return ""
    if r is None:
        return ""
    who = "you (Jarvis)" if r.out else "Sanjar" if r.sender_id == OWNER_ID else "someone"
    body = (r.raw_text or "").strip()
    if not body and (r.voice or r.video_note or r.audio):
        try:
            body = await transcribe_audio(await r.download_media(file=bytes))
            body = f"(voice) {body}" if body else ""
        except Exception:
            body = ""
    if _has_file(r):
        saved = await save_incoming_file(r)
        body = f"{body}\n{saved}".strip() if saved else (body or "[a file]")
    if not body:
        body = "[no text]"
    return f"[Replying to this earlier message from {who}:\n{body[:3000]}\n]\n\n"


JOBS_DIR = Path.home() / "dev" / "upwork_job"     # the Upwork job worker (tgjobs) and its own login
JOB_PHONE = "+79062609156"                          # its second Telegram account (Sanjar, 2026-10-28)
UV = "/opt/homebrew/bin/uv"


async def tgjobs(*args: str, detach: bool = False) -> str:
    """Run the job worker's own login commands. Its own process and its own session file for the
    +7 906 account: never Jarvis's session, so nothing here can freeze Jarvis."""
    cmd = [UV, "run", "tgjobs/tgjobs.py", "login", *args]
    if detach:          # the QR login waits for a scan for minutes; it reports through the hunter bot
        out = open(JOBS_DIR / ".captures" / "tgjobs-qr.out", "a")
        await asyncio.create_subprocess_exec(*cmd, cwd=JOBS_DIR, stdout=out, stderr=out,
                                             stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
        return ""
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=JOBS_DIR, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 120)
    except asyncio.TimeoutError:
        proc.kill()
        return "it took too long, sir; nothing finished."
    lines = [l for l in out.decode(errors="replace").splitlines() if l.strip() and not l.startswith(("INFO", "WARNING"))]
    return (lines[-1] if lines else "(no output)")[:500]


FORMAT_SAMPLES = VAR / "state" / "send_format_samples"   # flag: send them once at the next start


async def send_format_samples():
    """One sample of each message type in the HTML style (ticket 434209fd), once."""
    if not FORMAT_SAMPLES.exists():
        return
    FORMAT_SAMPLES.unlink()
    from types import SimpleNamespace as NS
    card = NS(ref="aet-4", project="AI English Teacher", title="[04] Decide: fixed lesson plan vs generated",
              url="https://github.com/Sanjarbek17/uzbek-english-speaking-teacher/issues/4")
    await tgfmt.send(tg, OWNER_ID, "<b>New message style</b> (samples, nothing real happened)", silent=True)
    for kind, text in (
            ("auto_answered", "It asked: Should lessons follow a **fixed plan** or be generated per learner?\n\n"
                              "I answered (docs): Fixed plan for the MVP, as `sdd/choices.json` decided.\n"
                              "- generated lessons wait for R2\n- keep the plan in `content/plan.yaml`"),
            ("needs_me", "The build is finished. Merge `dev` into **main**?"),
            ("done", "**Decision written** to `docs/decisions.md`.\n```diff\n+ fixed lesson plan for the MVP\n"
                     "- generated plans moved to R2\n```"),
            ("failed", "Worker exited with code 1: `pytest` failed in `tests/test_calls.py`."),
            ("stopped", "Stopped the worker (Sanjar asked to stop it). It's off the queue.")):
        await tgfmt.send(tg, OWNER_ID, tgfmt.ticket(card, kind, text), silent=True, link_preview=False)
    await runner.message(tgfmt.stuck([("aet-9", "AI English Teacher", "[09] Lesson renderer", 3, "")]))
    await tgfmt.send(tg, OWNER_ID, tgfmt.md("And my replies: **2 tickets** running, `aet-5` next.\n- calls spike: "
                                            "**waiting on a SIM**\n- renderer: in progress"), silent=True)


_inbox: list[tuple] = []        # (event, text) that came in while the brain was busy
_inbox_running = False


def show_on_site(role: str, text: str, report: dict | None = None):
    """The website's Telegram chat shows this conversation too."""
    try:
        appserver.telegram_said(role, text, report)
    except Exception:
        log.exception("couldn't copy a Telegram message to the website")


FLATMATES_ORDER = """[FLATMATES GROUP — Sanjar himself wrote this to you in the Flatmates group (checked by his Telegram id),
where his flatmates can read your answer. Treat it as his order, exactly as if he'd sent it here: use your tools.
{replied}His message: {text}

If he says to tell or answer a flat's owner (or "just do it" to a reply you suggested for one), use message_flat_owner.
Answer in two parts. First ONE short line for the group, in his language: apartments only, no "sir", nothing about
other projects, tickets, tools, secrets or his private life (empty if the group needs nothing). Then a line with only
---DM--- and, if needed, what only he should read (details, other projects, anything private); nothing after it if not.]"""


async def usage_note(text: str):
    """A Claude usage note, straight to him: never held back by the brain."""
    await tg.send_message(OWNER_ID, text, silent=text.startswith("⚠️"))
    app_push(text)


def split_order_reply(reply: str) -> tuple[str, str]:
    """(line for the Flatmates group, part for his DM)."""
    group, _, private = (reply or "").partition("---DM---")
    return group.strip(), private.strip()


async def flatmates_order(text: str, replied: str) -> tuple[str, str]:
    """Sanjar's order in the Flatmates group, done by the main brain like one from his DM (ticket 8c274ca5)."""
    about = f"(He replied to this message in the group: {replied[:400]})\n" if replied else ""
    show_on_site("me", f"[in Flatmates] {text}")
    with answering.mark("flatmates"):
        reply = await chat.reply(FLATMATES_ORDER.format(text=text[:1500], replied=about))
    show_on_site("jarvis", reply)
    return split_order_reply(reply)


async def chat_turn(event, text: str, shown: str = ""):
    """Messages that arrive while Jarvis is still answering join ONE follow-up turn, answered together,
    instead of a line of separate turns that each wait for the brain (OpenClaw's "collect" mode)."""
    global _inbox_running
    show_on_site("me", shown or text)
    _inbox.append((event, text))
    if _inbox_running:
        return                       # the loop below picks it up with the next turn
    _inbox_running = True
    try:
        while _inbox:
            batch = _inbox[:]
            _inbox.clear()
            msg = batch[0][1] if len(batch) == 1 else (
                "[He sent these one after another while you were busy; answer them together, in order]\n\n"
                + "\n\n---\n\n".join(t for _, t in batch))
            async with tg.action(OWNER_ID, "typing"):
                # a status question about the Instagram projects: live numbers first, in code (ticket 0960b065)
                live, report = await livestats.live_for("", "\n".join(t for _, t in batch))
                with answering.mark("telegram"):            # a restart waits for this answer (11ff3b8a)
                    reply = await chat.reply(live + msg)
            await tgfmt.reply(batch[-1][0], tgfmt.md(reply) if reply else "(no reply)")
            show_on_site("jarvis", reply, report)
    finally:
        _inbox_running = False


async def remember_history():
    """At start: scrub secrets out of stored memory, index old call transcripts, and import our
    Telegram chat (all of it the first time, then only newer messages) through this connection."""
    memory.scrub_existing()
    await memory.backfill_calls()
    # anything said since the last core-memory pass (e.g. right before this restart) gets saved now
    with contextlib.suppress(Exception):
        await coremem.consolidate()
    first = not memory.TG_STATE.exists()
    try:
        added = await memory.backfill_telegram(tg, OWNER_ID)
    except Exception:
        log.exception("chat history backfill failed")
        return
    if first and added:
        await tg.send_message(OWNER_ID, f"🧠 I read through our whole chat here and put {added} exchanges "
                                        "into my memory, sorted into topics. Passwords and keys were left out.")


@tg.on(events.NewMessage(incoming=True, from_users=OWNER_ID, func=lambda e: e.is_private))
async def on_message(event):
    text = (event.raw_text or "").strip()
    if not text and (event.message.voice or event.message.video_note or event.message.audio):
        async with tg.action(OWNER_ID, "typing"):
            text = await transcribe_audio(await tg.download_media(event.message, file=bytes))
        if not text:
            await event.reply("I couldn't make out that voice message, sir.")
            return
        await event.reply(f"🎙 I heard: {text}")
    file_note = await save_incoming_file(event.message)
    if not text and not file_note:
        return
    if file_note:
        show_on_site("me", text or "Sent a file on Telegram")
        ctx = await reply_context(event)
        async with tg.action(OWNER_ID, "typing"):
            reply = await chat.reply("\n".join(p for p in (file_note, ctx + text) if p.strip()))
        await tgfmt.reply(event, tgfmt.md(reply) if reply else "Saved it, sir.")
        show_on_site("jarvis", reply)
        return
    if text.startswith("/status"):
        project = text.removeprefix("/status").strip()
        await event.reply(await board.status_text(project) + f"\n\n{PROJECTS_URL}", link_preview=False)
    elif text.startswith("/stop"):
        project = text.removeprefix("/stop").strip()
        if project.lower() == "mac":
            await event.reply("Stopped the Mac task." if mac.stop() else "No Mac task is running.")
            return
        await event.reply(f"Stopped {project}." if runner.stop_project(project)
                          else f"Nothing running for {project!r}. Projects: {', '.join(PROJECTS)}")
    elif text.startswith("/guard") or text.startswith("/unguard"):
        arg = "off" if text.startswith("/unguard") else text.removeprefix("/guard").strip().lower()
        await event.reply(await guard_command(arg))
    elif text.startswith("/logoutall"):
        count = appserver.end_all_sessions()         # kill switch for app, website and Mac control (ticket 436ccc83)
        await event.reply(f"Done, sir: {count} login{'s' * (count != 1)} ended. The app and website need a new code from here.")
    elif text.startswith("/transcript"):
        files = sorted(CALLS.glob("*.txt")) if CALLS.exists() else []
        await event.reply(f"📞 {files[-1].stem}\n\n" + files[-1].read_text()[-3800:] if files else "No calls yet.")
    elif text.startswith("/call"):
        if not CALL_SANJAR:
            await event.reply("I don't phone you any more, sir, as you asked. Call me instead and I'll pick up.")
            return
        await event.reply("Calling you.")
        await start_call(OWNER_ID)
    elif text.startswith("/2fa"):
        # the 2FA password for a waiting Telegram re-login (scripts/qr_login.py). Never goes to the
        # brain or memory; Sanjar's message with it is deleted right away.
        with contextlib.suppress(Exception):
            await event.delete()
        pw = text.removeprefix("/2fa").strip()
        if not outbox.WANT_2FA.exists():
            await event.respond("No login is waiting for a 2FA password, sir, so I didn't keep it.")
            return
        if not pw:
            await event.respond("Send it as /2fa <password>, sir.")
            return
        outbox.SECRET_2FA.touch(mode=0o600)
        outbox.SECRET_2FA.chmod(0o600)
        outbox.SECRET_2FA.write_text(pw)
        await event.respond("🔑 Got the password (your message is deleted). Finishing the login…")
    elif text.startswith("/joblogin"):
        # log in the job account (+7 906) for the Upwork messaging worker: QR by default, or a code
        if text.removeprefix("/joblogin").strip().lower() == "code":
            out = await tgjobs("send", JOB_PHONE)
            await event.reply(f"🔐 {out}\nWhen the code arrives in the +7 906 account (the blue-tick \"Telegram\" "
                              "chat), send it as /jobcode 1-2-3-4-5, with dashes.")
        else:
            state = await tgjobs("check")
            if state.startswith("Logged in"):
                await event.reply(f"🔐 The job account is already logged in: {state}")
                return
            await tgjobs("qr", "15", detach=True)
            await event.reply("🔐 The QR for the +7 906 job account is coming in the hunter bot chat (new one every "
                              "~30 s, for 15 minutes). Open that chat on any computer or web.telegram.org, then on your "
                              "phone switch to the +7 906 account: Settings → Devices → Link Desktop Device → scan it. "
                              "Or: /joblogin code.")
    elif text.startswith("/jobcode"):
        with contextlib.suppress(Exception):
            await event.delete()            # a login code shouldn't stay in a chat
        code = re.sub(r"\D", "", text.removeprefix("/jobcode"))
        if not code:
            await event.respond("Send it as /jobcode 1-2-3-4-5, sir.")
            return
        await event.respond(f"🔐 {await tgjobs('code', code)}")
    elif text.startswith("/clear"):
        # save before forgetting: fold this conversation into core memory first (ticket 35ec396e)
        try:
            n_set, n_rm = await asyncio.wait_for(coremem.consolidate(), 300)
            saved = f" Saved {n_set} thing(s) to core memory first." if n_set else ""
        except Exception:
            log.exception("core memory before /clear failed")
            saved = " (Couldn't update core memory first; the chat log still has everything.)"
        await chat.clear()
        await event.reply("Cleared." + saved)
    else:
        await chat_turn(event, await reply_context(event) + text, shown=text)


@tg.on(events.NewMessage(incoming=True, func=lambda e: e.is_private and e.sender_id != OWNER_ID))
async def on_member_private(event):
    """Only a Flatmates member's reply to Jarvis's private question about the flats (ticket 521b9e14); every other
    private message from someone else is ignored, as before."""
    with contextlib.suppress(Exception):
        await flatmates.on_private(event)


@tg.on(events.NewMessage(incoming=True, func=lambda e: e.is_group and bool((e.raw_text or "").strip())))
async def on_group_message(event):
    """Follow the whole group conversation (read-only). Jarvis keeps the recent messages so it has
    the full thread, but only speaks up when it's addressed by name or a meeting is being arranged
    (e.g. "Jarvis, schedule it"). It answers in the language the group is using.
    Only the watched group ("Taxi loyiha") and Flatmates, which has its own rules; every other group is ignored."""
    title = getattr(await event.get_chat(), "title", "") or ""
    in_flatmates = flatmates.is_flatmates(event.chat_id)
    if not (in_flatmates or watched_group(event.chat_id, title)):
        return
    text = (event.raw_text or "").strip()
    sender = await event.get_sender()
    name = "Sanjar" if event.sender_id == OWNER_ID else (
        contacts.name_of(sender) if hasattr(sender, "first_name") else getattr(sender, "title", "someone"))
    if in_flatmates:                 # apartments only, and it answers without being named
        await flatmates.on_message(event, name)
        return
    hist = group_history.setdefault(event.chat_id, deque(maxlen=40))
    hist.append(f"{name}: {text}")

    meeting = bool(MEETING.search(text))
    if not (WAKE.search(text) or meeting):
        return   # just listening; keep the context, say nothing
    title = title or str(event.chat_id)
    if event.chat_id not in group_chats:
        group_chats[event.chat_id] = Brain("group", group_prompt(title, voice=False))
    ctx = await reply_context(event)
    convo = "\n".join(hist)
    msg = (f"{ctx}Recent conversation in the group (most recent last):\n{convo}\n\n"
           f"The latest message is from {name}. Reply only if you were addressed by name, or if the "
           f"group is asking you to arrange, schedule, or note a meeting; otherwise answer exactly NO_REPLY.")
    async with tg.action(event.chat_id, "typing"):
        reply = await group_chats[event.chat_id].reply(msg)
    if not is_silent(reply):
        await event.reply(reply)
        # a meeting was arranged in a group: make sure Sanjar actually has it
        if meeting and event.sender_id != OWNER_ID:
            with contextlib.suppress(Exception):
                await tg.send_message(OWNER_ID, f"📅 Possible meeting in \"{title}\":\n{reply}", silent=True)


# --- calls ---------------------------------------------------------------------------------

@calls.on_update(filters.stream_frame(Direction.INCOMING))
async def on_audio(_, update: StreamFrames):
    if call and not call.ended and update.chat_id == call.user_id:
        for frame in update.frames:
            call.hear(frame.frame)
    elif group and not group.ended and update.chat_id == group.user_id:
        for frame in update.frames:
            group.hear(frame.ssrc, frame.frame)


@calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
async def on_incoming_call(_, update: ChatUpdate):
    if update.chat_id != OWNER_ID or call is not None:
        log.info("ignoring call from %s", update.chat_id)
        return
    log.info("Sanjar is calling, picking up")
    await start_call(update.chat_id, incoming=True)


@calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
async def on_left(_, update: ChatUpdate):
    if call and update.chat_id == call.user_id:
        await finish_call()
    elif group and update.chat_id == group.user_id:
        await finish_group()


joined_calls: set[int] = set()     # voice chats already joined (or tried) once: no automatic rejoin
GROUP_CALL_CHECK = 60              # seconds


@tg.on(events.NewMessage(func=lambda e: isinstance(getattr(e.message, "action", None), MessageActionGroupCall)))
async def on_voice_chat(event):
    """A voice chat started (no duration yet) or ended in a group the Jarvis account is in."""
    if event.message.action.duration is None:
        chat_ = await event.get_chat()
        if not watched_group(event.chat_id, getattr(chat_, "title", "") or ""):
            return
        joined_calls.add(event.message.action.call.id)
        await join_group(event.chat_id, getattr(chat_, "title", "") or str(event.chat_id))
    elif group and event.chat_id == group.user_id:
        await finish_group()


async def live_call_id(chat_id: int) -> int | None:
    """The id of the voice chat going on in that group right now, if any."""
    peer = await tg.get_input_entity(chat_id)
    if isinstance(peer, InputPeerChannel):
        full = await tg(GetFullChannelRequest(peer))
    else:
        full = await tg(GetFullChatRequest(peer.chat_id))
    call = getattr(full.full_chat, "call", None)
    return call.id if call else None


async def watch_group_calls():
    """Join a voice chat in a watched group even when the "started" message was missed (Jarvis was
    restarting or offline then): look at start and every minute. Each call is joined automatically
    only once, so leaving or being removed doesn't make Jarvis rejoin over and over."""
    while True:
        for gid in WATCHED_GROUP_IDS:
            if group:
                break
            try:
                cid = await live_call_id(gid)
                if cid and cid not in joined_calls:
                    joined_calls.add(cid)
                    title = getattr(await tg.get_entity(gid), "title", "") or str(gid)
                    log.info("voice chat %s is live in %s, joining", cid, title)
                    await join_group(gid, title)
            except Exception as e:
                log.warning("couldn't check the voice chat of %s: %r", gid, e)
        await asyncio.sleep(GROUP_CALL_CHECK)


runner = Runner(notify)
mac = MacWorker()


_lock_fd = None


def take_single_instance_lock(timeout: float = 90) -> bool:
    """Hold an exclusive lock so only one Jarvis runs. After a restart the new process waits here
    for the old one to die and release it, so two instances never both answer messages, poll the
    board, or fire the daily timers. Returns False if the old one won't let go."""
    global _lock_fd
    _lock_fd = open(VAR / "jarvis.lock", "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() > deadline:
                return False
            time.sleep(1)


async def main():
    if not take_single_instance_lock():
        log.error("another Jarvis still holds the lock after waiting; exiting so it keeps running")
        return
    log.info("single-instance lock held")
    tools.hooks.send_photo = send_photo
    tools.hooks.send_photos = send_photos
    tools.hooks.hang_up = hang_up
    tools.hooks.call_contact = call_contact
    tools.hooks.worker_progress = runner.progress
    runner.message = lambda text: relay(tgfmt.plain(text))   # e.g. the stuck list: Jarvis decides, not me
    tools.hooks.mac = mac
    tools.hooks.runner = runner
    tools.hooks.mac_late = mac_late
    tools.hooks.expenses = lambda: expenses.text(tg)
    tools.hooks.post_expenses = lambda: expenses.post(tg)
    tools.hooks.guard = guard.Guard(guard_alert, guard_photos, tools.take_screenshot,
                                    # the Mac worker's clicks, or Sanjar driving it from the app (ticket 70e8ad2f)
                                    busy=lambda: bool(tools.hooks.mac.current) or macremote.controlling(),
                                    ours_at=macremote.last_input_at)          # by time too (ticket 52d168c3)
    runner_task = {"t": asyncio.create_task(runner.run_forever(), name="runner")}

    def restart_runner():                 # the self-check found the runner's task dead: start it again
        runner_task["t"] = asyncio.create_task(runner.run_forever(recover=False), name="runner")

    background = [asyncio.create_task(tools.serve()), runner_task["t"],
                  asyncio.create_task(daily_call()), asyncio.create_task(daily_reminder()),
                  asyncio.create_task(daily_at((3, 0), coremem.consolidate)),   # nightly core-memory pass
                  asyncio.create_task(tools.hooks.guard.run_forever()),
                  asyncio.create_task(google.serve_callback()),      # Google sign-ins done on this Mac
                  asyncio.create_task(selfcheck.run_forever(
                      runner, lambda: not runner_task["t"].done(), restart_runner, chat.reply,
                      lambda text: tgfmt.send(tg, OWNER_ID, "🔎 " + tgfmt.md(text), silent=True)))]
    await calls.start()
    me = await tg.get_me()
    log.info("Jarvis is up as @%s", me.username)
    await tg.send_message(OWNER_ID, "🟢 Jarvis is online. Call me, message me, or /status.", silent=True)
    asyncio.create_task(check_mac_permissions())
    asyncio.create_task(remember_history())
    asyncio.create_task(watch_group_calls())
    appserver.start_call = app_call
    appserver.worker_status = runner.snapshot
    appserver.send_code = lambda text: tg.send_message(OWNER_ID, text)   # app login codes (ticket 31ed52a7)
    appserver.main_brain = chat                                          # the website's Telegram chat
    flatmates.sanjar_order = flatmates_order                             # his own orders in the Flatmates group
    usagewatch.sender = usage_note                                       # Claude 5-hour limit notes (ticket e6e75d74)
    appserver.tell_telegram = lambda text: tgfmt.send(tg, OWNER_ID, tgfmt.md(text), link_preview=False)
    asyncio.create_task(appserver.serve())
    asyncio.create_task(send_format_samples())
    asyncio.create_task(outbox.run(tg, OWNER_ID, appserver.push_file))   # scripts send Sanjar things through us
    asyncio.create_task(expenses.send_if_asked(tg, OWNER_ID, app_push))
    asyncio.create_task(flatmates.run(tg, OWNER_ID))                     # found apartments, into the Flatmates group
    if "--call" in sys.argv and CALL_SANJAR:
        try:
            await start_call(OWNER_ID)
        except Exception as e:
            log.exception("call failed")
            await tg.send_message(OWNER_ID, f"Couldn't call you: {e}")
    try:
        await tg.run_until_disconnected()
    finally:
        for t in background:
            t.cancel()


if __name__ == "__main__":
    # Telethon and PyTgCalls bind to the loop that exists at import time
    tg.loop.run_until_complete(main())
