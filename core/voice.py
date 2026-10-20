"""Ears (Gemini live transcription) and mouth (Gemini text-to-speech) for calls.

Audio from the call is 48 kHz mono s16le; audio into the call is 48 kHz stereo.
Gemini hears 16 kHz and speaks 24 kHz.
"""
import asyncio
import contextlib
import logging
import re
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

    def __init__(self):
        self.out = bytearray()              # 48 kHz stereo audio waiting to be played
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending = 0                   # texts sent but not fully spoken yet
        self._spoken = 0                    # bytes of audio for the current text
        self._task = asyncio.create_task(self._run())

    @property
    def busy(self) -> bool:
        return bool(self.out) or self._pending > 0 or not self._queue.empty()

    def say(self, text: str):
        if text.strip():
            self._queue.put_nowait(text.strip())

    def stop(self):
        """I started talking: drop everything Jarvis was about to say.

        Audio already requested can't be cancelled mid-stream, so the session is restarted.
        """
        if not self.busy:
            return
        while not self._queue.empty():
            self._queue.get_nowait()
        self.out.clear()
        self._task.cancel()
        self._task = asyncio.create_task(self._run())

    async def wait_done(self, timeout: float = 20):
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                while self.busy:
                    await asyncio.sleep(0.1)

    async def close(self):
        self._task.cancel()

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
                if sc.turn_complete:
                    self._pending = max(0, self._pending - 1)
                    log.info("mouth spoke %.1f s of audio", self._spoken / 48000)  # 24 kHz s16
                    self._spoken = 0
