"""Ears (Gemini live transcription) and mouth (Gemini text-to-speech) for calls.

Audio from the call is 48 kHz mono s16le; audio into the call is 48 kHz stereo.
Gemini hears 16 kHz and speaks 24 kHz.
"""
import asyncio
import contextlib
import logging
import re
import time
from typing import Awaitable, Callable

import numpy as np
from google import genai
from google.genai import types

from .config import GEMINI_API_KEY, TRANSCRIBE_MODEL, TTS_MODEL, TTS_VOICE

log = logging.getLogger("jarvis.voice")
gemini = genai.Client(api_key=GEMINI_API_KEY)


def to_16k(pcm48: bytes) -> bytes:
    s = np.frombuffer(pcm48, dtype=np.int16)
    return s[: len(s) // 3 * 3].reshape(-1, 3).mean(axis=1).astype(np.int16).tobytes()


def to_48k(pcm24: bytes) -> bytes:
    """24 kHz mono -> 48 kHz stereo (what the call's external audio input expects)."""
    return np.repeat(np.frombuffer(pcm24, dtype=np.int16), 4).tobytes()


class Ears:
    """Streams my call audio to Gemini; calls back while I speak and when I finish a sentence."""

    def __init__(self, on_speaking: Callable[[str], Awaitable[None]],
                 on_sentence: Callable[[str], Awaitable[None]]):
        self.on_speaking = on_speaking      # interim text, used to interrupt Jarvis
        self.on_sentence = on_sentence      # final text after I pause
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run())

    def hear(self, pcm48: bytes):
        with contextlib.suppress(asyncio.QueueFull):
            self._audio.put_nowait(to_16k(pcm48))

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _run(self):
        config = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
        while True:  # sessions can drop or time out; reconnect for the whole call
            try:
                async with gemini.aio.live.connect(model=TRANSCRIBE_MODEL, config=config) as session:
                    log.info("ears connected")
                    sender = asyncio.create_task(self._send(session))
                    try:
                        await self._receive(session)
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ears dropped, reconnecting")
                await asyncio.sleep(1)

    async def _send(self, session):
        while True:
            chunk = await self._audio.get()
            while not self._audio.empty() and len(chunk) < 6400:   # batch to ~200 ms
                chunk += self._audio.get_nowait()
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))

    async def _receive(self, session):
        while True:
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interim_input_transcription and sc.interim_input_transcription.text:
                    await self.on_speaking(sc.interim_input_transcription.text)
                if sc.input_transcription and sc.input_transcription.text.strip():
                    await self.on_sentence(sc.input_transcription.text.strip())


