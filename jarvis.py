"""Jarvis: Telegram calls + messages, Claude brain, Notion board, runner. One process, always on.

    uv run jarvis.py           # start and wait
    uv run jarvis.py --call    # start and call me right away

- Call: my voice -> Gemini transcribe -> Claude (call brain) -> Gemini voice -> call.
- Telegram text from me -> Claude (chat brain) -> reply. /status, /stop <project>, /call, /transcript.
- Runner picks tickets marked Queued (Jarvis column) on each project's Notion board.
Only OWNER_ID is ever answered.
"""
import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import NotInCallError
from pytgcalls.types import (CallConfig, ChatUpdate, Device, Direction, ExternalMedia,
                             MediaStream, RecordStream, StreamFrames)
from pytgcalls.types.raw import AudioParameters
from telethon import TelegramClient, events

from core import board, tools
from core.board import Ticket
from core.brain import Brain
from core.config import CALL_RATE, LOGS, OWNER_ID, PROJECTS, PROJECTS_URL, SESSION, STATE, TG_API_HASH, TG_API_ID
from core.runner import Runner
from core.voice import Ears, Mouth, split_sentences

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOGS / "jarvis.log")])
for noisy in ("telethon", "google_genai", "httpx", "websockets", "mcp", "uvicorn"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("jarvis")

tg = TelegramClient(str(SESSION), TG_API_ID, TG_API_HASH)
calls = PyTgCalls(tg)
CHUNK = CALL_RATE // 100 * 2 * 2      # 10 ms of 48 kHz stereo s16 going into the call
CALLS = STATE / "calls"                # call transcripts
EMOJI = {"done": "✅", "failed": "❌", "needs_me": "❓"}


class Call:
    """One phone call with me: ears + Claude + mouth, and the audio clock."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.brain = Brain("call")
        self.mouth = Mouth()
        self.ears = Ears(self.on_speaking, self.on_sentence)
        self.turns: asyncio.Queue[str] = asyncio.Queue()
        self.turn_id = 0
        self.transcript: list[str] = []
        self.ended = False
        self.tasks: list[asyncio.Task] = []

    async def start(self):
        # Jarvis's voice goes in as stereo (the library's external-audio example); I come out as mono
        out_params, in_params = AudioParameters(CALL_RATE, 2), AudioParameters(CALL_RATE, 1)
        self.tasks = [asyncio.create_task(self.brain.start()), asyncio.create_task(self._play()),
                      asyncio.create_task(self._think())]
        self.ears.start()
        await calls.play(self.user_id, MediaStream(ExternalMedia.AUDIO, out_params), CallConfig(timeout=60))
        await calls.record(self.user_id, RecordStream(True, in_params))
        self.mouth.say("Hi Sanjar, Jarvis here.")
        log.info("on the call")

    async def on_speaking(self, text: str):
        # I'm talking over Jarvis: stop it (2+ words, so a cough or echo doesn't count)
        if self.mouth.busy and len(text.split()) >= 2:
            self.turn_id += 1
            self.mouth.stop()

    async def on_sentence(self, text: str):
        self._note("Sanjar", text)
        self.turn_id += 1
        self.mouth.stop()
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


call: Call | None = None
chat = Brain("chat")


async def start_call(user_id: int):
    global call
    if call:
        return
    call = Call(user_id)
    try:
        await call.start()
    except Exception:
        await finish_call()
        raise


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
    if done.transcript:  # kept on disk; sent only when I ask with /transcript
        CALLS.mkdir(exist_ok=True)
        (CALLS / f"{time.strftime('%Y-%m-%d_%H-%M')}.txt").write_text("\n".join(done.transcript))


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
    if call:
        call.tell(f"[UPDATE] {card.title} ({card.project}) is {kind.replace('_', ' ')}: {text[:800]}")


# --- Telegram ------------------------------------------------------------------------------

@tg.on(events.NewMessage(incoming=True, from_users=OWNER_ID, func=lambda e: e.is_private))
async def on_message(event):
    text = (event.raw_text or "").strip()
    if not text:
        return
    if text.startswith("/status"):
        project = text.removeprefix("/status").strip()
        await event.reply(await board.status_text(project) + f"\n\n{PROJECTS_URL}", link_preview=False)
    elif text.startswith("/stop"):
        project = text.removeprefix("/stop").strip()
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


# --- calls ---------------------------------------------------------------------------------

@calls.on_update(filters.stream_frame(Direction.INCOMING))
async def on_audio(_, update: StreamFrames):
    if call and not call.ended and update.chat_id == call.user_id:
        for frame in update.frames:
            call.ears.hear(frame.frame)


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


runner = Runner(notify)


async def main():
    tools.hooks.send_photo = send_photo
    tools.hooks.hang_up = hang_up
    background = [asyncio.create_task(tools.serve()), asyncio.create_task(runner.run_forever())]
    await calls.start()
    me = await tg.get_me()
    log.info("Jarvis is up as @%s", me.username)
    await tg.send_message(OWNER_ID, "🟢 Jarvis is online. Call me, message me, or /status.", silent=True)
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
