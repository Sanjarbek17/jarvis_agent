"""Jarvis: Telegram calls + messages, Claude brain, Notion board, runner. One process, always on.

    uv run jarvis.py           # start and wait
    uv run jarvis.py --call    # start and call me right away

- Call: my voice -> Gemini transcribe -> Claude (call brain) -> Gemini voice -> call.
- Telegram text or voice message from me -> Claude (chat brain) -> reply. /status, /stop <project|mac>, /call, /transcript.
- Runner picks tickets marked Queued (Jarvis column) on each project's Notion board.
- Jarvis calls me by itself when a ticket needs me or fails, and once a day with a summary
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
import functools
import logging
import re
import sys
import time
from pathlib import Path

from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import NotInCallError
from pytgcalls.types import (CallConfig, ChatUpdate, Device, Direction, ExternalMedia,
                             MediaStream, RecordStream, StreamFrames)
from pytgcalls.types.raw import AudioParameters
from telethon import TelegramClient, events
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import MessageActionGroupCall

from core import board, contacts, tools
from core.board import Ticket
from core.brain import SUMMARY_PROMPT, Brain, group_prompt, guest_prompt
from core.config import (HERE, CALLS, CALL_COOLDOWN, CALL_ON, CALL_RATE, DAILY_CALL_AT, LOGS, OWNER_ID, PROJECTS,
                         PROJECTS_URL, QUIET_HOURS, SESSION, TG_API_HASH, TG_API_ID)
from core.mac import MacWorker
from core.runner import Runner
from core.voice import Ears, Mouth, split_sentences, transcribe_audio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOGS / "jarvis.log")])
for noisy in ("telethon", "google_genai", "httpx", "websockets", "mcp", "uvicorn"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("jarvis")

tg = TelegramClient(str(SESSION), TG_API_ID, TG_API_HASH)
calls = PyTgCalls(tg)
CHUNK = CALL_RATE // 100 * 2 * 2      # 10 ms of 48 kHz stereo s16 going into the call
EMOJI = {"done": "✅", "failed": "❌", "needs_me": "❓"}
WAKE = re.compile(r"jarvis|джарвис|жарвис", re.I)   # someone in a group call speaks to Jarvis


class Call:
    """One phone call: ears + Claude + mouth, and the audio clock. With me, or with `guest`
    (someone Jarvis calls to give them `message` from me)."""

    def __init__(self, user_id: int, guest: str = "", message: str = ""):
        self.user_id = user_id
        self.guest = guest
        self.brain = Brain("guest", guest_prompt(guest, message)) if guest else Brain("call")
        self.mouth = Mouth()
        self.ears = Ears(self.on_speaking, self.on_sentence)
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

    async def on_speaking(self, text: str):
        # the other side talks over Jarvis: stop it. 3+ words, because a phone on speaker sends
        # bits of Jarvis's own voice back ("hour", "Hello") and that kept cutting it off
        if self.mouth.busy and len(text.split()) >= 3:
            self.turn_id += 1
            self.mouth.stop()

    async def on_sentence(self, text: str):
        if self.mouth.busy and len(text.split()) < 3:
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
                        said.append(s)
                        if turn == self.turn_id:   # not interrupted since
                            self.mouth.say(s)
                if buffer.strip():
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
            out = self.mouth.out
            if len(out) >= CHUNK:
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


async def start_call(user_id: int, reason: str = "", guest: str = "", message: str = ""):
    global call
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


async def daily_call():
    """Once a day at DAILY_CALL_AT: a short spoken summary of what Jarvis did and what needs me."""
    while True:
        now = time.time()
        h, m = DAILY_CALL_AT
        t = time.localtime(now)
        target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))
        if target <= now:
            target += 24 * 3600
        await asyncio.sleep(target - now)
        await auto_call("[DAILY SUMMARY] Give Sanjar a short spoken summary of today: use get_status "
                        "for all projects; say what got done, what's still running, and anything "
                        "that needs him. Keep it under a minute.")


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
        (CALLS / f"{time.strftime('%Y-%m-%d_%H-%M')}{suffix}.txt").write_text("\n".join(done.transcript))
    if done.guest:
        text = "\n".join(done.transcript) or "(they didn't say anything)"
        await tg.send_message(OWNER_ID, f"📞 Call with {done.guest}\n\n{text[-3800:]}")
    if waiting_calls:
        asyncio.create_task(call_someone(*waiting_calls.pop(0)))


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


async def notify(card: Ticket, kind: str, text: str, photos: list[Path]):
    """Runner result: tell me on the call if I'm on one, and always leave it in Telegram."""
    head = f"{EMOJI[kind]} {card.ref} [{card.project}] {card.title}"
    body = f"Question: {text}" if kind == "needs_me" else text
    await tg.send_message(OWNER_ID, f"{head}\n\n{body[:3500]}\n\n{card.url}",
                          silent=call is not None, link_preview=False)
    for p in photos[:5]:
        await send_photo(p, f"{card.ref} screenshot")
    update = f"[UPDATE] ticket {card.ref} \"{card.title}\" ({card.project}) is {kind.replace('_', ' ')}: {text[:800]}"
    if call and not call.guest:
        call.tell(update)
    elif not call and kind in CALL_ON:
        asyncio.create_task(auto_call(update))


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