async def transcribe_audio(data: bytes) -> str:
    """A whole recording (e.g. a Telegram voice message, any format ffmpeg reads) -> text.

    Uses the same live transcription model as calls (no daily limit on the free plan):
    streams the audio a bit faster than real time and collects every finished sentence.
    """
    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "quiet", "-i", "pipe:0", "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    pcm, _ = await ffmpeg.communicate(data)
    if not pcm:
        return ""
    pcm += bytes(16000 * 2 * 2)  # 2 s of silence so the last sentence gets finished
    config = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
    sentences: list[str] = []
    async with gemini.aio.live.connect(model=TRANSCRIBE_MODEL, config=config) as session:
        async def send():
            chunk = 6400  # 200 ms
            for i in range(0, len(pcm), chunk):
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm[i:i + chunk], mime_type="audio/pcm;rate=16000"))
                await asyncio.sleep(0.05)  # 4x real time

        async def receive(texts: asyncio.Queue):
            while True:  # receive() ends after each turn; long messages have several
                async for msg in session.receive():
                    sc = msg.server_content
                    if sc and sc.input_transcription and sc.input_transcription.text.strip():
                        await texts.put(sc.input_transcription.text.strip())

        texts: asyncio.Queue[str] = asyncio.Queue()
        sender, receiver = asyncio.create_task(send()), asyncio.create_task(receive(texts))
        deadline = time.monotonic() + len(pcm) / 32000 + 10   # never wait longer than this
        try:
            while time.monotonic() < deadline:
                try:
                    sentences.append(await asyncio.wait_for(texts.get(), 1.5))
                except TimeoutError:
                    if sender.done():   # everything sent and nothing new for 1.5 s: finished
                        break
        finally:
            sender.cancel()
            receiver.cancel()
    # the transcriber sometimes glues sentences together ("board.Second")
    return re.sub(r"([.!?])(?=[^\s\d.!?])", r"\1 ", " ".join(sentences))


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Cut finished sentences off the front of streamed text; return (sentences, rest)."""
    parts = _SENTENCE_END.split(buffer)
    return [p.strip() for p in parts[:-1] if p.strip()], parts[-1]


READER_PROMPT = ("You are a text-to-speech voice, not an assistant. Every message you get is text to "
                 "read aloud. Read it exactly as written, word for word, in a calm, refined, softly "
                 "confident voice, like a polished British butler (English with a British accent). "
                 "Never answer questions in it, never add or change words.")


class Mouth:
    """Turns text into Jarvis's voice and keeps it in a buffer the call plays from.

    Uses a Gemini Live model as a word-for-word reader: on the free plan Live models
    have no daily limit, while the TTS models allow only 10 requests a day.
    """

    # if a text has been sent but no audio and no turn-complete arrive within this long, the TTS
    # stream is treated as dead: the pending turn is cleared and the session restarted, so `busy`
    # can't get stuck on (which used to make the call ignore Sanjar as "echo")
    SILENCE_TIMEOUT = 8.0

    def __init__(self):
        self.out = bytearray()              # 48 kHz stereo audio waiting to be played
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending = 0                   # texts sent but not fully spoken yet
        self._spoken = 0                    # bytes of audio for the current text
        self._active = time.monotonic()     # last time a turn started / produced audio / completed
        self._task = asyncio.create_task(self._run())
        self._watchdog = asyncio.create_task(self._watch())

    @property
    def busy(self) -> bool:
        return bool(self.out) or self._pending > 0 or not self._queue.empty()

    @property
    def playing(self) -> bool:
        """Audio is actually going out to the call right now (buffer has sound). Use this, not
        `busy`, to tell Sanjar's voice from Jarvis's own echo: a pending-but-silent turn isn't echo."""
        return bool(self.out)

    def say(self, text: str):
        if text.strip():
            self._queue.put_nowait(text.strip())

    def stop(self):
        """I started talking: drop everything Jarvis was about to say.

        Audio already requested can't be cancelled mid-stream, so the session is restarted.
        """
        if not self.busy:
            return
        self._restart()

    def _restart(self):
        """Drop everything queued/pending and reconnect with a fresh TTS session."""
        while not self._queue.empty():
            self._queue.get_nowait()
        self.out.clear()
        self._pending = 0
        self._active = time.monotonic()
        self._task.cancel()
        self._task = asyncio.create_task(self._run())

    async def _watch(self):
        """If a turn was sent but nothing comes back (no audio, no turn-complete) for too long,
        the stream is dead: clear the stuck turn and reconnect so `busy` doesn't latch on."""
        while True:
            await asyncio.sleep(0.5)
            if self._pending > 0 and not self.out and time.monotonic() - self._active > self.SILENCE_TIMEOUT:
                log.error("TTS produced no audio for %.0fs; dropping %d stuck turn(s) and reconnecting",
                          self.SILENCE_TIMEOUT, self._pending)
                self._restart()

    async def wait_done(self, timeout: float = 20):
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                while self.busy:
                    await asyncio.sleep(0.1)

    async def close(self):
        self._task.cancel()
        self._watchdog.cancel()

    async def _run(self):
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=READER_PROMPT,
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE))))
        while True:
            self._pending = 0
            try:
                async with gemini.aio.live.connect(model=TTS_MODEL, config=config) as session:
                    sender = asyncio.create_task(self._send(session))
                    try:
                        await self._receive(session)
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mouth dropped, reconnecting")
                await asyncio.sleep(1)

    async def _send(self, session):
        while True:
            text = await self._queue.get()
            self._pending += 1
            self._spoken = 0
            self._active = time.monotonic()
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)

    async def _receive(self, session):
        while True:
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            self.out += to_48k(part.inline_data.data)
                            self._spoken += len(part.inline_data.data)
                            self._active = time.monotonic()
                if sc.turn_complete:
                    self._pending = max(0, self._pending - 1)
                    self._active = time.monotonic()
                    if self._spoken:
                        log.info("mouth spoke %.1f s of audio", self._spoken / 48000)  # 24 kHz s16
                    else:
                        log.error("TTS stream ended with no audio for a reply")
                    self._spoken = 0
