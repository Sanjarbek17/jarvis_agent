"""Ticket b9a14399: a clear voice message came back as "I couldn't make out that voice message".
The live transcriber sends a growing draft of each sentence and only sometimes a final; for the last
sentence the final often never comes, and the model can run seconds behind. transcribe_audio used to
keep finals only and stop 1.5 s after sending, so a one-sentence message lost everything."""
import asyncio
import io
import wave
from types import SimpleNamespace as NS

import numpy as np
import pytest

from core import voice


def wav(seconds=1.0, loud=True) -> bytes:
    t = np.arange(int(16000 * seconds)) / 16000
    pcm = (np.sin(2 * np.pi * 220 * t) * (8000 if loud else 0)).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(pcm.tobytes())
    return out.getvalue()


def msg(draft=None, final=None):
    return NS(server_content=NS(interim_input_transcription=NS(text=draft) if draft else None,
                                input_transcription=NS(text=final) if final else None))


class FakeSession:
    def __init__(self, script, fail=None):
        self.script, self.fail = script, fail      # script: [(delay after connect, message)]

    async def send_realtime_input(self, **kw):
        pass

    async def receive(self):
        if self.fail:
            raise self.fail
        start = asyncio.get_running_loop().time()
        for at, m in self.script:
            await asyncio.sleep(max(0, start + at - asyncio.get_running_loop().time()))
            yield m
        await asyncio.sleep(3600)


def fake_gemini(monkeypatch, session):
    class Conn:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(voice, "gemini", NS(aio=NS(live=NS(connect=lambda **kw: Conn()))))
    monkeypatch.setattr(voice, "DRAFT_SETTLE", 0.6)


def test_a_sentence_that_never_gets_a_final_is_kept(monkeypatch):
    repeats = [(0.2 + 0.1 * i, msg(draft="What is the status")) for i in range(3)]
    repeats += [(0.6 + 0.1 * i, msg(draft="What is the status of the taxi project?")) for i in range(30)]
    fake_gemini(monkeypatch, FakeSession(repeats))         # the same draft over and over, no final
    assert asyncio.run(voice.transcribe_audio(wav())) == "What is the status of the taxi project?"


def test_finals_plus_the_last_draft_and_a_model_running_behind(monkeypatch):
    script = [(0.3, msg(draft="First one")), (0.5, msg(final="First one.")),
              # sending ends after ~0.75 s; the model is still working on the next sentence for 2 s
              (1.2, msg(draft="Second")), (1.9, msg(draft="Second one")), (2.6, msg(draft="Second one is here"))]
    fake_gemini(monkeypatch, FakeSession(script))
    assert asyncio.run(voice.transcribe_audio(wav())) == "First one. Second one is here"


def test_a_service_error_is_a_failure_not_an_empty_text(monkeypatch):
    fake_gemini(monkeypatch, FakeSession([], fail=RuntimeError("1011 You exceeded your current quota")))
    with pytest.raises(voice.TranscriptionFailed, match="quota"):
        asyncio.run(voice.transcribe_audio(wav()))


def test_speech_with_no_text_back_is_a_failure(monkeypatch):
    fake_gemini(monkeypatch, FakeSession([]))
    with pytest.raises(voice.TranscriptionFailed, match="no text"):
        asyncio.run(voice.transcribe_audio(wav()))


def test_undecodable_audio_is_a_failure():
    with pytest.raises(voice.TranscriptionFailed, match="decode"):
        asyncio.run(voice.transcribe_audio(b"not audio at all"))


def test_a_silent_recording_is_empty_text(monkeypatch):
    fake_gemini(monkeypatch, FakeSession([], fail=AssertionError("must not even ask the service")))
    assert asyncio.run(voice.transcribe_audio(wav(loud=False))) == ""