async def mac_late(task: str, text: str, photos: list[Path], question: bool):
    """A Mac task that outlasted mac_task's wait: same path as a ticket result."""
    head = f"{'❓' if question else '❌' if text.startswith('Failed') else '✅'} Mac: {task}"
    await tg.send_message(OWNER_ID, f"{head}\n\n{text[:3500]}", silent=call is not None)
    for p in photos[:5]:
        await send_photo(p, "Mac")
    update = f"[UPDATE] Mac task \"{task}\" {'needs his decision' if question else 'finished'}: {text[:800]}"
    if call and not call.guest:
        call.tell(update)
    elif not call and question:
        asyncio.create_task(auto_call(update))


# --- Telegram ------------------------------------------------------------------------------

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
    if not text:
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
    elif text.startswith("/transcript"):
        files = sorted(CALLS.glob("*.txt")) if CALLS.exists() else []
        await event.reply(f"📞 {files[-1].stem}\n\n" + files[-1].read_text()[-3800:] if files else "No calls yet.")
    elif text.startswith("/call"):
        await event.reply("Calling you.")
        await start_call(OWNER_ID)
    else:
        async with tg.action(OWNER_ID, "typing"):
            reply = await chat.reply(text)
        await event.reply(reply or "(no reply)")


@tg.on(events.NewMessage(incoming=True, func=lambda e: e.is_group and bool(WAKE.search(e.raw_text or ""))))
async def on_group_message(event):
    """Someone in a group says "Jarvis" in a message: answer it there (read-only)."""
    if event.chat_id not in group_chats:
        title = getattr(await event.get_chat(), "title", "") or str(event.chat_id)
        group_chats[event.chat_id] = Brain("group", group_prompt(title, voice=False))
    sender = await event.get_sender()
    name = "Sanjar" if event.sender_id == OWNER_ID else (
        contacts.name_of(sender) if hasattr(sender, "first_name") else getattr(sender, "title", "someone"))
    async with tg.action(event.chat_id, "typing"):
        reply = await group_chats[event.chat_id].reply(f"{name}: {event.raw_text}")
    if reply:
        await event.reply(reply)


# --- calls ---------------------------------------------------------------------------------

@calls.on_update(filters.stream_frame(Direction.INCOMING))
async def on_audio(_, update: StreamFrames):
    if call and not call.ended and update.chat_id == call.user_id:
        for frame in update.frames:
            call.ears.hear(frame.frame)
    elif group and not group.ended and update.chat_id == group.user_id:
        for frame in update.frames:
            group.hear(frame.ssrc, frame.frame)


@calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
async def on_incoming_call(_, update: ChatUpdate):
    if update.chat_id != OWNER_ID or call is not None:
        log.info("ignoring call from %s", update.chat_id)
        return
    log.info("Sanjar is calling, picking up")
    await start_call(update.chat_id)


@calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
async def on_left(_, update: ChatUpdate):
    if call and update.chat_id == call.user_id:
        await finish_call()
    elif group and update.chat_id == group.user_id:
        await finish_group()


@tg.on(events.NewMessage(func=lambda e: isinstance(getattr(e.message, "action", None), MessageActionGroupCall)))
async def on_voice_chat(event):
    """A voice chat started (no duration yet) or ended in a group the Jarvis account is in."""
    if event.message.action.duration is None:
        chat_ = await event.get_chat()
        await join_group(event.chat_id, getattr(chat_, "title", "") or str(event.chat_id))
    elif group and event.chat_id == group.user_id:
        await finish_group()


runner = Runner(notify)
mac = MacWorker()


async def main():
    tools.hooks.send_photo = send_photo
    tools.hooks.hang_up = hang_up
    tools.hooks.call_contact = call_contact
    tools.hooks.worker_progress = runner.progress
    tools.hooks.mac = mac
    tools.hooks.mac_late = mac_late
    background = [asyncio.create_task(tools.serve()), asyncio.create_task(runner.run_forever()),
                  asyncio.create_task(daily_call())]
    await calls.start()
    me = await tg.get_me()
    log.info("Jarvis is up as @%s", me.username)
    await tg.send_message(OWNER_ID, "🟢 Jarvis is online. Call me, message me, or /status.", silent=True)
    asyncio.create_task(check_mac_permissions())
    if "--call" in sys.argv:
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
